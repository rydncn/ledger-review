import pytest

from tally.money import (
    AmountError,
    default_scale,
    format_minor,
    parse_amount,
    parse_decimal_string,
    validate_currency,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("42.17", 4217),
        ("1,234.56", 123456),
        ("1,234,567.89", 123456789),
        ("(12.50)", -1250),
        ("-12.50", -1250),
        ("12.50-", -1250),
        ("+12.50", 1250),
        ("£12.50", 1250),
        ("£-12.50", -1250),
        ("-£12.50", -1250),
        ("GBP 12.50", 1250),
        ("12.50 GBP", 1250),
        ("12.50 CR", 1250),
        ("12.50 DR", -1250),
        ("12.5", 1250),
        ("12", 1200),
        (".50", 50),
        ("0", 0),
        ("  42.17  ", 4217),
        ("(1,234.56)", -123456),
    ],
)
def test_parse_uk_style(text, expected):
    assert parse_amount(text) == expected


def test_blank_is_none():
    assert parse_amount("") is None
    assert parse_amount("   ") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [("-4,20", -420), ("150,00", 15000), ("1.234,56", 123456), ("-1.234.567,89", -123456789)],
)
def test_parse_continental_style(text, expected):
    assert parse_amount(text, decimal_separator=",", thousands_separator=".") == expected


def test_space_thousands():
    assert parse_amount("1 234,56", decimal_separator=",", thousands_separator=" ") == 123456


@pytest.mark.parametrize(
    "text",
    [
        "4,20",
        "1,23.45",
        "12.345",
        "abc",
        "12.5O",
        "1..2",
        "--5",
        "£--5",
        "-£-5",
        "£",
        "12,34,56.00",
        "1e3",
        "٣",
    ],
)
def test_rejects_malformed(text):
    with pytest.raises(AmountError):
        parse_amount(text)


def test_rejects_too_many_decimals_for_currency():
    with pytest.raises(AmountError, match="decimal places"):
        parse_amount("12.5", scale=0)
    assert parse_amount("12.345", scale=3) == 12345


def test_error_messages_never_quote_the_value():
    for text in ["secret 12.34", "abc", "12.345"]:
        with pytest.raises(AmountError) as exc:
            parse_amount(text)
        assert text not in str(exc.value)


def test_separators_must_differ():
    with pytest.raises(AmountError):
        parse_amount("1.00", decimal_separator=".", thousands_separator=".")


def test_format_minor():
    assert format_minor(-4217) == "-42.17"
    assert format_minor(5) == "0.05"
    assert format_minor(0) == "0.00"
    assert format_minor(245000) == "2450.00"
    assert format_minor(1200, scale=0) == "1200"
    assert format_minor(-12345, scale=3) == "-12.345"


def test_round_trip_exact():
    for minor in range(-100000, 100000, 7):
        assert parse_amount(format_minor(minor), thousands_separator="") == minor


def test_parse_decimal_string():
    assert parse_decimal_string("-50.00") == -5000
    assert parse_decimal_string("0.01") == 1
    assert parse_decimal_string(100) == 10000
    with pytest.raises(AmountError):
        parse_decimal_string("1,000.00")


def test_currency_helpers():
    assert validate_currency(" gbp ") == "GBP"
    assert default_scale("GBP") == 2
    assert default_scale("JPY") == 0
    assert default_scale("KWD") == 3
    with pytest.raises(ValueError):
        validate_currency("pounds")
