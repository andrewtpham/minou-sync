"""`minou-sync mcp`: a few read-only tools over the archive for MCP clients (Claude, Cursor, Open WebUI).

Every read tool calls the plain functions in queries.py, so answers are pre-computed in SQL, carry
units in their field names, and are capped at 50 rows, with `truncated` and a note whenever the cap hides rows.
Defaults keep answers small enough for a small local model's context window: list_activities returns 10 rows
unless asked, and totals defaults to one grand-total row. The archive is opened read-only for every tool except
add_note, so the server works while a sync or import runs. Over stdio by default; Streamable HTTP with
--http, bound to 127.0.0.1 with DNS-rebinding protection on, so a foreign Host or Origin is refused.
"""

from __future__ import annotations

import functools
import json
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field

from . import db, queries

# A single SELECT, nothing else. Used by the optional query_sql tool.
_SELECT_ONLY = re.compile(r"^\s*select\b", re.IGNORECASE)
_FORBIDDEN = re.compile(r";\s*\S|\b(insert|update|delete|drop|alter|create|replace|attach|pragma|vacuum)\b",
                        re.IGNORECASE)

# The activity types the archive uses.
_TYPES = ("run, run_trail, run_treadmill, ride, ride_indoor, ride_gravel, ride_mtb, swim, walk, hike, "
          "strength, row, pilates, yoga, mobility, cardio, climb, tennis, pickleball, multisport, ski, other")
_KNOWN_TYPES = frozenset(_TYPES.split(", "))
# How the descriptions list them: the family forms first, because a small model given a flat list picks 'run'
# for "how much have I run" and silently leaves out trail and treadmill runs. 'climb' is rock or wall
# climbing; without saying so, a model reads "my biggest climbing day" as activity_type='climb'.
_TYPES_HINT = ("'run*' (all running: run, run_trail, run_treadmill), 'ride*' (all cycling: ride, ride_indoor, "
               "ride_gravel, ride_mtb), or one exact type: swim, walk, hike, strength, row, pilates, yoga, "
               "mobility, cardio, climb (rock or wall climbing, not elevation gain), tennis, pickleball, "
               "multisport, ski, other, or a single running or cycling subtype such as run_trail")


def _date_hint(today: date) -> str:
    """Today's date, the calendar anchors, and the relative date form every date parameter takes.

    A model has no clock: without today's date a small one guesses a year from its training data. Even with
    it, a small local model got "12 weeks ago" and "the 8 weeks before" wrong, so dates can also be given relative to
    today and the server does the arithmetic. This replaced a list of seven precomputed "N weeks ago" dates,
    which cost about 800 tokens across the tool list. Calendar months stay as plain dates, since "last month"
    is not "-4w"."""
    month_start = today.replace(day=1)
    last_end = month_start - timedelta(days=1)
    return (f"(YYYY-MM-DD, or relative to today: 'today', '-10d' 10 days ago, '-3w' 3 weeks ago, '-3w-1d' the "
            f"day before that. Today is {today.isoformat()}, a {today.strftime('%A')}; this "
            f"month from "
            f"{month_start.isoformat()}; last month {last_end.replace(day=1).isoformat()} to "
            f"{last_end.isoformat()}; this year from {today.year}-01-01)")


def _relative(value: Any, today: date) -> str | None:
    """A relative date ('today', '-10d', '-3w', '-3w-1d', 'today-2w') as YYYY-MM-DD, or None if it isn't one."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower().replace(" ", "")
    text = text[len("today"):] if text.startswith("today") else text
    if not text:
        return today.isoformat()
    parts = re.findall(r"([+-])(\d+)([dw])", text)
    if not parts or "".join(sign + n + unit for sign, n, unit in parts) != text:
        return None
    days = sum((1 if sign == "+" else -1) * int(n) * (7 if unit == "w" else 1) for sign, n, unit in parts)
    return (today + timedelta(days=days)).isoformat()


def _family_note(conn: sqlite3.Connection, activity_type: str | None, from_date: OptFromDate = None,
                 to_date: str | None = None) -> str | None:
    """A note when an exact family root ('run', 'ride') leaves out sibling types that have activities in range.

    'run' means road runs only; a model that meant all running gets told, with the counts left out, instead of
    reporting a partial total as the whole."""
    if not activity_type or activity_type.endswith("*"):
        return None
    family = activity_type.split("_")[0]
    if activity_type != family:
        return None
    where, params = queries._activity_where(from_date, to_date, family + "*", None)
    others = [(r[0], r[1]) for r in conn.execute(
        f"SELECT activity_type, COUNT(*) FROM activities {where} GROUP BY activity_type ORDER BY 2 DESC", params)
        if r[0] != activity_type]
    if not others:
        return None
    left = ", ".join(f"{t} ({n})" for t, n in others)
    return (f"activity_type '{activity_type}' is the exact type only; it leaves out {left} in this range. "
            f"If the question is about all of them, call again with activity_type='{family}*'.")

def _empty_note(activity_type: str | None, found: bool) -> str | None:
    """A note when an activity_type filter matched nothing, so a model reconsiders the filter rather than
    reporting "none". A small local model passed activity_type='climb' for "my biggest climbing day" and answered that
    there was no climbing."""
    if not activity_type or found:
        return None
    return (f"No '{activity_type}' activities in this range. activity_type keeps only that sport and leaves "
            f"every other activity out; to cover all activities (for example elevation gain or distance across "
            f"every sport), call again without activity_type.")


# Fixed choices are Literal types, so they show up as enums in each tool's input schema: a small model sees
# the allowed values in the schema itself, and a wrong value is refused with the list of valid ones.
Period = Literal["total", "day", "week", "month"]
TotalsSort = Literal["period", "distance", "duration", "elevation", "activities"]
ActivitySort = Literal["start", "start_asc", "distance", "duration", "elevation", "load"]
GroupBy = Literal["type", "training_effect"]
Compare = Literal["previous_period"]
# Where the model fills in from_date, so it reaches for '-12w' rather than counting back on a calendar: in the
# tool description alone, a small local model still worked out "12 weeks ago" itself in four of five tries, and wrongly.
_FROM_HELP = "YYYY-MM-DD, or relative: for 'the last N weeks' use '-Nw', for 'the last N days' '-Nd'"
FromDate = Annotated[str, Field(description=_FROM_HELP)]
OptFromDate = Annotated[str | None, Field(description=_FROM_HELP)]
WellnessField = Literal["resting_hr", "hrv", "sleep", "body_battery", "stress", "steps", "readiness", "load"]

# The wellness columns each `fields` choice returns. A small model asked about HRV and sleep doesn't need 12
# other columns per day: every field for 6 weeks is ~16,000 characters, one group about a quarter of that.
_WELLNESS_FIELDS = {
    "resting_hr": ("resting_hr",),
    "hrv": ("hrv_avg_ms", "hrv_status"),
    "sleep": ("sleep_h", "sleep_score"),
    "body_battery": ("body_battery_high", "body_battery_low"),
    "stress": ("stress_avg",),
    "steps": ("steps",),
    "readiness": ("readiness_score",),
    "load": ("acute_load", "chronic_load", "acute_chronic_ratio"),
}
# Listed by choice, not by column: given the column names first, a small model passed them as fields.
_WELLNESS_FIELDS_HINT = "; ".join(f"'{f}' ({', '.join(cols)})" for f, cols in _WELLNESS_FIELDS.items())


def _wellness_fields(value: Any) -> Any:
    """Read `fields` the ways a small model sends it: one passed "hrv,sleep" as one string, and column
    names (acute_load) instead of their choice (load). Both map to the choices; anything else is still refused
    with the list of choices."""
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, list):
        by_column = {c: f for f, cols in _WELLNESS_FIELDS.items() for c in cols}
        value = list(dict.fromkeys(by_column.get(v, v) if isinstance(v, str) else v for v in value))
    return value


WellnessFields = Annotated[list[WellnessField], BeforeValidator(_wellness_fields)]


def _date_list(value: Any) -> Any:
    """Read `dates` as a list or one comma-separated string, keeping the YYYY-MM-DD part of a timestamp that a
    model copies from an activity's start_utc."""
    if isinstance(value, str):
        value = [v for v in re.split(r"[,\s]+", value) if v]
    if isinstance(value, list):
        value = [v[:10] if isinstance(v, str) else v for v in value]
    return value


DateList = Annotated[list[str], BeforeValidator(_date_list)]
# wellness_after takes at most this many dates in one call: 10 dates of 2 days is ~1,000 tokens with fields.
_AFTER_MAX = 10


def _pick_fields(rows: list[dict[str, Any]], fields: list[str]) -> tuple[list[dict[str, Any]], int]:
    """Each row cut to its date (and day_after) and the asked fields' columns, without the rows that have no
    value for any of them, and how many were dropped. Dropped and counted, so a model reports the gap in a
    sentence instead of a table of nulls (or reading the nulls as zero)."""
    cols = [c for f in _WELLNESS_FIELDS if f in fields for c in _WELLNESS_FIELDS[f]]
    keep = ("date", "day_after")
    picked = [{**{k: r[k] for k in keep if k in r}, **{c: r[c] for c in cols}} for r in rows]
    kept = [r for r in picked if any(r[c] is not None for c in cols)]
    return kept, len(picked) - len(kept)

# list_activities returns this many rows unless asked for more: about 400 characters a row, so 50 rows is
# ~5,000 tokens, more than a small local model's whole default context window.
_LIST_DEFAULT = 10
# What a model should read first. A small model reads a response front to back and acts on what comes first: it
# skipped a note placed after the data, and quoted a record's `value` in seconds over the `time` after it.
_NOTES_FIRST = ("error", "note", "type_note")
_RECORD_LEAD = ("label", "record", "time", "date", "activity_name", "activity_type")


def _lead(row: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """The same dict with `keys` (those present) moved to the front, in that order."""
    return {**{k: row[k] for k in keys if k in row}, **row}


# Fields list_activities leaves out of each row (get_activity has them all). In a small model's answers to "my
# five longest runs by distance and by time", the second list copied rows from the first when both were long
# and alike; dropping these makes each row about a third shorter. check_reason is kept when it is set.
_LIST_DROP = ("start_utc", "source", "aerobic_te", "anaerobic_te")


def _list_row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items()
            if k not in _LIST_DROP and not (k == "check_reason" and v is None)}


# load_balance with no dates returns this many months: every month is ~2,800 tokens for a two-year archive, and
# past three or four months a small model began misquoting the older ones.
_MONTHS_DEFAULT = 3


# The figures compare_to sets side by side, each with previous_<figure> and <figure>_change.
_COMPARE_FIGURES = ("activities", "hours", "distance_km", "elev_gain_m")


def _previous_span(from_date: FromDate, to_date: str) -> tuple[str, str]:
    """The span of the same length that ends the day before from_date."""
    start, end = date.fromisoformat(from_date), date.fromisoformat(to_date)
    prev_end = start - timedelta(days=1)
    return (prev_end - (end - start)).isoformat(), prev_end.isoformat()


def _compared(current: list[dict[str, Any]], previous: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """One row per group across both spans: each figure, its previous_ value and the change, worked out here.

    Comparing two spans was two calls and a subtraction per figure for the model; a small one got the second
    span's dates wrong and wrote "decreased from 14 to 23". Groups in only one span count 0 in the other."""
    now, before = {r[key]: r for r in current}, {r[key]: r for r in previous}
    rows = []
    for group in [*now, *(g for g in before if g not in now)]:
        row: dict[str, Any] = {key: group}
        for fig in _COMPARE_FIGURES:
            a, b = (now.get(group) or {}).get(fig) or 0, (before.get(group) or {}).get(fig) or 0
            row.update({fig: a, f"previous_{fig}": b, f"{fig}_change": round(a - b, 1)})
        rows.append(row)
    return rows


def _compare_error(compare_to: str | None, from_date: str | None, to_date: str | None,
                   period: str) -> dict[str, str] | None:
    if not compare_to:
        return None
    try:
        if period == "total" and from_date and to_date:
            _previous_span(from_date, to_date)
            return None
    except ValueError:
        pass
    return {"error": "compare_to='previous_period' needs from_date and to_date (YYYY-MM-DD or relative, such as "
                     "'-4w' and 'today') and period='total'."}


def _type_error(conn: sqlite3.Connection, activity_type: str | None) -> dict[str, str] | None:
    """An actionable error for an activity_type that would silently match nothing, or None if it's fine."""
    if not activity_type:
        return None
    known = _KNOWN_TYPES | {r[0] for r in conn.execute("SELECT DISTINCT activity_type FROM activities")}
    if activity_type.endswith("*"):
        ok = any(t.startswith(activity_type[:-1]) for t in known)
    else:
        ok = activity_type in known
    if ok:
        return None
    return {"error": f"unknown activity_type '{activity_type}'. Use one of: {_TYPES_HINT}."}


def _capped(rows: list, shown: int, hint: str) -> dict[str, Any]:
    """Trim rows fetched with one extra to `shown`, and say so plainly when that hid some (never silently)."""
    out: dict[str, Any] = {"truncated": len(rows) > shown}
    if out["truncated"]:
        out["note"] = f"Only the first {shown} rows are shown. {hint}"
    return out


def transport_security(port: int):
    """DNS-rebinding protection for the HTTP transport: accept only local Hosts and Origins."""
    from mcp.server.transport_security import TransportSecuritySettings

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost"],
        allowed_origins=[f"http://127.0.0.1:{port}", f"http://localhost:{port}"],
    )


def build_server(data_dir: Path, allow_sql: bool = False, http: bool = False, port: int = 8766):
    """Build the FastMCP server with the read tools, add_note, and (with allow_sql) query_sql."""
    from mcp.server.fastmcp import FastMCP

    db_path = data_dir / "archive.db"
    mcp = FastMCP("minou-sync", host="127.0.0.1", port=port, transport_security=transport_security(port))

    def _ro() -> sqlite3.Connection:
        return queries.connect_ro(db_path)

    from mcp.types import CallToolResult, TextContent, ToolAnnotations

    def tool(**kwargs):
        """mcp.tool, but the text a model reads is compact JSON.

        FastMCP pretty-prints results with a two-space indent, which costs a small model 30-40% more tokens for
        the same answer (load_balance: about 4,500 against 2,800). structuredContent is the same dict either way."""
        def register(fn):
            @functools.wraps(fn)
            def compact(*args, **kw):
                # Relative from_date/to_date become plain dates here, and the response says which, so an
                # answer can still name the dates it covers.
                today, resolved = date.today(), {}
                for name in ("from_date", "to_date"):
                    if (day := _relative(kw.get(name), today)) is not None:
                        kw[name] = resolved[name] = day
                out = fn(*args, **kw)
                if resolved:
                    # A tool that resolved the range itself (compare_to) has already said which dates.
                    out = {"dates": f"{kw.get('from_date') or 'start'} to {kw.get('to_date') or 'today'}", **out}
                out = _lead(out, _NOTES_FIRST)
                text = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
                return CallToolResult(content=[TextContent(type="text", text=text)], structuredContent=out)
            return mcp.tool(**kwargs)(compact)
        return register

    # Every tool but add_note only reads the local archive; clients can use this to skip confirmation.
    read = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

    # Today's date, in every tool that takes dates. A model has no clock: without this, a small one guesses a
    # year from its training data and answers "the last 12 weeks" from the wrong year. Set when the server
    # starts, so a client that keeps one server running past midnight should reload it.
    today = date.today()
    dates = _date_hint(today)

    @tool(annotations=read,
              description="Summary of the archive: how many activities by source and type, the date range, the "
                          "activity types present, how many wellness days, and the last sync time.")
    def archive_summary() -> dict[str, Any]:
        conn = _ro()
        try:
            return queries.archive_summary(conn)
        finally:
            conn.close()

    @tool(annotations=read,
              description=f"Activities between two dates {dates}, each with its date, name, activity_type, "
                          f"duration_h, distance_km, elev_gain_m, avg HR/power, training_effect_label and "
                          f"training_load, and check_reason if it was flagged as implausible; name each activity "
                          f"by date, name and type when you answer. Returns the top `limit` rows "
                          f"(default {_LIST_DEFAULT}, max 50) in `sort` order, so choose the sort that puts the "
                          f"ones you need first: 'start' (newest, default), 'start_asc', 'distance', 'duration', "
                          f"'elevation' or 'load' (hardest by Garmin training load, for 'my hardest sessions'). "
                          f"For 'my five longest runs' use activity_type='run*', sort='distance', limit=5, and a "
                          f"second call with sort='duration' for the longest by time. `total` is how many matched. "
                          f"Optional activity_type is {_TYPES_HINT}. Optional name_contains filters "
                          f"by title. To count or sum activities, use totals or breakdown instead.")
    def list_activities(from_date: FromDate, to_date: str, activity_type: str | None = None,
                        name_contains: str | None = None, sort: ActivitySort = "start",
                        limit: int = _LIST_DEFAULT) -> dict[str, Any]:
        conn = _ro()
        try:
            if err := _type_error(conn, activity_type):
                return err
            page = queries.list_activities(conn, from_date, to_date, activity_type, name_contains, sort=sort,
                                           limit=limit)
            out = {"activities": [_list_row(r) for r in page.rows], "total": page.total,
                   "truncated": page.truncated}
            if page.truncated:
                out["note"] = (f"Showing {len(page.rows)} of {page.total}. Pick a sort that puts the ones you need "
                               f"first, or raise limit (max 50).")
            if note := (_family_note(conn, activity_type, from_date, to_date)
                        or _empty_note(activity_type, page.total > 0)):
                out["type_note"] = note
            return out
        finally:
            conn.close()

    @tool(annotations=read,
              description="One activity by its key (from list_activities), with its merged fields, the sources it "
                          "came from, and any notes.")
    def get_activity(key: str) -> dict[str, Any]:
        conn = _ro()
        try:
            found = queries.get_activity(conn, key)
            return found or {"error": f"no activity with key {key}"}
        finally:
            conn.close()

    @tool(annotations=read,
              description=f"Training totals between two dates {dates}: activity count, hours, distance_km and "
                          f"elev_gain_m. period is 'total' (default: one grand-total row for the whole range, "
                          f"nothing to add up \u2014 use it for 'how much have I run this year'), or 'day', 'week' "
                          f"or 'month' for one row per period when the question asks for a trend (period='week' "
                          f"for 'weekly elevation' or 'weekly distance' across all activities). sort is 'period' "
                          f"(newest first, default) or 'distance', 'duration', 'elevation' or 'activities' to put "
                          f"the biggest period first \u2014 e.g. period='day', sort='elevation' for 'my biggest "
                          f"climbing day' (elevation of every activity, so leave activity_type out); ranked days "
                          f"return the top {_LIST_DEFAULT} unless you set limit (max 50). At most 50 rows; "
                          f"`truncated` says if more were left out. Optional activity_type is "
                          f"{_TYPES_HINT}; so a 'how much running / cycling' question is one call with 'run*' or "
                          f"'ride*', not one per subtype. compare_to='previous_period' (with period='total') "
                          f"also gives the span of the same length just before, with previous_ figures and the "
                          f"_change, for 'this span against the one before'.")
    def totals(from_date: FromDate, to_date: str, period: Period = "total", activity_type: str | None = None,
               sort: TotalsSort = "period", compare_to: Compare | None = None,
               limit: int | None = None) -> dict[str, Any]:
        if err := _compare_error(compare_to, from_date, to_date, period):
            return err
        # Ranked days ("my biggest climbing day") need only the top few: 84 days of rows is ~1,500 tokens, and
        # with the rest of a multi-call answer it overflowed a small model's 8k window mid-reply.
        ranked = period == "day" and sort != "period"
        shown = max(1, min(limit or (_LIST_DEFAULT if ranked else queries.MAX_ROWS), queries.MAX_ROWS))
        conn = _ro()
        try:
            if err := _type_error(conn, activity_type):
                return err
            rows = queries.totals(conn, from_date, to_date, period, activity_type, sort=sort, limit=shown + 1)
            hint = (f"These are the top {shown} days by {sort}; raise limit (max 50) for more." if ranked else
                    "Narrow from_date/to_date, use a longer period, or a sort that puts the periods you need "
                    "first.")
            out = {"period": period, "totals": rows[:shown], **_capped(rows, shown, hint)}
            if compare_to:
                prev_from, prev_to = _previous_span(from_date, to_date)
                previous = queries.totals(conn, prev_from, prev_to, period, activity_type)
                out.update(totals=_compared(out["totals"], previous, "period"),
                           dates=f"{from_date} to {to_date}", previous_dates=f"{prev_from} to {prev_to}")
            if note := (_family_note(conn, activity_type, from_date, to_date)
                        or _empty_note(activity_type, any(r["activities"] for r in rows))):
                out["type_note"] = note
            return out
        finally:
            conn.close()

    @tool(annotations=read,
              description=f"Activity count, hours, distance_km and elev_gain_m grouped by activity type or by "
                          f"Garmin's training-effect label, busiest first. group_by is 'type' (default) or "
                          f"'training_effect' (aerobic base / tempo / lactate threshold / vo2max / ...). period is "
                          f"'total' (default: one row per group for the whole range) or 'day', 'week' or 'month' "
                          f"for one row per period and group, newest first \u2014 e.g. period='week' for 'weekly "
                          f"distance by activity type', in one call. Optional from_date/to_date {dates} and "
                          f"activity_type ({_TYPES_HINT}). Use this for 'how many "
                          f"runs by training effect' or 'sessions by type' \u2014 the server counts, so you needn't "
                          f"tally rows yourself. To compare a span with the one before it, make one call with "
                          f"compare_to='previous_period' and period='total': for the last 4 weeks against the 4 "
                          f"before, from_date='-4w', to_date='today'. Each group then has its previous_ figures "
                          f"and the _change, so there is nothing to subtract. At most 50 rows; `truncated` says if "
                          f"more were left out.")
    def breakdown(group_by: GroupBy = "type", from_date: OptFromDate = None, to_date: str | None = None,
                  activity_type: str | None = None, period: Period = "total",
                  compare_to: Compare | None = None) -> dict[str, Any]:
        if compare_to and from_date and not to_date:
            to_date = date.today().isoformat()  # an open range ends today, here as everywhere else
        if err := _compare_error(compare_to, from_date, to_date, period):
            return err
        conn = _ro()
        try:
            if err := _type_error(conn, activity_type):
                return err
            rows = queries.breakdown(conn, group_by, from_date, to_date, activity_type, period=period,
                                     limit=queries.MAX_ROWS + 1)
            out = {"group_by": group_by, "period": period, "groups": rows[:queries.MAX_ROWS],
                   **_capped(rows, queries.MAX_ROWS, "Narrow from_date/to_date, use a longer period, or filter "
                                                     "by activity_type.")}
            if compare_to:
                prev_from, prev_to = _previous_span(from_date, to_date)
                previous = queries.breakdown(conn, group_by, prev_from, prev_to, activity_type)
                out.update(groups=_compared(out["groups"], previous, "group"),
                           dates=f"{from_date} to {to_date}", previous_dates=f"{prev_from} to {prev_to}")
            if note := (_family_note(conn, activity_type, from_date, to_date)
                        or _empty_note(activity_type, bool(rows))):
                out["type_note"] = note
            return out
        finally:
            conn.close()

    @tool(annotations=read,
              description=f"Up to 50 days of daily wellness between two dates {dates}, newest first. `fields` "
                          f"picks what each day carries; give only what the question asks about, since every "
                          f"field for 6 weeks is about 4,000 tokens. The choices: {_WELLNESS_FIELDS_HINT}. Leave "
                          f"fields out for all of them. With fields, days with no value for any of them are left "
                          f"out and counted in days_without_data. Ask for only the days you need; `truncated` says "
                          f"if older days were left out. For the days after particular sessions or dates, use "
                          f"wellness_after instead.")
    def wellness(from_date: FromDate, to_date: str, fields: WellnessFields | None = None) -> dict[str, Any]:
        conn = _ro()
        try:
            rows = queries.wellness(conn, from_date, to_date, limit=queries.MAX_ROWS + 1)
        finally:
            conn.close()
        out: dict[str, Any] = _capped(rows, queries.MAX_ROWS, "Older days were left out; split the range into "
                                                              "shorter spans.")
        rows = rows[:queries.MAX_ROWS]
        if fields:
            kept, out["days_without_data"] = _pick_fields(rows, fields)
            if kept and out["days_without_data"]:
                out["note"] = (f"{out['days_without_data']} of {len(rows)} days have no {', '.join(fields)} "
                               f"values; the days shown run from {kept[-1]['date']} to {kept[0]['date']}.")
            elif not kept and rows:
                out["note"] = f"None of the {len(rows)} days has {', '.join(fields)} values."
            rows = kept
        return {"days": rows, **out}

    @tool(annotations=read,
              description=f"Daily wellness on the days after each of up to {_AFTER_MAX} dates, in one call: for "
                          f"'how did I recover after my hardest sessions / my race / that trip'. Get the dates "
                          f"first (for example list_activities with sort='load'), then pass them all here at once "
                          f"as `dates` (YYYY-MM-DD): the session dates themselves, since the tool works out the "
                          f"days after. `days` is how many days after each date (default 2, max 7). "
                          f"Sleep and HRV on a date are from the night before it, so day_after 1 holds the first "
                          f"night after the session. `fields` as in wellness: {_WELLNESS_FIELDS_HINT}; leave it "
                          f"out for all. Days with no value for the asked fields are left out and counted in "
                          f"days_without_data.")
    def wellness_after(dates: DateList, days: int = 2, fields: WellnessFields | None = None) -> dict[str, Any]:
        if not 1 <= days <= 7:
            return {"error": "days must be between 1 and 7"}
        try:
            starts = [date.fromisoformat(d) for d in dates]
        except ValueError:
            return {"error": f"dates must be YYYY-MM-DD; got {dates}"}
        if not starts:
            return {"error": "give at least one date"}
        out: dict[str, Any] = {"truncated": len(starts) > _AFTER_MAX}
        if out["truncated"]:
            out["note"] = (f"Only the first {_AFTER_MAX} of {len(starts)} dates are shown; call again with the "
                           f"rest.")
        conn = _ro()
        try:
            after, missing = [], 0
            for start in starts[:_AFTER_MAX]:
                first, last = start + timedelta(days=1), start + timedelta(days=days)
                rows = queries.wellness(conn, first.isoformat(), last.isoformat(), limit=days)[::-1]
                for r in rows:
                    r["day_after"] = (date.fromisoformat(r["date"]) - start).days
                missing += days - len(rows)  # days with no wellness row at all
                if fields:
                    rows, dropped = _pick_fields(rows, fields)
                    missing += dropped
                after.append({"after": start.isoformat(), "days": [_lead(r, ("date", "day_after")) for r in rows]})
        finally:
            conn.close()
        out["days_without_data"] = missing
        return {"after": after, **out}

    @tool(annotations=read,
              description=f"Garmin's monthly training-load split by intensity between two dates {dates}, one "
                          "row per month, newest first. Each month gives aerobic_low, aerobic_high and anaerobic, "
                          "each with the load, Garmin's target_min/target_max and a status of 'below', 'in range' "
                          "or 'above', plus Garmin's feedback phrase. Use this for 'am I balanced' or load-by-zone "
                          "questions. It has no acute load, chronic load or their ratio: for those use wellness "
                          "with fields=['load']. With no dates it returns "
                          f"the latest {_MONTHS_DEFAULT} months; give from_date for older ones. The current month "
                          "may not have a split yet.")
    def load_balance(from_date: OptFromDate = None, to_date: str | None = None) -> dict[str, Any]:
        conn = _ro()
        try:
            months = queries.load_balance(conn, from_date, to_date)
            if from_date or to_date:
                out: dict[str, Any] = {"months": months}
            else:
                out = {"months": months[:_MONTHS_DEFAULT], "truncated": len(months) > _MONTHS_DEFAULT}
                if out["truncated"]:
                    out["note"] = (f"The latest {_MONTHS_DEFAULT} of {len(months)} months. For older ones, call "
                                   f"again with from_date and to_date.")
            if not months and (from_date or to_date):
                # An empty range usually means the month isn't in yet; say where the data is, so a model asks
                # again rather than answering "no data".
                latest = queries.load_balance(conn)
                if latest:
                    out["note"] = (f"No load split in this range. The latest month with one is "
                                   f"{latest[0]['month']}; call again with a range that includes it.")
            return out
        finally:
            conn.close()

    @tool(annotations=read,
              description="Best efforts and records for a sport. sport is 'run' (fastest 1 km, mile, 5K, 10K, "
                          "half marathon, marathon, 50K, in seconds) or 'ride' (fastest 40 km in seconds, best 5-, "
                          f"20- and 60-minute power in watts). Optional from_date/to_date {dates} limit the "
                          "window. `efforts` are the records: each gives the time (h:mm:ss) or watts, the date, and "
                          "the activity_name and activity_type that set it; report all three. Flagged and hidden "
                          "activities are left out. Garmin's own records come alongside as a cross-check; any set "
                          "by an activity flagged as implausible are moved to garmin_records_left_out and must "
                          "not be reported as records.")
    def best_efforts(sport: Literal["run", "ride"], from_date: OptFromDate = None,
                     to_date: str | None = None) -> dict[str, Any]:
        conn = _ro()
        try:
            out = queries.best_efforts(conn, sport, from_date, to_date)
        finally:
            conn.close()
        # A small model reports every record it is given, flag or not, so the ones set by an implausible
        # activity go in their own list with a plain note instead of a boolean beside a clean-looking value.
        out["efforts"] = [_lead(r, _RECORD_LEAD) for r in out["efforts"]]
        records = [_lead(r, _RECORD_LEAD) for r in out.pop("garmin_records")]
        out["garmin_records"] = [r for r in records if not r["set_by_flagged_activity"]]
        left_out = [r for r in records if r["set_by_flagged_activity"]]
        if left_out:
            out["garmin_records_left_out"] = left_out
            names = sorted({f"'{r['activity_name']}' ({r['date']})" for r in left_out})
            out["note"] = (f"Garmin's own {', '.join(r['record'].removeprefix('Garmin ') for r in left_out)} "
                           f"{'was' if len(left_out) == 1 else 'were'} set by {', '.join(names)}, flagged as "
                           f"implausible, so {'it is' if len(left_out) == 1 else 'they are'} not records. "
                           f"Report `efforts` instead.")
        return out

    @tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                                          openWorldHint=False),
              description="Save a short note against an activity or a day. target is 'activity' (ref is an activity "
                          "key) or 'day' (ref is a date YYYY-MM-DD). Notes never change activity or wellness values.")
    def add_note(target: Literal["activity", "day"], ref: str, text: str) -> dict[str, Any]:
        if not text.strip():
            return {"error": "text is empty"}
        conn = db.connect(data_dir)
        db.migrate(conn)
        try:
            with conn:
                cur = conn.execute(
                    "INSERT INTO notes (target, ref, text, author, created_utc) VALUES (?, ?, ?, ?, ?)",
                    (target, ref, text, "mcp", db.utc_now()),
                )
            return {"note_id": cur.lastrowid}
        finally:
            conn.close()

    if allow_sql:
        @tool(annotations=read,
                  description="Run one read-only SQL SELECT against the archive and return up to 50 rows. Only a "
                              "single SELECT is allowed; anything that writes is refused.")
        def query_sql(sql: str) -> dict[str, Any]:
            if not _SELECT_ONLY.match(sql) or _FORBIDDEN.search(sql):
                return {"error": "only a single read-only SELECT is allowed"}
            conn = _ro()
            try:
                cur = conn.execute(sql)
                rows = [dict(r) for r in cur.fetchmany(queries.MAX_ROWS + 1)]
                truncated = len(rows) > queries.MAX_ROWS
                return {"rows": rows[:queries.MAX_ROWS], "truncated": truncated}
            except sqlite3.Error as e:
                return {"error": str(e)}
            finally:
                conn.close()

    return mcp


def serve(data_dir: Path, http: bool = False, port: int = 8766, allow_sql: bool = False) -> None:
    """Run the MCP server over stdio, or Streamable HTTP on 127.0.0.1 with --http."""
    mcp = build_server(data_dir, allow_sql=allow_sql, http=http, port=port)
    mcp.run(transport="streamable-http" if http else "stdio")
