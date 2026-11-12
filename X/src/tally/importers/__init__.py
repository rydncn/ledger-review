"""Readers for the statement files banks let you download."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedRow:
    """One transaction as read from a file, before deduplication."""

    line: int  # physical line the row starts on, 1-based
    date: str  # ISO YYYY-MM-DD
    amount_minor: int
    description: str
    memo: str | None = None
    external_id: str | None = None  # the bank's own id for the transaction, if it gives one
    currency: str | None = None  # the file's currency, if it says


@dataclass(frozen=True)
class RowProblem:
    """A row that could not be read. ``values`` is shown only on request."""

    line: int
    reason: str
    values: tuple[str, ...] = ()


@dataclass(frozen=True)
class Note:
    """Something worth telling the user that is not an error."""

    line: int | None
    text: str
