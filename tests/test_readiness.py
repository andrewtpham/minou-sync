import json
from datetime import datetime

from hypothesis import given
from hypothesis import strategies as st

from minou_sync import cli, readiness
from test_notify import _sync_with, posted  # noqa: F401 (posted is a fixture)
from test_sync import FakeApi

NOW = datetime(2026, 10, 26, 9, 0)


def _entry(at: str, score=38, level="LOW", recovery_min=1500, **factors):
    # Made-up values in the shape of Garmin's get_training_readiness entries.
    return {"calendarDate": at[:10], "timestampLocal": f"{at}.0", "timestamp": f"{at}.0", "score": score,
            "level": level, "recoveryTime": recovery_min, "acwrFactorFeedback": "GOOD",
            "hrvFactorFeedback": "GOOD", "sleepScoreFactorFeedback": "GOOD", **factors}


def _store(conn, day: str, entries):
    conn.execute("INSERT OR REPLACE INTO garmin_raw (endpoint, key, raw_json, fetched_utc) "
                 "VALUES ('get_training_readiness', ?, ?, '2026-10-26T08:00:00Z')", (day, json.dumps(entries)))


def test_latest_takes_the_newest_entry_and_counts_recovery_down(conn):
    _store(conn, "2026-10-25", [_entry("2026-10-25T07:00:00", score=70), _entry("2026-10-25T20:00:00", score=41)])
    _store(conn, "2026-10-26", [_entry("2026-10-26T07:00:00", score=38, recovery_min=1500)])
    r = readiness.latest(conn, NOW)
    assert (r.score, r.level, r.at) == (38, "LOW", datetime(2026, 10, 26, 7, 0))
    assert r.recovery_left_h == 23  # 25 h from 07:00, 2 h gone by 09:00


def test_latest_ignores_entries_without_a_score_and_old_ones(conn):
    _store(conn, "2026-10-26", [{"timestampLocal": "2026-10-26T07:00:00.0", "score": None}])
    assert readiness.latest(conn, NOW) is None
    _store(conn, "2026-10-25", [_entry("2026-10-25T07:00:00")])
    assert readiness.latest(conn, datetime(2026, 10, 26, 20, 0)) is None  # 37 h old
    assert readiness.latest(conn, NOW).score == 38


def test_message_matches_the_example():
    r = readiness.Readiness(datetime(2026, 10, 26, 7, 0), 38, "LOW", 22, load_high=True)
    assert readiness.message(r, NOW) == (
        "Readiness 38, low. About 22 h of recovery left. Recent load is high. Keep today easy.")


def test_message_names_poor_factors_and_an_older_entry():
    r = readiness.Readiness(datetime(2026, 10, 25, 20, 35), 25, "POOR", 0, False, ["last night's sleep", "HRV"])
    assert readiness.message(r, NOW) == (
        "Readiness 25, poor, as of Sun 20:35. Fully recovered. Last night's sleep and HRV are holding it back. "
        "Rest or keep it very easy today.")


def test_advice_follows_level_recovery_and_load():
    def tip(level, left=0, load=False):
        return readiness.advice(readiness.Readiness(NOW, 50, level, left, load))

    assert tip("PRIME") == tip("HIGH") == "Ready for a hard session."
    assert tip("MODERATE") == "A moderate session is fine today."
    assert tip("HIGH", left=13) == tip("HIGH", load=True) == tip("LOW") == "Keep today easy."
    assert tip("HIGH", left=37) == tip("POOR") == "Rest or keep it very easy today."
    assert tip(None) is None


@given(score=st.integers(0, 100), level=st.sampled_from([None, "POOR", "LOW", "MODERATE", "HIGH", "PRIME"]),
       left=st.one_of(st.none(), st.integers(0, 96)), load=st.booleans(),
       factors=st.lists(st.sampled_from(["last night's sleep", "HRV", "recent sleep", "recent stress"]),
                        unique=True))
def test_every_number_in_the_message_comes_from_garmin(score, level, left, load, factors):
    # The note only restates Garmin's figures, and stays short enough for a banner.
    r = readiness.Readiness(datetime(2026, 10, 26, 7, 0), score, level, left, load, factors)
    text = readiness.message(r, NOW)
    numbers = {int(n) for n in "".join(c if c.isdigit() else " " for c in text).split()}
    assert numbers <= {score, left}
    assert len(text) <= 160


def test_sync_posts_the_readiness_note_once(data_dir, monkeypatch, posted):  # noqa: F811
    _sync_with(monkeypatch, FakeApi())
    assert cli.main(["--data-dir", str(data_dir), "sync", "--days", "1", "--readiness"]) == 0
    # FakeApi's newest entry today scores 65.
    assert len(posted) == 1 and posted[0].startswith("Readiness 65.")
    assert cli.main(["--data-dir", str(data_dir), "sync", "--days", "1", "--readiness"]) == 0
    assert len(posted) == 1  # nothing newer from Garmin, so no repeat


def test_sync_without_readiness_posts_no_note(data_dir, monkeypatch, posted):  # noqa: F811
    _sync_with(monkeypatch, FakeApi())
    assert cli.main(["--data-dir", str(data_dir), "sync", "--days", "1", "--notify", "always"]) == 0
    assert all(not m.startswith("Readiness") for m in posted)
