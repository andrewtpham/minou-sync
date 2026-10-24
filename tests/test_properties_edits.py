"""Edit-layer properties 8, 9 and 10. Made-up data only.

8  Edits survive rebuilds: for any edit set, reconciling any number of times leaves every non-undone edit applied.
9  Undo restores the source value: after undoing an edit, the field equals its source value at the next rebuild.
10 Imports never change edited records: a row with edited_utc set is unchanged by any re-import.
"""

import csv
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from minou_sync import db, edits, importer, reconcile
from minou_sync.edits import ActivityChange

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _fresh(tmp_path):
    data_dir = tmp_path / "data"
    conn = db.connect(data_dir)
    db.migrate(conn)
    return conn, data_dir


@st.composite
def garmin_activities(draw):
    n = draw(st.integers(1, 8))
    rows = []
    for i in range(1, n + 1):
        start = T0 + timedelta(seconds=draw(st.integers(0, 3 * 365 * 86400)) + i)  # +i keeps starts distinct
        rows.append((i, start.replace(microsecond=0).isoformat().replace("+00:00", "Z")))
    return rows


def _seed(conn, rows):
    for garmin_id, start in rows:
        conn.execute(
            "INSERT INTO garmin_activities (garmin_id, activity_type, name, start_utc, elapsed_s, origin, "
            "raw_json, imported_utc) VALUES (?, 'running', 'Original', ?, 3000, 'sync', '{}', 'x')",
            (garmin_id, start))
    conn.commit()


names = st.text(min_size=1, max_size=12).filter(lambda s: s.strip())


@settings(max_examples=100, deadline=None)
@given(garmin_activities(), st.data())
def test_edits_survive_rebuilds(rows, data):
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        conn, data_dir = _fresh(Path(tmp))
        _seed(conn, rows)
        reconcile.run(conn, data_dir / "reports")
        keys = [f"garmin:{gid}" for gid, _ in rows]
        chosen = data.draw(st.lists(st.sampled_from(keys), max_size=len(keys), unique=True))
        wanted = {}
        for key in chosen:
            name = data.draw(names)
            edits.edit_activities(conn, [ActivityChange(key, "name", name)])
            wanted[key] = name
        for _ in range(data.draw(st.integers(1, 3))):
            reconcile.run(conn, data_dir / "reports")
        for key, name in wanted.items():
            row = conn.execute("SELECT name, edited FROM activities WHERE key = ?", (key,)).fetchone()
            assert row["name"] == name and row["edited"] == 1
        conn.close()


@settings(max_examples=100, deadline=None)
@given(garmin_activities(), st.data())
def test_undo_restores_the_source_value(rows, data):
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        conn, data_dir = _fresh(Path(tmp))
        _seed(conn, rows)
        reconcile.run(conn, data_dir / "reports")
        key = f"garmin:{data.draw(st.sampled_from([gid for gid, _ in rows]))}"
        edits.edit_activities(conn, [ActivityChange(key, "name", data.draw(names))])
        edits.undo(conn, edits.history(conn, key)[0].id)
        reconcile.run(conn, data_dir / "reports")
        row = conn.execute("SELECT name, edited FROM activities WHERE key = ?", (key,)).fetchone()
        assert row["name"] == "Original" and row["edited"] == 0
        conn.close()


weights = st.floats(min_value=40, max_value=150, allow_nan=False).map(lambda f: round(f, 1))


@settings(max_examples=100, deadline=None)
@given(st.lists(st.tuples(st.integers(0, 600), weights), min_size=1, max_size=6, unique_by=lambda t: t[0]),
       st.data())
def test_imports_never_change_edited_records(days, data):
    """An edited weigh-in is untouched by a re-import that would otherwise change it."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        conn, data_dir = _fresh(Path(tmp))
        header = ["measured_at", "weight_kg"]
        rows = [[(datetime(2025, 1, 1) + timedelta(days=d)).isoformat() + "Z", str(w)] for d, w in days]
        buf = io.StringIO()
        csv.writer(buf).writerows([header, *rows])
        path = Path(tmp) / "w.csv"
        path.write_text(buf.getvalue())
        importer.run(conn, path, "scale", data_dir)

        # The athlete edits one weigh-in: a new weight and an edited_utc stamp.
        all_measured = [r[0] for r in conn.execute("SELECT measured_utc FROM weigh_ins ORDER BY measured_utc")]
        target = all_measured[data.draw(st.integers(0, len(all_measured) - 1))]
        conn.execute("UPDATE weigh_ins SET weight_kg = 99.9, edited_utc = '2026-10-20T00:00:00Z' "
                     "WHERE measured_utc = ?", (target,))
        conn.commit()

        # Re-import the original CSV (which has the old weight for that day).
        importer.run(conn, path, "scale", data_dir)
        kept = conn.execute("SELECT weight_kg, edited_utc FROM weigh_ins WHERE measured_utc = ?", (target,)).fetchone()
        assert kept["weight_kg"] == 99.9 and kept["edited_utc"] == "2026-10-20T00:00:00Z"
        conn.close()
