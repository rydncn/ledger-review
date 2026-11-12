import pytest

from tally.config import parse_config
from tally.errors import UsageError
from tally.report import by_category, date_range, month_bounds, parse_month, top_payees, totals
from tally.rules import UNCATEGORISED, categorise, explain
from tally.store import Transaction


def txn(i, amount, description, account="current", date="2026-10-01", memo=None, currency="GBP"):
    return Transaction(
        id=i,
        account_id=1,
        account=account,
        currency=currency,
        scale=2,
        date=date,
        amount_minor=amount,
        description=description,
        memo=memo,
        source_line=None,
        import_id=1,
    )


RULES = parse_config(
    '[[rules]]\ncategory = "Groceries"\ndescription = "tesco"\n'
    '[[rules]]\ncategory = "Big"\namount_max = "-100.00"\n'
    '[[rules]]\ncategory = "Card stuff"\naccount = "card"\npattern = "^amazon"\n'
    '[[rules]]\ncategory = "Memo match"\ndescription = "ref99"\n'
).rules


def test_first_match_wins_and_is_case_insensitive():
    category, rule = categorise(RULES, txn(1, -15000, "Tesco Extra"))
    assert category == "Groceries" and rule.index == 1


def test_amount_range_in_minor_units():
    assert categorise(RULES, txn(1, -10000, "x"))[0] == "Big"
    assert categorise(RULES, txn(1, -9999, "x"))[0] == UNCATEGORISED


def test_account_and_pattern():
    assert categorise(RULES, txn(1, -5, "AMAZON MARKETPLACE", account="card"))[0] == "Card stuff"
    assert (
        categorise(RULES, txn(1, -5, "AMAZON MARKETPLACE", account="current"))[0] == UNCATEGORISED
    )


def test_memo_is_searched_too():
    assert categorise(RULES, txn(1, -5, "DIRECT DEBIT", memo="REF99 water"))[0] == "Memo match"


def test_explain_lists_failures_up_to_the_match():
    verdicts = explain(RULES, txn(1, -5, "AMAZON", account="card"))
    assert [(v.rule.index, v.matched) for v in verdicts] == [(1, False), (2, False), (3, True)]
    assert verdicts[0].failed_condition == 'description contains "tesco"'
    assert verdicts[1].failed_condition == "amount is at most the maximum"
    assert verdicts[2].failed_condition is None


def test_explain_unmatched_tries_every_rule():
    verdicts = explain(RULES, txn(1, 5, "nothing"))
    assert len(verdicts) == 4 and not any(v.matched for v in verdicts)


def test_month_helpers():
    assert parse_month("2026-02", "--month") == "2026-02"
    assert month_bounds("2026-02") == ("2026-02-01", "2026-02-28")
    assert month_bounds("2024-02") == ("2024-02-01", "2024-02-29")
    assert month_bounds("2026-12") == ("2026-12-01", "2026-12-31")
    assert date_range("2026-01", "2026-03") == ("2026-01-01", "2026-03-31")
    assert date_range(None, None) == (None, None)
    with pytest.raises(UsageError):
        parse_month("2026-13", "--month")
    with pytest.raises(UsageError):
        parse_month("October", "--month")
    with pytest.raises(UsageError):
        date_range("2026-05", "2026-04")


def test_by_category_sums_exactly_and_splits_currency():
    txns = [
        txn(1, -4217, "TESCO"),
        txn(2, -1, "TESCO", date="2026-10-31"),
        txn(3, 245000, "SALARY"),
        txn(4, -15000, "RENT"),
        txn(5, -420, "CAFE", currency="EUR"),
        txn(6, -100, "TESCO", date="2026-11-01"),
    ]
    lines = by_category(txns, RULES)
    key = {(ln.month, ln.currency, ln.category): (ln.spending, ln.income, ln.count) for ln in lines}
    assert key[("2026-10", "GBP", "Groceries")] == (4218, 0, 2)
    assert key[("2026-10", "GBP", "Big")] == (15000, 0, 1)
    assert key[("2026-10", "GBP", UNCATEGORISED)] == (0, 245000, 1)
    assert key[("2026-10", "EUR", UNCATEGORISED)] == (420, 0, 1)
    assert key[("2026-11", "GBP", "Groceries")] == (100, 0, 1)
    # Sorted by month, currency, then biggest spending first.
    assert [(ln.month, ln.currency, ln.category) for ln in lines][:3] == [
        ("2026-10", "EUR", UNCATEGORISED),
        ("2026-10", "GBP", "Big"),
        ("2026-10", "GBP", "Groceries"),
    ]
    tot = {(t.month, t.currency): (t.spending, t.income) for t in totals(lines)}
    assert tot[("2026-10", "GBP")] == (19218, 245000)
    assert tot[("2026-10", "EUR")] == (420, 0)


def test_exclude_category():
    lines = by_category([txn(1, -4217, "TESCO"), txn(2, -5, "x")], RULES, {"groceries"})
    assert [ln.category for ln in lines] == [UNCATEGORISED]


def test_top_payees_groups_spellings_and_ignores_income():
    txns = [
        txn(1, -100, "Tesco  Stores"),
        txn(2, -200, "TESCO STORES"),
        txn(3, -50, "tesco stores"),
        txn(4, -1000, "RENT"),
        txn(5, 5000, "SALARY"),
        txn(6, -1, "A"),
        txn(7, -9, "B", currency="EUR"),
    ]
    lines = top_payees(txns, RULES, 2)
    assert [(ln.currency, ln.payee, ln.spending, ln.count) for ln in lines] == [
        ("EUR", "B", 9, 1),
        ("GBP", "RENT", 1000, 1),
        ("GBP", "Tesco  Stores", 350, 3),
    ]
    assert len(top_payees(txns, RULES, 0)) == 4
    assert [ln.payee for ln in top_payees(txns, RULES, 0, {"Groceries"})] == ["B", "RENT", "A"]


def test_rule_amounts_apply_to_accounts_with_other_decimal_places():
    rules = parse_config('[[rules]]\ncategory = "Big"\namount_max = "-1000"\n').rules
    yen = Transaction(
        id=1,
        account_id=1,
        account="yen",
        currency="JPY",
        scale=0,
        date="2026-10-01",
        amount_minor=-5000,
        description="RENT",
        memo=None,
        source_line=None,
        import_id=1,
    )
    assert categorise(rules, yen)[0] == "Big"
    dinar = Transaction(
        id=2,
        account_id=2,
        account="kwd",
        currency="KWD",
        scale=3,
        date="2026-10-01",
        amount_minor=-10000,
        description="X",
        memo=None,
        source_line=None,
        import_id=1,
    )  # 10.000 KWD out: not below -1000
    assert categorise(rules, dinar)[0] == UNCATEGORISED
    assert categorise(rules, txn(3, -100000, "x"))[0] == "Big"  # -1000.00 GBP, inclusive
    assert categorise(rules, txn(4, -99999, "x"))[0] == UNCATEGORISED
