"""The edit layer: the athlete's corrections to activities, kept apart from imported and synced records.

An edit is a row in activity_edits, keyed by the activity's stable key (activities.key, or 'manual:<uuid>'
for a created activity). It names a column of activities, or '_hidden' to drop the row. reconcile rebuilds
activities from the sources, then apply_edits overlays the edit layer, so a correction survives every rebuild
and the source records stay untouched. Edits can be undone, restoring the imported or synced value.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from . import db

log = logging.getLogger(__name__)

HIDDEN = "_hidden"
# Clears an activity's plausibility flag: reconcile re-flags from the values each rebuild, so a live
# '_plausible' edit tells apply_edits to set check_reason back to NULL after the rebuild.
PLAUSIBLE = "_plausible"

# Columns of activities an edit may set (not id, key, source links or the edited flag).
EDITABLE_FIELDS = frozenset({
    "activity_type", "name", "start_utc", "start_local", "elapsed_s", "moving_s", "distance_m", "elev_gain_m",
    "avg_hr", "max_hr", "avg_watts", "avg_cadence", "calories", "aerobic_te", "anaerobic_te",
})
# Fields a manual activity carries; the rest default to NULL.
MANUAL_FIELDS = EDITABLE_FIELDS


@dataclass(frozen=True)
class ActivityChange:
    key: str
    field: str
    value: Any


@dataclass(frozen=True)
class EditResult:
    key: str
    field: str
    ok: bool
    error: str | None = None


@dataclass(frozen=True)
class Edit:
    id: int
    key: str
    field: str
    value: Any
    created_utc: str
    undone_utc: str | None


def _record_edit(conn: sqlite3.Connection, key: str, field: str, value: Any) -> int:
    cur = conn.execute(
        "INSERT INTO activity_edits (key, field, value_json, created_utc) VALUES (?, ?, ?, ?)",
        (key, field, json.dumps(value), db.utc_now()),
    )
    return cur.lastrowid


def edit_activities(conn: sqlite3.Connection, changes: list[ActivityChange]) -> list[EditResult]:
    """Record one edit per change and refresh the affected activities rows. One result per change.

    Each change commits on its own, so a bad one doesn't lose the good ones (the grid's Save all).
    """
    results = []
    for change in changes:
        if change.field not in EDITABLE_FIELDS:
            results.append(EditResult(change.key, change.field, False, f"{change.field} isn't editable"))
            continue
        exists = conn.execute("SELECT 1 FROM activities WHERE key = ?", (change.key,)).fetchone()
        if exists is None:
            results.append(EditResult(change.key, change.field, False, "no activity with that key"))
            continue
        with conn:
            _record_edit(conn, change.key, change.field, change.value)
            conn.execute(
                f"UPDATE activities SET {change.field} = ?, edited = 1 WHERE key = ?",
                (change.value, change.key),
            )
        results.append(EditResult(change.key, change.field, True))
    return results


def hide(conn: sqlite3.Connection, keys: list[str]) -> int:
    """Hide one or more activities: record a hide edit and remove the row. Source records stay unchanged."""
    hidden = 0
    for key in keys:
        if conn.execute("SELECT 1 FROM activities WHERE key = ?", (key,)).fetchone() is None:
            continue
        with conn:
            _record_edit(conn, key, HIDDEN, True)
            conn.execute("DELETE FROM activities WHERE key = ?", (key,))
        hidden += 1
    return hidden


def clear_flag(conn: sqlite3.Connection, key: str) -> bool:
    """Clear an activity's plausibility flag: record a '_plausible' edit and set check_reason NULL now.

    The edit survives every rebuild, so reconcile's re-flagging is overridden until the edit is undone. Returns
    False when no activity has that key.
    """
    if conn.execute("SELECT 1 FROM activities WHERE key = ?", (key,)).fetchone() is None:
        return False
    with conn:
        _record_edit(conn, key, PLAUSIBLE, True)
        conn.execute("UPDATE activities SET check_reason = NULL WHERE key = ?", (key,))
    return True


def create_manual(conn: sqlite3.Connection, fields: dict[str, Any]) -> str:
    """Create a manual activity. Its fields live in the edit layer; it appears in activities after every rebuild."""
    if not fields.get("start_utc"):
        raise ValueError("a manual activity needs start_utc")
    bad = set(fields) - MANUAL_FIELDS
    if bad:
        raise ValueError(f"not editable fields: {', '.join(sorted(bad))}")
    key = f"manual:{uuid.uuid4()}"
    now = db.utc_now()
    row = {"key": key, "source": "manual", "edited": 1,
           "activity_type": fields.get("activity_type") or "other", **{
               f: fields.get(f) for f in MANUAL_FIELDS if f != "activity_type"}}
    with conn:
        conn.execute("INSERT INTO manual_activities (key, created_utc) VALUES (?, ?)", (key, now))
        for field, value in fields.items():
            _record_edit(conn, key, field, value)
        columns = ["key", "source", "activity_type", "edited", *(f for f in MANUAL_FIELDS if f != "activity_type")]
        conn.execute(
            f"INSERT INTO activities ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            [row.get(c) for c in columns],
        )
    return key


def undo(conn: sqlite3.Connection, edit_id: int) -> None:
    """Mark an edit undone, so it stops applying. The field returns to its source value at the next rebuild."""
    with conn:
        conn.execute("UPDATE activity_edits SET undone_utc = ? WHERE id = ? AND undone_utc IS NULL",
                     (db.utc_now(), edit_id))


def history(conn: sqlite3.Connection, key: str) -> list[Edit]:
    """Every edit recorded against a key, newest first."""
    rows = conn.execute(
        "SELECT id, key, field, value_json, created_utc, undone_utc FROM activity_edits WHERE key = ? "
        "ORDER BY created_utc DESC, id DESC", (key,)
    ).fetchall()
    return [Edit(r["id"], r["key"], r["field"], json.loads(r["value_json"]) if r["value_json"] else None,
                 r["created_utc"], r["undone_utc"]) for r in rows]


def apply_edits(conn: sqlite3.Connection, reports: list[str] | None = None) -> int:
    """Overlay the edit layer on the freshly rebuilt activities table. Returns the number of rows edited.

    Runs inside reconcile's rebuild transaction. For each key with live (non-undone) edits: re-insert a manual
    activity, apply field edits, or drop a hidden row. The latest live edit of a field wins. An edit whose key
    has no activities row and isn't manual is skipped and named in `reports`.
    """
    # Latest live edit per (key, field).
    live: dict[tuple[str, str], Any] = {}
    for row in conn.execute(
        "SELECT key, field, value_json FROM activity_edits WHERE undone_utc IS NULL ORDER BY created_utc, id"
    ):
        live[(row["key"], row["field"])] = json.loads(row["value_json"]) if row["value_json"] else None

    manual_keys = {r["key"] for r in conn.execute("SELECT key FROM manual_activities")}
    present = {r["key"] for r in conn.execute("SELECT key FROM activities WHERE key IS NOT NULL")}
    edited_keys: set[str] = set()

    # Re-create manual activities (the rebuild dropped them; their fields live only in the edit layer).
    for key in manual_keys:
        if key in present:
            continue
        fields = {f: v for (k, f), v in live.items() if k == key and f in EDITABLE_FIELDS}
        conn.execute(
            "INSERT INTO activities (key, source, activity_type, start_utc, edited) VALUES (?, 'manual', ?, ?, 1)",
            (key, fields.get("activity_type") or "other", fields.get("start_utc") or ""),
        )
        present.add(key)

    for (key, field), value in live.items():
        if key not in present and key not in manual_keys:
            if reports is not None:
                reports.append(f"{key}:{field}")
            continue
        if field == HIDDEN:
            if value:
                conn.execute("DELETE FROM activities WHERE key = ?", (key,))
                present.discard(key)
            continue
        if field == PLAUSIBLE:
            # Clear the flag reconcile set from the values. Doesn't count as a field edit.
            if value and key in present:
                conn.execute("UPDATE activities SET check_reason = NULL WHERE key = ?", (key,))
            continue
        if field not in EDITABLE_FIELDS or key not in present:
            continue
        conn.execute(f"UPDATE activities SET {field} = ? WHERE key = ?", (value, key))
        edited_keys.add(key)

    if edited_keys:
        conn.executemany("UPDATE activities SET edited = 1 WHERE key = ?", [(k,) for k in edited_keys])
    return len(edited_keys | (manual_keys & present))
