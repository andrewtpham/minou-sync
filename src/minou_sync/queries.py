"""Read-only queries over the archive, shared by the MCP server and (later) the interface's read endpoints.

Plain functions over a connection. Totals and averages are computed in SQL; every returned dict names its
units in its keys (distance_km, duration_h, sleep_h). Lists are capped at MAX_ROWS with a truncated flag, so
answers stay small for a model. The connection from connect_ro is read-only, so these never write and
run happily while a sync holds the write lock.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_ROWS = 50

# Activity columns a caller may sort by, mapped to safe SQL (no user string reaches the query).
# "load" sorts by Garmin's activity training load (from the garmin_raw join), hardest first, NULLs last, so
# "my hardest sessions" is answerable in one capped call instead of ranking a truncated page client-side.
_SORTS = {
    "start": "a.start_utc DESC",
    "start_asc": "a.start_utc ASC",
    "distance": "a.distance_m DESC",
    "duration": "a.elapsed_s DESC",
    "elevation": "a.elev_gain_m DESC",
    "load": "training_load IS NULL, training_load DESC",
}


@dataclass(frozen=True)
class Page:
    rows: list[dict[str, Any]]
    total: int
    truncated: bool


def connect_ro(db_path: Path) -> sqlite3.Connection:
    """Open the archive read-only (file:...?mode=ro), so a running sync isn't blocked and no tool can write."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _round(value: Any, places: int) -> float | None:
    return None if value is None else round(value, places)


def archive_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Counts by source and type, the date range, the known activity types, and the last sync time."""
    by_source = {r["source"]: r["n"] for r in
                 conn.execute("SELECT source, COUNT(*) n FROM activities GROUP BY source")}
    by_type = {r["activity_type"]: r["n"] for r in
               conn.execute("SELECT activity_type, COUNT(*) n FROM activities GROUP BY activity_type ORDER BY n DESC")}
    first, last = conn.execute("SELECT MIN(start_utc), MAX(start_utc) FROM activities").fetchone()
    wfirst, wlast, wn = conn.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM daily_wellness").fetchone()
    last_sync = conn.execute("SELECT value FROM meta WHERE key='last_sync_utc'").fetchone()
    return {
        "activities_total": sum(by_source.values()),
        "activities_by_source": by_source,
        "activities_by_type": by_type,
        "activity_types": sorted(by_type),
        "first_activity_date": (first or "")[:10] or None,
        "last_activity_date": (last or "")[:10] or None,
        "wellness_days": wn,
        "first_wellness_date": wfirst,
        "last_wellness_date": wlast,
        "last_sync_utc": last_sync[0] if last_sync else None,
    }


def _activity_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "key": row["key"],
        "date": (row["start_local"] or row["start_utc"] or "")[:10],
        "start_utc": row["start_utc"],
        "activity_type": row["activity_type"],
        "name": row["name"],
        "duration_h": _round((row["elapsed_s"] or 0) / 3600, 2),
        "distance_km": _round((row["distance_m"] or 0) / 1000, 2),
        "elev_gain_m": _round(row["elev_gain_m"], 0) if row["elev_gain_m"] is not None else None,
        "avg_hr": row["avg_hr"],
        "avg_watts": _round(row["avg_watts"], 0),
        "source": row["source"],
        "check_reason": row["check_reason"] if "check_reason" in row.keys() else None,
        "aerobic_te": _round(row["aerobic_te"], 1) if "aerobic_te" in row.keys() else None,
        "anaerobic_te": _round(row["anaerobic_te"], 1) if "anaerobic_te" in row.keys() else None,
        # Garmin's own label and load, from its activity detail (NULL for non-Garmin or unsynced detail).
        "training_effect_label": _te_label(row["te_label"]) if "te_label" in row.keys() else None,
        "training_load": _round(row["training_load"], 0) if "training_load" in row.keys() else None,
    }


def _te_label(raw: str | None) -> str | None:
    """Garmin's trainingEffectLabel, tidied: 'LACTATE_THRESHOLD' -> 'lactate threshold'."""
    return raw.replace("_", " ").lower() if raw else None


def list_activities(conn: sqlite3.Connection, from_date: str | None = None, to_date: str | None = None,
                    activity_type: str | None = None, q: str | None = None, sort: str = "start",
                    limit: int = MAX_ROWS, offset: int = 0) -> Page:
    """A page of activities matching the filters, newest first by default. At most MAX_ROWS rows."""
    where, params = _activity_where(from_date, to_date, activity_type, q)
    total = conn.execute(f"SELECT COUNT(*) FROM activities a {where}", params).fetchone()[0]
    limit = max(1, min(limit, MAX_ROWS))
    order = _SORTS.get(sort, _SORTS["start"])
    rows = conn.execute(
        f"SELECT {_ACTIVITY_COLS} FROM activities a {_TE_JOIN} {where} ORDER BY {order} LIMIT ? OFFSET ?",
        [*params, limit, offset]
    ).fetchall()
    return Page([_activity_dict(r) for r in rows], total, total > offset + len(rows))


# The master-activity columns the activity dict needs, plus Garmin's training effect label and load pulled from
# its stored activity detail. Qualified with the `a` alias so the garmin_raw join is unambiguous.
_ACTIVITY_COLS = (
    "a.key, a.start_utc, a.start_local, a.activity_type, a.name, a.elapsed_s, a.distance_m, a.elev_gain_m, "
    "a.avg_hr, a.avg_watts, a.source, a.check_reason, a.aerobic_te, a.anaerobic_te, "
    "json_extract(gr.raw_json, '$.summaryDTO.trainingEffectLabel') AS te_label, "
    "json_extract(gr.raw_json, '$.summaryDTO.activityTrainingLoad') AS training_load"
)
_TE_JOIN = "LEFT JOIN garmin_raw gr ON gr.endpoint='get_activity' AND gr.key=CAST(a.garmin_id AS TEXT)"


def _activity_where(from_date, to_date, activity_type, q, alias: str = "") -> tuple[str, list[Any]]:
    """Build the WHERE for an activities query. alias prefixes the columns (e.g. "a") for queries that join."""
    p = f"{alias}." if alias else ""
    clauses, params = [], []
    if from_date:
        clauses.append(f"substr(COALESCE({p}start_local, {p}start_utc), 1, 10) >= ?")
        params.append(from_date)
    if to_date:
        clauses.append(f"substr(COALESCE({p}start_local, {p}start_utc), 1, 10) <= ?")
        params.append(to_date)
    if activity_type:
        if activity_type.endswith("*"):  # a family prefix, e.g. "run*" matches run, run_trail, run_treadmill
            clauses.append(f"{p}activity_type LIKE ? ESCAPE '\\'")
            prefix = activity_type[:-1].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(prefix + "%")
        else:
            clauses.append(f"{p}activity_type = ?")
            params.append(activity_type)
    if q:
        clauses.append("name LIKE ? ESCAPE '\\'")
        params.append("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


def get_activity(conn: sqlite3.Connection, key: str) -> dict | None:
    """One activity by key, with the sources it was merged from and any notes."""
    row = conn.execute(
        f"SELECT {_ACTIVITY_COLS}, a.edited FROM activities a {_TE_JOIN} WHERE a.key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    out = _activity_dict(row)
    out["edited"] = bool(row["edited"])
    out["sources"] = [f"{r['source']}:{r['source_id']}" for r in
                      conn.execute("SELECT source, source_id FROM activity_sources WHERE key = ?", (key,))]
    out["notes"] = [{"text": r["text"], "author": r["author"], "created_utc": r["created_utc"]} for r in
                    conn.execute("SELECT text, author, created_utc FROM notes WHERE target='activity' AND ref = ? "
                                 "ORDER BY created_utc", (key,))]
    return out


# Period buckets for totals and breakdown. Week is %Y-W%W (weeks start Monday, W00 possible), not ISO week.
_PERIOD_FMT = {"day": "%Y-%m-%d", "week": "%Y-W%W", "month": "%Y-%m"}

# Ways to order totals rows, mapped to safe SQL. Anything but "period" puts the biggest period first, so
# "my biggest climbing day" is the first row rather than something a model hunts for in a capped list.
_TOTALS_SORTS = {
    "period": "period DESC",
    "distance": "distance_km DESC, period DESC",
    "duration": "hours DESC, period DESC",
    "elevation": "elev_gain_m DESC, period DESC",
    "activities": "activities DESC, period DESC",
}

# The summed columns totals and breakdown both return. alias qualifies them for queries that join.
def _agg_cols(alias: str = "") -> str:
    p = f"{alias}." if alias else ""
    return (f"COUNT(*) AS activities, "
            f"ROUND(SUM(COALESCE({p}elapsed_s, 0)) / 3600.0, 1) AS hours, "
            f"ROUND(SUM(COALESCE({p}distance_m, 0)) / 1000.0, 1) AS distance_km, "
            f"ROUND(SUM(COALESCE({p}elev_gain_m, 0)), 0) AS elev_gain_m")


def _agg_dict(r: sqlite3.Row) -> dict[str, Any]:
    return {"activities": r["activities"], "hours": r["hours"], "distance_km": r["distance_km"],
            "elev_gain_m": r["elev_gain_m"]}


def totals(conn: sqlite3.Connection, from_date: str | None = None, to_date: str | None = None,
           period: str = "week", activity_type: str | None = None, sort: str = "period",
           limit: int = MAX_ROWS) -> list[dict[str, Any]]:
    """Count, hours, kilometres and elevation per day, week, month, or (period="total") one grand total for the
    whole range. Computed in SQL. Newest period first by default; sort="elevation" etc. puts the biggest first.
    At most `limit` rows (callers that report truncation ask for one more than they show)."""
    where, params = _activity_where(from_date, to_date, activity_type, None)
    if period == "total":  # one row for the whole range, so a model needn't sum periods itself
        r = conn.execute(f"SELECT {_agg_cols()} FROM activities {where}", params).fetchone()
        return [{"period": "total", **_agg_dict(r)}]
    fmt = _PERIOD_FMT.get(period, _PERIOD_FMT["week"])
    order = _TOTALS_SORTS.get(sort, _TOTALS_SORTS["period"])
    rows = conn.execute(
        f"SELECT strftime('{fmt}', COALESCE(start_local, start_utc)) AS period, {_agg_cols()} "
        f"FROM activities {where} GROUP BY period ORDER BY {order} LIMIT ?", [*params, limit]
    ).fetchall()
    return [{"period": r["period"], **_agg_dict(r)} for r in rows]


def breakdown(conn: sqlite3.Connection, group_by: str = "type", from_date: str | None = None,
              to_date: str | None = None, activity_type: str | None = None, period: str = "total",
              limit: int = MAX_ROWS) -> list[dict[str, Any]]:
    """Count, hours, kilometres and elevation grouped by activity type or by Garmin's training-effect label,
    busiest first. With period day/week/month, one row per period and group, newest period first, so "weekly
    totals by type" is one call. The server does the counting so a model needn't tally rows itself."""
    where, params = _activity_where(from_date, to_date, activity_type, None, alias="a")
    if group_by == "training_effect":
        grp, join = "json_extract(gr.raw_json, '$.summaryDTO.trainingEffectLabel')", _TE_JOIN
    else:  # group_by == "type" (default)
        grp, join = "a.activity_type", ""
    if period == "total":
        per_col, group, order = "", "raw_group", "activities DESC"
    else:
        fmt = _PERIOD_FMT.get(period, _PERIOD_FMT["week"])
        per_col = f"strftime('{fmt}', COALESCE(a.start_local, a.start_utc)) AS period, "
        group, order = "period, raw_group", "period DESC, activities DESC"
    rows = conn.execute(
        f"SELECT {per_col}{grp} AS raw_group, {_agg_cols('a')} FROM activities a {join} {where} "
        f"GROUP BY {group} ORDER BY {order} LIMIT ?", [*params, limit]
    ).fetchall()
    out = []
    for r in rows:
        label = (_te_label(r["raw_group"]) or "unknown") if group_by == "training_effect" else r["raw_group"]
        out.append({**({"period": r["period"]} if period != "total" else {}), "group": label, **_agg_dict(r)})
    return out


def wellness(conn: sqlite3.Connection, from_date: str | None = None, to_date: str | None = None,
             limit: int = MAX_ROWS) -> list[dict[str, Any]]:
    """One row per day of daily wellness in the range, newest first. At most `limit` days."""
    clauses, params = [], []
    if from_date:
        clauses.append("date >= ?")
        params.append(from_date)
    if to_date:
        clauses.append("date <= ?")
        params.append(to_date)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT date, resting_hr, hrv_avg_ms, hrv_status, sleep_s, sleep_score, body_battery_high, "
        f"body_battery_low, stress_avg, steps, readiness_score FROM daily_wellness {where} "
        f"ORDER BY date DESC LIMIT ?", [*params, limit]
    ).fetchall()
    load = _training_load_by_date(conn, from_date, to_date)
    return [{
        "date": r["date"],
        "resting_hr": r["resting_hr"],
        "hrv_avg_ms": _round(r["hrv_avg_ms"], 0),
        "hrv_status": r["hrv_status"],
        "sleep_h": _round((r["sleep_s"] or 0) / 3600, 1) if r["sleep_s"] is not None else None,
        "sleep_score": r["sleep_score"],
        "body_battery_high": r["body_battery_high"],
        "body_battery_low": r["body_battery_low"],
        "stress_avg": r["stress_avg"],
        "steps": r["steps"],
        "readiness_score": r["readiness_score"],
        # Garmin's acute (7-day) and chronic (28-day) training load and their ratio, for the day (NULL if absent).
        **load.get(r["date"], {"acute_load": None, "chronic_load": None, "acute_chronic_ratio": None}),
    } for r in rows]


def _training_load_by_date(conn: sqlite3.Connection, from_date: str | None, to_date: str | None) -> dict[str, dict]:
    """Acute load, chronic load and their ratio per date, from Garmin's stored get_training_status responses.

    The figures sit under a device-id-keyed object, so the path varies; a short walk finds the acuteTrainingLoadDTO
    rather than a fixed json_extract. Returns {date: {acute_load, chronic_load, acute_chronic_ratio}}.
    """
    import json

    clauses = ["endpoint = 'get_training_status'"]
    params: list[Any] = []
    if from_date:
        clauses.append("key >= ?")
        params.append(from_date)
    if to_date:
        clauses.append("key <= ?")
        params.append(to_date)
    out: dict[str, dict] = {}
    for row in conn.execute(f"SELECT key, raw_json FROM garmin_raw WHERE {' AND '.join(clauses)}", params):
        try:
            dto = _find_acute_load_dto(json.loads(row["raw_json"]))
        except (ValueError, TypeError):
            dto = None
        if dto is None:
            continue
        out[row["key"]] = {
            "acute_load": _round(dto.get("dailyTrainingLoadAcute"), 0),
            "chronic_load": _round(dto.get("dailyTrainingLoadChronic"), 0),
            "acute_chronic_ratio": _round(dto.get("dailyAcuteChronicWorkloadRatio"), 2),
        }
    return out


def _find_acute_load_dto(obj: Any) -> dict | None:
    """Find the first acuteTrainingLoadDTO anywhere in a training-status response (its key path has a device id)."""
    if isinstance(obj, dict):
        if "acuteTrainingLoadDTO" in obj and isinstance(obj["acuteTrainingLoadDTO"], dict):
            return obj["acuteTrainingLoadDTO"]
        for v in obj.values():
            found = _find_acute_load_dto(v)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_acute_load_dto(v)
            if found is not None:
                return found
    return None


def load_balance(conn: sqlite3.Connection, from_date: str | None = None,
                 to_date: str | None = None) -> list[dict[str, Any]]:
    """Garmin's monthly training-load split by intensity, with its target ranges, one row per month.

    Garmin keeps a rolling monthly split of training load into low aerobic, high aerobic and anaerobic, each
    against a target min/max, inside get_training_status (mostRecentTrainingLoadBalance). The figures restate
    the same rolling month on every day, so this takes the latest day present in each calendar month and reports
    that month's split, the target ranges, whether each bucket is below/in/above its range, and Garmin's own
    feedback phrase. Newest month first, at most MAX_ROWS months.
    """
    import json

    clauses = ["endpoint = 'get_training_status'"]
    params: list[Any] = []
    if from_date:
        clauses.append("key >= ?")
        params.append(from_date)
    if to_date:
        clauses.append("key <= ?")
        params.append(to_date)
    # Walk days oldest-first so the last write for each month wins.
    by_month: dict[str, dict] = {}
    for row in conn.execute(
        f"SELECT key, raw_json FROM garmin_raw WHERE {' AND '.join(clauses)} ORDER BY key", params
    ):
        try:
            dto = _find_load_balance_dto(json.loads(row["raw_json"]))
        except (ValueError, TypeError):
            dto = None
        if dto is None:
            continue
        by_month[row["key"][:7]] = _load_balance_row(row["key"][:7], dto)
    return sorted(by_month.values(), key=lambda r: r["month"], reverse=True)[:MAX_ROWS]


def _load_balance_row(month: str, dto: dict) -> dict[str, Any]:
    def bucket(value_key: str, min_key: str, max_key: str) -> dict[str, Any]:
        value, lo, hi = dto.get(value_key), dto.get(min_key), dto.get(max_key)
        status = None
        if value is not None and lo is not None and hi is not None:
            status = "below" if value < lo else "above" if value > hi else "in range"
        return {"load": _round(value, 0), "target_min": lo, "target_max": hi, "status": status}

    return {
        "month": month,
        "aerobic_low": bucket("monthlyLoadAerobicLow", "monthlyLoadAerobicLowTargetMin", "monthlyLoadAerobicLowTargetMax"),
        "aerobic_high": bucket("monthlyLoadAerobicHigh", "monthlyLoadAerobicHighTargetMin", "monthlyLoadAerobicHighTargetMax"),
        "anaerobic": bucket("monthlyLoadAnaerobic", "monthlyLoadAnaerobicTargetMin", "monthlyLoadAnaerobicTargetMax"),
        "feedback": dto.get("trainingBalanceFeedbackPhrase"),
    }


def _find_load_balance_dto(obj: Any) -> dict | None:
    """Find the first object carrying monthlyLoadAerobicLow in a training-status response (its key path has a
    device id under metricsTrainingLoadBalanceDTOMap)."""
    if isinstance(obj, dict):
        if "monthlyLoadAerobicLow" in obj:
            return obj
        for v in obj.values():
            found = _find_load_balance_dto(v)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_load_balance_dto(v)
            if found is not None:
                return found
    return None


# --- Best efforts and records --------------------------------------------------------------

# The efforts reported per sport, in order, and whether bigger is better (power) or smaller (a time).
_RUN_EFFORTS = ("run_1km", "run_1mi", "run_5k", "run_10k", "run_half", "run_marathon", "run_50k")
_RIDE_EFFORTS = ("ride_40km", "power_5min", "power_20min", "power_60min")
_POWER_EFFORTS = frozenset({"power_5min", "power_20min", "power_60min"})

# Human labels and the unit each effort is reported in.
_EFFORT_LABEL = {
    "run_1km": "fastest 1 km", "run_1mi": "fastest mile", "run_5k": "fastest 5K", "run_10k": "fastest 10K",
    "run_half": "fastest half marathon", "run_marathon": "fastest marathon", "run_50k": "fastest 50K",
    "ride_40km": "fastest 40 km", "power_5min": "best 5-min power", "power_20min": "best 20-min power",
    "power_60min": "best 60-min power",
}

# Garmin personal-record typeIds we can name, for the cross-check. Garmin's fastest-time running records
# are typeIds 1-6 (1 km, 1 mile, 5K, 10K, half marathon, marathon, in seconds; 5 and 6 match the half and
# marathon times computed from the same activities' files) and 7 is the longest run, in metres; 8-11 are
# cycling; 12-14 steps; 17 the longest swim. Labels stay generic where the exact meaning is uncertain, so the
# cross-check never overclaims what a Garmin record means.
_GARMIN_PR_LABEL = {
    1: "Garmin fastest 1 km", 2: "Garmin fastest 1 mile", 3: "Garmin fastest 5K", 4: "Garmin fastest 10K",
    5: "Garmin fastest half marathon", 6: "Garmin fastest marathon", 7: "Garmin longest run (distance)",
    8: "Garmin longest ride", 9: "Garmin best avg power (ride)", 10: "Garmin best 20-min power",
    11: "Garmin longest ride (distance)",
    12: "Garmin most steps (day)", 13: "Garmin most steps (week)", 14: "Garmin most steps (month)",
    17: "Garmin longest swim",
}
_GARMIN_PR_SPORT = {
    "run": (1, 2, 3, 4, 5, 6, 7),
    "ride": (8, 9, 10, 11),
}
# The unit of each Garmin record's value, where it's known; the ride records are left unlabelled.
_GARMIN_PR_UNIT = {1: "seconds", 2: "seconds", 3: "seconds", 4: "seconds", 5: "seconds", 6: "seconds", 7: "metres"}


def _clock(seconds: float | None) -> str | None:
    """A time in seconds as h:mm:ss (or m:ss under an hour), so a model needn't convert 1190.4 s itself."""
    if seconds is None:
        return None
    s = int(round(seconds))
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def best_efforts(conn: sqlite3.Connection, sport: str, from_date: str | None = None,
                 to_date: str | None = None) -> dict[str, Any]:
    """Best efforts for a sport ('run' or 'ride'), from activity_efforts first then Garmin's splits.

    Flagged (check_reason set) and hidden activities are left out, so a GPS error or a mis-typed activity can't
    set a record. Each effort names its value, date and activity key. Garmin's own record list
    comes alongside as a cross-check, with any record set by a flagged activity marked, so a wrong Garmin PR is
    visible rather than silently trusted.
    """
    efforts = _RIDE_EFFORTS if sport == "ride" else _RUN_EFFORTS
    computed = _best_from_efforts(conn, efforts, from_date, to_date)
    # Fall back to Garmin's own fastest splits for distance efforts an activity with no file didn't yield.
    _fill_from_garmin_splits(conn, sport, efforts, computed, from_date, to_date)
    results = []
    for effort in efforts:
        best = computed.get(effort)
        row = {"effort": effort, "label": _EFFORT_LABEL[effort]}
        if best is None:
            row.update({"value": None, "unit": "watts" if effort in _POWER_EFFORTS else "seconds",
                        "date": None, "activity_key": None, "from": None})
        else:
            row.update(best)
        if row["unit"] == "seconds":
            row["time"] = _clock(row["value"])
        results.append(row)
    out = {"sport": sport, "efforts": results, "garmin_records": _garmin_records(conn, sport)}
    if sport == "ride":
        # The design lists the longest and most-climbing ride beside the efforts; both are simple maxes over
        # clean ride activities (not stored efforts), so they come straight from the activities table.
        out["longest_ride"] = _ride_extreme(conn, "distance_m", from_date, to_date, unit="km", divide=1000)
        out["most_climbing_ride"] = _ride_extreme(conn, "elev_gain_m", from_date, to_date, unit="m", divide=1)
    _name_activities(conn, [*results, out.get("longest_ride") or {}, out.get("most_climbing_ride") or {}])
    return out


def _name_activities(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    """Add the activity's name and type beside every activity_key, so an answer can say which activity set a
    record ("Sunday long run, run_trail") rather than quoting an opaque key."""
    keys = {r["activity_key"] for r in rows if r.get("activity_key")}
    found = {}
    if keys:
        marks = ",".join("?" for _ in keys)
        found = {r["key"]: (r["name"], r["activity_type"]) for r in conn.execute(
            f"SELECT key, name, activity_type FROM activities WHERE key IN ({marks})", list(keys))}
    for r in rows:
        if "activity_key" in r:
            r["activity_name"], r["activity_type"] = found.get(r["activity_key"], (None, None))


def _ride_extreme(conn, column: str, from_date, to_date, unit: str, divide: int) -> dict[str, Any]:
    """The clean ride with the greatest distance or elevation gain, as {value, unit, date, activity_key}."""
    date_clause, params = _date_clause("a", from_date, to_date)
    row = conn.execute(
        f"SELECT a.key AS key, a.{column} AS v, COALESCE(a.start_local, a.start_utc) AS start FROM activities a "
        f"WHERE a.activity_type LIKE 'ride%' AND a.check_reason IS NULL AND a.{column} IS NOT NULL {date_clause} "
        f"ORDER BY a.{column} DESC LIMIT 1", params
    ).fetchone()
    if row is None:
        return {"value": None, "unit": unit, "date": None, "activity_key": None}
    return {"value": _round(row["v"] / divide, 1), "unit": unit,
            "date": (row["start"] or "")[:10] or None, "activity_key": row["key"]}


def _best_from_efforts(conn, efforts, from_date, to_date) -> dict[str, dict]:
    """The best stored effort per name, excluding flagged and hidden activities. Smaller wins for a time,
    bigger for power. Returns {effort: {value, unit, date, activity_key, from}}."""
    best: dict[str, dict] = {}
    date_clause, params = _date_clause("a", from_date, to_date)
    for effort in efforts:
        bigger = effort in _POWER_EFFORTS
        order = "DESC" if bigger else "ASC"
        row = conn.execute(
            f"SELECT e.value AS value, a.key AS key, COALESCE(a.start_local, a.start_utc) AS start, e.source AS src "
            f"FROM activity_efforts e "
            f"JOIN activity_sources s ON s.source = e.source AND s.source_id = e.source_id "
            f"JOIN activities a ON a.key = s.key "
            f"WHERE e.effort = ? AND a.check_reason IS NULL {date_clause} "
            f"ORDER BY e.value {order} LIMIT 1",
            [effort, *params]
        ).fetchone()
        if row is not None:
            best[effort] = {
                "value": _round(row["value"], 0 if bigger else 1),
                "unit": "watts" if bigger else "seconds",
                "date": (row["start"] or "")[:10] or None,
                "activity_key": row["key"],
                "from": "file",
            }
    return best


def _fill_from_garmin_splits(conn, sport, efforts, computed, from_date, to_date) -> None:
    """For distance efforts with no file-derived best, use Garmin's fastest standard splits.

    Reads get_activity splitSummaries for non-flagged, non-hidden Garmin activities and keeps the fastest time
    per standard distance, so the years before a file existed still count.
    """
    import json

    from .efforts import RIDE_DISTANCES, RUN_DISTANCES

    targets = {e: (RUN_DISTANCES.get(e) or RIDE_DISTANCES.get(e)) for e in efforts
               if (RUN_DISTANCES.get(e) or RIDE_DISTANCES.get(e)) and computed.get(e) is None}
    if not targets:
        return
    date_clause, params = _date_clause("a", from_date, to_date)
    rows = conn.execute(
        f"SELECT a.key AS key, COALESCE(a.start_local, a.start_utc) AS start, gr.raw_json AS raw "
        f"FROM activities a "
        f"JOIN garmin_raw gr ON gr.endpoint='get_activity' AND gr.key = CAST(a.garmin_id AS TEXT) "
        f"WHERE a.check_reason IS NULL AND a.garmin_id IS NOT NULL {date_clause}", params
    ).fetchall()
    best: dict[str, tuple[float, str, str]] = {}
    for row in rows:
        try:
            detail = json.loads(row["raw"])
        except (ValueError, TypeError):
            continue
        for summary in detail.get("splitSummaries") or []:
            if not str(summary.get("splitType", "")).startswith("DISTANCE"):
                continue
            dist, dur = summary.get("distance"), summary.get("duration")
            if not dist or not dur:
                continue
            for effort, target in targets.items():
                if abs(round(dist) - round(target)) <= 1 and (effort not in best or dur < best[effort][0]):
                    best[effort] = (dur, (row["start"] or "")[:10] or None, row["key"])
    for effort, (dur, date, key) in best.items():
        computed[effort] = {"value": _round(dur, 1), "unit": "seconds", "date": date,
                            "activity_key": key, "from": "garmin_split"}


def _garmin_records(conn: sqlite3.Connection, sport: str) -> list[dict[str, Any]]:
    """Garmin's own personal records for the sport, each marked when the activity that set it is flagged."""
    import json

    row = conn.execute("SELECT raw_json FROM garmin_raw WHERE endpoint='get_personal_record' AND key=''").fetchone()
    if row is None:
        return []
    try:
        records = json.loads(row["raw_json"])
    except (ValueError, TypeError):
        return []
    if isinstance(records, dict):
        records = records.get("personalRecords") or records.get("PersonalRecordDTO") or []
    wanted = set(_GARMIN_PR_SPORT.get(sport, ()))
    # Which Garmin activity ids are flagged in our archive, so a PR set by one can be named, and the name
    # and type of every Garmin activity, so each record says which activity set it.
    flagged = {str(r["garmin_id"]) for r in conn.execute(
        "SELECT garmin_id FROM activities WHERE check_reason IS NOT NULL AND garmin_id IS NOT NULL")}
    named = {str(r["garmin_id"]): (r["name"], r["activity_type"]) for r in conn.execute(
        "SELECT garmin_id, name, activity_type FROM activities WHERE garmin_id IS NOT NULL")}
    out = []
    for pr in records:
        type_id = pr.get("typeId")
        if type_id not in wanted:
            continue
        act_id = pr.get("activityId")
        unit = _GARMIN_PR_UNIT.get(type_id)
        value = _round(pr.get("value"), 1)
        out.append({
            "record": _GARMIN_PR_LABEL.get(type_id, f"Garmin typeId {type_id}"),
            "value": value,
            "unit": unit,
            **({"time": _clock(value)} if unit == "seconds" else {}),
            "date": (pr.get("prStartTimeGmtFormatted") or "")[:10] or None,
            "activity_id": act_id,
            "activity_name": named.get(str(act_id), (None, None))[0] if act_id else None,
            "activity_type": named.get(str(act_id), (None, None))[1] if act_id else None,
            "set_by_flagged_activity": str(act_id) in flagged if act_id else False,
        })
    return out


def _date_clause(alias: str, from_date: str | None, to_date: str | None) -> tuple[str, list[Any]]:
    """An AND-prefixed date filter on an activities alias, over start_local then start_utc."""
    clauses, params = [], []
    if from_date:
        clauses.append(f"AND substr(COALESCE({alias}.start_local, {alias}.start_utc), 1, 10) >= ?")
        params.append(from_date)
    if to_date:
        clauses.append(f"AND substr(COALESCE({alias}.start_local, {alias}.start_utc), 1, 10) <= ?")
        params.append(to_date)
    return " ".join(clauses), params
