"""Readable outputs: CSV copies of the derived tables and the status summary."""

from __future__ import annotations

import csv
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

# Table, sort order. activities.id is included but isn't stable across reconcile runs.
EXPORTS = (("activities", "start_utc, id"), ("daily_wellness", "date"))


@dataclass(frozen=True)
class StatusReport:
    by_source: dict[str, int]
    first_start_utc: str | None
    last_start_utc: str | None
    last_wellness_date: str | None
    last_sync_utc: str | None
    without_file: int
    db_bytes: int

    @property
    def total(self) -> int:
        return sum(self.by_source.values())


def export_csv(conn: sqlite3.Connection, exports_dir: Path) -> list[Path]:
    """Write one CSV per table in EXPORTS, replacing the previous files."""
    exports_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    written = []
    for table, order in EXPORTS:
        cursor = conn.execute(f"SELECT * FROM {table} ORDER BY {order}")
        path = exports_dir / f"{table}.csv"
        # Write to a temporary file and swap it in, so a failed run never leaves half a CSV.
        tmp = path.with_suffix(".csv.tmp")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(d[0] for d in cursor.description)
            writer.writerows(tuple(row) for row in cursor)
        os.replace(tmp, path)
        written.append(path)
    return written


def status(conn: sqlite3.Connection) -> StatusReport:
    by_source = {row[0]: row[1] for row in conn.execute("SELECT source, COUNT(*) FROM activities GROUP BY source")}
    first, last = conn.execute("SELECT MIN(start_utc), MAX(start_utc) FROM activities").fetchone()
    last_wellness = conn.execute("SELECT MAX(date) FROM daily_wellness").fetchone()[0]
    last_sync = conn.execute("SELECT value FROM meta WHERE key='last_sync_utc'").fetchone()
    without_file = conn.execute(
        "SELECT COUNT(*) FROM activities a WHERE NOT EXISTS ("
        " SELECT 1 FROM files f WHERE (f.source='garmin' AND f.source_id=a.garmin_id)"
        " OR (f.source='strava' AND f.source_id=a.strava_id))"
    ).fetchone()[0]
    db_file = conn.execute("SELECT file FROM pragma_database_list WHERE name='main'").fetchone()[0]
    return StatusReport(
        by_source=by_source,
        first_start_utc=first,
        last_start_utc=last,
        last_wellness_date=last_wellness,
        last_sync_utc=last_sync[0] if last_sync else None,
        without_file=without_file,
        db_bytes=Path(db_file).stat().st_size if db_file else 0,
    )
