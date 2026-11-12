"""AC30, AC31, AC36: ``report.monthly`` and ``report.payees`` alone, and the store's reads."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tally.report import Payee, Total, monthly, payees
from tally.rules import load
from tally.store import Entry, Store
from tests.test_categorise import samples
from tests.test_rules import AC26_RULES

BIG = 2**63 - 1  # the largest amount_minor SQLite stores


def entry(
    description: str,
    minor: int,
    *,
    date: str = "2026-10-01",
    account: str = "current",
    currency: str = "GBP",
    scale: int = 2,
) -> Entry:
    return Entry(1, date, account, currency, scale, description, minor)


def test_ac30_totals_by_category_with_income_and_spending_apart() -> None:
    entries = [
        entry("TESCO", -4217),
        entry("TESCO", -15000),
        entry("TFL TRAVEL CH", -340),
        entry("SALARY", 245000),
        entry("REFUND", 1250),
        entry("TRANSFER", -50000, account="savings"),
    ]
    assert monthly(entries, load(AC26_RULES)) == [
        Total("2026-10", "GBP", 2, "Groceries", 0, -19217),
        Total("2026-10", "GBP", 2, "Travel", 0, -340),
        Total("2026-10", "GBP", 2, None, 246250, -50000),
    ]


def test_ac30_a_total_is_the_exact_sum() -> None:
    rows = monthly([entry("A", 10), entry("B", 20), entry("C", -30)], load([{"name": "Small"}]))
    assert rows == [Total("2026-10", "GBP", 2, "Small", 30, -30)]
    assert (rows[0].income, rows[0].spending, rows[0].net) == (
        Decimal("0.30"),
        Decimal("-0.30"),
        Decimal("0.00"),
    )
    assert str(rows[0].net) == "0.00"


def test_ac30_a_total_past_sqlites_integer_is_still_exact() -> None:
    (row,) = monthly([entry("A", BIG), entry("B", BIG), entry("C", -BIG)], [])
    assert (row.income_minor, row.spending_minor) == (2 * BIG, -BIG)
    assert str(row.income) == "184467440737095516.14"
    assert str(row.net) == "92233720368547758.07"


def test_ac30_ac31_rows_sort_by_month_currency_and_category_uncategorised_last() -> None:
    rules = load([{"name": "Zoo", "description_contains": "Z"}, {"name": "Art", "account": "a"}])
    entries = [
        entry("Z", -1, date="2026-11-01"),
        entry("Q", -2, date="2026-10-09"),
        entry("Z", -3, date="2026-10-02", currency="EUR"),
        entry("Q", -4, date="2026-10-03", account="a"),
        entry("Z", -5, date="2026-10-04"),
    ]
    assert [(r.month, r.currency, r.category) for r in monthly(entries, rules)] == [
        ("2026-10", "EUR", "Zoo"),
        ("2026-10", "GBP", "Art"),
        ("2026-10", "GBP", "Zoo"),
        ("2026-10", "GBP", None),
        ("2026-11", "GBP", "Zoo"),
    ]


def test_ac36_nothing_sums_across_currencies_or_scales() -> None:
    entries = [
        entry("A", -100, currency="GBP"),
        entry("A", -100, currency="EUR"),
        entry("A", -100, currency="JPY", scale=0),
        entry("A", -100, currency="GBP", scale=3),
    ]
    assert monthly(entries, []) == [
        Total("2026-10", "EUR", 2, None, 0, -100),
        Total("2026-10", "GBP", 2, None, 0, -100),
        Total("2026-10", "GBP", 3, None, 0, -100),
        Total("2026-10", "JPY", 0, None, 0, -100),
    ]
    assert str(monthly([entry("A", -100, currency="JPY", scale=0)], [])[0].spending) == "-100"


def test_ac31_payees_taking_the_most_in_order_with_totals_and_counts() -> None:
    payments = [
        entry("A", -100),
        entry("A", -150),
        entry("B", -300),
        entry("C", -200),
        entry("D", -50),
    ]
    assert payees(payments, 3) == [
        Payee("GBP", 2, "B", -300, 1),
        Payee("GBP", 2, "A", -250, 2),
        Payee("GBP", 2, "C", -200, 1),
    ]
    assert str(payees(payments, 1)[0].total) == "-3.00"


def test_ac31_payees_are_per_currency_and_ties_go_to_the_earlier_description() -> None:
    payments = [
        entry("X", -100, currency="EUR"),
        entry("Y", -100, currency="EUR"),
        entry("X", -100, currency="GBP"),
        entry("Z", -900, currency="GBP"),
    ]
    assert payees(payments, 1) == [
        Payee("EUR", 2, "X", -100, 1),
        Payee("GBP", 2, "Z", -900, 1),
    ]
    assert payees([], 3) == []


def test_ac31_a_payees_total_past_sqlites_integer_is_still_exact() -> None:
    assert payees([entry("A", -BIG), entry("A", -BIG)], 1) == [Payee("GBP", 2, "A", -2 * BIG, 2)]


def test_ac31_the_store_reads_a_period_inclusive_and_payments_grouped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = samples(tmp_path, capsys)
    with Store.open(place.data_dir) as store:
        assert [e.id for e in store.entries(("2026-10", "2026-10"))] == [
            1, 9, 2, 6, 3, 4, 7, 8, 5, 10,
        ]  # fmt: skip
        assert list(store.entries(("2026-09", "2026-09"))) == []
        assert list(store.entries(("2026-11", "2026-12"))) == []
        assert len(list(store.entries())) == 10
        assert [(e.currency, e.description) for e in store.payments(("2026-10", "2026-10"))] == [
            ("EUR", "AMAZON MARKETPLACE"),
            ("EUR", "CAFÉ NERO"),
            ("GBP", "DIRECT DEBIT COUNCIL TAX"),
            ("GBP", "TESCO STORES 2045, LEEDS"),
            ("GBP", "TFL TRAVEL CH"),
            ("GBP", "TRANSFER TO CURRENT"),
        ]
