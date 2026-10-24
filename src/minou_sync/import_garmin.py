"""Garmin account export import: activities, original files, daily wellness and weigh-ins.

Garmin's export (Account → Data Management → Export Your Data) is a zip, or an unpacked folder, with everything
under DI_CONNECT/. The units differ from the API's; parse_activity and the parse_* functions say which. This
reads:

- DI-Connect-Fitness/*_summarizedActivities.json → garmin_activities (origin 'export')
- DI-Connect-Uploaded-Files/UploadedFiles_*.zip, nested zips of FIT files → files/garmin/<activity_id>.fit in the data folder.
  The files are named by upload, not by activity, so each activity recording is matched to the activity that
  starts closest to its file_id time_created (within MATCH_WINDOW_S).
- DI-Connect-Aggregator/UDSFile_*.json, DI-Connect-Wellness/*_sleepData.json, *_healthStatusData.json and
  DI-Connect-Metrics/TrainingReadinessDTO_*.json → daily_wellness
- DI-Connect-Wellness/*_userBioMetrics.json → weigh_ins

The export and the API hold the same activities and days, and the API's copy is richer, so the export fills what
isn't there and never overwrites what sync stored: an activity sync saved is left alone, a day only gets its empty
fields filled, and a stored weigh-in is kept. Safe to run again.
"""

from __future__ import annotations

import bisect
import hashlib
import io
import json
import logging
import re
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from . import db

log = logging.getLogger(__name__)

ACTIVITIES = re.compile(r"DI-Connect-Fitness/[^/]*_summarizedActivities\.json$")
UPLOADED_FILES = re.compile(r"DI-Connect-Uploaded-Files/[^/]*\.zip$")
DAILY_SUMMARY = re.compile(r"DI-Connect-Aggregator/UDSFile_[^/]*\.json$")
SLEEP = re.compile(r"DI-Connect-Wellness/[^/]*_sleepData\.json$")
HEALTH_STATUS = re.compile(r"DI-Connect-Wellness/[^/]*_healthStatusData\.json$")
READINESS = re.compile(r"DI-Connect-Metrics/TrainingReadinessDTO_[^/]*\.json$")
BIOMETRICS = re.compile(r"DI-Connect-Wellness/[^/]*_userBioMetrics\.json$")
KNOWN = (ACTIVITIES, UPLOADED_FILES, DAILY_SUMMARY, SLEEP)

# An activity recording's file_id time_created is its start time; on a real export all 654 recordings were
# within a few seconds of their activity's start, and byte for byte the files the API serves.
MATCH_WINDOW_S = 120
# The export gives calories in kilojoules: exactly 4.19 times the API's kilocalories on every activity checked.
KJ_PER_KCAL = 4.19
WELLNESS_FIELDS = ("resting_hr", "hrv_avg_ms", "hrv_status", "sleep_s", "sleep_score", "deep_s", "light_s",
                   "rem_s", "awake_s", "body_battery_high", "body_battery_low", "stress_avg", "steps",
                   "readiness_score")


class NotAnExportError(ValueError):
    pass


@dataclass
class ImportCounts:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    from_sync: int = 0          # already in the archive from sync or backfill, left as they are
    files: int = 0
    wellness_days: int = 0      # days added, or with empty fields filled in
    weigh_ins: int = 0
    null_fields: int = 0


class _Export:
    """Members of a Garmin export, from a zip or an unpacked folder, found by pattern."""

    def __init__(self, path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(f"{path} not found")
        self._zip = zipfile.ZipFile(path) if path.is_file() else None
        self._dir = path
        if self._zip is not None:
            self._names = [n for n in self._zip.namelist() if not n.endswith("/")]
        else:
            self._names = [p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()]
        if not any(p.search(n) for p in KNOWN for n in self._names):
            raise NotAnExportError(
                f"This doesn't look like a Garmin export: DI_CONNECT/DI-Connect-Fitness/*_summarizedActivities"
                f".json not found in {path}")

    def members(self, pattern: re.Pattern) -> list[str]:
        return sorted(n for n in self._names if pattern.search(n))

    def read(self, name: str) -> bytes:
        return self._zip.read(name) if self._zip is not None else (self._dir / name).read_bytes()

    def json(self, pattern: re.Pattern) -> Iterator[Any]:
        """Each item of every JSON file that matches, flattening the top-level list most of these files use."""
        for name in self.members(pattern):
            try:
                data = json.loads(self.read(name))
            except ValueError as e:
                log.warning("Garmin export: %s isn't valid JSON (%s); skipped", name, e)
                continue
            yield from data if isinstance(data, list) else [data]


def _number(raw: dict[str, Any], key: str, counts: ImportCounts | None) -> float | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        log.warning("Garmin activity %s: %s %r isn't a number; stored as NULL", raw.get("activityId"), key, value)
        if counts is not None:
            counts.null_fields += 1
        return None
    return float(value)


def _scaled(value: float | None, divide: float) -> float | None:
    return None if value is None else value / divide


def _int(value: float | None) -> int | None:
    return None if value is None else round(value)


def _iso(epoch_ms: float) -> datetime:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).replace(microsecond=0)


def parse_activity(raw: dict[str, Any], imported_utc: str, counts: ImportCounts | None = None) -> dict[str, Any]:
    """Map one summarizedActivities item to a garmin_activities row, in the same units sync stores.

    The export uses centimetres for distance and elevation, milliseconds for durations and times, kilojoules
    for calories, and gives startTimeLocal as the local clock time in epoch milliseconds. Missing or unparseable
    fields become NULL. Raises ValueError without an ID, type or start time."""
    start_ms = raw.get("startTimeGmt", raw.get("beginTimestamp"))
    if raw.get("activityId") is None or not raw.get("activityType") or not isinstance(start_ms, (int, float)):
        raise ValueError("no activityId, activityType or start time")
    start = _iso(start_ms)
    local_ms = raw.get("startTimeLocal")
    local = _iso(local_ms).replace(tzinfo=None) if isinstance(local_ms, (int, float)) else None
    num = lambda key: _number(raw, key, counts)  # noqa: E731
    cadence = num("avgDoubleCadence") if raw.get("avgDoubleCadence") is not None else num("avgBikeCadence")
    cadence = cadence or None  # the export gives 0 where the activity has no cadence; the API gives nothing
    return {
        "garmin_id": int(raw["activityId"]),
        "activity_type": raw["activityType"],
        "name": raw.get("name"),
        "start_utc": start.isoformat().replace("+00:00", "Z"),
        "start_local": local.isoformat() if local else None,
        "utc_offset_min": round((local - start.replace(tzinfo=None)).total_seconds() / 60) if local else None,
        "elapsed_s": _int(_scaled(num("elapsedDuration"), 1000)),
        "moving_s": _int(_scaled(num("movingDuration"), 1000)),
        "distance_m": _scaled(num("distance"), 100),
        "elev_gain_m": _scaled(num("elevationGain"), 100),
        "elev_loss_m": _scaled(num("elevationLoss"), 100),
        "avg_hr": _int(num("avgHr")),
        "max_hr": _int(num("maxHr")),
        "avg_watts": num("avgPower"),
        "max_watts": num("maxPower"),
        "norm_watts": num("normPower"),
        "avg_cadence": cadence,
        "calories": _scaled(num("calories"), KJ_PER_KCAL),
        "aerobic_te": num("aerobicTrainingEffect"),
        "anaerobic_te": num("anaerobicTrainingEffect"),
        "training_load": num("activityTrainingLoad"),
        "origin": "export",
        "raw_json": json.dumps(raw, sort_keys=True, ensure_ascii=False),
        "imported_utc": imported_utc,
    }


def _not_negative(value: Any) -> Any:
    # Garmin uses -1 and -2 for "not enough data" in stress and Body Battery.
    return None if not isinstance(value, (int, float)) or value < 0 else value


def parse_daily_summary(raw: dict[str, Any]) -> dict[str, Any]:
    """Resting HR, steps, stress and Body Battery from one UDSFile day. The day's own resting HR is
    currentDayRestingHeartRate; the export's restingHeartRate is the seven-day average."""
    stress = next((a for a in (raw.get("allDayStress") or {}).get("aggregatorList") or []
                   if a.get("type") == "TOTAL"), {})
    battery = {s.get("bodyBatteryStatType"): s.get("statsValue")
               for s in (raw.get("bodyBattery") or {}).get("bodyBatteryStatList") or []}
    return {
        "resting_hr": _int(raw.get("currentDayRestingHeartRate")),
        "steps": raw.get("totalSteps"),
        "stress_avg": _not_negative(stress.get("averageStressLevel")),
        "body_battery_high": _not_negative(battery.get("HIGHEST")),
        "body_battery_low": _not_negative(battery.get("LOWEST")),
    }


def parse_sleep(raw: dict[str, Any]) -> dict[str, Any]:
    """Sleep stages and score for the night ending on calendarDate. Sleep time is deep + light + REM, as Garmin
    counts it; awake time is kept apart."""
    stages = [raw.get(k) for k in ("deepSleepSeconds", "lightSleepSeconds", "remSleepSeconds")]
    return {
        "sleep_s": sum(s for s in stages if s) if any(s is not None for s in stages) else None,
        "deep_s": raw.get("deepSleepSeconds"),
        "light_s": raw.get("lightSleepSeconds"),
        "rem_s": raw.get("remSleepSeconds"),
        "awake_s": raw.get("awakeSleepSeconds"),
        "sleep_score": (raw.get("sleepScores") or {}).get("overallScore"),
    }


def parse_health_status(raw: dict[str, Any]) -> dict[str, Any]:
    """The overnight HRV average from one health-status day. Garmin's HRV status (balanced, unbalanced) isn't in
    the export, and the health-status 'IN_RANGE' is a different measure, so hrv_status stays empty here."""
    hrv = next((m for m in raw.get("metrics") or [] if m.get("type") == "HRV"), {})
    return {"hrv_avg_ms": hrv.get("value")}


def wellness_days(export: _Export) -> dict[str, dict[str, Any]]:
    """Every day's daily_wellness fields from the export, by date. Readiness is recalculated during the day, so
    the latest entry for a date is its score."""
    days: dict[str, dict[str, Any]] = {}

    def merge(day: Any, values: dict[str, Any]) -> None:
        if isinstance(day, str) and len(day) >= 10:
            row = days.setdefault(day[:10], {})
            row.update({k: v for k, v in values.items() if v is not None})

    for raw in export.json(DAILY_SUMMARY):
        merge(raw.get("calendarDate"), parse_daily_summary(raw))
    for raw in export.json(SLEEP):
        merge(raw.get("calendarDate"), parse_sleep(raw))
    for raw in export.json(HEALTH_STATUS):
        merge(raw.get("calendarDate"), parse_health_status(raw))
    latest: dict[str, tuple[str, Any]] = {}
    for raw in export.json(READINESS):
        day, score = raw.get("calendarDate"), raw.get("score")
        if isinstance(day, str) and score is not None and (raw.get("timestamp") or "") >= latest.get(day, ("",))[0]:
            latest[day] = (raw.get("timestamp") or "", score)
    for day, (_, score) in latest.items():
        merge(day, {"readiness_score": score})
    return {d: v for d, v in days.items() if v}


def weigh_in_row(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Map one userBioMetrics entry that carries a weight. Garmin gives weight and muscle mass in grams."""
    w = raw.get("weight") if isinstance(raw.get("weight"), dict) else None
    if not w or not isinstance(w.get("weight"), (int, float)) or not w.get("timestampGMT"):
        return None
    try:
        measured = datetime.fromisoformat(w["timestampGMT"]).replace(microsecond=0)
    except ValueError:
        return None
    muscle = w.get("muscleMass")
    return {
        "measured_utc": measured.isoformat() + "Z",
        "weight_kg": w["weight"] / 1000,
        "body_fat_pct": w.get("bodyFat"),
        "muscle_mass_kg": muscle / 1000 if isinstance(muscle, (int, float)) else None,
        "body_water_pct": w.get("bodyWater"),
        "source": "garmin",
    }


def _activity_recordings(export: _Export) -> Iterator[tuple[datetime, bytes]]:
    """(time_created, bytes) for every activity recording in the nested upload zips. Monitoring, sleep and other
    watch files are skipped by their FIT file type."""
    from fitparse import FitFile, FitParseError

    for part in export.members(UPLOADED_FILES):
        try:
            inner = zipfile.ZipFile(io.BytesIO(export.read(part)))
        except zipfile.BadZipFile:
            log.warning("Garmin export: %s isn't a zip; skipped", part)
            continue
        with inner:
            for name in inner.namelist():
                if not name.lower().endswith(".fit"):
                    continue
                blob = inner.read(name)
                try:
                    file_id = next(FitFile(io.BytesIO(blob), check_crc=False).get_messages("file_id"), None)
                except (FitParseError, ValueError, KeyError, TypeError, IndexError):
                    continue
                created = file_id.get_value("time_created") if file_id else None
                if file_id is None or file_id.get_value("type") != "activity" or created is None:
                    continue
                yield created.replace(tzinfo=timezone.utc), blob


def _match_files(export: _Export, starts: list[tuple[float, int]]) -> dict[int, bytes]:
    """The closest activity recording within MATCH_WINDOW_S for each (start epoch, garmin_id), sorted by start."""
    keys = [s for s, _ in starts]
    best: dict[int, tuple[float, bytes]] = {}
    for created, blob in _activity_recordings(export):
        t = created.timestamp()
        i = bisect.bisect_left(keys, t)
        near = [j for j in (i - 1, i) if 0 <= j < len(keys)]
        if not near:
            continue
        j = min(near, key=lambda j: abs(keys[j] - t))
        gap, garmin_id = abs(keys[j] - t), starts[j][1]
        if gap <= MATCH_WINDOW_S and gap < best.get(garmin_id, (float("inf"),))[0]:
            best[garmin_id] = (gap, blob)
    return {g: blob for g, (_, blob) in best.items()}


def _save_file(conn: sqlite3.Connection, data_dir: Path, garmin_id: int, content: bytes) -> None:
    rel = Path("files", "garmin", f"{garmin_id}.fit")
    (data_dir / rel).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    (data_dir / rel).write_bytes(content)
    db.upsert(conn, "files", "path", {
        "path": rel.as_posix(), "source": "garmin", "source_id": garmin_id, "format": "fit",
        "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
    })


def _import_wellness(conn: sqlite3.Connection, export: _Export, counts: ImportCounts) -> None:
    days = wellness_days(export)
    # The export's newest day was still under way when Garmin built it, so it stays open for sync to finish.
    last = max(days, default=None)
    for day, values in sorted(days.items()):
        with conn:
            existing = conn.execute("SELECT * FROM daily_wellness WHERE date = ?", (day,)).fetchone()
            if existing is None:
                row = {"date": day, **{f: values.get(f) for f in WELLNESS_FIELDS}, "complete": int(day != last),
                       "source": "garmin", "updated_utc": db.utc_now()}
            else:
                gaps = {f: values[f] for f in WELLNESS_FIELDS if existing[f] is None and values.get(f) is not None}
                if not gaps:
                    continue
                row = {"date": day, **gaps, "updated_utc": db.utc_now()}
            if db.upsert(conn, "daily_wellness", "date", row) != "unchanged":
                counts.wellness_days += 1


def _import_weigh_ins(conn: sqlite3.Connection, export: _Export, counts: ImportCounts) -> None:
    for raw in export.json(BIOMETRICS):
        row = weigh_in_row(raw) if isinstance(raw, dict) else None
        if row is None:
            continue
        with conn:
            if conn.execute("SELECT 1 FROM weigh_ins WHERE measured_utc = ?", (row["measured_utc"],)).fetchone():
                continue
            db.upsert(conn, "weigh_ins", "measured_utc", row)
            counts.weigh_ins += 1


def run(conn: sqlite3.Connection, path: Path, data_dir: Path,
        progress: "Callable[[int, int], None] | None" = None) -> ImportCounts:
    """Import a Garmin account export (zip or folder). Safe to run again: nothing already stored is changed.

    `progress`, if given, is called with (activities_done, total) after each activity. The module prints
    nothing itself."""
    export = _Export(path)
    counts = ImportCounts()
    now = db.utc_now()
    # Each file is [{"summarizedActivitiesExport": [activity, ...]}].
    activities = [a for chunk in export.json(ACTIVITIES) if isinstance(chunk, dict)
                  for a in chunk.get("summarizedActivitiesExport") or [] if isinstance(a, dict)]
    for done, raw in enumerate(activities, start=1):
        if progress is not None:
            progress(done, len(activities))
        try:
            row = parse_activity(raw, now, counts)
        except (ValueError, TypeError, OverflowError) as e:
            log.warning("Garmin export activity %s skipped: %s", raw.get("activityId", "without an ID"), e)
            counts.failed += 1
            continue
        # One transaction per activity keeps write locks short while a sync may be running.
        with conn:
            existing = conn.execute("SELECT origin FROM garmin_activities WHERE garmin_id = ?",
                                    (row["garmin_id"],)).fetchone()
            if existing is not None and existing["origin"] == "sync":
                counts.from_sync += 1
                continue
            result = db.upsert(conn, "garmin_activities", "garmin_id", row)
            setattr(counts, result, getattr(counts, result) + 1)

    # Original files, for every Garmin activity in the archive that has none yet, so an export that comes in
    # several zips can bring its files in a later part. Multisport legs share their parent's recording and start
    # time, so only the parent is matched.
    wanted = sorted(
        (datetime.fromisoformat(r["start_utc"].replace("Z", "+00:00")).timestamp(), r["garmin_id"])
        for r in conn.execute(
            "SELECT garmin_id, start_utc FROM garmin_activities WHERE json_extract(raw_json, '$.parentId') IS NULL "
            "AND CAST(garmin_id AS TEXT) NOT IN (SELECT source_id FROM files WHERE source = 'garmin')"))
    if wanted:
        for garmin_id, content in sorted(_match_files(export, wanted).items()):
            with conn:
                _save_file(conn, data_dir, garmin_id, content)
            counts.files += 1

    _import_wellness(conn, export, counts)
    _import_weigh_ins(conn, export, counts)
    return counts
