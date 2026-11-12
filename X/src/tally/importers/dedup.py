"""Fingerprints: how Tally recognises a transaction it has seen before.

Within an account, a fingerprint is unique. Two sources:

* The bank's own transaction id (OFX ``FITID``) when the file gives one.
* Otherwise a hash of the date, the amount, the normalised description and
  an occurrence number: the n-th identical row in one file gets n. So a
  statement with two identical coffees on the same day keeps both, while
  importing that statement again, or an overlapping one, adds neither.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterable, Iterator

from tally.importers import ParsedRow
from tally.store import NewTransaction

_WS = re.compile(r"\s+")


def normalise_description(text: str) -> str:
    return _WS.sub(" ", text).strip().casefold()


def fingerprint(row: ParsedRow, occurrence: int) -> str:
    if row.external_id:
        return "id:" + row.external_id.strip()
    key = f"{row.date}|{row.amount_minor}|{normalise_description(row.description)}|{occurrence}"
    return "row:" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def with_fingerprints(rows: Iterable[ParsedRow]) -> Iterator[NewTransaction]:
    seen: Counter[tuple[str, int, str]] = Counter()
    for row in rows:
        key = (row.date, row.amount_minor, normalise_description(row.description))
        seen[key] += 1
        yield NewTransaction(
            date=row.date,
            amount_minor=row.amount_minor,
            description=row.description,
            memo=row.memo,
            fingerprint=fingerprint(row, seen[key]),
            source_line=row.line,
        )
