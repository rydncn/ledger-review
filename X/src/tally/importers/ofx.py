"""Read OFX statements: the old SGML form with unclosed tags, and the XML form.

Both are handled by one tolerant tokeniser rather than an XML parser,
because SGML OFX is not XML and real exports break the XML form too. The
tokeniser walks ``<TAG>text`` leaves and ``<TAG>``/``</TAG>`` aggregates,
closing aggregates implicitly where a file omits the closing tag (as the
sample does for ``STMTTRN``).
"""

from __future__ import annotations

import html
import os
import re
from collections.abc import Iterator

from tally.encoding import detect_encoding
from tally.errors import ImportError_
from tally.importers import Note, ParsedRow, RowProblem
from tally.money import AmountError, parse_amount

_TOKEN = re.compile(r"<(/?)([A-Za-z0-9_.:-]+)>|<\?.*?\?>|<!--.*?-->", re.DOTALL)
_HEADER_LINE = re.compile(r"^([A-Z]+):(.*)$")
_LEAF_FIELDS = {
    "TRNTYPE",
    "DTPOSTED",
    "DTUSER",
    "TRNAMT",
    "FITID",
    "NAME",
    "MEMO",
    "CHECKNUM",
    "REFNUM",
}
_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})")
_TXN_AGGREGATE = "STMTTRN"
_STATEMENT_AGGREGATES = {"STMTRS", "CCSTMTRS", "INVSTMTRS"}
_CODEPAGES = {"1252": "cp1252", "1250": "cp1250", "1251": "cp1251", "8859-1": "latin-1"}


def looks_like_ofx(head: bytes) -> bool:
    text = head[:4096].decode("latin-1").upper()
    return "OFXHEADER" in text or "<OFX>" in text or "<?OFX" in text


def _declared_encoding(text: str) -> str | None:
    """From the SGML header lines or the XML declaration."""
    charset = encoding = None
    for line in text[:2048].splitlines():
        m = _HEADER_LINE.match(line.strip())
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if key == "ENCODING":
            encoding = value
        elif key == "CHARSET":
            charset = value
    if encoding and encoding.upper() in {"UTF-8", "UTF8"}:
        return "utf-8"
    if charset:
        return _CODEPAGES.get(charset.upper())
    m = re.search(r'<\?xml[^>]*encoding=["\']([^"\']+)["\']', text[:512])
    if m:
        return m.group(1)
    return None


def read_ofx(path: str | os.PathLike[str], scale: int) -> Iterator[ParsedRow | RowProblem | Note]:
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise ImportError_(f"cannot open {path}: {exc.strerror}") from None
    # Decode the header as Latin-1 to read what it says about the encoding.
    declared = _declared_encoding(raw[:4096].decode("latin-1"))
    encoding = detect_encoding(path, declared)
    try:
        text = raw.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        text = raw.decode("latin-1")
    yield from parse_ofx_text(text, scale)


def parse_ofx_text(text: str, scale: int) -> Iterator[ParsedRow | RowProblem | Note]:
    opening = re.search(r"<OFX>", text, re.IGNORECASE)
    if opening is None:
        raise ImportError_(
            "the file does not contain an <OFX> element",
            hint="It does not look like an OFX statement. Nothing was imported.",
        )

    start = opening.start()
    stack: list[str] = []
    current: dict[str, str] | None = None
    current_pos = 0
    currency: str | None = None
    transactions = 0
    # Line numbers are counted forward from the last position asked about,
    # so a file with many transactions is scanned once, not once per row.
    counted_to = 0
    line_at_counted = 1

    def line_of(pos: int) -> int:
        nonlocal counted_to, line_at_counted
        if pos < counted_to:  # not expected; positions only move forward
            return text.count("\n", 0, pos) + 1
        line_at_counted += text.count("\n", counted_to, pos)
        counted_to = pos
        return line_at_counted

    def finish():
        nonlocal current
        if current is None:
            return None
        record, current = current, None
        return _to_row(record, line_of(current_pos), currency, scale)

    pos = start
    while True:
        m = _TOKEN.search(text, pos)
        if m is None:
            break
        pos = m.end()
        if m.group(2) is None:  # processing instruction or comment
            continue
        closing, tag = m.group(1) == "/", m.group(2).upper()
        next_lt = text.find("<", pos)
        content = text[pos : next_lt if next_lt >= 0 else len(text)]
        value = html.unescape(content).strip()

        if closing:
            if tag == _TXN_AGGREGATE or (
                tag in stack and _TXN_AGGREGATE in stack[stack.index(tag) :]
            ):
                row = finish()
                if row is not None:
                    transactions += 1
                    yield row
            if tag in stack:
                del stack[stack.index(tag) :]
            if tag == "OFX":
                break
            continue

        if value and _is_leaf(tag, value):
            if tag == "CURDEF":
                currency = value.upper()
            elif current is not None and tag in _LEAF_FIELDS:
                current.setdefault(tag, value)
            continue

        # An aggregate (or a leaf with empty value, which we treat the same).
        if tag == _TXN_AGGREGATE:
            row = finish()
            if row is not None:
                transactions += 1
                yield row
            current = {}
            current_pos = m.start()
            if _TXN_AGGREGATE in stack:
                del stack[stack.index(_TXN_AGGREGATE) :]
        elif tag in _STATEMENT_AGGREGATES:
            currency = None
        stack.append(tag)

    row = finish()
    if row is not None:
        transactions += 1
        yield row
    if transactions == 0:
        yield Note(None, "the file contains no transactions")


def _is_leaf(tag: str, value: str) -> bool:
    # In SGML OFX a leaf is any tag followed by text before the next tag.
    return bool(value)


def _to_row(record: dict[str, str], line: int, currency: str | None, scale: int):
    date_text = record.get("DTPOSTED") or record.get("DTUSER") or ""
    m = _DATE.match(date_text)
    if not m:
        return RowProblem(line, "the transaction has no readable DTPOSTED date", (date_text,))
    year, month, day = (int(g) for g in m.groups())
    try:
        from datetime import date

        iso = date(year, month, day).isoformat()
    except ValueError:
        return RowProblem(line, "the transaction's DTPOSTED date is not a real date", (date_text,))
    amount_text = record.get("TRNAMT", "")
    try:
        # OFX says "." is the decimal separator, but some banks send ",".
        if "," in amount_text and "." not in amount_text:
            amount = parse_amount(
                amount_text, decimal_separator=",", thousands_separator="", scale=scale
            )
        else:
            amount = parse_amount(
                amount_text, decimal_separator=".", thousands_separator="", scale=scale
            )
    except AmountError as exc:
        return RowProblem(line, f"could not read the TRNAMT amount: {exc}", (amount_text,))
    if amount is None:
        return RowProblem(line, "the transaction has no TRNAMT amount")
    name = " ".join(record.get("NAME", "").split())
    memo = " ".join(record.get("MEMO", "").split()) or None
    if not name:
        if memo is None:
            return RowProblem(line, "the transaction has neither NAME nor MEMO")
        name, memo = memo, None
    external_id = record.get("FITID", "").strip() or None
    return ParsedRow(line, iso, amount, name, memo, external_id, currency)
