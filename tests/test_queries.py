"""Read-only query functions against a made-up archive. All values invented."""

import sqlite3
from datetime import date, timedelta

import pytest

from minou_sync import db, queries
from minou_sync.queries import MAX_ROWS


def _activity(conn, key, start_local, type_="run", name="Run", elapsed_s=3600, distance_m=10000.0,
              avg_hr=150, avg_watts=None, elev=100.0, source="garmin"):
    conn.execute(
        "INSERT INTO activities (key, source, activity_type, name, start_utc, start_local, elapsed_s, distance_m, "
        "elev_gain_m, avg_hr, avg_watts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (key, source, type_, name, start_local + "Z", start_local, elapsed_s, distance_m, elev, avg_hr, avg_watts))


def _wellness(conn, d, **vals):
    cols = {"date": d, "updated_utc": "2026-10-19T00:00:00Z", **vals}
    conn.execute(f"INSERT INTO daily_wellness ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                 list(cols.values()))


@pytest.fixture
def archive(conn, data_dir):
    _activity(conn, "garmin:1", "2026-10-01T06:00:00", type_="run", distance_m=10000.0, elapsed_s=3600, elev=100.0)
    _activity(conn, "garmin:2", "2026-10-03T07:00:00", type_="ride", distance_m=40000.0, elapsed_s=5400, elev=300.0,
              avg_watts=200.0)
    _activity(conn, "garmin:3", "2026-10-08T06:00:00", type_="run", distance_m=21000.0, elapsed_s=7200, elev=250.0,
              name="Long run")
    _activity(conn, "strava:9", "2024-05-01T06:00:00", type_="run", distance_m=5000.0, elapsed_s=1800, source="strava")
    conn.execute("INSERT INTO activity_sources (key, source, source_id) VALUES ('garmin:1', 'garmin', '1')")
    conn.execute("INSERT INTO meta (key, value) VALUES ('last_sync_utc', '2026-10-08T20:00:00Z')")
    _wellness(conn, "2026-10-01", resting_hr=48, hrv_avg_ms=55.0, sleep_s=25200, sleep_score=80, steps=9000)
    _wellness(conn, "2026-10-02", resting_hr=50, sleep_s=21600, steps=8000)
    conn.commit()
    return conn, data_dir


def test_archive_summary(archive):
    conn, _ = archive
    s = queries.archive_summary(conn)
    assert s["activities_total"] == 4
    assert s["activities_by_type"]["run"] == 3 and s["activities_by_type"]["ride"] == 1
    assert s["first_activity_date"] == "2024-05-01" and s["last_activity_date"] == "2026-10-08"
    assert s["last_wellness_date"] == "2026-10-02" and s["wellness_days"] == 2
    assert s["last_sync_utc"] == "2026-10-08T20:00:00Z"
    assert "run" in s["activity_types"]


def test_list_activities_filters(archive):
    conn, _ = archive
    assert queries.list_activities(conn).total == 4
    assert queries.list_activities(conn, activity_type="run").total == 3
    assert queries.list_activities(conn, from_date="2026-10-01", to_date="2026-10-05").total == 2
    assert queries.list_activities(conn, q="Long").rows[0]["key"] == "garmin:3"
    # Newest first, units in field names.
    first = queries.list_activities(conn).rows[0]
    assert first["key"] == "garmin:3" and first["distance_km"] == 21.0 and first["duration_h"] == 2.0


def test_list_activities_sort(archive):
    conn, _ = archive
    by_distance = queries.list_activities(conn, sort="distance").rows
    assert by_distance[0]["key"] == "garmin:2"  # 40 km ride
    oldest = queries.list_activities(conn, sort="start_asc").rows[0]
    assert oldest["key"] == "strava:9"


def test_get_activity_with_sources_and_notes(archive):
    conn, _ = archive
    conn.execute("INSERT INTO notes (target, ref, text, author, created_utc) "
                 "VALUES ('activity', 'garmin:1', 'Felt easy', 'claude-code', '2026-10-01T20:00:00Z')")
    conn.commit()
    a = queries.get_activity(conn, "garmin:1")
    assert a["sources"] == ["garmin:1"] and a["notes"][0]["text"] == "Felt easy"
    assert a["distance_km"] == 10.0
    assert queries.get_activity(conn, "nope") is None


def test_totals_week_and_month_against_hand_sums(archive):
    conn, _ = archive
    weeks = {w["period"]: w for w in queries.totals(conn, period="week")}
    # 2026-10-01 (Thu) and 2026-10-03 (Sat) are the same ISO-%W week (W39); 10-08 is W40.
    w39 = next(w for p, w in weeks.items() if w["activities"] == 2)
    assert w39["hours"] == round((3600 + 5400) / 3600, 1)
    assert w39["distance_km"] == round((10000 + 40000) / 1000, 1)
    assert w39["elev_gain_m"] == 400

    months = {m["period"]: m for m in queries.totals(conn, period="month")}
    assert months["2026-10"]["activities"] == 3
    assert months["2026-10"]["distance_km"] == round((10000 + 40000 + 21000) / 1000, 1)
    assert months["2024-05"]["activities"] == 1


def test_totals_filtered_by_type(archive):
    conn, _ = archive
    runs = {m["period"]: m for m in queries.totals(conn, period="month", activity_type="run")}
    assert runs["2026-10"]["activities"] == 2  # two runs in October, the ride excluded


def test_totals_type_family_wildcard(conn, data_dir):
    # A trailing * matches every subtype in the family, exact match does not.
    _activity(conn, "g:1", "2026-10-01T06:00:00", type_="run", distance_m=10000.0, elev=100.0)
    _activity(conn, "g:2", "2026-10-02T06:00:00", type_="run_trail", distance_m=20000.0, elev=500.0)
    _activity(conn, "g:3", "2026-10-03T06:00:00", type_="run_treadmill", distance_m=5000.0, elev=0.0)
    _activity(conn, "g:4", "2026-10-04T06:00:00", type_="ride", distance_m=40000.0, elev=300.0)
    conn.commit()
    exact = queries.totals(conn, period="month", activity_type="run")
    assert exact[0]["activities"] == 1 and exact[0]["distance_km"] == 10.0
    fam = queries.totals(conn, period="month", activity_type="run*")
    assert fam[0]["activities"] == 3  # all three run subtypes, ride excluded
    assert fam[0]["distance_km"] == round((10000 + 20000 + 5000) / 1000, 1)
    assert fam[0]["elev_gain_m"] == 600


def test_totals_grand_total(conn, data_dir):
    # period="total" returns one row summing the whole range, not one per period.
    _activity(conn, "g:1", "2026-01-15T06:00:00", type_="run", distance_m=10000.0, elev=100.0)
    _activity(conn, "g:2", "2026-03-20T06:00:00", type_="run_trail", distance_m=20000.0, elev=500.0)
    _activity(conn, "g:3", "2026-09-10T06:00:00", type_="run", distance_m=5000.0, elev=50.0)
    conn.commit()
    rows = queries.totals(conn, from_date="2026-01-01", to_date="2026-10-21",
                          period="total", activity_type="run*")
    assert len(rows) == 1
    r = rows[0]
    assert r["period"] == "total" and r["activities"] == 3
    assert r["distance_km"] == round((10000 + 20000 + 5000) / 1000, 1)
    assert r["elev_gain_m"] == 650


def test_breakdown_by_type(conn, data_dir):
    _activity(conn, "g:1", "2026-10-01T06:00:00", type_="run", distance_m=10000.0)
    _activity(conn, "g:2", "2026-10-02T06:00:00", type_="run", distance_m=12000.0)
    _activity(conn, "g:3", "2026-10-03T06:00:00", type_="ride", distance_m=40000.0)
    conn.commit()
    groups = {g["group"]: g for g in queries.breakdown(conn, group_by="type")}
    assert groups["run"]["activities"] == 2 and groups["run"]["distance_km"] == 22.0
    assert groups["ride"]["activities"] == 1
    # Busiest group first.
    assert queries.breakdown(conn, group_by="type")[0]["group"] == "run"


def test_totals_sort_puts_biggest_period_first(conn, data_dir):
    _activity(conn, "g:1", "2026-10-01T06:00:00", elev=100.0)
    _activity(conn, "g:2", "2026-10-02T06:00:00", elev=900.0)
    _activity(conn, "g:3", "2026-10-03T06:00:00", elev=300.0)
    conn.commit()
    assert queries.totals(conn, period="day")[0]["period"] == "2026-10-03"  # newest first by default
    by_climb = queries.totals(conn, period="day", sort="elevation")
    assert [d["period"] for d in by_climb] == ["2026-10-02", "2026-10-03", "2026-10-01"]


def test_breakdown_by_period_and_type(conn, data_dir):
    _activity(conn, "g:1", "2026-10-01T06:00:00", type_="run", distance_m=10000.0)
    _activity(conn, "g:2", "2026-10-02T06:00:00", type_="ride", distance_m=40000.0)
    _activity(conn, "g:3", "2026-11-03T06:00:00", type_="run", distance_m=8000.0)
    conn.commit()
    rows = queries.breakdown(conn, group_by="type", period="month")
    assert len(rows) == 3
    assert (rows[0]["period"], rows[0]["group"], rows[0]["distance_km"]) == ("2026-11", "run", 8.0)  # newest first
    oct_ = {r["group"]: r for r in rows if r["period"] == "2026-10"}
    assert oct_["run"]["activities"] == 1 and oct_["ride"]["distance_km"] == 40.0
    # The default stays one row per group, with no period key.
    assert all("period" not in r for r in queries.breakdown(conn, group_by="type"))


def test_breakdown_by_training_effect(conn, data_dir):
    # The label comes from Garmin's stored activity detail, joined on garmin_id.
    for key, gid, label in (("g:1", 101, "AEROBIC_BASE"), ("g:2", 102, "AEROBIC_BASE"), ("g:3", 103, "TEMPO")):
        _act_full(conn, key, "2026-10-01T06:00:00", type_="run", garmin_id=gid)
        conn.execute("INSERT INTO garmin_raw (endpoint, key, raw_json, fetched_utc) VALUES "
                     "('get_activity', ?, ?, '2026-10-19T00:00:00Z')",
                     (str(gid), f'{{"summaryDTO": {{"trainingEffectLabel": "{label}"}}}}'))
    conn.commit()
    groups = {g["group"]: g for g in queries.breakdown(conn, group_by="training_effect")}
    assert groups["aerobic base"]["activities"] == 2
    assert groups["tempo"]["activities"] == 1


def test_wellness_range(archive):
    conn, _ = archive
    rows = queries.wellness(conn, from_date="2026-10-01", to_date="2026-10-02")
    assert [r["date"] for r in rows] == ["2026-10-02", "2026-10-01"]  # newest first
    assert rows[1]["sleep_h"] == 7.0 and rows[1]["resting_hr"] == 48


def test_list_caps_at_50_rows_with_truncated(conn, data_dir):
    for i in range(MAX_ROWS + 10):
        _activity(conn, f"garmin:{i}", (date(2026, 1, 1) + timedelta(days=i)).isoformat() + "T06:00:00")
    conn.commit()
    page = queries.list_activities(conn, limit=200)
    assert len(page.rows) == MAX_ROWS and page.total == MAX_ROWS + 10 and page.truncated is True
    # The last page isn't truncated.
    last = queries.list_activities(conn, limit=MAX_ROWS, offset=MAX_ROWS)
    assert last.truncated is False


def test_connect_ro_is_read_only(archive):
    conn, data_dir = archive
    conn.close()
    ro = queries.connect_ro(data_dir / "archive.db")
    assert queries.archive_summary(ro)["activities_total"] == 4
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("UPDATE activities SET name = 'x'")
    ro.close()


# --- best_efforts, day totals, sorts, and the enrichment fields. All values invented. ---------

def _act_full(conn, key, start_local, type_="run", garmin_id=None, strava_id=None, source="garmin",
              check_reason=None, distance_m=10000.0, elapsed_s=2400, elev=100.0):
    # Satisfy the activities FK to the source tables.
    if garmin_id is not None:
        conn.execute("INSERT OR IGNORE INTO garmin_activities (garmin_id, activity_type, start_utc, origin, "
                     "raw_json, imported_utc) VALUES (?, 'running', ?, 'sync', '{}', 'x')",
                     (garmin_id, start_local + "Z"))
    if strava_id is not None:
        conn.execute("INSERT OR IGNORE INTO strava_activities (strava_id, activity_type, start_utc, raw_json, "
                     "imported_utc) VALUES (?, 'Run', ?, '{}', 'x')", (strava_id, start_local + "Z"))
    conn.execute(
        "INSERT INTO activities (key, source, activity_type, name, start_utc, start_local, elapsed_s, distance_m, "
        "elev_gain_m, garmin_id, strava_id, check_reason) VALUES (?, ?, ?, 'x', ?, ?, ?, ?, ?, ?, ?, ?)",
        (key, source, type_, start_local + "Z", start_local, elapsed_s, distance_m, elev, garmin_id, strava_id,
         check_reason))


def _source(conn, key, source, source_id):
    conn.execute("INSERT INTO activity_sources (key, source, source_id) VALUES (?, ?, ?)", (key, source, source_id))


def _effort(conn, source, source_id, effort, value):
    conn.execute("INSERT INTO activity_efforts (source, source_id, effort, value, computed_utc) "
                 "VALUES (?, ?, ?, ?, '2026-10-19T00:00:00Z')", (source, source_id, effort, value))


def test_best_efforts_from_activity_efforts(conn, data_dir):
    # Two runs with a 5K effort; the faster one (1200 s) wins.
    _act_full(conn, "strava:1", "2024-05-01T06:00:00", source="strava", strava_id=1)
    _act_full(conn, "strava:2", "2024-06-01T06:00:00", source="strava", strava_id=2)
    _source(conn, "strava:1", "strava", "1")
    _source(conn, "strava:2", "strava", "2")
    _effort(conn, "strava", "1", "run_5k", 1300.0)
    _effort(conn, "strava", "2", "run_5k", 1200.0)
    conn.commit()
    out = queries.best_efforts(conn, "run")
    fivek = next(e for e in out["efforts"] if e["effort"] == "run_5k")
    assert fivek["value"] == 1200.0 and fivek["activity_key"] == "strava:2" and fivek["from"] == "file"
    assert fivek["unit"] == "seconds" and fivek["time"] == "20:00"  # h:mm:ss, so no model converts seconds
    assert (fivek["activity_name"], fivek["activity_type"]) == ("x", "run")  # which activity set it
    # An effort with no result still has the keys, empty.
    marathon = next(e for e in out["efforts"] if e["effort"] == "run_marathon")
    assert marathon["activity_key"] is None and marathon["activity_name"] is None


def test_best_efforts_excludes_a_flagged_activity(conn, data_dir):
    # The faster 5K is on a flagged activity, so the slower, clean one is reported.
    _act_full(conn, "garmin:1", "2026-04-10T06:00:00", garmin_id=1, check_reason="average 30.7 km/h for a run (limit 25)")
    _act_full(conn, "garmin:2", "2026-05-01T06:00:00", garmin_id=2)
    _source(conn, "garmin:1", "garmin", "1")
    _source(conn, "garmin:2", "garmin", "2")
    _effort(conn, "garmin", "1", "run_5k", 900.0)   # implausibly fast, on the flagged activity
    _effort(conn, "garmin", "2", "run_5k", 1250.0)
    conn.commit()
    fivek = next(e for e in queries.best_efforts(conn, "run")["efforts"] if e["effort"] == "run_5k")
    assert fivek["value"] == 1250.0 and fivek["activity_key"] == "garmin:2"


def test_best_efforts_falls_back_to_garmin_splits(conn, data_dir):
    import json
    # A Garmin run with no activity_efforts row, but Garmin's splitSummaries carry a fastest 5 km.
    _act_full(conn, "garmin:1", "2026-05-01T06:00:00", garmin_id=1)
    _source(conn, "garmin:1", "garmin", "1")
    detail = {"splitSummaries": [{"splitType": "DISTANCE_5000", "distance": 5000, "duration": 1333.0}]}
    conn.execute("INSERT INTO garmin_raw (endpoint, key, raw_json, fetched_utc) VALUES "
                 "('get_activity', '1', ?, 'x')", (json.dumps(detail),))
    conn.commit()
    fivek = next(e for e in queries.best_efforts(conn, "run")["efforts"] if e["effort"] == "run_5k")
    assert fivek["value"] == 1333.0 and fivek["from"] == "garmin_split" and fivek["activity_key"] == "garmin:1"


def test_best_efforts_names_a_garmin_record_set_by_a_flagged_activity(conn, data_dir):
    import json
    _act_full(conn, "garmin:99", "2026-04-10T06:00:00", garmin_id=99,
              check_reason="average 30.7 km/h for a run (limit 25)")
    conn.commit()
    prs = [{"typeId": 3, "value": 900.0, "activityId": 99, "prStartTimeGmtFormatted": "2026-04-10T20:22:29.0"},
           {"typeId": 4, "value": 2500.0, "activityId": 77, "prStartTimeGmtFormatted": "2026-05-01T07:00:00.0"}]
    conn.execute("INSERT INTO garmin_raw (endpoint, key, raw_json, fetched_utc) VALUES "
                 "('get_personal_record', '', ?, 'x')", (json.dumps(prs),))
    conn.commit()
    records = queries.best_efforts(conn, "run")["garmin_records"]
    by_act = {r["activity_id"]: r for r in records}
    assert by_act[99]["set_by_flagged_activity"] is True
    assert by_act[77]["set_by_flagged_activity"] is False
    # Each record names the activity that set it, when that activity is in the archive.
    assert (by_act[99]["activity_name"], by_act[99]["activity_type"]) == ("x", "run")
    assert by_act[77]["activity_name"] is None


def test_best_efforts_ride_power(conn, data_dir):
    _act_full(conn, "garmin:1", "2026-05-01T06:00:00", type_="ride", garmin_id=1, distance_m=40000.0)
    _source(conn, "garmin:1", "garmin", "1")
    _effort(conn, "garmin", "1", "power_20min", 255.0)
    conn.commit()
    out = queries.best_efforts(conn, "ride")
    p20 = next(e for e in out["efforts"] if e["effort"] == "power_20min")
    assert p20["value"] == 255.0 and p20["unit"] == "watts"


def test_best_efforts_ride_longest_and_most_climbing(conn, data_dir):
    _act_full(conn, "garmin:1", "2026-05-01T06:00:00", type_="ride", garmin_id=1, distance_m=40000.0, elev=200.0)
    _act_full(conn, "garmin:2", "2026-06-01T06:00:00", type_="ride", garmin_id=2, distance_m=120000.0, elev=1500.0)
    # A flagged ride with a huge distance must not win.
    _act_full(conn, "garmin:3", "2026-07-01T06:00:00", type_="ride", garmin_id=3, distance_m=900000.0, elev=9000.0,
              check_reason="average 120 km/h for a ride (limit 60)")
    conn.commit()
    out = queries.best_efforts(conn, "ride")
    assert out["longest_ride"]["value"] == 120.0 and out["longest_ride"]["activity_key"] == "garmin:2"
    assert out["most_climbing_ride"]["value"] == 1500.0 and out["most_climbing_ride"]["activity_key"] == "garmin:2"
    assert (out["longest_ride"]["activity_name"], out["longest_ride"]["activity_type"]) == ("x", "ride")


def test_totals_by_day(conn, data_dir):
    _activity(conn, "garmin:1", "2026-10-01T06:00:00", distance_m=10000.0)
    _activity(conn, "garmin:2", "2026-10-01T18:00:00", distance_m=5000.0)
    _activity(conn, "garmin:3", "2026-10-02T06:00:00", distance_m=8000.0)
    conn.commit()
    days = {d["period"]: d for d in queries.totals(conn, period="day")}
    assert days["2026-10-01"]["activities"] == 2 and days["2026-10-01"]["distance_km"] == 15.0
    assert days["2026-10-02"]["activities"] == 1


def test_list_sort_by_elevation(conn, data_dir):
    _activity(conn, "garmin:1", "2026-10-01T06:00:00", elev=100.0)
    _activity(conn, "garmin:2", "2026-10-02T06:00:00", elev=800.0)
    _activity(conn, "garmin:3", "2026-10-03T06:00:00", elev=300.0)
    conn.commit()
    rows = queries.list_activities(conn, sort="elevation").rows
    assert rows[0]["key"] == "garmin:2"


def test_list_rows_carry_training_label_and_load(conn, data_dir):
    import json
    _act_full(conn, "garmin:1", "2026-10-01T06:00:00", garmin_id=1)
    detail = {"summaryDTO": {"trainingEffectLabel": "LACTATE_THRESHOLD", "activityTrainingLoad": 172.7}}
    conn.execute("INSERT INTO garmin_raw (endpoint, key, raw_json, fetched_utc) VALUES "
                 "('get_activity', '1', ?, 'x')", (json.dumps(detail),))
    conn.commit()
    row = queries.list_activities(conn).rows[0]
    assert row["training_effect_label"] == "lactate threshold" and row["training_load"] == 173


def test_wellness_carries_acute_and_chronic_load(conn, data_dir):
    import json
    _wellness(conn, "2026-09-12", resting_hr=48)
    status = {"mostRecentTrainingStatus": {"latestTrainingStatusData": {"3499876809": {
        "acuteTrainingLoadDTO": {"dailyTrainingLoadAcute": 557, "dailyTrainingLoadChronic": 859,
                                 "dailyAcuteChronicWorkloadRatio": 0.6}}}}}
    conn.execute("INSERT INTO garmin_raw (endpoint, key, raw_json, fetched_utc) VALUES "
                 "('get_training_status', '2026-09-12', ?, 'x')", (json.dumps(status),))
    conn.commit()
    row = queries.wellness(conn, from_date="2026-09-12", to_date="2026-09-12")[0]
    assert row["acute_load"] == 557 and row["chronic_load"] == 859 and row["acute_chronic_ratio"] == 0.6


def test_list_sort_by_load_and_elevation_on_rows(conn, data_dir):
    """sort='load' ranks by Garmin's activity training load, hardest first, NULLs last; rows carry elev_gain_m."""
    import json
    _act_full(conn, "garmin:1", "2026-10-01T06:00:00", garmin_id=1, elev=120.0)
    _act_full(conn, "garmin:2", "2026-10-02T06:00:00", garmin_id=2, elev=800.0)
    _act_full(conn, "garmin:3", "2026-10-03T06:00:00", garmin_id=3, elev=50.0)  # no detail -> NULL load
    for gid, load in ((1, 200.0), (2, 500.0)):
        detail = {"summaryDTO": {"activityTrainingLoad": load}}
        conn.execute("INSERT INTO garmin_raw (endpoint, key, raw_json, fetched_utc) VALUES "
                     "('get_activity', ?, ?, 'x')", (str(gid), json.dumps(detail)))
    conn.commit()
    rows = queries.list_activities(conn, sort="load").rows
    assert [r["key"] for r in rows] == ["garmin:2", "garmin:1", "garmin:3"]  # 500, 200, NULL last
    assert rows[0]["elev_gain_m"] == 800 and rows[1]["elev_gain_m"] == 120


def test_load_balance_monthly_split_with_targets(conn, data_dir):
    """One row per month from the latest training-status day, with each bucket's status against its target."""
    import json

    def status_json(low, high, anaerobic):
        dto = {
            "monthlyLoadAerobicLow": low, "monthlyLoadAerobicLowTargetMin": 157, "monthlyLoadAerobicLowTargetMax": 367,
            "monthlyLoadAerobicHigh": high, "monthlyLoadAerobicHighTargetMin": 226,
            "monthlyLoadAerobicHighTargetMax": 435,
            "monthlyLoadAnaerobic": anaerobic, "monthlyLoadAnaerobicTargetMin": 69,
            "monthlyLoadAnaerobicTargetMax": 209,
            "trainingBalanceFeedbackPhrase": "AEROBIC_LOW_SHORTAGE",
        }
        return json.dumps({"mostRecentTrainingLoadBalance": {"metricsTrainingLoadBalanceDTOMap": {"dev1": dto}}})

    # September: two days; the later day (09-20) must win over the earlier (09-05).
    conn.execute("INSERT INTO garmin_raw (endpoint, key, raw_json, fetched_utc) VALUES "
                 "('get_training_status', '2026-09-05', ?, 'x')", (status_json(10, 10, 10),))
    conn.execute("INSERT INTO garmin_raw (endpoint, key, raw_json, fetched_utc) VALUES "
                 "('get_training_status', '2026-09-20', ?, 'x')", (status_json(75, 686, 37),))
    conn.commit()
    months = queries.load_balance(conn, from_date="2026-09-01", to_date="2026-09-30")
    assert len(months) == 1
    m = months[0]
    assert m["month"] == "2026-09" and m["feedback"] == "AEROBIC_LOW_SHORTAGE"
    assert m["aerobic_low"]["load"] == 75 and m["aerobic_low"]["status"] == "below"   # 75 < 157
    assert m["aerobic_high"]["load"] == 686 and m["aerobic_high"]["status"] == "above"  # 686 > 435
    assert m["anaerobic"]["load"] == 37 and m["anaerobic"]["status"] == "below"  # 37 < 69
