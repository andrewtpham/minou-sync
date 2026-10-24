"""MCP server tests through the SDK's in-memory client. Made-up archive only."""

import asyncio
import sqlite3

import pytest

from mcp.shared.memory import create_connected_server_and_client_session

from minou_sync import db, mcp_server


def _seed(conn):
    conn.execute(
        "INSERT INTO activities (key, source, activity_type, name, start_utc, start_local, elapsed_s, distance_m, "
        "elev_gain_m, avg_hr) VALUES ('garmin:1', 'garmin', 'run', 'Morning run', '2026-10-01T06:00:00Z', "
        "'2026-10-01T08:00:00', 3600, 10000.0, 100.0, 150)")
    conn.execute("INSERT INTO activity_sources (key, source, source_id) VALUES ('garmin:1', 'garmin', '1')")
    conn.execute("INSERT INTO daily_wellness (date, resting_hr, sleep_s, steps, updated_utc) "
                 "VALUES ('2026-10-01', 48, 25200, 9000, '2026-10-19T00:00:00Z')")
    conn.execute("INSERT INTO meta (key, value) VALUES ('last_sync_utc', '2026-10-01T20:00:00Z')")
    conn.commit()


@pytest.fixture
def archive(data_dir):
    conn = db.connect(data_dir)
    db.migrate(conn)
    _seed(conn)
    conn.close()
    return data_dir


def _run(data_dir, coro_factory, allow_sql=False):
    async def main():
        server = mcp_server.build_server(data_dir, allow_sql=allow_sql)
        async with create_connected_server_and_client_session(server) as session:
            await session.initialize()
            return await coro_factory(session)
    return asyncio.run(main())


def _tool_names(data_dir, allow_sql=False):
    async def coro(session):
        return {t.name for t in (await session.list_tools()).tools}
    return _run(data_dir, coro, allow_sql=allow_sql)


def _call(session, name, **args):
    return session.call_tool(name, args)


def test_ten_tools_without_sql_eleven_with(archive):
    base = {"archive_summary", "list_activities", "get_activity", "totals", "breakdown", "wellness",
            "wellness_after", "load_balance", "best_efforts", "add_note"}
    assert _tool_names(archive) == base
    assert _tool_names(archive, allow_sql=True) == base | {"query_sql"}


def test_each_read_tool_answers(archive):
    async def coro(session):
        summary = (await _call(session, "archive_summary")).structuredContent
        assert summary["activities_total"] == 1
        acts = (await _call(session, "list_activities", from_date="2026-10-01", to_date="2026-10-02")).structuredContent
        assert acts["total"] == 1 and acts["activities"][0]["key"] == "garmin:1"
        # A list row is the short form: no start_utc or source, and no check_reason when it isn't flagged.
        assert not {"start_utc", "source", "check_reason"} & set(acts["activities"][0])
        one = (await _call(session, "get_activity", key="garmin:1")).structuredContent
        assert one["sources"] == ["garmin:1"] and one["distance_km"] == 10.0
        tot = (await _call(session, "totals", from_date="2026-10-01", to_date="2026-10-31",
                           period="month")).structuredContent
        assert tot["totals"][0]["activities"] == 1
        # period="total" returns one grand-total row; "run*" matches the run family.
        gt = (await _call(session, "totals", from_date="2026-01-01", to_date="2026-12-31",
                          period="total", activity_type="run*")).structuredContent
        assert gt["totals"][0]["period"] == "total" and gt["totals"][0]["distance_km"] == 10.0
        brk = (await _call(session, "breakdown", group_by="type")).structuredContent
        assert brk["groups"][0]["group"] == "run" and brk["groups"][0]["activities"] == 1
        # Leaving out period gives the one-row grand total, not a long list of weeks.
        dflt = (await _call(session, "totals", from_date="2026-01-01", to_date="2026-12-31")).structuredContent
        assert dflt["period"] == "total" and len(dflt["totals"]) == 1
        # A value outside an enum is refused, naming the allowed values.
        badg = await _call(session, "breakdown", group_by="phase")
        assert badg.isError and "'type' or 'training_effect'" in badg.content[0].text
        well = (await _call(session, "wellness", from_date="2026-10-01", to_date="2026-10-02")).structuredContent
        assert well["days"][0]["sleep_h"] == 7.0 and well["truncated"] is False
        be = (await _call(session, "best_efforts", sport="run")).structuredContent
        assert be["sport"] == "run" and any(e["effort"] == "run_5k" for e in be["efforts"])
        bad = await _call(session, "best_efforts", sport="triathlon")
        assert bad.isError and "'run' or 'ride'" in bad.content[0].text
    _run(archive, coro)


def test_date_tools_tell_the_model_today(archive):
    # A model has no clock; without today's date a small one picks a year from its training data.
    from datetime import date

    async def coro(session):
        return {t.name: t.description for t in (await session.list_tools()).tools}
    desc = _run(archive, coro)
    today = f"Today is {date.today().isoformat()}"
    for name in ("list_activities", "totals", "breakdown", "wellness", "load_balance", "best_efforts"):
        assert today in desc[name], name


def test_fixed_choices_are_enums_and_reads_are_annotated(archive):
    # Small models read allowed values from the schema, and clients can skip confirming read-only tools.
    async def coro(session):
        return {t.name: t for t in (await session.list_tools()).tools}
    tools = _run(archive, coro)
    props = tools["totals"].inputSchema["properties"]
    assert props["period"]["enum"] == ["total", "day", "week", "month"]
    assert "elevation" in props["sort"]["enum"]
    assert tools["list_activities"].inputSchema["properties"]["limit"]["default"] == 10
    assert tools["breakdown"].inputSchema["properties"]["group_by"]["enum"] == ["type", "training_effect"]
    assert all(tools[n].annotations.readOnlyHint for n in tools if n != "add_note")
    assert tools["add_note"].annotations.readOnlyHint is False


def test_unknown_activity_type_is_an_error_not_an_empty_answer(archive):
    # Without this, "running" would match nothing and a model would report zero.
    async def coro(session):
        out = []
        for tool, args in (("totals", {"from_date": "2026-01-01", "to_date": "2026-12-31"}),
                           ("breakdown", {}),
                           ("list_activities", {"from_date": "2026-01-01", "to_date": "2026-12-31"})):
            for t in ("running", "running*"):
                out.append((await _call(session, tool, activity_type=t, **args)).structuredContent)
        ok = (await _call(session, "totals", from_date="2026-01-01", to_date="2026-12-31",
                          activity_type="ride")).structuredContent  # known type, just none in the archive
        return out, ok
    errors, ok = _run(archive, coro)
    assert all("unknown activity_type" in e["error"] and "run*" in e["error"] for e in errors)
    assert "error" not in ok and ok["totals"][0]["activities"] == 0
    # ...but says so, and how to cover every activity, instead of a bare zero.
    assert "No 'ride' activities" in ok["type_note"] and "without activity_type" in ok["type_note"]


def test_date_hint_gives_today_the_calendar_anchors_and_the_relative_form():
    from datetime import date
    hint = mcp_server._date_hint(date(2026, 10, 21))
    assert "Today is 2026-10-21, a Wednesday" in hint and "'-3w' 3 weeks ago" in hint
    assert "last month 2026-09-01 to 2026-09-30" in hint and "this year from 2026-01-01" in hint
    # January's "last month" is December of the year before.
    assert "last month 2025-12-01 to 2025-12-31" in mcp_server._date_hint(date(2026, 1, 15))


def test_exact_family_root_says_what_it_left_out(archive):
    # 'run' is road runs only; a trail run in range is named so a model can ask again with 'run*'.
    conn = db.connect(archive)
    conn.execute(
        "INSERT INTO activities (key, source, activity_type, name, start_utc, start_local, elapsed_s, distance_m) "
        "VALUES ('garmin:2', 'garmin', 'run_trail', 'Hill run', '2026-10-02T06:00:00Z', '2026-10-02T08:00:00', "
        "7200, 15000.0)")
    conn.commit()
    conn.close()

    async def coro(session):
        args = {"from_date": "2026-10-01", "to_date": "2026-10-31"}
        return [(await _call(session, tool, activity_type=t, **args)).structuredContent
                for tool in ("totals", "breakdown", "list_activities") for t in ("run", "run*", "run_trail")]
    out = _run(archive, coro)
    exact, family, sub = out[0], out[1], out[2]
    assert "run_trail (1)" in exact["type_note"] and "'run*'" in exact["type_note"]
    assert "type_note" not in family and "type_note" not in sub
    assert all("type_note" in out[i] for i in (0, 3, 6))


def test_wellness_fields_pick_groups_and_drop_empty_days(archive):
    conn = db.connect(archive)
    conn.execute("INSERT INTO daily_wellness (date, resting_hr, hrv_avg_ms, hrv_status, updated_utc) "
                 "VALUES ('2026-10-02', 50, 41.0, 'BALANCED', 'x')")
    conn.commit()
    conn.close()

    async def coro(session):
        args = {"from_date": "2026-10-01", "to_date": "2026-10-02"}
        return ((await _call(session, "wellness", fields=["hrv"], **args)).structuredContent,
                (await _call(session, "wellness", **args)).structuredContent,
                (await _call(session, "wellness", fields="hrv_avg_ms, resting_hr", **args)).structuredContent,
                await _call(session, "wellness", fields=["heart"], **args))
    hrv, everything, loose, bad = _run(archive, coro)
    # Only the date and the hrv columns; 10-01 has no HRV, so it's counted rather than shown as nulls.
    assert hrv["days"] == [{"date": "2026-10-02", "hrv_avg_ms": 41.0, "hrv_status": "BALANCED"}]
    assert hrv["days_without_data"] == 1 and "1 of 2 days" in hrv["note"]
    assert len(everything["days"]) == 2 and "steps" in everything["days"][0]
    assert "days_without_data" not in everything
    # A comma-separated string and column names are read as their choices (hrv, resting_hr).
    assert [sorted(d) for d in loose["days"]] == [["date", "hrv_avg_ms", "hrv_status", "resting_hr"]] * 2
    # Anything else is refused, and the error lists the choices.
    assert bad.isError and "'hrv'" in bad.content[0].text


def test_relative_dates_resolve_against_today():
    from datetime import date
    today = date(2026, 10, 21)
    rel = lambda v: mcp_server._relative(v, today)  # noqa: E731
    assert rel("today") == "2026-10-21" and rel("-30d") == "2026-09-21"
    assert rel("-8w") == "2026-08-26" and rel("-8w-1d") == "2026-08-25" and rel("today-16w") == "2026-07-01"
    assert rel(" -2W ") == "2026-10-07"
    assert rel("2026-10-01") is None and rel("-8x") is None and rel("8w") is None and rel(None) is None


def test_relative_dates_are_resolved_and_echoed(archive, monkeypatch):
    from datetime import date

    class Fixed(date):
        @classmethod
        def today(cls):
            return date(2026, 10, 3)
    monkeypatch.setattr(mcp_server, "date", Fixed)

    async def coro(session):
        rel = (await _call(session, "totals", from_date="-1w", to_date="today")).structuredContent
        plain = (await _call(session, "totals", from_date="2026-09-26", to_date="2026-10-03")).structuredContent
        return rel, plain
    rel, plain = _run(archive, coro)
    assert rel["dates"] == "2026-09-26 to 2026-10-03" and rel["totals"] == plain["totals"]
    assert rel["totals"][0]["activities"] == 1 and "dates" not in plain


def test_compare_to_previous_period_sets_both_spans_side_by_side(archive):
    conn = db.connect(archive)
    conn.execute(
        "INSERT INTO activities (key, source, activity_type, name, start_utc, start_local, elapsed_s, distance_m, "
        "elev_gain_m) VALUES ('garmin:2', 'garmin', 'ride', 'Made-up ride', '2026-09-27T06:00:00Z', "
        "'2026-09-27T08:00:00', 7200, 40000.0, 300.0)")
    conn.commit()
    conn.close()

    async def coro(session):
        span = {"from_date": "2026-10-01", "to_date": "2026-10-07", "compare_to": "previous_period"}
        return ((await _call(session, "totals", **span)).structuredContent,
                (await _call(session, "breakdown", **span)).structuredContent,
                (await _call(session, "totals", from_date="2026-10-01", to_date="2026-10-07", period="week",
                             compare_to="previous_period")).structuredContent)
    tot, brk, bad = _run(archive, coro)
    # The span before 10-01..10-07 is the 7 days ending 09-30; the made-up ride on 09-27 falls in it.
    assert tot["dates"] == "2026-10-01 to 2026-10-07" and tot["previous_dates"] == "2026-09-24 to 2026-09-30"
    row = tot["totals"][0]
    assert (row["activities"], row["previous_activities"], row["activities_change"]) == (1, 1, 0)
    assert (row["distance_km"], row["previous_distance_km"], row["distance_km_change"]) == (10.0, 40.0, -30.0)
    # By group, a type in only one span counts 0 in the other.
    by = {r["group"]: r for r in brk["groups"]}
    assert (by["run"]["activities"], by["run"]["previous_activities"]) == (1, 0)
    assert (by["ride"]["activities"], by["ride"]["previous_activities"], by["ride"]["activities_change"]) == (0, 1, -1)
    assert "period='total'" in bad["error"]


def test_compare_to_without_to_date_ends_today(archive, monkeypatch):
    from datetime import date

    class Fixed(date):
        @classmethod
        def today(cls):
            return date(2026, 10, 7)
    monkeypatch.setattr(mcp_server, "date", Fixed)

    async def coro(session):
        return (await _call(session, "breakdown", from_date="-6d", compare_to="previous_period")).structuredContent
    out = _run(archive, coro)
    assert out["dates"] == "2026-10-01 to 2026-10-07" and out["previous_dates"] == "2026-09-24 to 2026-09-30"


def test_wellness_after_gives_the_days_after_each_date(archive):
    conn = db.connect(archive)
    conn.execute("INSERT INTO daily_wellness (date, resting_hr, hrv_avg_ms, hrv_status, updated_utc) "
                 "VALUES ('2026-10-02', 50, 41.0, 'BALANCED', 'x'), ('2026-10-06', 52, 38.0, 'LOW', 'x')")
    conn.commit()
    conn.close()

    async def coro(session):
        return ((await _call(session, "wellness_after", dates="2026-10-01, 2026-10-05T07:00:00Z",
                             fields=["hrv"])).structuredContent,
                (await _call(session, "wellness_after", dates=["2026-09-30"], days=1)).structuredContent,
                (await _call(session, "wellness_after", dates=["Oct 1"])).structuredContent)
    hrv, everything, bad = _run(archive, coro)
    # A comma-separated string and a copied timestamp both work; each date gets its following days, oldest
    # first, numbered by day_after. 10-03 has no row and 10-07 has none, so 2 days are counted as missing.
    assert hrv["after"] == [
        {"after": "2026-10-01", "days": [{"date": "2026-10-02", "day_after": 1, "hrv_avg_ms": 41.0,
                                          "hrv_status": "BALANCED"}]},
        {"after": "2026-10-05", "days": [{"date": "2026-10-06", "day_after": 1, "hrv_avg_ms": 38.0,
                                          "hrv_status": "LOW"}]}]
    assert hrv["days_without_data"] == 2 and hrv["truncated"] is False
    # Without fields every column comes back, date and day_after first.
    day = everything["after"][0]["days"][0]
    assert list(day)[:2] == ["date", "day_after"] and day["steps"] == 9000
    assert "YYYY-MM-DD" in bad["error"]


def test_load_balance_empty_range_points_at_the_latest_month(data_dir):
    import json

    conn = db.connect(data_dir)
    db.migrate(conn)
    raw = json.dumps({"x": {"metricsTrainingLoadBalanceDTOMap": {"d": {"monthlyLoadAerobicLow": 10}}}})
    conn.execute("INSERT INTO garmin_raw (endpoint, key, raw_json, fetched_utc) VALUES "
                 "('get_training_status', '2026-09-20', ?, 'x')", (raw,))
    conn.commit()
    conn.close()

    async def coro(session):
        return (await _call(session, "load_balance", from_date="2026-10-01",
                            to_date="2026-10-21")).structuredContent
    out = _run(data_dir, coro)
    assert out["months"] == [] and "2026-09" in out["note"]


def test_load_balance_without_dates_returns_the_latest_months(data_dir):
    import json

    conn = db.connect(data_dir)
    db.migrate(conn)
    raw = json.dumps({"x": {"metricsTrainingLoadBalanceDTOMap": {"d": {"monthlyLoadAerobicLow": 10}}}})
    for day in ("2026-06-20", "2026-07-20", "2026-08-20", "2026-09-20"):
        conn.execute("INSERT INTO garmin_raw (endpoint, key, raw_json, fetched_utc) VALUES "
                     "('get_training_status', ?, ?, 'x')", (day, raw))
    conn.commit()
    conn.close()

    async def coro(session):
        latest = await _call(session, "load_balance")
        ranged = (await _call(session, "load_balance", from_date="2026-01-01")).structuredContent
        return latest, ranged
    latest, ranged = _run(data_dir, coro)
    out = latest.structuredContent
    assert [m["month"] for m in out["months"]] == ["2026-09", "2026-08", "2026-07"]
    assert out["truncated"] and "3 of 4 months" in out["note"]
    assert len(ranged["months"]) == 4  # a date range is never cut to the default
    # The text a model reads is compact JSON, the same data as structuredContent.
    text = latest.content[0].text
    assert "\n" not in text and ": " not in text and json.loads(text) == out


def test_best_efforts_moves_garmin_records_from_flagged_activities_aside(data_dir):
    import json

    conn = db.connect(data_dir)
    db.migrate(conn)
    conn.execute("INSERT INTO garmin_activities (garmin_id, activity_type, start_utc, origin, raw_json, "
                 "imported_utc) VALUES (9, 'running', '2026-04-10T06:00:00Z', 'sync', '{}', 'x')")
    conn.execute("INSERT INTO activities (key, source, activity_type, name, start_utc, start_local, garmin_id, "
                 "check_reason) VALUES ('garmin:9', 'garmin', 'run', 'Too fast', '2026-04-10T06:00:00Z', "
                 "'2026-04-10T08:00:00', 9, 'average 31 km/h for a run (limit 25)')")
    prs = [{"typeId": 3, "value": 600.0, "activityId": 9, "prStartTimeGmtFormatted": "2026-04-10T06:00:00.0"},
           {"typeId": 5, "value": 5400.0, "activityId": 8, "prStartTimeGmtFormatted": "2026-05-01T06:00:00.0"}]
    conn.execute("INSERT INTO garmin_raw (endpoint, key, raw_json, fetched_utc) VALUES "
                 "('get_personal_record', '', ?, 'x')", (json.dumps(prs),))
    conn.commit()
    conn.close()

    async def coro(session):
        return (await _call(session, "best_efforts", sport="run")).structuredContent
    out = _run(data_dir, coro)
    assert [r["record"] for r in out["garmin_records"]] == ["Garmin fastest half marathon"]
    assert out["garmin_records"][0]["time"] == "1:30:00" and out["garmin_records"][0]["unit"] == "seconds"
    assert [r["record"] for r in out["garmin_records_left_out"]] == ["Garmin fastest 5K"]
    assert "'Too fast' (2026-04-10)" in out["note"] and "not records" in out["note"]
    # What to report comes first: the note before the data, and a record's time before its raw seconds.
    assert list(out)[0] == "note"
    assert list(out["garmin_records"][0])[:3] == ["record", "time", "date"]
    assert list(out["efforts"][0])[:2] == ["label", "time"]


def _many_runs(data_dir, n):
    conn = db.connect(data_dir)
    db.migrate(conn)
    for i in range(n):
        conn.execute(
            "INSERT INTO activities (key, source, activity_type, name, start_utc, start_local, elapsed_s, distance_m, "
            "elev_gain_m) VALUES (?, 'garmin', 'run', 'Run', ?, ?, 3600, ?, ?)",
            (f"garmin:{i}", f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}T06:00:00Z",
             f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}T06:00:00", 1000.0 * (i + 1), float(i)))
        conn.execute("INSERT INTO daily_wellness (date, resting_hr, updated_utc) VALUES (?, 50, 'x')",
                     (f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}",))
    conn.commit()
    conn.close()


def test_long_lists_are_short_by_default_and_say_when_cut(data_dir):
    _many_runs(data_dir, 60)  # 60 days with one run each, Jan to early Mar 2026

    async def coro(session):
        r = {}
        r["list"] = (await _call(session, "list_activities", from_date="2026-01-01", to_date="2026-12-31",
                                 sort="distance")).structuredContent
        r["top5"] = (await _call(session, "list_activities", from_date="2026-01-01", to_date="2026-12-31",
                                 sort="distance", limit=5)).structuredContent
        r["days"] = (await _call(session, "totals", from_date="2026-01-01", to_date="2026-12-31",
                                 period="day")).structuredContent
        r["climb"] = (await _call(session, "totals", from_date="2026-01-01", to_date="2026-12-31",
                                  period="day", sort="elevation")).structuredContent
        r["climb3"] = (await _call(session, "totals", from_date="2026-01-01", to_date="2026-12-31",
                                   period="day", sort="elevation", limit=3)).structuredContent
        r["weeks"] = (await _call(session, "totals", from_date="2026-01-01", to_date="2026-12-31",
                                  period="week")).structuredContent
        r["byweek"] = (await _call(session, "breakdown", from_date="2026-01-01", to_date="2026-12-31",
                                   period="week")).structuredContent
        r["byday"] = (await _call(session, "breakdown", from_date="2026-01-01", to_date="2026-12-31",
                                  period="day")).structuredContent
        r["well"] = (await _call(session, "wellness", from_date="2026-01-01", to_date="2026-12-31")).structuredContent
        return r
    r = _run(data_dir, coro)
    # list_activities: 10 rows unless asked, the longest first, and a note saying more matched.
    assert len(r["list"]["activities"]) == 10 and r["list"]["total"] == 60
    assert r["list"]["truncated"] and "Showing 10 of 60" in r["list"]["note"]
    assert r["list"]["activities"][0]["distance_km"] == 60.0
    assert [a["distance_km"] for a in r["top5"]["activities"]] == [60.0, 59.0, 58.0, 57.0, 56.0]
    # totals: 60 days is over the 50-row cap, and that's flagged rather than silent.
    assert len(r["days"]["totals"]) == 50 and r["days"]["truncated"] and "note" in r["days"]
    # Sorted by elevation, the biggest day comes first, and ranked days are the top 10 unless asked.
    assert r["climb"]["totals"][0]["elev_gain_m"] == 59.0 and len(r["climb"]["totals"]) == 10
    assert r["climb"]["truncated"] and "top 10 days by elevation" in r["climb"]["note"]
    assert [d["elev_gain_m"] for d in r["climb3"]["totals"]] == [59.0, 58.0, 57.0]
    assert r["weeks"]["truncated"] is False and "note" not in r["weeks"]
    # breakdown by week: one row per week and type, in one call.
    assert r["byweek"]["truncated"] is False and all("period" in g for g in r["byweek"]["groups"])
    assert sum(g["activities"] for g in r["byweek"]["groups"]) == 60
    assert r["byday"]["truncated"] is True
    # wellness: 60 days, the newest 50 shown and the cut flagged.
    assert len(r["well"]["days"]) == 50 and r["well"]["truncated"]


def test_load_balance_tool_answers(data_dir):
    import json

    conn = db.connect(data_dir)
    db.migrate(conn)
    dto = {"monthlyLoadAerobicLow": 75, "monthlyLoadAerobicLowTargetMin": 157, "monthlyLoadAerobicLowTargetMax": 367,
           "monthlyLoadAerobicHigh": 686, "monthlyLoadAerobicHighTargetMin": 226,
           "monthlyLoadAerobicHighTargetMax": 435,
           "monthlyLoadAnaerobic": 37, "monthlyLoadAnaerobicTargetMin": 69, "monthlyLoadAnaerobicTargetMax": 209,
           "trainingBalanceFeedbackPhrase": "AEROBIC_LOW_SHORTAGE"}
    raw = json.dumps({"mostRecentTrainingLoadBalance": {"metricsTrainingLoadBalanceDTOMap": {"dev1": dto}}})
    conn.execute("INSERT INTO garmin_raw (endpoint, key, raw_json, fetched_utc) VALUES "
                 "('get_training_status', '2026-09-20', ?, 'x')", (raw,))
    conn.commit()
    conn.close()

    async def coro(session):
        return (await _call(session, "load_balance", from_date="2026-09-01",
                            to_date="2026-09-30")).structuredContent
    out = _run(data_dir, coro)
    assert out["months"][0]["month"] == "2026-09"
    assert out["months"][0]["aerobic_low"]["status"] == "below"


def test_add_note_writes_only_notes(archive):
    async def coro(session):
        return (await _call(session, "add_note", target="activity", ref="garmin:1",
                            text="Felt strong")).structuredContent
    before = _table_counts(archive)
    out = _run(archive, coro)
    assert out["note_id"] == 1
    after = _table_counts(archive)
    assert after["notes"] == before["notes"] + 1
    # No other table changed.
    for t, n in before.items():
        if t != "notes":
            assert after[t] == n
    note = _query(archive, "SELECT text, author FROM notes")
    assert note == [("Felt strong", "mcp")]


def test_query_sql_rejects_writes_and_runs_selects(archive):
    async def coro(session):
        bad = (await _call(session, "query_sql", sql="UPDATE activities SET name='x'")).structuredContent
        assert "error" in bad
        bad2 = (await _call(session, "query_sql",
                            sql="SELECT 1; DROP TABLE activities")).structuredContent
        assert "error" in bad2
        good = (await _call(session, "query_sql",
                            sql="SELECT COUNT(*) AS n FROM activities")).structuredContent
        assert good["rows"][0]["n"] == 1 and good["truncated"] is False
    _run(archive, coro, allow_sql=True)
    # The write was refused: the row is unchanged.
    assert _query(archive, "SELECT name FROM activities") == [("Morning run",)]


def test_query_sql_absent_without_flag(archive):
    assert "query_sql" not in _tool_names(archive)


def test_http_rejects_foreign_host_accepts_local(archive):
    """Over HTTP the DNS-rebinding layer refuses a non-local Host and allows a local one."""
    from starlette.requests import HTTPConnection
    from mcp.server.transport_security import TransportSecurityMiddleware

    mw = TransportSecurityMiddleware(settings=mcp_server.transport_security(8766))

    def conn(host):
        return HTTPConnection({"type": "http",
                               "headers": [(b"host", host.encode()), (b"content-type", b"application/json")]})

    foreign = asyncio.run(mw.validate_request(conn("evil.example.com"), is_post=True))
    local = asyncio.run(mw.validate_request(conn("127.0.0.1:8766"), is_post=True))
    assert foreign is not None and foreign.status_code == 421
    assert local is None


# --- helpers -------------------------------------------------------------------------------------------

_TABLES = ("activities", "activity_sources", "daily_wellness", "weigh_ins", "garmin_activities",
           "strava_activities", "source_activities", "notes")


def _table_counts(data_dir):
    conn = sqlite3.connect(data_dir / "archive.db")
    try:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in _TABLES}
    finally:
        conn.close()


def _query(data_dir, sql):
    conn = sqlite3.connect(data_dir / "archive.db")
    try:
        return [tuple(r) for r in conn.execute(sql)]
    finally:
        conn.close()
