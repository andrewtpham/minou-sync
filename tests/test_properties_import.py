"""Importing a Strava export twice leaves every table identical to importing it once, and the
same for a Garmin account export.
The same holds for the general importer, for any source.
"""

import csv
import io
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from minou_sync import db, import_garmin, import_strava, importer

from test_import_garmin import activity, write_export
from test_import_strava import strava_row, write_archive
from test_importer import ACTIVITY_HEADER, WELLNESS_HEADER, BODY_HEADER, LAB_HEADER, make_fit

TABLES = ("strava_activities", "files")

numbers = st.one_of(
    st.just(""), st.just("n/a"), st.floats(min_value=0, max_value=1e6, allow_nan=False).map(lambda f: f"{f:.1f}")
)


@st.composite
def archives(draw):
    ids = draw(st.lists(st.integers(min_value=1, max_value=10**11), min_size=1, max_size=15, unique=True))
    rows = []
    for i in ids:
        start = datetime(2017, 1, 1) + timedelta(seconds=draw(st.integers(0, 10 * 365 * 86400)))
        date = draw(st.sampled_from([start.strftime("%b %-d, %Y, %-I:%M:%S %p"), "garbled"]))
        filename = draw(st.sampled_from(["", f"activities/{i}.fit.gz", f"activities/{i}.tcx.gz"]))
        rows.append(strava_row(i, date=date, filename=filename, distance=draw(numbers), avg_hr=draw(numbers),
                               type_=draw(st.sampled_from(["Ride", "Run", "Weight Training", "Rock Climb"]))))
    return rows


def dump(conn):
    return {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in TABLES}


@settings(max_examples=100, deadline=None)
@given(archives())
def test_strava_import_is_idempotent(rows):
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        archive = write_archive(tmp / "export.zip", rows)
        data_dir = tmp / "data"
        conn = db.connect(data_dir)
        db.migrate(conn)
        import_strava.run(conn, archive, data_dir)
        once = [tuple(r) for t in dump(conn).values() for r in t]
        files_once = sorted(p.read_bytes() for p in (data_dir / "files").rglob("*") if p.is_file())
        counts = import_strava.run(conn, archive, data_dir)
        assert [tuple(r) for t in dump(conn).values() for r in t] == once
        assert sorted(p.read_bytes() for p in (data_dir / "files").rglob("*") if p.is_file()) == files_once
        assert counts.added == counts.updated == counts.files == 0
        conn.close()


# --- The general importer is idempotent for any source ---------------------------------------------------

GENERAL_TABLES = ("source_activities", "daily_wellness", "weigh_ins", "body_composition", "lab_tests", "files")

opt_num = st.one_of(st.just(""), st.just("n/a"),
                    st.floats(min_value=0, max_value=1e5, allow_nan=False).map(lambda f: f"{f:.1f}"))
opt_int = st.one_of(st.just(""), st.integers(min_value=0, max_value=10000).map(str))


def _iso(dt):
    return dt.replace(microsecond=0).isoformat()


@st.composite
def activity_rows(draw):
    rows = []
    for _ in range(draw(st.integers(0, 6))):
        start = datetime(2024, 1, 1) + timedelta(seconds=draw(st.integers(0, 3 * 365 * 86400)))
        rows.append([
            draw(st.sampled_from(["", "a" + str(draw(st.integers(1, 10**6)))])),  # id, sometimes blank
            draw(st.sampled_from(["Run", "Ride", "Swim", "Yoga"])),
            draw(st.sampled_from(["", "Session"])),
            _iso(start), draw(opt_int), draw(opt_num), draw(opt_int),
        ])
    return rows


@st.composite
def wellness_rows(draw):
    rows, used = [], set()
    for _ in range(draw(st.integers(0, 6))):
        day = (datetime(2025, 1, 1) + timedelta(days=draw(st.integers(0, 600)))).date().isoformat()
        if day in used:
            continue
        used.add(day)
        rows.append([day, draw(opt_int), draw(opt_int), draw(opt_int)])
    return rows


@st.composite
def body_rows(draw):
    rows, used = [], set()
    for _ in range(draw(st.integers(0, 4))):
        day = (datetime(2025, 1, 1) + timedelta(days=draw(st.integers(0, 600)))).date().isoformat()
        method = draw(st.sampled_from(["bia", "dexa", "DEXA", "other"]))
        key = (day, method.lower())
        if key in used:
            continue
        used.add(key)
        rows.append([day, method, draw(opt_num), draw(opt_num)])
    return rows


@st.composite
def lab_rows(draw):
    rows, used = [], set()
    for _ in range(draw(st.integers(0, 4))):
        day = (datetime(2025, 1, 1) + timedelta(days=draw(st.integers(0, 600)))).date().isoformat()
        sport = draw(st.sampled_from(["run", "bike", ""]))
        if (day, sport) in used:
            continue
        used.add((day, sport))
        rows.append([day, sport, draw(opt_int), draw(opt_int)])
    return rows


def _write(folder, name, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    (folder / name).write_text(buf.getvalue())


def _dump(conn):
    return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")] for t in GENERAL_TABLES}


def _files_on_disk(data_dir):
    return sorted(p.read_bytes() for p in (data_dir / "files").rglob("*") if p.is_file())


@settings(max_examples=100, deadline=None)
@given(activity_rows(), wellness_rows(), body_rows(), lab_rows(),
       st.lists(st.integers(0, 5 * 365 * 86400), min_size=0, max_size=4))
def test_general_import_is_idempotent(activities, wellness, body, labs, fit_offsets):
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "export"
        folder.mkdir()
        if activities:
            _write(folder, "activities.csv", ACTIVITY_HEADER, activities)
        if wellness:
            _write(folder, "wellness.csv", WELLNESS_HEADER, wellness)
        if body:
            _write(folder, "body.csv", BODY_HEADER, body)
        if labs:
            _write(folder, "labs.csv", LAB_HEADER, labs)
        for i, offset in enumerate(fit_offsets):
            start = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=offset)
            (folder / f"act{i}.fit").write_bytes(make_fit(start, elapsed_s=1800 + i, distance_m=5000.0 + i))

        data_dir = Path(tmp) / "data"
        conn = db.connect(data_dir)
        db.migrate(conn)
        importer.run(conn, folder, "src", data_dir)
        once, files_once = _dump(conn), _files_on_disk(data_dir)
        counts = importer.run(conn, folder, "src", data_dir)
        assert _dump(conn) == once
        assert _files_on_disk(data_dir) == files_once
        assert counts.added == counts.updated == counts.files == 0
        conn.close()


garmin_values = st.one_of(st.none(), st.just("n/a"), st.floats(min_value=0, max_value=1e9, allow_nan=False))


@st.composite
def garmin_exports(draw):
    ids = draw(st.lists(st.integers(min_value=1, max_value=10**11), min_size=1, max_size=12, unique=True))
    acts = []
    for i in ids:
        start = datetime(2020, 1, 1) + timedelta(seconds=draw(st.integers(0, 6 * 365 * 86400)))
        extra = {k: draw(garmin_values) for k in ("distance", "avgHr", "calories", "elapsedDuration")}
        if draw(st.booleans()):
            extra["startTimeGmt"] = "garbled"  # no usable start time: the row fails, the import goes on
            extra["beginTimestamp"] = None
        acts.append(activity(i, start.timetuple()[:6], type_=draw(st.sampled_from(["running", "cycling"])),
                             **extra))
    return acts


GARMIN_TABLES = ("garmin_activities", "files", "daily_wellness", "weigh_ins")


@settings(max_examples=100, deadline=None)
@given(garmin_exports())
def test_garmin_import_is_idempotent(acts):
    def dump(conn):
        # updated_utc changes on every write, so it's left out of the comparison.
        rows = {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1")] for t in GARMIN_TABLES}
        return {t: [{k: v for k, v in r.items() if k != "updated_utc"} for r in rs] for t, rs in rows.items()}

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        export = write_export(tmp / "garmin.zip", activities=acts)
        data_dir = tmp / "data"
        conn = db.connect(data_dir)
        db.migrate(conn)
        import_garmin.run(conn, export, data_dir)
        once = dump(conn)
        files_once = _files_on_disk(data_dir)
        counts = import_garmin.run(conn, export, data_dir)
        assert dump(conn) == once
        assert _files_on_disk(data_dir) == files_once
        assert counts.added == counts.updated == counts.files == counts.wellness_days == counts.weigh_ins == 0
        conn.close()
