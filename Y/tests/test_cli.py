"""The command line as a user meets it.

Most tests call ``main`` in-process, so mutation testing sees them; one goes
through a real subprocess, so the installed entry point is proved too.
"""

from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tally import __version__
from tally.cli import existing_name, main


def test_version_is_printed(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--version"])
    assert raised.value.code == 0
    assert capsys.readouterr().out.strip() == f"tally {__version__}"


def test_no_command_is_a_usage_error_on_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    code = main([])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "usage:" in captured.err


def test_the_installed_command_runs() -> None:
    tally = Path(sys.executable).with_name("tally")  # the console script beside this interpreter
    assert tally.exists(), "install the project first: uv sync"
    result = subprocess.run(
        [str(tally), "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"tally {__version__}"


TOP_HELP = """\
usage: tally [-h] [--version] <command> ...

See where your money goes, from your bank's statement files.

positional arguments:
  <command>
    account        add, list and forget accounts
    import         import a statement file into an account
    uncategorised  list the transactions no rule matched
    explain        say why a transaction got its category
    report         money in and out by category, each month
    payees         the payees taking the most money

options:
  -h, --help       show this help message and exit
  --version        show program's version number and exit
"""


def test_help_names_the_command_and_what_it_is_for(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--help"])
    assert raised.value.code == 0
    assert capsys.readouterr().out == TOP_HELP


ACCOUNT_HELP = """\
usage: tally account [-h] <action> ...

positional arguments:
  <action>
    add       add an account
    list      list the accounts
    forget    delete an account and everything imported into it

options:
  -h, --help  show this help message and exit
"""

ADD_HELP = """\
usage: tally account add [-h] [--config FILE] [--data-dir DIR]
                         --currency CURRENCY
                         name

positional arguments:
  name

options:
  -h, --help           show this help message and exit
  --config FILE        the tally.toml to read
  --data-dir DIR       where the data file lives
  --currency CURRENCY  three letters, e.g. GBP
"""

LIST_HELP = """\
usage: tally account list [-h] [--config FILE] [--data-dir DIR]
                          [--format {table,csv,json}]

options:
  -h, --help            show this help message and exit
  --config FILE         the tally.toml to read
  --data-dir DIR        where the data file lives
  --format {table,csv,json}
"""

FORGET_HELP = """\
usage: tally account forget [-h] [--config FILE] [--data-dir DIR] [--yes] name

positional arguments:
  name

options:
  -h, --help      show this help message and exit
  --config FILE   the tally.toml to read
  --data-dir DIR  where the data file lives
  --yes           do not ask first
"""

IMPORT_HELP = """\
usage: tally import [-h] [--config FILE] [--data-dir DIR] --account ACCOUNT
                    [--layout NAME] [--format {table,json}] [--verbose]
                    file

positional arguments:
  file                  a CSV or OFX statement

options:
  -h, --help            show this help message and exit
  --config FILE         the tally.toml to read
  --data-dir DIR        where the data file lives
  --account ACCOUNT     the account the statement belongs to
  --layout NAME         the [layouts.NAME] table that describes the CSV
  --format {table,json}
  --verbose             say what is being read, on standard error
"""

UNCATEGORISED_HELP = """\
usage: tally uncategorised [-h] [--config FILE] [--data-dir DIR]
                           [--format {table,csv,json}]

options:
  -h, --help            show this help message and exit
  --config FILE         the tally.toml to read
  --data-dir DIR        where the data file lives
  --format {table,csv,json}
"""

EXPLAIN_HELP = """\
usage: tally explain [-h] [--config FILE] [--data-dir DIR]
                     [--format {table,json}]
                     id

positional arguments:
  id                    the transaction's id, as listed

options:
  -h, --help            show this help message and exit
  --config FILE         the tally.toml to read
  --data-dir DIR        where the data file lives
  --format {table,json}
"""

REPORT_HELP = """\
usage: tally report [-h] [--config FILE] [--data-dir DIR] [--month MONTH]
                    [--from MONTH] [--to MONTH] [--format {table,csv,json}]

options:
  -h, --help            show this help message and exit
  --config FILE         the tally.toml to read
  --data-dir DIR        where the data file lives
  --month MONTH         one month, e.g. 2026-10
  --from MONTH          the first month, inclusive
  --to MONTH            the last month, inclusive
  --format {table,csv,json}
"""

PAYEES_HELP = """\
usage: tally payees [-h] [--config FILE] [--data-dir DIR] [--month MONTH]
                    [--from MONTH] [--to MONTH] [--format {table,csv,json}]
                    [--top N]

options:
  -h, --help            show this help message and exit
  --config FILE         the tally.toml to read
  --data-dir DIR        where the data file lives
  --month MONTH         one month, e.g. 2026-10
  --from MONTH          the first month, inclusive
  --to MONTH            the last month, inclusive
  --format {table,csv,json}
  --top N               how many per currency, 1 to 1000
"""


@pytest.mark.parametrize(
    "argv",
    [
        ["account", "add", "c", "--cur", "GBP"],
        ["account", "list", "--form", "json"],
        ["account", "forget", "c", "--ye"],
        ["import", "--acc", "c", "--lay", "bank", "x.csv"],
        ["account", "list", "--data", "d"],
        ["--vers"],
        ["account", "--he"],
        ["import", "--he"],
        ["uncategorised", "--form", "json"],
        ["explain", "1", "--form", "json"],
        ["report", "--mon", "2026-10"],
        ["payees", "--month", "2026-10", "--to", "2026-10", "--fro", "2026-09"],
        ["payees", "--month", "2026-10", "--t", "3"],
    ],
)
def test_an_abbreviated_option_is_a_usage_error(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """Every prefix argparse accepts would be part of the command line's shape."""
    with pytest.raises(SystemExit) as raised:
        main(argv)
    captured = capsys.readouterr()
    assert (raised.value.code, captured.out) == (2, "")
    assert "unrecognized arguments" in captured.err or "required" in captured.err
    assert "usage:" in captured.err  # not the help screen, which goes to standard output


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["account"], ACCOUNT_HELP),
        (["account", "add"], ADD_HELP),
        (["account", "list"], LIST_HELP),
        (["account", "forget"], FORGET_HELP),
        (["import"], IMPORT_HELP),
        (["uncategorised"], UNCATEGORISED_HELP),
        (["explain"], EXPLAIN_HELP),
        (["report"], REPORT_HELP),
        (["payees"], PAYEES_HELP),
    ],
)
def test_each_account_help_screen_is_whole(
    capsys: pytest.CaptureFixture[str], argv: list[str], expected: str
) -> None:
    with pytest.raises(SystemExit) as raised:
        main([*argv, "--help"])
    assert raised.value.code == 0
    assert capsys.readouterr().out == expected


ESC = chr(0x1B)
BEL = chr(0x07)


@pytest.mark.parametrize(
    "argv",
    [
        ["report", "--month", "2026-10", f"{ESC}]0;x{BEL}"],  # an unrecognised argument
        ["account", "list", "--format", f"{ESC}[2J"],  # an invalid choice
        ["report", "--month", f"{ESC}[2J"],  # a value its type refused
        [f"{ESC}[2J"],  # an invalid command
    ],
)  # argparse quotes the choices and the refused value with repr; the unrecognised it did not
def test_ac38_argparse_messages_show_control_characters_and_run_none(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(argv)
    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert captured.out == ""
    assert ESC not in captured.err
    assert BEL not in captured.err
    assert captured.err.endswith("\n")


class _ClosedPipe(io.StringIO):
    """Standard output after whoever read it went away, as ``| head`` leaves it."""

    def write(self, text: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")


def test_ac34_a_reader_closing_standard_output_is_not_a_data_file_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        main(["account", "add", "current", "--currency", "GBP", "--data-dir", str(tmp_path)]) == 0
    )
    monkeypatch.setattr(sys, "stdout", _ClosedPipe())
    code = main(["account", "list", "--data-dir", str(tmp_path)])
    assert (code, capsys.readouterr().err) == (1, "")


def test_ac34_a_closed_standard_output_ends_quietly_in_a_real_process(tmp_path: Path) -> None:
    tally = Path(sys.executable).with_name("tally")
    data = ["--data-dir", str(tmp_path)]
    subprocess.run(
        [str(tally), "account", "add", "current", "--currency", "GBP", *data], check=True
    )
    read_end, write_end = os.pipe()
    os.close(read_end)  # the reader has gone before Tally writes, as after ``| head``
    try:
        result = subprocess.run(
            [str(tally), "account", "list", *data],
            stdout=write_end,
            stderr=subprocess.PIPE,
            check=False,
        )
    finally:
        os.close(write_end)
    assert (result.returncode, result.stderr) == (1, b"")


def test_an_account_to_look_up_must_not_be_empty() -> None:
    with pytest.raises(argparse.ArgumentTypeError) as raised:
        existing_name("  ")
    assert str(raised.value) == "must not be empty"
    assert existing_name(" joint ") == "joint"
