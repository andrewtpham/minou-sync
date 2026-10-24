"""Garmin sync and full-history backfill: activities, original files, every daily endpoint and account snapshots.

Every response is stored whole in garmin_raw; daily_wellness and weigh_ins are summaries picked from it.
Every call goes through GarminClient, the read-only, paced wrapper.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from garminconnect import Garmin, GarminConnectConnectionError

from . import db
from .garmin import GarminClient, RateLimitedError, RequestCapReachedError

log = logging.getLogger(__name__)

DEFAULT_DAYS = 14
MAX_DAYS = 90
BACKFILL_PAGE = 100

# Daily endpoints that return data for this account, probed 2026-10-19. Left out: get_all_day_stress (same
# response as get_stress_data), get_rhr_day (in get_user_summary), get_all_day_events (always empty) and the
# reproductive-health and blood pressure endpoints.
DAILY_ENDPOINTS = (
    "get_user_summary", "get_sleep_data", "get_hrv_data", "get_training_readiness", "get_training_status",
    "get_max_metrics", "get_stress_data", "get_heart_rates", "get_respiration_data", "get_spo2_data",
    "get_steps_data", "get_floors", "get_intensity_minutes_data", "get_body_battery_events",
    "get_fitnessage_data", "get_lifestyle_logging_data",
)
# Per activity; get_activity_exercise_sets is added when the summary lists sets.
ACTIVITY_ENDPOINTS = ("get_activity", "get_activity_weather")
# Account-wide, no arguments; refreshed on every sync.
SNAPSHOT_ENDPOINTS = (
    "get_personal_record", "get_devices", "get_primary_training_device", "get_user_profile",
    "get_userprofile_settings", "get_cycling_ftp", "get_lactate_threshold", "get_earned_badges",
    "get_training_plans", "get_activity_types",
)
# Account-wide over a date range. Garmin refuses ranges over 365 days, so these are fetched in windows
# anchored on the first activity date and keyed by window start.
RANGE_WINDOW_DAYS = 365
RANGE_ENDPOINTS = {
    "get_endurance_score": (), "get_hill_score": (), "get_race_predictions": ("daily",),
    "get_running_tolerance": ("daily",),
}
FILE_FORMATS = ("fit", "gpx", "tcx")

WELLNESS_FIELDS = (
    "resting_hr", "hrv_avg_ms", "hrv_status", "sleep_s", "sleep_score", "deep_s", "light_s", "rem_s", "awake_s",
    "body_battery_high", "body_battery_low", "stress_avg", "steps", "readiness_score",
)


@dataclass
class SyncCounts:
    first_day: date
    last_day: date
    activities: dict[str, int]
    files: int = 0
    wellness_days: int = 0
    weigh_ins: int = 0
    requests: int = 0
    # Set when the run stopped early: why, and the first day that isn't fully saved.
    stopped: str | None = None
    resume_from: date | None = None


def _int(v: Any) -> int | None:
    return None if v is None else round(v)


def _not_negative(v: Any) -> Any:
    # Garmin uses -1 and -2 for "not enough data" in stress and Body Battery.
    return None if v is None or v < 0 else v


def _iso_utc(garmin_gmt: str) -> str:
    return garmin_gmt.replace(" ", "T") + "Z"


def activity_row(raw: dict[str, Any], imported_utc: str) -> dict[str, Any]:
    """Map one get_activities_by_date item to a garmin_activities row. Missing fields become NULL."""
    start_utc = datetime.fromisoformat(raw["startTimeGMT"])
    start_local = datetime.fromisoformat(raw["startTimeLocal"]) if raw.get("startTimeLocal") else None
    cadence = raw.get("averageBikingCadenceInRevPerMinute")
    if cadence is None:
        cadence = raw.get("averageRunningCadenceInStepsPerMinute")
    return {
        "garmin_id": raw["activityId"],
        "activity_type": raw["activityType"]["typeKey"],
        "name": raw.get("activityName"),
        "start_utc": _iso_utc(raw["startTimeGMT"]),
        "start_local": start_local.isoformat() if start_local else None,
        "utc_offset_min": round((start_local - start_utc).total_seconds() / 60) if start_local else None,
        "elapsed_s": _int(raw.get("elapsedDuration")),
        "moving_s": _int(raw.get("movingDuration")),
        "distance_m": raw.get("distance"),
        "elev_gain_m": raw.get("elevationGain"),
        "elev_loss_m": raw.get("elevationLoss"),
        "avg_hr": _int(raw.get("averageHR")),
        "max_hr": _int(raw.get("maxHR")),
        "avg_watts": raw.get("avgPower"),
        "max_watts": raw.get("maxPower"),
        "norm_watts": raw.get("normPower"),
        "avg_cadence": cadence,
        "calories": raw.get("calories"),
        "aerobic_te": raw.get("aerobicTrainingEffect"),
        "anaerobic_te": raw.get("anaerobicTrainingEffect"),
        "training_load": raw.get("activityTrainingLoad"),
        "origin": "sync",
        "raw_json": json.dumps(raw, sort_keys=True),
        "imported_utc": imported_utc,
    }


def summary_from_detail(detail: dict[str, Any], parent_id: int | None = None) -> dict[str, Any]:
    """Reshape a get_activity response like a get_activities item, so activity_row can map it.

    Used for multisport legs, which the activity list never returns.
    """
    s = detail.get("summaryDTO") or {}
    meta = detail.get("metadataDTO") or {}

    def plain(t: str | None) -> str | None:
        # Detail times look like 2026-10-18T07:00:00.0; the list uses 2026-10-18 07:00:00.
        return datetime.fromisoformat(t).strftime("%Y-%m-%d %H:%M:%S") if t else None

    return {
        "activityId": detail["activityId"],
        "activityName": detail.get("activityName"),
        "activityType": detail.get("activityTypeDTO") or {},
        "startTimeGMT": plain(s.get("startTimeGMT")),
        "startTimeLocal": plain(s.get("startTimeLocal")),
        "elapsedDuration": s.get("elapsedDuration"),
        "movingDuration": s.get("movingDuration"),
        "distance": s.get("distance"),
        "elevationGain": s.get("elevationGain"),
        "elevationLoss": s.get("elevationLoss"),
        "averageHR": s.get("averageHR"),
        "maxHR": s.get("maxHR"),
        "avgPower": s.get("averagePower"),
        "maxPower": s.get("maxPower"),
        "normPower": s.get("normalizedPower"),
        "averageBikingCadenceInRevPerMinute": s.get("averageBikeCadence"),
        "averageRunningCadenceInStepsPerMinute": s.get("averageRunCadence"),
        "calories": s.get("calories"),
        "aerobicTrainingEffect": s.get("trainingEffect"),
        "anaerobicTrainingEffect": s.get("anaerobicTrainingEffect"),
        "activityTrainingLoad": s.get("activityTrainingLoad"),
        "manualActivity": meta.get("manualActivity"),
        "parentId": parent_id,
    }


def wellness_values(sleep: dict[str, Any] | None, stats: dict[str, Any] | None, readiness: Any) -> dict[str, Any]:
    """Pick daily_wellness fields from the sleep, stats and training readiness responses for one date."""
    sleep = sleep or {}
    stats = stats or {}
    dto = sleep.get("dailySleepDTO") or {}
    scores = dto.get("sleepScores") or {}
    # Readiness is recalculated during the day; the latest entry is the day's score.
    entries = readiness if isinstance(readiness, list) else [readiness] if readiness else []
    entries = [e for e in entries if e and e.get("score") is not None]
    latest = max(entries, key=lambda e: e.get("timestamp") or "", default=None)
    resting_hr = stats.get("restingHeartRate") or sleep.get("restingHeartRate")
    return {
        "resting_hr": _int(resting_hr),
        "hrv_avg_ms": sleep.get("avgOvernightHrv"),
        "hrv_status": sleep.get("hrvStatus"),
        "sleep_s": dto.get("sleepTimeSeconds"),
        "sleep_score": (scores.get("overall") or {}).get("value"),
        "deep_s": dto.get("deepSleepSeconds"),
        "light_s": dto.get("lightSleepSeconds"),
        "rem_s": dto.get("remSleepSeconds"),
        "awake_s": dto.get("awakeSleepSeconds"),
        "body_battery_high": _not_negative(stats.get("bodyBatteryHighestValue")),
        "body_battery_low": _not_negative(stats.get("bodyBatteryLowestValue")),
        "stress_avg": _not_negative(stats.get("averageStressLevel")),
        "steps": stats.get("totalSteps"),
        "readiness_score": latest["score"] if latest else None,
    }


def weigh_in_row(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Map one dateWeightList item. Garmin reports weight and muscle mass in grams."""
    if raw.get("weight") is None or raw.get("timestampGMT") is None:
        return None
    measured = datetime.fromtimestamp(raw["timestampGMT"] / 1000, tz=timezone.utc)
    muscle = raw.get("muscleMass")
    return {
        "measured_utc": measured.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "weight_kg": raw["weight"] / 1000,
        "body_fat_pct": raw.get("bodyFat"),
        "muscle_mass_kg": muscle / 1000 if muscle is not None else None,
        "body_water_pct": raw.get("bodyWater"),
        "source": "garmin",
    }


def _start_dates(conn: sqlite3.Connection, today: date, days: int | None) -> tuple[date, date]:
    """First day to fetch for activities and for wellness."""
    if days is not None:
        first = today - timedelta(days=days - 1)
        return first, first
    default = today - timedelta(days=DEFAULT_DAYS - 1)
    # Inclusive: an activity recorded later on the latest stored day would otherwise be missed.
    latest_activity = conn.execute("SELECT MAX(substr(start_local, 1, 10)) FROM garmin_activities").fetchone()[0]
    latest_complete = conn.execute("SELECT MAX(date) FROM daily_wellness WHERE complete = 1").fetchone()[0]
    activities_from = date.fromisoformat(latest_activity) if latest_activity else default
    wellness_from = date.fromisoformat(latest_complete) + timedelta(days=1) if latest_complete else default
    return min(activities_from, today), min(wellness_from, today)


def _save_file(conn: sqlite3.Connection, data_dir: Path, garmin_id: int, blob: bytes) -> bool:
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        log.warning("Garmin activity %s: original download isn't a zip; skipped", garmin_id)
        return False
    with archive:
        members = [n for n in archive.namelist() if n.rsplit(".", 1)[-1].lower() in FILE_FORMATS]
        if not members:
            log.warning("Garmin activity %s: no FIT, GPX or TCX file in the download; skipped", garmin_id)
            return False
        content = archive.read(members[0])
    ext = members[0].rsplit(".", 1)[-1].lower()
    rel = Path("files", "garmin", f"{garmin_id}.{ext}")
    (data_dir / rel).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    (data_dir / rel).write_bytes(content)
    db.upsert(conn, "files", "path", {
        "path": rel.as_posix(), "source": "garmin", "source_id": garmin_id, "format": ext,
        "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
    })
    return True


def _download(conn, client: GarminClient, data_dir: Path, garmin_id: int, counts: SyncCounts) -> None:
    try:
        blob = client.call("download_activity", garmin_id, Garmin.ActivityDownloadFormat.ORIGINAL)
    except GarminConnectConnectionError as e:
        # Non-critical: the activity row is saved and the file is retried on a later run.
        log.warning("Garmin activity %s: original file download failed (%s)", garmin_id, e)
        return
    counts.files += _save_file(conn, data_dir, garmin_id, blob)


def _fetch_raw(conn: sqlite3.Connection, client: GarminClient, endpoint: str, key: str, *args: Any) -> Any:
    """Call a read-only endpoint and store its response whole, empty or not. Returns None if Garmin errored."""
    try:
        value = client.call(endpoint, *args)
    except GarminConnectConnectionError as e:
        # Non-critical: one endpoint failing (say, no data that far back) shouldn't stop the day or the run.
        log.warning("Garmin %s %s failed (%s)", endpoint, key, e)
        return None
    conn.execute(
        "INSERT INTO garmin_raw(endpoint, key, raw_json, fetched_utc) VALUES(?, ?, ?, ?) "
        "ON CONFLICT(endpoint, key) DO UPDATE SET raw_json=excluded.raw_json, fetched_utc=excluded.fetched_utc",
        (endpoint, key, json.dumps(value, sort_keys=True), db.utc_now()),
    )
    return value


def _has_raw(conn: sqlite3.Connection, endpoint: str, key: str) -> bool:
    return conn.execute("SELECT 1 FROM garmin_raw WHERE endpoint = ? AND key = ?", (endpoint, key)).fetchone() is not None


def _activity_details(conn, client: GarminClient, garmin_id: int, summary: dict[str, Any]) -> None:
    key = str(garmin_id)
    endpoints = list(ACTIVITY_ENDPOINTS)
    if summary.get("summarizedExerciseSets") or summary.get("totalSets"):
        endpoints.append("get_activity_exercise_sets")
    for endpoint in endpoints:
        if not _has_raw(conn, endpoint, key):
            _fetch_raw(conn, client, endpoint, key, garmin_id)


def range_windows(first_day: date, today: date) -> list[tuple[date, date]]:
    """Consecutive windows of at most RANGE_WINDOW_DAYS days from first_day to today."""
    windows = []
    start = first_day
    while start <= today:
        end = min(start + timedelta(days=RANGE_WINDOW_DAYS - 1), today)
        windows.append((start, end))
        start += timedelta(days=RANGE_WINDOW_DAYS)
    return windows


def _snapshots(conn, client: GarminClient, first_day: date, today: date, all_windows: bool) -> None:
    """Account-wide endpoints; range endpoints for every window, or only the latest one on a regular sync."""
    for endpoint in SNAPSHOT_ENDPOINTS:
        _fetch_raw(conn, client, endpoint, "")
    _fetch_raw(conn, client, "get_workouts", "", 0, BACKFILL_PAGE)
    windows = range_windows(first_day, today)
    for start, end in windows if all_windows else windows[-1:]:
        for endpoint, extra in RANGE_ENDPOINTS.items():
            _fetch_raw(conn, client, endpoint, start.isoformat(), start.isoformat(), end.isoformat(), *extra)


def _first_activity_day(conn: sqlite3.Connection, fallback: date) -> date:
    first = conn.execute("SELECT MIN(substr(start_local, 1, 10)) FROM garmin_activities").fetchone()[0]
    return date.fromisoformat(first) if first else fallback


def _multisport_legs(conn, client: GarminClient, data_dir: Path, counts: SyncCounts) -> None:
    """Fetch the legs of multisport activities; the activity list only returns the parent."""
    parents = conn.execute(
        "SELECT CAST(key AS INTEGER), json_extract(raw_json, '$.metadataDTO.childIds') FROM garmin_raw"
        " WHERE endpoint = 'get_activity' AND json_extract(raw_json, '$.isMultiSportParent') = 1"
    ).fetchall()
    known = {row[0] for row in conn.execute("SELECT garmin_id FROM garmin_activities")}
    for parent_id, child_ids in parents:
        for child_id in json.loads(child_ids or "[]"):
            if child_id in known:
                continue
            with conn:
                detail = _fetch_raw(conn, client, "get_activity", str(child_id), child_id)
                if not detail or not (detail.get("summaryDTO") or {}).get("startTimeGMT"):
                    continue
                summary = summary_from_detail(detail, parent_id)
                row = activity_row(summary, db.utc_now())
                # Keep the whole detail response, marked with its parent so reconcile can fold it in.
                row["raw_json"] = json.dumps({**detail, "parentId": parent_id}, sort_keys=True)
                counts.activities[db.upsert(conn, "garmin_activities", "garmin_id", row)] += 1
                if not summary["manualActivity"]:
                    _download(conn, client, data_dir, child_id, counts)
                _activity_details(conn, client, child_id, summary)


def _sync_activities(conn, client: GarminClient, data_dir: Path, day: date, counts: SyncCounts) -> None:
    now = db.utc_now()
    for raw in client.call("get_activities_by_date", day.isoformat(), day.isoformat()):
        row = activity_row(raw, now)
        counts.activities[db.upsert(conn, "garmin_activities", "garmin_id", row)] += 1
        has_file = conn.execute(
            "SELECT 1 FROM files WHERE source = 'garmin' AND source_id = ?", (row["garmin_id"],)
        ).fetchone()
        # Manual entries have no recording to download.
        if not has_file and not raw.get("manualActivity"):
            _download(conn, client, data_dir, row["garmin_id"], counts)
        _activity_details(conn, client, row["garmin_id"], raw)


def _sync_wellness(conn, client: GarminClient, day: date, today: date, counts: SyncCounts) -> None:
    d = day.isoformat()
    raw = {endpoint: _fetch_raw(conn, client, endpoint, d, d) for endpoint in DAILY_ENDPOINTS}
    fetched = wellness_values(raw["get_sleep_data"], raw["get_user_summary"], raw["get_training_readiness"])
    existing = conn.execute("SELECT * FROM daily_wellness WHERE date = ?", (d,)).fetchone()
    if existing is not None and existing["complete"]:
        # A finished day only gets its gaps filled.
        values = {f: existing[f] if existing[f] is not None else fetched[f] for f in WELLNESS_FIELDS}
    else:
        values = {f: fetched[f] if fetched[f] is not None else (existing[f] if existing else None)
                  for f in WELLNESS_FIELDS}
    if all(v is None for v in values.values()):
        return
    complete = 1 if day < today or (existing is not None and existing["complete"]) else 0
    db.upsert(conn, "daily_wellness", "date", {"date": d, **values, "complete": complete, "updated_utc": db.utc_now()})
    counts.wellness_days += 1


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)
    )


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _daily_gaps(conn: sqlite3.Connection, first_day: date, today: date) -> list[tuple[str, str]]:
    """(endpoint, date) pairs before today with no stored response."""
    have = {tuple(row) for row in conn.execute(
        "SELECT endpoint, key FROM garmin_raw WHERE key BETWEEN ? AND ?", (first_day.isoformat(), today.isoformat())
    )}
    days = [(first_day + timedelta(days=n)).isoformat() for n in range((today - first_day).days)]
    return [(e, d) for d in days for e in DAILY_ENDPOINTS if (e, d) not in have]


def backfill(
    conn: sqlite3.Connection, client: GarminClient, data_dir: Path, today: date | None = None,
    wellness_from: date | None = None,
) -> SyncCounts:
    """Fetch the whole Garmin history, resuming where the last run stopped.

    Order: the full activity list, weigh-ins, account snapshots, original files and activity details newest
    first, then every daily endpoint backwards from today to the first Garmin activity. Progress is kept in
    meta and garmin_raw, so runs can stop at the request cap and continue. `wellness_from` extends the daily
    walk to days before the first activity (the watch can record wellness before any workout).
    """
    today = today or date.today()
    counts = SyncCounts(today, today, {"added": 0, "updated": 0, "unchanged": 0})
    day: date | None = None
    try:
        if not _meta(conn, "backfill_activities_utc"):
            now = db.utc_now()
            start = 0
            while page := client.call("get_activities", start, BACKFILL_PAGE):
                with conn:
                    for raw in page:
                        counts.activities[db.upsert(conn, "garmin_activities", "garmin_id", activity_row(raw, now))] += 1
                start += len(page)
            with conn:
                _set_meta(conn, "backfill_activities_utc", now)

        if conn.execute("SELECT 1 FROM garmin_activities LIMIT 1").fetchone() is None:
            return counts
        first_day = _first_activity_day(conn, today)
        counts.first_day = first_day

        if not _meta(conn, "backfill_weigh_ins_utc"):
            composition = client.call("get_body_composition", first_day.isoformat(), today.isoformat())
            with conn:
                for raw in (composition or {}).get("dateWeightList") or []:
                    row = weigh_in_row(raw)
                    if row and db.upsert(conn, "weigh_ins", "measured_utc", row) != "unchanged":
                        counts.weigh_ins += 1
                _set_meta(conn, "backfill_weigh_ins_utc", db.utc_now())

        missing = conn.execute(
            "SELECT garmin_id FROM garmin_activities g WHERE NOT EXISTS ("
            " SELECT 1 FROM files f WHERE f.source = 'garmin' AND f.source_id = g.garmin_id)"
            " AND COALESCE(json_extract(raw_json, '$.manualActivity'), 0) = 0"
            " ORDER BY start_utc DESC"
        ).fetchall()
        for (garmin_id,) in missing:
            with conn:
                _download(conn, client, data_dir, garmin_id, counts)

        if not _meta(conn, "backfill_snapshots_utc"):
            with conn:
                _snapshots(conn, client, first_day, today, all_windows=True)
                _set_meta(conn, "backfill_snapshots_utc", db.utc_now())

        without_details = conn.execute(
            "SELECT garmin_id, raw_json FROM garmin_activities g WHERE NOT EXISTS ("
            " SELECT 1 FROM garmin_raw r WHERE r.endpoint = 'get_activity' AND r.key = CAST(g.garmin_id AS TEXT))"
            " ORDER BY start_utc DESC"
        ).fetchall()
        for garmin_id, raw_json in without_details:
            with conn:
                _activity_details(conn, client, garmin_id, json.loads(raw_json))
        _multisport_legs(conn, client, data_dir, counts)

        daily_from = min(first_day, wellness_from) if wellness_from else first_day

        # Walk backwards from today so the most recent history lands first; the cursor is the oldest day done.
        # It starts at today even where daily_wellness has rows, because those were saved before garmin_raw.
        cursor = _meta(conn, "backfill_daily_from") or (today + timedelta(days=1)).isoformat()
        day = date.fromisoformat(cursor) - timedelta(days=1)
        while day >= daily_from:
            with conn:
                _sync_wellness(conn, client, day, today, counts)
                _set_meta(conn, "backfill_daily_from", day.isoformat())
                # Replaced by backfill_daily_from in schema v2.
                conn.execute("DELETE FROM meta WHERE key = 'backfill_wellness_from'")
            day -= timedelta(days=1)
        day = None

        # Endpoints that errored on their day (timeouts, resets) are fetched again; each gap is one request.
        for endpoint, key in _daily_gaps(conn, daily_from, today):
            with conn:
                _fetch_raw(conn, client, endpoint, key, key)
        with conn:
            _set_meta(conn, "backfill_done_utc", db.utc_now())
    except (RequestCapReachedError, RateLimitedError) as e:
        counts.stopped = "cap" if isinstance(e, RequestCapReachedError) else "rate_limit"
        counts.resume_from = day
    counts.requests = client.requests
    return counts


def run(
    conn: sqlite3.Connection, client: GarminClient, data_dir: Path, days: int | None, today: date | None = None
) -> SyncCounts:
    """Fetch from the day after what's stored (or the last `days` days) to today, committing one day at a time."""
    if days is not None and not 1 <= days <= MAX_DAYS:
        raise ValueError(f"days must be 1 to {MAX_DAYS}")
    today = today or date.today()
    activities_from, wellness_from = _start_dates(conn, today, days)
    first = min(activities_from, wellness_from)
    counts = SyncCounts(first, today, {"added": 0, "updated": 0, "unchanged": 0})
    day = first
    try:
        with conn:
            composition = client.call("get_body_composition", wellness_from.isoformat(), today.isoformat())
            for raw in (composition or {}).get("dateWeightList") or []:
                row = weigh_in_row(raw)
                if row and db.upsert(conn, "weigh_ins", "measured_utc", row) != "unchanged":
                    counts.weigh_ins += 1
        while day <= today:
            with conn:
                if day >= activities_from:
                    _sync_activities(conn, client, data_dir, day, counts)
                if day >= wellness_from:
                    _sync_wellness(conn, client, day, today, counts)
            day += timedelta(days=1)
        _multisport_legs(conn, client, data_dir, counts)
        with conn:
            # After the days, so a first sync anchors range windows on its own activities.
            _snapshots(conn, client, _first_activity_day(conn, first), today, all_windows=False)
            _set_meta(conn, "last_sync_utc", db.utc_now())
    except (RequestCapReachedError, RateLimitedError) as e:
        # Earlier days are committed; the current day's transaction rolled back.
        counts.stopped = "cap" if isinstance(e, RequestCapReachedError) else "rate_limit"
        counts.resume_from = day
    counts.requests = client.requests
    return counts
