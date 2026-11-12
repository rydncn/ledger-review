"""Writing to the terminal, to CSV and to JSON.

Meaning never depends on colour: colour only decorates text that already
says what it means. Colour is off unless standard output is a terminal,
``NO_COLOR`` is unset and ``TERM`` is not ``dumb``, or ``--color always``.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
from collections.abc import Iterable, Sequence
from typing import TextIO

_PLAIN_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
_FORMULA_STARTERS = ("=", "+", "-", "@", "\t", "\r", "|")


class Style:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.enabled and text else text

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)


def colour_enabled(choice: str, stream: TextIO) -> bool:
    if choice == "always":
        return True
    if choice == "never":
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def guard_cell(value: str) -> str:
    """Stop a spreadsheet running a cell as a formula.

    A cell whose first non-blank character is ``=``, ``+``, ``-``, ``@``,
    a tab, a carriage return or ``|`` gets a leading apostrophe, which
    spreadsheets treat as "this is text". Plain numbers such as ``-42.17``
    are left alone so they stay numbers.
    """
    if not value:
        return value
    if _PLAIN_NUMBER.match(value):
        return value
    if value.lstrip().startswith(_FORMULA_STARTERS):
        return "'" + value
    return value


def write_csv(stream: TextIO, header: Sequence[str], rows: Iterable[Sequence[object]]) -> None:
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow([guard_cell(str(h)) for h in header])
    for row in rows:
        writer.writerow([guard_cell("" if v is None else str(v)) for v in row])


def write_json(stream: TextIO, payload: object) -> None:
    json.dump(payload, stream, indent=2, ensure_ascii=False)
    stream.write("\n")


def render_table(
    header: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    align_right: Iterable[int] = (),
    style: Style | None = None,
    emphasis: Iterable[int] = (),
) -> str:
    """A plain text table with aligned columns. Row indexes in ``emphasis`` are bold."""
    style = style or Style(False)
    right = set(align_right)
    emphasised = set(emphasis)
    widths = [len(h) for h in header]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def line(cells: Sequence[str], decorate=None) -> str:
        parts = []
        for i, cell in enumerate(cells):
            padded = cell.rjust(widths[i]) if i in right else cell.ljust(widths[i])
            if i == len(cells) - 1 and i not in right:
                padded = padded.rstrip()
            parts.append(decorate(padded) if decorate else padded)
        return "  ".join(parts).rstrip()

    out = [line(header, style.bold), line(["-" * w for w in widths])]
    for i, row in enumerate(rows):
        out.append(line(row, style.bold if i in emphasised else None))
    return "\n".join(out) + "\n"


def message(stream: TextIO, text: str) -> None:
    stream.write(text + "\n")


def error(text: str) -> None:
    sys.stderr.write(f"tally: error: {text}\n")


def warn(text: str) -> None:
    sys.stderr.write(f"tally: {text}\n")
