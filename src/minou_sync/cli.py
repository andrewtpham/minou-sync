"""The minou-sync command. Run `minou-sync --help` for subcommands."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import subprocess
import sys
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

from . import db, efforts, export, reconcile


def _status(data_dir: Path, source: str = "flag") -> int:
    from . import config

    conn = db.connect(data_dir)
    db.migrate(conn)
    r = export.status(conn)
    where = {"flag": "--data-dir", "env": config.ENV_VAR, "config": _tilde(config.config_path()),
             "default": "the default"}[source]
    print(f"Data folder:        {data_dir} (from {where})")
    print(f"Activities:         {r.total} "
          f"(both {r.by_source.get('both', 0)}, Garmin only {r.by_source.get('garmin', 0)}, "
          f"Strava only {r.by_source.get('strava', 0)})")
    print(f"First and last:     {r.first_start_utc or '-'} to {r.last_start_utc or '-'}")
    print(f"Last wellness day:  {r.last_wellness_date or '-'}")
    print(f"Last sync:          {r.last_sync_utc or 'never'}")
    print(f"Daily sync:         {_daily_sync_summary()}")
    print(f"Without a file:     {r.without_file}")
    print(f"archive.db size:    {r.db_bytes / 1024 / 1024:.1f} MB")
    return 0


def _import_strava(data_dir: Path, path: Path) -> int:
    from . import import_strava

    conn = db.connect(data_dir)
    db.migrate(conn)
    try:
        c = import_strava.run(conn, path, data_dir,
                              progress=_progress_bar("Strava activities", "Importing your Strava archive:"))
    except (import_strava.NotAnExportError, FileNotFoundError, zipfile.BadZipFile) as e:
        print(e, file=sys.stderr)
        return 1
    print(f"Strava: {c.added} added, {c.updated} updated, {c.unchanged} unchanged, {c.failed} failed; "
          f"{c.files} files copied; {c.null_fields} fields stored as NULL.")
    _compute_efforts(conn, data_dir)
    print("Run minou-sync reconcile to merge them with Garmin.")
    return 0 if c.failed == 0 else 1


def _import_garmin(data_dir: Path, path: Path) -> int:
    from . import import_garmin

    conn = db.connect(data_dir)
    db.migrate(conn)
    try:
        c = import_garmin.run(conn, path, data_dir,
                              progress=_progress_bar("Garmin activities", "Importing your Garmin export:"))
    except (import_garmin.NotAnExportError, FileNotFoundError, zipfile.BadZipFile) as e:
        print(e, file=sys.stderr)
        return 1
    print(f"Garmin export: {c.added} added, {c.updated} updated, {c.unchanged} unchanged, {c.failed} failed; "
          f"{c.from_sync} already in the archive from sync, left as they are.")
    print(f"{c.files} files copied; {c.wellness_days} wellness days added or filled in; {c.weigh_ins} weigh-ins "
          f"added; {c.null_fields} fields stored as NULL.")
    _compute_efforts(conn, data_dir)
    print("Run minou-sync reconcile to merge them with your other sources.")
    return 0 if c.failed == 0 else 1


def _import(data_dir: Path, path: Path, source: str, profile: str | None) -> int:
    from . import importer

    conn = db.connect(data_dir)
    db.migrate(conn)
    try:
        c = importer.run(conn, path, source, data_dir, profile,
                         progress=_progress_bar("files", f"Importing '{source}' and reading its activity files:"))
    except (importer.ProfileError, importer.NoProfileError, importer.MissingColumnError,
            FileNotFoundError, zipfile.BadZipFile) as e:
        print(e, file=sys.stderr)
        return 1
    kinds = ", ".join(f"{n} {k}" for k, n in sorted(c.kinds.items())) or "no CSV records"
    print(f"Imported as '{source}': {c.added} added, {c.updated} updated, {c.unchanged} unchanged, "
          f"{c.failed} skipped; {c.files} files copied; {c.null_fields} fields stored as NULL.")
    print(f"CSV records by kind: {kinds}.")
    _compute_efforts(conn, data_dir)
    print("Run minou-sync reconcile to merge activities with your other sources.")
    return 0 if c.failed == 0 else 1


def _export_csv(data_dir: Path) -> int:
    conn = db.connect(data_dir)
    db.migrate(conn)
    for path in export.export_csv(conn, data_dir / "exports"):
        print(f"Wrote {path}")
    return 0


def _progress_bar(noun: str, header: str | None = None):
    """A one-line stderr progress bar, or None when stderr isn't a terminal (logs, pipes, the server).

    A long run shows how far it's got. If `header` is given it's printed once before the bar starts, to explain
    what's happening and the bigger picture. The bar line reads "[####----] 120/546 files" so the two numbers'
    meaning is clear. Redraws one carriage-returned line, ends with a newline, and draws nothing when there's no
    work. Off when stderr isn't a TTY.
    """
    if not sys.stderr.isatty():
        return None
    width = 24
    started = {"done": False}

    def draw(done: int, total: int) -> None:
        if total <= 0:
            return
        if not started["done"]:
            if header:
                print(header, file=sys.stderr, flush=True)
            started["done"] = True
        filled = round(width * done / total)
        bar = "#" * filled + "-" * (width - filled)
        end = "\n" if done >= total else ""
        print(f"\r  [{bar}] {done}/{total} {noun}", end=end, file=sys.stderr, flush=True)

    return draw


def _efforts_summary(c: efforts.EffortCounts, scope: str) -> str:
    """A clear wrap-up of what the run did: efforts written, and anything the user should know about."""
    did_work = c.activities or c.empty or c.unreadable or c.files_missing
    if not did_work:
        return f"Done: already up to date; {c.skipped} files already processed, nothing to do."
    line = f"Done: {c.efforts} best efforts computed from {c.activities} files"
    line += f" ({scope})." if scope == "whole archive" else "."
    extras = []
    if c.skipped:
        extras.append(f"{c.skipped} already done, left as-is")
    if c.empty:
        extras.append(f"{c.empty} had no record-worthy split (short or non-distance activities)")
    if c.unreadable:
        extras.append(f"{c.unreadable} couldn't be read — they'll be retried next time")
    if c.files_missing:
        extras.append(f"{c.files_missing} files are recorded but missing from disk")
    if extras:
        line += " " + "; ".join(extras) + "."
    if c.garmin_checked:
        line += (f" Cross-check against Garmin's own splits: {c.garmin_differs} of {c.garmin_checked} "
                 f"Garmin activities differ by more than 2%.")
    line += " Records exclude flagged and hidden activities."
    return line


def _efforts_header(p: efforts.EffortPlan, whole: bool) -> str:
    """A short explanation printed before the bar: what's running, what's done vs left, and that it's resumable.

    Reading a file takes a moment, so a rough time hint is given at ~8 files/second, and the user is told they
    can stop and rerun without losing work (the ledger resumes).
    """
    mins = max(1, round(p.to_process / 8 / 60))
    eta = "under a minute" if p.to_process < 480 else f"about {mins} minute{'s' if mins != 1 else ''}"
    lines = ["Computing best efforts from your original activity files"
             " — fastest 1 K/5 K/10 K/half/marathon for runs, best power and fastest 40 km for rides."]
    if whole:
        lines.append(f"Recomputing all {p.total_files} files (--all). This takes {eta}.")
    else:
        lines.append(f"{p.total_files} files total: {p.already_done} already done, {p.to_process} to go "
                     f"(new, changed or retried). This takes {eta}.")
    lines.append("Safe to stop anytime — rerunning picks up where it left off.")
    return "\n".join(lines)


def _compute_efforts(conn, data_dir: Path, only_new: bool = True) -> efforts.EffortCounts:
    """Compute best efforts for new original files and print a one-line summary."""
    header = _efforts_header(efforts.plan(conn, only_new=only_new), whole=not only_new)
    c = efforts.run(conn, data_dir, only_new=only_new, progress=_progress_bar("files", header))
    if c.activities or c.empty or c.unreadable or c.files_missing:
        print(_efforts_summary(c, "new files"))
    return c


def _efforts(data_dir: Path, whole: bool) -> int:
    conn = db.connect(data_dir)
    db.migrate(conn)
    p = efforts.plan(conn, only_new=not whole)
    if p.to_process == 0:
        print(f"Best efforts: already up to date; all {p.already_done} of {p.total_files} files done. "
              f"Use --all to recompute from scratch.")
        return 0
    header = _efforts_header(p, whole)
    c = efforts.run(conn, data_dir, only_new=not whole, progress=_progress_bar("files", header))
    print(_efforts_summary(c, "whole archive" if whole else "new files"))
    return 0


def _reconcile(data_dir: Path, source_order: list[str] | None = None) -> int:
    conn = db.connect(data_dir)
    db.migrate(conn)
    report = reconcile.run(conn, data_dir / "reports", source_order)
    print(f"Matched {report.matched}, Garmin only {report.garmin_only}, Strava only {report.strava_only}; "
          f"multisport legs folded in: {len(report.garmin_legs)} Garmin, {len(report.legs)} Strava.")
    if report.other_matched or report.other_only:
        parts = []
        for src in sorted(set(report.other_matched) | set(report.other_only)):
            parts.append(f"{src} ({report.other_matched.get(src, 0)} merged, {report.other_only.get(src, 0)} new)")
        print("Other sources: " + ", ".join(parts) + ".")
    print(f"Distance differences over 5%: {len(report.distance_mismatches)}. "
          f"Implausible activities flagged: {len(report.flagged)}. Report: {report.report_path}")
    return 0


def _days(value: str) -> int:
    from .sync import MAX_DAYS

    days = int(value)
    if not 1 <= days <= MAX_DAYS:
        raise argparse.ArgumentTypeError(f"must be 1 to {MAX_DAYS}")
    return days


def _sync_run(conn, data_dir: Path, days: int | None, full_history: bool,
              wellness_from: date | None) -> tuple[int, str]:
    """Run sync or backfill and print the result. Returns the exit code and, when it stopped, a short reason."""
    # Imported here so commands that never touch Garmin don't load its HTTP stack.
    from garminconnect import GarminConnectAuthenticationError, GarminConnectConnectionError

    from . import garmin, sync

    # Exit 3 means "offline, try again"; the scheduled job retries on it (schedule.py).
    if not garmin.reachable():
        print(f"Couldn't reach Garmin ({garmin.API_HOST}). Check the connection and run sync again.",
              file=sys.stderr)
        return 3, "Garmin couldn't be reached."
    try:
        client = garmin.GarminClient(garmin.open_api())
        counts = (sync.backfill(conn, client, data_dir, wellness_from=wellness_from) if full_history
                  else sync.run(conn, client, data_dir, days))
    except (garmin.LoginNeededError, GarminConnectAuthenticationError):
        print("Garmin login needed: run minou-sync login", file=sys.stderr)
        return 2, "Garmin login needed."
    except GarminConnectConnectionError as e:
        print(f"Lost the connection to Garmin ({e}). Finished days are saved; run sync again.", file=sys.stderr)
        return 3, "Lost the connection to Garmin."
    a = counts.activities
    verb = "Backfilled" if full_history else "Synced"
    print(f"{verb} {counts.first_day} to {counts.last_day} in {counts.requests} requests.")
    print(f"Activities: {a['added']} new, {a['updated']} updated, {a['unchanged']} unchanged; "
          f"{counts.files} files. Wellness days: {counts.wellness_days}. Weigh-ins: {counts.weigh_ins}.")
    # Best efforts for the new files, computed quietly so sync's output is unchanged.
    efforts.run(conn, data_dir, only_new=True)
    report = reconcile.run(conn, data_dir / "reports")
    print(f"Reconciled: matched {report.matched}, Garmin only {report.garmin_only}, Strava only {report.strava_only}.")
    if counts.stopped and full_history:
        reason = f"Reached {garmin.MAX_REQUESTS} requests" if counts.stopped == "cap" else "Garmin rate limit"
        print(f"{reason}; progress is saved. Run minou-sync backfill again to continue.", file=sys.stderr)
        return 1, f"{reason}. Progress is saved."
    if full_history:
        print("Backfill complete.")
    if counts.stopped == "cap":
        print(f"Reached {garmin.MAX_REQUESTS} requests; run sync again to continue from {counts.resume_from}.",
              file=sys.stderr)
        return 1, f"it reached {garmin.MAX_REQUESTS} Garmin requests. Progress is saved and the next run continues."
    if counts.stopped == "rate_limit":
        saved = counts.resume_from - timedelta(days=1) if counts.resume_from else None
        print(f"Garmin rate limit; saved up to {saved}. Try again later.", file=sys.stderr)
        return 1, f"Garmin's rate limit. Saved up to {saved}; the next run continues."
    return 0, ""


def _sync_snapshot(conn) -> tuple[set[int], str | None, int]:
    """What the archive holds before a sync, to tell afterwards what the run brought in."""
    ids = {row[0] for row in conn.execute("SELECT garmin_id FROM activities WHERE garmin_id IS NOT NULL")}
    latest = conn.execute("SELECT MAX(date) FROM daily_wellness WHERE complete = 1").fetchone()[0]
    weigh_ins = conn.execute("SELECT COUNT(*) FROM weigh_ins").fetchone()[0]
    return ids, latest, weigh_ins


def _sync_news(conn, before: tuple[set[int], str | None, int]):
    from . import notify

    ids, latest_before, weigh_ins_before = before
    new = [row for row in conn.execute(
        "SELECT garmin_id, name, activity_type FROM activities WHERE garmin_id IS NOT NULL ORDER BY start_utc")
        if row["garmin_id"] not in ids]
    latest = conn.execute("SELECT MAX(date) FROM daily_wellness WHERE complete = 1").fetchone()[0]
    return notify.SyncNews(
        activities=[row["name"] or row["activity_type"].replace("_", " ") for row in new],
        wellness_through=date.fromisoformat(latest) if latest and latest != latest_before else None,
        weigh_ins=conn.execute("SELECT COUNT(*) FROM weigh_ins").fetchone()[0] - weigh_ins_before,
    )


def _readiness_note(conn, before) -> None:
    """Post the readiness note when this run brought in a newer Garmin readiness entry than the archive had."""
    from . import notify, readiness

    now = datetime.now()
    latest = readiness.latest(conn, now)
    if latest and (before is None or latest.at > before.at):
        notify.post(readiness.message(latest, now), title=readiness.TITLE)


def _sync(data_dir: Path, days: int | None, full_history: bool = False, wellness_from: date | None = None,
          notify_level: str = "off", readiness_note: bool = False) -> int:
    from . import notify, readiness

    conn = db.connect(data_dir)
    db.migrate(conn)
    before = _sync_snapshot(conn)
    readiness_before = readiness.latest(conn, datetime.now()) if readiness_note else None
    try:
        code, reason = _sync_run(conn, data_dir, days, full_history, wellness_from)
    except Exception as e:
        # A crash still deserves a notice when nobody is watching; the traceback goes to the log.
        message = notify.problem_message(1, f"{type(e).__name__}. See logs/sync.log in the data folder.",
                                         notify_level)
        if message:
            notify.post(message)
        raise
    if code == 0:
        message = notify.success_message(_sync_news(conn, before), notify_level)
    else:
        message = notify.problem_message(code, reason, notify_level)
    if message:
        notify.post(message)
    # A second notification, after the sync report, so each stays short enough for a banner.
    if code == 0 and readiness_note:
        _readiness_note(conn, readiness_before)
    return code


def _clock(value: str) -> tuple[int, int]:
    try:
        t = datetime.strptime(value, "%H:%M")
    except ValueError:
        raise argparse.ArgumentTypeError("must be HH:MM, e.g. 09:00") from None
    return t.hour, t.minute


_NOTIFY_SUMMARY = {"off": "no sync notifications", "errors": "notifies on errors", "new": "notifies when new data arrives",
                   "always": "notifies after every run"}


def _daily_sync_summary() -> str:
    from . import schedule

    job = schedule.installed()
    if not job:
        return "off"
    return (f"every day at {job.hour:02d}:{job.minute:02d}, {_NOTIFY_SUMMARY[job.notify]}, "
            f"readiness note {'on' if job.readiness else 'off'}")


def _schedule_off() -> int:
    from . import schedule

    print("Daily sync stopped." if schedule.remove() else "No daily sync was scheduled.")
    return 0


def _schedule(data_dir: Path, at: tuple[int, int] | None, notify_level: str | None,
              readiness: str | None = None) -> int:
    from . import config, schedule

    job = schedule.installed() if sys.platform == "darwin" else None
    hour, minute = at or ((job.hour, job.minute) if job else (9, 0))
    notify_level = notify_level or (job.notify if job else schedule.DEFAULT_NOTIFY)
    readiness_note = readiness == "on" if readiness else (job.readiness if job else True)
    if sys.platform != "darwin":
        print("schedule uses launchd, which is macOS only. Add this line with crontab -e instead:", file=sys.stderr)
        print(schedule.cron_line(data_dir, hour, minute), file=sys.stderr)
        return 1
    # Creates the data folder owner-only if this runs before the first sync.
    db.connect(data_dir).close()
    try:
        schedule.install(data_dir, hour, minute, notify_level, readiness_note)
    except subprocess.CalledProcessError as e:
        print(f"launchctl refused the job: {(e.stderr or '').strip() or e}", file=sys.stderr)
        return 1
    print(f"minou-sync sync will run every day at {hour:02d}:{minute:02d}, or when the Mac wakes if it was asleep "
          f"({_NOTIFY_SUMMARY[notify_level]}).")
    print("A second notification gives Garmin's readiness and recovery for the day. Turn it off with: "
          "minou-sync schedule --readiness off" if readiness_note else
          "The readiness note is off. Turn it on with: minou-sync schedule --readiness on")
    if notify_level != "off" or readiness_note:
        print("macOS may ask once to allow notifications from Script Editor, which posts them for minou-sync.")
    for warning in config.location_warnings(data_dir):
        print(f"Warning: {warning}", file=sys.stderr)
    print(f"Log: {data_dir / schedule.LOG_NAME}. Stop it with: minou-sync schedule --off")
    return 0


def _mcp(data_dir: Path, http: bool, port: int, allow_sql: bool) -> int:
    from . import mcp_server

    # Make sure the archive exists and is at the current schema before serving.
    conn = db.connect(data_dir)
    db.migrate(conn)
    conn.close()
    mcp_server.serve(data_dir, http=http, port=port, allow_sql=allow_sql)
    return 0


def _interactive() -> bool:
    # Scheduled runs, MCP clients and pipes never get a question they can't answer.
    return sys.stdin.isatty() and sys.stdout.isatty()


def _tilde(path: Path) -> str:
    home = Path.home()
    return f"~/{path.relative_to(home)}" if path.is_relative_to(home) and path != home else str(path)


def _confirm(question: str, default: bool) -> bool:
    answer = input(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return default if not answer else answer in ("y", "yes")


def _ask_folder(default: Path) -> Path:
    from . import config

    while True:
        answer = input(f"Data folder [{_tilde(default)}]: ").strip()
        folder = Path(answer).expanduser().resolve() if answer else default
        if folder.exists() and not folder.is_dir():
            print(f"{folder} is a file. Choose a folder.")
            continue
        warnings = config.location_warnings(folder)
        for warning in warnings:
            print(f"Warning: {warning}")
        if warnings and not _confirm(f"Use {_tilde(folder)} anyway?", default=False):
            continue
        return folder


def _data_location(flag: Path | None, command: str):
    """The data folder for this run. On the first interactive run, asks where it should go and saves the answer."""
    from . import config

    location = config.resolve_data_dir(flag)
    if location.source != "default" or command == "mcp" or not _interactive():
        return location
    print("minou-sync keeps your archive (activities, original files and daily wellness) in one folder on this "
          "computer. Where should it go? Press Enter for the default.")
    folder = _ask_folder(location.path)
    config.save_data_dir(folder, config.config_path())
    print(f"Saved to {_tilde(config.config_path())}. Change it any time with: minou-sync setup\n")
    return config.DataLocation(folder, "config")


def _move_archive(source: Path, target: Path) -> str | None:
    """Move the whole data folder. Returns why it couldn't, or None."""
    import shutil
    import sqlite3

    if target.exists() and any(target.iterdir()):
        return f"{target} already has files in it. Choose a new or empty folder."
    if target.is_relative_to(source):
        return f"{target} is inside the current data folder. Choose a folder outside it."
    # A sync, import or note being written holds the write lock; moving the files under it would lose that work.
    conn = sqlite3.connect(source / "archive.db", timeout=1)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
    except sqlite3.OperationalError:
        return "Something is writing to the archive (a sync, import or backfill). Try again when it finishes."
    finally:
        conn.close()
    if target.exists():
        target.rmdir()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(source, target)
    target.chmod(0o700)
    return None


def _setup(flag: Path | None, folder: Path | None, move: bool) -> int:
    from . import config, schedule

    # The folder in effect for this run is the one to move from, so `--data-dir old setup new --move` works.
    current = config.resolve_data_dir(flag)
    if current.source == "env":
        print(f"{config.ENV_VAR} is set, so it will keep overriding the saved folder until you unset it.")
    print(f"Data folder: {current.path}" + (" (the default)" if current.source == "default" else ""))
    if folder is None:
        if not _interactive():
            print("setup needs a terminal to ask, or a folder: minou-sync setup <folder>", file=sys.stderr)
            return 1
        folder = _ask_folder(current.path)
    else:
        folder = folder.expanduser().resolve()
        if folder.exists() and not folder.is_dir():
            print(f"{folder} is a file. Choose a folder.", file=sys.stderr)
            return 1
        for warning in config.location_warnings(folder):
            print(f"Warning: {warning}", file=sys.stderr)
    if folder != current.path and config.is_archive(current.path):
        if not move and _interactive():
            move = _confirm(f"Move your archive from {_tilde(current.path)} to {_tilde(folder)}?", default=True)
        if move:
            problem = _move_archive(current.path, folder)
            if problem:
                print(problem, file=sys.stderr)
                return 1
            print(f"Moved your archive to {folder}. Restart any MCP client that has it open.")
        else:
            print(f"Your archive stays in {current.path}; minou-sync starts a new one in {folder}.")
    config.save_data_dir(folder, config.config_path())
    print(f"Saved: minou-sync keeps its data in {folder} (set in {_tilde(config.config_path())}).")
    job = schedule.installed() if sys.platform == "darwin" else None
    if job and folder != current.path:
        schedule.install(folder, job.hour, job.minute, job.notify, job.readiness)
        print("The daily sync now uses the new folder.")
    return 0


def _login() -> int:
    # Imported here so commands that never touch Garmin don't load its HTTP stack.
    from . import garmin

    if not sys.stdin.isatty():
        print("minou-sync login needs a terminal to ask for your password and MFA code.", file=sys.stderr)
        return 1
    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password (not saved): ")
    garmin.login(email, password, prompt_mfa=lambda: input("MFA code: ").strip())
    print(f"Logged in. Tokens saved to {garmin.TOKEN_DIR} (owner-only); your password wasn't stored.")
    return 0


def main(argv: list[str] | None = None) -> int:
    from . import __version__, config

    parser = argparse.ArgumentParser(
        prog="minou-sync", description="Local archive of Garmin and Strava activities and Garmin wellness.",
        epilog=f"Data folder: --data-dir, else ${config.ENV_VAR}, else {_tilde(config.config_path())}, "
               f"else {_tilde(config.default_data_dir())}. Change it with: minou-sync setup")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--data-dir", type=Path, help="data folder for this run (overrides the saved one)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log details")
    sub = parser.add_subparsers(dest="command", required=True)
    setup_parser = sub.add_parser("setup", help="choose where the archive is kept, and move it there")
    setup_parser.add_argument("folder", nargs="?", type=Path, help="the new data folder; asked for if left out")
    setup_parser.add_argument("--move", action="store_true", help="move the existing archive without asking")
    sub.add_parser("login", help="log in to Garmin once (email, password, MFA); saves tokens only")
    sub.add_parser("status", help="counts, date range and archive size")
    garmin_parser = sub.add_parser(
        "import-garmin", help="load a Garmin account export (zip or unpacked folder); fills what sync hasn't stored")
    garmin_parser.add_argument("path", type=Path, help="the export zip from Garmin, or its unpacked folder")
    strava_parser = sub.add_parser("import-strava", help="load a Strava account archive (zip or unpacked folder)")
    strava_parser.add_argument("path", type=Path, help="the export_<id>.zip from Strava, or its unpacked folder")
    import_parser = sub.add_parser(
        "import", help="load a CSV, folder or zip from any platform through a profile (see docs/import.md)")
    import_parser.add_argument("path", type=Path, help="a CSV file, a folder or a zip of exports")
    import_parser.add_argument("--source", required=True, help="name for this source, e.g. nrc or intervals")
    import_parser.add_argument("--profile", help="profile name or .toml path; detected from the header if omitted")
    reconcile_parser = sub.add_parser("reconcile", help="merge every source's records into activities")
    reconcile_parser.add_argument(
        "--source-order", help="comma-separated source names to set precedence after Garmin and Strava, "
        "e.g. intervals,nrc (default: alphabetical)")
    sync_parser = sub.add_parser("sync", help="fetch recent Garmin activities, files and wellness, then reconcile")
    sync_parser.add_argument("--days", type=_days, help="fetch the last N days (1 to 90) instead of what's new")
    sync_parser.add_argument("--notify", choices=("off", "errors", "new", "always"), default="off",
                             help="desktop notification: on errors, when new data arrived, or always (default off)")
    sync_parser.add_argument("--readiness", action="store_true",
                             help="also post a readiness note when Garmin has a new readiness score")
    backfill_parser = sub.add_parser(
        "backfill", help="fetch the whole Garmin history in runs of up to 500 requests; rerun to continue")
    backfill_parser.add_argument("--from", dest="wellness_from", type=date.fromisoformat, metavar="YYYY-MM-DD",
                                 help="also fetch daily data from this date, if it's before the first activity")
    schedule_parser = sub.add_parser("schedule", help="run sync every day (macOS launchd); --off to stop")
    # Left out, each keeps what the installed job has, so turning one setting off doesn't reset the others.
    schedule_parser.add_argument("--at", type=_clock, metavar="HH:MM",
                                 help="local time to run (default 09:00)")
    schedule_parser.add_argument("--notify", choices=("off", "errors", "new", "always"),
                                 help="notify when new data arrived (default), on errors only, always, or never")
    schedule_parser.add_argument("--readiness", choices=("on", "off"),
                                 help="a second notification with Garmin's readiness and recovery (default on)")
    schedule_parser.add_argument("--off", action="store_true", help="stop the daily sync")
    efforts_parser = sub.add_parser(
        "efforts", help="compute best efforts (fastest distances, best power) from the original files")
    efforts_parser.add_argument("--all", action="store_true", dest="whole",
                                help="recompute the whole archive, not only files without efforts yet")
    sub.add_parser("export-csv", help="write activities.csv and daily_wellness.csv to <data-dir>/exports")
    mcp_parser = sub.add_parser("mcp", help="serve the archive to MCP clients (Claude, Cursor, Open WebUI)")
    mcp_parser.add_argument("--http", action="store_true", help="serve Streamable HTTP on 127.0.0.1 instead of stdio")
    mcp_parser.add_argument("--port", type=int, default=8766, help="port for --http (default 8766)")
    mcp_parser.add_argument("--sql", action="store_true", dest="allow_sql",
                            help="also offer the read-only query_sql tool (off by default)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    try:
        if args.command == "login":
            return _login()
        if args.command == "setup":
            return _setup(args.data_dir, args.folder, args.move)
        if args.command == "schedule" and args.off:
            return _schedule_off()
        location = _data_location(args.data_dir, args.command)
        data_dir = location.path
        if args.command == "status":
            return _status(data_dir, location.source)
        if args.command == "import-garmin":
            return _import_garmin(data_dir, args.path)
        if args.command == "import-strava":
            return _import_strava(data_dir, args.path)
        if args.command == "import":
            return _import(data_dir, args.path, args.source, args.profile)
        if args.command == "sync":
            return _sync(data_dir, args.days, notify_level=args.notify, readiness_note=args.readiness)
        if args.command == "backfill":
            return _sync(data_dir, None, full_history=True, wellness_from=args.wellness_from)
        if args.command == "schedule":
            return _schedule(data_dir, args.at, args.notify, args.readiness)
        if args.command == "efforts":
            return _efforts(data_dir, args.whole)
        if args.command == "export-csv":
            return _export_csv(data_dir)
        if args.command == "mcp":
            return _mcp(data_dir, args.http, args.port, args.allow_sql)
        order = [s.strip() for s in args.source_order.split(",")] if args.source_order else None
        return _reconcile(data_dir, order)
    except config.ConfigError as e:
        print(e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # The conventional exit code for Ctrl-C, without a traceback.
        print(file=sys.stderr)
        return 130
    except db.SchemaTooNewError as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
