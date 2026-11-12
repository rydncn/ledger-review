"""AC33: colour never carries meaning and turns off, on a real pseudo-terminal."""

from __future__ import annotations

import os
import re
import select
import subprocess
import sys
import tty
from pathlib import Path

import pytest

from tally.cli import main
from tests.test_categorise import samples
from tests.test_import import Place, console_script
from tests.test_reports import OCTOBER, PAYEES_TABLE, REPORT_TABLE

ESC = chr(0x1B)
RED = f"{ESC}[31m"
RESET = f"{ESC}[0m"
ESCAPES = re.compile(f"{ESC}\\[[0-9;]*m")


def drain(master: int) -> bytes:
    """Everything the terminal has to give, waiting a little for the last of it."""
    data = b""
    while select.select([master], [], [], 0.2)[0]:
        try:
            chunk = os.read(master, 65536)
        except OSError:  # Linux says EIO once the other side has closed
            break
        if not chunk:
            break
        data += chunk
    return data


def on_a_terminal(place: Place, monkeypatch: pytest.MonkeyPatch, *argv: str) -> str:
    """``main`` with standard output on the slave side of a pty, in raw mode (no added CR)."""
    master, slave = os.openpty()
    tty.setraw(slave)
    try:
        with (
            open(slave, "w", encoding="utf-8", closefd=False) as terminal,
            monkeypatch.context() as patch,
        ):
            patch.setattr(sys, "stdout", terminal)
            common = ["--config", str(place.config), "--data-dir", str(place.data_dir)]
            assert main([*argv, *common]) == 0
            terminal.flush()
        return drain(master).decode("utf-8")
    finally:
        os.close(slave)
        os.close(master)


@pytest.mark.parametrize(
    ("argv", "plain"),
    [(["report", *OCTOBER], REPORT_TABLE), (["payees", *OCTOBER, "--top", "2"], PAYEES_TABLE)],
)
def test_ac33_colour_on_a_terminal_marks_negative_amounts_which_keep_their_minus(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    plain: str,
) -> None:
    place = samples(tmp_path, capsys)
    monkeypatch.delenv("NO_COLOR", raising=False)
    shown = on_a_terminal(place, monkeypatch, *argv)
    assert ESCAPES.sub("", shown) == plain
    negatives = re.findall(r"-[0-9]+\.[0-9]{2}", plain)
    assert negatives
    assert re.findall(f"{re.escape(RED)}(-[0-9.]+){re.escape(RESET)}", shown) == negatives
    assert len(ESCAPES.findall(shown)) == 2 * len(negatives)  # only the negative amounts
    piped = place.run(*argv)
    assert piped == (0, plain, "")


@pytest.mark.parametrize("value", ["1", ""])
def test_ac33_no_color_turns_colour_off_on_a_terminal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    place = samples(tmp_path, capsys)
    monkeypatch.setenv("NO_COLOR", value)
    assert on_a_terminal(place, monkeypatch, "report", *OCTOBER) == REPORT_TABLE
    assert on_a_terminal(place, monkeypatch, "payees", *OCTOBER, "--top", "2") == PAYEES_TABLE


def test_ac33_machine_readable_forms_carry_no_colour_on_a_terminal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    place = samples(tmp_path, capsys)
    monkeypatch.delenv("NO_COLOR", raising=False)
    for command in ("report", "payees"):
        for form in ("csv", "json"):
            assert ESC not in on_a_terminal(place, monkeypatch, command, *OCTOBER, "--format", form)


def test_ac33_the_installed_command_on_a_terminal_piped_and_with_no_color(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = samples(tmp_path, capsys)
    command = [
        str(console_script()),
        "report",
        *OCTOBER,
        "--config",
        str(place.config),
        "--data-dir",
        str(place.data_dir),
    ]
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LC_ALL": "C.UTF-8"}

    def terminal(extra: dict[str, str]) -> str:
        master, slave = os.openpty()
        tty.setraw(slave)
        try:
            process = subprocess.Popen(command, stdout=slave, env={**env, **extra})
            os.close(slave)
            shown = drain(master)
            assert process.wait(timeout=60) == 0
            while more := drain(master):
                shown += more
            return shown.decode("utf-8")
        finally:
            os.close(master)

    coloured = terminal({})
    assert RED in coloured
    assert terminal({"NO_COLOR": "1"}) == REPORT_TABLE
    piped = subprocess.run(command, capture_output=True, text=True, check=False, env=env)
    assert piped.stdout == REPORT_TABLE
    assert ESCAPES.sub("", coloured) == REPORT_TABLE
