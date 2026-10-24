"""Desktop notifications for unattended syncs: what to say, and posting it through the OS's own tool.

macOS: `osascript`, so notifications show under Script Editor; macOS asks the user the first time and they can
turn them off in System Settings → Notifications. Linux: `notify-send` when it's installed. Elsewhere, nothing.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

log = logging.getLogger(__name__)

TITLE = "minou-sync"
Level = Literal["off", "errors", "new", "always"]
LEVELS: tuple[Level, ...] = ("off", "errors", "new", "always")

# The title and message go in as arguments, so no quoting of either is needed.
_APPLESCRIPT = ["-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                "-e", "end run"]


@dataclass
class SyncNews:
    """What a sync brought in, for the success message."""
    activities: list[str] = field(default_factory=list)  # names (or types) of activities new to the archive
    wellness_through: date | None = None  # the latest complete wellness day, if this run moved it forward
    weigh_ins: int = 0

    @property
    def empty(self) -> bool:
        return not self.activities and self.wellness_through is None and not self.weigh_ins


def success_message(news: SyncNews, level: Level) -> str | None:
    """The message for a sync that finished, or None when this level stays quiet."""
    if level in ("off", "errors") or (level == "new" and news.empty):
        return None
    if news.empty:
        return "Up to date. Nothing new since the last sync."
    parts = []
    n = len(news.activities)
    if n:
        names = ", ".join(news.activities[:2]) + (f" and {n - 2} more" if n > 2 else "")
        parts.append(f"{n} new {'activity' if n == 1 else 'activities'}: {names}.")
    if news.wellness_through:
        d = news.wellness_through
        parts.append(f"Sleep and recovery through {d:%a} {d.day} {d:%b}.")
    if news.weigh_ins:
        parts.append(f"{news.weigh_ins} new weigh-in{'s' if news.weigh_ins > 1 else ''}.")
    return " ".join(parts)


def problem_message(code: int, reason: str, level: Level) -> str | None:
    """The message for a sync that stopped. Offline (exit 3) stays quiet: the job retries and the next run
    catches up."""
    if level == "off" or code == 3:
        return None
    if code == 2:
        return "Garmin needs you to sign in again. Run minou-sync login in a terminal."
    return f"Sync didn't finish: {reason}"


def _run(args: list[str]) -> None:
    subprocess.run(args, capture_output=True, timeout=10, check=True)


def post(message: str, title: str = TITLE) -> bool:
    """Show a notification. Returns False if this system has no way to, or it failed."""
    if sys.platform == "darwin":
        args = ["/usr/bin/osascript", *_APPLESCRIPT, title, message]
    elif shutil.which("notify-send"):
        args = ["notify-send", title, message]
    else:
        return False
    try:
        _run(args)
    except (OSError, subprocess.SubprocessError) as e:
        # Non-critical: the sync result is in the log either way.
        log.warning("Couldn't show a notification (%s)", e)
        return False
    return True
