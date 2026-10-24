"""Plausibility flags: a derived reason an activity's values look impossible, or None.

reconcile calls check() on every rebuilt row and stores the result in activities.check_reason. A flagged
activity stays in the archive, in lists and in totals, but is left out of records and best efforts.
The limits sit well above world-record pace, so a flag means the data is wrong (a GPS error, a ride saved as
a run), not that the effort was merely fast. The reason names the limit, e.g. "average 31.7 km/h for a run
(limit 25)". An edit with field '_plausible' clears the flag (handled in edits.apply_edits).

Limits (design, "Plausibility limits"):

| Types                                             | Flag when                                                |
|---------------------------------------------------|----------------------------------------------------------|
| run, run_trail, run_treadmill                     | average speed over 25 km/h                               |
| walk, hike                                        | average speed over 12 km/h                               |
| ride, ride_gravel, ride_mtb, ride_indoor          | average speed over 60 km/h                               |
| swim                                              | average speed over 8 km/h                                |
| any                                               | max HR over 230, or average HR above max HR              |

Average speed is distance over moving time, or elapsed time when moving time is missing. A missing or zero
distance or duration can't be judged for speed, so it isn't flagged on speed. Checking against Garmin's
fastest 1 km split belongs to the efforts cross-check, not here, because it needs split data.
"""

from __future__ import annotations

from typing import Any

# Shared activity type -> average-speed limit in km/h. Types not listed have no speed limit.
SPEED_LIMIT_KMH: dict[str, float] = {
    "run": 25.0, "run_trail": 25.0, "run_treadmill": 25.0,
    "walk": 12.0, "hike": 12.0,
    "ride": 60.0, "ride_gravel": 60.0, "ride_mtb": 60.0, "ride_indoor": 60.0,
    "swim": 8.0,
}
MAX_HR_LIMIT = 230


def _avg_speed_kmh(row: dict[str, Any]) -> float | None:
    """Average speed in km/h from distance over moving time, or elapsed time when moving time is missing."""
    distance_m = row.get("distance_m")
    if not distance_m or distance_m <= 0:
        return None
    seconds = row.get("moving_s") or row.get("elapsed_s")
    if not seconds or seconds <= 0:
        return None
    return (distance_m / 1000) / (seconds / 3600)


def check(row: dict[str, Any]) -> str | None:
    """The reason an activity's values look impossible for its type, or None when they're plausible.

    `row` is a rebuilt master activity (a dict with activity_type, distance_m, moving_s, elapsed_s, avg_hr,
    max_hr). Only the first reason found is returned; one flag is enough to keep the activity out of records.
    """
    activity_type = row.get("activity_type")
    limit = SPEED_LIMIT_KMH.get(activity_type)
    if limit is not None:
        speed = _avg_speed_kmh(row)
        if speed is not None and speed > limit:
            return f"average {speed:.1f} km/h for a {activity_type} (limit {limit:g})"

    max_hr = row.get("max_hr")
    if max_hr is not None and max_hr > MAX_HR_LIMIT:
        return f"max HR {max_hr} (limit {MAX_HR_LIMIT})"

    avg_hr = row.get("avg_hr")
    if avg_hr is not None and max_hr is not None and avg_hr > max_hr:
        return f"average HR {avg_hr} above max HR {max_hr}"

    return None
