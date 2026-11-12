"""CSV statements in a bank's layout, described once as ``[layouts.<name>]``.

A layout is validated here and raised as ``ConfigError`` before the data file
opens (AC43). Encoding and line endings are dealt with once, on the way in:
the file is opened as text in the layout's encoding (UTF-8 without its byte
order mark) and the ``csv`` module ends rows on any line ending. Each row
becomes a ``Transaction`` with its fingerprint (ADR 0008) or an ``Unreadable``
with the line number and the reason, and never the row's content.
"""

from __future__ import annotations

import codecs
import csv
import logging
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import NoReturn, TextIO

from tally.config import ConfigError
from tally.importers import ENCODINGS, NotAStatementError, Transaction, Unreadable
from tally.money import AmountError, parse_amount, to_minor

MAX_FIELD = 1_048_576
# A physical line is read in chunks no longer than this, so a monstrous line is
# drained, never held whole (AC40). no mutate: the margin above MAX_FIELD is
# observable only as memory.
MAX_LINE = 2 * MAX_FIELD  # pragma: no mutate
TOO_LONG = "field longer than 1 MB"
_COMPACT = re.compile(r"(?:%[A-Za-z])+")  # a date format with nothing between its fields
csv.field_size_limit(MAX_FIELD)
log = logging.getLogger(__name__)

_KEYS = frozenset(
    {
        "date",
        "description",
        "amount",
        "money_in",
        "money_out",
        "date_format",
        "delimiter",
        "decimal",
        "thousands",
        "encoding",
    }
)
# Formatted with the layout's date_format and read back: a format that cannot
# round-trip this, or reads it back as another day, is not a date format.
_PROBE = datetime(2001, 2, 3, 4, 5, 6)


def _fail(name: str, problem: str) -> NoReturn:
    raise ConfigError(f"layouts.{name}: {problem}")


def _fields(name: str, table: Mapping[str, object]) -> dict[str, str]:
    """The layout's keys as strings, with every column the layout must name present."""
    text: dict[str, str] = {}
    for key, value in table.items():
        if key not in _KEYS:
            _fail(name, f'unknown key "{key}"')
        if not isinstance(value, str):
            _fail(name, f"{key} must be a string")
        text[key] = value
    for column in ("date", "description"):
        if column not in text:
            _fail(name, f'no "{column}" column named')
    if "date_format" not in text:
        _fail(name, "no date_format")
    if "amount" in text and ("money_in" in text or "money_out" in text):
        _fail(name, "amount cannot be named together with money_in or money_out")
    if "amount" not in text and not ("money_in" in text and "money_out" in text):
        _fail(name, 'no "amount" column named, nor both "money_in" and "money_out"')
    return text


@dataclass(frozen=True)
class Layout:
    """A bank's CSV layout, as ``[layouts.<name>]`` describes it."""

    name: str
    date: str
    description: str
    date_format: str
    amount: str | None = None
    money_in: str | None = None
    money_out: str | None = None
    delimiter: str = ","
    decimal: str = "."
    thousands: str = ","
    encoding: str = "utf-8"

    @property
    def columns(self) -> dict[str, str]:
        """Each role the layout fills and the header name it gives it."""
        roles = {
            "date": self.date,
            "description": self.description,
            "amount": self.amount,
            "money_in": self.money_in,
            "money_out": self.money_out,
        }
        return {role: name for role, name in roles.items() if name is not None}

    @classmethod
    def from_config(cls, name: str, table: Mapping[str, object]) -> Layout:
        """Build and validate the layout; any problem is a ``ConfigError`` naming it."""
        layout = cls(name=name, **_fields(name, table))
        for key in ("delimiter", "decimal", "thousands"):
            if len(getattr(layout, key)) != 1:
                _fail(name, f"{key} must be one character")
        if layout.decimal == layout.thousands:
            _fail(name, "decimal and thousands must differ")
        try:
            csv.reader([], delimiter=layout.delimiter)
        except ValueError:
            _fail(name, "delimiter cannot be a quote or a line ending")
        try:
            codec = codecs.lookup(layout.encoding).name
        except LookupError, ValueError:  # ValueError: a NUL in the name
            _fail(name, f'unknown encoding "{layout.encoding}"')
        if codec not in ENCODINGS:  # UTF-16 holds NULs; UTF-7 and the escape codecs, surrogates
            _fail(name, f'encoding "{layout.encoding}" is not supported')
        try:
            read_back = datetime.strptime(_PROBE.strftime(layout.date_format), layout.date_format)
        except ValueError:
            _fail(name, f'date_format "{layout.date_format}" is not a date format')
        if read_back.date() != _PROBE.date():
            _fail(name, f'date_format "{layout.date_format}" does not give a day, month and year')
        return layout


class _LineTooLongError(Exception):
    """A physical line longer than MAX_LINE; the csv reader starts afresh on the next one."""


class _Lines:
    """The file's lines, counted; one longer than MAX_LINE is drained and refused (AC40).

    A record that quoted line breaks carry over many short lines is held whole by the csv
    reader, so the record is bounded too: ``taken`` counts its characters, and past
    MAX_LINE it is refused as one long line is.
    """

    def __init__(self, handle: TextIO, encoding: str) -> None:
        self._handle = handle
        self._encoding = encoding
        self.number = 0
        self.taken = 0  # pragma: no mutate (read resets it before every record)

    def __iter__(self) -> _Lines:
        return self

    def __next__(self) -> str:
        line = self._read()
        if not line:
            raise StopIteration
        self.number += 1
        self.taken += len(line)
        if self.taken > MAX_LINE:
            raise _LineTooLongError
        if len(line) == MAX_LINE and not line.endswith(("\n", "\r")):
            while line and not line.endswith(("\n", "\r")):  # drain the rest, chunk by chunk
                line = self._read()
            raise _LineTooLongError
        return line

    def _read(self) -> str:
        try:
            line = self._handle.readline(MAX_LINE)
        except UnicodeDecodeError as error:
            # The decoder reads ahead of the lines, so no line number can be trusted here.
            raise NotAStatementError(f"not a text file: not valid {self._encoding}") from error
        if "\0" in line:  # the csv module would pass it through; SQLite's text functions stop at it
            raise NotAStatementError("not a text file")
        return line


def _reason(error: Exception) -> str:
    if isinstance(error, _LineTooLongError) or "field limit" in str(error):
        return TOO_LONG
    # no mutate: unreachable with the default dialect and newline="", kept for the module's
    # other errors, whose messages name no content
    return str(error)  # pragma: no mutate


class _Rows:
    """Row outcomes for one file: the layout's columns by position, and the file's repeats."""

    def __init__(self, layout: Layout, header: list[str], scale: int) -> None:
        self.layout = layout
        self.width = len(header)
        self.scale = scale
        self.at: dict[str, int] = {}
        for role, name in layout.columns.items():
            if name not in header:
                raise NotAStatementError(f'header has no column named "{name}"')
            self.at[role] = header.index(name)
        self.seen: dict[str, int] = {}

    def outcome(self, cells: list[str], line: int) -> Transaction | Unreadable:
        """One row as a ``Transaction`` with its ADR 0008 fingerprint, or why it cannot be read."""
        if len(cells) != self.width:
            return Unreadable(line, f"expected {self.width} columns, found {len(cells)}")
        date_format = self.layout.date_format
        text = cells[self.at["date"]].strip()
        try:
            day = datetime.strptime(text, date_format)
        except ValueError:
            return Unreadable(line, f"date does not match {date_format}")
        # With no separator, strptime guesses the width ("2026111" is 1 November): such a
        # format reads only what it writes (month names in any case, as strptime reads them).
        if (
            _COMPACT.fullmatch(date_format)
            and day.strftime(date_format).casefold() != text.casefold()
        ):
            return Unreadable(line, f"date does not match {date_format}")
        date = day.date().isoformat()
        try:
            if "amount" in self.at:
                minor = to_minor(self._amount(cells[self.at["amount"]]), self.scale)
            else:
                minor = self._in_minus_out(cells[self.at["money_in"]], cells[self.at["money_out"]])
        except AmountError as error:
            return Unreadable(line, str(error))
        description = cells[self.at["description"]].strip()
        key = f"{date}|{minor}|{' '.join(description.split()).casefold()}"
        self.seen[key] = n = self.seen.get(key, 0) + 1
        return Transaction(line, date, description, minor, f"csv:{key}|{n}")

    def _amount(self, text: str) -> Decimal:
        return parse_amount(text, self.layout.decimal, self.layout.thousands)

    def _magnitude(self, text: str) -> int:
        if not text.strip():
            return 0
        return to_minor(self._amount(text).copy_abs(), self.scale)  # copy_abs: never rounded

    def _in_minus_out(self, money_in: str, money_out: str) -> int:
        """The column decides the sign and the magnitude counts (design § Shape), in minor units."""
        if not money_in.strip() and not money_out.strip():
            raise AmountError("no amount")
        return self._magnitude(money_in) - self._magnitude(money_out)


def _spanning(reason: str, end: int, start: int) -> str:
    """The reason, saying how far the record ran when an open quote carried it past its line."""
    return reason if end == start else f"{reason}, in a record that runs to line {end}"


def _header(reader: Iterator[list[str]], lines: _Lines) -> list[str]:
    try:
        header = next(reader)
    except StopIteration:
        raise NotAStatementError("empty file") from None
    except (_LineTooLongError, csv.Error) as error:
        raise NotAStatementError(f"line {lines.number}: {_reason(error)}") from error
    return [cell.strip() for cell in header]


def read(path: Path, layout: Layout, scale: int) -> Iterator[Transaction | Unreadable]:
    """Every row of the file, streamed, as a ``Transaction`` or an ``Unreadable``.

    A file that cannot be read at all raises ``NotAStatementError`` (AC17).
    """
    encoding = layout.encoding
    if codecs.lookup(encoding).name == "utf-8":
        encoding = "utf-8-sig"  # a byte order mark is not data (AC10)
    try:
        handle = path.open(encoding=encoding, newline="")  # newline="": the csv module ends rows
    except OSError as error:
        raise NotAStatementError(f"cannot read the statement file: {error.strerror}") from error
    with handle:
        lines = _Lines(handle, layout.encoding)
        reader = csv.reader(lines, delimiter=layout.delimiter)
        rows = _Rows(layout, _header(reader, lines), scale)
        log.info("reading %s as %s, %d columns", path, encoding, rows.width)
        count = 0
        while True:
            start = lines.number + 1  # a quoted field can carry a record over many lines
            lines.taken = 0
            try:
                cells = next(reader)
            except StopIteration:
                break
            except (_LineTooLongError, csv.Error) as error:
                yield Unreadable(start, _spanning(_reason(error), lines.number, start))
                continue
            if not cells:
                continue  # a blank line carries no row
            count += 1
            outcome = rows.outcome(cells, start)
            if isinstance(outcome, Unreadable):
                outcome = Unreadable(outcome.line, _spanning(outcome.reason, lines.number, start))
            yield outcome
        log.info("read %d rows", count)
