"""minou-sync: a local archive of Garmin and Strava activities and Garmin wellness data."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("minou-sync")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0+unknown"
