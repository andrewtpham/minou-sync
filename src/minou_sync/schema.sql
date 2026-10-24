-- minou-sync archive schema, version 4. Applied by db.migrate() for a fresh database; an existing database
-- is upgraded step by step in db.py (ALTER and the files rebuild aren't expressible as idempotent SQL here).
-- v2 added garmin_raw. v3 adds source_activities, activity_sources, the edit layer, the athlete's own record
-- tables, daily_wellness.source, notes, and rebuilds files without its source CHECK. v4 adds
-- activities.check_reason (the plausibility flag) and activity_efforts (best efforts from files);
-- both additions are plain ADD COLUMN / CREATE TABLE, so the v3 -> v4 step needs no table rebuild.

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS garmin_activities (
  garmin_id        INTEGER PRIMARY KEY,
  activity_type    TEXT NOT NULL,
  name             TEXT,
  start_utc        TEXT NOT NULL,
  start_local      TEXT,
  utc_offset_min   INTEGER,
  elapsed_s        INTEGER,
  moving_s         INTEGER,
  distance_m       REAL,
  elev_gain_m      REAL,
  elev_loss_m      REAL,
  avg_hr           INTEGER,
  max_hr           INTEGER,
  avg_watts        REAL,
  max_watts        REAL,
  norm_watts       REAL,
  avg_cadence      REAL,
  calories         REAL,
  aerobic_te       REAL,
  anaerobic_te     REAL,
  training_load    REAL,
  origin           TEXT NOT NULL CHECK (origin IN ('export', 'sync')),
  raw_json         TEXT NOT NULL,
  imported_utc     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS garmin_start_idx ON garmin_activities(start_utc);

CREATE TABLE IF NOT EXISTS strava_activities (
  strava_id        INTEGER PRIMARY KEY,
  activity_type    TEXT NOT NULL,
  name             TEXT,
  description      TEXT,
  start_utc        TEXT NOT NULL,
  elapsed_s        INTEGER,
  moving_s         INTEGER,
  distance_m       REAL,
  elev_gain_m      REAL,
  avg_hr           INTEGER,
  max_hr           INTEGER,
  avg_watts        REAL,
  calories         REAL,
  raw_json         TEXT NOT NULL,
  imported_utc     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS strava_start_idx ON strava_activities(start_utc);

-- Activities from any source other than Garmin and Strava. Garmin and Strava keep their own tables.
CREATE TABLE IF NOT EXISTS source_activities (
  source           TEXT NOT NULL,            -- name given at import, e.g. 'nrc', 'intervals'
  source_id        TEXT NOT NULL,            -- the platform's ID, or a hash of start time + type when there is none
  activity_type    TEXT NOT NULL,
  name             TEXT,
  start_utc        TEXT NOT NULL,
  start_local      TEXT,
  elapsed_s        INTEGER,
  moving_s         INTEGER,
  distance_m       REAL,
  elev_gain_m      REAL,
  avg_hr           INTEGER,
  max_hr           INTEGER,
  avg_watts        REAL,
  calories         REAL,
  raw_json         TEXT NOT NULL,
  imported_utc     TEXT NOT NULL,
  PRIMARY KEY (source, source_id)
);
CREATE INDEX IF NOT EXISTS source_start_idx ON source_activities(start_utc);

-- Derived by reconcile; rebuilt in one transaction, so ids aren't stable across runs. key is the stable target
-- for edits and links: '<source>:<id>' of the main record, or 'manual:<uuid>'.
CREATE TABLE IF NOT EXISTS activities (
  id               INTEGER PRIMARY KEY,
  key              TEXT,
  garmin_id        INTEGER UNIQUE REFERENCES garmin_activities(garmin_id),
  strava_id        INTEGER UNIQUE REFERENCES strava_activities(strava_id),
  source           TEXT NOT NULL,
  activity_type    TEXT NOT NULL,
  name             TEXT,
  start_utc        TEXT NOT NULL,
  start_local      TEXT,
  elapsed_s        INTEGER,
  moving_s         INTEGER,
  distance_m       REAL,
  elev_gain_m      REAL,
  avg_hr           INTEGER,
  max_hr           INTEGER,
  avg_watts        REAL,
  avg_cadence      REAL,
  calories         REAL,
  aerobic_te       REAL,
  anaerobic_te     REAL,
  edited           INTEGER NOT NULL DEFAULT 0,
  check_reason     TEXT                             -- v4: why the values look impossible; NULL when plausible
);
CREATE INDEX IF NOT EXISTS activities_start_idx ON activities(start_utc);
CREATE UNIQUE INDEX IF NOT EXISTS activities_key_idx ON activities(key);
CREATE INDEX IF NOT EXISTS activities_type_idx ON activities(activity_type);

-- Every source behind each master record. Rebuilt by reconcile alongside activities.
CREATE TABLE IF NOT EXISTS activity_sources (
  key              TEXT NOT NULL,
  source           TEXT NOT NULL,
  source_id        TEXT NOT NULL,
  PRIMARY KEY (source, source_id)
);

-- Best efforts computed from original files (v4), keyed by the source record whose file was read. A
-- Garmin and a Strava copy of the same workout each keep their own row; best_efforts reads the main record's
-- row first, then any other source's. value is seconds for a distance effort, watts for a power effort.
CREATE TABLE IF NOT EXISTS activity_efforts (
  source           TEXT NOT NULL,
  source_id        TEXT NOT NULL,
  effort           TEXT NOT NULL,            -- run_1km, run_1mi, run_5k, run_10k, run_half, run_marathon,
                                             -- run_50k, ride_40km, power_5min, power_20min, power_60min
  value            REAL NOT NULL,
  start_offset_s   INTEGER,                  -- where in the activity the effort starts
  computed_utc     TEXT NOT NULL,
  PRIMARY KEY (source, source_id, effort)
);

-- The efforts ledger: one row per original file that `efforts` has attempted, so a stopped run resumes
-- without redoing work and a file that yields no effort isn't reparsed every run. file_sha256 is copied from
-- files.sha256, so a re-downloaded or corrected file (new hash) is reprocessed; the skip check joins this to
-- files and reads no file off disk. status: 'done' (efforts written), 'empty' (parsed fine, no qualifying
-- effort), 'unreadable' (couldn't parse; retried on the next normal run, being cheap and often self-healing).
CREATE TABLE IF NOT EXISTS effort_runs (
  source           TEXT NOT NULL,
  source_id        TEXT NOT NULL,
  file_sha256      TEXT NOT NULL,
  status           TEXT NOT NULL CHECK (status IN ('done', 'empty', 'unreadable')),
  n_efforts        INTEGER NOT NULL DEFAULT 0,
  computed_utc     TEXT NOT NULL,
  PRIMARY KEY (source, source_id)
);

-- The edit layer. An edit is keyed by activities.key, names a column of activities (or '_hidden'), and
-- carries the new value as JSON. undone_utc set on undo; the edit then stops applying.
CREATE TABLE IF NOT EXISTS activity_edits (
  id               INTEGER PRIMARY KEY,
  key              TEXT NOT NULL,
  field            TEXT NOT NULL,
  value_json       TEXT,
  created_utc      TEXT NOT NULL,
  undone_utc       TEXT
);
CREATE INDEX IF NOT EXISTS activity_edits_key_idx ON activity_edits(key);

-- Manually created activities. Their fields live in activity_edits, keyed by this key.
CREATE TABLE IF NOT EXISTS manual_activities (
  key              TEXT PRIMARY KEY,
  created_utc      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_wellness (
  date               TEXT PRIMARY KEY,
  resting_hr         INTEGER,
  hrv_avg_ms         REAL,
  hrv_status         TEXT,
  sleep_s            INTEGER,
  sleep_score        INTEGER,
  deep_s             INTEGER,
  light_s            INTEGER,
  rem_s              INTEGER,
  awake_s            INTEGER,
  body_battery_high  INTEGER,
  body_battery_low   INTEGER,
  stress_avg         INTEGER,
  steps              INTEGER,
  readiness_score    INTEGER,
  complete           INTEGER NOT NULL DEFAULT 0,
  source             TEXT NOT NULL DEFAULT 'garmin',  -- where the row came from; Garmin wins for a date
  updated_utc        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weigh_ins (
  measured_utc     TEXT PRIMARY KEY,
  weight_kg        REAL NOT NULL,
  body_fat_pct     REAL,
  muscle_mass_kg   REAL,
  body_water_pct   REAL,
  source           TEXT NOT NULL,
  edited_utc       TEXT             -- set when the athlete edits the row; import and sync then leave it alone
);

-- The athlete's own body-composition measurements (BIA or DEXA). Edited in place; a row with edited_utc is
-- never overwritten by import or sync.
CREATE TABLE IF NOT EXISTS body_composition (
  id               INTEGER PRIMARY KEY,
  source           TEXT NOT NULL,
  measured_on      TEXT NOT NULL,
  method           TEXT NOT NULL CHECK (method IN ('bia', 'dexa', 'other')),
  weight_kg        REAL,
  body_fat_pct     REAL,
  lean_mass_kg     REAL,
  bone_mass_kg     REAL,
  visceral_fat     REAL,
  raw_json         TEXT,
  edited_utc       TEXT,
  UNIQUE (source, measured_on, method)
);

-- Lactate threshold tests. Stages live in lab_test_stages.
CREATE TABLE IF NOT EXISTS lab_tests (
  id               INTEGER PRIMARY KEY,
  source           TEXT NOT NULL,
  tested_on        TEXT NOT NULL,
  sport            TEXT,
  protocol         TEXT,
  lt1_hr           INTEGER,
  lt1_pace_s_per_km REAL,
  lt1_watts        REAL,
  lt2_hr           INTEGER,
  lt2_pace_s_per_km REAL,
  lt2_watts        REAL,
  notes            TEXT,
  edited_utc       TEXT,
  UNIQUE (source, tested_on, sport)
);

CREATE TABLE IF NOT EXISTS lab_test_stages (
  test_id          INTEGER NOT NULL REFERENCES lab_tests(id) ON DELETE CASCADE,
  stage            INTEGER NOT NULL,
  pace_s_per_km    REAL,
  watts            REAL,
  hr               INTEGER,
  lactate_mmol     REAL,
  PRIMARY KEY (test_id, stage)
);

-- files has no source CHECK in v3: any import source can own a file. (A v2 database is rebuilt in db.py.)
CREATE TABLE IF NOT EXISTS files (
  path             TEXT PRIMARY KEY,
  source           TEXT NOT NULL,
  source_id        TEXT NOT NULL,
  format           TEXT NOT NULL CHECK (format IN ('fit', 'gpx', 'tcx')),
  bytes            INTEGER NOT NULL,
  sha256           TEXT NOT NULL
);

-- Every Garmin API response as it came, so nothing fetched is thrown away. key is a date (YYYY-MM-DD) for
-- daily endpoints, an activity ID for per-activity endpoints, and '' for account-wide snapshots.
CREATE TABLE IF NOT EXISTS garmin_raw (
  endpoint         TEXT NOT NULL,
  key              TEXT NOT NULL,
  raw_json         TEXT NOT NULL,
  fetched_utc      TEXT NOT NULL,
  PRIMARY KEY (endpoint, key)
);

-- Notes from MCP clients. Never change activity or wellness values.
CREATE TABLE IF NOT EXISTS notes (
  id               INTEGER PRIMARY KEY,
  target           TEXT NOT NULL CHECK (target IN ('activity', 'day')),
  ref              TEXT NOT NULL,            -- activities.key, or a local date YYYY-MM-DD
  text             TEXT NOT NULL,
  author           TEXT,                     -- the MCP client's name, e.g. 'claude-code'
  created_utc      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS notes_ref_idx ON notes(ref);
