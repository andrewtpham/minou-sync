"""GarminClient with a fake API and a fake clock: read-only calls, request pacing, the 429 path and the cap."""

import socket

import pytest
from garminconnect import GarminConnectTooManyRequestsError
from hypothesis import given, settings
from hypothesis import strategies as st

from minou_sync import garmin
from minou_sync.garmin import READ_ONLY_CALLS, GarminClient, RateLimitedError, RequestCapReachedError


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.request_times: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeApi:
    def __init__(self, clock: FakeClock, failures: int = 0):
        self._clock = clock
        self._failures = failures

    def __getattr__(self, name):
        def method(*args, **kwargs):
            self._clock.request_times.append(self._clock.now)
            if self._failures:
                self._failures -= 1
                raise GarminConnectTooManyRequestsError("429")
            return {"called": name}
        return method


def make_client(failures=0, max_requests=500):
    clock = FakeClock()
    client = GarminClient(FakeApi(clock, failures), max_requests=max_requests, sleep=clock.sleep, clock=clock.clock)
    return client, clock


@settings(max_examples=100)
@given(st.text(min_size=1, max_size=40))
def test_only_read_only_calls_reach_the_api(name):
    """Only read-only calls get through."""
    client, clock = make_client()
    if name in READ_ONLY_CALLS:
        assert client.call(name) == {"called": name}
    else:
        with pytest.raises(PermissionError):
            client.call(name)
        assert clock.request_times == []


@pytest.mark.parametrize("name", ["delete_activity", "upload_activity", "add_weigh_in", "set_activity_name"])
def test_write_calls_are_refused(name):
    client, clock = make_client()
    with pytest.raises(PermissionError):
        client.call(name)
    assert clock.request_times == []


@settings(max_examples=100)
@given(st.integers(min_value=1, max_value=60))
def test_requests_are_at_least_one_second_apart(n):
    """Requests are spaced at least the minimum interval apart."""
    client, clock = make_client()
    for _ in range(n):
        client.call("get_user_summary", "2026-10-19")
    gaps = [b - a for a, b in zip(clock.request_times, clock.request_times[1:])]
    assert all(gap >= 1.0 for gap in gaps)


def test_cap_stops_at_500_requests():
    """A run stops at the request cap."""
    client, clock = make_client()
    for _ in range(500):
        client.call("get_user_summary", "2026-10-19")
    with pytest.raises(RequestCapReachedError):
        client.call("get_user_summary", "2026-10-19")
    assert len(clock.request_times) == 500


def test_rate_limit_waits_60_s_and_retries():
    client, clock = make_client(failures=2)
    assert client.call("get_user_summary", "2026-10-19") == {"called": "get_user_summary"}
    assert len(clock.request_times) == 3
    assert clock.request_times[1] - clock.request_times[0] >= 60


def test_rate_limit_gives_up_after_3_retries():
    client, clock = make_client(failures=10)
    with pytest.raises(RateLimitedError):
        client.call("get_user_summary", "2026-10-19")
    assert len(clock.request_times) == 4


def test_open_api_without_tokens_asks_for_login(tmp_path):
    with pytest.raises(garmin.LoginNeededError, match="run minou-sync login"):
        garmin.open_api(tmp_path / "no-tokens")


def test_read_only_calls_exist_in_garminconnect():
    from garminconnect import Garmin
    missing = [name for name in READ_ONLY_CALLS if not callable(getattr(Garmin, name, None))]
    assert missing == []


def test_reachable_tells_an_open_port_from_a_closed_one():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    assert garmin.reachable("127.0.0.1", port, timeout_s=1) is True
    server.close()
    assert garmin.reachable("127.0.0.1", port, timeout_s=1) is False
