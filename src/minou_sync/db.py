"""SQLite connection, schema migration and idempotent upserts."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

SCHEMA_VERSION = 4
# A sync or backfill day holds the write lock while it fetches (about 17 s), so other commands wait this long.
LOCK_TIMEOUT_S = 60

# Timestamps that change on every run. Comparing them would make every re-import an "update".
VOLATILE_COLUMNS = frozenset({"imported_utc", "updated_utc"})

UpsertResult = Literal["added", "updated", "unchanged"]


class SchemaTooNewError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def connect(data_dir: Path) -> sqlite3.Connection:
    """Open data_dir/archive.db, creating data_dir with mode 0700 if needed."""
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    # mkdir's mode is masked by umask, so set it explicitly. Health data stays owner-only.
    data_dir.chmod(0o700)
    conn = sqlite3.connect(data_dir / "archive.db", timeout=LOCK_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    has_meta = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
    if not has_meta:
        return 0
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    return int(row["value"]) if row else 0


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def _upgrade_v2_to_v3(conn: sqlite3.Connection) -> None:
    """Bring a version 2 database to version 3 in place, keeping every existing row.

    schema.sql creates the new v3 tables (IF NOT EXISTS). What SQLite can't express idempotently is handled
    here: adding columns to existing tables, and rebuilding `files` and `activities` to drop CHECK constraints
    that v3 relaxes (any source may own a file; a master activity may come from a source other than Garmin or
    Strava). Both rebuilt tables are copied row for row.
    """
    # New columns on existing tables. Each guarded, so the upgrade is safe to re-run. (activities gains key and
    # edited through its rebuild below, not here.)
    if not _has_column(conn, "weigh_ins", "edited_utc"):
        conn.execute("ALTER TABLE weigh_ins ADD COLUMN edited_utc TEXT")
    if not _has_column(conn, "daily_wellness", "source"):
        conn.execute("ALTER TABLE daily_wellness ADD COLUMN source TEXT NOT NULL DEFAULT 'garmin'")

    # Rebuild `files` without its source CHECK and with a TEXT source_id (SQLite can't alter a CHECK).
    conn.execute("ALTER TABLE files RENAME TO files_v2")
    conn.execute(
        "CREATE TABLE files (path TEXT PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL, "
        "format TEXT NOT NULL CHECK (format IN ('fit', 'gpx', 'tcx')), bytes INTEGER NOT NULL, sha256 TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO files (path, source, source_id, format, bytes, sha256) "
        "SELECT path, source, CAST(source_id AS TEXT), format, bytes, sha256 FROM files_v2"
    )
    conn.execute("DROP TABLE files_v2")

    # Rebuild `activities` without the 'both/garmin/strava' source CHECK and the garmin_id/strava_id NOT NULL
    # check: a v3 master can be from another source or manual. It's derived, but keeping the rows satisfies
    # "every row kept" and leaves the archive queryable until the first v3 reconcile.
    conn.execute("ALTER TABLE activities RENAME TO activities_v2")
    conn.execute("DROP INDEX IF EXISTS activities_start_idx")
    conn.execute(
        "CREATE TABLE activities ("
        "id INTEGER PRIMARY KEY, key TEXT, "
        "garmin_id INTEGER UNIQUE REFERENCES garmin_activities(garmin_id), "
        "strava_id INTEGER UNIQUE REFERENCES strava_activities(strava_id), "
        "source TEXT NOT NULL, activity_type TEXT NOT NULL, name TEXT, start_utc TEXT NOT NULL, start_local TEXT, "
        "elapsed_s INTEGER, moving_s INTEGER, distance_m REAL, elev_gain_m REAL, avg_hr INTEGER, max_hr INTEGER, "
        "avg_watts REAL, avg_cadence REAL, calories REAL, aerobic_te REAL, anaerobic_te REAL, "
        "edited INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO activities (id, key, garmin_id, strava_id, source, activity_type, name, start_utc, start_local, "
        "elapsed_s, moving_s, distance_m, elev_gain_m, avg_hr, max_hr, avg_watts, avg_cadence, calories, aerobic_te, "
        "anaerobic_te, edited) SELECT id, NULL, garmin_id, strava_id, source, activity_type, name, start_utc, "
        "start_local, elapsed_s, moving_s, distance_m, elev_gain_m, avg_hr, max_hr, avg_watts, avg_cadence, calories, "
        "aerobic_te, anaerobic_te, 0 FROM activities_v2"
    )
    conn.execute("DROP TABLE activities_v2")


def _upgrade_v3_to_v4(conn: sqlite3.Connection) -> None:
    """Bring a version 3 database to version 4 in place. Additive only: add activities.check_reason (the
    plausibility flag); activity_efforts and effort_runs are created by schema.sql (IF NOT EXISTS). No
    table rebuild, so this step needs no go-ahead (design, data model note)."""
    if not _has_column(conn, "activities", "check_reason"):
        conn.execute("ALTER TABLE activities ADD COLUMN check_reason TEXT")


def migrate(conn: sqlite3.Connection) -> int:
    """Apply the schema and return its version. Refuses a database written by newer code."""
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise SchemaTooNewError(f"archive.db is schema v{current}; this code knows v{SCHEMA_VERSION}. Update the code.")
    if current == SCHEMA_VERSION:
        # Nothing to apply; skipping the write lets read-only commands run while a sync holds the lock.
        return SCHEMA_VERSION
    with conn:
        if current in (1, 2):
            # v1 and v2 both carry the pre-v3 activities and files tables with their now-relaxed CHECKs; both
            # need the rebuild. v1 additionally lacks garmin_raw, which schema.sql adds below.
            _upgrade_v2_to_v3(conn)
        if current in (1, 2, 3):
            # v4 is additive: add activities.check_reason (activity_efforts is created by schema.sql below).
            _upgrade_v3_to_v4(conn)
        # schema.sql creates anything still missing (every statement is CREATE ... IF NOT EXISTS) and defines a
        # fresh database outright. For a v2 upgrade the rebuilds above already ran; the new indexes land here.
        conn.executescript(files("minou_sync").joinpath("schema.sql").read_text())
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
    return SCHEMA_VERSION


def upsert(conn: sqlite3.Connection, table: str, key: str, row: dict[str, Any]) -> UpsertResult:
    """Insert or update one row by its key column, leaving unchanged rows untouched."""
    existing = conn.execute(f"SELECT * FROM {table} WHERE {key} = ?", (row[key],)).fetchone()
    columns = list(row)
    if existing is not None and "edited_utc" in existing.keys() and existing["edited_utc"] is not None:
        # A row the athlete has edited is never overwritten by an import or sync.
        return "unchanged"
    if existing is None:
        placeholders = ", ".join("?" for _ in columns)
        conn.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})", [row[c] for c in columns])
        return "added"
    changed = [c for c in columns if c not in VOLATILE_COLUMNS and existing[c] != row[c]]
    if not changed:
        return "unchanged"
    assignments = ", ".join(f"{c} = ?" for c in columns if c != key)
    conn.execute(
        f"UPDATE {table} SET {assignments} WHERE {key} = ?",
        [row[c] for c in columns if c != key] + [row[key]],
    )
    return "updated"
