"""Strava importer against a made-up archive laid out like a real Strava export (activities.csv + activities/)."""

import csv
import gzip
import hashlib
import io
import zipfile

import pytest

from minou_sync import cli, import_strava
from minou_sync.import_strava import NotAnExportError, parse_row, read_rows

# The real column order for the fields used, including the five repeated names (display units, then metric).
HEADER = [
    "Activity ID", "Activity Date", "Activity Name", "Activity Type", "Activity Description", "Elapsed Time",
    "Distance", "Max Heart Rate", "Relative Effort", "Commute", "Filename", "Elapsed Time", "Moving Time",
    "Distance", "Elevation Gain", "Max Heart Rate", "Average Heart Rate", "Average Watts", "Calories",
    "Relative Effort", "Commute",
]


def strava_row(activity_id, date="Oct 7, 2026, 6:30:00 AM", type_="Ride", filename=None, **metric):
    m = {"elapsed": "3600.0", "moving": "3500.0", "distance": "20500.0", "elev": "120.0", "max_hr": "170.0",
         "avg_hr": "140.0", "watts": "180.0", "calories": "700.0"} | metric
    try:
        display_km = str(round(float(m["distance"]) / 1000, 2)).replace(".", ",")
    except ValueError:
        display_km = m["distance"]
    return [str(activity_id), date, f"Made-up ride {activity_id}", type_, "", "3600", display_km, m["max_hr"], "50",
            "false", filename if filename is not None else f"activities/{activity_id + 900}.fit.gz",
            m["elapsed"], m["moving"], m["distance"], m["elev"], m["max_hr"], m["avg_hr"], m["watts"],
            m["calories"], "50.0", "0.0"]


def write_archive(path, rows, files=None, as_folder=False):
    buf = io.StringIO()
    csv.writer(buf).writerows([HEADER, *rows])
    files = files if files is not None else {r[10]: gzip.compress(f"fake fit {r[0]}".encode()) for r in rows if r[10]}
    members = {"activities.csv": buf.getvalue().encode(), "profile.csv": b"not read", **files}
    if as_folder:
        for name, content in members.items():
            (path / name).parent.mkdir(parents=True, exist_ok=True)
            (path / name).write_bytes(content)
    else:
        with zipfile.ZipFile(path, "w") as z:
            for name, content in members.items():
                z.writestr(name, content)
    return path


def test_read_rows_keeps_metric_value_and_display_copy():
    buf = io.StringIO()
    csv.writer(buf).writerows([HEADER, strava_row(1)])
    row = read_rows(buf.getvalue())[0]
    assert row["Distance"] == "20500.0" and row["Distance (display)"] == "20,5"
    assert row["Elapsed Time"] == "3600.0" and row["Elapsed Time (display)"] == "3600"


def test_parse_row_uses_utc_and_metric_columns():
    buf = io.StringIO()
    csv.writer(buf).writerows([HEADER, strava_row(1, date="Dec 3, 2019, 5:04:05 PM", avg_hr="", watts="n/a")])
    counts = import_strava.ImportCounts()
    row = parse_row(read_rows(buf.getvalue())[0], "2026-10-19T00:00:00Z", counts)
    assert row["start_utc"] == "2019-12-03T17:04:05Z"
    assert (row["elapsed_s"], row["moving_s"], row["distance_m"]) == (3600, 3500, 20500.0)
    assert row["avg_hr"] is None and row["max_hr"] == 170
    assert row["avg_watts"] is None and counts.null_fields == 1  # unparseable, not just empty
    assert row["description"] is None


def test_import_zip_loads_rows_and_decompresses_files(conn, data_dir, tmp_path):
    rows = [
        strava_row(1),
        strava_row(2, type_="Run", filename="activities/902.gpx.gz"),
        strava_row(3, filename=""),  # manual entry, no file
        strava_row(4, date="not a date"),
    ]
    archive = write_archive(tmp_path / "export_1.zip", rows)
    counts = import_strava.run(conn, archive, data_dir)

    assert (counts.added, counts.failed, counts.files) == (3, 1, 2)
    assert [r[0] for r in conn.execute("SELECT strava_id FROM strava_activities ORDER BY strava_id")] == [1, 2, 3]
    f = conn.execute("SELECT * FROM files WHERE source = 'strava' AND source_id = 2").fetchone()
    content = (data_dir / f["path"]).read_bytes()
    assert f["path"] == "files/strava/2.gpx" and f["format"] == "gpx"
    assert content == b"fake fit 2" and f["sha256"] == hashlib.sha256(content).hexdigest()
    raw = conn.execute("SELECT raw_json FROM strava_activities WHERE strava_id = 1").fetchone()[0]
    assert '"Distance (display)": "20,5"' in raw


def test_import_folder_works_like_zip(conn, data_dir, tmp_path):
    folder = tmp_path / "export_1"
    folder.mkdir()
    write_archive(folder, [strava_row(1)], as_folder=True)
    counts = import_strava.run(conn, folder, data_dir)
    assert (counts.added, counts.files) == (1, 1)


def test_progress_callback_is_called_per_row(conn, data_dir, tmp_path):
    archive = write_archive(tmp_path / "export_1.zip", [strava_row(1), strava_row(2), strava_row(3)])
    calls = []
    import_strava.run(conn, archive, data_dir, progress=lambda done, total: calls.append((done, total)))
    assert calls == [(1, 3), (2, 3), (3, 3)]


def test_second_import_changes_nothing(conn, data_dir, tmp_path):
    archive = write_archive(tmp_path / "export_1.zip", [strava_row(1), strava_row(2)])
    import_strava.run(conn, archive, data_dir)
    counts = import_strava.run(conn, archive, data_dir)
    assert (counts.added, counts.updated, counts.unchanged, counts.files) == (0, 0, 2, 0)


def test_listed_file_missing_from_archive_is_logged(conn, data_dir, tmp_path, caplog):
    archive = write_archive(tmp_path / "export_1.zip", [strava_row(1)], files={})
    counts = import_strava.run(conn, archive, data_dir)
    assert (counts.added, counts.files) == (1, 0)
    assert "not in the archive" in caplog.text


def test_not_an_export_is_refused(data_dir, tmp_path, capsys):
    bogus = tmp_path / "other.zip"
    with zipfile.ZipFile(bogus, "w") as z:
        z.writestr("readme.txt", "hello")
    with pytest.raises(NotAnExportError):
        import_strava.run(None, bogus, data_dir)
    assert cli.main(["--data-dir", str(data_dir), "import-strava", str(bogus)]) == 1
    assert "doesn't look like a Strava export: activities.csv not found" in capsys.readouterr().err


def test_cli_import_then_reconcile_merges_with_garmin(conn, data_dir, tmp_path, capsys):
    conn.execute(
        "INSERT INTO garmin_activities (garmin_id, activity_type, start_utc, elapsed_s, origin, raw_json, imported_utc)"
        " VALUES (50, 'indoor_cycling', '2026-10-07T06:30:30Z', 3620, 'sync', '{}', '2026-10-19T00:00:00Z')"
    )
    conn.commit()
    archive = write_archive(tmp_path / "export_1.zip", [strava_row(1), strava_row(2, date="May 4, 2019, 8:00:00 AM")])
    assert cli.main(["--data-dir", str(data_dir), "import-strava", str(archive)]) == 0
    assert "2 added" in capsys.readouterr().out
    assert cli.main(["--data-dir", str(data_dir), "reconcile"]) == 0
    sources = dict(conn.execute("SELECT source, COUNT(*) FROM activities GROUP BY source").fetchall())
    assert sources == {"both": 1, "strava": 1}
