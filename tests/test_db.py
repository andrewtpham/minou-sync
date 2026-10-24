import stat

import pytest

from minou_sync import db


V3_TABLES = {
    "meta", "garmin_activities", "strava_activities", "source_activities", "activities", "activity_sources",
    "activity_edits", "manual_activities", "daily_wellness", "weigh_ins", "body_composition", "lab_tests",
    "lab_test_stages", "files", "garmin_raw", "notes",
}
V4_TABLES = V3_TABLES | {"activity_efforts", "effort_runs"}


def test_new_database_has_schema_version_4(conn):
    assert db.schema_version(conn) == 4


def test_data_folder_is_owner_only(conn, data_dir):
    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700


def test_migrate_twice_is_harmless(conn):
    assert db.migrate(conn) == 4
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert V4_TABLES <= tables


def test_new_database_has_check_reason_and_activity_efforts(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(activities)")}
    assert "check_reason" in cols
    effort_cols = {r[1] for r in conn.execute("PRAGMA table_info(activity_efforts)")}
    assert {"source", "source_id", "effort", "value", "start_offset_s", "computed_utc"} == effort_cols


def test_newer_schema_is_refused(conn):
    conn.execute("UPDATE meta SET value='5' WHERE key='schema_version'")
    conn.commit()
    with pytest.raises(db.SchemaTooNewError, match="schema v5"):
        db.migrate(conn)


def _strava_row(**overrides):
    row = {"strava_id": 1, "activity_type": "Ride", "start_utc": "2026-10-04T07:00:00Z",
           "elapsed_s": 3600, "raw_json": "{}", "imported_utc": "2026-10-19T10:00:00Z"}
    return row | overrides


def test_upsert_added_unchanged_updated(conn):
    assert db.upsert(conn, "strava_activities", "strava_id", _strava_row()) == "added"
    # A new import time alone doesn't count as a change.
    assert db.upsert(conn, "strava_activities", "strava_id", _strava_row(imported_utc="2026-10-20T10:00:00Z")) == "unchanged"
    assert db.upsert(conn, "strava_activities", "strava_id", _strava_row(elapsed_s=3700)) == "updated"
    assert conn.execute("SELECT elapsed_s FROM strava_activities").fetchone()[0] == 3700


# The pre-v3 activities and files tables, with the CHECK constraints v3 relaxes.
_V2_ACTIVITIES = (
    "CREATE TABLE activities (id INTEGER PRIMARY KEY, "
    "garmin_id INTEGER UNIQUE REFERENCES garmin_activities(garmin_id), "
    "strava_id INTEGER UNIQUE REFERENCES strava_activities(strava_id), "
    "source TEXT NOT NULL CHECK (source IN ('both', 'garmin', 'strava')), activity_type TEXT NOT NULL, name TEXT, "
    "start_utc TEXT NOT NULL, start_local TEXT, elapsed_s INTEGER, moving_s INTEGER, distance_m REAL, "
    "elev_gain_m REAL, avg_hr INTEGER, max_hr INTEGER, avg_watts REAL, avg_cadence REAL, calories REAL, "
    "aerobic_te REAL, anaerobic_te REAL, CHECK (garmin_id IS NOT NULL OR strava_id IS NOT NULL))"
)
_V2_FILES = (
    "CREATE TABLE files (path TEXT PRIMARY KEY, source TEXT NOT NULL CHECK (source IN ('garmin', 'strava')), "
    "source_id INTEGER NOT NULL, format TEXT NOT NULL CHECK (format IN ('fit', 'gpx', 'tcx')), "
    "bytes INTEGER NOT NULL, sha256 TEXT NOT NULL)"
)


def _make_v2_database(conn):
    """Reshape a fresh database to look like schema v2, with the pre-v3 activities and files tables."""
    conn.execute("DROP TABLE activities")
    conn.execute(_V2_ACTIVITIES)
    conn.execute("CREATE INDEX activities_start_idx ON activities(start_utc)")
    conn.execute("DROP TABLE files")
    conn.execute(_V2_FILES)
    for t in ("source_activities", "activity_sources", "activity_edits", "manual_activities",
              "body_composition", "lab_tests", "lab_test_stages", "notes", "activity_efforts", "effort_runs"):
        conn.execute(f"DROP TABLE {t}")
    conn.execute("ALTER TABLE daily_wellness DROP COLUMN source")
    conn.execute("ALTER TABLE weigh_ins DROP COLUMN edited_utc")
    conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    conn.commit()


def test_version_1_database_upgrades_to_3(data_dir):
    conn = db.connect(data_dir)
    db.migrate(conn)
    _make_v2_database(conn)
    conn.execute("DROP TABLE garmin_raw")
    conn.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    conn.commit()
    assert db.migrate(conn) == 4
    assert db.schema_version(conn) == 4
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='garmin_raw'").fetchone()
    assert "check_reason" in {r[1] for r in conn.execute("PRAGMA table_info(activities)")}


def test_version_2_database_upgrades_to_4_keeping_every_row(data_dir):
    conn = db.connect(data_dir)
    db.migrate(conn)
    _make_v2_database(conn)

    # Made-up rows across the tables that change, to prove nothing is dropped.
    conn.execute(
        "INSERT INTO garmin_activities (garmin_id, activity_type, start_utc, origin, raw_json, imported_utc)"
        " VALUES (1, 'running', '2026-10-01T06:00:00Z', 'sync', '{}', '2026-10-19T00:00:00Z')")
    conn.execute(
        "INSERT INTO activities (id, garmin_id, source, activity_type, start_utc) "
        "VALUES (1, 1, 'garmin', 'run', '2026-10-01T06:00:00Z')")
    conn.execute(
        "INSERT INTO files (path, source, source_id, format, bytes, sha256) "
        "VALUES ('files/garmin/1.fit', 'garmin', 1, 'fit', 10, 'abc')")
    conn.execute(
        "INSERT INTO daily_wellness (date, steps, updated_utc) VALUES ('2026-10-01', 5000, '2026-10-19T00:00:00Z')")
    conn.execute("INSERT INTO weigh_ins (measured_utc, weight_kg, source) VALUES ('2026-10-01T07:00:00Z', 72.0, 'garmin')")
    conn.commit()

    assert db.migrate(conn) == 4
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert V4_TABLES <= tables

    # Every row kept, with the new columns present and defaulted.
    act = dict(conn.execute("SELECT * FROM activities WHERE id = 1").fetchone())
    assert act["garmin_id"] == 1 and act["key"] is None and act["edited"] == 0 and act["check_reason"] is None
    f = dict(conn.execute("SELECT * FROM files WHERE path = 'files/garmin/1.fit'").fetchone())
    assert f["source"] == "garmin" and f["source_id"] == "1"
    assert conn.execute("SELECT source FROM daily_wellness WHERE date = '2026-10-01'").fetchone()[0] == "garmin"
    assert conn.execute("SELECT edited_utc FROM weigh_ins").fetchone()[0] is None

    # The relaxed activities table now accepts a source other than Garmin or Strava, with neither id.
    conn.execute(
        "INSERT INTO activities (id, key, source, activity_type, start_utc) "
        "VALUES (2, 'nrc:x', 'nrc', 'run', '2026-10-02T06:00:00Z')")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 2


def _make_v3_database(conn):
    """Reshape a fresh database to look like schema v3: drop the v4 additions."""
    conn.execute("DROP TABLE activity_efforts")
    conn.execute("DROP TABLE effort_runs")
    conn.execute("ALTER TABLE activities DROP COLUMN check_reason")
    conn.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    conn.commit()


def test_version_3_database_upgrades_to_4_additively_keeping_every_row(data_dir):
    conn = db.connect(data_dir)
    db.migrate(conn)
    _make_v3_database(conn)
    conn.execute(
        "INSERT INTO activities (id, key, source, activity_type, start_utc) "
        "VALUES (1, 'garmin:1', 'garmin', 'run', '2026-10-01T06:00:00Z')")
    conn.commit()

    assert db.migrate(conn) == 4
    assert db.schema_version(conn) == 4
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert V4_TABLES <= tables
    act = dict(conn.execute("SELECT * FROM activities WHERE id = 1").fetchone())
    assert act["key"] == "garmin:1" and act["check_reason"] is None
    conn.close()


def test_current_schema_migrates_without_taking_the_write_lock(conn, data_dir):
    other = db.connect(data_dir)
    other.execute("BEGIN IMMEDIATE")  # another process mid-sync, holding the write lock
    try:
        assert db.migrate(conn) == db.SCHEMA_VERSION
    finally:
        other.rollback()
        other.close()
