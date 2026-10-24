import subprocess
from datetime import date

import pytest

from minou_sync import cli, garmin, notify
from test_sync import FakeApi, client_for, fake_activity


@pytest.fixture
def posted(monkeypatch):
    """Records notifications instead of showing them."""
    messages = []
    monkeypatch.setattr(notify, "post", lambda message, title=notify.TITLE: messages.append(message) or True)
    return messages


def test_success_message_lists_what_arrived():
    news = notify.SyncNews(["Morning ride", "Easy run", "Yoga"], date(2026, 10, 21), 1)
    assert notify.success_message(news, "new") == (
        "3 new activities: Morning ride, Easy run and 1 more. Sleep and recovery through Wed 21 Oct. "
        "1 new weigh-in.")
    assert notify.success_message(notify.SyncNews(["Swim"]), "new") == "1 new activity: Swim."


def test_quiet_levels_and_empty_runs():
    nothing, something = notify.SyncNews(), notify.SyncNews(["Swim"])
    assert notify.success_message(nothing, "new") is None
    assert notify.success_message(something, "errors") is None
    assert notify.success_message(something, "off") is None
    assert notify.success_message(nothing, "always") == "Up to date. Nothing new since the last sync."


def test_problem_messages():
    assert "minou-sync login" in notify.problem_message(2, "Garmin login needed.", "errors")
    assert notify.problem_message(1, "the cap.", "new") == "Sync didn't finish: the cap."
    assert notify.problem_message(3, "offline", "always") is None  # the job retries; offline isn't an error
    assert notify.problem_message(2, "x", "off") is None


def test_post_on_macos_passes_text_as_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    monkeypatch.setattr(notify, "_run", calls.append)
    assert notify.post('He said "hi" \\ bye') is True
    args = calls[0]
    assert args[0] == "/usr/bin/osascript" and args[-2:] == ["minou-sync", 'He said "hi" \\ bye']


def test_post_on_linux_uses_notify_send_when_present(monkeypatch):
    calls = []
    monkeypatch.setattr(notify.sys, "platform", "linux")
    monkeypatch.setattr(notify.shutil, "which", lambda name: "/usr/bin/notify-send")
    monkeypatch.setattr(notify, "_run", calls.append)
    assert notify.post("hello") is True
    assert calls == [["notify-send", "minou-sync", "hello"]]
    monkeypatch.setattr(notify.shutil, "which", lambda name: None)
    assert notify.post("hello") is False


def test_a_failed_post_is_not_an_error(monkeypatch):
    def broken(args):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(notify.sys, "platform", "darwin")
    monkeypatch.setattr(notify, "_run", broken)
    assert notify.post("hello") is False


def _sync_with(monkeypatch, api):
    monkeypatch.setattr(garmin, "reachable", lambda: True)
    monkeypatch.setattr(garmin, "open_api", lambda: api)
    monkeypatch.setattr(garmin, "GarminClient", client_for)


def test_sync_notifies_new_activities(data_dir, monkeypatch, posted):
    _sync_with(monkeypatch, FakeApi(activities=[fake_activity(1, date.today().isoformat())]))
    assert cli.main(["--data-dir", str(data_dir), "sync", "--days", "1", "--notify", "new"]) == 0
    assert len(posted) == 1 and posted[0].startswith("1 new activity: Indoor ride.")
    # Running again brings nothing new, so "new" stays quiet and "always" says so.
    assert cli.main(["--data-dir", str(data_dir), "sync", "--days", "1", "--notify", "new"]) == 0
    assert len(posted) == 1
    assert cli.main(["--data-dir", str(data_dir), "sync", "--days", "1", "--notify", "always"]) == 0
    assert posted[-1] == "Up to date. Nothing new since the last sync."


def test_sync_without_notify_stays_quiet(data_dir, monkeypatch, posted):
    _sync_with(monkeypatch, FakeApi(activities=[fake_activity(1, date.today().isoformat())]))
    assert cli.main(["--data-dir", str(data_dir), "sync", "--days", "1"]) == 0
    assert posted == []


def test_sync_notifies_login_needed(data_dir, monkeypatch, posted):
    def no_tokens():
        raise garmin.LoginNeededError("login")

    monkeypatch.setattr(garmin, "reachable", lambda: True)
    monkeypatch.setattr(garmin, "open_api", no_tokens)
    assert cli.main(["--data-dir", str(data_dir), "sync", "--notify", "errors"]) == 2
    assert "minou-sync login" in posted[0]


def test_sync_stays_quiet_offline(data_dir, monkeypatch, posted):
    monkeypatch.setattr(garmin, "reachable", lambda: False)
    assert cli.main(["--data-dir", str(data_dir), "sync", "--notify", "always"]) == 3
    assert posted == []


def test_a_crash_is_notified_then_raised(data_dir, monkeypatch, posted):
    class Broken(FakeApi):
        def get_activities_by_date(self, start, end):
            raise KeyError("activityId")

    _sync_with(monkeypatch, Broken())
    with pytest.raises(KeyError):
        cli.main(["--data-dir", str(data_dir), "sync", "--days", "1", "--notify", "errors"])
    assert posted == ["Sync didn't finish: KeyError. See logs/sync.log in the data folder."]
