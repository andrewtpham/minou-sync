import os
import sqlite3
from pathlib import Path

import pytest

from minou_sync import __version__, cli, config, db, schedule


def test_default_paths_follow_xdg(isolated_home):
    assert config.default_data_dir() == isolated_home / ".local/share/minou-sync"
    assert config.config_path() == isolated_home / ".config/minou-sync/config.toml"
    env = {"XDG_DATA_HOME": "/xdg/data", "XDG_CONFIG_HOME": "/xdg/config"}
    assert config.default_data_dir(env) == Path("/xdg/data/minou-sync")
    assert config.config_path(env) == Path("/xdg/config/minou-sync/config.toml")
    # The XDG spec says relative values are ignored.
    assert config.default_data_dir({"XDG_DATA_HOME": "relative"}) == isolated_home / ".local/share/minou-sync"


def test_resolution_order_flag_env_config_default(tmp_path, isolated_home):
    env = {}
    assert config.resolve_data_dir(None, env) == config.DataLocation(config.default_data_dir(env), "default")
    config.save_data_dir(tmp_path / "saved", config.config_path(env))
    assert config.resolve_data_dir(None, env) == config.DataLocation(tmp_path / "saved", "config")
    env[config.ENV_VAR] = str(tmp_path / "from-env")
    assert config.resolve_data_dir(None, env) == config.DataLocation(tmp_path / "from-env", "env")
    assert config.resolve_data_dir(tmp_path / "flag", env) == config.DataLocation(tmp_path / "flag", "flag")


def test_saved_paths_round_trip_with_awkward_characters(tmp_path):
    path = tmp_path / 'my "data" \\ folder'
    config.save_data_dir(path, tmp_path / "config.toml")
    assert config.read_config(tmp_path / "config.toml") == {"data_dir": str(path)}


def test_save_keeps_other_settings(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('units = "metric"\ndata_dir = "/old"\n')
    config.save_data_dir(Path("/new"), cfg)
    assert config.read_config(cfg) == {"units": "metric", "data_dir": "/new"}


def test_a_broken_settings_file_names_itself(tmp_path, data_dir, capsys):
    cfg = config.config_path()
    cfg.parent.mkdir(parents=True)
    cfg.write_text("data_dir = ")
    assert cli.main(["status"]) == 1
    assert str(cfg) in capsys.readouterr().err


def test_location_warnings(tmp_path):
    home = tmp_path / "home"
    assert config.location_warnings(home / ".local/share/minou-sync", "darwin", home) == []
    for protected in ("Downloads/minou", "Documents/minou", "Desktop/minou"):
        [warning] = config.location_warnings(home / protected, "darwin", home)
        assert "daily sync can't answer" in warning
    assert len(config.location_warnings(Path("/Volumes/External/minou"), "darwin", home)) == 1
    icloud = config.location_warnings(home / "Library/Mobile Documents/com~apple~CloudDocs/minou", "darwin", home)
    assert len(icloud) == 2 and "cloud sync" in icloud[1]
    # Linux has no folder permissions of this kind, but a synced folder is still a risk.
    assert config.location_warnings(home / "Downloads/minou", "linux", home) == []
    assert "cloud sync" in config.location_warnings(home / "Dropbox/minou", "linux", home)[0]
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    assert "git repository" in config.location_warnings(tmp_path / "repo" / "data", "linux", home)[0]


def test_setup_warns_about_a_risky_folder(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "location_warnings", lambda path: ["made-up warning"])
    assert cli.main(["setup", str(tmp_path / "new")]) == 0
    assert "Warning: made-up warning" in capsys.readouterr().err


def test_git_repo_containing(tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    assert config.git_repo_containing(tmp_path / "repo" / "data" / "deeper") == tmp_path / "repo"
    assert config.git_repo_containing(tmp_path / "elsewhere") is None


def _answers(monkeypatch, *answers):
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))
    monkeypatch.setattr(cli, "_interactive", lambda: True)


def test_first_interactive_run_asks_once_and_saves(isolated_home, monkeypatch, capsys):
    _answers(monkeypatch, "")
    assert cli.main(["status"]) == 0
    default = config.default_data_dir()
    assert config.read_config(config.config_path()) == {"data_dir": str(default)}
    assert config.is_archive(default)
    assert "Data folder:        " + str(default) in capsys.readouterr().out
    _answers(monkeypatch)  # a second run must not ask
    assert cli.main(["status"]) == 0


def test_first_run_takes_a_chosen_folder_and_warns_inside_a_git_repo(tmp_path, monkeypatch):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    _answers(monkeypatch, str(tmp_path / "repo" / "data"), "n", str(tmp_path / "archive"))
    assert cli.main(["status"]) == 0
    assert config.read_config(config.config_path())["data_dir"] == str(tmp_path / "archive")


def test_non_interactive_runs_use_the_default_without_asking(isolated_home, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked a question"))
    assert cli.main(["status"]) == 0
    assert config.is_archive(config.default_data_dir())
    assert not config.config_path().exists()


def test_mcp_never_asks(monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    assert cli._data_location(None, "mcp").source == "default"


def test_flag_and_env_skip_the_question(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked a question"))
    assert cli.main(["--data-dir", str(tmp_path / "a"), "status"]) == 0
    monkeypatch.setenv(config.ENV_VAR, str(tmp_path / "b"))
    assert cli.main(["status"]) == 0
    assert config.is_archive(tmp_path / "a") and config.is_archive(tmp_path / "b")


def _archive(path):
    conn = db.connect(path)
    db.migrate(conn)
    conn.close()
    (path / "files").mkdir()
    (path / "files" / "1.fit").write_bytes(b"made up")


def test_setup_moves_the_archive_and_saves_the_new_folder(tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new" / "minou"
    _archive(old)
    config.save_data_dir(old, config.config_path())
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    assert cli.main(["setup", str(new), "--move"]) == 0
    assert not old.exists()
    assert config.is_archive(new) and (new / "files" / "1.fit").read_bytes() == b"made up"
    assert oct(new.stat().st_mode & 0o777) == "0o700"
    assert config.resolve_data_dir(None).path == new


def test_setup_asks_before_moving(tmp_path, monkeypatch, capsys):
    old, new = tmp_path / "old", tmp_path / "new"
    _archive(old)
    config.save_data_dir(old, config.config_path())
    _answers(monkeypatch, str(new), "n")
    assert cli.main(["setup"]) == 0
    assert config.is_archive(old) and not new.exists()
    assert "stays in" in capsys.readouterr().out
    assert config.resolve_data_dir(None).path == new


def test_setup_refuses_a_folder_with_files_in_it(tmp_path, monkeypatch, capsys):
    old, new = tmp_path / "old", tmp_path / "busy"
    _archive(old)
    new.mkdir()
    (new / "other.txt").write_text("x")
    config.save_data_dir(old, config.config_path())
    assert cli.main(["setup", str(new), "--move"]) == 1
    assert "already has files" in capsys.readouterr().err
    assert config.is_archive(old) and config.resolve_data_dir(None).path == old


def test_setup_refuses_to_move_while_something_writes(tmp_path, capsys):
    old = tmp_path / "old"
    _archive(old)
    config.save_data_dir(old, config.config_path())
    writer = sqlite3.connect(old / "archive.db")
    writer.execute("BEGIN IMMEDIATE")
    try:
        assert cli.main(["setup", str(tmp_path / "new"), "--move"]) == 1
    finally:
        writer.rollback()
        writer.close()
    assert "Something is writing" in capsys.readouterr().err
    assert config.is_archive(old)


def test_setup_without_a_terminal_needs_a_folder(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    assert cli.main(["setup"]) == 1
    assert "minou-sync setup <folder>" in capsys.readouterr().err


def test_setup_repoints_the_daily_sync(tmp_path, monkeypatch):
    installed = []
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(schedule, "installed", lambda: schedule.Job(7, 30, "always"))
    monkeypatch.setattr(schedule, "install", lambda d, h, m, n: installed.append((d, h, m, n)))
    assert cli.main(["setup", str(tmp_path / "new")]) == 0
    assert installed == [(tmp_path / "new", 7, 30, "always")]


def test_version(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out.strip() == f"minou-sync {__version__}"
    assert __version__ != "0+unknown"


def test_ctrl_c_exits_130_without_a_traceback(monkeypatch, capsys):
    def interrupted(prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", interrupted)
    assert cli.main(["status"]) == 130
    assert "Traceback" not in capsys.readouterr().err


def test_python_dash_m_runs_the_cli(tmp_path):
    import subprocess
    import sys

    r = subprocess.run([sys.executable, "-m", "minou_sync", "--version"], capture_output=True, text=True,
                       env={**os.environ, "HOME": str(tmp_path)})
    assert r.returncode == 0 and r.stdout.startswith("minou-sync ")


def test_setup_moves_from_the_data_dir_given_for_this_run(tmp_path, monkeypatch):
    old, new = tmp_path / "repo-data", tmp_path / "new"
    _archive(old)
    db.connect(config.default_data_dir()).close()  # an empty default archive must not be what moves
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    assert cli.main(["--data-dir", str(old), "setup", str(new), "--move"]) == 0
    assert not old.exists() and (new / "files" / "1.fit").exists()
    assert config.resolve_data_dir(None).path == new
