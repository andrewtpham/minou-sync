import pytest

from minou_sync.types import normalise


@pytest.mark.parametrize(
    ("source", "raw", "expected"),
    [
        ("garmin", "indoor_cycling", "ride_indoor"),
        ("garmin", "Indoor Cycling", "ride_indoor"),
        ("strava", "VirtualRide", "ride_indoor"),
        ("strava", "Virtual Ride", "ride_indoor"),
        ("garmin", "trail_running", "run_trail"),
        ("strava", "TrailRun", "run_trail"),
        ("garmin", "strength_training", "strength"),
        ("strava", "WeightTraining", "strength"),
        ("garmin", "indoor_rowing", "row"),
        ("garmin", "Pilates", "pilates"),
        ("garmin", "Mobility", "mobility"),
        ("strava", "Run", "run"),
        ("garmin", "bouldering", "climb"),
        ("strava", "RockClimbing", "climb"),
        ("strava", "Rock Climb", "climb"),
        ("garmin", "tennis_v2", "tennis"),
        ("garmin", "pickleball", "pickleball"),
        ("garmin", "multi_sport", "multisport"),
    ],
)
def test_known_types(source, raw, expected):
    assert normalise(source, raw) == expected


def test_unknown_type_is_other_and_logged(caplog):
    assert normalise("garmin", "underwater_basket_weaving") == "other"
    assert "underwater_basket_weaving" in caplog.text
