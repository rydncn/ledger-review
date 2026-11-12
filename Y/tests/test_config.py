"""The configuration file is found and read; a configuration error names its key."""

from __future__ import annotations

from pathlib import Path

import pytest

from tally.cli import main
from tally.config import Config, ConfigError, data_dir_for, load_config


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_no_file_is_an_empty_configuration() -> None:
    config = load_config(None)
    assert config == Config(path=None, data_dir=None, layouts={}, rules=[])


def test_the_default_file_is_read_from_the_home_directory(tmp_path: Path) -> None:
    write(tmp_path / "home" / ".config" / "tally" / "tally.toml", 'data_dir = "ledger"\n')
    config = load_config(None)
    assert config.path == tmp_path / "home" / ".config" / "tally" / "tally.toml"
    assert config.data_dir == tmp_path / "home" / ".config" / "tally" / "ledger"


def test_config_flag_wins_over_the_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flag = write(tmp_path / "flag.toml", 'data_dir = "/flag"\n')
    variable = write(tmp_path / "var.toml", 'data_dir = "/var"\n')
    monkeypatch.setenv("TALLY_CONFIG", str(variable))
    assert load_config(str(flag)).data_dir == Path("/flag")
    assert load_config(None).data_dir == Path("/var")


def test_data_dir_expands_the_home_directory(tmp_path: Path) -> None:
    path = write(tmp_path / "t.toml", 'data_dir = "~/ledger"\n')
    assert load_config(str(path)).data_dir == tmp_path / "home" / "ledger"


def test_layouts_and_rules_are_kept_as_written(tmp_path: Path) -> None:
    path = write(
        tmp_path / "t.toml",
        '[layouts.bank]\ndate = "Date"\n'
        '[[rules]]\nname = "Groceries"\n'
        '[[rules]]\nname = "Travel"\n',
    )
    config = load_config(str(path))
    assert config.layouts == {"bank": {"date": "Date"}}
    assert config.rules == [{"name": "Groceries"}, {"name": "Travel"}]


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("data_dir = 1\n", "data_dir must be a string"),
        ("layouts = 1\n", "layouts must be a table of tables"),
        ("[layouts]\nbank = 1\n", "layouts must be a table of tables"),
        ("rules = 1\n", "rules must be an array of tables"),
        ("rules = [1]\n", "rules must be an array of tables"),
        ("datadir = 'x'\n", 'unknown key "datadir"'),
        ("data_dir = \n", "line 1"),
    ],
)
def test_a_configuration_error_names_the_file_and_the_problem(
    tmp_path: Path, text: str, problem: str
) -> None:
    path = write(tmp_path / "t.toml", text)
    with pytest.raises(ConfigError) as raised:
        load_config(str(path))
    assert str(raised.value).startswith(f"{path}: ")
    assert problem in str(raised.value)


def test_a_configuration_nested_too_deeply_for_the_toml_reader_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "tally.toml"
    path.write_text("x = " + "[" * 100_000 + "]" * 100_000 + "\n", encoding="utf-8")
    with pytest.raises(ConfigError) as raised:
        load_config(str(path))
    assert str(raised.value) == f"{path}: nested too deeply to read"


def test_an_explicit_file_that_is_missing_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing.toml"
    with pytest.raises(ConfigError, match=rf"^{missing}: "):
        load_config(str(missing))
    monkeypatch.setenv("TALLY_CONFIG", str(missing))
    with pytest.raises(ConfigError, match=rf"^{missing}: "):
        load_config(None)


@pytest.mark.parametrize(
    ("toml", "named"),
    [
        (b'data_dir = "caf\xe9"\n', "not UTF-8 text"),  # Latin-1, as a copied file might be
        (b'data_dir = "~nosuchuser-for-tally/d"\n', "data_dir"),  # no such user for ~
        (b'data_dir = "a\\u0000b"\n', "data_dir"),  # a NUL, which no path can hold
        (
            b'[layouts.x]\ndate = "D"\ndescription = "T"\namount = "A"\n'
            b'date_format = "%Y"\nencoding = "utf\\u00008"\n',
            "encoding",
        ),
    ],
)
def test_ac34_every_configuration_error_is_one_line_and_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], toml: bytes, named: str
) -> None:
    config = tmp_path / "tally.toml"
    config.write_bytes(toml)
    argv = ["import", "x.csv", "--account", "a", "--layout", "x", "--config", str(config)]
    code = main([*argv, "--data-dir", str(tmp_path / "data")] if b"data_dir" not in toml else argv)
    captured = capsys.readouterr()
    assert (code, captured.out) == (2, "")
    assert named in captured.err
    assert captured.err.count("\n") == 1
    assert not (tmp_path / "data").exists()


def test_ac34_a_data_directory_flag_holding_a_nul_is_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The variable cannot hold one: the environment refuses a NUL before Tally runs.
    code = main(["account", "list", "--data-dir", str(tmp_path / "a") + chr(0) + "b"])
    captured = capsys.readouterr()
    assert (code, captured.out) == (2, "")
    assert "data directory" in captured.err
    assert captured.err.count("\n") == 1


def test_data_dir_order_is_flag_variable_config_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = Config(path=None, data_dir=None, layouts={}, rules=[])
    configured = Config(path=None, data_dir=tmp_path / "configured", layouts={}, rules=[])
    assert data_dir_for(None, empty) == tmp_path / "home" / ".local" / "share" / "tally"
    assert data_dir_for(None, configured) == tmp_path / "configured"
    monkeypatch.setenv("TALLY_DATA_DIR", str(tmp_path / "variable"))
    assert data_dir_for(None, configured) == tmp_path / "variable"
    assert data_dir_for(str(tmp_path / "flag"), configured) == tmp_path / "flag"


def test_a_data_directory_holding_a_nul_is_refused_by_name() -> None:
    with pytest.raises(ConfigError) as raised:
        data_dir_for("a" + chr(0) + "b", Config(path=None, data_dir=None))
    assert str(raised.value) == "the data directory's name must not hold a NUL character"
