"""Merge Garmin and Strava source records into one master record per activity."""

from __future__ import annotations

import bisect
import csv
import json
import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import edits, plausibility
from .types import normalise

log = logging.getLogger(__name__)

START_WINDOW_S = 120
# Starts this close are the same recording even when one side was cropped or left running (decided 2026-10-20).
SAME_START_S = 5
ELAPSED_TOLERANCE_PCT = 0.05
ELAPSED_TOLERANCE_MIN_S = 60
DISTANCE_REPORT_PCT = 0.05

# Fields copied to the master record, Garmin first, Strava where Garmin is NULL.
MERGED_FIELDS = (
    "start_utc", "start_local", "elapsed_s", "moving_s", "distance_m", "elev_gain_m",
    "avg_hr", "max_hr", "avg_watts", "avg_cadence", "calories", "aerobic_te", "anaerobic_te",
)


@dataclass(frozen=True)
class SourceRef:
    """One source record, reduced to what matching needs."""

    source: str  # "garmin" or "strava"
    source_id: int
    start: datetime
    elapsed_s: int | None


@dataclass
class ReconcileReport:
    matched: int = 0
    garmin_only: int = 0
    strava_only: int = 0
    # Per further source: records that attached to a Garmin/Strava master, and records that stand alone.
    other_matched: Counter[str] = field(default_factory=Counter)
    other_only: Counter[str] = field(default_factory=Counter)
    by_year: Counter[tuple[int, str]] = field(default_factory=Counter)
    distance_mismatches: list[tuple[int, int, float | None, float | None]] = field(default_factory=list)
    alternates: list[tuple[int, int]] = field(default_factory=list)
    # Strava legs folded into a Garmin multisport activity, as (garmin_id, strava_id); they get no master row.
    legs: list[tuple[int, int]] = field(default_factory=list)
    # Garmin's own multisport legs, as (parent garmin_id, leg garmin_id); also no master row.
    garmin_legs: list[tuple[int, int]] = field(default_factory=list)
    # Edits whose source record vanished, as 'key:field'.
    vanished_edits: list[str] = field(default_factory=list)
    # Activities flagged as implausible, as (key, reason), after the '_plausible' edit cleared any.
    flagged: list[tuple[str, str]] = field(default_factory=list)
    report_path: Path | None = None


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def is_match(g: SourceRef, s: SourceRef) -> bool:
    """Same activity: starts within 5 s, or within 120 s with elapsed time within max(5%, 60 s) of Garmin's."""
    if abs((g.start - s.start).total_seconds()) <= SAME_START_S:
        return True
    if g.elapsed_s is None or s.elapsed_s is None:
        # Without durations two short sessions in a row could merge, so don't guess.
        return False
    if abs((g.start - s.start).total_seconds()) > START_WINDOW_S:
        return False
    tolerance = max(ELAPSED_TOLERANCE_PCT * g.elapsed_s, ELAPSED_TOLERANCE_MIN_S)
    return abs(g.elapsed_s - s.elapsed_s) <= tolerance


def pair(
    garmin: list[SourceRef], strava: list[SourceRef]
) -> tuple[list[tuple[SourceRef | None, SourceRef | None]], list[tuple[int, int]]]:
    """Pair records one to one, closest start time first.

    Returns the pairs (unmatched records paired with None) and the candidate matches that lost
    to a closer one, as (garmin_id, strava_id), for the report.
    """
    strava_sorted = sorted(strava, key=lambda r: r.start)
    strava_starts = [r.start.timestamp() for r in strava_sorted]
    candidates: list[tuple[float, SourceRef, SourceRef]] = []
    for g in garmin:
        t = g.start.timestamp()
        lo = bisect.bisect_left(strava_starts, t - START_WINDOW_S)
        hi = bisect.bisect_right(strava_starts, t + START_WINDOW_S)
        for s in strava_sorted[lo:hi]:
            if is_match(g, s):
                candidates.append((abs(t - s.start.timestamp()), g, s))

    # Ties broken by ids so the result doesn't depend on input order.
    candidates.sort(key=lambda c: (c[0], c[1].source_id, c[2].source_id))
    used_g: set[int] = set()
    used_s: set[int] = set()
    pairs: list[tuple[SourceRef | None, SourceRef | None]] = []
    alternates: list[tuple[int, int]] = []
    for _, g, s in candidates:
        if g.source_id in used_g or s.source_id in used_s:
            alternates.append((g.source_id, s.source_id))
            continue
        used_g.add(g.source_id)
        used_s.add(s.source_id)
        pairs.append((g, s))
    pairs.extend((g, None) for g in garmin if g.source_id not in used_g)
    pairs.extend((None, s) for s in strava if s.source_id not in used_s)
    return pairs, alternates


def absorb_legs(
    pairs: list[tuple[SourceRef | None, SourceRef | None]], multisport_ids: set[int]
) -> tuple[list[tuple[SourceRef | None, SourceRef | None]], list[tuple[int, int]]]:
    """Fold unmatched Strava records that start inside a Garmin multisport activity into it.

    Strava splits a multisport recording into one activity per leg. A multisport record without a Strava
    partner takes its earliest leg as partner; the remaining legs are returned as (garmin_id, strava_id)
    and get no master record of their own.
    """
    hosts = [
        g for g, _ in pairs
        if g is not None and g.source_id in multisport_ids and g.elapsed_s is not None
    ]
    window = timedelta(seconds=START_WINDOW_S)
    found: dict[int, list[SourceRef]] = {}
    kept: list[tuple[SourceRef | None, SourceRef | None]] = []
    for g, s in pairs:
        if g is None and s is not None:
            host = next(
                (h for h in hosts if h.start - window <= s.start <= h.start + timedelta(seconds=h.elapsed_s) + window),
                None,
            )
            if host is not None:
                found.setdefault(host.source_id, []).append(s)
                continue
        kept.append((g, s))
    legs: list[tuple[int, int]] = []
    for i, (g, s) in enumerate(kept):
        if g is None or g.source_id not in found:
            continue
        extra = sorted(found[g.source_id], key=lambda r: r.start)
        if s is None:
            kept[i] = (g, extra.pop(0))
        legs.extend((g.source_id, leg.source_id) for leg in extra)
    return kept, legs


def merge(
    g: dict[str, Any] | None, s: dict[str, Any] | None, others: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Build a master record from a Garmin row, a Strava row, and any further source rows.

    Fields fill from Garmin first, then Strava, then each row in `others` in the order given (the athlete's
    source order). The title comes from Strava when it has one, else the first further source with a name,
    else Garmin. `others` rows are source_activities dicts (source, source_id, activity_type, ...).
    """
    others = others or []
    if g is None and s is None and not others:
        raise ValueError("merge needs at least one source record")
    if g:
        activity_type = normalise("garmin", g["activity_type"])
    elif s:
        activity_type = normalise("strava", s["activity_type"])
    else:
        activity_type = normalise(others[0]["source"], others[0]["activity_type"])
    if g and s:
        source = "both"
    elif g:
        source = "garmin"
    elif s:
        source = "strava"
    else:
        source = others[0]["source"]
    master: dict[str, Any] = {
        "garmin_id": g["garmin_id"] if g else None,
        "strava_id": s["strava_id"] if s else None,
        "source": source,
        "activity_type": activity_type,
    }
    for name in MERGED_FIELDS:
        value = g.get(name) if g else None
        if value is None and s:
            value = s.get(name)
        for other in others:
            if value is None:
                value = other.get(name)
        master[name] = value
    # Strava titles are the ones the athlete edits, so they win; then a further source; then Garmin.
    s_name = (s.get("name") or "").strip() if s else ""
    other_name = next((o["name"].strip() for o in others if (o.get("name") or "").strip()), None)
    master["name"] = s_name or other_name or (g.get("name") if g else None)
    return master


def _load(conn: sqlite3.Connection, table: str, key: str) -> dict[int, dict[str, Any]]:
    return {row[key]: dict(row) for row in conn.execute(f"SELECT * FROM {table}")}


def master_key(master: dict[str, Any]) -> str:
    """The stable key of a master record: '<source>:<id>' of its main record (Garmin, then Strava, then the
    first further source). Stable across rebuilds, so edits and links can target it."""
    if master.get("garmin_id") is not None:
        return f"garmin:{master['garmin_id']}"
    if master.get("strava_id") is not None:
        return f"strava:{master['strava_id']}"
    main = master["_sources"][0]
    return f"{main[0]}:{main[1]}"


def _match_other_source(
    masters: list[dict[str, Any]], rows: list[dict[str, Any]], source: str
) -> tuple[dict[int, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Match one source's records against the existing masters by start and elapsed time.

    Returns a map from master index to the rows that attached to it, and the rows that matched nothing (each
    becomes its own master). A record attaches to the closest master whose Garmin/Strava anchor it matches.
    """
    anchors = [SourceRef(m["source"], 0, parse_utc(m["start_utc"]), m.get("elapsed_s")) for m in masters]
    order = sorted(range(len(masters)), key=lambda i: anchors[i].start)
    starts = [anchors[i].start.timestamp() for i in order]
    attached: dict[int, list[dict[str, Any]]] = {}
    unmatched: list[dict[str, Any]] = []
    taken: set[int] = set()
    for row in sorted(rows, key=lambda r: parse_utc(r["start_utc"])):
        ref = SourceRef(source, 0, parse_utc(row["start_utc"]), row.get("elapsed_s"))
        t = ref.start.timestamp()
        lo = bisect.bisect_left(starts, t - START_WINDOW_S)
        hi = bisect.bisect_right(starts, t + START_WINDOW_S)
        best, best_gap = None, None
        for pos in range(lo, hi):
            mi = order[pos]
            if mi in taken:
                continue
            if is_match(anchors[mi], ref):
                gap = abs(anchors[mi].start.timestamp() - t)
                if best_gap is None or gap < best_gap:
                    best, best_gap = mi, gap
        if best is None:
            unmatched.append(row)
        else:
            attached.setdefault(best, []).append(row)
            taken.add(best)
    return attached, unmatched


def run(conn: sqlite3.Connection, reports_dir: Path, source_order: list[str] | None = None) -> ReconcileReport:
    """Rebuild activities from every source and write a CSV report.

    Garmin and Strava match as in the MVP; each further source then matches against the result, in
    `source_order` (default: alphabetical). Fields fill Garmin first, then Strava, then the other sources in
    order. Each master gets a stable `key`, and activity_sources records every source behind it.
    """
    garmin = _load(conn, "garmin_activities", "garmin_id")
    strava = _load(conn, "strava_activities", "strava_id")
    # A multisport leg is part of its parent's master record, so it's left out of matching.
    garmin_legs = []
    for garmin_id, row in list(garmin.items()):
        parent_id = json.loads(row["raw_json"]).get("parentId")
        if parent_id is not None:
            garmin_legs.append((parent_id, garmin_id))
            del garmin[garmin_id]
    refs_g = [SourceRef("garmin", i, parse_utc(r["start_utc"]), r["elapsed_s"]) for i, r in garmin.items()]
    refs_s = [SourceRef("strava", i, parse_utc(r["start_utc"]), r["elapsed_s"]) for i, r in strava.items()]
    pairs, alternates = pair(refs_g, refs_s)
    multisport = {i for i, r in garmin.items() if normalise("garmin", r["activity_type"]) == "multisport"}
    pairs, legs = absorb_legs(pairs, multisport)

    report = ReconcileReport(alternates=alternates, legs=legs, garmin_legs=sorted(garmin_legs))
    masters: list[dict[str, Any]] = []
    for g_ref, s_ref in pairs:
        g_row = garmin[g_ref.source_id] if g_ref else None
        s_row = strava[s_ref.source_id] if s_ref else None
        master = merge(g_row, s_row)
        master["_sources"] = ([("garmin", str(g_row["garmin_id"]))] if g_row else []) + \
                             ([("strava", str(s_row["strava_id"]))] if s_row else [])
        masters.append(master)
        if master["source"] == "both":
            report.matched += 1
            g_dist, s_dist = g_row["distance_m"], s_row["distance_m"]  # type: ignore[index]
            if g_dist and s_dist and abs(g_dist - s_dist) / g_dist > DISTANCE_REPORT_PCT:
                report.distance_mismatches.append((g_row["garmin_id"], s_row["strava_id"], g_dist, s_dist))  # type: ignore[index]
        elif master["source"] == "garmin":
            report.garmin_only += 1
        else:
            report.strava_only += 1

    # Further sources, matched against the Garmin/Strava masters, in the athlete's order (default alphabetical).
    other = _load_sources(conn)
    for source in (source_order or sorted(other)):
        rows = other.get(source)
        if not rows:
            continue
        attached, unmatched = _match_other_source(masters, rows, source)
        for mi, extra in attached.items():
            master = masters[mi]
            all_others = master.get("_other_rows", []) + extra
            merged = merge(
                garmin.get(master["garmin_id"]) if master["garmin_id"] is not None else None,
                strava.get(master["strava_id"]) if master["strava_id"] is not None else None,
                all_others,
            )
            merged["_other_rows"] = all_others
            merged["_sources"] = master["_sources"] + [(source, r["source_id"]) for r in extra]
            masters[mi] = merged
            report.other_matched[source] += 1
        for row in unmatched:
            m = merge(None, None, [row])
            m["_other_rows"] = [row]
            m["_sources"] = [(source, row["source_id"])]
            masters.append(m)
            report.other_only[source] += 1

    for master in masters:
        report.by_year[(parse_utc(master["start_utc"]).year, master["source"])] += 1
        master["key"] = master_key(master)
        # Flag impossible values for the type. A '_plausible' edit clears it in apply_edits.
        master["check_reason"] = plausibility.check(master)

    _write_activities(conn, masters, report.vanished_edits)
    for g_id, s_id in alternates:
        log.info("Garmin %s also matched Strava %s; a closer match won", g_id, s_id)

    # Read back the flags that survived the edit overlay (a '_plausible' edit clears one).
    report.flagged = [(r["key"], r["check_reason"]) for r in conn.execute(
        "SELECT key, check_reason FROM activities WHERE check_reason IS NOT NULL ORDER BY start_utc")]

    report.report_path = _write_report(report, reports_dir)
    return report


def _load_sources(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    by_source: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute("SELECT * FROM source_activities"):
        by_source.setdefault(row["source"], []).append(dict(row))
    return by_source


def _write_activities(conn: sqlite3.Connection, masters: list[dict[str, Any]], vanished: list[str]) -> None:
    columns = ["key", "garmin_id", "strava_id", "source", "activity_type", "name", *MERGED_FIELDS, "check_reason"]
    with conn:
        conn.execute("DELETE FROM activities")
        conn.execute("DELETE FROM activity_sources")
        conn.executemany(
            f"INSERT INTO activities ({', '.join(columns)}, edited) "
            f"VALUES ({', '.join('?' for _ in columns)}, 0)",
            [[m[c] for c in columns] for m in sorted(masters, key=lambda m: m["start_utc"])],
        )
        links = [(m["key"], src, sid) for m in masters for src, sid in m["_sources"]]
        conn.executemany("INSERT INTO activity_sources (key, source, source_id) VALUES (?, ?, ?)", links)
        # Overlay the edit layer in the same transaction as the rebuild.
        edits.apply_edits(conn, vanished)


def _write_report(report: ReconcileReport, reports_dir: Path) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"reconcile-{datetime.now().date().isoformat()}.csv"
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["section", "year", "source", "count", "garmin_id", "strava_id", "garmin_distance_m", "strava_distance_m",
                    "parent_garmin_id", "key", "reason"])
        for (year, source), count in sorted(report.by_year.items()):
            w.writerow(["by_year", year, source, count, "", "", "", "", "", "", ""])
        for g_id, s_id, g_dist, s_dist in report.distance_mismatches:
            w.writerow(["distance_mismatch", "", "both", "", g_id, s_id, g_dist, s_dist, "", "", ""])
        for g_id, s_id in report.alternates:
            w.writerow(["alternate_candidate", "", "", "", g_id, s_id, "", "", "", "", ""])
        for g_id, s_id in report.legs:
            w.writerow(["multisport_leg", "", "both", "", g_id, s_id, "", "", "", "", ""])
        for parent_id, leg_id in report.garmin_legs:
            w.writerow(["garmin_multisport_leg", "", "garmin", "", leg_id, "", "", "", parent_id, "", ""])
        for key, reason in report.flagged:
            w.writerow(["flagged", "", "", "", "", "", "", "", "", key, reason])
    return path
