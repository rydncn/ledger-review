"""Mutation testing on the source files a change touches.

Usage: uv run python scripts/mutation_changed.py <base-ref>

Finds the files under src/tally/ that differ from <base-ref>, turns each into
a mutmut name pattern (src/tally/csv_import.py -> tally.csv_import.*), runs
mutmut on those mutants only, and fails when any mutant survived or when a
changed file produced nothing to run. No changed source file is reported and
passes: a change to docs or tests alone has nothing to mutate.

mutmut only counts tests that run the mutated code in-process, so tests that
drive the command line through a subprocess kill nothing (ADR 0002).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

SOURCE = Path("src/tally")
RESULT_LINE = re.compile(r"^\s*(?P<name>\S+): (?P<status>.+)$")


def changed_source_files(base: str) -> list[Path]:
    diff = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=AM",
            "--end-of-options",
            f"{base}...HEAD",
            "--",
            str(SOURCE),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [Path(line) for line in diff.stdout.split() if line.endswith(".py")]


def pattern_for(path: Path) -> str:
    module = ".".join(path.relative_to("src").with_suffix("").parts)
    return f"{module}.*"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: mutation_changed.py <base-ref>", file=sys.stderr)
        return 2
    files = changed_source_files(argv[1])
    if not files:
        print("No source file under src/tally changed: nothing to mutate.")
        return 0
    patterns = [pattern_for(path) for path in files]
    # mutmut's stats (which test reaches which function) are incremental: a function
    # added since the last run is mapped only to tests added since, so start clean.
    shutil.rmtree("mutants", ignore_errors=True)
    print("Mutating:", " ".join(patterns))
    run = subprocess.run(["uv", "run", "mutmut", "run", *patterns], check=False)
    if run.returncode != 0:
        print(f"mutmut run exited {run.returncode}", file=sys.stderr)
        return 1
    results = subprocess.run(
        ["uv", "run", "mutmut", "results", "--all", "true"],
        capture_output=True,
        text=True,
        check=True,
    )
    statuses: dict[str, str] = {}
    for line in results.stdout.splitlines():
        match = RESULT_LINE.match(line)
        if match:
            statuses[match["name"]] = match["status"].strip()
    chosen = {
        name: status
        for name, status in statuses.items()
        if any(name.startswith(pattern[:-1]) for pattern in patterns)
    }
    if not chosen:
        print("No mutants matched the changed files; the gate checked nothing.", file=sys.stderr)
        return 1
    survivors = sorted(name for name, status in chosen.items() if status == "survived")
    bad = sorted(
        name
        for name, status in chosen.items()
        if status not in {"killed", "timeout", "survived", "suspicious"}
    )
    print(f"{len(chosen)} mutants: {len(survivors)} survived")
    for name in survivors:
        print(f"  survived: {name}")
    for name in bad:
        print(f"  {chosen[name]}: {name}")
    return 1 if survivors or bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
