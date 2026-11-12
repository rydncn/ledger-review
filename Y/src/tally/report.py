"""Reports as plain rows: monthly totals by category, and the payees taking the most.

``cli.py`` renders them. Sums are Python ``int``s of minor units, exact at any size
(SQLite's integer ``sum`` raises past 2**63), turned into ``Decimal`` only by
``from_minor``. Every row is keyed by currency and scale, so nothing is ever summed
across currencies (AC36). Categories come from ``rules.categorise``; none is stored.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import groupby

from tally.money import from_minor
from tally.rules import Match, Rule, categorise
from tally.store import Entry


@dataclass(frozen=True)
class Total:
    """One month's money in and out for one category in one currency; ``None`` is no rule."""

    month: str
    currency: str
    scale: int
    category: str | None
    income_minor: int
    spending_minor: int

    @property
    def income(self) -> Decimal:
        return from_minor(self.income_minor, self.scale)

    @property
    def spending(self) -> Decimal:
        return from_minor(self.spending_minor, self.scale)

    @property
    def net(self) -> Decimal:
        return from_minor(self.income_minor + self.spending_minor, self.scale)


@dataclass(frozen=True)
class Payee:
    """A description's money out in one currency: the total (negative) and how many."""

    currency: str
    scale: int
    description: str
    total_minor: int
    count: int

    @property
    def total(self) -> Decimal:
        return from_minor(self.total_minor, self.scale)


def monthly(entries: Iterable[Entry], rules: Sequence[Rule]) -> list[Total]:
    """Totals per month, currency and category; memory is one total per row, not per entry."""
    sums: dict[tuple[str, str, int, str | None], tuple[int, int]] = {}
    for e in entries:
        result = categorise(e, rules)
        key = (
            e.date[:7],
            e.currency,
            e.scale,
            result.rule.name if isinstance(result, Match) else None,
        )
        income, spending = sums.get(key, (0, 0))
        sums[key] = (income + max(e.amount_minor, 0), spending + min(e.amount_minor, 0))
    ordered = sorted(sums, key=_uncategorised_last)
    return [Total(*key, *sums[key]) for key in ordered]


def _uncategorised_last(key: tuple[str, str, int, str | None]) -> tuple[object, ...]:
    """Month, currency, scale, then category name with uncategorised last.

    Two keys reach the name only when both are categories: a section has one ``None``.
    """
    month, currency, scale, category = key
    return (month, currency, scale, category is None, category)


def payees(payments: Iterable[Entry], top: int) -> list[Payee]:
    """Per currency, the ``top`` descriptions with the most money out, the most first.

    ``payments`` must arrive grouped by currency, scale and description, as
    ``Store.payments`` reads them; only ``top`` payees per currency are held at a time.
    """
    found: list[Payee] = []
    for (currency, scale), in_currency in groupby(payments, key=lambda e: (e.currency, e.scale)):
        each = (
            _payee(currency, scale, description, group)
            for description, group in groupby(in_currency, key=lambda e: e.description)
        )
        found += heapq.nsmallest(top, each, key=lambda p: (p.total_minor, p.description))
    return found


def _payee(currency: str, scale: int, description: str, group: Iterable[Entry]) -> Payee:
    total = count = 0
    for e in group:
        total += e.amount_minor
        count += 1
    return Payee(currency, scale, description, total, count)
