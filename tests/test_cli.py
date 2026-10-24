import csv

from minou_sync import cli, db, export

# Made-up values; never real activity or health data.
ACTIVITIES = [
    dict(garmin_id=101, strava_id=201, source="both", activity_type="ride", name="Morning ride",
         start_utc="2026-10-02T06:00:00Z", elapsed_s=3600, avg_hr=140),
    dict(garmin_id=102, strava_id=None, source="garmin", activity_type="strength", name=None,
         start_utc="2026-10-01T17:00:00Z", elapsed_s=1800, avg_hr=None),
    dict(garmin_id=None, strava_id=202, source="strava", activity_type="run", name="Old run, with comma",
         start_utc="2019-05-04T08:00:00Z", elapsed_s=2400, avg_hr=150),
]


def _insert(conn, table, row):
    cols = ", ".join(row)
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' for _ in row)})", list(row.values()))


def _seed(conn):
    for gid in (101, 102):
        _insert(conn, "garmin_activities", dict(garmin_id=gid, activity_type="ride", start_utc="2026-10-01T00:00:00Z",
                                                origin="export", raw_json="{}", imported_utc="2026-10-19T00:00:00Z"))
    for sid in (201, 202):
        _insert(conn, "strava_activities", dict(strava_id=sid, activity_type="Ride", start_utc="2026-10-01T00:00:00Z",
                                                raw_json="{}", imported_utc="2026-10-19T00:00:00Z"))
    for row in ACTIVITIES:
        _insert(conn, "activities", row)
    for date, rhr in (("2026-10-02", 48), ("2026-10-01", 50)):
        _insert(conn, "daily_wellness", dict(date=date, resting_hr=rhr, complete=1, updated_utc="2026-10-19T00:00:00Z"))
    # Only the Garmin side of the matched ride has a file, which still counts as having one.
    _insert(conn, "files", dict(path="files/garmin/101.fit", source="garmin", source_id=101, format="fit",
                                bytes=10, sha256="0" * 64))
    _insert(conn, "meta", dict(key="last_sync_utc", value="2026-10-18T20:00:00Z"))
    conn.commit()


def _read(path):
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_status_counts_dates_and_missing_files(conn):
    _seed(conn)
    r = export.status(conn)
    assert r.by_source == {"both": 1, "garmin": 1, "strava": 1}
    assert r.total == 3
    assert (r.first_start_utc, r.last_start_utc) == ("2019-05-04T08:00:00Z", "2026-10-02T06:00:00Z")
    assert r.last_wellness_date == "2026-10-02"
    assert r.last_sync_utc == "2026-10-18T20:00:00Z"
    assert r.without_file == 2
    assert r.db_bytes > 0


def test_status_on_empty_archive(conn):
    r = export.status(conn)
    assert r.total == 0
    assert r.first_start_utc is None and r.last_wellness_date is None and r.last_sync_utc is None
    assert r.without_file == 0


def test_export_csv_writes_both_tables_sorted(conn, data_dir):
    _seed(conn)
    paths = export.export_csv(conn, data_dir / "exports")
    assert [p.name for p in paths] == ["activities.csv", "daily_wellness.csv"]

    activities = _read(paths[0])
    assert [a["start_utc"] for a in activities] == sorted(a["start_utc"] for a in ACTIVITIES)
    assert activities[0]["name"] == "Old run, with comma"
    assert activities[1]["name"] == "" and activities[1]["avg_hr"] == ""  # NULL becomes an empty cell
    assert [w["date"] for w in _read(paths[1])] == ["2026-10-01", "2026-10-02"]
    assert not list((data_dir / "exports").glob("*.tmp"))


def test_export_csv_overwrites_previous_files(conn, data_dir):
    exports_dir = data_dir / "exports"
    exports_dir.mkdir()
    (exports_dir / "activities.csv").write_text("stale\n" * 100)
    export.export_csv(conn, exports_dir)
    assert _read(exports_dir / "activities.csv") == []
    assert (exports_dir / "activities.csv").read_text().startswith("id,key,garmin_id,strava_id,source,")


def test_cli_status_prints_summary(conn, data_dir, capsys):
    _seed(conn)
    assert cli.main(["--data-dir", str(data_dir), "status"]) == 0
    out = capsys.readouterr().out
    assert "Activities:         3 (both 1, Garmin only 1, Strava only 1)" in out
    assert "First and last:     2019-05-04T08:00:00Z to 2026-10-02T06:00:00Z" in out
    assert "Last wellness day:  2026-10-02" in out
    assert "Last sync:          2026-10-18T20:00:00Z" in out
    assert "Without a file:     2" in out


def test_cli_export_csv(conn, data_dir, capsys):
    _seed(conn)
    assert cli.main(["--data-dir", str(data_dir), "export-csv"]) == 0
    assert len(_read(data_dir / "exports" / "activities.csv")) == 3
    assert "daily_wellness.csv" in capsys.readouterr().out


def test_cli_efforts_on_empty_archive_says_up_to_date(conn, data_dir, capsys):
    assert cli.main(["--data-dir", str(data_dir), "efforts"]) == 0
    assert "already up to date" in capsys.readouterr().out.lower()


def test_cli_refuses_newer_schema(conn, data_dir, capsys):
    conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(db.SCHEMA_VERSION + 1),))
    conn.commit()
    assert cli.main(["--data-dir", str(data_dir), "export-csv"]) == 1
    assert "Update the code" in capsys.readouterr().err
