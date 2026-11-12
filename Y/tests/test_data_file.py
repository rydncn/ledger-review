"""AC6 to AC8: the data file is the user's alone, and two copies never collide."""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tally.cli import main
from tally.store import RefusedError, Store


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_ac06_a_fresh_data_directory_and_file_are_the_users_alone(tmp_path: Path) -> None:
    data_dir = tmp_path / "deep" / "data"
    with Store.open(data_dir) as store:
        assert store.tightened is False
        assert store._path == data_dir / "tally.sqlite"  # what a read-only refusal reports
    assert mode(data_dir) == 0o700
    assert mode(data_dir / "tally.sqlite") == 0o600
    connection = sqlite3.connect(data_dir / "tally.sqlite")
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    connection.close()


def test_ac06_a_wider_data_file_is_tightened_and_reported(tmp_path: Path) -> None:
    data_file = tmp_path / "tally.sqlite"
    with Store.open(tmp_path):
        pass
    data_file.chmod(0o644)
    with Store.open(tmp_path) as store:
        assert store.tightened is True
    assert mode(data_file) == 0o600
    with Store.open(tmp_path) as store:
        assert store.tightened is False


def test_ac06_a_data_directory_others_can_write_is_refused_and_left_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o775)  # as a folder a group could plant a -wal file in; may be the user's ~
    argv = ["account", "add", "current", "--currency", "GBP", "--data-dir", str(shared)]
    assert main(argv) == 1
    assert capsys.readouterr().err == f"other users can write the data directory {shared}\n"
    assert mode(shared) == 0o775  # not Tally's to change
    assert list(shared.iterdir()) == []
    shared.chmod(0o755)  # readable by others is no risk: the file is 0600
    assert main(argv) == 0


def test_ac07_two_first_opens_at_once_both_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Store.open(tmp_path):
        pass
    # As if another copy created the file between this one's check and its open.
    monkeypatch.setattr(Path, "exists", lambda self: False)
    with Store.open(tmp_path):
        pass


def test_ac06_a_data_directory_that_belongs_to_another_user_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    theirs = tmp_path / "theirs"
    theirs.mkdir(mode=0o700)
    monkeypatch.setattr("os.geteuid", lambda: theirs.stat().st_uid + 1)  # as if it were theirs
    assert main(["account", "list", "--data-dir", str(theirs)]) == 1
    captured = capsys.readouterr()
    assert captured.err == f"the data directory {theirs} belongs to another user\n"
    assert list(theirs.iterdir()) == []


def test_ac06_a_data_file_that_is_a_symbolic_link_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o700)
    elsewhere = tmp_path / "elsewhere"
    (data_dir / "tally.sqlite").symlink_to(elsewhere)  # dangling: the open would create it
    assert main(["account", "list", "--data-dir", str(data_dir)]) == 1
    assert capsys.readouterr().err == f"{data_dir / 'tally.sqlite'} is not a file of your own\n"
    assert not elsewhere.exists()


@pytest.mark.parametrize("side", ["tally.sqlite-wal", "tally.sqlite-shm", "tally.sqlite-journal"])
def test_ac06_a_side_file_planted_while_the_directory_was_open_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], side: str
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o700)  # private now, but planted in while it was open to others
    planted = shared / side
    planted.write_bytes(b"")
    planted.chmod(0o666)
    keep = tmp_path / "keep"
    keep.hardlink_to(planted)  # the planter keeps a link, so SQLite's unlink on close is no cure
    assert main(["account", "add", "current", "--currency", "GBP", "--data-dir", str(shared)]) == 1
    err = capsys.readouterr().err
    assert err.endswith(f"{planted} is not a file of your own\n")
    assert keep.read_bytes() == b""  # nothing was written into it


def test_ac06_a_side_file_of_another_user_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o700)
    (data_dir / "tally.sqlite-wal").write_bytes(b"")
    uid = data_dir.stat().st_uid
    monkeypatch.setattr("os.lstat", _owned_by(uid + 1, data_dir / "tally.sqlite-wal"))
    assert main(["account", "list", "--data-dir", str(data_dir)]) == 1
    assert capsys.readouterr().err == f"{data_dir / 'tally.sqlite-wal'} is not a file of your own\n"


def _owned_by(uid: int, target: Path):  # type: ignore[no-untyped-def]
    real = os.lstat

    def lstat(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        found = real(path, *args, **kwargs)
        if Path(path) != target:
            return found
        fields = list(found)
        fields[4] = uid  # st_uid
        return os.stat_result(fields)

    return lstat


def test_the_schema_holds_accounts_imports_and_transactions_once_per_account(
    tmp_path: Path,
) -> None:
    with Store.open(tmp_path), Store.open(tmp_path):
        pass  # opening twice applies the schema once
    connection = sqlite3.connect(tmp_path / "tally.sqlite")
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"accounts", "imports", "transactions"} <= tables
    connection.execute("INSERT INTO accounts VALUES (1, 'a', 'GBP', 2, '2026-11-10T00:00:00Z')")
    connection.execute("INSERT INTO accounts VALUES (2, 'b', 'GBP', 2, '2026-11-10T00:00:00Z')")
    connection.execute(
        "INSERT INTO imports VALUES (1, 1, 'f.csv', '2026-11-10T00:00:00Z', 1, 0, 0)"
    )
    row = (
        "INSERT INTO transactions"
        " (account_id, import_id, date, description, amount_minor, fingerprint)"
        " VALUES (?, 1, '2026-10-01', 'x', -4217, 'csv:1')"
    )
    connection.execute(row, (1,))
    connection.execute(row, (2,))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(row, (1,))
    connection.close()


def test_an_account_carries_its_currency_scale_and_when_it_was_added(tmp_path: Path) -> None:
    with Store.open(tmp_path) as store:
        yen = store.add_account("yen", "JPY")
        assert yen == store.account("yen")
        assert yen.scale == 0
        assert store.add_account("current", "GBP").scale == 2
        assert [a.name for a in store.accounts()] == ["current", "yen"]
    connection = sqlite3.connect(tmp_path / "tally.sqlite")
    rows = connection.execute(
        "SELECT name, scale, created_at FROM accounts ORDER BY name"
    ).fetchall()
    connection.close()
    assert [(name, scale) for name, scale, _ in rows] == [("current", 2), ("yen", 0)]
    assert all(created_at.endswith("+00:00") for _, _, created_at in rows)


def test_forgetting_an_account_another_copy_already_forgot_is_refused(tmp_path: Path) -> None:
    with Store.open(tmp_path) as store:
        account = store.add_account("current", "GBP")
        other = sqlite3.connect(tmp_path / "tally.sqlite")
        other.execute("DELETE FROM accounts WHERE name = 'current'")
        other.commit()
        other.close()
        with pytest.raises(RefusedError, match=r'^no account named "current"$'):
            store.forget_account(account)


def console_script() -> Path:
    tally = Path(sys.executable).with_name("tally")
    assert tally.exists(), "install the project first: uv sync"
    return tally


def test_ac06_the_variable_places_the_data_file_and_a_wider_one_is_tightened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "from-variable"
    monkeypatch.setenv("TALLY_DATA_DIR", str(data_dir))
    assert main(["account", "add", "current", "--currency", "GBP"]) == 0
    assert mode(data_dir) == 0o700
    assert mode(data_dir / "tally.sqlite") == 0o600
    (data_dir / "tally.sqlite").chmod(0o644)
    assert main(["account", "list"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "current GBP\n"
    assert captured.err == "tightened the data file's permissions to 0600\n"
    assert mode(data_dir / "tally.sqlite") == 0o600
    # The installed command, with the variable inherited from this process.
    result = subprocess.run(
        [str(console_script()), "account", "add", "savings", "--currency", "GBP"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert mode(data_dir / "tally.sqlite") == 0o600


def test_ac07_two_copies_at_once_each_write_once_and_the_file_stays_whole(tmp_path: Path) -> None:
    tally = str(console_script())
    data_dir = tmp_path / "data"
    both = [
        subprocess.Popen(
            [tally, "account", "add", name, "--currency", "GBP", "--data-dir", str(data_dir)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for name in ("current", "savings")
    ]
    outcomes = [(p.wait(), *p.communicate()) for p in both]
    assert outcomes == [(0, "", ""), (0, "", "")]
    listed = subprocess.run(
        [tally, "account", "list", "--data-dir", str(data_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert listed.stdout == "current GBP\nsavings GBP\n"
    connection = sqlite3.connect(data_dir / "tally.sqlite")
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()


def test_ac07_a_copy_waits_for_a_lock_that_is_released_in_time(tmp_path: Path) -> None:
    assert (
        main(["account", "add", "current", "--currency", "GBP", "--data-dir", str(tmp_path)]) == 0
    )
    holder = sqlite3.connect(tmp_path / "tally.sqlite", autocommit=True)
    holder.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    waiting = subprocess.Popen(
        [
            str(console_script()),
            "account",
            "add",
            "savings",
            "--currency",
            "GBP",
            "--data-dir",
            str(tmp_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(2)
    holder.execute("COMMIT")
    out, err = waiting.communicate()
    assert (waiting.returncode, out, err) == (0, "", "")
    assert time.monotonic() - started >= 2
    assert [row[0] for row in holder.execute("SELECT name FROM accounts ORDER BY name")] == [
        "current",
        "savings",
    ]
    holder.close()


def test_ac08_a_copy_that_cannot_get_the_file_in_time_says_so_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(["account", "add", "current", "--currency", "GBP", "--data-dir", str(tmp_path)]) == 0
    )
    capsys.readouterr()
    holder = sqlite3.connect(tmp_path / "tally.sqlite", autocommit=True)
    holder.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    # Waits the whole busy timeout (ADR 0005: 10 seconds), on purpose.
    code = main(["account", "add", "savings", "--currency", "GBP", "--data-dir", str(tmp_path)])
    waited = time.monotonic() - started
    captured = capsys.readouterr()
    assert (code, captured.out, captured.err) == (
        3,
        "",
        "another copy of tally is using the data file\n",
    )
    assert waited >= 9
    holder.execute("COMMIT")
    assert [row[0] for row in holder.execute("SELECT name FROM accounts")] == ["current"]
    holder.close()


def test_a_reader_never_waits_on_a_writer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(["account", "add", "current", "--currency", "GBP", "--data-dir", str(tmp_path)]) == 0
    )
    holder = sqlite3.connect(tmp_path / "tally.sqlite", autocommit=True)
    holder.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    code = main(["account", "list", "--data-dir", str(tmp_path)])
    assert time.monotonic() - started < 2
    holder.execute("COMMIT")
    holder.close()
    assert (code, capsys.readouterr().out) == (0, "current GBP\n")


def test_a_data_file_from_a_newer_tally_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(["account", "add", "current", "--currency", "GBP", "--data-dir", str(tmp_path)]) == 0
    )
    connection = sqlite3.connect(tmp_path / "tally.sqlite", autocommit=True)
    connection.execute("PRAGMA user_version = 99")
    connection.close()
    code = main(["account", "list", "--data-dir", str(tmp_path)])
    captured = capsys.readouterr()
    assert (code, captured.out) == (1, "")
    assert captured.err == "the data file was written by a newer version of tally\n"


def test_a_data_file_that_is_not_a_database_is_refused_in_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "tally.sqlite").write_bytes(b"not a database " * 100)
    code = main(["account", "list", "--data-dir", str(tmp_path)])
    captured = capsys.readouterr()
    assert (code, captured.out) == (1, "")
    assert captured.err.startswith("cannot open the data file: ")
    assert captured.err.count("\n") == 1


def test_ac34_a_data_file_damaged_after_it_opened_is_refused_in_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with Store.open(tmp_path) as store:
        for n in range(200):  # enough rows that the accounts table has pages of its own
            store.add_account(f"account {n:03d} " + "x" * 200, "GBP")
    data_file = tmp_path / "tally.sqlite"
    content = bytearray(data_file.read_bytes())
    content[8192:] = b"\xff" * (len(content) - 8192)  # the header and schema survive
    data_file.write_bytes(bytes(content))
    code = main(["account", "list", "--data-dir", str(tmp_path)])
    captured = capsys.readouterr()
    assert (code, captured.out) == (1, "")
    assert captured.err == "the data file failed: database disk image is malformed\n"


def test_ac07_a_copy_waits_for_a_fresh_file_another_copy_is_still_creating(tmp_path: Path) -> None:
    """Switching a brand-new file to WAL while another copy holds it is SQLITE_BUSY without
    the busy handler (SQLite's deadlock avoidance), so the wait has to be Tally's own."""
    data_file = tmp_path / "tally.sqlite"
    data_file.touch(mode=0o600)
    holder = sqlite3.connect(data_file, autocommit=True, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("CREATE TABLE placeholder (x)")
    holder.execute("DROP TABLE placeholder")

    def release() -> None:
        time.sleep(1)
        holder.execute("COMMIT")

    releasing = threading.Thread(target=release)
    started = time.monotonic()
    releasing.start()
    with Store.open(tmp_path) as store:
        assert store.accounts() == []
    releasing.join()
    holder.close()
    assert time.monotonic() - started >= 1
