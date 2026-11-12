"""Shared test constants. ``SAMPLES`` is found by walking up from this file, because mutmut
copies the tests under ``mutants/`` and runs them from there."""

from __future__ import annotations

from pathlib import Path

SAMPLES = next(p / "samples" for p in Path(__file__).resolve().parents if (p / "samples").is_dir())
