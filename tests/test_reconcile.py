import csv
import time
from datetime import datetime, timedelta, timezone

from minou_sync import reconcile
from minou_sync.reconcile import SourceRef, is_match, pair

T0 = datetime(2026, 10, 4, 7, 0, tzinfo=timezone.utc)


def g(i, offset_s=0, elapsed=3600):
    return SourceRef("garmin", i, T0 + timedelta(seconds=offset_s), elapsed)


def s(i, offset_s=0, elapsed=3600):
    return SourceRef("strava", i, T0 + timedelta(seconds=offset_s), elapsed)


def test_start_window_edges():
    assert is_match(g(1), s(1, offset_s=119))
    assert is_match(g(1), s(1, offset_s=120))
    assert not is_match(g(1), s(1, offset_s=121))


def test_elapsed_tolerance_uses_5_percent_for_long_activities():
    # 5% of 3600 s is 180 s, larger than the 60 s floor. Starts 30 s apart, outside the same-start rule.
    assert is_match(g(1, elapsed=3600), s(1, offset_s=30, elapsed=3600 + 176))  # 4.9%
    assert not is_match(g(1, elapsed=3600), s(1, offset_s=30, elapsed=3600 + 184))  # 5.1%


def test_elapsed_tolerance_uses_60_s_floor_for_short_activities():
    assert is_match(g(1, elapsed=600), s(1, offset_s=30, elapsed=660))
    assert not is_match(g(1, elapsed=600), s(1, offset_s=30, elapsed=661))


def test_missing_elapsed_matches_only_on_the_same_start():
    assert not is_match(g(1, elapsed=None), s(1, offset_s=30))
    assert is_match(g(1, elapsed=None), s(1))


def test_closest_start_wins_and_loser_is_reported():
    pairs, alternates = pair([g(1)], [s(10, offset_s=90), s(11, offset_s=5)])
    matched = [(a.source_id, b.source_id) for a, b in pairs if a and b]
    assert matched == [(1, 11)]
    assert alternates == [(1, 10)]
    assert (None, s(10, offset_s=90)) in pairs


def test_back_to_back_sessions_stay_separate():
    # Two 10-minute sessions 11 minutes apart on Garmin, one on Strava.
    pairs, _ = pair([g(1, 0, 600), g(2, 660, 600)], [s(10, 650, 600)])
    matched = [(a.source_id, b.source_id) for a, b in pairs if a and b]
    assert matched == [(2, 10)]


def _insert(conn, table, row):
    cols = ", ".join(row)
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' for _ in row)})", list(row.values()))


def test_run_merges_prefers_garmin_and_strava_title(conn, data_dir):
    _insert(conn, "garmin_activities", {
        "garmin_id": 1, "activity_type": "indoor_cycling", "name": "Indoor Cycling",
        "start_utc": "2026-10-04T07:00:00Z", "elapsed_s": 8820, "distance_m": None, "avg_hr": 137,
        "avg_watts": 110.0, "origin": "export", "raw_json": "{}", "imported_utc": "2026-10-19T10:00:00Z"})
    _insert(conn, "strava_activities", {
        "strava_id": 100, "activity_type": "VirtualRide", "name": "Long ride in the boot",
        "start_utc": "2026-10-04T07:00:02Z", "elapsed_s": 8820, "distance_m": 52000.0, "avg_hr": 136,
        "raw_json": "{}", "imported_utc": "2026-10-19T10:00:00Z"})
    _insert(conn, "strava_activities", {
        "strava_id": 101, "activity_type": "Run", "name": "Old run",
        "start_utc": "2019-05-01T06:00:00Z", "elapsed_s": 3000,
        "raw_json": "{}", "imported_utc": "2026-10-19T10:00:00Z"})
    conn.commit()

    report = reconcile.run(conn, data_dir / "reports")

    assert (report.matched, report.garmin_only, report.strava_only) == (1, 0, 1)
    row = dict(conn.execute("SELECT * FROM activities WHERE garmin_id = 1").fetchone())
    assert row["source"] == "both" and row["strava_id"] == 100
    assert row["avg_hr"] == 137  # Garmin wins
    assert row["distance_m"] == 52000.0  # Strava fills the gap
    assert row["name"] == "Long ride in the boot"
    assert row["activity_type"] == "ride_indoor"
    with report.report_path.open() as f:
        sections = {r["section"] for r in csv.DictReader(f)}
    assert "by_year" in sections


def test_run_is_repeatable(conn, data_dir):
    _insert(conn, "strava_activities", {
        "strava_id": 1, "activity_type": "Run", "start_utc": "2026-01-01T06:00:00Z", "elapsed_s": 3000,
        "raw_json": "{}", "imported_utc": "2026-10-19T10:00:00Z"})
    conn.commit()
    reconcile.run(conn, data_dir / "reports")
    reconcile.run(conn, data_dir / "reports")
    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 1


def test_pairing_10000_records_is_fast():
    garmin = [g(i, offset_s=i * 4000) for i in range(5000)]
    strava = [s(i, offset_s=i * 4000 + 3) for i in range(5000)]
    start = time.perf_counter()
    pairs, _ = pair(garmin, strava)
    assert time.perf_counter() - start < 60
    assert sum(1 for a, b in pairs if a and b) == 5000


def test_run_never_changes_source_records(conn, data_dir):
    """Reconcile never changes a source record."""
    _insert(conn, "garmin_activities", {
        "garmin_id": 1, "activity_type": "running", "start_utc": "2026-10-01T06:00:00Z", "elapsed_s": 3000,
        "origin": "export", "raw_json": "{}", "imported_utc": "2026-10-19T10:00:00Z"})
    _insert(conn, "strava_activities", {
        "strava_id": 2, "activity_type": "Run", "name": "Morning run", "start_utc": "2026-10-01T06:00:01Z",
        "elapsed_s": 3000, "raw_json": "{}", "imported_utc": "2026-10-19T10:00:00Z"})
    conn.commit()
    before = [conn.execute(f"SELECT * FROM {t}").fetchall() for t in ("garmin_activities", "strava_activities")]
    reconcile.run(conn, data_dir / "reports")
    after = [conn.execute(f"SELECT * FROM {t}").fetchall() for t in ("garmin_activities", "strava_activities")]
    assert [list(map(tuple, rows)) for rows in before] == [list(map(tuple, rows)) for rows in after]


def test_same_start_matches_whatever_the_elapsed_time():
    # Watch left running: 750 min on Garmin, 156 min on Strava, same start second.
    assert is_match(g(1, elapsed=45000), s(1, offset_s=3, elapsed=9360))
    assert is_match(g(1, elapsed=None), s(1, offset_s=5, elapsed=None))
    assert not is_match(g(1, elapsed=45000), s(1, offset_s=6, elapsed=9360))


def test_multisport_legs_fold_into_the_garmin_activity(conn, data_dir):
    _insert(conn, "garmin_activities", dict(garmin_id=1, activity_type="multi_sport", start_utc="2026-04-06T12:09:00Z",
                                            elapsed_s=4600, origin="sync", raw_json="{}", imported_utc="x"))
    for sid, start, elapsed in ((10, "2026-04-06T12:09:00Z", 126), (11, "2026-04-06T12:11:10Z", 4480),
                                (12, "2026-04-06T18:00:00Z", 1800)):  # the last one is a separate activity
        _insert(conn, "strava_activities", dict(strava_id=sid, activity_type="Ride", name=f"Leg {sid}",
                                                start_utc=start, elapsed_s=elapsed, raw_json="{}", imported_utc="x"))
    report = reconcile.run(conn, data_dir / "reports")
    rows = [tuple(r) for r in conn.execute("SELECT garmin_id, strava_id, source, name FROM activities ORDER BY start_utc")]
    assert rows == [(1, 10, "both", "Leg 10"), (None, 12, "strava", "Leg 12")]
    assert report.legs == [(1, 11)]
    with open(report.report_path) as f:
        assert "multisport_leg,,both,,1,11,,," in f.read()


def test_lone_leg_becomes_the_multisport_partner():
    host = g(1, elapsed=4600)
    pairs, legs = reconcile.absorb_legs([(host, None), (None, s(10, offset_s=900, elapsed=600))], {1})
    assert [(a.source_id, b.source_id) for a, b in pairs] == [(1, 10)] and legs == []


def _source_activity(conn, source, source_id, start, type_="Run", elapsed=3600, **extra):
    row = {"source": source, "source_id": source_id, "activity_type": type_, "start_utc": start,
           "elapsed_s": elapsed, "raw_json": "{}", "imported_utc": "2026-10-19T10:00:00Z", **extra}
    _insert(conn, "source_activities", row)


def test_three_sources_merge_with_garmin_first_then_strava_then_order(conn, data_dir):
    # One workout on all three sources, 2 s apart. Garmin has HR, Strava the title and distance, nrc watts.
    _insert(conn, "garmin_activities", {
        "garmin_id": 1, "activity_type": "running", "name": "Garmin", "start_utc": "2026-10-04T07:00:00Z",
        "elapsed_s": 3600, "avg_hr": 150, "origin": "sync", "raw_json": "{}", "imported_utc": "x"})
    _insert(conn, "strava_activities", {
        "strava_id": 2, "activity_type": "Run", "name": "Morning run", "start_utc": "2026-10-04T07:00:01Z",
        "elapsed_s": 3600, "distance_m": 10000.0, "raw_json": "{}", "imported_utc": "x"})
    _source_activity(conn, "nrc", "n1", "2026-10-04T07:00:02Z", elapsed=3600, avg_watts=250.0, distance_m=9999.0)
    conn.commit()

    report = reconcile.run(conn, data_dir / "reports")
    row = dict(conn.execute("SELECT * FROM activities").fetchone())
    assert row["source"] == "both"               # Garmin+Strava anchor
    assert row["key"] == "garmin:1"               # Garmin is the main record
    assert row["avg_hr"] == 150                   # Garmin wins
    assert row["distance_m"] == 10000.0           # Strava fills before nrc
    assert row["avg_watts"] == 250.0              # nrc fills what the others lack
    assert row["name"] == "Morning run"           # Strava title wins
    assert report.other_matched["nrc"] == 1
    sources = {(r["source"], r["source_id"]) for r in conn.execute("SELECT source, source_id FROM activity_sources")}
    assert sources == {("garmin", "1"), ("strava", "2"), ("nrc", "n1")}


def test_unmatched_source_activity_becomes_its_own_master(conn, data_dir):
    _source_activity(conn, "nrc", "n1", "2020-01-01T06:00:00Z", type_="Run", avg_watts=200.0)
    conn.commit()
    report = reconcile.run(conn, data_dir / "reports")
    row = dict(conn.execute("SELECT * FROM activities").fetchone())
    assert row["source"] == "nrc" and row["key"] == "nrc:n1" and row["activity_type"] == "run"
    assert report.other_only["nrc"] == 1


def test_source_order_sets_precedence_among_other_sources(conn, data_dir):
    # Two further sources on the same unmatched workout; both supply distance. The first in the order wins.
    _source_activity(conn, "a", "a1", "2026-10-04T07:00:00Z", distance_m=100.0)
    _source_activity(conn, "b", "b1", "2026-10-04T07:00:01Z", distance_m=200.0)
    conn.commit()
    reconcile.run(conn, data_dir / "reports", source_order=["b", "a"])
    row = dict(conn.execute("SELECT * FROM activities").fetchone())
    # 'b' is processed first and becomes the master; 'a' attaches and only fills gaps, so 'b' keeps its distance.
    assert row["key"] == "b:b1" and row["distance_m"] == 200.0


def test_reconcile_keeps_the_mvp_activities_columns(conn, data_dir):
    """An agent reading activities sees the same columns, now with key and edited added."""
    _insert(conn, "garmin_activities", {
        "garmin_id": 1, "activity_type": "running", "start_utc": "2026-10-01T06:00:00Z", "elapsed_s": 3000,
        "origin": "sync", "raw_json": "{}", "imported_utc": "x"})
    conn.commit()
    reconcile.run(conn, data_dir / "reports")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(activities)")]
    mvp = ["id", "garmin_id", "strava_id", "source", "activity_type", "name", "start_utc", "start_local",
           "elapsed_s", "moving_s", "distance_m", "elev_gain_m", "avg_hr", "max_hr", "avg_watts", "avg_cadence",
           "calories", "aerobic_te", "anaerobic_te"]
    assert set(mvp) <= set(cols) and "key" in cols and "edited" in cols


def test_reconcile_is_repeatable_with_sources(conn, data_dir):
    _insert(conn, "garmin_activities", {
        "garmin_id": 1, "activity_type": "running", "start_utc": "2026-10-01T06:00:00Z", "elapsed_s": 3000,
        "origin": "sync", "raw_json": "{}", "imported_utc": "x"})
    _source_activity(conn, "nrc", "n1", "2026-10-01T06:00:01Z", elapsed=3000)
    conn.commit()
    reconcile.run(conn, data_dir / "reports")
    first = [tuple(r) for r in conn.execute("SELECT key, source, activity_type FROM activities ORDER BY key")]
    links_first = sorted(tuple(r) for r in conn.execute("SELECT * FROM activity_sources"))
    reconcile.run(conn, data_dir / "reports")
    assert [tuple(r) for r in conn.execute("SELECT key, source, activity_type FROM activities ORDER BY key")] == first
    assert sorted(tuple(r) for r in conn.execute("SELECT * FROM activity_sources")) == links_first
