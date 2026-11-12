#!/usr/bin/env python3
"""Would each build's own tests catch a regression? The same six behaviours are
broken in each build, one at a time, and its whole test suite is run.

Usage: python3 review/mutations.py X|Y
Each mutation is one or more exact text replacements; the file is restored afterwards
(and `git diff --quiet` is checked at the end).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

MUTATIONS = {
    "X": [
        ("CSV export no longer guards formulae", "src/tally/output.py", 'return "\'" + value', "return value"),
        ("identical rows in one file share one fingerprint", "src/tally/importers/dedup.py", "fingerprint=fingerprint(row, seen[key]),", "fingerprint=fingerprint(row, 1),"),
        ("NO_COLOR ignored", "src/tally/output.py", 'if os.environ.get("NO_COLOR"):', "if False:"),
        ("forget leaves deleted rows in the file", "src/tally/store.py", "        if count:\n            self._vacuum()\n        return count\n\n    # -- imports", "        return count\n\n    # -- imports"),
        ("(12.50) read as positive", "src/tally/money.py", "        negative = True\n        s = s[1:-1].strip()", "        s = s[1:-1].strip()"),
        ("data file created 0644 and never tightened", "src/tally/store.py", [("os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)", "os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)"), ("    if mode & 0o077:\n        os.chmod(path, mode & 0o700)", "    os.chmod(path, 0o644)")]),
    ],
    "Y": [
        ("CSV export no longer guards formulae", "src/tally/cli.py", "return f\"'{shown}\" if text.startswith(_FORMULA) else shown", "return shown"),
        ("identical rows in one file share one fingerprint", "src/tally/importers/csv_layout.py", "self.seen[key] = n = self.seen.get(key, 0) + 1", "n = 1"),
        ("NO_COLOR ignored", "src/tally/cli.py", 'return sys.stdout.isatty() and "NO_COLOR" not in os.environ', "return sys.stdout.isatty()"),
        ("forget leaves deleted rows in the file", "src/tally/store.py", 'connection.execute("PRAGMA secure_delete=ON")', 'connection.execute("PRAGMA secure_delete=OFF")'),
        ("(12.50) read as positive", "src/tally/money.py", "negations = int(bracketed)", "negations = 0"),
        ("data file created 0644 and never tightened", "src/tally/store.py", [("os.close(os.open(path, flags, 0o600))", "os.close(os.open(path, flags, 0o644)); path.chmod(0o644)"), ("side.chmod(0o600)", "side.chmod(0o644)")]),
    ],
}

# Each build's suite as its README / CI runs it.
COMMAND = {"X": ["uv", "run", "pytest", "-q", "-x"], "Y": ["uv", "run", "pytest", "-q", "-x", "-n", "auto"]}


def main() -> None:
    build = sys.argv[1]
    root = REPO / build
    results = []
    for title, rel, *change in MUTATIONS[build]:
        pairs = change[0] if len(change) == 1 else [tuple(change)]
        path = root / rel
        text = path.read_text()
        mutated = text
        for old, new in pairs:
            assert mutated.count(old) == 1, (build, title, old)
            mutated = mutated.replace(old, new)
        path.write_text(mutated)
        try:
            p = subprocess.run(COMMAND[build], cwd=root, capture_output=True, text=True, timeout=1200)
        finally:
            path.write_text(text)
        tail = [ln for ln in p.stdout.splitlines() if ln.strip()][-1:]
        failed = [ln for ln in p.stdout.splitlines() if ln.startswith("FAILED")][:2]
        caught = p.returncode != 0
        results.append((title, caught, tail, failed))
        print(f"{build}: {title}: {'CAUGHT' if caught else 'NOT CAUGHT'} {tail} {failed}", flush=True)
    clean = subprocess.run(["git", "diff", "--quiet", "--", build], cwd=REPO).returncode == 0
    print(f"{build}: working tree restored: {clean}")


if __name__ == "__main__":
    main()
