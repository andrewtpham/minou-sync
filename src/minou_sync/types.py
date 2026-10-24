"""Map Garmin and Strava activity types to one shared set."""

from __future__ import annotations

import logging
import re
from typing import Literal

log = logging.getLogger(__name__)

# Keys are lowercased with spaces and underscores removed, so Garmin's "indoor_cycling" and
# "Indoor Cycling" and Strava's "VirtualRide" and "Virtual Ride" all hit the same entry.
_TYPES: dict[str, str] = {
    # Running
    "running": "run", "run": "run", "streetrunning": "run", "trackrunning": "run",
    "trailrunning": "run_trail", "trailrun": "run_trail",
    "treadmillrunning": "run_treadmill", "indoorrunning": "run_treadmill", "virtualrun": "run_treadmill",
    # Cycling
    "cycling": "ride", "roadbiking": "ride", "roadcycling": "ride", "ride": "ride",
    "ebikeride": "ride", "ebiking": "ride", "commuting": "ride",
    "indoorcycling": "ride_indoor", "virtualride": "ride_indoor", "virtualcycling": "ride_indoor",
    "gravelcycling": "ride_gravel", "gravelride": "ride_gravel",
    "mountainbiking": "ride_mtb", "mountainbikeride": "ride_mtb",
    # Swimming
    "swimming": "swim", "swim": "swim", "lapswimming": "swim", "poolswim": "swim", "openwaterswimming": "swim",
    # On foot
    "walking": "walk", "walk": "walk",
    "hiking": "hike", "hike": "hike",
    "mountaineering": "hike",
    # Gym and mobility
    "strengthtraining": "strength", "weighttraining": "strength", "strength": "strength",
    "indoorrowing": "row", "rowing": "row", "virtualrow": "row",
    "pilates": "pilates",
    "yoga": "yoga",
    "mobility": "mobility", "stretching": "mobility", "breathwork": "mobility",
    "hiit": "cardio", "crossfit": "cardio", "indoorcardio": "cardio", "cardio": "cardio", "workout": "cardio",
    "elliptical": "cardio", "stairstepper": "cardio", "stairclimbing": "cardio", "floorclimbing": "cardio",
    # Climbing, racket sports and multisport (Garmin types seen in a real account)
    "bouldering": "climb", "indoorclimbing": "climb", "rockclimbing": "climb", "rockclimb": "climb", "climbing": "climb",
    "tennis": "tennis", "tennisv2": "tennis", "pickleball": "pickleball",
    "multisport": "multisport",
    # Snow
    "resortskiing": "ski", "resortskiingsnowboarding": "ski", "alpineski": "ski", "alpineskiing": "ski",
    "backcountryski": "ski", "backcountryskiing": "ski", "nordicski": "ski", "crosscountryskiing": "ski",
    "skitouring": "ski", "snowboard": "ski", "snowshoe": "hike", "snowshoeing": "hike",
}


def normalise(source: Literal["garmin", "strava"], raw_type: str) -> str:
    """Return the shared type for a source's activity type, or "other" if it isn't known."""
    key = re.sub(r"[\s_\-]", "", raw_type.lower())
    shared = _TYPES.get(key)
    if shared is None:
        log.warning("Unknown %s activity type %r; stored as 'other'", source, raw_type)
        return "other"
    return shared
