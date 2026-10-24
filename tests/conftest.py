import sqlite3
from pathlib import Path

import pytest

from minou_sync import db, notify, schedule


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    """No test reads or writes the real settings file, data folder or environment override."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "MINOU_SYNC_DATA_DIR"):
        monkeypatch.delenv(var, raising=False)

    def no_launchctl(*args, **kwargs):
        raise AssertionError("a test tried to run launchctl, which would change the real daily sync")

    monkeypatch.setattr(schedule, "_launchctl", no_launchctl)

    def no_notification(args):
        raise AssertionError("a test tried to post a real desktop notification")

    monkeypatch.setattr(notify, "_run", no_notification)
    return home


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def conn(data_dir: Path) -> sqlite3.Connection:
    c = db.connect(data_dir)
    db.migrate(c)
    yield c
    c.close()
