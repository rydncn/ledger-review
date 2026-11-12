"""Read a bank's CSV export using a layout from the configuration.

Rows stream through; the file is never held in memory. Problems with a
row are reported with the line number and a reason that does not quote
the row. Problems with the file as a whole (no header, wrong columns, not
text) stop the import before anything is written.
"""

from __future__ import annotations

import csv
import os
from collections.abc import Iterator
from datetime import datetime

from tally.config import Layout
from tally.encoding import detect_encoding
from tally.errors import ImportError_
from tally.importers import Note, ParsedRow, RowProblem
from tally.money import AmountError, parse_amount

# Fields longer than this are not statement rows. Default csv limit is 128 KiB.
csv.field_size_limit(1 << 20)


def _resolve(layout: Layout, header: list[str] | None) -> dict[str, int]:
    """Map each layout field to a column index."""
    wanted = {
        "date": layout.date,
        "description": layout.description,
        "amount": layout.amount,
        "money_in": layout.money_in,
        "money_out": layout.money_out,
        "memo": layout.memo,
        "balance": layout.balance,
    }
    columns: dict[str, int] = {}
    missing: list[str] = []
    if header is not None:
        cleaned = [h.strip() for h in header]
        folded = [h.casefold() for h in cleaned]
    for field, spec in wanted.items():
        if spec is None:
            continue
        if isinstance(spec, int):
            if header is not None and spec >= len(header):
                missing.append(f"column {spec}")
            else:
                columns[field] = spec
            continue
        assert header is not None
        name = spec.strip()
        if name in cleaned:
            columns[field] = cleaned.index(name)
        elif name.casefold() in folded:
            columns[field] = folded.index(name.casefold())
        else:
            missing.append(repr(spec))
    if missing:
        count = len(header) if header is not None else 0
        raise ImportError_(
            f"the file does not have the column(s) {', '.join(missing)} "
            f"that layout {layout.name!r} expects",
            hint=f"The file's header has {count} column(s). Check the layout, or that this "
            "is the right file. Run with --show-rows to see the header as the file has it.",
            details=["Columns found in the file's header: " + ", ".join(repr(h) for h in header)]
            if header
            else [],
        )
    return columns


def read_csv(
    path: str | os.PathLike[str], layout: Layout, scale: int, encoding: str | None = None
) -> Iterator[ParsedRow | RowProblem | Note]:
    """Yield the file's rows. ``encoding`` skips detection when the caller already knows."""
    if encoding is None:
        try:
            encoding = detect_encoding(path, layout.encoding)
        except ValueError as exc:
            raise ImportError_(str(exc)) from None
    try:
        fh = open(path, encoding=encoding, newline="", errors="strict")  # noqa: SIM115
    except OSError as exc:
        raise ImportError_(f"cannot open {path}: {exc.strerror}") from None
    with fh:
        yield from _read(fh, layout, scale, encoding)


def _read(fh, layout: Layout, scale: int, encoding: str) -> Iterator[ParsedRow | RowProblem | Note]:
    reader = csv.reader(fh, delimiter=layout.delimiter, quotechar=layout.quotechar)
    try:
        for _ in range(layout.skip_lines):
            next(reader, None)
        header: list[str] | None = None
        if layout.header:
            header = _next_nonblank(reader)
            if header is None:
                raise ImportError_("the file is empty or has no header line")
        columns = _resolve(layout, header)
        width_needed = max(columns.values()) + 1
        expected_width = len(header) if header is not None else None
        balance_check = _BalanceCheck() if "balance" in columns else None

        while True:
            start_line = reader.line_num + 1
            try:
                row = next(reader)
            except StopIteration:
                break
            if not row or all(not cell.strip() for cell in row):
                continue
            if len(row) < width_needed:
                yield RowProblem(
                    start_line, f"expected at least {width_needed} fields, found {len(row)}"
                )
                continue
            repaired = False
            if expected_width is not None and len(row) > expected_width:
                row = _absorb_extra_fields(row, expected_width, columns, layout.delimiter)
                if row is None:
                    continue
                repaired = True
            result = _parse_row(row, columns, layout, scale, start_line)
            if isinstance(result, RowProblem):
                yield result
                continue
            parsed, signed, balance = result
            if balance_check is not None:
                yield from balance_check.observe(parsed, balance)
            if repaired:
                yield Note(
                    start_line,
                    "the row had more fields than the header; the extra text was "
                    "treated as part of the description",
                )
            if signed:
                yield Note(
                    start_line,
                    "a money-in or money-out amount carried a minus sign or brackets; "
                    "the column decides the direction, so the sign was ignored",
                )
            yield parsed
        if balance_check is not None:
            yield from balance_check.summary()
    except csv.Error as exc:
        raise ImportError_(
            f"line {reader.line_num}: the file is not readable as CSV ({exc})",
            hint="Nothing was imported from it.",
        ) from None
    except UnicodeDecodeError:
        raise ImportError_(
            f"after line {reader.line_num}: the file is not valid {encoding} text",
            hint="Set `encoding` in the layout if you know the right one. Nothing was imported.",
        ) from None


def _absorb_extra_fields(
    row: list[str], expected: int, columns: dict[str, int], delimiter: str
) -> list[str] | None:
    """Fold surplus fields into the description.

    A description containing an unquoted delimiter splits into several
    fields. The description is the only free-text column, so when a row is
    too wide the surplus is rejoined there, provided the row still has a
    description column to hold it.
    """
    extra = len(row) - expected
    d = columns["description"]
    merged = delimiter.join(row[d : d + extra + 1])
    return [*row[:d], merged, *row[d + extra + 1 :]]


def _next_nonblank(reader) -> list[str] | None:
    for row in reader:
        if row and any(cell.strip() for cell in row):
            return row
    return None


def _parse_row(
    row: list[str], columns: dict[str, int], layout: Layout, scale: int, line: int
) -> tuple[ParsedRow, bool, int | _Unreadable | None] | RowProblem:
    if any("\x00" in cell for cell in row):
        return RowProblem(line, "the row contains binary data")
    date_text = row[columns["date"]].strip()
    try:
        date = datetime.strptime(date_text, layout.date_format).date()
    except ValueError:
        return RowProblem(
            line,
            f"could not read the date (expected the format {layout.date_format})",
            (date_text,),
        )
    description = " ".join(row[columns["description"]].split())
    if not description:
        return RowProblem(line, "the description is empty")
    memo = None
    if "memo" in columns:
        memo = " ".join(row[columns["memo"]].split()) or None

    kw = dict(
        decimal_separator=layout.decimal_separator,
        thousands_separator=layout.thousands_separator,
        scale=scale,
    )
    signed = False
    if layout.uses_two_columns:
        in_text = row[columns["money_in"]]
        out_text = row[columns["money_out"]]
        try:
            money_in = parse_amount(in_text, **kw)
        except AmountError as exc:
            return RowProblem(line, f"could not read the money-in amount: {exc}", (in_text,))
        try:
            money_out = parse_amount(out_text, **kw)
        except AmountError as exc:
            return RowProblem(line, f"could not read the money-out amount: {exc}", (out_text,))
        if money_in is None and money_out is None:
            return RowProblem(line, "both the money-in and money-out amounts are empty")
        # The column says which way the money went; a sign or brackets in a
        # dedicated column is the bank's presentation of a debit or credit.
        if (money_in is not None and money_in < 0) or (money_out is not None and money_out < 0):
            signed = True
        amount = abs(money_in or 0) - abs(money_out or 0)
    else:
        amount_text = row[columns["amount"]]
        try:
            amount = parse_amount(amount_text, **kw)
        except AmountError as exc:
            return RowProblem(line, f"could not read the amount: {exc}", (amount_text,))
        if amount is None:
            return RowProblem(line, "the amount is empty")
    if layout.negate:
        amount = -amount
    balance: int | _Unreadable | None = None
    if "balance" in columns:
        try:
            balance = parse_amount(row[columns["balance"]], **kw)
        except AmountError:
            balance = _UNREADABLE
    return ParsedRow(line, date.isoformat(), amount, description, memo), signed, balance


class _Unreadable:
    """A balance cell that could not be read; the check skips over it."""


_UNREADABLE = _Unreadable()


class _BalanceCheck:
    """Check each row's balance against the previous row's balance and amount.

    Statements list rows oldest first or newest first; the first pair of
    rows that can tell the two apart decides. A row whose balance does not
    follow from its neighbour is noted by line, with no values. This catches
    a layout with the wrong sign or column, and rows missing from the file.
    """

    def __init__(self) -> None:
        self.order: str | None = None  # "forward" (oldest first) or "reverse"
        self.prev: tuple[int, int] | None = None  # (amount, balance)
        self.mismatches = 0
        self.unreadable = 0

    def observe(self, row: ParsedRow, balance: int | _Unreadable | None) -> Iterator[Note]:
        if balance is None or isinstance(balance, _Unreadable):
            if isinstance(balance, _Unreadable):
                self.unreadable += 1
                yield Note(row.line, "the balance could not be read, so this row was not checked")
            self.prev = None
            return
        if self.prev is not None:
            prev_amount, prev_balance = self.prev
            forward_ok = balance - prev_balance == row.amount_minor
            reverse_ok = prev_balance - balance == prev_amount
            if self.order is None:
                if forward_ok and not reverse_ok:
                    self.order = "forward"
                elif reverse_ok and not forward_ok:
                    self.order = "reverse"
                elif not forward_ok and not reverse_ok:
                    self.mismatches += 1
                    yield Note(row.line, _MISMATCH)
            elif not (forward_ok if self.order == "forward" else reverse_ok):
                self.mismatches += 1
                yield Note(row.line, _MISMATCH)
        self.prev = (row.amount_minor, balance)

    def summary(self) -> Iterator[Note]:
        if self.mismatches:
            yield Note(
                None,
                f"{self.mismatches} row(s) did not follow from the running balance; "
                "check the layout's sign and columns, or whether the statement is "
                "missing rows",
            )


_MISMATCH = "the balance does not follow from the previous row's balance and the amount read"
