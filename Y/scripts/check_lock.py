"""The lockfile only points at PyPI, over HTTPS, with a hash on every file.

Usage: uv run python scripts/check_lock.py

The Python side of lockfile-lint: a tampered uv.lock can quietly send one
package somewhere else or drop its hash. Fails on any package whose source is
not the PyPI registry, or any wheel or sdist without a sha256 hash.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

REGISTRY = "https://pypi.org/simple"


def project_name() -> str:
    return str(tomllib.loads(Path("pyproject.toml").read_text("utf-8"))["project"]["name"])


def main() -> int:
    lock = tomllib.loads(Path("uv.lock").read_text("utf-8"))
    packages = lock.get("package", [])
    problems: list[str] = []
    checked = 0
    own = project_name()
    for package in packages:
        name = package["name"]
        source = package.get("source", {})
        if name == own:
            if "editable" not in source and "virtual" not in source:
                problems.append(f"{name}: the project should be an editable or virtual source")
            continue
        checked += 1
        if source.get("registry") != REGISTRY:
            problems.append(f"{name}: source is {source}, not the PyPI registry")
        files = [*package.get("wheels", [])]
        if "sdist" in package:
            files.append(package["sdist"])
        if not files:
            problems.append(f"{name}: no files to verify")
        for item in files:
            if not str(item.get("hash", "")).startswith("sha256:"):
                problems.append(f"{name}: {item.get('url')} has no sha256 hash")
            if not str(item.get("url", "")).startswith("https://files.pythonhosted.org/"):
                problems.append(f"{name}: {item.get('url')} is not on files.pythonhosted.org")
    if checked == 0:
        problems.append("no packages found in uv.lock: nothing was checked")
    for problem in problems:
        print(problem, file=sys.stderr)
    print(f"{checked} locked packages checked, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
