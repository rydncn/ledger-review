"""AC44: amounts in every form a statement uses, exactly."""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

from tally.money import AmountError, Money, from_minor, parse_amount, scale_of, to_minor

READABLE = [
    ("1,234.56", "1234.56"),
    ("(12.50)", "-12.50"),
    ("-12.50", "-12.50"),
    ("12.50-", "-12.50"),
    ("£12.50", "12.50"),
    ("-£12.50", "-12.50"),
    ("£-12.50", "-12.50"),
    ("0", "0.00"),
    (".5", "0.50"),
    ("1,234", "1234.00"),
    ("(1,234.56)", "-1234.56"),
    ("(0.07)", "-0.07"),
    ("12.57-", "-12.57"),
    ("€0.01", "0.01"),
    (" 12.50 ", "12.50"),
    ("+12.50", "12.50"),
]
UNREADABLE = [
    "12.50 CR",
    "12.50 DR",
    "1,2,3.4",
    "12.5.0",
    ",",
    "--12.50",
    "(12.50",
    "",
    "12.",
    "(-1)",
    "-12.50-",
    "12.50)",
    "X12.50",
    ".",
    "-£-12.50",
    "£--12.50",
    "١٢٣.٤٥",  # Arabic-Indic digits, which \d matches, Decimal reads
    "１,２３４",  # noqa: RUF001 (fullwidth digits, on purpose)
    "12.٥",  # noqa: RUF001 (an Arabic-Indic five, on purpose)
]


@pytest.mark.parametrize(("text", "expected"), READABLE)
def test_ac44_readable_amounts_are_exact(text: str, expected: str) -> None:
    amount = parse_amount(text, decimal=".", thousands=",")
    assert amount == Decimal(expected)
    assert isinstance(amount, Decimal)


@pytest.mark.parametrize("text", UNREADABLE)
def test_ac44_unreadable_amounts_say_so(text: str) -> None:
    with pytest.raises(AmountError, match=r"^amount is not a number$"):
        parse_amount(text, decimal=".", thousands=",")


def test_ac44_decimal_comma_layout() -> None:
    assert parse_amount("1.234,56", decimal=",", thousands=".") == Decimal("1234.56")
    with pytest.raises(AmountError, match=r"^amount is not a number$"):
        parse_amount("1,234.56", decimal=",", thousands=".")


def test_ac44_minor_units_round_trip_at_every_scale() -> None:
    assert to_minor(Decimal("12.50"), 2) == 1250
    assert to_minor(Decimal("-0.3"), 2) == -30
    assert to_minor(Decimal("1234"), 0) == 1234
    assert to_minor(Decimal("1.234"), 3) == 1234
    assert str(from_minor(1250, 2)) == "12.50"
    assert str(from_minor(-30, 2)) == "-0.30"
    assert str(from_minor(1234, 0)) == "1234"
    assert str(from_minor(1234, 3)) == "1.234"
    with pytest.raises(AmountError, match=r"^amount has more than 2 decimal places$"):
        to_minor(Decimal("12.345"), 2)


def test_ac44_scale_is_two_unless_the_currency_says_otherwise() -> None:
    assert scale_of("GBP") == 2
    assert scale_of("EUR") == 2
    assert scale_of("JPY") == 0
    assert scale_of("KWD") == 3


def test_money_holds_an_exact_amount_and_a_currency() -> None:
    money = Money(Decimal("12.50"), "GBP")
    assert money.amount == Decimal("12.50")
    assert money.currency == "GBP"
    assert money == Money(Decimal("12.5"), "GBP")


def test_ac44_thirty_digit_amounts_are_exact_and_the_data_file_bound_is_refused() -> None:
    big = "9" * 30 + ".99"
    assert str(parse_amount("-" + big, decimal=".", thousands=",")) == "-" + big
    assert str(parse_amount("(" + big + ")", decimal=".", thousands=",")) == "-" + big
    assert to_minor(Decimal("12345678901234567.89"), 2) == 1234567890123456789
    assert to_minor(Decimal("-92233720368547758.08"), 2) == -9223372036854775808
    with pytest.raises(AmountError, match=r"^amount is too large to store$"):
        to_minor(Decimal("92233720368547758.08"), 2)
    with pytest.raises(AmountError, match=r"^amount is too large to store$"):
        to_minor(Decimal("-" + big), 2)
    with pytest.raises(AmountError, match=r"^amount is too large to store$"):
        to_minor(Decimal("-92233720368547758.09"), 2)
    with pytest.raises(AmountError, match=r"^amount is not a number$"):
        to_minor(Decimal("NaN"), 2)


def test_minor_units_come_back_exactly_past_the_decimal_contexts_precision() -> None:
    """from_minor is built from digits, never by arithmetic, which rounds at 28 digits."""
    minor = int("9" * 40)
    assert str(from_minor(minor, 2)) == "9" * 38 + ".99"
    assert str(from_minor(-minor, 3)) == "-" + "9" * 37 + ".999"
    assert str(from_minor(0, 2)) == "0.00"
    assert str(from_minor(5, 0)) == "5"


def test_ac44_a_huge_amount_is_refused_before_it_is_converted() -> None:
    """Turning a Decimal of n digits into an int costs about n squared: 1.6 s for 300,000."""
    started = time.monotonic()
    for digits in (300_000, 20):
        with pytest.raises(AmountError, match=r"^amount is too large to store$"):
            to_minor(Decimal("9" * digits), 2)
        with pytest.raises(AmountError, match=r"^amount is too large to store$"):
            to_minor(Decimal("-" + "9" * digits + ".5"), 1)
    assert time.monotonic() - started < 0.5
    assert to_minor(Decimal("1." + "0" * 300_000), 2) == 100
    assert to_minor(Decimal("0" * 300_000 + "1"), 2) == 100


@pytest.mark.parametrize("minus", [chr(c) for c in (0x2212, 0x2013, 0x2014, 0xFF0D)])
def test_ac44_a_dash_that_is_not_a_hyphen_is_not_a_currency_symbol(minus: str) -> None:
    # Read as a symbol and dropped, a typographic minus made a debit a credit.
    with pytest.raises(AmountError):
        parse_amount(f"{minus}12.50", ".", ",")
    assert parse_amount("£12.50", ".", ",") == Decimal("12.50")
    assert parse_amount("-€12.50", ".", ",") == Decimal("-12.50")
