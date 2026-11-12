"""The SQLite data file (ADR 0005).

One file, ``tally.sqlite``, in a directory created ``0700``, itself created
``0600``. Every write runs in one ``BEGIN IMMEDIATE`` transaction with a busy
timeout, so two copies of Tally never corrupt it or count a transaction
twice; a copy that cannot get the file in time raises ``LockedError``. Journal
mode is WAL, so a report can read while an import writes. The schema and its
migrations live here and nowhere else. This module never prints.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Self

from tally.importers import Transaction, Unreadable
from tally.money import from_minor, scale_of

DATA_FILE = "tally.sqlite"
BUSY_TIMEOUT_SECONDS = 10
LOCKED_MESSAGE = "another copy of tally is using the data file"
UNREADABLE_KEPT = 1000  # an import's unreadable rows kept for the summary; the rest are counted

# Each entry is one schema version, applied in order inside one transaction;
# PRAGMA user_version records how many have been applied. SQL text carries
# "pragma: no mutate": SQLite reads keywords and names in any case, so a
# case-swapped statement is the same statement, not a mutant.
MIGRATIONS: list[list[str]] = [
    [
        """CREATE TABLE accounts (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            currency TEXT NOT NULL,
            scale INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE imports (
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            file TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            added INTEGER NOT NULL,
            duplicates INTEGER NOT NULL,
            unreadable INTEGER NOT NULL
        )""",
        """CREATE TABLE transactions (
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            import_id INTEGER NOT NULL REFERENCES imports(id),
            date TEXT NOT NULL,
            description TEXT NOT NULL,
            amount_minor INTEGER NOT NULL,
            fingerprint TEXT NOT NULL,
            UNIQUE (account_id, fingerprint)
        )""",
    ],
]


class RefusedError(Exception):
    """The data said no (an unknown account, a name already taken): exit code 1."""


class LockedError(Exception):
    """Another copy holds the data file past the busy timeout: exit code 3."""


@dataclass(frozen=True)
class Account:
    id: int
    name: str
    currency: str
    scale: int


@dataclass(frozen=True)
class Entry:
    """A stored transaction with its account's name and currency: what rules and reports read."""

    id: int
    date: str
    account: str
    currency: str
    scale: int
    description: str
    amount_minor: int

    @property
    def amount(self) -> Decimal:
        return from_minor(self.amount_minor, self.scale)


_ENTRIES = (
    "SELECT t.id, t.date, a.name, a.currency, a.scale,"  # pragma: no mutate
    " t.description, t.amount_minor"  # pragma: no mutate
    " FROM transactions t JOIN accounts a ON a.id = t.account_id"  # pragma: no mutate
)
# Dates are ISO text, so a month is their first seven characters.
_IN_MONTHS = "WHERE substr(t.date, 1, 7) BETWEEN ? AND ?"  # pragma: no mutate


@dataclass(frozen=True)
class ImportSummary:
    """What one import run did: counted once each (design § Lifecycles)."""

    added: int
    duplicates: int
    unreadable: list[Unreadable]  # the first UNREADABLE_KEPT of them
    unreadable_count: int


@contextmanager
def _busy_is_locked() -> Iterator[None]:
    try:
        yield
    except sqlite3.OperationalError as error:
        if error.sqlite_errorcode == sqlite3.SQLITE_BUSY:
            raise LockedError(LOCKED_MESSAGE) from error
        raise


def _switch_to_wal(connection: sqlite3.Connection) -> None:
    """Set WAL, waiting if another copy is creating the same file right now.

    Converting a brand-new file while another connection holds it is SQLITE_BUSY
    without the busy handler (SQLite's deadlock avoidance), so the wait is ours.
    """
    deadline = time.monotonic() + BUSY_TIMEOUT_SECONDS
    while True:
        try:
            connection.execute("PRAGMA journal_mode=WAL")  # pragma: no mutate
        except sqlite3.OperationalError as error:
            out_of_time = time.monotonic() > deadline  # pragma: no mutate
            # no mutate: the deadline arm is reachable only by waiting the whole timeout
            if error.sqlite_errorcode != sqlite3.SQLITE_BUSY or out_of_time:  # pragma: no mutate
                raise
            time.sleep(0.01)  # pragma: no mutate  rules-check-ignore: seconds to wait, not money
        else:
            return


def _still_present(connection: sqlite3.Connection, account: Account) -> None:
    """Under the lock: the account read before it is still this one.

    SQLite gives a forgotten account's id to the next one added, so the id alone
    could name another account by now.
    """
    row = connection.execute(
        "SELECT 1 FROM accounts WHERE id = ? AND name = ? AND currency = ?",  # pragma: no mutate
        (account.id, account.name, account.currency),
    ).fetchone()
    if row is None:
        raise RefusedError(f'no account named "{account.name}"')


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


FINGERPRINT_TAKEN = "its FITID is already used by a different transaction"


def _taken(connection: sqlite3.Connection, account: Account, row: Transaction) -> bool:
    """Whether this fingerprint is already stored for a different date or amount.

    A fingerprint is the file's claim: only an OFX FITID can be reused for a different
    transaction, since a CSV fingerprint holds both (ADR 0012). The description may differ:
    a bank's re-export renames payees.
    """
    found = connection.execute(
        "SELECT date, amount_minor FROM transactions"  # pragma: no mutate
        " WHERE account_id = ? AND fingerprint = ?",  # pragma: no mutate
        (account.id, row.fingerprint),
    ).fetchone()
    return found is not None and tuple(found) != (row.date, row.amount_minor)


SIDE_FILES = ("-wal", "-shm", "-journal")  # what SQLite opens beside the data file


class Store:
    """An open data file. ``tightened`` says a wider permission was corrected on open."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        path: Path,
        tightened: bool,
    ) -> None:
        self._connection = connection
        self._path = path
        self.tightened = tightened

    @classmethod
    def open(cls, data_dir: Path) -> Self:
        """Open the data file under ``data_dir``, creating both privately, and migrate."""
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # The directory is checked before any file in it opens: whoever can write it can plant
        # a -wal file SQLite would write into, or a link the file would be created through.
        info = data_dir.stat()
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            raise RefusedError(f"the data directory {data_dir} belongs to another user")
        # Readable by others is no risk (the files are 0600); writable is. A directory that
        # was there already, perhaps the user's home, is not Tally's to change: refused.
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise RefusedError(f"other users can write the data directory {data_dir}")
        path = data_dir / DATA_FILE
        tightened = False
        # A file planted while the directory was writable outlives any check of it, so every
        # file SQLite will open here must be absent or the user's own: regular, one link.
        for side in (path, *(data_dir / f"{DATA_FILE}{end}" for end in SIDE_FILES)):
            try:
                found = os.lstat(side)
            except FileNotFoundError:
                continue
            ours = not hasattr(os, "geteuid") or found.st_uid == os.geteuid()
            if not (stat.S_ISREG(found.st_mode) and found.st_nlink == 1 and ours):
                raise RefusedError(f"{side} is not a file of your own")
            if stat.S_IMODE(found.st_mode) & 0o077:
                side.chmod(0o600)
                tightened = True
        if not path.exists():
            # no mutate: the lstat check above refuses a link first; O_NOFOLLOW guards a race
            nofollow = getattr(os, "O_NOFOLLOW", 0)  # pragma: no mutate
            # no mutate: opened only to be created and closed, so any access mode will do
            flags = os.O_CREAT | os.O_WRONLY | nofollow  # pragma: no mutate
            os.close(os.open(path, flags, 0o600))
        connection = sqlite3.connect(  # autocommit: Python opens no transaction of its own
            path,
            timeout=BUSY_TIMEOUT_SECONDS,
            autocommit=True,  # pragma: no mutate
        )
        store = cls(connection, path=path, tightened=tightened)
        try:
            with _busy_is_locked():
                _switch_to_wal(connection)
                connection.execute("PRAGMA foreign_keys=ON")  # pragma: no mutate
                # forget means gone: freed pages are zeroed, in the WAL and the file
                connection.execute("PRAGMA secure_delete=ON")  # pragma: no mutate (SQL case)
                store._migrate()
        except sqlite3.DatabaseError as error:
            connection.close()
            raise RefusedError(f"cannot open the data file: {error}") from error
        except BaseException:
            connection.close()
            raise
        return store

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """One ``BEGIN IMMEDIATE`` transaction: committed on success, rolled back on any error.

        A data file that cannot be written (``SQLITE_READONLY``, which the first write
        raises, not ``BEGIN``) is refused with its permissions, reported and not changed
        (AC42).
        """
        try:
            with _busy_is_locked():
                self._connection.execute("BEGIN IMMEDIATE")  # pragma: no mutate
            yield self._connection
        except BaseException as error:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")  # pragma: no mutate
            if (
                isinstance(error, sqlite3.OperationalError)
                and error.sqlite_errorcode == sqlite3.SQLITE_READONLY
            ):
                mode = stat.S_IMODE(self._path.stat().st_mode)
                raise RefusedError(
                    f"the data file cannot be written (its permissions are {mode:04o})"
                ) from error
            raise
        self._connection.execute("COMMIT")  # pragma: no mutate

    def _applied(self) -> int:
        version: int = self._connection.execute(
            "PRAGMA user_version"  # pragma: no mutate
        ).fetchone()[0]
        if version > len(MIGRATIONS):
            raise RefusedError("the data file was written by a newer version of tally")
        return version

    def _migrate(self) -> None:
        if self._applied() == len(MIGRATIONS):
            return  # a reader never takes the write lock (ADR 0005)
        with self.write() as connection:
            applied = self._applied()  # again, under the lock: another copy may have migrated
            for version, statements in enumerate(MIGRATIONS[applied:], start=applied + 1):
                for statement in statements:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {version}")  # pragma: no mutate

    def add_account(self, name: str, currency: str) -> Account:
        """Create an account; the UNIQUE name decides a race between two copies (AC2)."""
        scale = scale_of(currency)
        with self.write() as connection:
            try:
                (account_id,) = connection.execute(
                    "INSERT INTO accounts (name, currency, scale, created_at)"  # pragma: no mutate
                    " VALUES (?, ?, ?, ?) RETURNING id",  # pragma: no mutate
                    (name, currency, scale, _now()),
                ).fetchone()
            except sqlite3.IntegrityError as error:
                raise RefusedError(f'account "{name}" already exists') from error
        return Account(account_id, name, currency, scale)

    def accounts(self) -> list[Account]:
        rows = self._connection.execute(
            "SELECT id, name, currency, scale FROM accounts ORDER BY name"  # pragma: no mutate
        ).fetchall()
        return [Account(*row) for row in rows]

    def account(self, name: str) -> Account | None:
        """Whether the account is present: the one query that reads accounts by name."""
        row = self._connection.execute(
            "SELECT id, name, currency, scale FROM accounts WHERE name = ?",  # pragma: no mutate
            (name,),
        ).fetchone()
        return Account(*row) if row else None

    def transaction_count(self, account: Account) -> int:
        row = self._connection.execute(
            "SELECT count(*) FROM transactions WHERE account_id = ?",  # pragma: no mutate
            (account.id,),
        ).fetchone()
        return int(row[0])

    def entries(self, months: tuple[str, str] | None = None) -> Iterator[Entry]:
        """Every transaction, or those in months ``first`` to ``last`` inclusive, oldest first.

        Only reads, so it never waits on a writer (ADR 0005).
        """
        query = f"{_ENTRIES} {_IN_MONTHS if months else ''} ORDER BY t.date, t.id"
        for row in self._connection.execute(query, months or ()):
            yield Entry(*row)

    def payments(self, months: tuple[str, str]) -> Iterator[Entry]:
        """The money out in the months, grouped by currency, scale and description (payees)."""
        query = (
            f"{_ENTRIES} {_IN_MONTHS} AND t.amount_minor < 0"  # pragma: no mutate
            " ORDER BY a.currency, a.scale, t.description"  # pragma: no mutate
        )
        for row in self._connection.execute(query, months):
            yield Entry(*row)

    def entry(self, transaction_id: int) -> Entry | None:
        """One transaction by its id, or None when there is none."""
        row = self._connection.execute(
            f"{_ENTRIES} WHERE t.id = ?",  # pragma: no mutate
            (transaction_id,),
        ).fetchone()
        return Entry(*row) if row else None

    def import_transactions(
        self, account: Account, file: str, outcomes: Iterable[Transaction | Unreadable]
    ) -> ImportSummary:
        """One import run, all or nothing (ADR 0005): the record, each row, then its counts."""
        added = duplicates = unreadable_count = 0
        unreadable: list[Unreadable] = []
        with self.write() as connection:
            _still_present(connection, account)
            (import_id,) = connection.execute(
                "INSERT INTO imports (account_id, file, imported_at,"  # pragma: no mutate
                " added, duplicates, unreadable)"  # pragma: no mutate
                " VALUES (?, ?, ?, 0, 0, 0) RETURNING id",  # pragma: no mutate
                (account.id, file, _now()),
            ).fetchone()
            for read in outcomes:
                taken = not isinstance(read, Unreadable) and _taken(connection, account, read)
                outcome = Unreadable(read.line, FINGERPRINT_TAKEN) if taken else read
                if isinstance(outcome, Unreadable):
                    unreadable_count += 1
                    if unreadable_count <= UNREADABLE_KEPT:  # a flood of them costs a count
                        unreadable.append(outcome)
                    continue
                inserted = connection.execute(
                    "INSERT INTO transactions"  # pragma: no mutate
                    " (account_id, import_id, date, description,"  # pragma: no mutate
                    " amount_minor, fingerprint)"  # pragma: no mutate
                    " VALUES (?, ?, ?, ?, ?, ?)"  # pragma: no mutate
                    " ON CONFLICT (account_id, fingerprint) DO NOTHING",  # pragma: no mutate
                    (
                        account.id,
                        import_id,
                        outcome.date,
                        outcome.description,
                        outcome.amount_minor,
                        outcome.fingerprint,
                    ),
                ).rowcount
                if inserted:
                    added += 1
                else:
                    duplicates += 1
            connection.execute(
                "UPDATE imports SET added = ?, duplicates = ?, unreadable = ?"  # pragma: no mutate
                " WHERE id = ?",  # pragma: no mutate
                (added, duplicates, unreadable_count, import_id),
            )
        return ImportSummary(added, duplicates, unreadable, unreadable_count)

    def forget_account(self, account: Account, expected: int | None = None) -> int:
        """Delete the account, its transactions and its import records, all or nothing (AC3).

        ``expected`` is the count the person confirmed: if another copy changed the
        account since, nothing is deleted. Returns how many transactions went.
        """
        with self.write() as connection:
            _still_present(connection, account)
            count: int = connection.execute(
                "DELETE FROM transactions WHERE account_id = ?",  # pragma: no mutate
                (account.id,),
            ).rowcount
            if expected is not None and count != expected:  # the rollback undoes the delete
                raise RefusedError("nothing forgotten: the account changed after it was read")
            connection.execute(
                "DELETE FROM imports WHERE account_id = ?",  # pragma: no mutate
                (account.id,),
            )
            connection.execute(
                "DELETE FROM accounts WHERE id = ?",  # pragma: no mutate
                (account.id,),
            )
        return count
