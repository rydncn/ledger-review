"""Every test owns its environment: a home of its own, no Tally variables set."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def own_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("TALLY_CONFIG", raising=False)
    monkeypatch.delenv("TALLY_DATA_DIR", raising=False)
