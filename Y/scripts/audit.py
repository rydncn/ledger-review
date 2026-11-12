"""Dependency audit with a dated allowlist (the Python side of templates/audit).

Usage: uv run python scripts/audit.py [scripts/audit-allowlist.json]

Exports the lockfile and runs pip-audit over it, failing on any known
vulnerability except those allowlisted with a reason and an expiry date. An
expired entry fails the audit until someone renews it on purpose.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path


def load_allowlist(path: Path) -> list[str]:
    entries = json.loads(path.read_text("utf-8"))
    today = datetime.now(UTC).date()
    ignored: list[str] = []
    for entry in entries:
        expires = date.fromisoformat(entry["expires"])
        if expires < today:
            print(
                f"allowlist entry {entry['id']} expired on {expires}: {entry['reason']}",
                file=sys.stderr,
            )
            return []
        ignored.append(entry["id"])
    return ignored


def main(argv: list[str]) -> int:
    allowlist = Path(argv[1]) if len(argv) > 1 else Path("scripts/audit-allowlist.json")
    entries = json.loads(allowlist.read_text("utf-8"))
    ignored = load_allowlist(allowlist)
    if entries and not ignored:
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        requirements = Path(tmp) / "requirements.txt"
        with requirements.open("w") as out:
            subprocess.run(
                [
                    "uv",
                    "export",
                    "--locked",
                    "--all-groups",
                    "--no-emit-project",
                    "--format",
                    "requirements.txt",
                ],
                stdout=out,
                check=True,
            )
        command = [
            "uv",
            "run",
            "--no-build",
            "pip-audit",
            "--strict",
            "--disable-pip",
            "--no-deps",
            "-r",
            str(requirements),
        ]
        for vuln in ignored:
            command += ["--ignore-vuln", vuln]
        print(" ".join(command))
        return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv))
