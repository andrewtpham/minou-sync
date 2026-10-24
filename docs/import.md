# Importing from any platform

Garmin is minou-sync's live source. History from any other platform comes in through one command: Strava,
intervals.icu, Nike Run Club, COROS, Polar, Suunto, a spreadsheet of your own, or anything else you can export.

```sh
minou-sync import <path> --source <name>
```

- `<path>` is a CSV file, a folder, or a zip. A folder or zip can mix activity CSVs and original files.
- `--source <name>` is a short label for where the data came from, such as `intervals`, `nrc` or `coros`. It
  tells reconcile which records belong together and lets you set a precedence order later.
- `--profile <name-or-path>` picks how to read a CSV. Leave it off and minou-sync matches the file to a
  built-in profile by its columns.

Re-running the same import changes nothing: records already stored are left alone, and files already copied
aren't copied again.

## What gets read

- **Activity summary CSVs** go through a profile and land in `source_activities`, ready for `reconcile` to
  merge with your Garmin and Strava history.
- **Original activity files** (`.fit`, `.gpx`, `.tcx`, gzipped or not) are read for their start time,
  duration, distance and sport, stored as source activities, and copied into `files/<source>/` in the data folder.
- **Daily wellness, weigh-in, body-composition and lab-test CSVs** go through their own profiles into
  `daily_wellness`, `weigh_ins`, `body_composition` and `lab_tests`. For a day Garmin already covers, Garmin's
  wellness row wins. An imported row only fills a date Garmin doesn't have.

A CSV missing a column the profile needs is rejected before anything is written, and the message names the
column. A value that isn't a number is stored as empty (NULL) and counted in the summary.

## Profiles

A profile is a small TOML file that maps one platform's CSV columns to minou-sync's fields, says which columns
must be present, converts units to minou-sync's base units (metres, seconds, kilograms) and gives the date
format. Adding a platform takes a profile and no code.

Built-in profiles:

| Profile | Kind | For |
|---|---|---|
| `minou-sync` | activities | the activity template below |
| `strava-activities` | activities | a plain Strava `activities.csv` |
| `minou-sync-wellness` | wellness | the daily wellness template |
| `minou-sync-weigh-ins` | weigh_ins | the weigh-in template |
| `minou-sync-body-composition` | body_composition | BIA or DEXA scans |
| `minou-sync-lab-tests` | lab_tests | lactate-threshold tests |

A profile looks like this:

```toml
kind = "activities"            # activities | wellness | weigh_ins | body_composition | lab_tests
date_format = "iso"            # "iso" for ISO 8601, else a strptime format like "%b %d, %Y, %I:%M:%S %p"
timezone = "utc"               # how to read a time with no offset: "utc" or "local"
required = ["type", "start_time"]   # columns that must be present

[columns]                      # minou-sync field = CSV column name
activity_type = "type"
start_time = "start_time"
distance_m = "distance"

[units]                        # minou-sync field = multiply the CSV value by this to reach the base unit
distance_m = 1000.0            # for a CSV in kilometres
```

To import with your own profile, pass its path: `--profile ./my-platform.toml`.

## CSV templates

If you keep records in a spreadsheet, use these column names and the matching built-in profile is detected
automatically. Dates are ISO 8601 (`2026-10-01` or `2026-10-01T06:00:00Z`). Distances are metres, durations
seconds and weights kilograms. Leave a cell blank for a value you don't have.

### Activities (`minou-sync`)

`id, type, name, start_time, elapsed_s, moving_s, distance_m, elev_gain_m, avg_hr, max_hr, avg_watts, calories`

Required: `type`, `start_time`. `id` is the platform's own activity ID. Leave it blank and minou-sync derives
a stable one from the start time and type.

### Daily wellness (`minou-sync-wellness`)

`date, resting_hr, hrv_avg_ms, hrv_status, sleep_s, sleep_score, body_battery_high, body_battery_low, stress_avg, steps, readiness_score`

Required: `date`.

### Weigh-ins (`minou-sync-weigh-ins`)

`measured_at, weight_kg, body_fat_pct, muscle_mass_kg, body_water_pct`

Required: `measured_at`, `weight_kg`.

### Body composition (`minou-sync-body-composition`)

`measured_on, method, weight_kg, body_fat_pct, lean_mass_kg, bone_mass_kg, visceral_fat`

Required: `measured_on`, `method` (`bia`, `dexa` or `other`).

### Lactate-threshold tests (`minou-sync-lab-tests`)

`tested_on, sport, protocol, lt1_hr, lt1_pace_s_per_km, lt1_watts, lt2_hr, lt2_pace_s_per_km, lt2_watts, notes`

Required: `tested_on`.

## Exporting from common platforms

- **Strava:** Settings → My Account → Download or Delete Your Account → Request your archive. Import the whole
  zip with `minou-sync import-strava <zip>` to keep every column and original file, or import just the
  `activities.csv` with `minou-sync import activities.csv --source strava`.
- **intervals.icu:** the activity and wellness pages offer CSV downloads. Import each with `--source intervals`.
- **Other platforms:** export whatever CSV or original files they offer and import the file or folder. If the
  columns don't match a built-in profile, write a short profile (above) and pass it with `--profile`.
