"""Sync against a fake Garmin API returning made-up responses in the shapes garminconnect 0.3.2 produces."""

import hashlib
import io
import zipfile
from datetime import date, timedelta

import pytest
from garminconnect import GarminConnectConnectionError

from minou_sync import cli, garmin, reconcile, sync
from minou_sync.garmin import READ_ONLY_CALLS, GarminClient

TODAY = date(2026, 10, 19)


@pytest.fixture(autouse=True)
def online(monkeypatch):
    # The CLI checks Garmin is reachable before logging in; tests never touch the network.
    monkeypatch.setattr(garmin, "reachable", lambda: True)


def fake_activity(activity_id, day, hour=7, **extra):
    raw = {
        "activityId": activity_id,
        "activityName": "Indoor ride",
        "startTimeLocal": f"{day} {hour + 2:02d}:00:00",
        "startTimeGMT": f"{day} {hour:02d}:00:00",
        "activityType": {"typeId": 25, "typeKey": "indoor_cycling", "parentTypeId": 2},
        "distance": 20000.0,
        "duration": 3601.4,
        "elapsedDuration": 3660.6,
        "movingDuration": 3500.2,
        "calories": 600.0,
        "averageHR": 131.6,
        "maxHR": 160.0,
        "averageBikingCadenceInRevPerMinute": 85.0,
        "avgPower": 180.0,
        "maxPower": 400.0,
        "normPower": 190.0,
        "aerobicTrainingEffect": 3.1,
        "anaerobicTrainingEffect": 0.8,
        "activityTrainingLoad": 95.0,
        "manualActivity": False,
    }
    raw.update(extra)
    return raw


def fake_sleep(day, empty=False):
    if empty:
        return {"dailySleepDTO": {"calendarDate": day, "sleepTimeSeconds": None}, "sleepLevels": None}
    return {
        "dailySleepDTO": {
            "calendarDate": day, "sleepTimeSeconds": 25000, "deepSleepSeconds": 5000, "lightSleepSeconds": 14000,
            "remSleepSeconds": 5000, "awakeSleepSeconds": 1000, "sleepScores": {"overall": {"value": 77}},
        },
        "avgOvernightHrv": 55.0,
        "hrvStatus": "BALANCED",
        "restingHeartRate": 49,
    }


def fake_stats(day):
    return {"calendarDate": day, "restingHeartRate": 48, "totalSteps": 9000, "averageStressLevel": 30,
            "bodyBatteryHighestValue": 80, "bodyBatteryLowestValue": 20}


def fit_zip(activity_id, content=b"fake fit bytes"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{activity_id}_ACTIVITY.fit", content)
    return buf.getvalue()


class FakeApi:
    """Answers the read-only calls sync uses; records every call."""

    def __init__(self, activities=(), weigh_ins=(), empty_sleep=(), failing=()):
        self.activities = list(activities)
        self.weigh_ins = list(weigh_ins)
        self.empty_sleep = set(empty_sleep)
        self.failing = set(failing)
        self.details: dict[int, dict] = {}
        self.calls: list[tuple] = []

    def _record(self, name, *args):
        self.calls.append((name, *args))

    def get_activities(self, start, limit):
        self._record("get_activities", start, limit)
        newest_first = sorted(self.activities, key=lambda a: a["startTimeGMT"], reverse=True)
        return newest_first[start:start + limit]

    def get_activities_by_date(self, start, end):
        self._record("get_activities_by_date", start, end)
        return [a for a in self.activities if start <= a["startTimeLocal"][:10] <= end]

    def download_activity(self, activity_id, fmt):
        self._record("download_activity", activity_id)
        return fit_zip(activity_id)

    def get_sleep_data(self, d):
        self._record("get_sleep_data", d)
        return fake_sleep(d, empty=d in self.empty_sleep)

    def get_user_summary(self, d):
        self._record("get_user_summary", d)
        return fake_stats(d)

    def get_training_readiness(self, d):
        self._record("get_training_readiness", d)
        return [{"calendarDate": d, "timestamp": f"{d}T06:00:00.0", "score": 40},
                {"calendarDate": d, "timestamp": f"{d}T18:00:00.0", "score": 65},
                {"calendarDate": d, "timestamp": f"{d}T12:00:00.0", "score": 50}]

    def get_activity(self, activity_id):
        self._record("get_activity", activity_id)
        return self.details.get(activity_id, {"activityId": activity_id, "summaryDTO": {}, "metadataDTO": {}})

    def get_body_composition(self, start, end):
        self._record("get_body_composition", start, end)
        return {"startDate": start, "endDate": end, "dateWeightList": self.weigh_ins}

    def __getattr__(self, name):
        # Every other allowlisted endpoint answers with a small made-up payload.
        if name not in READ_ONLY_CALLS:
            raise AttributeError(name)

        def endpoint(*args):
            self._record(name, *args)
            if name in self.failing:
                raise GarminConnectConnectionError(f"{name} failed")
            return {"endpoint": name}
        return endpoint


def client_for(api, max_requests=500):
    return GarminClient(api, min_interval_s=0, max_requests=max_requests, sleep=lambda s: None)


def called(api, name):
    return [c for c in api.calls if c[0] == name]


def test_activity_row_maps_fields_and_times():
    row = sync.activity_row(fake_activity(123, "2026-10-18"), "2026-10-19T00:00:00Z")
    assert row["garmin_id"] == 123
    assert row["activity_type"] == "indoor_cycling"
    assert row["start_utc"] == "2026-10-18T07:00:00Z"
    assert row["start_local"] == "2026-10-18T09:00:00"
    assert row["utc_offset_min"] == 120
    assert (row["elapsed_s"], row["moving_s"], row["avg_hr"]) == (3661, 3500, 132)
    assert (row["avg_watts"], row["norm_watts"], row["avg_cadence"]) == (180.0, 190.0, 85.0)
    assert row["origin"] == "sync"


def test_activity_row_without_power_or_bike_cadence():
    raw = fake_activity(124, "2026-10-18", activityType={"typeKey": "pilates"})
    for key in ("avgPower", "maxPower", "normPower", "averageBikingCadenceInRevPerMinute", "distance"):
        del raw[key]
    raw["averageRunningCadenceInStepsPerMinute"] = 160.0
    row = sync.activity_row(raw, "2026-10-19T00:00:00Z")
    assert row["avg_watts"] is None and row["distance_m"] is None and row["elev_gain_m"] is None
    assert row["avg_cadence"] == 160.0


def test_wellness_values_pick_latest_readiness_and_drop_negative_stress():
    stats = fake_stats("2026-10-18") | {"averageStressLevel": -1, "bodyBatteryLowestValue": -2}
    v = sync.wellness_values(fake_sleep("2026-10-18"), stats,
                             FakeApi().get_training_readiness("2026-10-18"))
    assert v["readiness_score"] == 65
    assert v["stress_avg"] is None and v["body_battery_low"] is None
    assert (v["resting_hr"], v["hrv_avg_ms"], v["hrv_status"], v["sleep_score"]) == (48, 55.0, "BALANCED", 77)


def test_wellness_values_tolerate_empty_responses():
    v = sync.wellness_values(fake_sleep("2026-10-18", empty=True), {}, [])
    assert all(value is None for value in v.values())


def test_weigh_in_converts_grams_to_kg():
    row = sync.weigh_in_row({"weight": 72500.0, "muscleMass": 33000, "timestampGMT": 1760770800000})
    assert row == {"measured_utc": "2025-10-18T07:00:00Z", "weight_kg": 72.5, "body_fat_pct": None,
                   "muscle_mass_kg": 33.0, "body_water_pct": None, "source": "garmin"}


def test_first_run_fetches_14_days_files_and_wellness(conn, data_dir):
    api = FakeApi(
        activities=[fake_activity(1, "2026-10-07"), fake_activity(2, "2026-10-18"),
                    fake_activity(3, "2026-10-01")],  # outside the 14-day window
        weigh_ins=[{"weight": 72500.0, "timestampGMT": 1760770800000}],
    )
    counts = sync.run(conn, client_for(api), data_dir, days=None, today=TODAY)

    assert (counts.first_day, counts.last_day) == (date(2026, 10, 6), TODAY)
    assert len(called(api, "get_activities_by_date")) == 14
    assert counts.activities["added"] == 2 and counts.files == 2
    assert counts.stopped is None
    ids = [r[0] for r in conn.execute("SELECT garmin_id FROM garmin_activities ORDER BY garmin_id")]
    assert ids == [1, 2]

    f = conn.execute("SELECT * FROM files WHERE source_id = 2").fetchone()
    content = (data_dir / f["path"]).read_bytes()
    assert f["path"] == "files/garmin/2.fit" and f["format"] == "fit"
    assert f["sha256"] == hashlib.sha256(content).hexdigest() and f["bytes"] == len(content)

    days = conn.execute("SELECT date, complete, readiness_score FROM daily_wellness ORDER BY date").fetchall()
    assert len(days) == 14
    assert days[-1]["date"] == "2026-10-19" and days[-1]["complete"] == 0
    assert all(d["complete"] == 1 for d in days[:-1])
    assert conn.execute("SELECT weight_kg FROM weigh_ins").fetchone()[0] == 72.5
    assert conn.execute("SELECT value FROM meta WHERE key = 'last_sync_utc'").fetchone() is not None

    # Every daily endpoint is stored whole for every day, plus account snapshots and activity details.
    endpoints = {}
    for endpoint, key in conn.execute("SELECT endpoint, key FROM garmin_raw"):
        endpoints.setdefault(endpoint, []).append(key)
    assert all(len(endpoints[e]) == 14 for e in sync.DAILY_ENDPOINTS)
    assert all(endpoints[e] == [""] for e in sync.SNAPSHOT_ENDPOINTS)
    assert all(endpoints[e] == ["2026-10-07"] for e in sync.RANGE_ENDPOINTS)  # window from the first activity
    assert {r[0] for r in conn.execute("SELECT endpoint FROM garmin_raw WHERE key = '2'")} == set(sync.ACTIVITY_ENDPOINTS)
    sleep = conn.execute("SELECT raw_json FROM garmin_raw WHERE endpoint = 'get_sleep_data' AND key = '2026-10-18'")
    assert '"avgOvernightHrv": 55.0' in sleep.fetchone()[0]


def test_failing_endpoint_is_skipped_and_the_day_still_saved(conn, data_dir, caplog):
    api = FakeApi(failing={"get_training_status"})
    counts = sync.run(conn, client_for(api), data_dir, days=2, today=TODAY)
    assert counts.stopped is None and counts.wellness_days == 2
    assert not conn.execute("SELECT 1 FROM garmin_raw WHERE endpoint = 'get_training_status'").fetchone()
    assert "get_training_status" in caplog.text


def test_exercise_sets_fetched_only_when_the_summary_has_sets(conn, data_dir):
    api = FakeApi(activities=[fake_activity(1, "2026-10-18"), fake_activity(2, "2026-10-19", totalSets=12)])
    sync.run(conn, client_for(api), data_dir, days=2, today=TODAY)
    assert [c[1] for c in called(api, "get_activity_exercise_sets")] == [2]


def test_only_read_only_calls_are_made(conn, data_dir):
    api = FakeApi(activities=[fake_activity(1, "2026-10-18")])
    sync.run(conn, client_for(api), data_dir, days=3, today=TODAY)
    assert {c[0] for c in api.calls} <= READ_ONLY_CALLS


def test_second_run_resumes_and_doesnt_redownload(conn, data_dir):
    api = FakeApi(activities=[fake_activity(1, "2026-10-17"), fake_activity(2, "2026-10-18")])
    sync.run(conn, client_for(api), data_dir, days=None, today=date(2026, 10, 18))

    api.calls.clear()
    api.activities.append(fake_activity(3, "2026-10-18", hour=17))  # recorded later on the same day
    counts = sync.run(conn, client_for(api), data_dir, days=None, today=TODAY)

    # Activities restart on the latest stored day; wellness restarts after the last complete day.
    assert [c[1] for c in called(api, "get_activities_by_date")] == ["2026-10-18", "2026-10-19"]
    assert [c[1] for c in called(api, "get_sleep_data")] == ["2026-10-18", "2026-10-19"]
    assert [c[1] for c in called(api, "download_activity")] == [3]
    assert counts.activities == {"added": 1, "updated": 0, "unchanged": 1}


def test_today_is_partial_and_refetched_complete_day_only_fills_gaps(conn, data_dir):
    sync.run(conn, client_for(FakeApi(empty_sleep={"2026-10-19"})), data_dir, days=2, today=TODAY)
    row = conn.execute("SELECT * FROM daily_wellness WHERE date = '2026-10-19'").fetchone()
    assert row["complete"] == 0 and row["sleep_s"] is None and row["steps"] == 9000

    # Next day: the partial day is overwritten with fresh values and becomes complete.
    conn.execute("UPDATE daily_wellness SET steps = 1 WHERE date IN ('2026-10-18', '2026-10-19')")
    conn.commit()
    sync.run(conn, client_for(FakeApi()), data_dir, days=2, today=TODAY + timedelta(days=1))
    partial = conn.execute("SELECT * FROM daily_wellness WHERE date = '2026-10-19'").fetchone()
    assert partial["complete"] == 1 and partial["sleep_s"] == 25000 and partial["steps"] == 9000

    sync.run(conn, client_for(FakeApi()), data_dir, days=3, today=TODAY + timedelta(days=1))
    finished = conn.execute("SELECT steps FROM daily_wellness WHERE date = '2026-10-18'").fetchone()
    assert finished["steps"] == 1  # complete days keep their values


def test_manual_activity_isnt_downloaded(conn, data_dir):
    api = FakeApi(activities=[fake_activity(1, "2026-10-18", manualActivity=True)])
    counts = sync.run(conn, client_for(api), data_dir, days=2, today=TODAY)
    assert counts.activities["added"] == 1 and not called(api, "download_activity")


def test_request_cap_keeps_finished_days_and_reports_resume_day(conn, data_dir):
    api = FakeApi(activities=[fake_activity(1, "2026-10-17")])
    # 1 body composition + 17 for 10-16 = 18, then 10-17 needs 20 (with the activity's 3).
    counts = sync.run(conn, client_for(api, max_requests=30), data_dir, days=4, today=TODAY)
    assert counts.stopped == "cap" and counts.resume_from == date(2026, 10, 17)
    assert [r[0] for r in conn.execute("SELECT date FROM daily_wellness")] == ["2026-10-16"]
    assert conn.execute("SELECT COUNT(*) FROM garmin_activities").fetchone()[0] == 0  # 10-17 rolled back
    assert conn.execute("SELECT value FROM meta WHERE key = 'last_sync_utc'").fetchone() is None


def test_days_out_of_range_rejected():
    for bad in ("0", "91"):
        with pytest.raises(SystemExit) as e:
            cli.main(["sync", "--days", bad])
        assert e.value.code == 2
    with pytest.raises(ValueError):
        sync.run(None, None, None, days=91)


def test_cli_sync_without_login_exits_2(data_dir, monkeypatch, capsys):
    def no_tokens():
        raise garmin.LoginNeededError("Garmin login needed: run minou-sync login")

    monkeypatch.setattr(garmin, "open_api", no_tokens)
    assert cli.main(["--data-dir", str(data_dir), "sync"]) == 2
    assert "Garmin login needed: run minou-sync login" in capsys.readouterr().err


def test_cli_sync_offline_exits_3_without_logging_in(data_dir, monkeypatch, capsys):
    def must_not_log_in():
        raise AssertionError("logged in while offline")

    monkeypatch.setattr(garmin, "reachable", lambda: False)
    monkeypatch.setattr(garmin, "open_api", must_not_log_in)
    assert cli.main(["--data-dir", str(data_dir), "sync"]) == 3
    assert "Couldn't reach Garmin" in capsys.readouterr().err


def test_cli_sync_connection_lost_mid_run_exits_3(data_dir, monkeypatch, capsys):
    class DroppedApi(FakeApi):
        def get_activities_by_date(self, start, end):
            raise GarminConnectConnectionError("connection reset")

    monkeypatch.setattr(garmin, "open_api", DroppedApi)
    monkeypatch.setattr(garmin, "GarminClient", client_for)
    assert cli.main(["--data-dir", str(data_dir), "sync", "--days", "1"]) == 3
    assert "Lost the connection to Garmin" in capsys.readouterr().err


def test_cli_sync_runs_and_reconciles(data_dir, monkeypatch, capsys):
    monkeypatch.setattr(garmin, "open_api", lambda: FakeApi(activities=[fake_activity(1, date.today().isoformat())]))
    monkeypatch.setattr(garmin, "GarminClient", client_for)
    assert cli.main(["--data-dir", str(data_dir), "sync", "--days", "1"]) == 0
    out = capsys.readouterr().out
    assert "Activities: 1 new" in out and "Garmin only 1" in out


def backfill_api():
    # Five activities over 12 days, one manual; more than one list page with BACKFILL_PAGE = 2.
    days = ["2026-10-08", "2026-10-10", "2026-10-12", "2026-10-15", "2026-10-18"]
    acts = [fake_activity(i, d) for i, d in enumerate(days, start=1)]
    acts[1]["manualActivity"] = True
    return FakeApi(activities=acts, weigh_ins=[{"weight": 72500.0, "timestampGMT": 1760770800000}])


def test_backfill_fetches_whole_history(conn, data_dir, monkeypatch):
    monkeypatch.setattr(sync, "BACKFILL_PAGE", 2)
    api = backfill_api()
    counts = sync.backfill(conn, client_for(api), data_dir, today=TODAY)

    assert counts.stopped is None and counts.first_day == date(2026, 10, 8)
    assert counts.activities["added"] == 5
    assert [c[1] for c in called(api, "get_activities")] == [0, 2, 4, 5]
    # Files newest first; the manual entry has none.
    assert [c[1] for c in called(api, "download_activity")] == [5, 4, 3, 1]
    # Wellness from today back to the first activity day.
    assert [c[1] for c in called(api, "get_sleep_data")][:2] == ["2026-10-19", "2026-10-18"]
    assert tuple(conn.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM daily_wellness").fetchone()) == \
        ("2026-10-08", "2026-10-19", 12)
    assert conn.execute("SELECT COUNT(*) FROM weigh_ins").fetchone()[0] == 1
    assert sync._meta(conn, "backfill_done_utc") is not None
    assert {c[0] for c in api.calls} <= READ_ONLY_CALLS


def test_backfill_resumes_after_cap_without_repeating_work(conn, data_dir, monkeypatch):
    monkeypatch.setattr(sync, "BACKFILL_PAGE", 2)
    api = backfill_api()
    # 4 list pages + 1 weigh-in + 4 downloads + 15 snapshots + 10 activity details = 34, then 16 a day:
    # three days done at 82 requests, the fourth stops.
    first = sync.backfill(conn, client_for(api, max_requests=90), data_dir, today=TODAY)
    assert first.stopped == "cap" and first.resume_from == date(2026, 10, 16)
    assert sync._meta(conn, "backfill_daily_from") == "2026-10-17"

    api.calls.clear()
    second = sync.backfill(conn, client_for(api), data_dir, today=TODAY)
    assert second.stopped is None
    assert not called(api, "get_activities") and not called(api, "download_activity")
    assert not called(api, "get_body_composition")
    assert not called(api, "get_personal_record") and not called(api, "get_activity")
    assert [c[1] for c in called(api, "get_sleep_data")][0] == "2026-10-16"
    assert conn.execute("SELECT COUNT(*) FROM daily_wellness").fetchone()[0] == 12


def test_backfill_covers_days_synced_before_and_drops_the_old_cursor(conn, data_dir):
    api = backfill_api()
    sync.run(conn, client_for(api), data_dir, days=3, today=TODAY)  # 10-17 to 10-19
    conn.execute("INSERT INTO meta(key, value) VALUES('backfill_wellness_from', '2026-10-17')")  # schema v1 cursor
    conn.commit()
    api.calls.clear()
    sync.backfill(conn, client_for(api), data_dir, today=TODAY)
    days = [c[1] for c in called(api, "get_sleep_data")]
    assert days[0] == "2026-10-19" and days[-1] == "2026-10-08"
    assert sync._meta(conn, "backfill_wellness_from") is None
    # The activity synced earlier already has its file.
    assert 5 not in [c[1] for c in called(api, "download_activity")]


def test_cli_backfill_reports_cap(data_dir, monkeypatch, capsys):
    monkeypatch.setattr(garmin, "open_api", backfill_api)
    monkeypatch.setattr(garmin, "GarminClient", lambda api: client_for(api, max_requests=3))
    assert cli.main(["--data-dir", str(data_dir), "backfill"]) == 1
    assert "Run minou-sync backfill again to continue" in capsys.readouterr().err


def test_range_windows_stay_within_365_days():
    windows = sync.range_windows(date(2024, 12, 14), date(2026, 10, 19))
    assert windows[0] == (date(2024, 12, 14), date(2025, 12, 13))
    assert windows[-1] == (date(2025, 12, 14), date(2026, 10, 19))
    assert all((end - start).days < 365 for start, end in windows)
    assert sync.range_windows(TODAY, TODAY) == [(TODAY, TODAY)]


def test_backfill_fetches_every_range_window(conn, data_dir):
    api = FakeApi(activities=[fake_activity(1, "2025-01-10"), fake_activity(2, "2026-10-18")])
    sync.backfill(conn, client_for(api, max_requests=60), data_dir, today=TODAY)
    assert [c[1:] for c in called(api, "get_hill_score")] == [("2025-01-10", "2026-01-09"), ("2026-01-10", "2026-10-19")]


def test_backfill_refetches_endpoints_that_failed(conn, data_dir):
    api = backfill_api()
    api.failing = {"get_training_status"}
    sync.backfill(conn, client_for(api), data_dir, today=TODAY)
    assert not conn.execute("SELECT 1 FROM garmin_raw WHERE endpoint = 'get_training_status'").fetchone()

    api.failing = set()
    api.calls.clear()
    counts = sync.backfill(conn, client_for(api), data_dir, today=TODAY)
    # Only the gaps: 10-08 to 10-18 (today is left to sync), no other daily calls.
    assert [c[1] for c in called(api, "get_training_status")] == [f"2026-10-{d:02d}" for d in range(8, 19)]
    assert not called(api, "get_sleep_data")
    assert counts.stopped is None


def fake_detail(activity_id, start_gmt, type_key="running", parent=False, child_ids=()):
    return {
        "activityId": activity_id, "activityName": f"Leg {activity_id}", "isMultiSportParent": parent,
        "activityTypeDTO": {"typeKey": type_key},
        "metadataDTO": {"childIds": list(child_ids), "manualActivity": False},
        "summaryDTO": {"startTimeGMT": start_gmt, "startTimeLocal": start_gmt.replace("T08", "T10"),
                       "elapsedDuration": 1800.4, "distance": 5000.0, "averageHR": 150.0, "averagePower": 200.0,
                       "averageBikeCadence": 88.0, "trainingEffect": 2.5},
    }


def test_summary_from_detail_maps_like_a_list_item():
    row = sync.activity_row(sync.summary_from_detail(fake_detail(7, "2026-04-06T08:00:00.0"), parent_id=6), "x")
    assert row["start_utc"] == "2026-04-06T08:00:00Z" and row["start_local"] == "2026-04-06T10:00:00"
    assert (row["elapsed_s"], row["avg_watts"], row["avg_cadence"], row["aerobic_te"]) == (1800, 200.0, 88.0, 2.5)


def test_backfill_fetches_multisport_legs_with_their_parent(conn, data_dir):
    api = FakeApi(activities=[fake_activity(1, "2026-10-18", activityType={"typeKey": "multi_sport"})])
    api.details = {
        1: fake_detail(1, "2026-10-18T07:00:00.0", "multi_sport", parent=True, child_ids=(11, 12)),
        11: fake_detail(11, "2026-10-18T07:00:00.0", "hiking"),
        12: fake_detail(12, "2026-10-18T08:00:00.0", "trail_running"),
    }
    sync.backfill(conn, client_for(api), data_dir, today=TODAY)
    legs = conn.execute("SELECT garmin_id, activity_type, json_extract(raw_json, '$.parentId') FROM garmin_activities"
                        " WHERE garmin_id > 1 ORDER BY garmin_id").fetchall()
    assert [tuple(r) for r in legs] == [(11, "hiking", 1), (12, "trail_running", 1)]
    assert {c[1] for c in called(api, "download_activity")} >= {11, 12}

    # Reconcile keeps one master for the session and reports the legs.
    report = reconcile.run(conn, data_dir / "reports")
    assert [r[0] for r in conn.execute("SELECT garmin_id FROM activities")] == [1]
    assert report.garmin_legs == [(1, 11), (1, 12)]

    api.calls.clear()
    sync.backfill(conn, client_for(api), data_dir, today=TODAY)
    assert [c[1] for c in called(api, "get_activity")] == []  # legs aren't fetched twice


def test_edited_weigh_in_survives_a_sync(conn, data_dir):
    """On the sync side: a weigh-in the athlete edited isn't overwritten by sync."""
    # 2025-10-18 07:00 UTC, the timestamp fake_weigh_ins below reports.
    api = FakeApi(weigh_ins=[{"weight": 72500.0, "timestampGMT": 1760770800000}])
    sync.run(conn, client_for(api), data_dir, days=1, today=date(2025, 10, 18))
    measured = conn.execute("SELECT measured_utc FROM weigh_ins").fetchone()[0]

    # The athlete corrects the weight and stamps edited_utc.
    conn.execute("UPDATE weigh_ins SET weight_kg = 70.0, edited_utc = '2026-10-20T00:00:00Z' WHERE measured_utc = ?",
                 (measured,))
    conn.commit()

    # A later sync of the same day re-reports the old weight; the edit must stand.
    sync.run(conn, client_for(api), data_dir, days=1, today=date(2025, 10, 18))
    row = conn.execute("SELECT weight_kg, edited_utc FROM weigh_ins WHERE measured_utc = ?", (measured,)).fetchone()
    assert row["weight_kg"] == 70.0 and row["edited_utc"] == "2026-10-20T00:00:00Z"


def test_backfill_from_reaches_days_before_the_first_activity(conn, data_dir):
    api = backfill_api()  # first activity 2026-10-08
    sync.backfill(conn, client_for(api), data_dir, today=TODAY)
    api.calls.clear()
    sync.backfill(conn, client_for(api), data_dir, today=TODAY, wellness_from=date(2026, 10, 6))
    assert [c[1] for c in called(api, "get_sleep_data")] == ["2026-10-07", "2026-10-06"]
