"""General importer against made-up CSVs, generated FIT/GPX/TCX files, and the shipped profiles.

No real activity or health values: every number here is invented.
"""

import csv
import gzip
import io
import struct
import zipfile
from datetime import datetime, timezone

import pytest

from minou_sync import cli, importer
from minou_sync.importer import detect_kind, load_profile, parse_original

# --- FIT encoder (a minimal valid file with one session message) ----------------------------------------

FIT_EPOCH = 631065600  # seconds between the Unix epoch and the FIT epoch (1989-12-31 UTC)


def _fit_crc(data: bytes) -> int:
    table = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
             0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]
    crc = 0
    for byte in data:
        for nibble in (byte & 0xF, (byte >> 4) & 0xF):
            tmp = table[crc & 0xF]
            crc = (crc >> 4) & 0x0FFF
            crc = crc ^ tmp ^ table[nibble]
    return crc


def make_fit(start: datetime, elapsed_s: int, distance_m: float, sport: int = 1) -> bytes:
    """A FIT file with one session message. sport: 1 running, 2 cycling."""
    start_fit = int(start.replace(tzinfo=timezone.utc).timestamp()) - FIT_EPOCH
    # Definition message for session (global 18): start_time, total_elapsed_time, total_distance, sport.
    local = 0
    definition = struct.pack("<BBBHB", 0x40, 0, 0, 18, 4)
    definition += struct.pack("<BBB", 2, 4, 0x86)   # start_time: uint32
    definition += struct.pack("<BBB", 7, 4, 0x86)   # total_elapsed_time: uint32
    definition += struct.pack("<BBB", 9, 4, 0x86)   # total_distance: uint32
    definition += struct.pack("<BBB", 5, 1, 0x00)   # sport: enum
    data = struct.pack("<B", local)
    data += struct.pack("<III", start_fit, elapsed_s * 1000, round(distance_m * 100))
    data += struct.pack("<B", sport)
    records = definition + data

    header = struct.pack("<BBHI4s", 12, 0x10, 2140, len(records), b".FIT")
    body = header + records
    return body + struct.pack("<H", _fit_crc(body))


def make_gpx(start: datetime, points: int = 3, step_s: int = 60) -> bytes:
    pts = []
    for i in range(points):
        t = datetime.fromtimestamp(start.replace(tzinfo=timezone.utc).timestamp() + i * step_s, tz=timezone.utc)
        pts.append(f'<trkpt lat="52.5" lon="13.4"><time>{t.isoformat().replace("+00:00", "Z")}</time></trkpt>')
    xml = (
        '<?xml version="1.0"?>'
        '<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>'
        + "".join(pts) +
        '</trkseg></trk></gpx>'
    )
    return xml.encode()


def make_tcx(start: datetime, total_s: int, sport: str = "Running") -> bytes:
    iso = start.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    xml = (
        '<?xml version="1.0"?>'
        '<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">'
        f'<Activities><Activity Sport="{sport}"><Id>{iso}</Id>'
        f'<Lap StartTime="{iso}"><TotalTimeSeconds>{total_s}</TotalTimeSeconds>'
        '<DistanceMeters>5000</DistanceMeters></Lap>'
        '</Activity></Activities></TrainingCenterDatabase>'
    )
    return xml.encode()


# --- CSV helpers ----------------------------------------------------------------------------------------

def write_csv(path, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    path.write_text(buf.getvalue())
    return path


ACTIVITY_HEADER = ["id", "type", "name", "start_time", "elapsed_s", "distance_m", "avg_hr"]
WELLNESS_HEADER = ["date", "resting_hr", "sleep_s", "steps"]
BODY_HEADER = ["measured_on", "method", "weight_kg", "body_fat_pct"]
LAB_HEADER = ["tested_on", "sport", "lt2_hr", "lt2_watts"]


# --- Profiles -------------------------------------------------------------------------------------------

def test_load_builtin_and_detect_kind():
    assert load_profile("minou-sync").kind == "activities"
    assert detect_kind(ACTIVITY_HEADER).name == "minou-sync"
    assert detect_kind(WELLNESS_HEADER).kind == "wellness"
    assert detect_kind(BODY_HEADER).kind == "body_composition"
    assert detect_kind(LAB_HEADER).kind == "lab_tests"


def test_detect_kind_rejects_unknown_header():
    with pytest.raises(importer.NoProfileError):
        detect_kind(["foo", "bar"])


def test_load_profile_from_toml_path(tmp_path):
    p = tmp_path / "custom.toml"
    p.write_text('kind = "activities"\ndate_format = "iso"\ntimezone = "utc"\n'
                 '[columns]\nstart_time = "when"\ntype = "kind"\nrequired = []\n[units]\n')
    prof = load_profile(str(p))
    assert prof.kind == "activities" and prof.columns["start_time"] == "when"


# --- CSV import -----------------------------------------------------------------------------------------

def test_import_activity_csv(conn, data_dir, tmp_path):
    csv_path = write_csv(tmp_path / "runs.csv", ACTIVITY_HEADER, [
        ["a1", "Run", "Morning run", "2026-10-01T06:00:00Z", "1800", "5000", "150"],
        ["a2", "Ride", "Commute", "2026-10-02T07:30:00+02:00", "2400", "12000", "120"],
    ])
    counts = importer.run(conn, csv_path, "nrc", data_dir)
    assert counts.added == 2 and counts.kinds["activities"] == 2
    rows = conn.execute("SELECT source, source_id, activity_type, start_utc, elapsed_s, distance_m, avg_hr "
                        "FROM source_activities ORDER BY source_id").fetchall()
    assert tuple(rows[0]) == ("nrc", "a1", "Run", "2026-10-01T06:00:00Z", 1800, 5000.0, 150)
    # Offset time is converted to UTC.
    assert rows[1]["start_utc"] == "2026-10-02T05:30:00Z"


def test_import_wellness_body_and_lab_csvs(conn, data_dir, tmp_path):
    importer.run(conn, write_csv(tmp_path / "w.csv", WELLNESS_HEADER,
                                 [["2026-10-01", "48", "25000", "9000"]]), "intervals", data_dir)
    importer.run(conn, write_csv(tmp_path / "b.csv", BODY_HEADER,
                                 [["2026-10-01", "DEXA", "72.0", "14.5"]]), "clinic", data_dir)
    importer.run(conn, write_csv(tmp_path / "l.csv", LAB_HEADER,
                                 [["2026-09-15", "run", "168", "300"]]), "clinic", data_dir)
    assert tuple(conn.execute("SELECT resting_hr, steps, source FROM daily_wellness WHERE date='2026-10-01'")
                 .fetchone()) == (48, 9000, "intervals")
    assert tuple(conn.execute("SELECT method, weight_kg FROM body_composition").fetchone()) == ("dexa", 72.0)
    assert tuple(conn.execute("SELECT sport, lt2_hr FROM lab_tests").fetchone()) == ("run", 168)


def test_missing_required_column_rejected_before_any_write(conn, data_dir, tmp_path):
    # The activities template needs start_time; a file without it is refused and writes nothing.
    bad = write_csv(tmp_path / "bad.csv", ["id", "type", "elapsed_s"], [["a1", "Run", "1800"]])
    with pytest.raises(importer.MissingColumnError, match="start_time"):
        importer.run(conn, bad, "nrc", data_dir, profile="minou-sync")
    assert conn.execute("SELECT COUNT(*) FROM source_activities").fetchone()[0] == 0


def test_unparseable_number_becomes_null_and_is_counted(conn, data_dir, tmp_path):
    csv_path = write_csv(tmp_path / "runs.csv", ACTIVITY_HEADER,
                         [["a1", "Run", "x", "2026-10-01T06:00:00Z", "n/a", "5000", ""]])
    counts = importer.run(conn, csv_path, "nrc", data_dir)
    row = conn.execute("SELECT elapsed_s, avg_hr FROM source_activities").fetchone()
    assert row["elapsed_s"] is None and row["avg_hr"] is None and counts.null_fields == 1


def test_garmin_wellness_wins_over_imported_date(conn, data_dir, tmp_path):
    conn.execute("INSERT INTO daily_wellness (date, steps, source, updated_utc) "
                 "VALUES ('2026-10-01', 5000, 'garmin', '2026-10-19T00:00:00Z')")
    conn.commit()
    importer.run(conn, write_csv(tmp_path / "w.csv", WELLNESS_HEADER,
                                 [["2026-10-01", "48", "25000", "1"]]), "intervals", data_dir)
    row = conn.execute("SELECT steps, source FROM daily_wellness WHERE date='2026-10-01'").fetchone()
    assert row["steps"] == 5000 and row["source"] == "garmin"  # Garmin's row is untouched


# --- Original files -------------------------------------------------------------------------------------

def test_parse_fit_gpx_tcx(tmp_path):
    start = datetime(2026, 10, 1, 6, 0, 0)
    fit = tmp_path / "a.fit"
    fit.write_bytes(make_fit(start, elapsed_s=1800, distance_m=5000.0, sport=2))
    s = parse_original(fit)
    assert s.start_utc == "2026-10-01T06:00:00Z" and s.elapsed_s == 1800 and s.distance_m == 5000.0
    assert s.sport == "cycling"

    gpx = tmp_path / "a.gpx"
    gpx.write_bytes(make_gpx(start, points=3, step_s=60))
    g = parse_original(gpx)
    assert g.start_utc == "2026-10-01T06:00:00Z" and g.elapsed_s == 120

    tcx = tmp_path / "a.tcx"
    tcx.write_bytes(make_tcx(start, total_s=1800, sport="Running"))
    t = parse_original(tcx)
    assert t.start_utc == "2026-10-01T06:00:00Z" and t.elapsed_s == 1800 and t.sport == "Running"


def test_parse_gzipped_fit(tmp_path):
    raw = make_fit(datetime(2026, 10, 1, 6, 0, 0), elapsed_s=1800, distance_m=5000.0)
    gz = tmp_path / "a.fit.gz"
    gz.write_bytes(gzip.compress(raw))
    assert parse_original(gz).start_utc == "2026-10-01T06:00:00Z"


def test_import_folder_of_original_files(conn, data_dir, tmp_path):
    folder = tmp_path / "export"
    folder.mkdir()
    (folder / "a.fit").write_bytes(make_fit(datetime(2026, 10, 1, 6, 0), 1800, 5000.0, sport=1))
    (folder / "b.gpx.gz").write_bytes(gzip.compress(make_gpx(datetime(2026, 10, 2, 7, 0), points=2, step_s=120)))
    (folder / "c.tcx").write_bytes(make_tcx(datetime(2026, 10, 3, 8, 0), total_s=2400))
    counts = importer.run(conn, folder, "coros", data_dir)
    assert counts.files == 3
    rows = conn.execute("SELECT start_utc, activity_type, elapsed_s FROM source_activities ORDER BY start_utc").fetchall()
    assert len(rows) == 3
    assert rows[0]["start_utc"] == "2026-10-01T06:00:00Z" and rows[0]["activity_type"] == "running"
    files = conn.execute("SELECT path, format FROM files WHERE source='coros' ORDER BY path").fetchall()
    assert {f["format"] for f in files} == {"fit", "gpx", "tcx"}
    for f in files:
        assert (data_dir / f["path"]).is_file()


def test_progress_callback_counts_original_files(conn, data_dir, tmp_path):
    folder = tmp_path / "export"
    folder.mkdir()
    (folder / "a.fit").write_bytes(make_fit(datetime(2026, 10, 1, 6, 0), 1800, 5000.0, sport=1))
    (folder / "b.tcx").write_bytes(make_tcx(datetime(2026, 10, 3, 8, 0), total_s=2400))
    calls = []
    importer.run(conn, folder, "coros", data_dir, progress=lambda done, total: calls.append((done, total)))
    assert calls == [(1, 2), (2, 2)]


def test_import_zip_mixes_csv_and_files(conn, data_dir, tmp_path):
    zpath = tmp_path / "export.zip"
    buf = io.StringIO()
    csv.writer(buf).writerows([ACTIVITY_HEADER, ["a1", "Run", "x", "2026-10-01T06:00:00Z", "1800", "5000", "150"]])
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("activities.csv", buf.getvalue())
        z.writestr("files/a.fit", make_fit(datetime(2026, 10, 2, 6, 0), 1800, 5000.0))
    counts = importer.run(conn, zpath, "mix", data_dir)
    assert counts.kinds["activities"] == 1 and counts.files == 1
    assert conn.execute("SELECT COUNT(*) FROM source_activities").fetchone()[0] == 2


def test_reimport_changes_nothing(conn, data_dir, tmp_path):
    folder = tmp_path / "export"
    folder.mkdir()
    write_csv(folder / "runs.csv", ACTIVITY_HEADER,
              [["a1", "Run", "x", "2026-10-01T06:00:00Z", "1800", "5000", "150"]])
    (folder / "a.fit").write_bytes(make_fit(datetime(2026, 10, 2, 6, 0), 1800, 5000.0))
    importer.run(conn, folder, "nrc", data_dir)
    before = {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
              for t in ("source_activities", "files", "daily_wellness")}
    counts = importer.run(conn, folder, "nrc", data_dir)
    after = {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
             for t in ("source_activities", "files", "daily_wellness")}
    assert {t: [tuple(r) for r in rows] for t, rows in before.items()} == \
           {t: [tuple(r) for r in rows] for t, rows in after.items()}
    assert counts.added == counts.updated == counts.files == 0


# --- CLI ------------------------------------------------------------------------------------------------

def test_cli_import(data_dir, tmp_path, capsys):
    csv_path = write_csv(tmp_path / "runs.csv", ACTIVITY_HEADER,
                         [["a1", "Run", "x", "2026-10-01T06:00:00Z", "1800", "5000", "150"]])
    assert cli.main(["--data-dir", str(data_dir), "import", str(csv_path), "--source", "nrc"]) == 0
    assert "1 added" in capsys.readouterr().out


def test_cli_import_rejects_missing_column(data_dir, tmp_path, capsys):
    bad = write_csv(tmp_path / "bad.csv", ["id", "type"], [["a1", "Run"]])
    code = cli.main(["--data-dir", str(data_dir), "import", str(bad), "--source", "nrc", "--profile", "minou-sync"])
    assert code == 1 and "start_time" in capsys.readouterr().err
