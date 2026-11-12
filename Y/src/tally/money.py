"""Exact amounts: a ``Decimal`` with a currency, and the integer minor units
the data file stores (ADR 0004). Parsing goes from text straight to
``Decimal``; nothing here is ever a binary fraction.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal

NOT_A_NUMBER = "amount is not a number"

# ISO 4217 currencies whose minor unit is not a hundredth.
_SCALES = {
    "BHD": 3,
    "IQD": 3,
    "JOD": 3,
    "KWD": 3,
    "LYD": 3,
    "OMR": 3,
    "TND": 3,
    "BIF": 0,
    "CLP": 0,
    "DJF": 0,
    "GNF": 0,
    "ISK": 0,
    "JPY": 0,
    "KMF": 0,
    "KRW": 0,
    "PYG": 0,
    "RWF": 0,
    "UGX": 0,
    "VND": 0,
    "VUV": 0,
    "XAF": 0,
    "XOF": 0,
    "XPF": 0,
}

_SIGNS = "+-"


class AmountError(ValueError):
    """An amount text that cannot be read, or an amount that does not fit its scale."""


@dataclass(frozen=True)
class Money:
    """An exact amount in one currency."""

    amount: Decimal
    currency: str


def scale_of(currency: str) -> int:
    """How many decimal places the currency's minor unit has."""
    return _SCALES.get(currency, 2)


def parse_amount(text: str, decimal: str, thousands: str) -> Decimal:
    """The exact value of an amount as a statement writes it (AC44).

    Accepts an optional sign before or after, a currency symbol before the
    digits (with the sign on either side of it), parentheses for a negative,
    thousands separators in groups of three, and a fraction with or without a
    leading digit. Anything else raises ``AmountError``.
    """
    s = text.strip()
    bracketed = s.startswith("(") and s.endswith(")")
    if bracketed:
        s = s[1:-1]
    negations = int(bracketed)
    if s and s[0] in _SIGNS:
        negations += s[0] == "-"
        s = s[1:]
    # A currency symbol: one character Unicode calls one (Sc). Anything wider took a
    # typographic minus (U+2212, a dash) for a symbol and made a debit a credit.
    if s and unicodedata.category(s[0]) == "Sc":
        s = s[1:]
    if s and s[0] in _SIGNS:
        negations += s[0] == "-"
        s = s[1:]
    if s.endswith("-"):
        negations += 1
        s = s[:-1]
    if negations > 1 or not s:
        raise AmountError(NOT_A_NUMBER)

    whole, sep, fraction = s.partition(decimal)
    # [0-9], never \d: \d matches Arabic-Indic and fullwidth digits, which Decimal reads too.
    grouped = re.fullmatch(rf"[0-9]{{1,3}}(?:{re.escape(thousands)}[0-9]{{3}})+", whole)
    if not (grouped or re.fullmatch(r"[0-9]*", whole)):
        raise AmountError(NOT_A_NUMBER)
    if sep and not re.fullmatch(r"[0-9]+", fraction):
        raise AmountError(NOT_A_NUMBER)
    digits = whole.replace(thousands, "")
    # Built as text, never by arithmetic: Decimal's context would round at 28 digits.
    return Decimal(f"{'-' if negations else ''}{digits or '0'}.{fraction or '0'}")


def to_minor(amount: Decimal, scale: int) -> int:
    """The amount as integer minor units, refusing one that does not fit the scale."""
    sign, digits, exponent = amount.as_tuple()
    if not isinstance(exponent, int):
        raise AmountError(NOT_A_NUMBER)
    shifted = Decimal((sign, digits, exponent + scale))  # exact, unlike scaleb
    if shifted != shifted.to_integral_value():
        raise AmountError(f"amount has more than {scale} decimal places")
    # More than 19 whole digits is past SQLite's INTEGER, and int() of a huge Decimal costs
    # its length squared, so it is refused before converting. no mutate: at 20 or 21 the
    # range check below refuses the same amounts, and only the cost differs.
    if len(digits) + exponent + scale > 19:  # pragma: no mutate
        raise AmountError("amount is too large to store")
    minor = int(shifted)
    if not -(2**63) <= minor < 2**63:  # SQLite's INTEGER
        raise AmountError("amount is too large to store")
    return minor


def from_minor(minor: int, scale: int) -> Decimal:
    """Integer minor units back to an exact amount with ``scale`` decimal places."""
    digits = tuple(map(int, str(abs(minor))))
    return Decimal((int(minor < 0), digits, -scale))  # from digits, never arithmetic: exact
