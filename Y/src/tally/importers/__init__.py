"""What every importer shares: the outcomes a row can have, the whole-file
refusal, and which importer reads a file, decided from its first bytes and
never from its name.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Kind = Literal["csv", "ofx-sgml", "ofx-xml"]
# The codecs a statement may be read in, by Python's canonical name. Many others write ASCII
# as ASCII and still decode to what SQLite cannot store (UTF-7 and the escape codecs give lone
# surrogates) or hold NUL bytes (UTF-16): allowed by name, never by a probe.
ENCODINGS = frozenset({"utf-8", "utf-8-sig", "cp1252", "iso8859-1", "iso8859-15", "ascii"})


class NotAStatementError(Exception):
    """The file cannot be imported at all; the message names the reason, never the content."""


@dataclass(frozen=True)
class Transaction:
    """A row that was read: what the data file stores, with its fingerprint (ADR 0008)."""

    line: int
    date: str
    description: str
    amount_minor: int
    fingerprint: str


@dataclass(frozen=True)
class Unreadable:
    """A row that was not read, by line number and reason."""

    line: int
    reason: str


def kind(path: Path) -> Kind:
    """Which importer reads the file, from its first bytes (design § Shape)."""
    try:
        with path.open("rb") as handle:
            head = handle.read(1024)
    except OSError as error:
        raise NotAStatementError(f"cannot read the statement file: {error.strerror}") from error
    if not head:
        raise NotAStatementError("empty file")
    if b"\0" in head:
        raise NotAStatementError("not a text file")
    if head.startswith(b"OFXHEADER:"):
        return "ofx-sgml"
    if head.removeprefix(codecs.BOM_UTF8).lstrip().startswith((b"<?xml", b"<?OFX", b"<OFX>")):
        return "ofx-xml"
    return "csv"
