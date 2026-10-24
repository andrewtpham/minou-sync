"""MCP answers match the archive.

For any made-up archive and date range, the totals and list_activities tools agree with a direct SQL sum and
count over activities, and no answer carries more than 50 rows.
"""

import asyncio
import sqlite3
import tempfile
from datetime import date, timedelta
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from mcp.shared.memory import create_connected_server_and_client_session

from minou_sync import db, mcp_server, queries

TYPES = ["run", "ride", "swim", "yoga"]


@st.composite
def activities(draw):
    rows = []
    for i in range(draw(st.integers(0, 80))):
        d = date(2026, 1, 1) + timedelta(days=draw(st.integers(0, 300)))
        rows.append((
            f"garmin:{i}", draw(st.sampled_from(TYPES)),
            d.isoformat() + "T06:00:00",
            draw(st.integers(0, 20000)),            # elapsed_s
            float(draw(st.integers(0, 60000))),     # distance_m
        ))
    return rows


def _seed(conn, rows):
    for key, type_, start_local, elapsed, dist in rows:
        conn.execute(
            "INSERT INTO activities (key, source, activity_type, name, start_utc, start_local, elapsed_s, distance_m, "
            "elev_gain_m) VALUES (?, 'garmin', ?, 'x', ?, ?, ?, ?, 0)",
            (key, type_, start_local + "Z", start_local, elapsed, dist))
    conn.commit()


async def _both(data_dir, f, t):
    """One in-memory session, both tool calls, to keep the property fast."""
    server = mcp_server.build_server(data_dir)
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        listed = (await session.call_tool("list_activities", {"from_date": f, "to_date": t})).structuredContent
        tot = (await session.call_tool(
            "totals", {"from_date": f, "to_date": t, "period": "month"})).structuredContent
        return listed, tot


@settings(max_examples=100, deadline=None)
@given(activities(), st.integers(0, 300), st.integers(0, 300))
def test_mcp_totals_and_list_match_sql(rows, lo, hi):
    f = (date(2026, 1, 1) + timedelta(days=min(lo, hi))).isoformat()
    t = (date(2026, 1, 1) + timedelta(days=max(lo, hi))).isoformat()
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "data"
        conn = db.connect(data_dir)
        db.migrate(conn)
        _seed(conn, rows)
        conn.close()

        truth = sqlite3.connect(data_dir / "archive.db")
        count = truth.execute(
            "SELECT COUNT(*) FROM activities WHERE substr(start_local,1,10) BETWEEN ? AND ?", (f, t)).fetchone()[0]
        # Per-month SQL truth, so the comparison is exact (no accumulated rounding).
        per_month = {r[0]: (r[1], r[2], r[3]) for r in truth.execute(
            "SELECT strftime('%Y-%m', start_local) AS p, COUNT(*), "
            "ROUND(SUM(elapsed_s)/3600.0, 1), ROUND(SUM(distance_m)/1000.0, 1) FROM activities "
            "WHERE substr(start_local,1,10) BETWEEN ? AND ? GROUP BY p", (f, t))}
        truth.close()

        listed, tot = asyncio.run(_both(data_dir, f, t))
        assert listed["total"] == count
        assert len(listed["activities"]) <= queries.MAX_ROWS
        if count > queries.MAX_ROWS:
            assert listed["truncated"] is True

        periods = tot["totals"]
        assert len(periods) <= queries.MAX_ROWS
        assert sum(p["activities"] for p in periods) == count
        # Each month's figures match the direct SQL sum for that month, exactly.
        for p in periods:
            n, hours, dist = per_month[p["period"]]
            assert (p["activities"], p["hours"], p["distance_km"]) == (n, hours, dist)


# --- Flagged activities are kept but never set records ---------------------------------------------------

@st.composite
def flagged_archive(draw):
    """Runs with a 5K effort each; some flagged, some hidden, the rest clean, with invented times."""
    rows = []
    for i in range(draw(st.integers(1, 40))):
        d = date(2026, 1, 1) + timedelta(days=draw(st.integers(0, 300)))
        state = draw(st.sampled_from(["clean", "flagged", "hidden"]))
        effort_s = float(draw(st.integers(900, 2000)))   # 5K time in seconds
        rows.append((i, d.isoformat() + "T06:00:00", state, effort_s))
    return rows


def _seed_flagged(conn, rows):
    for i, start_local, state, effort_s in rows:
        if state == "hidden":
            # A hidden activity has a hide edit and no activities row, but its effort row still exists.
            conn.execute("INSERT INTO activity_edits (key, field, value_json, created_utc) "
                         "VALUES (?, '_hidden', 'true', 'x')", (f"garmin:{i}",))
        else:
            reason = "average 99 km/h for a run (limit 25)" if state == "flagged" else None
            conn.execute(
                "INSERT INTO garmin_activities (garmin_id, activity_type, start_utc, origin, raw_json, imported_utc) "
                "VALUES (?, 'running', ?, 'sync', '{}', 'x')", (i, start_local + "Z"))
            conn.execute(
                "INSERT INTO activities (key, source, activity_type, name, start_utc, start_local, elapsed_s, "
                "distance_m, elev_gain_m, garmin_id, check_reason) "
                "VALUES (?, 'garmin', 'run', 'x', ?, ?, 2400, 10000.0, 50.0, ?, ?)",
                (f"garmin:{i}", start_local + "Z", start_local, i, reason))
            conn.execute("INSERT INTO activity_sources (key, source, source_id) VALUES (?, 'garmin', ?)",
                         (f"garmin:{i}", str(i)))
        conn.execute("INSERT INTO activity_efforts (source, source_id, effort, value, computed_utc) "
                     "VALUES ('garmin', ?, 'run_5k', ?, 'x')", (str(i), effort_s))
    conn.commit()


@settings(max_examples=100, deadline=None)
@given(flagged_archive())
def test_property_13_flagged_kept_but_never_sets_a_record(rows):
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "data"
        conn = db.connect(data_dir)
        db.migrate(conn)
        _seed_flagged(conn, rows)

        # best_efforts must ignore flagged and hidden activities.
        be = queries.best_efforts(conn, "run")
        fivek = next(e for e in be["efforts"] if e["effort"] == "run_5k")
        clean = [(i, s) for i, _, state, s in [(r[0], r[1], r[2], r[3]) for r in rows] if state == "clean"]
        if clean:
            assert fivek["value"] == round(min(s for _, s in clean), 1)
            assert fivek["activity_key"] == f"garmin:{min(clean, key=lambda c: c[1])[0]}"
        else:
            assert fivek["value"] is None

        # A flagged (not hidden) activity still appears in lists and counts in totals.
        flagged_keys = {f"garmin:{r[0]}" for r in rows if r[2] == "flagged"}
        listed_keys = {a["key"] for a in queries.list_activities(conn, limit=queries.MAX_ROWS).rows}
        total_count = sum(t["activities"] for t in queries.totals(conn, period="month"))
        visible = {f"garmin:{r[0]}" for r in rows if r[2] in ("clean", "flagged")}
        assert flagged_keys <= listed_keys or len(visible) > queries.MAX_ROWS
        assert total_count == len(visible)
        conn.close()
