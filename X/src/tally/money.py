"""Exact money handling.

Amounts are integers of minor units (pence, cents) everywhere money is
held or summed. Floating point is never used. ``scale`` is the number of
decimal places the currency has: 2 for GBP, 0 for JPY, 3 for KWD.
"""

from __future__ import annotations

import re

# ISO 4217 minor-unit exponents for currencies that do not use 2.
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

_CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")
_CURRENCY_MARKS = "£$€¥₹"
_LEADING_CODE = re.compile(r"^[A-Za-z]{3}\s*")
_TRAILING_CODE = re.compile(r"\s*[A-Za-z]{3}$")


def default_scale(currency: str) -> int:
    return _SCALES.get(currency.upper(), 2)


def validate_currency(code: str) -> str:
    code = code.strip().upper()
    if not _CURRENCY_CODE.match(code):
        raise ValueError("currency must be a three-letter code such as GBP")
    return code


class AmountError(ValueError):
    """The text is not an amount we can read. The message never quotes the text."""


def parse_amount(
    text: str,
    *,
    decimal_separator: str = ".",
    thousands_separator: str = ",",
    scale: int = 2,
) -> int | None:
    """Turn the text of an amount into minor units, or None if it is blank.

    Understands ``1,234.56``, ``(12.50)`` and ``-12.50`` as well as a
    trailing ``-``, a leading ``+``, ``CR``/``DR`` suffixes and a currency
    mark or code. Thousands grouping is checked, so ``4,20`` with a ``,``
    thousands separator is rejected rather than read as 420.
    """
    if decimal_separator == thousands_separator:
        raise AmountError("decimal and thousands separators must differ")
    s = text.strip()
    if not s:
        return None

    negative = False
    # Parentheses mean negative, in the accounting style.
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()
    # CR/DR suffixes: debit is money out.
    upper = s.upper()
    if upper.endswith("DR"):
        negative = not negative
        s = s[:-2].strip()
    elif upper.endswith("CR"):
        s = s[:-2].strip()
    # Leading or trailing sign.
    sign_seen = False
    if s.startswith("-"):
        negative = not negative
        sign_seen = True
        s = s[1:].strip()
    elif s.startswith("+"):
        sign_seen = True
        s = s[1:].strip()
    elif s.endswith("-"):
        negative = not negative
        sign_seen = True
        s = s[:-1].strip()
    # Currency marks and codes.
    s = s.lstrip(_CURRENCY_MARKS).rstrip(_CURRENCY_MARKS).strip()
    if _LEADING_CODE.match(s) and len(s) > 3:
        s = _LEADING_CODE.sub("", s, count=1)
    elif _TRAILING_CODE.search(s) and len(s) > 3:
        s = _TRAILING_CODE.sub("", s, count=1)
    # A sign may also follow the currency mark: "£-12.50", but only one sign.
    if s.startswith("-") and not sign_seen:
        negative = not negative
        s = s[1:].strip()
    if not s:
        raise AmountError("amount has no digits")

    if decimal_separator in s:
        integer_part, _, fraction = s.partition(decimal_separator)
        if decimal_separator in fraction:
            raise AmountError("amount has more than one decimal separator")
    else:
        integer_part, fraction = s, ""

    if thousands_separator and thousands_separator in integer_part:
        groups = integer_part.split(thousands_separator)
        if not groups[0] or len(groups[0]) > 3 or any(len(g) != 3 for g in groups[1:]):
            raise AmountError("amount has malformed thousands grouping")
        integer_part = "".join(groups)
    if thousands_separator == " ":
        integer_part = integer_part.replace(" ", "")

    if not integer_part:
        integer_part = "0"
    if not integer_part.isdigit() or not integer_part.isascii():
        raise AmountError("amount contains characters that are not digits")
    if fraction and (not fraction.isdigit() or not fraction.isascii()):
        raise AmountError("amount contains characters that are not digits")
    if len(fraction) > scale:
        raise AmountError(f"amount has more than {scale} decimal places")

    minor = int(integer_part) * 10**scale + int(fraction.ljust(scale, "0") or 0)
    return -minor if negative else minor


def parse_decimal_string(text: str, scale: int = 2) -> int:
    """Parse a plain machine-style amount such as ``-12.50`` or ``1200``.

    Used for configuration values and command-line options, where only the
    ``.`` decimal separator and no grouping is accepted.
    """
    value = parse_amount(str(text), decimal_separator=".", thousands_separator="", scale=scale)
    if value is None:
        raise AmountError("amount is blank")
    return value


def format_minor(minor: int, scale: int = 2) -> str:
    """Render minor units as a plain decimal string: ``-42.17``, ``1200``."""
    sign = "-" if minor < 0 else ""
    minor = abs(minor)
    if scale == 0:
        return f"{sign}{minor}"
    whole, frac = divmod(minor, 10**scale)
    return f"{sign}{whole}.{frac:0{scale}d}"
