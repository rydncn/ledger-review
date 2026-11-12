"""AC26, AC28's texts and AC29: the rules loaded from ``tally.toml`` and ``rules.categorise``."""

from __future__ import annotations

import time

import pytest

from tally.config import ConfigError
from tally.rules import Match, NoMatch, Verdict, categorise, load
from tally.store import Entry

AC26_RULES: list[dict[str, object]] = [
    {"name": "Groceries", "description_contains": "TESCO"},
    {"name": "Travel", "description_matches": "^TFL"},
    {"name": "Big", "account": "current", "amount_below": "-100.00"},
    {"name": "Card payments", "account": "card", "amount_above": "0"},
]


HIDDEN = "name must not contain control characters or line breaks"


def entry(description: str, minor: int, account: str = "current", scale: int = 2) -> Entry:
    return Entry(1, "2026-10-01", account, "GBP", scale, description, minor)


def category(e: Entry, tables: list[dict[str, object]] = AC26_RULES) -> str | None:
    result = categorise(e, load(tables))
    return result.rule.name if isinstance(result, Match) else None


@pytest.mark.parametrize(
    ("e", "expected"),
    [
        (entry("TESCO STORES 2045, LEEDS", -4217), "Groceries"),
        (entry("TFL TRAVEL CH", -340), "Travel"),
        (entry("DIRECT DEBIT COUNCIL TAX", -15600), "Big"),
        (entry("PAYMENT RECEIVED - THANK YOU", 15000, "card"), "Card payments"),
        (entry("TESCO STORES 2045, LEEDS", -15000), "Groceries"),  # first match wins, not Big
        (entry("TRANSFER TO CURRENT", -50000, "savings"), None),  # Big names another account
        (entry("SALARY ACME LTD", 245000), None),
        (entry("CAFÉ NERO", -420, "card"), None),
        (entry("AMAZON MARKETPLACE", -2399, "card"), None),
        (entry("INTEREST", 1234, "savings"), None),
    ],
)
def test_ac26_rules_match_on_description_amount_and_account_first_match_wins(
    e: Entry, expected: str | None
) -> None:
    assert category(e) == expected


def test_ac26_description_contains_ignores_case_and_matches_searches() -> None:
    assert category(entry("Tesco Metro", -100)) == "Groceries"
    assert category(entry("tfl travel", -100)) is None  # the pattern is case-sensitive
    assert category(entry("x", -100), [{"name": "T", "description_matches": "TFL"}]) is None
    assert category(entry("PAID TFL", -100), [{"name": "T", "description_matches": "TFL"}]) == "T"
    assert category(entry("tfl", -1), [{"name": "T", "description_matches": "(?i)^TFL"}]) == "T"


@pytest.mark.parametrize(
    ("minor", "rule", "expected"),
    [
        (-10000, {"amount_below": "-100.00"}, False),  # strict: equal is not below
        (-10001, {"amount_below": "-100.00"}, True),
        (0, {"amount_above": "0"}, False),
        (1, {"amount_above": "0"}, True),
        (1, {"amount_above": "-0"}, True),
        (5, {"amount_above": "0.004"}, True),
        (0, {"amount_above": "-0.001"}, True),
        (-1, {"amount_above": "-0.001"}, False),
        (500, {"amount_above": "4.99", "amount_below": "5.01"}, True),
        (501, {"amount_above": "4.99", "amount_below": "5.01"}, False),
        (499, {"amount_above": "4.99", "amount_below": "5.01"}, False),
        (100, {"amount_above": "+0.99"}, True),
    ],
)
def test_ac26_amounts_compare_exactly_and_strictly(
    minor: int, rule: dict[str, object], expected: bool
) -> None:
    assert (category(entry("x", minor), [{"name": "R", **rule}]) == "R") is expected


def test_ac26_an_amount_is_in_the_accounts_own_decimal_places() -> None:
    yen = entry("x", 150, scale=0)  # 150 yen
    assert category(yen, [{"name": "R", "amount_above": "149.999"}]) == "R"
    assert category(yen, [{"name": "R", "amount_above": "150"}]) is None
    dinar = entry("x", 1001, scale=3)  # 1.001
    assert category(dinar, [{"name": "R", "amount_above": "1.000"}]) == "R"
    assert category(dinar, [{"name": "R", "amount_below": "1.001"}]) is None


def test_ac26_a_huge_amount_compares_exactly() -> None:
    huge = entry("x", 9_223_372_036_854_775_807)
    assert category(huge, [{"name": "R", "amount_above": "92233720368547758.06"}]) == "R"
    assert category(huge, [{"name": "R", "amount_above": "92233720368547758.07"}]) is None


def test_ac26_a_rule_with_only_a_name_matches_everything() -> None:
    rules: list[dict[str, object]] = [*AC26_RULES, {"name": "Other"}]
    assert category(entry("SALARY ACME LTD", 245000), rules) == "Other"
    assert category(entry("", 0, "savings"), rules) == "Other"
    assert category(entry("", 0), []) is None


def test_ac28_a_match_names_its_conditions_and_the_earlier_rules_that_failed() -> None:
    result = categorise(entry("TFL TRAVEL CH", -340), load(AC26_RULES))
    assert isinstance(result, Match)
    rules = result.rule, *(v.rule for v in result.verdicts)
    assert [r.position for r in rules] == [2, 1, 2]
    assert [(v.matched, v.conditions) for v in result.verdicts] == [
        (False, ("description does not contain TESCO",)),
        (True, ("description matches ^TFL",)),
    ]


def test_ac28_no_match_lists_each_rules_failing_condition() -> None:
    result = categorise(entry("SALARY ACME LTD", 245000), load(AC26_RULES))
    assert isinstance(result, NoMatch)
    assert [(v.rule.name, v.matched, v.conditions) for v in result.verdicts] == [
        ("Groceries", False, ("description does not contain TESCO",)),
        ("Travel", False, ("description does not match ^TFL",)),
        ("Big", False, ("amount is not below -100.00",)),
        ("Card payments", False, ("account is not card",)),
    ]


def test_ac28_every_condition_is_named_when_a_rule_matches() -> None:
    rule = {
        "name": "All",
        "account": "card",
        "description_contains": "nero",
        "description_matches": "É",
        "amount_above": "-5",
        "amount_below": "-4.00",
    }
    result = categorise(entry("CAFÉ NERO", -420, "card"), load([rule]))
    assert result == Match(
        load([rule])[0],
        (
            Verdict(
                load([rule])[0],
                True,
                (
                    "account is card",
                    "description contains nero",
                    "description matches É",
                    "amount is above -5",
                    "amount is below -4.00",
                ),
            ),
        ),
    )


@pytest.mark.parametrize(
    ("e", "failed"),
    [
        (entry("CAFÉ NERO", -420, "current"), "account is not card"),
        (entry("CAFÉ", -420, "card"), "description does not contain nero"),
        (entry("CAFE NERO", -420, "card"), "description does not match É"),
        (entry("CAFÉ NERO", -500, "card"), "amount is not above -5"),
        (entry("CAFÉ NERO", -400, "card"), "amount is not below -4.00"),
    ],
)
def test_ac28_the_first_condition_that_fails_is_named(e: Entry, failed: str) -> None:
    rule = {
        "name": "All",
        "account": "card",
        "description_contains": "nero",
        "description_matches": "É",
        "amount_above": "-5",
        "amount_below": "-4.00",
    }
    result = categorise(e, load([rule]))
    assert isinstance(result, NoMatch)
    assert result.verdicts[0].conditions == (failed,)


def test_ac28_a_rule_with_no_conditions_has_none_to_name() -> None:
    result = categorise(entry("x", 1), load([{"name": "Other"}]))
    assert isinstance(result, Match)
    assert result.verdicts[0].conditions == ()


@pytest.mark.parametrize(
    ("tables", "message"),
    [
        (
            [{"name": "A"}, {"name": "B", "description_matches": "("}],
            "rules[2]: description_matches is not a valid pattern:"
            " missing ), unterminated subpattern",
        ),
        (
            [{"name": "A", "amount_below": "ten"}],
            'rules[1]: amount_below is not an amount like "-100.00"',
        ),
        ([{"name": "A"}, {"name": "B"}, {"description_contains": "X"}], "rules[3]: no name"),
        ([{"name": "A", "category": "B"}], 'rules[1]: unknown key "category"'),
        ([{"name": 3}], "rules[1]: name must be a string"),
        ([{"name": "A", "amount_below": -100}], "rules[1]: amount_below must be a string"),
        ([{"name": "A", "account": ["x"]}], "rules[1]: account must be a string"),
        ([{"name": "  "}], "rules[1]: name must not be empty"),
        ([{"name": "A\x1b[31m"}], f"rules[1]: {HIDDEN}"),
        ([{"name": "A" + chr(0x202E)}], f"rules[1]: {HIDDEN}"),
        (
            [{"name": "A", "description_contains": ""}],
            "rules[1]: description_contains must not be empty",
        ),
        (
            [{"name": "A", "description_matches": ""}],
            "rules[1]: description_matches must not be empty",
        ),
        (
            [{"name": "A", "description_matches": "a{4294967296}"}],
            "rules[1]: description_matches is not a valid pattern:"
            " the repetition number is too large",
        ),
        (
            [{"name": "A", "description_matches": "(" * 5000 + ")" * 5000}],
            "rules[1]: description_matches is not a valid pattern: nested too deeply",
        ),
        (
            [{"name": "A", "amount_above": "5", "amount_below": "5"}],
            "rules[1]: amount_above must be less than amount_below",
        ),
        (
            [{"name": "A", "amount_above": "6", "amount_below": "5.00"}],
            "rules[1]: amount_above must be less than amount_below",
        ),
    ],
)
def test_ac29_a_broken_rule_is_a_configuration_error_naming_its_position(
    tables: list[dict[str, object]], message: str
) -> None:
    with pytest.raises(ConfigError) as raised:
        load(tables)
    assert str(raised.value) == message


@pytest.mark.parametrize(
    "text",
    [
        "ten",
        "",
        " 5",
        "5 ",
        "1_000",
        "1,000",
        "NaN",
        "Infinity",
        "1e5",
        chr(0x663),  # an Arabic-Indic digit, which Decimal and int read
        "5.",
        ".5",
        "--5",
        "0.0001",  # more places than any currency has
        "1" * 20,  # more whole digits than an amount can have
        "£5",
        "(5)",
    ],
)
def test_ac29_a_rule_amount_has_one_shape(text: str) -> None:
    for key in ("amount_above", "amount_below"):
        with pytest.raises(
            ConfigError, match=rf'^rules\[1\]: {key} is not an amount like "-100.00"$'
        ):
            load([{"name": "A", key: text}])


@pytest.mark.parametrize("text", ["0", "-0", "+5", "-100.00", "0.001", "1" * 19, "1" * 19 + ".999"])
def test_ac29_a_rule_amount_in_that_shape_loads(text: str) -> None:
    assert load([{"name": "A", "amount_below": text}])[0].position == 1


def test_ac29_no_rules_is_no_rules() -> None:
    assert load([]) == []


def test_ac29_a_pattern_reads_a_bounded_part_of_a_hostile_description() -> None:
    # An unanchored pattern backtracks quadratically over a long subject: 1 MiB took minutes.
    rules = load([{"name": "Prime", "description_matches": ".*PRIME"}])
    hostile = entry("A" * 200_000, -100)  # about 4 s unbounded, so a failure is quick
    started = time.perf_counter()
    assert isinstance(categorise(hostile, rules), NoMatch)
    assert time.perf_counter() - started < 1
    assert isinstance(categorise(entry("A" * 990 + "PRIME", -100), rules), Match)
    assert isinstance(categorise(entry("A" * 1000 + "PRIME", -100), rules), NoMatch)


@pytest.mark.parametrize(
    ("name", "said"),
    [
        ("a" + chr(0x2028) + "b", "name must not contain control characters or line breaks"),
        ("a" + chr(0x2029) + "b", "name must not contain control characters or line breaks"),
        ("uncategorised", 'name must not be "uncategorised", which the report gives no match'),
    ],
)
def test_ac29_a_rule_name_the_report_would_show_otherwise_is_refused(name: str, said: str) -> None:
    with pytest.raises(ConfigError, match=f"^rules\\[1\\]: {said}$"):
        load([{"name": name}])
