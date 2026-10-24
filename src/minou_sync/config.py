"""Where the archive lives, and the small settings file that remembers it.

The data folder is chosen in this order: `--data-dir`, `MINOU_SYNC_DATA_DIR`, `data_dir` in the settings file,
then the default. Paths follow the XDG layout on macOS and Linux (as uv and gh do), so they have no spaces and
read easily in a shell: data in `~/.local/share/minou-sync`, settings in `~/.config/minou-sync/config.toml`.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

APP = "minou-sync"
ENV_VAR = "MINOU_SYNC_DATA_DIR"

Source = Literal["flag", "env", "config", "default"]


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class DataLocation:
    path: Path
    source: Source


def _xdg(env: Mapping[str, str], var: str, fallback: str) -> Path:
    # The XDG spec says to ignore relative values.
    value = env.get(var, "")
    return Path(value) if value and Path(value).is_absolute() else Path.home() / fallback


def default_data_dir(env: Mapping[str, str] = os.environ) -> Path:
    return _xdg(env, "XDG_DATA_HOME", ".local/share") / APP


def config_path(env: Mapping[str, str] = os.environ) -> Path:
    return _xdg(env, "XDG_CONFIG_HOME", ".config") / APP / "config.toml"


def read_config(path: Path) -> dict:
    """The settings file as a dict, or {} if there isn't one. Raises ConfigError if it can't be read."""
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"Couldn't read {path}: {e}. Fix or delete it, then run minou-sync setup.") from e


def save_data_dir(data_dir: Path, path: Path) -> None:
    """Write the data folder to the settings file, keeping any other settings in it."""
    settings = {**read_config(path), "data_dir": str(data_dir)}
    lines = ["# minou-sync settings. Change the data folder with: minou-sync setup"]
    # A JSON string is a valid TOML basic string, escapes included. Settings are flat strings so far.
    lines += [f"{key} = {json.dumps(str(value))}" for key, value in settings.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def resolve_data_dir(flag: Path | None, env: Mapping[str, str] = os.environ) -> DataLocation:
    if flag is not None:
        return DataLocation(flag.expanduser().resolve(), "flag")
    if env.get(ENV_VAR):
        return DataLocation(Path(env[ENV_VAR]).expanduser().resolve(), "env")
    configured = read_config(config_path(env)).get("data_dir")
    if configured:
        return DataLocation(Path(configured).expanduser().resolve(), "config")
    return DataLocation(default_data_dir(env), "default")


def git_repo_containing(path: Path) -> Path | None:
    """The git working tree a folder would sit in, if any: health data there could be committed by mistake."""
    for folder in [path, *path.parents]:
        if (folder / ".git").exists():
            return folder
    return None


def location_warnings(path: Path, platform: str = sys.platform, home: Path | None = None) -> list[str]:
    """Why a data folder may cause trouble. Any folder is allowed; these are told to the user before they pick it."""
    home = home or Path.home()
    warnings = []
    repo = git_repo_containing(path)
    if repo:
        warnings.append(f"It's inside the git repository {repo}, where your health data could be committed by "
                        "mistake.")
    if platform == "darwin":
        # macOS asks the user before an app reads these. The daily sync runs with no window, so it can't ask and
        # is refused; commands typed in a terminal are allowed by the terminal's own permission.
        protected = [home / name for name in ("Desktop", "Documents", "Downloads")]
        protected += [home / "Library" / "Mobile Documents", home / "Library" / "CloudStorage", Path("/Volumes")]
        if any(path.is_relative_to(p) for p in protected):
            warnings.append("macOS asks permission before a program reads this folder, and the daily sync can't "
                            "answer, so scheduled runs would fail. Commands you type in a terminal still work.")
    synced = [home / "Library" / "Mobile Documents", home / "Library" / "CloudStorage", home / "Dropbox",
              home / "OneDrive", home / "Google Drive"]
    if any(path.is_relative_to(p) for p in synced):
        warnings.append("A cloud sync service watches this folder. It can copy the database while it's being "
                        "written and damage it, or leave conflicting copies.")
    return warnings


def is_archive(path: Path) -> bool:
    return (path / "archive.db").is_file()
