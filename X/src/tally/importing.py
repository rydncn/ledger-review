"""Import a statement file into an account."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from tally.config import Config, Layout
from tally.encoding import scan_file
from tally.errors import ImportError_, UsageError
from tally.importers import Note, ParsedRow, RowProblem
from tally.importers.csvfile import read_csv
from tally.importers.dedup import with_fingerprints
from tally.importers.ofx import looks_like_ofx, read_ofx
from tally.store import Account, ImportRecord, Store, _StrictAbort


@dataclass
class ImportResult:
    account: str
    file_name: str
    added: int = 0
    duplicates: int = 0
    problems: list[RowProblem] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    previous: ImportRecord | None = None
    rolled_back: bool = False

    @property
    def skipped(self) -> int:
        return len(self.problems)


def detect_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".ofx", ".qfx"}:
        return "ofx"
    if suffix in {".csv", ".txt", ".tsv"}:
        return "csv"
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError as exc:
        raise ImportError_(f"cannot open {path}: {exc.strerror}") from None
    return "ofx" if looks_like_ofx(head) else "csv"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _split(
    items: Iterable[ParsedRow | RowProblem | Note], result: ImportResult, account: Account
) -> Iterator[ParsedRow]:
    for item in items:
        if isinstance(item, ParsedRow):
            if item.currency and item.currency != account.currency:
                raise ImportError_(
                    f"the file is in {item.currency} but account {account.name!r} is in "
                    f"{account.currency}",
                    hint="Tally never converts currencies. Import it into an account "
                    "in that currency. Nothing was imported.",
                )
            yield item
        elif isinstance(item, RowProblem):
            result.problems.append(item)
        else:
            result.notes.append(item)


def import_file(
    store: Store,
    config: Config,
    path: Path,
    account_name: str,
    *,
    layout_name: str | None = None,
    file_type: str | None = None,
    strict: bool = False,
) -> ImportResult:
    if not path.is_file():
        raise ImportError_(f"{path} is not a file")
    account = store.account(account_name)
    file_type = file_type or detect_type(path)
    layout: Layout | None = None
    if file_type == "csv":
        if layout_name is None:
            raise UsageError(
                "a CSV file needs --layout NAME to say which columns are which",
                hint="Layouts are defined in the configuration file; see `tally init`.",
            )
        layout = config.layout(layout_name)
    elif layout_name is not None:
        raise UsageError("--layout applies to CSV files only; OFX files describe themselves")

    result = ImportResult(account=account.name, file_name=path.name)
    if file_type == "ofx":
        sha = _sha256(path)
        items = read_ofx(path, account.scale)
    else:
        assert layout is not None
        # One pass gives both the encoding and the digest.
        try:
            encoding, sha = scan_file(path, layout.encoding)
        except ValueError as exc:
            raise ImportError_(str(exc)) from None
        items = read_csv(path, layout, account.scale, encoding=encoding)
    result.previous = store.previous_import_of(account.id, sha)

    rows = with_fingerprints(_split(items, result, account))
    try:
        _, result.added, result.duplicates = store.record_import(
            account, path.name, sha, rows, lambda: result.skipped, strict=strict
        )
    except _StrictAbort:
        result.rolled_back = True
        result.added = result.duplicates = 0
    return result
