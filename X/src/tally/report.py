"""Spending and income by category and month, and the payees taking the most."""

from __future__ import annotations

import calendar
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from tally.config import Rule
from tally.errors import UsageError
from tally.importers.dedup import normalise_description
from tally.rules import categorise
from tally.store import Transaction

_MONTH = re.compile(r"^(\d{4})-(\d{2})$")


def parse_month(text: str, flag: str) -> str:
    m = _MONTH.match(text.strip())
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise UsageError(f"{flag} must be a month written as YYYY-MM, for example 2026-10")
    return f"{m.group(1)}-{m.group(2)}"


def month_bounds(month: str) -> tuple[str, str]:
    """First and last ISO dates of a YYYY-MM month."""
    year, mon = int(month[:4]), int(month[5:7])
    last_day = calendar.monthrange(year, mon)[1]
    return date(year, mon, 1).isoformat(), date(year, mon, last_day).isoformat()


def date_range(start_month: str | None, end_month: str | None) -> tuple[str | None, str | None]:
    start = month_bounds(start_month)[0] if start_month else None
    end = month_bounds(end_month)[1] if end_month else None
    if start and end and start > end:
        raise UsageError("--from is after --to")
    return start, end


@dataclass
class CategoryLine:
    month: str
    currency: str
    scale: int
    category: str
    spending: int = 0  # minor units, positive
    income: int = 0  # minor units, positive
    count: int = 0

    @property
    def net(self) -> int:
        return self.income - self.spending


@dataclass
class PayeeLine:
    currency: str
    scale: int
    payee: str
    spending: int = 0
    count: int = 0


def by_category(
    transactions: Iterable[Transaction], rules: list[Rule], exclude: set[str] | None = None
) -> list[CategoryLine]:
    exclude_folded = {c.casefold() for c in exclude or ()}
    lines: dict[tuple[str, str, str], CategoryLine] = {}
    for txn in transactions:
        category, _ = categorise(rules, txn)
        if category.casefold() in exclude_folded:
            continue
        key = (txn.month, txn.currency, category)
        line = lines.get(key)
        if line is None:
            line = lines[key] = CategoryLine(txn.month, txn.currency, txn.scale, category)
        if txn.amount_minor < 0:
            line.spending += -txn.amount_minor
        else:
            line.income += txn.amount_minor
        line.count += 1
    return sorted(lines.values(), key=lambda x: (x.month, x.currency, -x.spending, x.category))


def totals(lines: Iterable[CategoryLine]) -> list[CategoryLine]:
    """One total line per month and currency."""
    agg: dict[tuple[str, str], CategoryLine] = {}
    for line in lines:
        key = (line.month, line.currency)
        total = agg.get(key)
        if total is None:
            total = agg[key] = CategoryLine(line.month, line.currency, line.scale, "Total")
        total.spending += line.spending
        total.income += line.income
        total.count += line.count
    return sorted(agg.values(), key=lambda x: (x.month, x.currency))


def top_payees(
    transactions: Iterable[Transaction],
    rules: list[Rule],
    limit: int,
    exclude: set[str] | None = None,
) -> list[PayeeLine]:
    exclude_folded = {c.casefold() for c in exclude or ()}
    lines: dict[tuple[str, str], PayeeLine] = {}
    spellings: dict[tuple[str, str], dict[str, int]] = {}
    for txn in transactions:
        if txn.amount_minor >= 0:
            continue
        if exclude_folded:
            category, _ = categorise(rules, txn)
            if category.casefold() in exclude_folded:
                continue
        key = (txn.currency, normalise_description(txn.description))
        line = lines.get(key)
        if line is None:
            line = lines[key] = PayeeLine(txn.currency, txn.scale, txn.description)
            spellings[key] = {}
        spellings[key][txn.description] = spellings[key].get(txn.description, 0) + 1
        line.spending += -txn.amount_minor
        line.count += 1
    for key, line in lines.items():
        # Show the spelling the bank used most often; on a tie, the first seen.
        line.payee = max(spellings[key], key=spellings[key].__getitem__)
    ordered = sorted(lines.values(), key=lambda x: (x.currency, -x.spending, x.payee))
    if limit <= 0:
        return ordered
    # Top N within each currency.
    out: list[PayeeLine] = []
    seen: defaultdict[str, int] = defaultdict(int)
    for line in ordered:
        if seen[line.currency] < limit:
            out.append(line)
            seen[line.currency] += 1
    return out
