"""Strava account archive import: activities.csv into strava_activities, original files into files/strava/ in the data folder.

A Strava archive is a zip (or unpacked folder) with activities.csv at the top and the original activity files
under activities/ (FIT, GPX or TCX, some gzipped); this reads activities.csv and copies the files it names.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import logging
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import db

log = logging.getLogger(__name__)

ACTIVITIES_CSV = "activities.csv"
DATE_FORMAT = "%b %d, %Y, %I:%M:%S %p"  # "Oct 7, 2026, 6:30:00 AM", in UTC
FILE_FORMATS = ("fit", "gpx", "tcx")


class NotAnExportError(ValueError):
    pass


@dataclass
class ImportCounts:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    files: int = 0
    null_fields: int = 0


class _Archive:
    """Read members by name from a zip or an unpacked folder."""

    def __init__(self, path: Path) -> None:
        self._zip = zipfile.ZipFile(path) if path.is_file() else None
        self._dir = path
        if not self.exists(ACTIVITIES_CSV):
            raise NotAnExportError(f"This doesn't look like a Strava export: {ACTIVITIES_CSV} not found in {path}")

    def exists(self, name: str) -> bool:
        if self._zip is not None:
            return name in self._zip.NameToInfo
        return (self._dir / name).is_file()

    def read(self, name: str) -> bytes:
        return self._zip.read(name) if self._zip is not None else (self._dir / name).read_bytes()


def read_rows(text: str) -> list[dict[str, str]]:
    """Rows of activities.csv as dicts. A repeated column name keeps its last (metric) value under the plain
    name and its first (display-unit) value under '<name> (display)'."""
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    rows = []
    for values in reader:
        row: dict[str, str] = {}
        for name, value in zip(header, values):
            if name in row:
                row[f"{name} (display)"] = row[name]
            row[name] = value
        rows.append(row)
    return rows


def _number(row: dict[str, str], field: str, counts: ImportCounts | None) -> float | None:
    value = row.get(field, "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        log.warning("Strava activity %s: %s %r isn't a number; stored as NULL", row.get("Activity ID"), field, value)
        if counts is not None:
            counts.null_fields += 1
        return None


def _int(value: float | None) -> int | None:
    return None if value is None else round(value)


def parse_row(row: dict[str, str], imported_utc: str, counts: ImportCounts | None = None) -> dict[str, Any]:
    """Map one activities.csv row to a strava_activities row. Raises ValueError without an ID, date or type."""
    start = datetime.strptime(row["Activity Date"].strip(), DATE_FORMAT)
    if not row.get("Activity Type"):
        raise ValueError("no Activity Type")
    return {
        "strava_id": int(row["Activity ID"]),
        "activity_type": row["Activity Type"],
        "name": row.get("Activity Name") or None,
        "description": row.get("Activity Description") or None,
        "start_utc": start.isoformat() + "Z",
        "elapsed_s": _int(_number(row, "Elapsed Time", counts)),
        "moving_s": _int(_number(row, "Moving Time", counts)),
        "distance_m": _number(row, "Distance", counts),
        "elev_gain_m": _number(row, "Elevation Gain", counts),
        "avg_hr": _int(_number(row, "Average Heart Rate", counts)),
        "max_hr": _int(_number(row, "Max Heart Rate", counts)),
        "avg_watts": _number(row, "Average Watts", counts),
        "calories": _number(row, "Calories", counts),
        "raw_json": json.dumps(row, sort_keys=True, ensure_ascii=False),
        "imported_utc": imported_utc,
    }


def _copy_file(conn: sqlite3.Connection, archive: _Archive, data_dir: Path, strava_id: int, member: str) -> bool:
    """Decompress one original file into files/strava/ in the data folder and record it. False if it was already there."""
    name = member.rsplit("/", 1)[-1].lower()
    ext = name.removesuffix(".gz").rsplit(".", 1)[-1]
    if ext not in FILE_FORMATS:
        log.warning("Strava activity %s: %s isn't FIT, GPX or TCX; skipped", strava_id, member)
        return False
    rel = Path("files", "strava", f"{strava_id}.{ext}")
    recorded = conn.execute("SELECT 1 FROM files WHERE path = ?", (rel.as_posix(),)).fetchone()
    if recorded and (data_dir / rel).is_file():
        return False
    if not archive.exists(member):
        log.warning("Strava activity %s: %s is listed but not in the archive", strava_id, member)
        return False
    blob = archive.read(member)
    try:
        content = gzip.decompress(blob) if name.endswith(".gz") else blob
    except (OSError, EOFError) as e:
        log.warning("Strava activity %s: %s couldn't be decompressed (%s); skipped", strava_id, member, e)
        return False
    (data_dir / rel).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    (data_dir / rel).write_bytes(content)
    db.upsert(conn, "files", "path", {
        "path": rel.as_posix(), "source": "strava", "source_id": strava_id, "format": ext,
        "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
    })
    return True


def run(conn: sqlite3.Connection, path: Path, data_dir: Path,
        progress: "Callable[[int, int], None] | None" = None) -> ImportCounts:
    """Import a Strava archive (zip or folder). Safe to run again: unchanged rows and files are left alone.

    `progress`, if given, is called with (rows_done, total) after each activity row, so the CLI can show a bar
    while a large archive's files are copied. The module prints nothing itself.
    """
    archive = _Archive(path)
    counts = ImportCounts()
    now = db.utc_now()
    rows = read_rows(archive.read(ACTIVITIES_CSV).decode("utf-8-sig"))
    total = len(rows)
    for done, row in enumerate(rows, start=1):
        if progress is not None:
            progress(done, total)
        try:
            parsed = parse_row(row, now, counts)
        except (KeyError, ValueError) as e:
            log.warning("Strava row %s skipped: %s", row.get("Activity ID") or "without an ID", e)
            counts.failed += 1
            continue
        # One transaction per activity keeps write locks short while a Garmin sync may be running.
        with conn:
            result = db.upsert(conn, "strava_activities", "strava_id", parsed)
            setattr(counts, result, getattr(counts, result) + 1)
            if row.get("Filename"):
                counts.files += _copy_file(conn, archive, data_dir, parsed["strava_id"], row["Filename"])
    return counts
