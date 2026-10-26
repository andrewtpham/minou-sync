import os
import plistlib
import stat
import subprocess
from pathlib import Path

import pytest

from minou_sync import cli, schedule


def _fake_python(tmp_path, *codes):
    # Stands in for the venv python: records its arguments and exits like sync would, one code per run
    # (the last repeats).
    path = tmp_path / "python"
    runs = tmp_path / "runs"
    cases = "".join(f"  {i}) exit {c} ;;\n" for i, c in enumerate(codes[:-1], start=1))
    path.write_text(f'#!/bin/sh\necho "args: $*"\necho x >> "{runs}"\n'
                    f'case $(wc -l < "{runs}" | tr -d " ") in\n{cases}  *) exit {codes[-1]} ;;\nesac\n')
    path.chmod(0o755)
    return str(path)


def _run_script(tmp_path, *codes):
    data_dir = tmp_path / "my data"
    text = schedule.script(data_dir, _fake_python(tmp_path, *codes), wait_s=0)
    return subprocess.run(["/bin/sh", "-c", text], capture_output=True, text=True)


def _runs(tmp_path):
    return len((tmp_path / "runs").read_text().splitlines())


def test_script_runs_sync_with_notifications_for_the_data_dir(tmp_path):
    r = _run_script(tmp_path, 0)
    assert r.returncode == 0
    assert f"args: -m minou_sync --data-dir {tmp_path / 'my data'} sync --notify new" in r.stdout
    assert r.stdout.startswith("--- ")


def test_script_passes_failures_through_without_retrying(tmp_path):
    # sync posts its own notices; the script only retries while offline.
    for code in (1, 2):
        (tmp_path / "runs").unlink(missing_ok=True)
        r = _run_script(tmp_path, code)
        assert r.returncode == code
        assert _runs(tmp_path) == 1


def test_script_carries_the_notify_level():
    assert "sync --notify errors" in schedule.script(Path("/data"), "/venv/bin/python", notify="errors")
    assert "sync --notify off" in schedule.script(Path("/data"), "/venv/bin/python", notify="off")


def test_script_carries_the_readiness_note():
    assert "sync --notify new --readiness" in schedule.script(Path("/data"), "/venv/bin/python", readiness=True)
    assert "--readiness" not in schedule.script(Path("/data"), "/venv/bin/python")


def test_script_retries_while_offline_then_syncs(tmp_path):
    r = _run_script(tmp_path, 3, 3, 0)
    assert r.returncode == 0
    assert _runs(tmp_path) == 3


def test_script_gives_up_quietly_when_still_offline(tmp_path):
    r = _run_script(tmp_path, 3)
    assert r.returncode == 3
    assert _runs(tmp_path) == schedule.OFFLINE_RETRIES + 1


def test_script_waits_between_offline_retries():
    text = schedule.script(Path("/data"), "/venv/bin/python")
    assert f"sleep {schedule.OFFLINE_WAIT_S}" in text


def test_plist_runs_daily_at_the_given_time_and_logs_to_the_data_dir(tmp_path):
    p = plistlib.loads(schedule.plist(tmp_path, 7, 5, python="/venv/bin/python"))
    assert p["Label"] == schedule.LABEL
    assert p["StartCalendarInterval"] == {"Hour": 7, "Minute": 5}
    assert p["StandardOutPath"] == p["StandardErrorPath"] == str(tmp_path / "logs" / "sync.log")
    assert p["ProgramArguments"][:2] == ["/bin/sh", "-c"]
    assert "/venv/bin/python -m minou_sync --data-dir" in p["ProgramArguments"][2]


@pytest.fixture
def launchctl(monkeypatch):
    """Records launchctl calls instead of running them."""
    calls = []
    monkeypatch.setattr(schedule, "_launchctl", lambda *args, check=False: calls.append(list(args)))
    return calls


def test_agents_dir_follows_home(isolated_home):
    assert schedule.agents_dir() == isolated_home / "Library" / "LaunchAgents"


def test_install_writes_and_loads_the_agent(tmp_path, launchctl):
    path = schedule.install(tmp_path / "data", 9, 30)
    assert path == schedule.agents_dir() / f"{schedule.LABEL}.plist"
    assert plistlib.loads(path.read_bytes())["StartCalendarInterval"] == {"Hour": 9, "Minute": 30}
    domain = f"gui/{os.getuid()}"
    assert launchctl == [["bootout", f"{domain}/{schedule.LABEL}"], ["bootstrap", domain, str(path)]]
    assert stat.S_IMODE((tmp_path / "data" / "logs").stat().st_mode) == 0o700


def test_install_again_replaces_the_time(tmp_path, launchctl):
    schedule.install(tmp_path / "data", 9, 0)
    path = schedule.install(tmp_path / "data", 6, 45)
    assert plistlib.loads(path.read_bytes())["StartCalendarInterval"] == {"Hour": 6, "Minute": 45}
    assert len(list(schedule.agents_dir().iterdir())) == 1


def test_remove_unloads_and_deletes(tmp_path, launchctl):
    assert schedule.remove() is False
    schedule.install(tmp_path / "data", 9, 0)
    launchctl.clear()
    assert schedule.remove() is True
    assert not any(schedule.agents_dir().iterdir())
    assert launchctl == [["bootout", f"gui/{os.getuid()}/{schedule.LABEL}"]]


def test_installed_reads_the_time_and_notify_level(tmp_path, launchctl):
    assert schedule.installed() is None
    path = schedule.install(tmp_path / "data", 6, 15, "always")
    assert schedule.installed() == schedule.Job(6, 15, "always", False)
    schedule.install(tmp_path / "data", 6, 15, "always", readiness=True)
    assert schedule.installed() == schedule.Job(6, 15, "always", True)
    path.write_text("not a plist")
    assert schedule.installed() is None


def test_an_agent_from_before_notify_levels_reads_as_errors(tmp_path):
    path = schedule.agents_dir() / f"{schedule.LABEL}.plist"
    path.parent.mkdir(parents=True)
    path.write_bytes(plistlib.dumps({"Label": schedule.LABEL, "StartCalendarInterval": {"Hour": 9, "Minute": 0},
                                     "ProgramArguments": ["/bin/sh", "-c", "python -m minou_sync.cli sync"]}))
    assert schedule.installed() == schedule.Job(9, 0, "errors")


def test_cli_status_shows_the_daily_sync(data_dir, launchctl, capsys):
    assert cli.main(["--data-dir", str(data_dir), "status"]) == 0
    assert "Daily sync:         off" in capsys.readouterr().out
    schedule.install(data_dir, 9, 0)
    assert cli.main(["--data-dir", str(data_dir), "status"]) == 0
    assert ("Daily sync:         every day at 09:00, notifies when new data arrives, readiness note off"
            in capsys.readouterr().out)
    schedule.install(data_dir, 9, 0, readiness=True)
    assert cli.main(["--data-dir", str(data_dir), "status"]) == 0
    assert "readiness note on" in capsys.readouterr().out


def test_tests_cannot_reach_launchctl(tmp_path):
    with pytest.raises(AssertionError, match="real daily sync"):
        schedule.install(tmp_path / "data", 9, 0)


def test_cron_line(tmp_path):
    line = schedule.cron_line(tmp_path / "my data", 9, 5, python="/venv/bin/python")
    log = str(tmp_path / "my data" / "logs" / "sync.log")
    assert line.startswith("5 9 * * * /venv/bin/python -m minou_sync --data-dir ")
    assert line.endswith(f">> '{log}' 2>&1")


def test_cli_schedule_installs_at_the_given_time(data_dir, monkeypatch, capsys):
    got = []
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(schedule, "install", lambda d, h, m, n, r: got.append((d, h, m, n, r)))
    assert cli.main(["--data-dir", str(data_dir), "schedule", "--at", "7:05"]) == 0
    assert cli.main(["--data-dir", str(data_dir), "schedule", "--notify", "errors", "--readiness", "off"]) == 0
    assert got == [(data_dir, 7, 5, "new", True), (data_dir, 9, 0, "errors", False)]
    out = capsys.readouterr().out
    assert "every day at 07:05" in out and "notifies when new data arrives" in out
    assert "--readiness off" in out and "--readiness on" in out


def test_cli_schedule_keeps_the_installed_settings_it_isnt_given(data_dir, monkeypatch):
    got = []
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(schedule, "installed", lambda: schedule.Job(6, 30, "always", True))
    monkeypatch.setattr(schedule, "install", lambda d, h, m, n, r: got.append((h, m, n, r)))
    assert cli.main(["--data-dir", str(data_dir), "schedule", "--readiness", "off"]) == 0
    assert cli.main(["--data-dir", str(data_dir), "schedule", "--at", "08:00"]) == 0
    assert got == [(6, 30, "always", False), (8, 0, "always", True)]


def test_cli_schedule_rejects_a_bad_time(data_dir):
    with pytest.raises(SystemExit) as e:
        cli.main(["--data-dir", str(data_dir), "schedule", "--at", "25:00"])
    assert e.value.code == 2


def test_cli_schedule_off(data_dir, monkeypatch, capsys):
    monkeypatch.setattr(schedule, "remove", lambda: False)
    assert cli.main(["--data-dir", str(data_dir), "schedule", "--off"]) == 0
    assert "No daily sync was scheduled." in capsys.readouterr().out


def test_cli_schedule_without_launchd_prints_a_cron_line(data_dir, monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    assert cli.main(["--data-dir", str(data_dir), "schedule"]) == 1
    err = capsys.readouterr().err
    assert "crontab -e" in err and "0 9 * * * " in err


def test_cli_schedule_warns_about_a_folder_the_job_cant_read(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(schedule, "install", lambda d, h, m, n, r: None)
    downloads = tmp_path / "home" / "Downloads" / "minou"
    assert cli.main(["--data-dir", str(downloads), "schedule"]) == 0
    assert "daily sync can't answer" in capsys.readouterr().err
