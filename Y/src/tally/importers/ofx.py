"""OFX statements: 1.x SGML (unclosed tags) and 2.x XML, read by one scanner.

No XML parser runs. OFX 2.x is OFX 1.x with closed tags and an XML prolog,
so a scanner that knows SGML's one rule (a value ends at the next tag) reads
both, and there is no entity expansion, no DTD and no external fetch to get
wrong: a DOCTYPE is refused where the scanner meets it (AC39). The file is
decoded once, in the encoding its header names, and streamed in chunks; a
value or tag longer than 1 MB, or tags nested deeper than 64, refuse the
whole file (AC41). Each ``STMTTRN`` becomes a ``Transaction`` fingerprinted
``ofx:<FITID>`` (ADR 0008) or an ``Unreadable`` with its position and the
reason, never its content.
"""

from __future__ import annotations

import codecs
import logging
import re
from collections.abc import Iterable, Iterator
from datetime import date
from decimal import Decimal
from itertools import chain
from pathlib import Path
from typing import BinaryIO

from tally.importers import ENCODINGS, NotAStatementError, Transaction, Unreadable
from tally.money import NOT_A_NUMBER, AmountError, to_minor

CHUNK = 65_536
MAX_PIECE = 1_048_576
MAX_DEPTH = 64
DOCTYPE = "the file declares a DOCTYPE, which statements never do"
TOO_LONG = "not a statement: a value or tag longer than 1 MB"
log = logging.getLogger(__name__)

# A transaction keeps only the leaves it reads, so other leaves cost nothing.
_LEAVES = frozenset({"FITID", "DTPOSTED", "TRNAMT", "NAME", "MEMO"})
_STATEMENTS = frozenset({"STMTRS", "CCSTMTRS", "INVSTMTRS", "LOANSTMTRS"})
_AMOUNT = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")  # OFX's one number shape
_DATE = re.compile(r"([0-9]{4})([0-9]{2})([0-9]{2})")
_CURRENCY = re.compile(r"[A-Za-z]{3}")
_NAME = re.compile(r"[^\s/]+")  # a tag's name: its first word, without the slashes
_ENTITY = re.compile(r"&(?:#([0-9]{1,7})|#[xX]([0-9A-Fa-f]{1,6})|(lt|gt|amp|quot|apos|nbsp));")
_NAMED = {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'", "nbsp": "\xa0"}
_XML_ENCODING = re.compile(rb"""<\?xml[^>]*?\sencoding\s*=\s*["']([A-Za-z0-9._-]{1,40})["']""")
_CHARSETS = {b"1252": "cp1252", b"NONE": "cp1252", b"ISO-8859-1": "latin-1", b"8859-1": "latin-1"}


def _encoding(head: bytes, *, sgml: bool) -> str:
    """The codec the header names: SGML's ``ENCODING`` and ``CHARSET``, XML's prolog."""
    if sgml:
        fields: dict[bytes, bytes] = {}
        for line in head.partition(b"<")[0].splitlines():
            key, _, value = line.partition(b":")
            fields[key.strip().upper()] = value.strip().upper()
        if fields.get(b"ENCODING") in {b"UTF-8", b"UNICODE"}:  # OFX 1.0.2 wrote UNICODE
            return "utf-8"
        charset = fields.get(b"CHARSET", b"NONE")
        if charset not in _CHARSETS:
            raise NotAStatementError("not a text file: CHARSET is not 1252, ISO-8859-1 or NONE")
        return _CHARSETS[charset]
    if head.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"  # a byte order mark wins over the prolog, as in XML
    declared = _XML_ENCODING.match(head.lstrip())
    if declared is None:
        return "utf-8"
    try:
        # no mutate: codec names are case-insensitive, so "ASCII" is the same codec
        name = codecs.lookup(declared[1].decode("ascii")).name  # pragma: no mutate
    except LookupError:
        raise NotAStatementError("not a text file: the declared encoding is unknown") from None
    # The encodings OFX files use. Many others write "<OFX>" as ASCII and still decode to
    # what SQLite cannot store: UTF-7 and the escape codecs give lone surrogates.
    if name not in ENCODINGS:
        raise NotAStatementError(f'not a text file: encoding "{name}" is not supported')
    return name


def _decoded(handle: BinaryIO, head: bytes, encoding: str) -> Iterator[str]:
    """The file as text, chunk by chunk; a bad byte or a NUL refuses the whole file."""
    decoder = codecs.getincrementaldecoder(encoding)()
    raw = head
    while True:
        try:
            text = decoder.decode(raw, final=not raw)
        except UnicodeDecodeError as error:
            shown = encoding.removesuffix("-sig")
            raise NotAStatementError(f"not a text file: not valid {shown}") from error
        if "\0" in text:  # SQLite's text functions stop at it
            raise NotAStatementError("not a text file")
        if text:
            yield text
        if not raw:
            return
        raw = handle.read(CHUNK)


def _markup_end(buffer: str, lt: int, *, end_of_file: bool, sgml: bool) -> int:
    """Where the markup starting at ``lt`` ends, or -1 when the rest has not been read."""
    # no mutate: waiting for one more chunk when exactly nine characters are there is harmless
    short = len(buffer) - lt < len("<!DOCTYPE")  # pragma: no mutate
    if short and not end_of_file:
        return -1
    head = buffer[lt : lt + len("<!DOCTYPE")]
    if head.startswith("<!--"):
        close, width = buffer.find("-->", lt + 4), 3
    elif head.startswith("<?"):
        if sgml:  # SGML ends one at the first ">", XML at "?>": the two would read apart
            raise NotAStatementError(
                "not a statement: a processing instruction, which SGML never holds"
            )
        # no mutate: searching from lt + 3 differs only for "<??>", which has no target
        close, width = buffer.find("?>", lt + 2), 2  # pragma: no mutate
    elif head.startswith("<!"):
        if head.upper() == "<!DOCTYPE":
            raise NotAStatementError(DOCTYPE)
        raise NotAStatementError("not a statement: a <! declaration, which statements never hold")
    else:
        close, width = buffer.find(">", lt + 1), 1
    return -1 if close == -1 else close + width


def _pieces(chunks: Iterable[str], *, sgml: bool) -> Iterator[tuple[bool, str]]:
    """Each tag (``True``) and each run of text between tags (``False``), in order.

    Comments and XML processing instructions (the prolog, ``<?OFX ...?>``) are skipped,
    and the text either side of one is one run, as an XML parser reads it.
    """
    buffer = run = ""  # pragma: no mutate (text before the first tag is never read)
    for chunk in chain(chunks, [""]):
        end_of_file = not chunk
        buffer += chunk
        start = 0  # pragma: no mutate (find and slicing read None as 0)
        while (lt := buffer.find("<", start)) != -1:
            run += buffer[start:lt]
            start = lt
            if len(run) > MAX_PIECE:
                raise NotAStatementError(TOO_LONG)
            stop = _markup_end(buffer, lt, end_of_file=end_of_file, sgml=sgml)
            if stop == -1:
                break
            if stop - lt > MAX_PIECE:
                raise NotAStatementError(TOO_LONG)
            if not buffer.startswith(("<!--", "<?"), lt):
                if run:
                    yield False, run
                yield True, buffer[lt:stop]
                run = ""
            start = stop
        buffer = buffer[start:]
        if len(buffer) > MAX_PIECE:
            raise NotAStatementError(TOO_LONG)
    if buffer.startswith("<"):
        raise NotAStatementError("not a statement: the file ends inside a tag")
    if run + buffer:
        yield False, run + buffer


def _reference(match: re.Match[str]) -> str:
    if match[3]:
        return _NAMED[match[3]]
    code = int(match[1]) if match[1] else int(match[2], 16)
    if code == 0 or 0xD800 <= code <= 0xDFFF or code > 0x10FFFF:
        return match[0]  # NUL stops SQLite's text functions; a surrogate cannot be stored
    return chr(code)


def _unescape(text: str) -> str:
    """The five XML entities, ``&nbsp;`` and numeric references; any other ``&`` is text."""
    return _ENTITY.sub(_reference, text)


def _day(text: str) -> str | None:
    """``DTPOSTED``'s first eight characters as an ISO date, or ``None`` if they are not one."""
    digits = _DATE.match(text)
    if digits is None:
        return None
    try:
        return date(int(digits[1]), int(digits[2]), int(digits[3])).isoformat()
    except ValueError:
        return None


def _outcome(
    position: int, leaves: dict[str, str], currency: str, scale: int
) -> Transaction | Unreadable:
    """One ``STMTTRN`` as a ``Transaction`` with its ADR 0008 fingerprint, or why not."""
    for leaf in ("FITID", "DTPOSTED", "TRNAMT"):
        if leaf not in leaves:
            return Unreadable(position, f"no {leaf}")
    if leaves.get("CURSYM", currency).upper() != currency:  # Tally never converts (AC36)
        return Unreadable(position, "the transaction is in another currency")
    day = _day(leaves["DTPOSTED"])
    if day is None:
        return Unreadable(position, "DTPOSTED is not a date")
    amount = leaves["TRNAMT"]
    if not _AMOUNT.fullmatch(amount):
        return Unreadable(position, NOT_A_NUMBER)
    try:
        minor = to_minor(Decimal(amount), scale)  # from text: exact
    except AmountError as error:
        return Unreadable(position, str(error))
    description = leaves.get("NAME", leaves.get("MEMO", ""))
    return Transaction(position, day, description, minor, f"ofx:{leaves['FITID']}")


class _Reader:
    """The structure, in SGML's terms: aggregates on a stack, leaves into the open one."""

    def __init__(self, currency: str, scale: int) -> None:
        self.currency = currency
        self.scale = scale
        self.stack: list[str] = []
        self.pending: str | None = None  # an open tag not yet known to be a leaf or aggregate
        self.transaction: dict[str, str] | None = None  # the open STMTTRN's leaves
        # no mutate (both): None is as false as False
        self.started = False  # pragma: no mutate
        self.ended = False  # pragma: no mutate  (</OFX> has been met: nothing is read after it)
        self.statements = 0
        self.position = 0
        self.ready: list[Transaction | Unreadable] = []

    def markup(self, markup: str) -> None:
        inner = markup[1:-1].strip()
        closing = inner.startswith("/")
        empty = inner.endswith("/")
        found = _NAME.search(inner)
        if found is None:  # SGML's <> is "the last element again", XML's an error: neither
            raise NotAStatementError("not a statement: a tag with no name")
        name = found[0].upper()  # SGML's names are case-insensitive
        if not self.started:
            if closing or name != "OFX":
                raise NotAStatementError("not a statement: the first tag is not <OFX>")
            self.started = True
        if closing:
            self._close(name)
            return
        self._resolve()
        self.pending = name
        if empty:
            self._close(name)

    def text(self, text: str) -> None:
        value = text.strip()
        if self.pending is None or not value:
            return
        name, self.pending = self.pending, None
        value = _unescape(value)
        if name == "CURDEF":
            self._check_currency(value)
        elif self.transaction is None:
            return
        elif self.stack[-1] == "STMTTRN" and name in _LEAVES:
            self.transaction.setdefault(name, value)
        elif self.stack[-2:] == ["STMTTRN", "CURRENCY"] and name == "CURSYM":
            self.transaction.setdefault(name, value)  # the amounts are in this currency

    def finish(self) -> None:
        if not self.started:
            raise NotAStatementError("not a statement: the first tag is not <OFX>")
        if self.stack:
            self._close(self.stack[0])

    def _resolve(self) -> None:
        """A pending tag that met another tag before any value is an aggregate."""
        if self.pending is None:
            return
        name, self.pending = self.pending, None
        if name in self.stack:  # an aggregate never contains itself: the next STMTTRN
            self._close(name)
        if self.ended:
            raise NotAStatementError("not a statement: tags after </OFX>")
        self.stack.append(name)
        if len(self.stack) > MAX_DEPTH:
            raise NotAStatementError(f"not a statement: tags nested deeper than {MAX_DEPTH}")
        if name == "STMTTRN":
            self.transaction = {}
        if name in _STATEMENTS:
            self.statements += 1
            if self.statements > 1:  # FITIDs are unique per account, not across two
                raise NotAStatementError("the file holds more than one statement")

    def _close(self, name: str) -> None:
        if self.pending == name:  # <X></X>: an empty aggregate
            self._resolve()
        self.pending = None  # an SGML leaf that never got a value
        if name not in self.stack:
            return  # banks' SGML is loose: a close of what is not open is ignored
        while True:
            top = self.stack.pop()
            if top == "STMTTRN":
                self.position += 1
                leaves = self.transaction or {}
                self.ready.append(_outcome(self.position, leaves, self.currency, self.scale))
                self.transaction = None
            self.ended = top == "OFX"
            if top == name:
                return

    def _check_currency(self, value: str) -> None:
        if not _CURRENCY.fullmatch(value):
            raise NotAStatementError("not a statement: CURDEF is not a currency code")
        code = value.upper()
        if code != self.currency:
            raise NotAStatementError(f"the file is in {code} but the account is in {self.currency}")


def read(path: Path, currency: str, scale: int) -> Iterator[Transaction | Unreadable]:
    """Every ``STMTTRN`` of the file, streamed, as a ``Transaction`` or an ``Unreadable``.

    A file that cannot be read at all, or whose ``CURDEF`` is not ``currency``, raises
    ``NotAStatementError`` (AC24), wherever in the file that is found.
    """
    try:
        handle = path.open("rb")
    except OSError as error:
        raise NotAStatementError(f"cannot read the statement file: {error.strerror}") from error
    with handle:
        head = handle.read(CHUNK)
        sgml = head.startswith(b"OFXHEADER:")
        encoding = _encoding(head, sgml=sgml)
        log.info("reading %s as OFX %s in %s", path, "SGML" if sgml else "XML", encoding)
        reader = _Reader(currency, scale)
        for is_markup, piece in _pieces(_decoded(handle, head, encoding), sgml=sgml):
            if is_markup:
                reader.markup(piece)
            else:
                reader.text(piece)
            yield from reader.ready
            reader.ready.clear()
        reader.finish()
        yield from reader.ready
    log.info("read %d transactions", reader.position)
