"""Garmin Connect access: one-time login and a read-only, rate-limited client.

Wraps the unofficial `garminconnect` library. Only the calls in READ_ONLY_CALLS can be made, so
nothing in this project can change the Garmin account.
"""

from __future__ import annotations

import logging
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from garminconnect import Garmin, GarminConnectAuthenticationError, GarminConnectTooManyRequestsError

log = logging.getLogger(__name__)

TOKEN_DIR = Path("~/.garminconnect").expanduser()  # shared with the community Garmin MCP
API_HOST = "connectapi.garmin.com"

# Checked against garminconnect 0.3.2 source on 2026-10-19: each method only calls connectapi (GET) or download.
# request_reload is left out on purpose: it POSTs to make Garmin restore offloaded days.
READ_ONLY_CALLS = frozenset({
    # Activities
    "count_activities", "get_activities", "get_activities_by_date", "download_activity",
    "get_activity", "get_activity_weather", "get_activity_exercise_sets",
    # Daily
    "get_user_summary", "get_sleep_data", "get_hrv_data", "get_training_readiness", "get_training_status",
    "get_max_metrics", "get_stress_data", "get_heart_rates", "get_respiration_data", "get_spo2_data",
    "get_steps_data", "get_floors", "get_intensity_minutes_data", "get_body_battery_events",
    "get_fitnessage_data", "get_lifestyle_logging_data", "get_body_composition",
    # Account snapshots and ranges
    "get_personal_record", "get_devices", "get_primary_training_device", "get_user_profile",
    "get_userprofile_settings", "get_cycling_ftp", "get_lactate_threshold", "get_earned_badges",
    "get_training_plans", "get_activity_types", "get_workouts", "get_endurance_score", "get_hill_score",
    "get_race_predictions", "get_running_tolerance",
    # Garmin's own totals for a date range; used to check the archive's yearly totals.
    "get_progress_summary_between_dates",
})

MIN_INTERVAL_S = 1.0
MAX_REQUESTS = 500
RATE_LIMIT_WAIT_S = 60
RATE_LIMIT_RETRIES = 3


class LoginNeededError(RuntimeError):
    pass


class RequestCapReachedError(RuntimeError):
    pass


class RateLimitedError(RuntimeError):
    pass


def _lock_down(token_dir: Path) -> None:
    # garminconnect writes the token file with default permissions; the tokens give full account access.
    token_dir.chmod(0o700)
    for path in token_dir.glob("*.json"):
        path.chmod(0o600)


def reachable(host: str = API_HOST, port: int = 443, timeout_s: float = 10) -> bool:
    """Whether Garmin's API accepts a connection.

    Checked before logging in because garminconnect reports a failed token refresh or profile fetch as an
    authentication error even when the real cause is no network, which would wrongly say "login needed".
    """
    try:
        socket.create_connection((host, port), timeout=timeout_s).close()
    except OSError:
        return False
    return True


def login(email: str, password: str, prompt_mfa: Callable[[], str], token_dir: Path = TOKEN_DIR) -> None:
    """Log in once with email, password and MFA, and save only the returned tokens."""
    token_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    # A fresh folder has no tokens to load, so login() falls through to the password and saves tokens there.
    Garmin(email=email, password=password, prompt_mfa=prompt_mfa).login(tokenstore=str(token_dir))
    _lock_down(token_dir)


def open_api(token_dir: Path = TOKEN_DIR) -> Garmin:
    """Log in from saved tokens, without a password. Raises LoginNeededError if that isn't possible."""
    if not any(token_dir.glob("*.json")):
        raise LoginNeededError("Garmin login needed: run minou-sync login")
    api = Garmin()
    try:
        api.login(tokenstore=str(token_dir))
    except GarminConnectAuthenticationError as e:
        raise LoginNeededError("Garmin login needed: run minou-sync login") from e
    _lock_down(token_dir)  # a token refresh may have rewritten the file
    return api


class GarminClient:
    """Read-only access to a Garmin API object, at most one request a second and 500 per run."""

    def __init__(
        self,
        api: Any,
        min_interval_s: float = MIN_INTERVAL_S,
        max_requests: int = MAX_REQUESTS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._api = api
        self._min_interval_s = min_interval_s
        self._max_requests = max_requests
        self._sleep = sleep
        self._clock = clock
        self._last_request: float | None = None
        self.requests = 0

    def call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if name not in READ_ONLY_CALLS:
            raise PermissionError(f"{name} isn't a read-only Garmin call allowed by minou-sync")
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            if self.requests >= self._max_requests:
                raise RequestCapReachedError(f"Reached {self._max_requests} Garmin requests in this run")
            self._pace()
            self.requests += 1
            try:
                return getattr(self._api, name)(*args, **kwargs)
            except GarminConnectTooManyRequestsError as e:
                if attempt == RATE_LIMIT_RETRIES:
                    raise RateLimitedError("Garmin rate limit") from e
                log.warning("Garmin rate limit; waiting %s s (retry %s of %s)", RATE_LIMIT_WAIT_S, attempt + 1, RATE_LIMIT_RETRIES)
                self._sleep(RATE_LIMIT_WAIT_S)
        raise AssertionError("unreachable")

    def _pace(self) -> None:
        now = self._clock()
        if self._last_request is not None:
            wait = self._min_interval_s - (now - self._last_request)
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
        self._last_request = now
