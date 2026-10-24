"""Best efforts from original files: fastest distances for runs, best power and fastest 40 km for rides.

The original FIT, GPX or TCX file is the ground truth; a platform's own splits can be missing or wrong,
so efforts are computed from the file once and stored in activity_efforts, keyed by
the source record whose file was read. `best_efforts` (queries.py) reads the main record's row first,
then any other source's.

The method (design, efforts.py):

- Read time, cumulative distance and power from each file. FIT records carry a cumulative `distance` and
  `power`; GPX track points carry only time and position, so distance is summed with the haversine formula;
  TCX carries DistanceMeters per trackpoint. FIT's own distance is preferred when present.
- Drop a track point whose speed from the previous kept point is over the type's plausibility limit
  (plausibility.SPEED_LIMIT_KMH), so a GPS jump can't make a fast split. A Garmin distance_m of 0 is
  treated as missing on distance sports, by the same confirmation that governs records.
- Fastest distance efforts use a two-pointer sliding window over cumulative distance, interpolating the start
  and end times. Power efforts use a sliding mean over 1-second-resampled power.
"""

from __future__ import annotations

import gzip
import io
import logging
import math
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import db
from .plausibility import SPEED_LIMIT_KMH

log = logging.getLogger(__name__)

# Distance efforts per shared activity type (metres). Rides get a 40 km effort plus power (below).
_RUN_TYPES = frozenset({"run", "run_trail", "run_treadmill"})
_RIDE_TYPES = frozenset({"ride", "ride_gravel", "ride_mtb", "ride_indoor"})

RUN_DISTANCES: dict[str, float] = {
    "run_1km": 1000.0, "run_1mi": 1609.344, "run_5k": 5000.0, "run_10k": 10000.0,
    "run_half": 21097.5, "run_marathon": 42195.0, "run_50k": 50000.0,
}
RIDE_DISTANCES: dict[str, float] = {"ride_40km": 40000.0}
# Power efforts (seconds of the averaging window) for rides.
POWER_WINDOWS: dict[str, int] = {"power_5min": 300, "power_20min": 1200, "power_60min": 3600}

# FIT semicircle -> degrees.
_SEMICIRCLE = 180.0 / 2**31


@dataclass
class EffortCounts:
    activities: int = 0          # files processed that produced at least one effort
    efforts: int = 0             # effort rows written
    empty: int = 0               # files parsed fine but with no qualifying effort (recorded, not retried)
    unreadable: int = 0          # files that couldn't be parsed (recorded, retried next run)
    files_missing: int = 0       # recorded files not on disk (retried next run)
    skipped: int = 0             # files already in the ledger with a matching hash, so left alone
    garmin_checked: int = 0      # Garmin activities cross-checked against their splits
    garmin_differs: int = 0      # of those, how many differ from Garmin's fastest splits by more than 2%


@dataclass(frozen=True)
class Point:
    t: float                     # seconds from the activity start
    distance: float | None       # cumulative metres, when the file carries it
    power: float | None          # instantaneous watts
    lat: float | None
    lon: float | None


# --- Reading the three file formats ---------------------------------------------------------------------

def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two lat/lon points."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _read_fit(blob: bytes) -> list[Point]:
    from fitparse import FitFile, FitParseError

    try:
        fit = FitFile(io.BytesIO(blob))
        records = list(fit.get_messages("record"))
    except (FitParseError, ValueError, KeyError) as e:
        log.warning("FIT file couldn't be parsed for efforts (%s); skipped", e)
        return []
    points: list[Point] = []
    t0: datetime | None = None
    for rec in records:
        v = {d.name: d.value for d in rec}
        ts = v.get("timestamp")
        if ts is None:
            continue
        ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        if t0 is None:
            t0 = ts
        lat = v.get("position_lat")
        lon = v.get("position_long")
        points.append(Point(
            t=(ts - t0).total_seconds(),
            distance=v.get("distance"),
            power=v.get("power"),
            lat=lat * _SEMICIRCLE if lat is not None else None,
            lon=lon * _SEMICIRCLE if lon is not None else None,
        ))
    return points


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _read_gpx(blob: bytes) -> list[Point]:
    root = ET.fromstring(blob)
    points: list[Point] = []
    t0: datetime | None = None
    for el in root.iter():
        if _local_name(el.tag) != "trkpt":
            continue
        time_el = next((c for c in el if _local_name(c.tag) == "time"), None)
        if time_el is None or not time_el.text or not time_el.text.strip():
            continue
        ts = datetime.fromisoformat(time_el.text.strip().replace("Z", "+00:00"))
        if t0 is None:
            t0 = ts
        lat = el.get("lat")
        lon = el.get("lon")
        points.append(Point(
            t=(ts - t0).total_seconds(), distance=None, power=None,
            lat=float(lat) if lat is not None else None,
            lon=float(lon) if lon is not None else None,
        ))
    return points


def _read_tcx(blob: bytes) -> list[Point]:
    root = ET.fromstring(blob)
    points: list[Point] = []
    t0: datetime | None = None
    for tp in root.iter():
        if _local_name(tp.tag) != "Trackpoint":
            continue
        children = {_local_name(c.tag): c for c in tp.iter()}
        time_el = children.get("Time")
        if time_el is None or not time_el.text or not time_el.text.strip():
            continue
        ts = datetime.fromisoformat(time_el.text.strip().replace("Z", "+00:00"))
        if t0 is None:
            t0 = ts

        def num(name: str) -> float | None:
            el = children.get(name)
            if el is None or not el.text or not el.text.strip():
                return None
            try:
                return float(el.text.strip())
            except ValueError:
                return None

        points.append(Point(
            t=(ts - t0).total_seconds(),
            distance=num("DistanceMeters"),
            power=num("Watts"),
            lat=num("LatitudeDegrees"),
            lon=num("LongitudeDegrees"),
        ))
    return points


def _read_points(path: Path) -> list[Point]:
    name = path.name.lower()
    blob = path.read_bytes()
    if name.endswith(".gz"):
        blob = gzip.decompress(blob)
        name = name.removesuffix(".gz")
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    try:
        if ext == "fit":
            return _read_fit(blob)
        if ext == "gpx":
            return _read_gpx(blob)
        if ext == "tcx":
            return _read_tcx(blob)
    except (ET.ParseError, ValueError) as e:
        log.warning("%s couldn't be parsed for efforts (%s); skipped", path.name, e)
    return []


# --- Cleaning and cumulative distance -------------------------------------------------------------------

def _clean(points: list[Point], limit_kmh: float | None) -> list[tuple[float, float]]:
    """Return (time_s, cumulative_distance_m) pairs, dropping GPS jumps over the type's limit.

    Distance comes from the file's cumulative distance when present (FIT, TCX), else from the haversine sum of
    kept positions (GPX). A point whose speed from the previous kept point exceeds `limit_kmh` is dropped, so a
    GPS spike can't shorten a split. Points are kept in order; cumulative distance is rebuilt from the kept
    deltas so a dropped point doesn't leave a gap.
    """
    limit_ms = (limit_kmh / 3.6) if limit_kmh else None
    kept: list[tuple[float, float]] = []
    cum = 0.0
    prev: Point | None = None
    prev_file_dist: float | None = None
    for p in points:
        if p.t < 0:
            continue
        step: float | None = None
        if p.distance is not None and prev_file_dist is not None:
            step = p.distance - prev_file_dist
        elif p.lat is not None and p.lon is not None and prev is not None and prev.lat is not None \
                and prev.lon is not None:
            step = _haversine(prev.lat, prev.lon, p.lat, p.lon)
        if step is not None and step < 0:
            step = 0.0  # a cumulative distance that goes backwards is noise; treat as no movement
        if step is not None and limit_ms is not None and prev is not None:
            dt = p.t - prev.t
            if dt > 0 and (step / dt) > limit_ms:
                # A jump: skip this point entirely, keeping the previous kept point as the anchor.
                continue
        if not kept:
            kept.append((p.t, 0.0))
        elif step is not None:
            cum += step
            kept.append((p.t, cum))
        else:
            kept.append((p.t, cum))
        prev = p
        if p.distance is not None:
            prev_file_dist = p.distance
    return kept


# --- Sliding windows ------------------------------------------------------------------------------------

def _fastest_for_distance(stream: list[tuple[float, float]], target_m: float) -> tuple[float, int] | None:
    """Fastest time (s) and start offset (s) to cover target_m, over cumulative distance, or None if too short.

    Two pointers over (time, cumulative distance), interpolating the exact crossing time at each end so the
    split isn't quantised to the sample rate.
    """
    if len(stream) < 2 or stream[-1][1] < target_m:
        return None
    best: float | None = None
    best_start = 0
    j = 0
    for i in range(len(stream)):
        if j < i:
            j = i
        while j < len(stream) - 1 and stream[j][1] - stream[i][1] < target_m:
            j += 1
        if stream[j][1] - stream[i][1] < target_m:
            break
        # Interpolate the time at which exactly target_m was covered from point i.
        t_start = stream[i][0]
        d_need = stream[i][1] + target_m
        (t_a, d_a), (t_b, d_b) = stream[j - 1], stream[j]
        t_end = t_b if d_b == d_a else t_a + (t_b - t_a) * (d_need - d_a) / (d_b - d_a)
        elapsed = t_end - t_start
        if elapsed > 0 and (best is None or elapsed < best):
            best = elapsed
            best_start = round(t_start)
    return (best, best_start) if best is not None else None


def _best_power(points: list[Point], window_s: int) -> tuple[float, int] | None:
    """Best average power (W) over any window_s seconds, and its start offset, or None if too short or no power.

    Power is resampled to 1 Hz (held between samples), then a sliding sum gives the mean over each window.
    """
    powered = [(p.t, p.power) for p in points if p.power is not None and p.t >= 0]
    if len(powered) < 2:
        return None
    start_t = powered[0][0]
    end_t = powered[-1][0]
    total_s = int(end_t - start_t)
    if total_s < window_s:
        return None
    # Build a 1 Hz series, holding each sample's power until the next.
    series = [0.0] * (total_s + 1)
    idx = 0
    for sec in range(total_s + 1):
        t = start_t + sec
        while idx < len(powered) - 1 and powered[idx + 1][0] <= t:
            idx += 1
        series[sec] = powered[idx][1]
    # Sliding sum over window_s seconds.
    running = sum(series[:window_s])
    best = running
    best_start = 0
    for s in range(window_s, len(series)):
        running += series[s] - series[s - window_s]
        if running > best:
            best = running
            best_start = s - window_s + 1
    return (best / window_s, round(start_t + best_start))


# --- Public: compute one file ---------------------------------------------------------------------------

def compute(path: Path, activity_type: str) -> dict[str, tuple[float, int]]:
    """Best efforts in one original file: effort name -> (value, start_offset_s).

    Runs get the fastest distances in RUN_DISTANCES; rides get the fastest 40 km and the best power windows.
    value is seconds for a distance effort, watts for a power effort. A type with no distance or power effort
    returns an empty dict.
    """
    points = _read_points(path)
    if not points:
        return {}
    limit = SPEED_LIMIT_KMH.get(activity_type)
    out: dict[str, tuple[float, int]] = {}

    distances = RUN_DISTANCES if activity_type in _RUN_TYPES else RIDE_DISTANCES if activity_type in _RIDE_TYPES else {}
    if distances:
        stream = _clean(points, limit)
        for name, target in distances.items():
            found = _fastest_for_distance(stream, target)
            if found is not None:
                out[name] = found

    if activity_type in _RIDE_TYPES:
        for name, window in POWER_WINDOWS.items():
            found = _best_power(points, window)
            if found is not None:
                out[name] = found
    return out


# --- Public: compute across the archive -----------------------------------------------------------------

def _work_list(conn: sqlite3.Connection, only_new: bool) -> list[tuple[str, str, str, str, str]]:
    """(source, source_id, activity_type, relative path, sha256) for the files to process, in one query.

    The activity type is joined in from the source tables (Garmin and Strava types are normalised below), and
    the efforts ledger decides what to skip. With only_new, a file is skipped when it has a ledger row whose
    status is 'done' or 'empty' and whose file_sha256 still matches files.sha256 — so finished and no-effort
    files are left alone, a changed file (new hash) is redone, and an 'unreadable' one is retried. With
    only_new False (--all) every file is returned. No file is read off disk to make this decision.
    """
    from .types import normalise

    rows = conn.execute(
        "SELECT f.source AS source, f.source_id AS source_id, f.path AS path, f.sha256 AS sha256, "
        "       ga.activity_type AS g_type, sa.activity_type AS s_type, oa.activity_type AS o_type, "
        "       er.status AS status, er.file_sha256 AS led_sha "
        "FROM files f "
        "LEFT JOIN garmin_activities ga ON f.source='garmin' AND ga.garmin_id = f.source_id "
        "LEFT JOIN strava_activities sa ON f.source='strava' AND sa.strava_id = f.source_id "
        "LEFT JOIN source_activities oa ON oa.source = f.source AND oa.source_id = f.source_id "
        "LEFT JOIN effort_runs er ON er.source = f.source AND er.source_id = f.source_id"
    ).fetchall()
    out = []
    for r in rows:
        if only_new and r["status"] in ("done", "empty") and r["led_sha"] == r["sha256"]:
            continue
        if r["source"] == "garmin" and r["g_type"] is not None:
            atype = normalise("garmin", r["g_type"])
        elif r["source"] == "strava" and r["s_type"] is not None:
            atype = normalise("strava", r["s_type"])
        elif r["o_type"] is not None:
            atype = r["o_type"]  # source_activities already holds the shared type
        else:
            continue  # the source record is gone; nothing to type the file as
        out.append((r["source"], str(r["source_id"]), atype, r["path"], r["sha256"]))
    return out


def _skipped_count(conn: sqlite3.Connection) -> int:
    """How many files the ledger lets this run skip (done/empty with a matching hash)."""
    return conn.execute(
        "SELECT COUNT(*) FROM files f JOIN effort_runs er ON er.source=f.source AND er.source_id=f.source_id "
        "WHERE er.status IN ('done','empty') AND er.file_sha256 = f.sha256"
    ).fetchone()[0]


@dataclass
class EffortPlan:
    """What an efforts run is about to do, for an informative header before any work starts."""
    total_files: int       # original files recorded in the archive
    already_done: int      # files the ledger lets this run skip (only_new)
    to_process: int        # files this run will read


def plan(conn: sqlite3.Connection, only_new: bool = True) -> EffortPlan:
    """Count what a `run` would do, without doing it, so the CLI can explain the run before the bar moves."""
    _ensure_ledger(conn)
    total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    skipped = _skipped_count(conn) if only_new else 0
    return EffortPlan(total_files=total, already_done=skipped, to_process=len(_work_list(conn, only_new)))


def _ensure_ledger(conn: sqlite3.Connection) -> None:
    """Create the effort_runs ledger if it's missing.

    schema.sql defines it, but an archive that reached schema v4 before the ledger was added to v4 won't have
    it (migrate does nothing once the version matches). Creating it here, matching schema.sql, keeps the module
    self-contained and such an archive working without a version bump.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS effort_runs ("
        "source TEXT NOT NULL, source_id TEXT NOT NULL, file_sha256 TEXT NOT NULL, "
        "status TEXT NOT NULL CHECK (status IN ('done', 'empty', 'unreadable')), "
        "n_efforts INTEGER NOT NULL DEFAULT 0, computed_utc TEXT NOT NULL, "
        "PRIMARY KEY (source, source_id))"
    )


def _seed_ledger_from_existing(conn: sqlite3.Connection, now: str) -> None:
    """One-time: seed the ledger from efforts an earlier build wrote before the ledger existed.

    An archive that computed efforts under an older version has activity_efforts rows but an empty effort_runs
    ledger; without this, the next run would reparse every file. Each file that already has efforts becomes a
    'done' row carrying its current files.sha256, so the run resumes. Guarded to run only on an empty ledger, so
    it's a cheap no-op once seeded and never overwrites real entries.
    """
    if conn.execute("SELECT 1 FROM effort_runs LIMIT 1").fetchone():
        return
    if not conn.execute("SELECT 1 FROM activity_efforts LIMIT 1").fetchone():
        return
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO effort_runs (source, source_id, file_sha256, status, n_efforts, computed_utc) "
            "SELECT e.source, e.source_id, f.sha256, 'done', COUNT(*), ? "
            "FROM activity_efforts e JOIN files f ON f.source = e.source AND f.source_id = e.source_id "
            "GROUP BY e.source, e.source_id",
            (now,),
        )


def _record_run(conn, source, source_id, sha, status, n, now) -> None:
    conn.execute(
        "INSERT INTO effort_runs (source, source_id, file_sha256, status, n_efforts, computed_utc) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(source, source_id) DO UPDATE SET "
        "file_sha256=excluded.file_sha256, status=excluded.status, n_efforts=excluded.n_efforts, "
        "computed_utc=excluded.computed_utc",
        (source, source_id, sha, status, n, now),
    )


def run(conn: sqlite3.Connection, data_dir: Path, only_new: bool = True,
        progress: "Callable[[int, int], None] | None" = None) -> EffortCounts:
    """Compute best efforts for every original file (or, by default, only ones not yet done) into activity_efforts.

    Resumable and crash-safe: each file's efforts and its ledger row commit in one transaction, so a stop leaves
    a file either fully done or untouched; a rerun picks up where it left off from the effort_runs ledger, never
    reparsing a finished or no-effort file (design, efforts ledger). A file whose bytes changed (new sha) is
    redone. Re-running is idempotent. For Garmin activities, each computed effort is compared with Garmin's own
    fastest split and counted when it differs by more than 2%. Called by `minou-sync efforts`, and after
    import, import-strava and sync. With only_new False (--all) every file is reprocessed.

    `progress`, if given, is called with (files_processed, files_to_process) after each file, so the CLI can show
    a progress bar over just the outstanding work. The module itself prints nothing.
    """
    counts = EffortCounts()
    now = db.utc_now()
    _ensure_ledger(conn)
    _seed_ledger_from_existing(conn, now)
    work = _work_list(conn, only_new)
    counts.skipped = _skipped_count(conn) if only_new else 0
    total = len(work)
    for done, (source, source_id, atype, rel, sha) in enumerate(work, start=1):
        if progress is not None:
            progress(done, total)
        path = data_dir / rel
        if not path.is_file():
            # Don't record a ledger row: the file may appear later, and it should be retried then.
            counts.files_missing += 1
            continue
        try:
            efforts = compute(path, atype)
        except Exception as e:  # a malformed file shouldn't crash the whole run
            log.warning("%s couldn't be processed for efforts (%s); recorded for retry", rel, e)
            with conn:
                _record_run(conn, source, source_id, sha, "unreadable", 0, now)
            counts.unreadable += 1
            continue
        with conn:
            # Replace this file's efforts atomically, so a changed file can't leave stale rows behind.
            conn.execute("DELETE FROM activity_efforts WHERE source = ? AND source_id = ?", (source, source_id))
            for effort, (value, offset) in efforts.items():
                conn.execute(
                    "INSERT INTO activity_efforts (source, source_id, effort, value, start_offset_s, computed_utc) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (source, source_id, effort, value, offset, now),
                )
            _record_run(conn, source, source_id, sha, "done" if efforts else "empty", len(efforts), now)
        counts.efforts += len(efforts)
        if efforts:
            counts.activities += 1
        else:
            counts.empty += 1
        if source == "garmin" and efforts:
            _cross_check_garmin(conn, source_id, efforts, counts)
    return counts


def _cross_check_garmin(conn: sqlite3.Connection, garmin_id: str, efforts: dict[str, tuple[float, int]],
                        counts: EffortCounts) -> None:
    """Count how many computed distance efforts differ from Garmin's own fastest splits by over 2%.

    Garmin stores per-activity best splits in get_activity's splitSummaries; the DISTANCE_* entries give the
    fastest time for a standard distance. Only the distances Garmin reports are compared.
    """
    import json

    raw = conn.execute("SELECT raw_json FROM garmin_raw WHERE endpoint='get_activity' AND key=?",
                       (garmin_id,)).fetchone()
    if raw is None:
        return
    try:
        detail = json.loads(raw["raw_json"])
    except (ValueError, TypeError):
        return
    garmin_splits = _garmin_fastest_splits(detail)
    checked = False
    for effort, (value, _offset) in efforts.items():
        target = RUN_DISTANCES.get(effort) or RIDE_DISTANCES.get(effort)
        if target is None:
            continue
        garmin_time = garmin_splits.get(round(target))
        if garmin_time is None or garmin_time <= 0:
            continue
        checked = True
        if abs(value - garmin_time) / garmin_time > 0.02:
            counts.garmin_differs += 1
    if checked:
        counts.garmin_checked += 1


def _garmin_fastest_splits(detail: dict[str, Any]) -> dict[int, float]:
    """Garmin's fastest time (s) per standard distance (m), from get_activity's splitSummaries DISTANCE_* rows."""
    out: dict[int, float] = {}
    for summary in detail.get("splitSummaries") or []:
        if summary.get("splitType", "").startswith("DISTANCE") and summary.get("distance") and summary.get("duration"):
            out[round(summary["distance"])] = summary["duration"]
    return out
