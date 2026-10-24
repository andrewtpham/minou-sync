"""Garmin account export import against a made-up export built in the test. Every value here is invented."""

import io
import json
import logging
import struct
import zipfile
from datetime import datetime, timezone

import pytest

from minou_sync import cli, db, import_garmin, reconcile
from minou_sync.import_garmin import parse_activity

from test_importer import FIT_EPOCH, _fit_crc

ROOT = "DI_CONNECT/"


def make_file_id_fit(created: datetime, file_type: int = 4) -> bytes:
    """A FIT file with only a file_id message. file_type 4 is an activity, 32 all-day monitoring."""
    t = int(created.replace(tzinfo=timezone.utc).timestamp()) - FIT_EPOCH
    definition = struct.pack("<BBBHB", 0x40, 0, 0, 0, 2)  # file_id is global message 0
    definition += struct.pack("<BBB", 0, 1, 0x00)         # type: enum
    definition += struct.pack("<BBB", 4, 4, 0x86)         # time_created: uint32
    records = definition + struct.pack("<BBI", 0, file_type, t)
    body = struct.pack("<BBHI4s", 12, 0x10, 2140, len(records), b".FIT") + records
    return body + struct.pack("<H", _fit_crc(body))


def epoch_ms(*args) -> float:
    return datetime(*args, tzinfo=timezone.utc).timestamp() * 1000


def activity(activity_id, start, type_="running", **extra):
    """A summarizedActivities item in the export's units: cm, ms, kJ, epoch ms; local time two hours ahead."""
    base = {
        "activityId": activity_id, "activityType": type_, "name": f"Made-up {type_}",
        "startTimeGmt": epoch_ms(*start), "beginTimestamp": epoch_ms(*start),
        "startTimeLocal": epoch_ms(*start) + 2 * 3600 * 1000,
        "elapsedDuration": 3_600_000.0, "movingDuration": 3_500_000.0, "duration": 3_550_000.0,
        "distance": 1_000_000.0, "elevationGain": 5_000.0, "elevationLoss": 4_000.0,
        "avgHr": 141.0, "maxHr": 170.0, "avgDoubleCadence": 170.5, "avgRunCadence": 85.0,
        "calories": 2095.0, "aerobicTrainingEffect": 3.1, "anaerobicTrainingEffect": 1.2,
        "activityTrainingLoad": 120.0,
    }
    return {**base, **extra}


ACTIVITIES = [
    activity(1001, (2026, 9, 1, 6, 0)),
    activity(1002, (2026, 9, 3, 6, 0), avgHr=None, maxHr="n/a"),  # missing and unparseable
    activity(1003, (2026, 9, 5, 7, 0), type_="multi_sport", parent=True),
    activity(1004, (2026, 9, 5, 7, 0), type_="cycling", parentId=1003),  # the parent's first leg
]


def write_export(path, activities=ACTIVITIES, folder=False):
    """A made-up Garmin export as a zip (or folder): activities, two upload zips, wellness and a weigh-in."""
    uploads = io.BytesIO()
    with zipfile.ZipFile(uploads, "w") as z:
        z.writestr("someone_1.fit", make_file_id_fit(datetime(2026, 9, 1, 6, 0, 3)))       # → 1001
        z.writestr("someone_2.fit", make_file_id_fit(datetime(2026, 9, 5, 7, 0, 1)))       # → 1003, not the leg
        z.writestr("someone_3.fit", make_file_id_fit(datetime(2026, 9, 3, 6, 0), 32))      # monitoring: skipped
        z.writestr("someone_4.fit", make_file_id_fit(datetime(2026, 9, 20, 6, 0)))         # no activity near it
    members = {
        "DI-Connect-Fitness/someone_1_summarizedActivities.json":
            json.dumps([{"summarizedActivitiesExport": activities}]),
        "DI-Connect-Uploaded-Files/UploadedFiles_0-_Part1.zip": uploads.getvalue(),
        "DI-Connect-Aggregator/UDSFile_2026-09-01_2026-09-03.json": json.dumps([
            {"calendarDate": "2026-09-01", "currentDayRestingHeartRate": 50, "restingHeartRate": 53,
             "totalSteps": 9000,
             "allDayStress": {"aggregatorList": [{"type": "AWAKE", "averageStressLevel": 40},
                                                 {"type": "TOTAL", "averageStressLevel": 30}]},
             "bodyBattery": {"bodyBatteryStatList": [{"bodyBatteryStatType": "HIGHEST", "statsValue": 80},
                                                     {"bodyBatteryStatType": "LOWEST", "statsValue": -1}]}},
            {"calendarDate": "2026-09-02", "currentDayRestingHeartRate": 51, "totalSteps": 8000},
        ]),
        "DI-Connect-Wellness/2026-09-01_2026-09-03_1_sleepData.json": json.dumps([
            {"calendarDate": "2026-09-01", "deepSleepSeconds": 3600, "lightSleepSeconds": 14400,
             "remSleepSeconds": 5400, "awakeSleepSeconds": 600, "sleepScores": {"overallScore": 77}},
        ]),
        "DI-Connect-Wellness/2026-09-01_2026-09-03_1_healthStatusData.json": json.dumps([
            {"calendarDate": "2026-09-01", "metrics": [{"type": "HR", "value": 60.0},
                                                       {"type": "HRV", "value": 44.0, "status": "IN_RANGE"}]},
        ]),
        "DI-Connect-Metrics/TrainingReadinessDTO_20260901_20260903_1.json": json.dumps([
            {"calendarDate": "2026-09-01", "timestamp": "2026-09-01T07:00:00.0", "score": 60},
            {"calendarDate": "2026-09-01", "timestamp": "2026-09-01T12:00:00.0", "score": 55},
        ]),
        "DI-Connect-Wellness/1_userBioMetrics.json": json.dumps([
            {"metaData": {"calendarDate": "2026-09-01T00:00:00.0"}, "vo2MaxRunning": 50.0},
            {"metaData": {"calendarDate": "2026-09-02T07:00:00.0"},
             "weight": {"weight": 70500.0, "sourceType": "MANUAL", "timestampGMT": "2026-09-02T07:00:00.4"}},
        ]),
    }
    if folder:
        for name, content in members.items():
            target = path / ROOT / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content if isinstance(content, bytes) else content.encode())
        return path
    with zipfile.ZipFile(path, "w") as z:
        for name, content in members.items():
            z.writestr(ROOT + name, content)
    return path


@pytest.fixture
def conn(data_dir):
    c = db.connect(data_dir)
    db.migrate(c)
    yield c
    c.close()


def test_parse_activity_converts_the_export_units():
    counts = import_garmin.ImportCounts()
    row = parse_activity(ACTIVITIES[0], "2026-10-21T00:00:00Z", counts)
    assert row["garmin_id"] == 1001 and row["activity_type"] == "running" and row["origin"] == "export"
    assert row["start_utc"] == "2026-09-01T06:00:00Z" and row["start_local"] == "2026-09-01T08:00:00"
    assert row["utc_offset_min"] == 120
    assert (row["elapsed_s"], row["moving_s"]) == (3600, 3500)                       # ms → s
    assert (row["distance_m"], row["elev_gain_m"], row["elev_loss_m"]) == (10000.0, 50.0, 40.0)  # cm → m
    assert row["calories"] == pytest.approx(500.0)                                  # kJ → kcal
    assert row["avg_cadence"] == 170.5                                              # steps a minute, both feet
    assert parse_activity({**ACTIVITIES[0], "avgDoubleCadence": 0.0}, "x")["avg_cadence"] is None
    assert json.loads(row["raw_json"])["activityId"] == 1001
    assert counts.null_fields == 0


def test_missing_and_unparseable_fields_become_null_and_are_logged(caplog):
    counts = import_garmin.ImportCounts()
    with caplog.at_level(logging.WARNING):
        row = parse_activity(ACTIVITIES[1], "x", counts)
    assert row["avg_hr"] is None and row["max_hr"] is None
    assert counts.null_fields == 1 and "1002" in caplog.text and "maxHr" in caplog.text
    with pytest.raises(ValueError):
        parse_activity({"activityId": 9, "activityType": "running"}, "x")  # no start time


def test_import_loads_activities_files_wellness_and_weigh_ins(tmp_path, conn, data_dir):
    c = import_garmin.run(conn, write_export(tmp_path / "garmin.zip"), data_dir)
    assert (c.added, c.updated, c.unchanged, c.failed, c.from_sync) == (4, 0, 0, 0, 0)
    assert c.null_fields == 1
    # Recordings are matched by start time: one to 1001, one to the multisport parent (not its leg); the
    # monitoring file and the file with no activity near it are left out.
    files = {r["source_id"]: dict(r) for r in conn.execute("SELECT * FROM files")}
    assert set(files) == {"1001", "1003"} and c.files == 2
    assert (data_dir / "files/garmin/1001.fit").read_bytes() == make_file_id_fit(datetime(2026, 9, 1, 6, 0, 3))
    # Wellness from four files merges into one row per day: the day's own resting HR (not the seven-day
    # average), the latest readiness of the day, and a Body Battery of -1 (no data) stored as NULL.
    day = dict(conn.execute("SELECT * FROM daily_wellness WHERE date = '2026-09-01'").fetchone())
    assert (day["resting_hr"], day["steps"], day["stress_avg"]) == (50, 9000, 30)
    assert (day["body_battery_high"], day["body_battery_low"]) == (80, None)
    assert (day["sleep_s"], day["deep_s"], day["awake_s"], day["sleep_score"]) == (23400, 3600, 600, 77)
    assert (day["hrv_avg_ms"], day["hrv_status"], day["readiness_score"]) == (44.0, None, 55)
    # The newest day in the export was still under way when it was built, so it stays open for sync.
    assert day["complete"] == 1
    assert conn.execute("SELECT complete FROM daily_wellness WHERE date = '2026-09-02'").fetchone()[0] == 0
    assert c.wellness_days == 2
    weigh = dict(conn.execute("SELECT * FROM weigh_ins").fetchone())
    assert (weigh["measured_utc"], weigh["weight_kg"], weigh["source"]) == ("2026-09-02T07:00:00Z", 70.5, "garmin")


def test_running_the_import_again_changes_nothing(tmp_path, conn, data_dir):
    export = write_export(tmp_path / "garmin.zip")
    import_garmin.run(conn, export, data_dir)
    tables = ("garmin_activities", "files", "daily_wellness", "weigh_ins")
    dump = lambda: {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1")]  # noqa: E731
                    for t in tables}
    once = dump()
    c = import_garmin.run(conn, export, data_dir)
    assert (c.added, c.updated, c.unchanged, c.files, c.wellness_days, c.weigh_ins) == (0, 0, 4, 0, 0, 0)
    # updated_utc is volatile, so compare without it.
    strip = lambda d: {t: [r[:-1] if t == "daily_wellness" else r for r in rows] for t, rows in d.items()}  # noqa
    assert strip(dump()) == strip(once)


def test_what_sync_stored_is_left_alone(tmp_path, conn, data_dir):
    conn.execute("INSERT INTO garmin_activities (garmin_id, activity_type, name, start_utc, origin, raw_json, "
                 "imported_utc) VALUES (1001, 'running', 'From the API', '2026-09-01T06:00:00Z', 'sync', '{}', 'x')")
    conn.execute("INSERT INTO files (path, source, source_id, format, bytes, sha256) VALUES "
                 "('files/garmin/1001.fit', 'garmin', '1001', 'fit', 1, 'abc')")
    conn.execute("INSERT INTO daily_wellness (date, resting_hr, steps, complete, updated_utc) "
                 "VALUES ('2026-09-01', 49, NULL, 1, 'x')")
    conn.execute("INSERT INTO weigh_ins (measured_utc, weight_kg, source) VALUES ('2026-09-02T07:00:00Z', 71.0, "
                 "'garmin')")
    conn.commit()
    c = import_garmin.run(conn, write_export(tmp_path / "garmin.zip"), data_dir)
    assert c.from_sync == 1 and c.added == 3
    assert conn.execute("SELECT name FROM garmin_activities WHERE garmin_id = 1001").fetchone()[0] == "From the API"
    # 1001 already had its file, so only the parent's recording is copied.
    assert conn.execute("SELECT sha256 FROM files WHERE source_id = '1001'").fetchone()[0] == "abc" and c.files == 1
    # A stored day only gets its empty fields filled; a stored weigh-in is kept.
    day = conn.execute("SELECT resting_hr, steps, complete FROM daily_wellness WHERE date = '2026-09-01'").fetchone()
    assert tuple(day) == (49, 9000, 1)
    assert conn.execute("SELECT weight_kg FROM weigh_ins").fetchone()[0] == 71.0 and c.weigh_ins == 0


def test_files_in_a_later_part_of_the_export_are_matched(tmp_path, conn, data_dir):
    # Split the made-up export: activities in one zip, the upload files in another.
    whole = zipfile.ZipFile(write_export(tmp_path / "garmin.zip"))
    for part, keep in (("part1.zip", "Fitness"), ("part2.zip", "Uploaded-Files")):
        with zipfile.ZipFile(tmp_path / part, "w") as z:
            for name in whole.namelist():
                if keep in name:
                    z.writestr(name, whole.read(name))
    assert import_garmin.run(conn, tmp_path / "part1.zip", data_dir).files == 0
    c = import_garmin.run(conn, tmp_path / "part2.zip", data_dir)
    assert c.files == 2 and c.added == 0


def test_an_unpacked_folder_works_and_other_folders_are_refused(tmp_path, conn, data_dir):
    folder = write_export(tmp_path / "unpacked", folder=True)
    assert import_garmin.run(conn, folder, data_dir).added == 4
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "notes.txt").write_text("not an export")
    with pytest.raises(import_garmin.NotAnExportError, match="doesn't look like a Garmin export"):
        import_garmin.run(conn, tmp_path / "other", data_dir)


def test_reconcile_treats_export_rows_like_synced_ones(tmp_path, conn, data_dir):
    import_garmin.run(conn, write_export(tmp_path / "garmin.zip"), data_dir)
    reconcile.run(conn, data_dir / "reports")
    # The multisport leg folds into its parent, so three activities.
    keys = [r[0] for r in conn.execute("SELECT key FROM activities ORDER BY key")]
    assert keys == ["garmin:1001", "garmin:1002", "garmin:1003"]


def test_cli_import_garmin_prints_the_counts(tmp_path, data_dir, capsys):
    code = cli.main(["--data-dir", str(data_dir), "import-garmin", str(write_export(tmp_path / "garmin.zip"))])
    out = capsys.readouterr().out
    assert code == 0
    assert "Garmin export: 4 added, 0 updated, 0 unchanged, 0 failed; 0 already in the archive from sync" in out
    assert "2 files copied; 2 wellness days added or filled in; 1 weigh-ins added" in out
    assert cli.main(["--data-dir", str(data_dir), "import-garmin", str(tmp_path / "missing.zip")]) == 1
