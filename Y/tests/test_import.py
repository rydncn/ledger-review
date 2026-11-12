"""AC5, AC7, AC8, AC9 to AC20, AC34, AC35, AC40, AC42: ``tally import`` on CSV files."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import stat
import subprocess
import sys
import time
import tracemalloc
from collections.abc import Iterator
from pathlib import Path

import pytest

from tally.cli import main
from tally.importers import Transaction, Unreadable
from tally.store import RefusedError, Store
from tests import SAMPLES

HEADER = "Date,Description,Money Out,Money In,Balance\r\n"
CONFIG = """\
[layouts.bank]
date = "Date"
description = "Description"
money_out = "Money Out"
money_in = "Money In"
date_format = "%d/%m/%Y"
decimal = "."
thousands = ","

[layouts.card]
delimiter = ";"
date = "Transaction Date"
description = "Payee"
amount = "Amount"
date_format = "%m/%d/%Y"
decimal = ","
thousands = "."
encoding = "latin-1"
"""


class Place:
    """A test's own configuration file and data directory, with the three sample accounts."""

    def __init__(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        self.capsys = capsys
        self.config = tmp_path / "tally.toml"
        self.config.write_text(CONFIG, encoding="utf-8")
        self.data_dir = tmp_path / "data"
        self.data_file = self.data_dir / "tally.sqlite"
        for name, currency in (("current", "GBP"), ("card", "EUR"), ("savings", "GBP")):
            assert self.run("account", "add", name, "--currency", currency) == (0, "", "")

    def run(self, *argv: str) -> tuple[int, str, str]:
        code = main([*argv, "--config", str(self.config), "--data-dir", str(self.data_dir)])
        captured = self.capsys.readouterr()
        return code, captured.out, captured.err

    def import_(self, file: Path, *argv: str, account: str = "current") -> tuple[int, str, str]:
        layout = "card" if account == "card" else "bank"
        return self.run("import", "--account", account, "--layout", layout, str(file), *argv)

    def query(self, sql: str) -> list[tuple[object, ...]]:
        connection = sqlite3.connect(self.data_file)
        rows = connection.execute(sql).fetchall()
        connection.close()
        return rows

    def transactions(self) -> list[tuple[object, ...]]:
        return self.query(
            "SELECT t.date, t.description, t.amount_minor FROM transactions t ORDER BY t.id"
        )

    def imports(self) -> list[tuple[object, ...]]:
        return self.query("SELECT file, added, duplicates, unreadable FROM imports ORDER BY id")


def bank_file(tmp_path: Path, *rows: str, name: str = "statement.csv") -> Path:
    path = tmp_path / name
    path.write_bytes((HEADER + "".join(row + "\r\n" for row in rows)).encode("utf-8"))
    return path


SAMPLE_ROWS = [
    ("2026-10-01", "TESCO STORES 2045, LEEDS", -4217),
    ("2026-10-02", "SALARY ACME LTD", 245000),
    ("2026-10-03", '=HYPERLINK("http://example.invalid","Refund")', 1250),
    ("2026-10-03", "TFL TRAVEL CH", -340),
    ("2026-10-05", "DIRECT DEBIT COUNCIL TAX", -15600),
]


def test_ac09_ac10_ac12_the_current_account_sample_imports_as_five_exact_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    assert place.import_(SAMPLES / "current-account.csv") == (
        0,
        "added 5, already there 0, unreadable 0\n",
        "",
    )
    assert place.transactions() == SAMPLE_ROWS
    assert place.imports() == [(str(SAMPLES / "current-account.csv"), 5, 0, 0)]
    columns = {row[1] for row in place.query("PRAGMA table_info(transactions)")}
    assert "balance" not in columns


def test_ac11_the_credit_card_sample_in_latin_1_with_semicolons_and_us_dates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    assert place.import_(SAMPLES / "credit-card.csv", account="card") == (
        0,
        "added 3, already there 0, unreadable 0\n",
        "",
    )
    assert place.transactions() == [
        ("2026-10-02", "CAFÉ NERO", -420),
        ("2026-10-03", "AMAZON MARKETPLACE", -2399),
        ("2026-10-03", "PAYMENT RECEIVED - THANK YOU", 15000),
    ]


def test_ac14_the_same_file_twice_adds_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    place.import_(SAMPLES / "current-account.csv")
    assert place.import_(SAMPLES / "current-account.csv") == (
        0,
        "added 0, already there 5, unreadable 0\n",
        "",
    )
    assert len(place.transactions()) == 5
    assert [row[1:] for row in place.imports()] == [(5, 0, 0), (0, 5, 0)]


def test_ac15_overlapping_statements_add_only_the_new_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    place.import_(SAMPLES / "current-account.csv")
    later = bank_file(
        tmp_path,
        '03/10/2026,"=HYPERLINK(""http://example.invalid"",""Refund"")",,12.50,"4,420.33"',
        "03/10/2026,TFL TRAVEL CH,(3.40),,4416.93",
        "05/10/2026,DIRECT DEBIT COUNCIL TAX,156.00,,4260.93",
        "06/10/2026,TESCO STORES 2045 LEEDS,8.10,,4252.83",
        "07/10/2026,TFL TRAVEL CH,3.40,,4249.43",
    )
    assert place.import_(later) == (0, "added 2, already there 3, unreadable 0\n", "")
    assert place.transactions()[5:] == [
        ("2026-10-06", "TESCO STORES 2045 LEEDS", -810),
        ("2026-10-07", "TFL TRAVEL CH", -340),
    ]


def test_ac16_identical_rows_in_one_file_are_separate_transactions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    twins = bank_file(tmp_path, "03/10/2026,COSTA,2.50,,", "03/10/2026,COSTA,2.50,,")
    assert place.import_(twins) == (0, "added 2, already there 0, unreadable 0\n", "")
    assert place.import_(twins) == (0, "added 0, already there 2, unreadable 0\n", "")
    assert len(place.transactions()) == 2


def test_ac05_importing_into_an_unknown_account_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    before = place.data_file.read_bytes()
    assert place.import_(SAMPLES / "current-account.csv", account="old") == (
        1,
        "",
        'no account named "old"\n',
    )
    assert place.data_file.read_bytes() == before
    assert place.imports() == []


def test_ac20_ac35_the_summary_has_a_json_form_and_nothing_else_on_standard_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    mixed = bank_file(tmp_path, "01/10/2026,A,1.00,,", "31/02/2026,B,1.00,,")
    code, out, err = place.import_(mixed, "--format", "json")
    assert (code, err) == (0, "line 3: date does not match %d/%m/%Y\n")
    assert json.loads(out) == {
        "account": "current",
        "file": str(mixed),
        "added": 1,
        "duplicates": 0,
        "unreadable_count": 1,
        "unreadable": [{"line": 3, "reason": "date does not match %d/%m/%Y"}],
    }
    assert list(json.loads(out)) == [
        "account",
        "file",
        "added",
        "duplicates",
        "unreadable_count",
        "unreadable",
    ]
    assert out.endswith("\n")
    assert out.count("\n") == 1
    assert place.import_(mixed) == (
        0,
        "added 0, already there 1, unreadable 1\n",
        "line 3: date does not match %d/%m/%Y\n",
    )


def test_ac13_rows_that_cannot_be_read_are_listed_by_line_and_reason_and_the_rest_imported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    broken = bank_file(
        tmp_path,
        "01/10/2026,GOOD ONE,1.00,,",
        "31/02/2026,BAD DATE,1.00,,",
        "01/10/2026,BAD AMOUNT,twelve,,",
        "01/10/2026,SHORT,1.00",
        "02/10/2026,GOOD TWO,,2.00,",
    )
    assert place.import_(broken) == (
        0,
        "added 2, already there 0, unreadable 3\n",
        "line 3: date does not match %d/%m/%Y\n"
        "line 4: amount is not a number\n"
        "line 5: expected 5 columns, found 3\n",
    )
    assert [row[1] for row in place.transactions()] == ["GOOD ONE", "GOOD TWO"]
    assert place.imports() == [(str(broken), 2, 0, 3)]


def test_ac13_a_flood_of_unreadable_rows_is_listed_to_fifty_and_counted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    flood = bank_file(tmp_path, *["31/02/2026,X,1.00,,"] * 500, "01/10/2026,Y,1.00,,")
    code, out, err = place.import_(flood)
    assert (code, out) == (0, "added 1, already there 0, unreadable 500\n")
    lines = err.splitlines()
    assert lines[:50] == [f"line {n}: date does not match %d/%m/%Y" for n in range(2, 52)]
    assert lines[50:] == ["and 450 more"]
    fifty = bank_file(tmp_path, *["31/02/2026,X,1.00,,"] * 50, name="fifty.csv")
    code, out, err = place.import_(fifty)
    assert (code, out) == (0, "added 0, already there 0, unreadable 50\n")
    assert err.count("\n") == 50
    assert "more" not in err
    code, out, err = place.import_(flood, "--format", "json")
    assert code == 0
    assert len(json.loads(out)["unreadable"]) == 500
    assert json.loads(out)["unreadable"][-1] == {
        "line": 501,
        "reason": "date does not match %d/%m/%Y",
    }
    assert err.splitlines()[50:] == ["and 450 more"]


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256)) * 4, "not a text file"),
        (b"", "empty file"),
        (
            b"\xef\xbb\xbfDay,Payee,Out,In\r\n01/10/2026,X,1.00,,\r\n",
            'header has no column named "Date"',
        ),
    ],
)
def test_ac17_a_file_that_is_not_a_statement_is_refused_whole(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: bytes, reason: str
) -> None:
    place = Place(tmp_path, capsys)
    path = tmp_path / "statement.csv"
    path.write_bytes(content)
    assert place.import_(path) == (1, "", reason + "\n")
    assert place.transactions() == []
    assert place.imports() == []


def test_ac19_nothing_personal_in_messages_with_or_without_verbose(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    markers = ["PAYEE-A7F3K9", "PAYEE-Q2M8ZT", "PAYEE-UNREAD-55RX", "4242.42", "1313.13", "9876.54"]
    personal = bank_file(
        tmp_path,
        f"01/10/2026,{markers[0]},{markers[3]},,",
        f"02/10/2026,{markers[1]},,{markers[4]},",
        f"31/02/2026,{markers[2]},{markers[5]},,",
    )
    for flags in ([], ["--verbose"], ["--verbose", "--format", "json"]):
        code, out, err = place.import_(personal, *flags)
        assert code == 0
        for marker in markers:
            assert marker not in err
            assert marker not in out
    assert "reading" in err  # --verbose said something, on standard error only


def test_ac19_verbose_is_off_by_default_and_leaves_nothing_behind(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    code, _, err = place.import_(SAMPLES / "current-account.csv", "--verbose")
    assert code == 0
    assert err.splitlines() == [
        f"reading {SAMPLES / 'current-account.csv'} as utf-8-sig, 5 columns",
        "read 5 rows",
    ]
    assert logging.getLogger("tally").handlers == []
    assert logging.getLogger("tally").level == logging.NOTSET
    assert place.import_(SAMPLES / "current-account.csv") == (
        0,
        "added 0, already there 5, unreadable 0\n",
        "",
    )


@pytest.mark.parametrize(
    ("argv", "named"),
    [
        (["import", str(SAMPLES / "current-account.csv")], "--account"),
        (
            [
                "import",
                "--account",
                "current",
                "--format",
                "xml",
                str(SAMPLES / "current-account.csv"),
            ],
            "--format",
        ),
        (["import", "--account", "current"], "file"),
        (["import", "--account", "current", "--layout", "bank"], "file"),
        (["import", "--account", "  ", "--layout", "bank", "x.csv"], "must not be empty"),
        (["import", "--account", " ", "--layout", "bank", "x.csv"], "must not be empty"),
    ],
)
def test_ac34_usage_errors_from_the_parser_go_to_stderr_with_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str], named: str
) -> None:
    place = Place(tmp_path, capsys)
    with pytest.raises(SystemExit) as raised:
        place.run(*argv)
    captured = capsys.readouterr()
    assert (raised.value.code, captured.out) == (2, "")
    assert named in captured.err
    assert place.imports() == []


@pytest.mark.parametrize(
    ("argv", "code", "message"),
    [
        (
            ["import", "--account", "current", str(SAMPLES / "current-account.csv")],
            2,
            "--layout is needed for a CSV file",
        ),
        (
            ["import", "--account", "current", "--layout", "bank", str(SAMPLES / "savings.ofx")],
            2,
            "--layout is for CSV files, and this is an OFX file",
        ),
        (
            ["import", "--account", "current", "--layout", "bank", str(SAMPLES / "nosuch.csv")],
            1,
            "cannot read the statement file: No such file or directory",
        ),
        (
            ["import", "--account", "current", "--layout", "bank", str(SAMPLES)],
            1,
            "cannot read the statement file: Is a directory",
        ),
    ],
)
def test_ac34_refusals_of_the_import_command_are_one_exact_line_and_write_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str], code: int, message: str
) -> None:
    place = Place(tmp_path, capsys)
    assert place.run(*argv) == (code, "", message + "\n")
    assert place.imports() == []


def test_the_account_name_is_trimmed_and_the_configuration_places_the_data_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "conf" / "tally.toml"
    config.parent.mkdir()
    config.write_text('data_dir = "ledger"\n' + CONFIG, encoding="utf-8")

    def run(*argv: str) -> tuple[int, str, str]:
        code = main([*argv, "--config", str(config)])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    assert run("account", "add", "current", "--currency", "GBP") == (0, "", "")
    sample = str(SAMPLES / "current-account.csv")
    assert run("import", "--account", " current ", "--layout", "bank", sample) == (
        0,
        "added 5, already there 0, unreadable 0\n",
        "",
    )
    assert run("account", "list") == (0, "current GBP\n", "")
    assert run("account", "forget", "current", "--yes") == (
        0,
        "",
        'forgot account "current" and its 5 transactions\n',
    )
    assert (tmp_path / "conf" / "ledger" / "tally.sqlite").exists()
    assert not (tmp_path / "data").exists()


def test_ac18_a_very_large_file_is_streamed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    path = tmp_path / "large.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(HEADER)
        for i in range(300_000):
            day = 1 + i % 28
            month = 1 + (i // 28) % 12
            handle.write(
                f"{day:02d}/{month:02d}/2026,PAYEE NUMBER {i:06d} HIGH STREET LEEDS BRANCH 0042,"
                f"{i % 1000}.{i % 100:02d},,\r\n"
            )
    assert path.stat().st_size > 20_000_000
    tracemalloc.start()
    try:
        result = place.import_(path)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result == (0, "added 300000, already there 0, unreadable 0\n", "")
    assert peak < 100 * 1024 * 1024
    assert place.query("SELECT count(*) FROM transactions") == [(300_000,)]


def test_ac40_a_monstrous_field_does_not_crash_the_import(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    monstrous = bank_file(
        tmp_path,
        "01/10/2026,FIRST,1.00,,",
        "02/10/2026,SECOND,2.00,,",
        '03/10/2026,"' + "x" * 50_000_000 + '",3.00,,',
        "04/10/2026,FOURTH,4.00,,",
    )
    assert place.import_(monstrous) == (
        0,
        "added 3, already there 0, unreadable 1\n",
        "line 4: field longer than 1 MB\n",
    )
    assert [row[1] for row in place.transactions()] == ["FIRST", "SECOND", "FOURTH"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes a 0400 file; CI's runner is not root")
def test_ac42_an_import_that_cannot_write_the_data_file_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    place.data_file.chmod(0o400)
    before = place.data_file.read_bytes()
    assert place.import_(SAMPLES / "current-account.csv") == (
        1,
        "",
        "the data file cannot be written (its permissions are 0400)\n",
    )
    assert stat.S_IMODE(place.data_file.stat().st_mode) == 0o400
    assert place.data_file.read_bytes() == before
    assert place.transactions() == []
    assert place.imports() == []


def test_ac42_a_read_only_data_file_is_refused_with_its_permissions_and_nothing_written(
    tmp_path: Path,
) -> None:
    """The permission itself cannot bite root, so the connection is opened read-only instead."""
    with Store.open(tmp_path) as store:
        account = store.add_account("current", "GBP")
    data_file = tmp_path / "tally.sqlite"
    data_file.chmod(0o400)
    connection = sqlite3.connect(f"file:{data_file}?mode=ro", uri=True, autocommit=True, timeout=1)
    with Store(connection, path=data_file, tightened=False) as store:
        with pytest.raises(
            RefusedError, match=r"^the data file cannot be written \(its permissions are 0400\)$"
        ):
            store.import_transactions(
                account, "a.csv", [Transaction(2, "2026-10-01", "x", -100, "csv:1")]
            )
        assert store.transaction_count(account) == 0
    assert stat.S_IMODE(data_file.stat().st_mode) == 0o400
    with Store.open(tmp_path) as store:
        assert store.transaction_count(account) == 0
    connection = sqlite3.connect(data_file)
    assert connection.execute("SELECT count(*) FROM imports").fetchone() == (0,)
    connection.close()


def test_the_store_writes_an_import_run_all_or_nothing(tmp_path: Path) -> None:
    def rows(*fingerprints: str) -> list[Transaction | Unreadable]:
        return [Transaction(n, "2026-10-01", "x", -100, f) for n, f in enumerate(fingerprints, 2)]

    with Store.open(tmp_path) as store:
        account = store.add_account("current", "GBP")
        first = store.import_transactions(account, "a.csv", rows("csv:1", "csv:2", "csv:3"))
        assert (first.added, first.duplicates, first.unreadable) == (3, 0, [])
        second = store.import_transactions(
            account, "b.csv", [*rows("csv:3", "csv:4"), Unreadable(9, "no amount")]
        )
        assert (second.added, second.duplicates, second.unreadable) == (
            1,
            1,
            [Unreadable(9, "no amount")],
        )
        assert store.transaction_count(account) == 4

        def failing() -> Iterator[Transaction | Unreadable]:
            yield from rows("csv:5")
            raise OSError("disk gone")

        with pytest.raises(OSError, match="disk gone"):
            store.import_transactions(account, "c.csv", failing())
        assert store.transaction_count(account) == 4
    connection = sqlite3.connect(tmp_path / "tally.sqlite")
    assert connection.execute(
        "SELECT file, added, duplicates, unreadable FROM imports ORDER BY id"
    ).fetchall() == [("a.csv", 3, 0, 0), ("b.csv", 1, 1, 1)]
    assert connection.execute(
        "SELECT count(*) FROM transactions WHERE import_id NOT IN (SELECT id FROM imports)"
    ).fetchone() == (0,)
    connection.close()


def console_script() -> Path:
    tally = Path(sys.executable).with_name("tally")
    assert tally.exists(), "install the project first: uv sync"
    return tally


def test_the_installed_command_imports_and_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    common = ["--config", str(place.config), "--data-dir", str(place.data_dir)]

    def tally_run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(console_script()), "import", *args, *common],
            capture_output=True,
            text=True,
            check=False,
        )

    sample = str(SAMPLES / "current-account.csv")
    imported = tally_run("--account", "current", "--layout", "bank", sample, "--format", "json")
    assert (imported.returncode, imported.stderr) == (0, "")
    assert json.loads(imported.stdout) == {
        "account": "current",
        "file": sample,
        "added": 5,
        "duplicates": 0,
        "unreadable_count": 0,
        "unreadable": [],
    }
    refused = tally_run("--account", "old", "--layout", "bank", sample)
    assert (refused.returncode, refused.stdout, refused.stderr) == (
        1,
        "",
        'no account named "old"\n',
    )
    usage = tally_run("--account", "current", sample)
    assert (usage.returncode, usage.stdout) == (2, "")
    assert usage.stderr == "--layout is needed for a CSV file\n"


def test_ac07_two_copies_at_once_count_each_transaction_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    statement = bank_file(
        tmp_path, *(f"{1 + i % 28:02d}/10/2026,PAYEE {i},{i}.00,," for i in range(2000))
    )
    common = ["--config", str(place.config), "--data-dir", str(place.data_dir), "--format", "json"]
    both = [
        subprocess.Popen(
            [
                str(console_script()),
                "import",
                "--account",
                "current",
                "--layout",
                "bank",
                str(statement),
                *common,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    outcomes = [(p.wait(), *p.communicate()) for p in both]
    assert {code for code, _, _ in outcomes} <= {0, 3}
    added = sum(json.loads(out)["added"] for code, out, _ in outcomes if code == 0)
    assert added == 2000
    assert place.query("SELECT count(*) FROM transactions") == [(2000,)]
    assert place.query("PRAGMA integrity_check") == [("ok",)]
    assert len(place.imports()) == sum(1 for code, _, _ in outcomes if code == 0)


def test_ac08_a_copy_that_cannot_get_the_file_in_time_says_so_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    holder = sqlite3.connect(place.data_file, autocommit=True)
    holder.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    # Waits the whole busy timeout (ADR 0005: 10 seconds), on purpose.
    result = place.import_(SAMPLES / "current-account.csv")
    waited = time.monotonic() - started
    assert result == (3, "", "another copy of tally is using the data file\n")
    assert waited >= 9
    holder.execute("COMMIT")
    assert holder.execute("SELECT count(*) FROM imports").fetchone() == (0,)
    assert holder.execute("SELECT count(*) FROM transactions").fetchone() == (0,)
    holder.close()


def test_an_account_read_before_the_lock_never_reaches_the_account_that_reused_its_id(
    tmp_path: Path,
) -> None:
    """SQLite gives a forgotten account's id to the next one added: the name is checked again."""
    with Store.open(tmp_path) as first, Store.open(tmp_path) as second:
        first.add_account("savings", "GBP")
        seen = first.add_account("current", "GBP")
        second.forget_account(second.account("current"))  # type: ignore[arg-type]
        joint = second.add_account("joint", "EUR")
        assert joint.id == seen.id
        second.import_transactions(joint, "a.csv", [Transaction(2, "2026-10-01", "x", -1, "csv:1")])
        with pytest.raises(RefusedError, match=r'^no account named "current"$'):
            first.forget_account(seen)
        with pytest.raises(RefusedError, match=r'^no account named "current"$'):
            first.import_transactions(
                seen, "b.csv", [Transaction(2, "2026-10-02", "y", -2, "csv:2")]
            )
        assert second.transaction_count(joint) == 1
        assert [a.name for a in second.accounts()] == ["joint", "savings"]
        assert second.forget_account(joint) == 1  # how many it deleted, counted under the lock


def test_unreadable_rows_are_counted_and_only_the_first_thousand_kept(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    path = tmp_path / "flood.csv"
    path.write_text(HEADER + "x\r\n" * 200_000 + "01/10/2026,Y,1.00,,\r\n", encoding="utf-8")
    tracemalloc.start()
    try:
        code, out, err = place.import_(path, "--format", "json")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    summary = json.loads(out)
    assert (code, summary["added"], summary["unreadable_count"]) == (0, 1, 200_000)
    assert len(summary["unreadable"]) == 1000
    assert summary["unreadable"][-1] == {"line": 1001, "reason": "expected 5 columns, found 1"}
    assert err.splitlines()[50:] == ["and 199950 more"]
    assert peak < 16 * 1024 * 1024
    assert place.imports()[-1] == (str(path), 1, 0, 200_000)
    assert place.import_(path)[1] == "added 0, already there 1, unreadable 200000\n"


def test_ac05_an_account_forgotten_by_another_copy_during_the_import_is_refused(
    tmp_path: Path,
) -> None:
    with Store.open(tmp_path) as store, Store.open(tmp_path) as other:
        account = store.add_account("current", "GBP")
        other.forget_account(account)
        with pytest.raises(RefusedError, match=r'^no account named "current"$'):
            store.import_transactions(
                account, "a.csv", [Transaction(2, "2026-10-01", "x", -100, "csv:1")]
            )
        assert store.accounts() == []
    connection = sqlite3.connect(tmp_path / "tally.sqlite")
    assert connection.execute("SELECT count(*) FROM imports").fetchone() == (0,)
    connection.close()


ESC = chr(0x1B)
BEL = chr(0x07)


def test_ac38_an_unreadable_rows_reason_shows_controls_from_the_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    hostile = f"%d/%m/%Y{ESC}]0;x{BEL}"
    place.config.write_text(
        CONFIG.replace('date_format = "%d/%m/%Y"', 'date_format = "%d/%m/%Y\\u001b]0;x\\u0007"'),
        encoding="utf-8",
    )
    code, _, err = place.import_(bank_file(tmp_path, "not a date,SHOP,1.00,,"))
    assert code == 0
    assert "line 2: date does not match %d/%m/%Y␛]0;x␇\n" in err
    assert ESC not in err
    assert BEL not in err
    assert hostile not in err


def test_ac38_verbose_logging_shows_controls_in_a_file_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    file = bank_file(tmp_path, "06/10/2026,SHOP,1.00,,", name=f"statement{ESC}[2J.csv")
    code, _, err = place.import_(file, "--verbose")
    assert code == 0
    assert "statement␛[2J.csv" in err
    assert ESC not in err


def test_ac40_a_record_of_many_quoted_lines_is_bounded_and_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    # One record of a million short quoted fields, each holding a line break: every physical
    # line is short, so only a bound on the record keeps it from being held whole.
    path = tmp_path / "many-lines.csv"
    path.write_bytes(
        HEADER.encode() + b'"x\n",' * 1_000_000 + b'"x"\r\n' + b"06/10/2026,AFTER,1.00,,\r\n"
    )
    tracemalloc.start()
    try:
        code, _, err = place.import_(path)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert code == 0
    assert peak < 30 * 1024 * 1024  # about what one 2 MiB line can take; unbounded, 55 MB
    assert err.startswith("line 2: field longer than 1 MB")
    assert "AFTER" in [r[1] for r in place.transactions()]
