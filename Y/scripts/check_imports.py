"""Every import under src/ is one Tally allows, read from the syntax tree (design: security boundaries).

Usage: uv run python scripts/check_imports.py

The word checks in rules.json catch ``socket`` and ``float`` written out, but not
``from urllib import request as r`` or ``import ssl``. This reads each file's imports
and fails on any module outside the list below, on a float literal, and when it
finds no file to read (a check that reads nothing must not pass). Division is not
flagged: ``/`` also joins paths, which a syntax tree cannot tell apart.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

SOURCE = Path("src")
# The standard library Tally uses (ADR 0001), and Tally itself. Adding one is a decision.
ALLOWED = frozenset(
    {
        "__future__",
        "argparse",
        "codecs",
        "collections",
        "collections.abc",
        "contextlib",
        "csv",
        "dataclasses",
        "datetime",
        "decimal",
        "heapq",  # report.payees: the top payees in memory bounded by --top (slice 5)
        "io",  # cli: TextIOWrapper, to turn newline translation off for CSV (slice 5)
        "itertools",
        "json",
        "logging",
        "os",
        "pathlib",
        "re",
        "sqlite3",
        "stat",
        "sys",
        "tally",
        "time",
        "tomllib",
        "types",
        "typing",
        "unicodedata",
    }
)
# Names that reach an import, or run text as code, without an import statement.
DYNAMIC = frozenset({"__import__", "__builtins__", "builtins", "exec", "eval", "compile"})


def allowed(module: str) -> bool:
    """Exact names only: ``logging`` is allowed and ``logging.handlers`` (sockets) is not."""
    return module in ALLOWED or module.startswith("tally.")


def is_module(name: str) -> bool:
    """Whether ``from X import Y`` brings in a module ``X.Y`` rather than a name inside X."""
    try:
        return importlib.util.find_spec(name) is not None
    except ImportError, ValueError:  # X is not a package: Y is a name inside it
        return False


def imported(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        inner = [f"{node.module}.{alias.name}" for alias in node.names]
        return [node.module, *(name for name in inner if is_module(name))]
    return []  # a relative import reaches only inside tally


def problems(path: Path) -> list[str]:
    """Each import, dynamic import and float literal in the file that Tally does not allow.

    A guard against careless code, not a sandbox: it reads names, so ``exec``,
    ``eval`` and the ways to reach ``__import__`` are refused outright.
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    found = []
    for node in ast.walk(ast.parse(text, str(path))):
        line: int = getattr(node, "lineno", 0)
        if 0 < line <= len(lines) and "rules-check-ignore:" in lines[line - 1]:
            continue  # the same exemption check-rules.js honours, with its reason on the line
        if isinstance(node, ast.Name) and node.id in DYNAMIC:
            found.append(f"{path}:{line}: {node.id} is not allowed")
        elif isinstance(node, ast.Constant) and node.value == "__import__":
            found.append(f"{path}:{line}: __import__ is not allowed")
        elif isinstance(node, ast.Constant) and isinstance(node.value, float):
            found.append(f"{path}:{line}: a float literal; amounts are exact")
        found += [
            f"{path}:{line}: imports {name}, which is not on the allowed list"
            for name in imported(node)
            if not allowed(name)
        ]
    return found


def main() -> int:
    files = sorted(SOURCE.rglob("*.py"))
    if not files:
        print(f"no Python files under {SOURCE}: the check read nothing", file=sys.stderr)
        return 1
    found = [problem for path in files for problem in problems(path)]
    for problem in found:
        print(problem, file=sys.stderr)
    print(f"{len(files)} files read, {len(found)} problems")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
