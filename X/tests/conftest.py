from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"


@pytest.fixture
def samples() -> Path:
    return SAMPLES


@pytest.fixture
def home(tmp_path: Path) -> dict[str, str]:
    """An isolated environment: config and data under tmp_path, colour off."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"NO_COLOR", "TALLY_CONFIG", "TALLY_DATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME"}
    }
    env["HOME"] = str(tmp_path)
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["XDG_DATA_HOME"] = str(tmp_path / "data")
    env["TERM"] = "xterm-256color"
    return env


class Result:
    def __init__(self, proc: subprocess.CompletedProcess):
        self.code = proc.returncode
        self.out = proc.stdout
        self.err = proc.stderr

    def __repr__(self) -> str:
        return f"Result(code={self.code}, out={self.out!r}, err={self.err!r})"


@pytest.fixture
def tally(home):
    def run(*args: str, stdin: str | None = None, env: dict | None = None) -> Result:
        full_env = dict(home)
        if env:
            full_env.update(env)
        # Standard input is a closed pipe unless the test supplies text, so
        # "stdin is not a terminal" holds on every runner, Windows included.
        proc = subprocess.run(
            [sys.executable, "-m", "tally", *args],
            capture_output=True,
            stdin=subprocess.DEVNULL if stdin is None else None,
            text=True,
            encoding="utf-8",
            input=stdin,
            env=full_env,
            cwd=ROOT,
        )
        return Result(proc)

    return run


@pytest.fixture
def ready(tally, tmp_path):
    """A configured install with the three sample accounts created."""
    assert tally("init").code == 0
    assert tally("account", "add", "current", "--currency", "GBP").code == 0
    assert tally("account", "add", "card", "--currency", "EUR").code == 0
    assert tally("account", "add", "savings", "--currency", "GBP").code == 0
    return tally
