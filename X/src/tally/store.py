"""SQLite storage. One file, readable only by its owner.

Amounts are stored as integers of minor units. Dates are ISO text. Every
transaction carries a fingerprint that is unique within its account, which
is how importing the same statement twice never counts anything twice;
see ``tally.dedup``.

Two copies of Tally may run at once. Writes happen inside ``BEGIN
IMMEDIATE`` transactions, so the second copy waits for the first (up to
``BUSY_TIMEOUT`` seconds) and then sees its rows as duplicates.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import sys
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tally.errors import DataLocked, TallyError

SCHEMA_VERSION = 1
BUSY_TIMEOUT = 30.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    currency    TEXT NOT NULL,
    scale       INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS imports (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    file_name   TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    added       INTEGER NOT NULL,
    duplicates  INTEGER NOT NULL,
    skipped     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    import_id    INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
    date         TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    description  TEXT NOT NULL,
    memo         TEXT,
    fingerprint  TEXT NOT NULL,
    source_line  INTEGER,
    UNIQUE (account_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS transactions_by_account_date ON transactions (account_id, date);
CREATE INDEX IF NOT EXISTS transactions_by_date ON transactions (date);
"""


@dataclass(frozen=True)
class Account:
    id: int
    name: str
    currency: str
    scale: int
    created_at: str


@dataclass(frozen=True)
class Transaction:
    id: int
    account_id: int
    account: str
    currency: str
    scale: int
    date: str
    amount_minor: int
    description: str
    memo: str | None
    source_line: int | None
    import_id: int

    @property
    def month(self) -> str:
        return self.date[:7]

    @property
    def searchable_text(self) -> str:
        """What rules match against: the description, plus the memo if any."""
        return f"{self.description} {self.memo}" if self.memo else self.description


@dataclass(frozen=True)
class NewTransaction:
    date: str
    amount_minor: int
    description: str
    memo: str | None
    fingerprint: str
    source_line: int | None


@dataclass(frozen=True)
class ImportRecord:
    id: int
    account: str
    file_name: str
    imported_at: str
    added: int
    duplicates: int
    skipped: int


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _secure_create(path: Path) -> None:
    """Create the data file, and its directory, readable only by the user."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    os.close(fd)


def _tighten_permissions(path: Path) -> None:
    if sys.platform == "win32":
        return
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        return
    if mode & 0o077:
        os.chmod(path, mode & 0o700)


class Store:
    def __init__(self, path: Path):
        self.path = path
        _secure_create(path)
        _tighten_permissions(path)
        try:
            self.conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT, isolation_level=None)
        except sqlite3.OperationalError as exc:
            raise TallyError(f"cannot open data file {path}: {exc}") from None
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _migrate(self) -> None:
        with self.write():
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise TallyError(
                    f"data file {self.path} was written by a newer version of Tally",
                    hint="Upgrade Tally to read it.",
                )
            if version < SCHEMA_VERSION:
                # Not executescript(): that commits any open transaction first.
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        self.conn.execute(statement)
                self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """A write transaction that other copies of Tally must wait for."""
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc) or "busy" in str(exc):
                raise DataLocked(
                    "another copy of Tally is using the data file",
                    hint=f"Waited {BUSY_TIMEOUT:.0f} seconds. Try again when it has finished.",
                ) from None
            raise
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # -- accounts -----------------------------------------------------------

    def add_account(self, name: str, currency: str, scale: int) -> Account:
        with self.write() as conn:
            try:
                conn.execute(
                    "INSERT INTO accounts (name, currency, scale, created_at) VALUES (?, ?, ?, ?)",
                    (name, currency, scale, _now()),
                )
            except sqlite3.IntegrityError:
                raise TallyError(f"an account named {name!r} already exists") from None
        return self.account(name)

    def account(self, name: str) -> Account:
        row = self.conn.execute("SELECT * FROM accounts WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise TallyError(
                f"no account named {name!r}",
                hint="Create it with `tally account add NAME --currency CODE`, "
                "or see `tally account list`.",
            )
        return Account(**row)

    def accounts(self) -> list[Account]:
        rows = self.conn.execute("SELECT * FROM accounts ORDER BY name").fetchall()
        return [Account(**row) for row in rows]

    def account_summaries(self) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT a.id, a.name, a.currency, a.scale,
                   COUNT(t.id) AS transactions, MIN(t.date) AS first, MAX(t.date) AS last
            FROM accounts a LEFT JOIN transactions t ON t.account_id = a.id
            GROUP BY a.id ORDER BY a.name
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def forget_account(self, name: str) -> int:
        """Delete the account and everything imported into it. Returns rows removed."""
        account = self.account(name)
        with self.write() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE account_id = ?", (account.id,)
            ).fetchone()[0]
            conn.execute("DELETE FROM transactions WHERE account_id = ?", (account.id,))
            conn.execute("DELETE FROM imports WHERE account_id = ?", (account.id,))
            conn.execute("DELETE FROM accounts WHERE id = ?", (account.id,))
        if count:
            self._vacuum()
        return count

    # -- imports ------------------------------------------------------------

    _IMPORT_SELECT = """
        SELECT i.id, a.name AS account, i.file_name, i.imported_at,
               i.added, i.duplicates, i.skipped
        FROM imports i JOIN accounts a ON a.id = i.account_id
    """

    def previous_import_of(self, account_id: int, sha256: str) -> ImportRecord | None:
        row = self.conn.execute(
            self._IMPORT_SELECT
            + " WHERE i.account_id = ? AND i.file_sha256 = ? ORDER BY i.id DESC LIMIT 1",
            (account_id, sha256),
        ).fetchone()
        return ImportRecord(**row) if row else None

    def imports(self, account: str | None = None) -> list[ImportRecord]:
        sql = self._IMPORT_SELECT
        params: list[object] = []
        if account is not None:
            sql += " WHERE a.name = ?"
            params.append(account)
        rows = self.conn.execute(sql + " ORDER BY i.id", params).fetchall()
        return [ImportRecord(**row) for row in rows]

    def import_record(self, import_id: int) -> ImportRecord:
        row = self.conn.execute(self._IMPORT_SELECT + " WHERE i.id = ?", (import_id,)).fetchone()
        if row is None:
            raise _no_import(import_id)
        return ImportRecord(**row)

    def undo_import(self, import_id: int) -> int:
        """Remove the transactions one import added, and its record. Returns rows removed.

        Only rows this import inserted go: rows it found already there belong
        to the earlier import that added them and are untouched. The deletes
        are explicit rather than left to ON DELETE CASCADE, and the existence
        check happens inside the write transaction, so a second copy of Tally
        undoing the same import is told it is gone rather than succeeding at
        nothing.
        """
        with self.write() as conn:
            count = conn.execute(
                "DELETE FROM transactions WHERE import_id = ?", (import_id,)
            ).rowcount
            if conn.execute("DELETE FROM imports WHERE id = ?", (import_id,)).rowcount == 0:
                raise _no_import(import_id)
        if count:
            self._vacuum()
        return count

    def _vacuum(self) -> None:
        """Reclaim the space deleted rows held, so they do not linger in the file.

        Best effort: another copy of Tally reading at this moment makes VACUUM
        fail with "database is locked", and the deletion has already committed.
        The space is reclaimed by the next VACUUM instead.
        """
        with suppress(sqlite3.OperationalError):
            self.conn.execute("VACUUM")

    def record_import(
        self,
        account: Account,
        file_name: str,
        sha256: str,
        rows: Iterable[NewTransaction],
        skipped: Callable[[], int],
        *,
        strict: bool = False,
    ) -> tuple[int, int, int]:
        """Insert transactions in one transaction. Returns (import id, added, duplicates).

        ``rows`` may be a generator; it is consumed inside the write
        transaction so that two imports of the same file serialise.
        ``skipped`` is asked, after the rows are consumed, how many rows
        could not be read; in strict mode any at all rolls everything back.
        """
        with self.write() as conn:
            cur = conn.execute(
                """
                INSERT INTO imports (account_id, file_name, file_sha256, imported_at,
                                     added, duplicates, skipped)
                VALUES (?, ?, ?, ?, 0, 0, 0)
                """,
                (account.id, file_name, sha256, _now()),
            )
            import_id = cur.lastrowid
            assert import_id is not None
            added = duplicates = 0
            for row in rows:
                cur = conn.execute(
                    """
                    INSERT INTO transactions (account_id, import_id, date, amount_minor,
                                              description, memo, fingerprint, source_line)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (account_id, fingerprint) DO NOTHING
                    """,
                    (
                        account.id,
                        import_id,
                        row.date,
                        row.amount_minor,
                        row.description,
                        row.memo,
                        row.fingerprint,
                        row.source_line,
                    ),
                )
                if cur.rowcount == 1:
                    added += 1
                else:
                    duplicates += 1
            skipped_count = skipped()
            if strict and skipped_count:
                raise _StrictAbort()
            conn.execute(
                "UPDATE imports SET added = ?, duplicates = ?, skipped = ? WHERE id = ?",
                (added, duplicates, skipped_count, import_id),
            )
        return import_id, added, duplicates

    # -- transactions -------------------------------------------------------

    _SELECT = """
        SELECT t.id, t.account_id, a.name AS account, a.currency, a.scale, t.date,
               t.amount_minor, t.description, t.memo, t.source_line, t.import_id
        FROM transactions t JOIN accounts a ON a.id = t.account_id
    """

    def transaction(self, transaction_id: int) -> Transaction:
        row = self.conn.execute(self._SELECT + " WHERE t.id = ?", (transaction_id,)).fetchone()
        if row is None:
            raise TallyError(f"no transaction with id {transaction_id}")
        return Transaction(**row)

    def transactions(
        self,
        *,
        account: str | None = None,
        start: str | None = None,  # inclusive ISO date
        end: str | None = None,  # inclusive ISO date
    ) -> Iterator[Transaction]:
        clauses, params = [], []
        if account is not None:
            clauses.append("a.name = ?")
            params.append(account)
        if start is not None:
            clauses.append("t.date >= ?")
            params.append(start)
        if end is not None:
            clauses.append("t.date <= ?")
            params.append(end)
        sql = self._SELECT
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY t.date, t.id"
        for row in self.conn.execute(sql, params):
            yield Transaction(**row)


def _no_import(import_id: int) -> TallyError:
    return TallyError(f"no import with id {import_id}", hint="See `tally imports`.")


class _StrictAbort(Exception):
    """Raised inside the write transaction to roll back a strict import."""
