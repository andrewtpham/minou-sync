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

from minou_sync import cli, importer, reconcile
from minou_sync.importer import detect_kind, load_profile, parse_original
from minou_sync.types import normalise

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


# FIT base types: (size, base type id, struct code).
_UINT8, _UINT16, _UINT32, _ENUM = (1, 0x02, "B"), (2, 0x84, "H"), (4, 0x86, "I"), (1, 0x00, "B")
# Session (global 18) fields: name -> (field number, base type, scale).
_SESSION_FIELDS = {
    "start_time": (2, _UINT32, 1), "sport": (5, _ENUM, 1), "sub_sport": (6, _ENUM, 1),
    "total_elapsed_time": (7, _UINT32, 1000), "total_timer_time": (8, _UINT32, 1000),
    "total_distance": (9, _UINT32, 100), "total_calories": (11, _UINT16, 1),
    "avg_heart_rate": (16, _UINT8, 1), "max_heart_rate": (17, _UINT8, 1), "avg_power": (20, _UINT16, 1),
    "total_ascent": (22, _UINT16, 1), "total_moving_time": (59, _UINT32, 1000),
}
# Activity (global 34) fields.
_ACTIVITY_FIELDS = {"timestamp": (253, _UINT32, 1), "local_timestamp": (5, _UINT32, 1)}


def _fit_time(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp()) - FIT_EPOCH


def _fit_message(global_num: int, fields: dict, values: dict) -> bytes:
    """A definition message (local type 0, redefined each time) and its data message."""
    present = [(fields[name], value) for name, value in values.items()]
    record = struct.pack("<BBBHB", 0x40, 0, 0, global_num, len(present))
    for (num, (size, base, _), _), _ in present:
        record += struct.pack("<BBB", num, size, base)
    record += b"\x00"
    for (_, (_, _, code), scale), value in present:
        record += struct.pack("<" + code, round(value * scale))
    return record


def make_fit_file(sessions: list[dict], activity: dict | None = None) -> bytes:
    """A FIT file with the given session messages and an optional activity message.

    Times are naive UTC datetimes; other values are in FIT's units (s, m, bpm, W, kcal) before scaling.
    """
    records = b""
    for session in sessions:
        values = {k: (_fit_time(v) if k == "start_time" else v) for k, v in session.items()}
        records += _fit_message(18, _SESSION_FIELDS, values)
    if activity:
        records += _fit_message(34, _ACTIVITY_FIELDS, {k: _fit_time(v) for k, v in activity.items()})
    header = struct.pack("<BBHI4s", 12, 0x10, 2140, len(records), b".FIT")
    body = header + records
    return body + struct.pack("<H", _fit_crc(body))


def make_fit(start: datetime, elapsed_s: int, distance_m: float, sport: int = 1) -> bytes:
    """A FIT file with one session message. sport: 1 running, 2 cycling."""
    return make_fit_file([{"start_time": start, "total_elapsed_time": elapsed_s, "total_distance": distance_m,
                           "sport": sport}])


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


def test_parse_fit_reads_the_full_session_summary(tmp_path):
    # A made-up trail run recorded in UTC+2: every summary field the session carries comes through.
    start = datetime(2026, 10, 1, 6, 0, 0)
    fit = tmp_path / "a.fit"
    fit.write_bytes(make_fit_file(
        [{"start_time": start, "sport": 1, "sub_sport": 3, "total_elapsed_time": 3700, "total_timer_time": 3600,
          "total_distance": 10000.0, "total_ascent": 250, "avg_heart_rate": 150, "max_heart_rate": 178,
          "avg_power": 260, "total_calories": 700}],
        activity={"timestamp": datetime(2026, 10, 1, 7, 2, 0), "local_timestamp": datetime(2026, 10, 1, 9, 2, 0)},
    ))
    s = parse_original(fit)
    assert s.start_utc == "2026-10-01T06:00:00Z" and s.start_local == "2026-10-01T08:00:00"
    assert s.sport == "trail running"
    assert (s.elapsed_s, s.moving_s, s.distance_m, s.elev_gain_m) == (3700, 3600, 10000.0, 250)
    assert (s.avg_hr, s.max_hr, s.avg_watts, s.calories) == (150, 178, 260, 700)


def test_parse_fit_prefers_total_moving_time(tmp_path):
    fit = tmp_path / "a.fit"
    fit.write_bytes(make_fit_file([{"start_time": datetime(2026, 10, 1, 6, 0), "sport": 2,
                                    "total_elapsed_time": 4000, "total_timer_time": 3800,
                                    "total_moving_time": 3500}]))
    assert parse_original(fit).moving_s == 3500


def test_parse_fit_missing_fields_stay_none(tmp_path):
    # A file with only the basics (no HR strap, no power, no activity message) leaves the rest empty.
    fit = tmp_path / "a.fit"
    fit.write_bytes(make_fit(datetime(2026, 10, 1, 6, 0), elapsed_s=1800, distance_m=5000.0))
    s = parse_original(fit)
    assert (s.start_local, s.avg_hr, s.max_hr, s.avg_watts, s.calories, s.elev_gain_m) == (None,) * 6
    assert s.moving_s is None


def test_parse_fit_multisport_is_one_activity(tmp_path):
    # Swim, transition, ride: one activity spanning all three, with summed totals and timer-weighted averages.
    t0 = datetime(2026, 10, 1, 6, 0, 0)
    fit = tmp_path / "tri.fit"
    fit.write_bytes(make_fit_file([
        {"start_time": t0, "sport": 5, "total_elapsed_time": 1800, "total_timer_time": 1800,
         "total_distance": 1500.0, "avg_heart_rate": 140, "max_heart_rate": 160, "total_calories": 300},
        {"start_time": datetime(2026, 10, 1, 6, 30, 0), "sport": 3, "total_elapsed_time": 120,
         "total_timer_time": 120},
        {"start_time": datetime(2026, 10, 1, 6, 32, 0), "sport": 2, "total_elapsed_time": 3600,
         "total_timer_time": 3600, "total_distance": 40000.0, "avg_heart_rate": 150, "max_heart_rate": 175,
         "avg_power": 220, "total_calories": 900},
    ]))
    s = parse_original(fit)
    assert s.sport == "multisport" and s.start_utc == "2026-10-01T06:00:00Z"
    assert s.elapsed_s == 1800 + 120 + 3600 and s.moving_s == 1800 + 120 + 3600
    assert s.distance_m == 41500.0 and s.calories == 1200 and s.max_hr == 175
    assert s.avg_hr == round((140 * 1800 + 150 * 3600) / 5400)
    assert s.avg_watts == 220  # only the ride has power


def test_parse_fit_ignores_an_implausible_local_offset(tmp_path):
    # A device clock 7 minutes off isn't a time zone, so no local start is guessed.
    fit = tmp_path / "a.fit"
    fit.write_bytes(make_fit_file(
        [{"start_time": datetime(2026, 10, 1, 6, 0), "sport": 1, "total_elapsed_time": 1800}],
        activity={"timestamp": datetime(2026, 10, 1, 6, 30), "local_timestamp": datetime(2026, 10, 1, 6, 37)},
    ))
    assert parse_original(fit).start_local is None


def test_unreadable_fit_is_skipped_and_counted(conn, data_dir, tmp_path):
    folder = tmp_path / "export"
    folder.mkdir()
    (folder / "broken.fit").write_bytes(b"not a fit file at all")
    (folder / "a.fit").write_bytes(make_fit(datetime(2026, 10, 1, 6, 0), 1800, 5000.0))
    counts = importer.run(conn, folder, "coros", data_dir)
    assert counts.added == 1 and counts.failed == 1


@pytest.mark.parametrize("pair", sorted(importer._FIT_SUB_SPORTS))
def test_every_fit_sub_sport_maps_to_a_known_type(pair):
    assert normalise("garmin", importer._fit_type(*pair)) != "other"


@pytest.mark.parametrize("sport", ["running", "cycling", "swimming", "walking", "hiking", "rowing", "multisport",
                                   "e_biking", "rock_climbing", "alpine_skiing", "cross_country_skiing",
                                   "snowboarding", "snowshoeing", "mountaineering", "tennis", "floor_climbing"])
def test_fit_sports_map_to_a_known_type(sport):
    assert normalise("garmin", importer._fit_type(sport, "generic")) != "other"


def test_coros_export_folder_imports_with_full_summaries(conn, data_dir, tmp_path):
    # A made-up COROS Training Hub export: a zip of FIT files, imported under --source coros and reconciled.
    zpath = tmp_path / "coros-export.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("471234567890.fit", make_fit_file([
            {"start_time": datetime(2026, 10, 3, 7, 0), "sport": 1, "sub_sport": 1, "total_elapsed_time": 2400,
             "total_timer_time": 2350, "total_distance": 6000.0, "avg_heart_rate": 152, "max_heart_rate": 171}]))
        z.writestr("471234567891.fit", make_fit_file([
            {"start_time": datetime(2026, 10, 4, 8, 0), "sport": 10, "sub_sport": 20, "total_elapsed_time": 2700,
             "total_timer_time": 2700, "total_calories": 250}]))
    counts = importer.run(conn, zpath, "coros", data_dir)
    assert counts.added == 2 and counts.files == 2
    rows = conn.execute("SELECT activity_type, moving_s, avg_hr, max_hr, calories FROM source_activities "
                        "WHERE source = 'coros' ORDER BY start_utc").fetchall()
    assert tuple(rows[0]) == ("treadmill running", 2350, 152, 171, None)
    assert tuple(rows[1]) == ("strength training", 2700, None, None, 250)
    reconcile.run(conn, data_dir / "reports")
    types = [r[0] for r in conn.execute("SELECT activity_type FROM activities WHERE source = 'coros' ORDER BY start_utc")]
    assert types == ["run_treadmill", "strength"]


def test_reimport_fills_rows_the_earlier_fit_reader_left_empty(conn, data_dir, tmp_path):
    # The earlier reader stored only start, elapsed, distance and sport. A re-import updates that row in place.
    folder = tmp_path / "export"
    folder.mkdir()
    (folder / "a.fit").write_bytes(make_fit_file([{"start_time": datetime(2026, 10, 1, 6, 0), "sport": 1,
                                                   "total_elapsed_time": 1800, "avg_heart_rate": 148}]))
    importer.run(conn, folder, "coros", data_dir)
    conn.execute("UPDATE source_activities SET avg_hr = NULL")
    conn.commit()
    counts = importer.run(conn, folder, "coros", data_dir)
    assert (counts.added, counts.updated) == (0, 1)
    assert tuple(conn.execute("SELECT COUNT(*), MAX(avg_hr) FROM source_activities").fetchone()) == (1, 148)


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
