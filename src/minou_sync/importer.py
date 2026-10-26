"""General importer: CSV exports through TOML Profiles, and original FIT/GPX/TCX files.

One command, `minou-sync import <path> --source <name>`, reads a CSV, a folder or a zip. CSVs go through a
Profile (built in under minou_sync/profiles/, or a file path) that maps columns to minou-sync fields, names
the required columns, converts units and gives the date format. Original files are read for their summary (see
parse_original) and copied into files/<source>/ in the data folder. Every record is kept as it came in raw_json.
Everything is read from local files: an import makes no network requests, so no platform's API limits apply.

Strava's full account archive still goes through import_strava; this is for everything else.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import logging
import sqlite3
import tomllib
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable

from . import db

log = logging.getLogger(__name__)

FILE_FORMATS = ("fit", "gpx", "tcx")
KINDS = ("activities", "wellness", "weigh_ins", "body_composition", "lab_tests")

# How each kind lands in the archive: table, its key column, and the minou-sync fields a row may carry.
_INTEGER_FIELDS = frozenset({
    "elapsed_s", "moving_s", "avg_hr", "max_hr", "resting_hr", "sleep_s", "sleep_score", "deep_s", "light_s",
    "rem_s", "awake_s", "body_battery_high", "body_battery_low", "stress_avg", "steps", "readiness_score",
    "lt1_hr", "lt1_watts", "lt2_hr", "lt2_watts",
})


class NotACsvError(ValueError):
    pass


class MissingColumnError(ValueError):
    pass


class ProfileError(ValueError):
    pass


class NoProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    kind: str
    date_format: str          # "iso" for datetime.fromisoformat, else a strptime format
    tz: str                   # "utc" or "local": how to read a naive datetime
    columns: dict[str, str]   # minou-sync field -> CSV column name
    required: list[str]       # CSV column names that must be present
    units: dict[str, float]   # minou-sync field -> multiplier to the base unit


@dataclass(frozen=True)
class OriginalSummary:
    start_utc: str
    elapsed_s: int | None
    distance_m: float | None
    sport: str | None
    # Only FIT files carry these; GPX and TCX summaries leave them None.
    start_local: str | None = None
    moving_s: int | None = None
    elev_gain_m: float | None = None
    avg_hr: int | None = None
    max_hr: int | None = None
    avg_watts: float | None = None
    calories: float | None = None


@dataclass
class ImportCounts:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    files: int = 0
    null_fields: int = 0
    kinds: dict[str, int] = field(default_factory=dict)


# --- Profiles -------------------------------------------------------------------------------------------

def _parse_profile(name: str, raw: dict[str, Any]) -> Profile:
    kind = raw.get("kind")
    if kind not in KINDS:
        raise ProfileError(f"Profile {name}: kind must be one of {', '.join(KINDS)}, not {kind!r}")
    columns = raw.get("columns") or {}
    return Profile(
        name=name,
        kind=kind,
        date_format=raw.get("date_format", "iso"),
        tz=raw.get("timezone", "utc"),
        columns={k: str(v) for k, v in columns.items()},
        required=[str(c) for c in raw.get("required", [])],
        units={k: float(v) for k, v in (raw.get("units") or {}).items()},
    )


def _builtin_profiles() -> dict[str, Profile]:
    profiles = {}
    for resource in files("minou_sync.profiles").iterdir():
        if resource.name.endswith(".toml"):
            name = resource.name.removesuffix(".toml")
            profiles[name] = _parse_profile(name, tomllib.loads(resource.read_text()))
    return profiles


def load_profile(name_or_path: str) -> Profile:
    """Load a built-in profile by name (e.g. 'strava-activities') or a profile from a .toml file path."""
    path = Path(name_or_path)
    if path.suffix == ".toml" and path.is_file():
        return _parse_profile(path.stem, tomllib.loads(path.read_text()))
    profiles = _builtin_profiles()
    if name_or_path in profiles:
        return profiles[name_or_path]
    raise ProfileError(f"No profile {name_or_path!r}: built-ins are {', '.join(sorted(profiles))}, or give a .toml path")


def detect_kind(header: list[str]) -> Profile:
    """Pick the built-in profile whose required columns are all in the header. Raises if none or several match."""
    present = set(header)
    matches = [p for p in _builtin_profiles().values() if p.required and present.issuperset(p.required)]
    if not matches:
        raise NoProfileError(
            "No built-in profile matches this file's columns. Pass --profile with a profile name or a .toml "
            "path (see docs/import.md)."
        )
    # The most specific match (most required columns) wins; a tie is ambiguous.
    matches.sort(key=lambda p: len(p.required), reverse=True)
    if len(matches) > 1 and len(matches[0].required) == len(matches[1].required):
        names = ", ".join(sorted(p.name for p in matches if len(p.required) == len(matches[0].required)))
        raise NoProfileError(f"Several profiles match this file ({names}); pass --profile to choose one.")
    return matches[0]


# --- Values ---------------------------------------------------------------------------------------------

def _parse_datetime(value: str, profile: Profile) -> datetime:
    value = value.strip()
    dt = datetime.fromisoformat(value) if profile.date_format == "iso" else datetime.strptime(value, profile.date_format)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc if profile.tz == "utc" else None)
    return dt


def _to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:  # a local naive time with no offset: store as-is, no 'Z'
        return dt.replace(microsecond=0).isoformat()
    return dt.astimezone(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"


def _number(value: str, field_name: str, counts: ImportCounts) -> float | None:
    value = value.strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        log.warning("Column %s value %r isn't a number; stored as NULL", field_name, value)
        counts.null_fields += 1
        return None


def _field_value(row: dict[str, str], profile: Profile, field_name: str, counts: ImportCounts) -> Any:
    column = profile.columns.get(field_name)
    if column is None or column not in row:
        return None
    raw = row[column]
    if field_name in ("activity_type", "name", "hrv_status", "method", "sport", "protocol", "notes"):
        return raw.strip() or None
    number = _number(raw, column, counts)
    if number is None:
        return None
    number *= profile.units.get(field_name, 1.0)
    return round(number) if field_name in _INTEGER_FIELDS else number


# --- Original files -------------------------------------------------------------------------------------

# FIT (sport, sub_sport) pairs whose sub-sport says more than the sport, as type names types.py knows.
# Any other pair falls back to the sport alone ("running", "cycling", ...), which types.py also knows.
_FIT_SUB_SPORTS = {
    ("running", "trail"): "trail running",
    ("running", "track"): "track running",
    ("running", "treadmill"): "treadmill running",
    ("running", "indoor_running"): "indoor running",
    ("running", "virtual_activity"): "virtual run",
    ("cycling", "road"): "road biking",
    ("cycling", "indoor_cycling"): "indoor cycling",
    ("cycling", "spin"): "indoor cycling",
    ("cycling", "virtual_activity"): "virtual ride",
    ("cycling", "mountain"): "mountain biking",
    ("cycling", "gravel_cycling"): "gravel cycling",
    ("cycling", "cyclocross"): "gravel cycling",
    ("cycling", "e_bike_fitness"): "e-bike ride",
    ("swimming", "lap_swimming"): "lap swimming",
    ("swimming", "open_water"): "open water swimming",
    ("training", "strength_training"): "strength training",
    ("training", "cardio_training"): "cardio",
    ("training", "yoga"): "yoga",
    ("training", "pilates"): "pilates",
    ("training", "flexibility_training"): "mobility",
    ("fitness_equipment", "indoor_rowing"): "indoor rowing",
    ("fitness_equipment", "elliptical"): "elliptical",
    ("fitness_equipment", "stair_climbing"): "stair climbing",
    ("fitness_equipment", "treadmill"): "treadmill running",
    ("fitness_equipment", "indoor_cycling"): "indoor cycling",
}


def _fit_type(sport: Any, sub_sport: Any) -> str | None:
    """The activity type for a FIT sport and sub-sport. fitparse gives a name, or a number it doesn't know."""
    if sport is None:
        return None
    return _FIT_SUB_SPORTS.get((str(sport), str(sub_sport)), str(sport))


def _fit_naive_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def _fit_total(sessions: list[dict[str, Any]], name: str) -> float | None:
    values = [s[name] for s in sessions if s.get(name) is not None]
    return sum(values) if values else None


def _fit_weighted(sessions: list[dict[str, Any]], name: str) -> float | None:
    """A per-session average across sessions, weighted by each session's timer time (multisport legs)."""
    pairs = [(s[name], s.get("total_timer_time") or 0) for s in sessions if s.get(name) is not None]
    if not pairs:
        return None
    weight = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / weight if weight else sum(v for v, _ in pairs) / len(pairs)


def _fit_local_start(start: datetime, activity: dict[str, Any]) -> str | None:
    """The local start time, from the offset between the activity message's local and UTC timestamps."""
    local, utc = activity.get("local_timestamp"), activity.get("timestamp")
    if not isinstance(local, datetime) or not isinstance(utc, datetime):
        return None
    offset_s = (local - _fit_naive_utc(utc)).total_seconds()
    # Real offsets are whole quarter hours within ±14 h; anything else is a device clock problem, so leave it.
    quarter_hours = round(offset_s / 900)
    if abs(offset_s - quarter_hours * 900) > 60 or abs(quarter_hours) > 14 * 4:
        return None
    return (start + timedelta(seconds=quarter_hours * 900)).isoformat()


def _fit_summary(blob: bytes) -> OriginalSummary | None:
    from fitparse import FitFile, FitParseError

    try:
        fit = FitFile(io.BytesIO(blob))
        messages = [(m.name, {d.name: d.value for d in m}) for m in fit.get_messages(["session", "activity"])]
    except (FitParseError, ValueError, KeyError) as e:
        log.warning("FIT file couldn't be parsed (%s); skipped", e)
        return None
    sessions = sorted((v for name, v in messages if name == "session" and v.get("start_time") is not None),
                      key=lambda s: _fit_naive_utc(s["start_time"]))
    if not sessions:
        return None
    activity = next((v for name, v in messages if name == "activity"), {})
    start = _fit_naive_utc(sessions[0]["start_time"]).replace(microsecond=0)

    # One session per leg in a multisport file (transitions included), so the whole activity spans them all.
    ends = [_fit_naive_utc(s["start_time"])
            + timedelta(seconds=s.get("total_elapsed_time") or s.get("total_timer_time") or 0) for s in sessions]
    elapsed = (max(ends) - start).total_seconds() or None
    # Garmin writes total_moving_time; most other devices only write the timer time, which excludes pauses.
    moving = _fit_total(sessions, "total_moving_time") or _fit_total(sessions, "total_timer_time")
    avg_hr = _fit_weighted(sessions, "avg_heart_rate")
    max_hrs = [s["max_heart_rate"] for s in sessions if s.get("max_heart_rate") is not None]
    sport = "multisport" if len(sessions) > 1 else _fit_type(sessions[0].get("sport"), sessions[0].get("sub_sport"))
    return OriginalSummary(
        start_utc=start.isoformat() + "Z",
        elapsed_s=round(elapsed) if elapsed is not None else None,
        distance_m=_fit_total(sessions, "total_distance"),
        sport=sport,
        start_local=_fit_local_start(start, activity),
        moving_s=round(moving) if moving is not None else None,
        elev_gain_m=_fit_total(sessions, "total_ascent"),
        avg_hr=round(avg_hr) if avg_hr is not None else None,
        max_hr=max(max_hrs) if max_hrs else None,
        avg_watts=_fit_weighted(sessions, "avg_power"),
        calories=_fit_total(sessions, "total_calories"),
    )


def _xml_text(path_tag: str, root: ET.Element) -> list[ET.Element]:
    # GPX and TCX use namespaces; match on the local tag name only.
    tag = path_tag.rsplit("}", 1)[-1]
    return [el for el in root.iter() if el.tag.rsplit("}", 1)[-1] == tag]


def _gpx_summary(blob: bytes) -> OriginalSummary | None:
    root = ET.fromstring(blob)
    times = [el.text for el in _xml_text("time", root) if el.text]
    if not times:
        return None
    starts = sorted(datetime.fromisoformat(t.replace("Z", "+00:00")) for t in times)
    start, end = starts[0], starts[-1]
    return OriginalSummary(
        start_utc=start.astimezone(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z",
        elapsed_s=round((end - start).total_seconds()) or None,
        distance_m=None,
        sport=None,
    )


def _tcx_summary(blob: bytes) -> OriginalSummary | None:
    root = ET.fromstring(blob)
    laps = _xml_text("Lap", root)
    start_el = laps[0].get("StartTime") if laps else None
    if not start_el:
        ids = _xml_text("Id", root)
        start_el = ids[0].text if ids else None
    if not start_el:
        return None
    start = datetime.fromisoformat(start_el.replace("Z", "+00:00"))
    # Sum the per-lap totals; these are reliable, unlike the cumulative DistanceMeters on each trackpoint.
    total_s = sum(float(el.text) for el in _xml_text("TotalTimeSeconds", root) if el.text) or None
    activities = _xml_text("Activity", root)
    return OriginalSummary(
        start_utc=start.astimezone(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z",
        elapsed_s=round(total_s) if total_s else None,
        distance_m=None,  # TCX distance is cumulative per trackpoint; summing laps isn't reliable, so leave it
        sport=activities[0].get("Sport") if activities else None,
    )


def parse_original(path: Path) -> OriginalSummary | None:
    """Read the summary of a FIT, GPX or TCX file (gzipped or not).

    Every format gives the start time and duration. FIT files also give the sport, distance, moving time,
    local start, elevation gain, heart rate, power and calories; a multisport FIT file is one activity.
    """
    name = path.name.lower()
    blob = path.read_bytes()
    if name.endswith(".gz"):
        blob = gzip.decompress(blob)
        name = name.removesuffix(".gz")
    ext = name.rsplit(".", 1)[-1]
    try:
        if ext == "fit":
            return _fit_summary(blob)
        if ext == "gpx":
            return _gpx_summary(blob)
        if ext == "tcx":
            return _tcx_summary(blob)
    except (ET.ParseError, ValueError) as e:
        log.warning("%s couldn't be parsed (%s); skipped", path.name, e)
        return None
    return None


# --- Rows per kind --------------------------------------------------------------------------------------

def _source_id(row: dict[str, str], profile: Profile) -> str:
    """The platform's activity ID if the profile maps one, else a stable hash of the whole CSV row.

    Hashing the whole row (not just start time and type) keeps the id stable across re-imports while giving
    two rows that differ anywhere their own id, so two activities at the same start don't collide.
    """
    column = profile.columns.get("source_id")
    if column and row.get(column, "").strip():
        return row[column].strip()
    canonical = json.dumps(row, sort_keys=True, ensure_ascii=False)
    return "row:" + hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _activity_row(row: dict[str, str], profile: Profile, source: str, now: str, counts: ImportCounts) -> dict[str, Any]:
    column = profile.columns["start_time"]
    start = _parse_datetime(row[column], profile)
    activity_type = _field_value(row, profile, "activity_type", counts) or "other"
    start_utc = _to_utc_iso(start)
    return {
        "source": source,
        "source_id": _source_id(row, profile),
        "activity_type": activity_type,
        "name": _field_value(row, profile, "name", counts),
        "start_utc": start_utc,
        "start_local": start.replace(tzinfo=None, microsecond=0).isoformat() if profile.tz == "local" else None,
        "elapsed_s": _field_value(row, profile, "elapsed_s", counts),
        "moving_s": _field_value(row, profile, "moving_s", counts),
        "distance_m": _field_value(row, profile, "distance_m", counts),
        "elev_gain_m": _field_value(row, profile, "elev_gain_m", counts),
        "avg_hr": _field_value(row, profile, "avg_hr", counts),
        "max_hr": _field_value(row, profile, "max_hr", counts),
        "avg_watts": _field_value(row, profile, "avg_watts", counts),
        "calories": _field_value(row, profile, "calories", counts),
        "raw_json": json.dumps(row, sort_keys=True, ensure_ascii=False),
        "imported_utc": now,
    }


def _wellness_row(row: dict[str, str], profile: Profile, source: str, now: str, counts: ImportCounts) -> dict[str, Any]:
    date = _parse_datetime(row[profile.columns["date"]], profile).date().isoformat()
    fields = ("resting_hr", "hrv_avg_ms", "hrv_status", "sleep_s", "sleep_score", "body_battery_high",
              "body_battery_low", "stress_avg", "steps", "readiness_score")
    return {"date": date, **{f: _field_value(row, profile, f, counts) for f in fields},
            "complete": 1, "source": source, "updated_utc": now}


def _weigh_in_row(row: dict[str, str], profile: Profile, source: str, counts: ImportCounts) -> dict[str, Any]:
    measured = _to_utc_iso(_parse_datetime(row[profile.columns["measured_utc"]], profile))
    weight = _field_value(row, profile, "weight_kg", counts)
    if weight is None:
        raise ValueError("no weight")
    return {"measured_utc": measured, "weight_kg": weight,
            "body_fat_pct": _field_value(row, profile, "body_fat_pct", counts),
            "muscle_mass_kg": _field_value(row, profile, "muscle_mass_kg", counts),
            "body_water_pct": _field_value(row, profile, "body_water_pct", counts),
            "source": source}


def _body_composition_row(row: dict[str, str], profile: Profile, source: str, now: str,
                          counts: ImportCounts) -> dict[str, Any]:
    measured = _parse_datetime(row[profile.columns["measured_on"]], profile).date().isoformat()
    method = (_field_value(row, profile, "method", counts) or "other").lower()
    if method not in ("bia", "dexa", "other"):
        method = "other"
    fields = ("weight_kg", "body_fat_pct", "lean_mass_kg", "bone_mass_kg", "visceral_fat")
    return {"source": source, "measured_on": measured, "method": method,
            **{f: _field_value(row, profile, f, counts) for f in fields},
            "raw_json": json.dumps(row, sort_keys=True, ensure_ascii=False)}


def _lab_test_row(row: dict[str, str], profile: Profile, source: str, counts: ImportCounts) -> dict[str, Any]:
    tested = _parse_datetime(row[profile.columns["tested_on"]], profile).date().isoformat()
    fields = ("sport", "protocol", "lt1_hr", "lt1_pace_s_per_km", "lt1_watts", "lt2_hr", "lt2_pace_s_per_km",
              "lt2_watts", "notes")
    return {"source": source, "tested_on": tested,
            **{f: _field_value(row, profile, f, counts) for f in fields}}


# --- CSV import -----------------------------------------------------------------------------------------

def _read_csv(text: str) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if header is None:
        return [], []
    rows = []
    for values in reader:
        row: dict[str, str] = {}
        for name, value in zip(header, values):
            # A repeated column name keeps its last occurrence (Strava's metric column), like import_strava.
            row[name] = value
        rows.append(row)
    return header, rows


def _upsert_result(counts: ImportCounts, result: str) -> None:
    setattr(counts, result, getattr(counts, result) + 1)


# Per kind: the table, its key columns, and how to upsert one row.
_WRITERS = {
    "activities": ("source_activities", ("source", "source_id")),
    "wellness": ("daily_wellness", ("date",)),
    "weigh_ins": ("weigh_ins", ("measured_utc",)),
    "body_composition": ("body_composition", ("source", "measured_on", "method")),
    "lab_tests": ("lab_tests", ("source", "tested_on", "sport")),
}


def _build_row(row: dict[str, str], source: str, profile: Profile, now: str, counts: ImportCounts) -> dict[str, Any]:
    if profile.kind == "activities":
        return _activity_row(row, profile, source, now, counts)
    if profile.kind == "wellness":
        return _wellness_row(row, profile, source, now, counts)
    if profile.kind == "weigh_ins":
        return _weigh_in_row(row, profile, source, counts)
    if profile.kind == "body_composition":
        return _body_composition_row(row, profile, source, now, counts)
    return _lab_test_row(row, profile, source, counts)


def _upsert_row(conn: sqlite3.Connection, kind: str, row: dict[str, Any]) -> str:
    table, keys = _WRITERS[kind]
    if kind == "wellness":
        return _upsert_wellness(conn, row)
    return _upsert_unique(conn, table, keys, row)


def _import_csv(conn: sqlite3.Connection, text: str, source: str, profile: Profile, counts: ImportCounts) -> None:
    header, rows = _read_csv(text)
    missing = [c for c in profile.required if c not in header]
    if missing:
        # Reject before any write, naming the column.
        raise MissingColumnError(f"This file has no {missing[0]!r} column (profile {profile.name}, kind {profile.kind}).")
    now = db.utc_now()
    counts.kinds[profile.kind] = counts.kinds.get(profile.kind, 0)
    _, keys = _WRITERS[profile.kind]
    # Build every row first, keeping the last of any duplicate key within this file, so one key is written once
    # and re-importing is stable even when a platform repeats an activity ID.
    deduped: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        try:
            built = _build_row(row, source, profile, now, counts)
        except (KeyError, ValueError) as e:
            log.warning("Row skipped (%s): %s", profile.kind, e)
            counts.failed += 1
            continue
        deduped[tuple(built[k] for k in keys)] = built
        counts.kinds[profile.kind] += 1
    for built in deduped.values():
        with conn:
            _upsert_result(counts, _upsert_row(conn, profile.kind, built))


def _upsert_unique(conn: sqlite3.Connection, table: str, keys: tuple[str, ...], row: dict[str, Any]) -> str:
    """Insert or update one row identified by a composite key, ignoring volatile timestamps (like db.upsert).

    Key columns are matched with IS, not =, so a NULL key (e.g. lab_tests.sport) finds its existing row
    instead of inserting a duplicate (SQLite treats NULLs as distinct in a UNIQUE index). A row the athlete has
    edited (edited_utc set) is left untouched, so an import never overwrites a correction.
    """
    where = " AND ".join(f"{k} IS ?" for k in keys)
    existing = conn.execute(f"SELECT * FROM {table} WHERE {where}", [row[k] for k in keys]).fetchone()
    columns = list(row)
    if existing is not None and "edited_utc" in existing.keys() and existing["edited_utc"] is not None:
        return "unchanged"
    if existing is None:
        placeholders = ", ".join("?" for _ in columns)
        conn.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})", [row[c] for c in columns])
        return "added"
    changed = [c for c in columns if c not in db.VOLATILE_COLUMNS and existing[c] != row[c]]
    if not changed:
        return "unchanged"
    assignments = ", ".join(f"{c} = ?" for c in columns if c not in keys)
    conn.execute(f"UPDATE {table} SET {assignments} WHERE {where}",
                 [row[c] for c in columns if c not in keys] + [row[k] for k in keys])
    return "updated"


def _upsert_wellness(conn: sqlite3.Connection, row: dict[str, Any]) -> str:
    """Fill a wellness date only when Garmin doesn't already own it: Garmin's row wins for the same date."""
    existing = conn.execute("SELECT source FROM daily_wellness WHERE date = ?", (row["date"],)).fetchone()
    if existing is not None and existing["source"] == "garmin" and row["source"] != "garmin":
        return "unchanged"
    return _upsert_unique(conn, "daily_wellness", ("date",), row)


# --- Original files -------------------------------------------------------------------------------------

def _original_ext(name: str) -> str | None:
    lower = name.lower().removesuffix(".gz")
    ext = lower.rsplit(".", 1)[-1] if "." in lower else ""
    return ext if ext in FILE_FORMATS else None


def _import_original(conn: sqlite3.Connection, path: Path, blob: bytes, source: str, data_dir: Path,
                     now: str, counts: ImportCounts) -> None:
    """Store one original file as a source activity and copy it into files/<source>/ in the data folder."""
    ext = _original_ext(path.name)
    # Write the decompressed bytes to a temp file named with the real extension, so parse_original dispatches
    # on it and reads the content once.
    tmp = data_dir / "files" / source / f".tmp-{hashlib.sha256(blob).hexdigest()[:16]}.{ext}"
    tmp.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp.write_bytes(gzip.decompress(blob) if path.name.lower().endswith(".gz") else blob)
    try:
        summary = parse_original(tmp)
        if summary is None:
            log.warning("%s: no start time in the file; skipped", path.name)
            counts.failed += 1
            tmp.unlink(missing_ok=True)
            return
        content = tmp.read_bytes()
        source_id = f"file:{hashlib.sha256(content).hexdigest()[:16]}"
        activity = {
            "source": source, "source_id": source_id,
            "activity_type": summary.sport or "other", "name": None,
            "start_utc": summary.start_utc, "start_local": summary.start_local,
            "elapsed_s": summary.elapsed_s, "moving_s": summary.moving_s, "distance_m": summary.distance_m,
            "elev_gain_m": summary.elev_gain_m, "avg_hr": summary.avg_hr, "max_hr": summary.max_hr,
            "avg_watts": summary.avg_watts, "calories": summary.calories,
            "raw_json": json.dumps({"file": path.name, "sport": summary.sport}, sort_keys=True),
            "imported_utc": now,
        }
        rel = Path("files", source, f"{source_id.split(':', 1)[1]}.{ext}")
        final = data_dir / rel
        recorded = conn.execute("SELECT 1 FROM files WHERE path = ?", (rel.as_posix(),)).fetchone()
        with conn:
            _upsert_result(counts, _upsert_unique(conn, "source_activities", ("source", "source_id"), activity))
            if not (recorded and final.is_file()):
                final.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                final.write_bytes(content)
                db.upsert(conn, "files", "path", {
                    "path": rel.as_posix(), "source": source, "source_id": source_id, "format": ext,
                    "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                })
                counts.files += 1
    finally:
        tmp.unlink(missing_ok=True)


# --- Entry point ----------------------------------------------------------------------------------------

def _members(path: Path) -> list[tuple[str, bytes]]:
    """(name, bytes) for every file in a CSV path, a folder or a zip."""
    if path.is_file() and path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            return [(n, z.read(n)) for n in z.namelist() if not n.endswith("/")]
    if path.is_file():
        return [(path.name, path.read_bytes())]
    return [(p.relative_to(path).as_posix(), p.read_bytes()) for p in sorted(path.rglob("*")) if p.is_file()]


def run(conn: sqlite3.Connection, path: Path, source: str, data_dir: Path, profile: str | None = None,
        progress: "Callable[[int, int], None] | None" = None) -> ImportCounts:
    """Import a CSV, a folder or a zip under the given source name.

    CSVs go through a Profile (the named one, or detected from the header). Original FIT/GPX/TCX files are read
    for their summary (parse_original) and copied into files/<source>/ in the data folder. Re-running changes nothing.
    A CSV missing a required column is rejected before any write.

    `progress`, if given, is called with (original_files_done, total) after each original file. Parsing and
    copying a platform's whole archive of FIT/GPX/TCX files takes minutes, so the CLI shows a bar; the CSVs are
    quick by comparison and aren't counted. The module prints nothing itself.
    """
    if not path.exists():
        raise FileNotFoundError(f"{path} not found")
    counts = ImportCounts()
    now = db.utc_now()
    chosen = load_profile(profile) if profile else None
    members = _members(path)
    total_files = sum(1 for name, _ in members if _original_ext(name))
    done_files = 0
    for name, blob in members:
        lower = name.lower()
        if lower.endswith(".csv"):
            text = blob.decode("utf-8-sig")
            header, _ = _read_csv(text)
            prof = chosen or detect_kind(header)
            _import_csv(conn, text, source, prof, counts)
        elif _original_ext(name):
            _import_original(conn, Path(name), blob, source, data_dir, now, counts)
            done_files += 1
            if progress is not None:
                progress(done_files, total_files)
    return counts
