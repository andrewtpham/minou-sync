"""Best efforts from generated FIT, GPX and TCX files. All distances, times and power are invented.

The generators emit per-second track points with cumulative distance (and power for rides), so a known pace
makes a known split and the computed effort can be checked to the second.
"""

import math
import struct
from datetime import datetime, timezone

from minou_sync import db, efforts

START = datetime(2026, 10, 1, 6, 0, 0, tzinfo=timezone.utc)
FIT_EPOCH = 631065600


def _crc(data: bytes) -> int:
    table = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
             0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]
    crc = 0
    for byte in data:
        for nibble in (byte & 0xF, (byte >> 4) & 0xF):
            tmp = table[crc & 0xF]
            crc = (crc >> 4) & 0x0FFF
            crc = crc ^ tmp ^ table[nibble]
    return crc


def make_fit_records(samples, with_power=False, with_position=False):
    """A FIT file of record messages. samples: list of (t_offset_s, cumulative_distance_m, power|None,
    lat|None, lon|None). Emits a definition then one data message per sample."""
    fields = [(253, 4, 0x86), (5, 4, 0x86)]  # timestamp, distance (uint32)
    if with_power:
        fields.append((7, 2, 0x84))          # power (uint16)
    if with_position:
        fields += [(0, 4, 0x85), (1, 4, 0x85)]  # position_lat, position_long (sint32)

    definition = struct.pack("<BBBHB", 0x40, 0, 0, 20, len(fields))  # global msg 20 = record
    for num, size, base in fields:
        definition += struct.pack("<BBB", num, size, base)

    body = definition
    for t, dist, power, lat, lon in samples:
        ts = int(START.timestamp()) - FIT_EPOCH + int(t)
        msg = struct.pack("<B", 0)  # header, local type 0
        msg += struct.pack("<II", ts, round(dist * 100))
        if with_power:
            msg += struct.pack("<H", int(power or 0))
        if with_position:
            sc = 2**31 / 180.0
            msg += struct.pack("<ii", int((lat or 0) * sc), int((lon or 0) * sc))
        body += msg

    header = struct.pack("<BBHI4s", 12, 0x10, 2140, len(body), b".FIT")
    full = header + body
    return full + struct.pack("<H", _crc(full))


def _offset_latlon(dist_m):
    """A lat/lon north of a base point by dist_m, for haversine-based GPX."""
    base_lat, base_lon = 52.5, 13.4
    return base_lat + (dist_m / 111320.0), base_lon


def make_gpx(samples):
    """samples: list of (t_offset_s, cumulative_distance_m). Positions are derived so haversine recovers it."""
    pts = []
    for t, dist in samples:
        lat, lon = _offset_latlon(dist)
        ts = datetime.fromtimestamp(START.timestamp() + t, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        pts.append(f'<trkpt lat="{lat}" lon="{lon}"><time>{ts}</time></trkpt>')
    return ('<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>'
            + "".join(pts) + '</trkseg></trk></gpx>').encode()


def make_tcx(samples):
    """samples: list of (t_offset_s, cumulative_distance_m)."""
    tps = []
    for t, dist in samples:
        ts = datetime.fromtimestamp(START.timestamp() + t, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        tps.append(f'<Trackpoint><Time>{ts}</Time><DistanceMeters>{dist}</DistanceMeters></Trackpoint>')
    return ('<?xml version="1.0"?>'
            '<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">'
            '<Activities><Activity Sport="Running"><Id>x</Id><Lap><Track>'
            + "".join(tps) + '</Track></Lap></Activity></Activities></TrainingCenterDatabase>').encode()


def _steady_run(total_m, pace_s_per_m):
    """One sample per second at constant pace, covering total_m."""
    samples = []
    t = 0.0
    while True:
        dist = t / pace_s_per_m
        if dist > total_m:
            samples.append((t, float(total_m)))
            break
        samples.append((t, dist))
        t += 1
    return samples


# --- compute() on each format ---------------------------------------------------------------------------

def test_fit_run_distance_efforts_within_a_second(tmp_path):
    # 6 km at 4:00/km (0.24 s/m) -> 1 km in 240 s, 5 km in 1200 s.
    blob = make_fit_records([(t, d, None, None, None) for t, d in _steady_run(6000, 0.24)])
    path = tmp_path / "run.fit"
    path.write_bytes(blob)
    out = efforts.compute(path, "run")
    assert abs(out["run_1km"][0] - 240) <= 1
    assert abs(out["run_5k"][0] - 1200) <= 1
    assert "run_10k" not in out  # only 6 km recorded


def test_gpx_distance_via_haversine(tmp_path):
    # 2 km at 5:00/km (0.3 s/m) -> 1 km in 300 s. Positions encode the distance, so haversine recovers it.
    path = tmp_path / "run.gpx"
    path.write_bytes(make_gpx(_steady_run(2000, 0.3)))
    out = efforts.compute(path, "run")
    # Haversine on a straight north line is within a metre or two; allow 2 s.
    assert abs(out["run_1km"][0] - 300) <= 2


def test_tcx_distance_efforts(tmp_path):
    path = tmp_path / "run.tcx"
    path.write_bytes(make_tcx(_steady_run(2000, 0.3)))
    out = efforts.compute(path, "run")
    assert abs(out["run_1km"][0] - 300) <= 1


def test_tcx_with_whitespace_in_time_and_distance(tmp_path):
    """Real Strava TCX files indent <Time> and <DistanceMeters>, so their text has newlines and spaces."""
    tps = []
    for t, dist in _steady_run(2000, 0.3):
        ts = datetime.fromtimestamp(START.timestamp() + t, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        tps.append(f"<Trackpoint>\n      <Time>\n      {ts}\n    </Time>\n"
                   f"      <DistanceMeters>\n      {dist}\n    </DistanceMeters>\n    </Trackpoint>")
    xml = ('<?xml version="1.0"?>'
           '<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">'
           '<Activities><Activity Sport="Running"><Id>x</Id><Lap><Track>'
           + "".join(tps) + '</Track></Lap></Activity></Activities></TrainingCenterDatabase>').encode()
    path = tmp_path / "run.tcx"
    path.write_bytes(xml)
    out = efforts.compute(path, "run")
    assert abs(out["run_1km"][0] - 300) <= 1


def test_ride_power_efforts(tmp_path):
    # 700 s of riding: 250 W throughout except a 300 s block at 300 W -> best 5-min power is 300 W.
    samples = []
    for t in range(701):
        dist = t * 8.0  # 28.8 km/h, well under the 60 limit
        power = 300 if 100 <= t < 400 else 250
        samples.append((t, dist, power, None, None))
    path = tmp_path / "ride.fit"
    path.write_bytes(make_fit_records(samples, with_power=True))
    out = efforts.compute(path, "ride")
    assert abs(out["power_5min"][0] - 300) <= 1
    assert "power_20min" not in out  # under 20 minutes recorded


def test_ride_40km_distance_effort(tmp_path):
    # 40 km at 10 m/s (36 km/h) -> 4000 s.
    samples = [(t, t * 10.0, 200, None, None) for t in range(4001)]
    path = tmp_path / "ride.fit"
    path.write_bytes(make_fit_records(samples, with_power=True))
    out = efforts.compute(path, "ride")
    assert abs(out["ride_40km"][0] - 4000) <= 1


# --- GPS jump is dropped --------------------------------------------------------------------------------

def test_gps_jump_does_not_shorten_the_fastest_km(tmp_path):
    # 3 km at 5:00/km (0.3 s/m). A single point jumps 2 km forward then back: a >25 km/h spike to be dropped.
    samples = _steady_run(3000, 0.3)
    clean = efforts.compute(tmp_path_fit(tmp_path, "clean.fit", samples), "run")["run_1km"][0]

    jumped = list(samples)
    i = len(jumped) // 2
    t_i, d_i = jumped[i]
    jumped[i] = (t_i, d_i + 2000.0)  # one bad point 2 km ahead; the next point returns to the real distance
    with_jump = efforts.compute(tmp_path_fit(tmp_path, "jump.fit", jumped), "run")["run_1km"][0]

    # The jump would otherwise make a ~1 s "kilometre"; dropping it keeps the real ~300 s split.
    assert abs(with_jump - clean) <= 1
    assert with_jump > 250


def tmp_path_fit(tmp_path, name, samples):
    p = tmp_path / name
    p.write_bytes(make_fit_records([(t, d, None, None, None) for t, d in samples]))
    return p


# --- idempotence and the archive run --------------------------------------------------------------------

def _seed_file(conn, data_dir, source, source_id, atype, name, samples, fmt="fit", garmin_type="running"):
    rel = f"files/{source}/{source_id}.{fmt}"
    full = data_dir / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "fit":
        full.write_bytes(make_fit_records([(t, d, None, None, None) for t, d in samples]))
    elif fmt == "gpx":
        full.write_bytes(make_gpx(samples))
    else:
        full.write_bytes(make_tcx(samples))
    conn.execute("INSERT INTO files (path, source, source_id, format, bytes, sha256) VALUES (?, ?, ?, ?, ?, ?)",
                 (rel, source, str(source_id), fmt, full.stat().st_size, "x" * 64))
    if source == "garmin":
        conn.execute("INSERT INTO garmin_activities (garmin_id, activity_type, start_utc, origin, raw_json, "
                     "imported_utc) VALUES (?, ?, '2026-10-01T06:00:00Z', 'sync', '{}', 'x')",
                     (source_id, garmin_type))
    elif source == "strava":
        conn.execute("INSERT INTO strava_activities (strava_id, activity_type, start_utc, raw_json, imported_utc) "
                     "VALUES (?, 'Run', '2026-10-01T06:00:00Z', '{}', 'x')", (source_id,))
    else:
        conn.execute("INSERT INTO source_activities (source, source_id, activity_type, start_utc, raw_json, "
                     "imported_utc) VALUES (?, ?, ?, '2026-10-01T06:00:00Z', '{}', 'x')", (source, source_id, atype))


def test_run_over_archive_is_idempotent(conn, data_dir):
    _seed_file(conn, data_dir, "strava", 100, "run", "Run", _steady_run(6000, 0.24))
    conn.commit()
    first = efforts.run(conn, data_dir, only_new=True)
    assert first.activities == 1 and first.efforts >= 2
    rows_after_first = [tuple(r) for r in conn.execute(
        "SELECT source, source_id, effort, value FROM activity_efforts ORDER BY effort")]
    # A second run over only-new files does nothing new.
    second = efforts.run(conn, data_dir, only_new=True)
    assert second.activities == 0 and second.efforts == 0
    # A whole-archive recompute gives the same values.
    efforts.run(conn, data_dir, only_new=False)
    rows_after_whole = [tuple(r) for r in conn.execute(
        "SELECT source, source_id, effort, value FROM activity_efforts ORDER BY effort")]
    assert rows_after_first == rows_after_whole


def test_run_computes_the_strava_years_for_every_source(conn, data_dir):
    # A Strava run and an NRC run, both with files, both get efforts.
    _seed_file(conn, data_dir, "strava", 100, "run", "Run", _steady_run(5000, 0.24))
    _seed_file(conn, data_dir, "nrc", "n1", "run", "Run", _steady_run(5000, 0.3), fmt="tcx")
    conn.commit()
    c = efforts.run(conn, data_dir, only_new=True)
    assert c.activities == 2
    sources = {r[0] for r in conn.execute("SELECT DISTINCT source FROM activity_efforts")}
    assert sources == {"strava", "nrc"}


def test_cross_check_counts_a_garmin_difference(conn, data_dir):
    import json
    _seed_file(conn, data_dir, "garmin", 1, "run", "Run", _steady_run(6000, 0.24))
    # Garmin claims a 5 km split far from the computed ~1200 s, so it must be counted as differing.
    detail = {"splitSummaries": [{"splitType": "DISTANCE_5000", "distance": 5000, "duration": 1000.0}]}
    conn.execute("INSERT INTO garmin_raw (endpoint, key, raw_json, fetched_utc) VALUES "
                 "('get_activity', '1', ?, 'x')", (json.dumps(detail),))
    conn.commit()
    c = efforts.run(conn, data_dir, only_new=True)
    assert c.garmin_checked == 1 and c.garmin_differs == 1


def test_progress_callback_is_called_once_per_file(conn, data_dir):
    _seed_file(conn, data_dir, "strava", 100, "run", "Run", _steady_run(5000, 0.24))
    _seed_file(conn, data_dir, "strava", 101, "run", "Run", _steady_run(5000, 0.3), fmt="tcx")
    conn.commit()
    calls = []
    efforts.run(conn, data_dir, only_new=True, progress=lambda done, total: calls.append((done, total)))
    assert calls == [(1, 2), (2, 2)]


def test_ledger_resumes_without_reparsing_done_files(conn, data_dir):
    _seed_file(conn, data_dir, "strava", 100, "run", "Run", _steady_run(6000, 0.24))
    conn.commit()
    first = efforts.run(conn, data_dir, only_new=True)
    assert first.activities == 1
    # The ledger recorded it as done; a rerun skips it and processes nothing.
    second = efforts.run(conn, data_dir, only_new=True)
    assert second.activities == 0 and second.efforts == 0 and second.skipped == 1
    assert conn.execute("SELECT status FROM effort_runs WHERE source='strava' AND source_id='100'").fetchone()[0] == "done"


def test_empty_file_is_recorded_and_not_reparsed(conn, data_dir):
    # A 200 m jog yields no qualifying distance effort, so it's 'empty' — and must not be retried each run.
    _seed_file(conn, data_dir, "strava", 100, "run", "Run", _steady_run(200, 0.3))
    conn.commit()
    first = efforts.run(conn, data_dir, only_new=True)
    assert first.empty == 1 and first.activities == 0
    assert conn.execute("SELECT status FROM effort_runs WHERE source_id='100'").fetchone()[0] == "empty"
    second = efforts.run(conn, data_dir, only_new=True)
    assert second.empty == 0 and second.skipped == 1  # left alone


def test_changed_file_is_reprocessed(conn, data_dir):
    _seed_file(conn, data_dir, "strava", 100, "run", "Run", _steady_run(6000, 0.24))
    conn.commit()
    efforts.run(conn, data_dir, only_new=True)
    # The file's bytes change (a re-download); the stored sha changes, so the ledger no longer matches.
    conn.execute("UPDATE files SET sha256 = 'y' || substr(sha256, 2) WHERE source_id = '100'")
    conn.commit()
    again = efforts.run(conn, data_dir, only_new=True)
    assert again.activities == 1 and again.skipped == 0


def test_unreadable_file_is_recorded_and_retried(conn, data_dir, monkeypatch):
    _seed_file(conn, data_dir, "strava", 100, "run", "Run", _steady_run(6000, 0.24))
    conn.commit()
    monkeypatch.setattr(efforts, "compute", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad file")))
    c = efforts.run(conn, data_dir, only_new=True)
    assert c.unreadable == 1 and c.activities == 0
    assert conn.execute("SELECT status FROM effort_runs WHERE source_id='100'").fetchone()[0] == "unreadable"
    # 'unreadable' is retried on the next normal run (not skipped).
    c2 = efforts.run(conn, data_dir, only_new=True)
    assert c2.unreadable == 1 and c2.skipped == 0


def test_ledger_is_seeded_from_efforts_an_earlier_build_left(conn, data_dir):
    """An archive with efforts but an empty ledger (computed under an older version) resumes, not reparses."""
    conn.execute("INSERT INTO strava_activities (strava_id, activity_type, start_utc, raw_json, imported_utc) "
                 "VALUES (100, 'Run', '2026-10-01T06:00:00Z', '{}', 'x')")
    conn.execute("INSERT INTO files (path, source, source_id, format, bytes, sha256) VALUES "
                 "('files/strava/100.fit', 'strava', '100', 'fit', 10, 'abc123')")
    conn.execute("INSERT INTO activity_efforts (source, source_id, effort, value, computed_utc) VALUES "
                 "('strava', '100', 'run_5k', 1200.0, 'x'), ('strava', '100', 'run_1km', 230.0, 'x')")
    conn.commit()
    # No file on disk, so if the run tried to reparse it'd be files_missing; instead it's seeded and skipped.
    c = efforts.run(conn, data_dir, only_new=True)
    assert c.skipped == 1 and c.files_missing == 0 and c.activities == 0
    row = conn.execute("SELECT status, n_efforts, file_sha256 FROM effort_runs WHERE source_id='100'").fetchone()
    assert row["status"] == "done" and row["n_efforts"] == 2 and row["file_sha256"] == "abc123"


def test_run_creates_the_ledger_if_missing(conn, data_dir):
    """An archive that reached v4 before the ledger was added has no effort_runs; efforts must create it."""
    conn.execute("DROP TABLE effort_runs")
    conn.commit()
    _seed_file(conn, data_dir, "strava", 100, "run", "Run", _steady_run(6000, 0.24))
    conn.commit()
    c = efforts.run(conn, data_dir, only_new=True)
    assert c.activities == 1
    assert conn.execute("SELECT status FROM effort_runs WHERE source_id='100'").fetchone()[0] == "done"


def test_all_reprocesses_even_done_files(conn, data_dir):
    _seed_file(conn, data_dir, "strava", 100, "run", "Run", _steady_run(6000, 0.24))
    conn.commit()
    efforts.run(conn, data_dir, only_new=True)
    whole = efforts.run(conn, data_dir, only_new=False)
    assert whole.activities == 1 and whole.skipped == 0


def test_missing_file_is_counted_not_fatal(conn, data_dir):
    conn.execute("INSERT INTO files (path, source, source_id, format, bytes, sha256) VALUES "
                 "('files/strava/999.fit', 'strava', '999', 'fit', 1, 'x')")
    conn.execute("INSERT INTO strava_activities (strava_id, activity_type, start_utc, raw_json, imported_utc) "
                 "VALUES (999, 'Run', '2026-10-01T06:00:00Z', '{}', 'x')")
    conn.commit()
    c = efforts.run(conn, data_dir, only_new=True)
    assert c.files_missing == 1 and c.activities == 0
