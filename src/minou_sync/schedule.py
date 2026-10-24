"""Daily `minou-sync sync` on macOS, as a launchd agent in ~/Library/LaunchAgents.

launchd rather than cron: a run missed while the Mac sleeps happens when it wakes. A run missed while it's
off is skipped, which loses nothing, because sync fetches every day since the last one.
"""

from __future__ import annotations

import os
import plistlib
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

LABEL = "local.minou-sync.sync"
LOG_NAME = "logs/sync.log"

DEFAULT_NOTIFY = "new"
# Exit 3 is "couldn't reach Garmin". launchd starts a missed run the moment the Mac wakes, often before
# Wi-Fi is back, so the job retries for a while; if still offline it stays quiet and the next run catches up.
OFFLINE_RETRIES = 5
OFFLINE_WAIT_S = 120


@dataclass(frozen=True)
class Job:
    hour: int
    minute: int
    notify: str


def sync_command(data_dir: Path, python: str = sys.executable, notify: str | None = None) -> list[str]:
    # This install's own python (a venv or the pipx environment), so the job runs it without relying on launchd's PATH.
    # sync posts its own notifications (notify.py), since only it knows what arrived.
    return [python, "-m", "minou_sync", "--data-dir", str(data_dir), "sync", *(["--notify", notify] if notify else [])]


def script(data_dir: Path, python: str = sys.executable, wait_s: int = OFFLINE_WAIT_S,
           notify: str = DEFAULT_NOTIFY) -> str:
    """The shell script launchd runs: a dated log line, then sync, retried while offline."""
    return "\n".join([
        'echo "--- $(date)"',
        "n=0",
        "while :; do",
        f"  {shlex.join(sync_command(data_dir, python, notify))}",
        "  code=$?",
        "  n=$((n + 1))",
        f"  [ $code -eq 3 ] && [ $n -le {OFFLINE_RETRIES} ] || break",
        f"  sleep {wait_s}",
        "done",
        "exit $code",
    ])


def plist(data_dir: Path, hour: int, minute: int, notify: str = DEFAULT_NOTIFY, python: str = sys.executable) -> bytes:
    log = str(data_dir / LOG_NAME)
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": ["/bin/sh", "-c", script(data_dir, python, notify=notify)],
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": log,
        "StandardErrorPath": log,
        # Garmin is a network job; keep it out of the way of foreground work.
        "ProcessType": "Background",
    })


def agents_dir() -> Path:
    # Looked up on each call rather than at import, so it follows HOME.
    return Path.home() / "Library" / "LaunchAgents"


def _launchctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=check)


def install(data_dir: Path, hour: int, minute: int, notify: str = DEFAULT_NOTIFY) -> Path:
    """Write the agent and load it, replacing an earlier one. Raises CalledProcessError if launchctl refuses."""
    data_dir = data_dir.resolve()
    logs = (data_dir / LOG_NAME).parent
    logs.mkdir(parents=True, exist_ok=True)
    logs.chmod(0o700)  # mkdir's mode is masked by umask
    path = agents_dir() / f"{LABEL}.plist"
    path.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    # Fails when nothing is loaded yet, which is fine.
    _launchctl("bootout", f"{domain}/{LABEL}")
    path.write_bytes(plist(data_dir, hour, minute, notify))
    _launchctl("bootstrap", domain, str(path), check=True)
    return path


def remove() -> bool:
    """Unload and delete the agent. Returns False if there wasn't one."""
    path = agents_dir() / f"{LABEL}.plist"
    if not path.exists():
        return False
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    path.unlink()
    return True


def installed() -> Job | None:
    """The installed agent's time and notification level, or None if there isn't one."""
    try:
        agent = plistlib.loads((agents_dir() / f"{LABEL}.plist").read_bytes())
        when = agent["StartCalendarInterval"]
        level = re.search(r"--notify (\w+)", agent["ProgramArguments"][-1])
    except (FileNotFoundError, plistlib.InvalidFileException, KeyError, IndexError, TypeError):
        return None
    # An agent written before --notify existed notified on errors from its shell script.
    return Job(when["Hour"], when["Minute"], level.group(1) if level else "errors")


def cron_line(data_dir: Path, hour: int, minute: int, python: str = sys.executable) -> str:
    """The crontab line for systems without launchd."""
    log = shlex.quote(str(data_dir.resolve() / LOG_NAME))
    return f"{minute} {hour} * * * {shlex.join(sync_command(data_dir.resolve(), python))} >> {log} 2>&1"
