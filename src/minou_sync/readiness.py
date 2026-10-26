"""The daily readiness note: Garmin's own training readiness, put into one short notification.

Built from fixed rules over the latest stored `get_training_readiness` entry, with no model involved, so it is
instant, free and testable. Garmin recalculates readiness several times a day (on waking, after each activity), so
the newest entry by timestamp is the current one. Its recovery time is counted from that timestamp, so the note
subtracts the hours since then.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

TITLE = "minou-sync readiness"
# An entry older than this says nothing about today; the note stays quiet rather than report it.
MAX_AGE = timedelta(hours=36)
# Recovery hours above which the note says to rest, and above which it says to keep it easy.
REST_ABOVE_H = 36
EASY_ABOVE_H = 12

# Garmin rates each readiness factor; only a POOR one is worth naming in a short note.
_FACTORS = (
    ("sleepScoreFactorFeedback", "last night's sleep"),
    ("hrvFactorFeedback", "HRV"),
    ("sleepHistoryFactorFeedback", "recent sleep"),
    ("stressHistoryFactorFeedback", "recent stress"),
)


@dataclass(frozen=True)
class Readiness:
    """The facts the note is built from, all taken from one Garmin readiness entry."""
    at: datetime  # local time Garmin calculated it
    score: int
    level: str | None  # POOR, LOW, MODERATE, HIGH or PRIME
    recovery_left_h: int | None  # hours still to recover now, after the time since `at`
    load_high: bool  # Garmin rates the acute load factor POOR: recent load is high for your fitness
    poor_factors: list[str] = field(default_factory=list)


def _parse_time(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def latest(conn: sqlite3.Connection, now: datetime) -> Readiness | None:
    """The newest readiness entry from today or yesterday (local), or None if there isn't a recent one."""
    days = [(now.date() - timedelta(days=n)).isoformat() for n in (0, 1)]
    newest: tuple[datetime, dict] | None = None
    for (raw,) in conn.execute("SELECT raw_json FROM garmin_raw WHERE endpoint = 'get_training_readiness' "
                               "AND key IN (?, ?)", days):
        try:
            entries = json.loads(raw)
        except ValueError:
            continue
        for entry in entries if isinstance(entries, list) else [entries]:
            if not isinstance(entry, dict) or entry.get("score") is None:
                continue
            at = _parse_time(entry.get("timestampLocal") or entry.get("timestamp"))
            if at and (newest is None or at > newest[0]):
                newest = (at, entry)
    if newest is None or now - newest[0] > MAX_AGE:
        return None
    at, entry = newest
    recovery = entry.get("recoveryTime")  # minutes, counted from `at`
    left = None
    if isinstance(recovery, (int, float)):
        elapsed_min = max((now - at).total_seconds() / 60, 0)
        left = math.ceil(max(recovery - elapsed_min, 0) / 60)
    return Readiness(
        at=at,
        score=int(entry["score"]),
        level=entry.get("level") if isinstance(entry.get("level"), str) else None,
        recovery_left_h=left,
        load_high=entry.get("acwrFactorFeedback") == "POOR",
        poor_factors=[name for key, name in _FACTORS if entry.get(key) == "POOR"],
    )


def advice(r: Readiness) -> str | None:
    """What kind of session today suits, from Garmin's level, the recovery left and the load rating."""
    left = r.recovery_left_h or 0
    if r.level == "POOR" or left > REST_ABOVE_H:
        return "Rest or keep it very easy today."
    if r.level == "LOW" or left > EASY_ABOVE_H or r.load_high:
        return "Keep today easy."
    if r.level == "MODERATE":
        return "A moderate session is fine today."
    if r.level in ("HIGH", "PRIME"):
        return "Ready for a hard session."
    return None


def message(r: Readiness, now: datetime) -> str:
    """The note, e.g. "Readiness 38, low. About 22 h of recovery left. Recent load is high. Keep today easy." """
    head = f"Readiness {r.score}" + (f", {r.level.lower()}" if r.level else "")
    if r.at.date() != now.date():
        head += f", as of {r.at:%a} {r.at:%H:%M}"
    parts = [head + "."]
    if r.recovery_left_h is not None:
        parts.append("Fully recovered." if r.recovery_left_h == 0 else
                     f"About {r.recovery_left_h} h of recovery left.")
    if r.load_high:
        parts.append("Recent load is high.")
    if r.poor_factors:
        names = r.poor_factors[:2]
        joined = names[0] if len(names) == 1 else f"{names[0]} and {names[1]}"
        verb = "is" if len(names) == 1 else "are"
        parts.append(f"{joined[0].upper()}{joined[1:]} {verb} holding it back.")
    tip = advice(r)
    if tip:
        parts.append(tip)
    return " ".join(parts)
