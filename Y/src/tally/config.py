"""Finding and reading ``tally.toml`` (ADR 0006).

The file is at ``--config``, else ``$TALLY_CONFIG``, else
``~/.config/tally/tally.toml``; the default may be absent, a path given
explicitly must exist. A configuration error is raised before the data file
is opened and names the file and the problem; the caller turns it into exit
code 2. Layouts and rules are checked here only for shape; their contents are
validated by the importer and the rules module that read them.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG = Path("~/.config/tally/tally.toml")
DEFAULT_DATA_DIR = Path("~/.local/share/tally")


class ConfigError(Exception):
    """The configuration cannot be used; the message names the file and the problem."""


@dataclass(frozen=True)
class Config:
    """What ``tally.toml`` says, with ``path`` None when there is no file."""

    path: Path | None
    data_dir: Path | None
    layouts: dict[str, dict[str, object]] = field(default_factory=dict)
    rules: list[dict[str, object]] = field(default_factory=list)


def find_config(explicit: str | None) -> Path | None:
    """The configuration file to read, or None when the default is absent."""
    given = explicit or os.environ.get("TALLY_CONFIG")
    if given:
        return Path(given)
    default = DEFAULT_CONFIG.expanduser()
    return default if default.exists() else None


def load_config(explicit: str | None) -> Config:
    """Read the configuration file, or an empty configuration when there is none."""
    path = find_config(explicit)
    if path is None:
        return Config(path=None, data_dir=None)
    try:
        with path.open("rb") as handle:
            table = tomllib.load(handle)
    except OSError as error:
        raise ConfigError(f"{path}: {error.strerror}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from error
    except UnicodeDecodeError:  # TOML is UTF-8; a file saved as Latin-1 is not
        raise ConfigError(f"{path}: not UTF-8 text") from None
    except RecursionError:  # tomllib reads nested arrays and tables by recursion
        raise ConfigError(f"{path}: nested too deeply to read") from None

    for key in table:
        if key not in {"data_dir", "layouts", "rules"}:
            raise ConfigError(f'{path}: unknown key "{key}"')
    data_dir = table.get("data_dir")
    if data_dir is not None and not isinstance(data_dir, str):
        raise ConfigError(f"{path}: data_dir must be a string")
    layouts = table.get("layouts", {})
    if not isinstance(layouts, dict) or not all(isinstance(v, dict) for v in layouts.values()):
        raise ConfigError(f"{path}: layouts must be a table of tables")
    rules = table.get("rules", [])
    if not isinstance(rules, list) or not all(isinstance(r, dict) for r in rules):
        raise ConfigError(f"{path}: rules must be an array of tables")
    return Config(path=path, data_dir=_data_dir(path, data_dir), layouts=layouts, rules=rules)


def _data_dir(path: Path, data_dir: str | None) -> Path | None:
    """``data_dir`` relative to the configuration file, ``~`` expanded."""
    if not data_dir:
        return None
    if "\0" in data_dir:
        raise ConfigError(f"{path}: data_dir must not hold a NUL character")
    try:
        return path.parent / Path(data_dir).expanduser()
    except RuntimeError:  # ~name for a user who does not exist
        raise ConfigError(f"{path}: data_dir names a home directory that does not exist") from None


def data_dir_for(explicit: str | None, config: Config) -> Path:
    """``--data-dir``, else ``$TALLY_DATA_DIR``, else the config's, else the platform default."""
    given = explicit or os.environ.get("TALLY_DATA_DIR")
    if given and "\0" in given:
        raise ConfigError("the data directory's name must not hold a NUL character")
    if given:
        return Path(given)
    if config.data_dir is not None:
        return config.data_dir
    if os.name == "nt":  # pragma: no cover, no mutate: no Windows in the test run
        return Path(os.environ.get("LOCALAPPDATA", "~")).expanduser() / "tally"  # pragma: no mutate
    return DEFAULT_DATA_DIR.expanduser()
