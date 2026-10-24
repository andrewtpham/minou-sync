"""The edit layer: edit, hide, create, undo, history, and a vanished source record. Made-up data only."""

import pytest

from minou_sync import edits, reconcile
from minou_sync.edits import ActivityChange


def _garmin(conn, garmin_id, start="2026-10-01T06:00:00Z", **extra):
    row = {"garmin_id": garmin_id, "activity_type": "running", "name": "Run", "start_utc": start,
           "elapsed_s": 3000, "origin": "sync", "raw_json": "{}", "imported_utc": "x", **extra}
    cols = ", ".join(row)
    conn.execute(f"INSERT INTO garmin_activities ({cols}) VALUES ({', '.join('?' for _ in row)})", list(row.values()))


def _reconcile(conn, data_dir):
    return reconcile.run(conn, data_dir / "reports")


def test_edit_sets_the_field_and_marks_edited(conn, data_dir):
    _garmin(conn, 1)
    conn.commit()
    _reconcile(conn, data_dir)
    results = edits.edit_activities(conn, [ActivityChange("garmin:1", "name", "Fixed title")])
    assert results[0].ok
    row = conn.execute("SELECT name, edited FROM activities WHERE key='garmin:1'").fetchone()
    assert row["name"] == "Fixed title" and row["edited"] == 1


def test_edit_survives_reconcile(conn, data_dir):
    """A correction stays applied after a rebuild."""
    _garmin(conn, 1)
    conn.commit()
    _reconcile(conn, data_dir)
    edits.edit_activities(conn, [ActivityChange("garmin:1", "name", "Fixed title")])
    _reconcile(conn, data_dir)
    _reconcile(conn, data_dir)
    row = conn.execute("SELECT name, edited FROM activities WHERE key='garmin:1'").fetchone()
    assert row["name"] == "Fixed title" and row["edited"] == 1


def test_edit_rejects_unknown_key_and_field(conn, data_dir):
    _garmin(conn, 1)
    conn.commit()
    _reconcile(conn, data_dir)
    r = edits.edit_activities(conn, [
        ActivityChange("garmin:999", "name", "x"),
        ActivityChange("garmin:1", "id", "x"),
    ])
    assert not r[0].ok and "no activity" in r[0].error
    assert not r[1].ok and "editable" in r[1].error


def test_hide_removes_the_row_and_keeps_the_source(conn, data_dir):
    _garmin(conn, 1)
    conn.commit()
    _reconcile(conn, data_dir)
    assert edits.hide(conn, ["garmin:1"]) == 1
    assert conn.execute("SELECT COUNT(*) FROM activities WHERE key='garmin:1'").fetchone()[0] == 0
    # Source untouched.
    assert conn.execute("SELECT COUNT(*) FROM garmin_activities WHERE garmin_id=1").fetchone()[0] == 1
    # Stays hidden after a rebuild.
    _reconcile(conn, data_dir)
    assert conn.execute("SELECT COUNT(*) FROM activities WHERE key='garmin:1'").fetchone()[0] == 0


def test_create_manual_appears_after_rebuild(conn, data_dir):
    key = edits.create_manual(conn, {"start_utc": "2026-10-05T07:00:00Z", "activity_type": "yoga",
                                     "name": "Evening yoga", "elapsed_s": 1800})
    assert key.startswith("manual:")
    row = conn.execute("SELECT source, activity_type, name, elapsed_s FROM activities WHERE key=?", (key,)).fetchone()
    assert tuple(row) == ("manual", "yoga", "Evening yoga", 1800)
    _reconcile(conn, data_dir)
    row = conn.execute("SELECT source, name, elapsed_s FROM activities WHERE key=?", (key,)).fetchone()
    assert tuple(row) == ("manual", "Evening yoga", 1800)


def test_create_manual_needs_start(conn):
    with pytest.raises(ValueError, match="start_utc"):
        edits.create_manual(conn, {"name": "x"})


def test_undo_restores_the_source_value(conn, data_dir):
    """Undoing an edit restores the source value."""
    _garmin(conn, 1, name="Original")
    conn.commit()
    _reconcile(conn, data_dir)
    edits.edit_activities(conn, [ActivityChange("garmin:1", "name", "Edited")])
    edit_id = edits.history(conn, "garmin:1")[0].id
    edits.undo(conn, edit_id)
    _reconcile(conn, data_dir)
    row = conn.execute("SELECT name, edited FROM activities WHERE key='garmin:1'").fetchone()
    assert row["name"] == "Original" and row["edited"] == 0


def test_history_lists_edits_newest_first(conn, data_dir):
    _garmin(conn, 1)
    conn.commit()
    _reconcile(conn, data_dir)
    edits.edit_activities(conn, [ActivityChange("garmin:1", "name", "First")])
    edits.edit_activities(conn, [ActivityChange("garmin:1", "distance_m", 5000.0)])
    h = edits.history(conn, "garmin:1")
    assert [e.field for e in h] == ["distance_m", "name"]
    assert h[0].value == 5000.0


def test_vanished_source_record_is_skipped_and_reported(conn, data_dir):
    _garmin(conn, 1)
    conn.commit()
    _reconcile(conn, data_dir)
    edits.edit_activities(conn, [ActivityChange("garmin:1", "name", "Fixed")])
    # The Garmin record disappears (e.g. deleted upstream); its key no longer rebuilds. The derived activities
    # row (which references it) is removed first, as reconcile's rebuild would.
    conn.execute("DELETE FROM activities WHERE garmin_id=1")
    conn.execute("DELETE FROM garmin_activities WHERE garmin_id=1")
    conn.commit()
    report = _reconcile(conn, data_dir)
    assert "garmin:1:name" in report.vanished_edits
    assert conn.execute("SELECT COUNT(*) FROM activities WHERE key='garmin:1'").fetchone()[0] == 0


def test_latest_edit_of_a_field_wins(conn, data_dir):
    _garmin(conn, 1)
    conn.commit()
    _reconcile(conn, data_dir)
    edits.edit_activities(conn, [ActivityChange("garmin:1", "name", "First")])
    edits.edit_activities(conn, [ActivityChange("garmin:1", "name", "Second")])
    _reconcile(conn, data_dir)
    assert conn.execute("SELECT name FROM activities WHERE key='garmin:1'").fetchone()[0] == "Second"
