"""Plausibility flags. All values invented, never real."""

from minou_sync import db, edits, plausibility, queries, reconcile


# --- plausibility.check -------------------------------------------------------------------------------

def test_flags_an_impossible_run_speed():
    # 11 km in 21 minutes is about 31.4 km/h, well over the 25 km/h run limit.
    reason = plausibility.check(
        {"activity_type": "run", "distance_m": 11000.0, "moving_s": 21 * 60, "elapsed_s": 21 * 60})
    assert reason is not None and "run" in reason and "limit 25" in reason


def test_flags_a_max_hr_over_230():
    reason = plausibility.check({"activity_type": "run", "distance_m": 1000.0, "elapsed_s": 300, "max_hr": 240})
    assert reason == "max HR 240 (limit 230)"


def test_flags_average_hr_above_max_hr():
    reason = plausibility.check(
        {"activity_type": "yoga", "distance_m": None, "elapsed_s": 1800, "avg_hr": 150, "max_hr": 120})
    assert reason == "average HR 150 above max HR 120"


def test_does_not_flag_a_fast_but_possible_run():
    # 3:00/km is 20 km/h: genuinely fast, under the 25 km/h limit.
    assert plausibility.check(
        {"activity_type": "run", "distance_m": 10000.0, "moving_s": 30 * 60, "elapsed_s": 30 * 60}) is None


def test_does_not_flag_a_fast_but_possible_ride():
    # 45 km/h for a ride is under the 60 km/h limit.
    assert plausibility.check(
        {"activity_type": "ride", "distance_m": 45000.0, "moving_s": 3600, "elapsed_s": 3600}) is None


def test_uses_elapsed_time_when_moving_time_is_missing():
    reason = plausibility.check(
        {"activity_type": "run", "distance_m": 11000.0, "moving_s": None, "elapsed_s": 21 * 60})
    assert reason is not None


def test_missing_distance_or_duration_is_not_flagged_on_speed():
    assert plausibility.check({"activity_type": "run", "distance_m": None, "elapsed_s": 300}) is None
    assert plausibility.check({"activity_type": "run", "distance_m": 10000.0, "elapsed_s": 0}) is None


def test_type_without_a_speed_limit_is_not_flagged_on_speed():
    # A strength session with a nominal distance has no speed limit.
    assert plausibility.check({"activity_type": "strength", "distance_m": 50000.0, "elapsed_s": 60}) is None


# --- reconcile flags and the report -------------------------------------------------------------------

def _garmin(conn, garmin_id, type_, start, elapsed, distance=None, max_hr=None):
    conn.execute(
        "INSERT INTO garmin_activities (garmin_id, activity_type, start_utc, elapsed_s, distance_m, max_hr, "
        "origin, raw_json, imported_utc) VALUES (?, ?, ?, ?, ?, ?, 'sync', '{}', 'x')",
        (garmin_id, type_, start, elapsed, distance, max_hr))


def test_reconcile_flags_implausible_and_reports(conn, data_dir):
    # A plausible run and an impossible one (11 km in 21 min).
    _garmin(conn, 1, "running", "2026-10-01T06:00:00Z", 1800, distance=10000.0)
    _garmin(conn, 2, "running", "2026-10-02T06:00:00Z", 21 * 60, distance=11000.0)
    conn.commit()
    report = reconcile.run(conn, data_dir / "reports")
    assert len(report.flagged) == 1 and report.flagged[0][0] == "garmin:2"
    flagged = conn.execute("SELECT check_reason FROM activities WHERE key = 'garmin:2'").fetchone()[0]
    assert flagged is not None
    assert conn.execute("SELECT check_reason FROM activities WHERE key = 'garmin:1'").fetchone()[0] is None
    with report.report_path.open() as f:
        assert "flagged" in f.read()


def test_cleared_flag_survives_reconcile(conn, data_dir):
    _garmin(conn, 2, "running", "2026-10-02T06:00:00Z", 21 * 60, distance=11000.0)
    conn.commit()
    reconcile.run(conn, data_dir / "reports")
    assert edits.clear_flag(conn, "garmin:2") is True
    assert conn.execute("SELECT check_reason FROM activities WHERE key = 'garmin:2'").fetchone()[0] is None
    # A rebuild re-flags from the values, but the '_plausible' edit clears it again.
    report = reconcile.run(conn, data_dir / "reports")
    assert report.flagged == []
    assert conn.execute("SELECT check_reason FROM activities WHERE key = 'garmin:2'").fetchone()[0] is None


def test_clear_flag_unknown_key_returns_false(conn):
    assert edits.clear_flag(conn, "garmin:999") is False


def test_queries_surface_the_reason(conn, data_dir):
    _garmin(conn, 2, "running", "2026-10-02T06:00:00Z", 21 * 60, distance=11000.0)
    conn.commit()
    reconcile.run(conn, data_dir / "reports")
    listed = queries.list_activities(conn)
    assert listed.rows[0]["check_reason"] is not None
    one = queries.get_activity(conn, "garmin:2")
    assert one["check_reason"] is not None


def test_flagged_activity_stays_in_lists_and_totals(conn, data_dir):
    """A flagged activity is still listed and still counts in totals."""
    _garmin(conn, 2, "running", "2026-10-02T06:00:00Z", 21 * 60, distance=11000.0)
    conn.commit()
    reconcile.run(conn, data_dir / "reports")
    assert queries.list_activities(conn).total == 1
    assert queries.totals(conn, period="month")[0]["activities"] == 1
