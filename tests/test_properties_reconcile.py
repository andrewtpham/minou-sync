"""Property tests for the reconciler: one master per source record, the match rule, Garmin first."""

from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from minou_sync.reconcile import (
    ELAPSED_TOLERANCE_MIN_S, ELAPSED_TOLERANCE_PCT, SAME_START_S, START_WINDOW_S, SourceRef, absorb_legs, merge, pair,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

# Starts cluster within a few hours so near-misses around the 120 s window are common.
offsets = st.integers(min_value=0, max_value=4 * 3600)
elapsed = st.one_of(st.none(), st.integers(min_value=60, max_value=6 * 3600))


def refs(source: str):
    return st.lists(st.tuples(offsets, elapsed), max_size=25).map(
        lambda items: [SourceRef(source, i, T0 + timedelta(seconds=o), e) for i, (o, e) in enumerate(items)]
    )


@settings(max_examples=200)
@given(refs("garmin"), refs("strava"))
def test_every_source_record_has_exactly_one_master(garmin, strava):
    """Every source record has exactly one master."""
    pairs, _ = pair(garmin, strava)
    g_ids = [a.source_id for a, _ in pairs if a]
    s_ids = [b.source_id for _, b in pairs if b]
    assert sorted(g_ids) == sorted(r.source_id for r in garmin)
    assert sorted(s_ids) == sorted(r.source_id for r in strava)


@settings(max_examples=200)
@given(refs("garmin"), refs("strava"))
def test_every_match_obeys_the_rule(garmin, strava):
    """Every match obeys the matching rule."""
    pairs, _ = pair(garmin, strava)
    for a, b in pairs:
        if a and b:
            gap = abs((a.start - b.start).total_seconds())
            if gap <= SAME_START_S:
                continue
            assert gap <= START_WINDOW_S
            assert abs(a.elapsed_s - b.elapsed_s) <= max(ELAPSED_TOLERANCE_PCT * a.elapsed_s, ELAPSED_TOLERANCE_MIN_S)


optional_number = st.one_of(st.none(), st.integers(min_value=0, max_value=10_000))


@settings(max_examples=200)
@given(
    g_values=st.fixed_dictionaries({k: optional_number for k in ("elapsed_s", "avg_hr", "distance_m", "calories")}),
    s_values=st.fixed_dictionaries({k: optional_number for k in ("elapsed_s", "avg_hr", "distance_m", "calories")}),
    s_name=st.one_of(st.none(), st.just(""), st.text(min_size=1, max_size=20)),
)
def test_garmin_values_take_precedence(g_values, s_values, s_name):
    """Garmin values take precedence over Strava."""
    g = {"garmin_id": 1, "activity_type": "running", "name": "Garmin name", "start_utc": "2026-01-01T00:00:00Z", **g_values}
    s = {"strava_id": 2, "activity_type": "Run", "name": s_name, "start_utc": "2026-01-01T00:00:01Z", **s_values}
    m = merge(g, s)
    for key, value in g_values.items():
        assert m[key] == (value if value is not None else s_values[key])
    expected_name = s_name.strip() if s_name and s_name.strip() else "Garmin name"
    assert m["name"] == expected_name


@settings(max_examples=200)
@given(refs("garmin"), refs("strava"), st.sets(st.integers(min_value=0, max_value=24)))
def test_every_record_has_one_master_or_is_a_leg(garmin, strava, multisport_ids):
    """One master per record with multisport legs: each Strava record is in exactly one pair or exactly one leg."""
    pairs, _ = pair(garmin, strava)
    pairs, legs = absorb_legs(pairs, multisport_ids)
    assert sorted(a.source_id for a, _ in pairs if a) == sorted(r.source_id for r in garmin)
    s_ids = [b.source_id for _, b in pairs if b] + [s for _, s in legs]
    assert sorted(s_ids) == sorted(r.source_id for r in strava)
    assert all(g in multisport_ids for g, _ in legs)
