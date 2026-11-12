"""AC1 to AC5, AC34 and AC35: ``tally account add|list|forget``."""

from __future__ import annotations

import json
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tally.cli import main
from tally.importers import Transaction
from tally.store import RefusedError, Store
from tests import SAMPLES


def run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def add(data_dir: Path, name: str, currency: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(
        ["account", "add", name, "--currency", currency, "--data-dir", str(data_dir)], capsys
    ) == (
        0,
        "",
        "",
    )


def insert_rows(data_dir: Path, account: str, count: int) -> None:
    """Rows in the real data file, standing in for an import until slice 2."""
    connection = sqlite3.connect(data_dir / "tally.sqlite")
    (account_id,) = connection.execute(
        "SELECT id FROM accounts WHERE name = ?", (account,)
    ).fetchone()
    import_id = connection.execute(
        "INSERT INTO imports (account_id, file, imported_at, added, duplicates, unreadable)"
        " VALUES (?, 'f.csv', '2026-11-10T00:00:00Z', ?, 0, 0)",
        (account_id, count),
    ).lastrowid
    for n in range(count):
        connection.execute(
            "INSERT INTO transactions"
            " (account_id, import_id, date, description, amount_minor, fingerprint)"
            " VALUES (?, ?, '2026-10-01', 'x', -100, ?)",
            (account_id, import_id, f"csv:{account}:{n}"),
        )
    connection.commit()
    connection.close()


def counts(data_dir: Path) -> dict[str, tuple[int, int]]:
    """Per account name: (transactions, import records)."""
    connection = sqlite3.connect(data_dir / "tally.sqlite")
    rows = connection.execute(
        "SELECT a.name,"
        " (SELECT count(*) FROM transactions t WHERE t.account_id = a.id),"
        " (SELECT count(*) FROM imports i WHERE i.account_id = a.id)"
        " FROM accounts a"
    ).fetchall()
    connection.close()
    return {name: (transactions, imports) for name, transactions, imports in rows}


class Terminal:
    """A standard input that says it is a terminal and answers one line."""

    def __init__(self, answer: str) -> None:
        self.answer = answer

    def isatty(self) -> bool:
        return True

    def readline(self) -> str:
        return self.answer


def test_ac03_forget_refuses_when_the_account_changed_after_the_person_confirmed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prompt names a count; if another copy imports before "yes", nothing is deleted."""
    add(tmp_path, "current", "GBP", capsys)

    class Busy(Terminal):
        def readline(self) -> str:
            with Store.open(tmp_path) as other:
                account = other.account("current")
                assert account is not None
                other.import_transactions(
                    account, "late.csv", [Transaction(2, "2026-10-01", "x", -1, "csv:late")]
                )
            return "yes\n"

    monkeypatch.setattr("sys.stdin", Busy("yes\n"))
    code, out, err = run(["account", "forget", "current", "--data-dir", str(tmp_path)], capsys)
    assert (code, out) == (1, "")
    assert err.endswith("nothing forgotten: the account changed after it was read\n")
    with Store.open(tmp_path) as store:
        account = store.account("current")
        assert account is not None
        assert store.transaction_count(account) == 1
        with pytest.raises(RefusedError, match=r"^nothing forgotten: the account changed"):
            store.forget_account(account, expected=0)
        assert store.forget_account(account, expected=1) == 1


def test_ac03_forget_with_yes_confirms_no_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With --yes nobody saw a count, so a count read before the lock decides nothing."""
    add(tmp_path, "current", "GBP", capsys)
    monkeypatch.setattr(Store, "transaction_count", lambda self, account: 999)
    assert run(["account", "forget", "current", "--yes", "--data-dir", str(tmp_path)], capsys) == (
        0,
        "",
        'forgot account "current" and its 0 transactions\n',
    )


def test_ac01_add_an_account(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data_dir = tmp_path / "data"
    add(data_dir, "current", "GBP", capsys)
    assert run(["account", "list", "--data-dir", str(data_dir)], capsys) == (0, "current GBP\n", "")
    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((data_dir / "tally.sqlite").stat().st_mode) == 0o600


def test_ac01_list_is_empty_before_any_account(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["account", "list", "--data-dir", str(tmp_path)], capsys) == (0, "", "")
    assert run(["account", "list", "--data-dir", str(tmp_path), "--format", "json"], capsys) == (
        0,
        "[]\n",
        "",
    )


def test_ac02_a_name_is_taken_once(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    add(tmp_path, "current", "GBP", capsys)
    code, out, err = run(
        ["account", "add", "current", "--currency", "EUR", "--data-dir", str(tmp_path)], capsys
    )
    assert (code, out, err) == (1, "", 'account "current" already exists\n')
    assert run(["account", "list", "--data-dir", str(tmp_path)], capsys)[1] == "current GBP\n"


def test_ac03_forget_deletes_the_account_and_everything_imported_into_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    add(tmp_path, "current", "GBP", capsys)
    add(tmp_path, "savings", "GBP", capsys)
    insert_rows(tmp_path, "current", 3)
    insert_rows(tmp_path, "savings", 2)
    code, out, err = run(
        ["account", "forget", "current", "--yes", "--data-dir", str(tmp_path)], capsys
    )
    assert (code, out, err) == (0, "", 'forgot account "current" and its 3 transactions\n')
    assert run(["account", "list", "--data-dir", str(tmp_path)], capsys) == (0, "savings GBP\n", "")
    assert counts(tmp_path) == {"savings": (2, 1)}


def test_ac03_without_yes_off_a_terminal_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    add(tmp_path, "current", "GBP", capsys)
    insert_rows(tmp_path, "current", 3)
    code, out, err = run(["account", "forget", "current", "--data-dir", str(tmp_path)], capsys)
    assert (code, out) == (2, "")
    assert err == 'forgetting "current" needs --yes when standard input is not a terminal\n'
    assert counts(tmp_path) == {"current": (3, 1)}


def test_ac03_on_a_terminal_the_prompt_names_the_account_and_the_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    add(tmp_path, "current", "GBP", capsys)
    insert_rows(tmp_path, "current", 3)
    prompt = 'Forget account "current" and its 3 transactions? Type yes to continue: '
    for answer in ("no\n", "y\n", "YES\n", "\n"):
        monkeypatch.setattr("sys.stdin", Terminal(answer))
        code, out, err = run(["account", "forget", "current", "--data-dir", str(tmp_path)], capsys)
        assert (code, out, err) == (1, "", prompt + "nothing forgotten\n")
        assert counts(tmp_path) == {"current": (3, 1)}
    monkeypatch.setattr("sys.stdin", Terminal("yes\n"))
    code, out, err = run(["account", "forget", "current", "--data-dir", str(tmp_path)], capsys)
    assert (code, out, err) == (0, "", prompt + 'forgot account "current" and its 3 transactions\n')
    assert counts(tmp_path) == {}


def test_ac04_forgetting_an_unknown_account_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    add(tmp_path, "current", "GBP", capsys)
    code, out, err = run(["account", "forget", "old", "--yes", "--data-dir", str(tmp_path)], capsys)
    assert (code, out, err) == (1, "", 'no account named "old"\n')
    assert counts(tmp_path) == {"current": (0, 0)}


@pytest.mark.parametrize(
    ("argv", "named"),
    [
        (
            ["account", "add", "current", "--currency", "pounds"],
            "'pounds' is not a three-letter currency code",
        ),
        (
            ["account", "add", "current", "--currency", "gb1"],
            "'gb1' is not a three-letter currency code",
        ),
        (["account", "add", "current"], "--currency"),
        (["account", "add", " ", "--currency", "GBP"], "name: must not be empty"),
        (["account", "add", "two\nlines", "--currency", "GBP"], "name: must not contain"),
        (["account", "add", "a\tb", "--currency", "GBP"], "name: must not contain"),
        (["account", "add", "bell\x07", "--currency", "GBP"], "name: must not contain"),
        (["account", "list", "--format", "xml"], "--format"),
        (["account"], "usage"),
        (["account", "forget"], "name"),
    ],
)
def test_ac34_usage_errors_go_to_stderr_with_exit_2_and_touch_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str], named: str
) -> None:
    with pytest.raises(SystemExit) as raised:
        main([*argv, "--data-dir", str(tmp_path)])
    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert captured.out == ""
    assert named in captured.err
    assert not (tmp_path / "tally.sqlite").exists()


def test_ac34_account_alone_asks_for_an_action(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["account"])
    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "the following arguments are required: <action>" in captured.err


def test_ac34_a_configuration_error_is_exit_2_before_the_data_file_opens(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "tally.toml"
    config.write_text("data_dir = 1\n", encoding="utf-8")
    argv = [
        "account",
        "add",
        "current",
        "--currency",
        "GBP",
        "--config",
        str(config),
        "--data-dir",
        str(tmp_path),
    ]
    assert run(argv, capsys) == (2, "", f"{config}: data_dir must be a string\n")
    assert not (tmp_path / "tally.sqlite").exists()


def test_ac34_a_data_directory_that_cannot_be_created_is_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory", encoding="utf-8")
    code, out, err = run(["account", "list", "--data-dir", str(blocker / "data")], capsys)
    assert (code, out) == (1, "")
    assert err.startswith("cannot open the data file: ")
    assert err.endswith("\n")
    assert err.count("\n") == 1


def test_ac35_account_list_has_a_json_form(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    add(tmp_path, "savings", "GBP", capsys)
    add(tmp_path, "card", "EUR", capsys)
    add(tmp_path, "yen", "jpy", capsys)
    code, out, err = run(
        ["account", "list", "--data-dir", str(tmp_path), "--format", "json"], capsys
    )
    assert (code, err) == (0, "")
    assert json.loads(out) == [
        {"name": "card", "currency": "EUR"},
        {"name": "savings", "currency": "GBP"},
        {"name": "yen", "currency": "JPY"},
    ]
    assert out.endswith("\n")
    assert out.count("\n") == 1
    assert (
        run(["account", "list", "--data-dir", str(tmp_path)], capsys)[1]
        == "card EUR\nsavings GBP\nyen JPY\n"
    )


def test_the_installed_command_adds_lists_and_forgets(tmp_path: Path) -> None:
    tally = Path(sys.executable).with_name("tally")
    assert tally.exists(), "install the project first: uv sync"

    def tally_run(*args: str, stdin: int = subprocess.DEVNULL) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(tally), "account", *args, "--data-dir", str(tmp_path)],
            capture_output=True,
            text=True,
            check=False,
            stdin=stdin,
        )

    added = tally_run("add", "current", "--currency", "GBP")
    assert (added.returncode, added.stdout, added.stderr) == (0, "", "")
    taken = tally_run("add", "current", "--currency", "EUR")
    assert (taken.returncode, taken.stdout, taken.stderr) == (
        1,
        "",
        'account "current" already exists\n',
    )
    listed = tally_run("list", "--format", "json")
    assert (listed.returncode, listed.stderr) == (0, "")
    assert json.loads(listed.stdout) == [{"name": "current", "currency": "GBP"}]
    no_terminal = tally_run("forget", "current")
    assert (no_terminal.returncode, no_terminal.stdout) == (2, "")
    assert "needs --yes" in no_terminal.stderr
    forgotten = tally_run("forget", "current", "--yes")
    assert (forgotten.returncode, forgotten.stdout) == (0, "")
    assert tally_run("list").stdout == ""


def test_ac01_a_name_is_trimmed_and_may_hold_a_space(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    add(tmp_path, " my account ", "GBP", capsys)
    assert run(["account", "list", "--data-dir", str(tmp_path)], capsys) == (
        0,
        "my account GBP\n",
        "",
    )
    code, out, err = run(
        ["account", "forget", " my account ", "--yes", "--data-dir", str(tmp_path)], capsys
    )
    assert (code, out, err) == (0, "", 'forgot account "my account" and its 0 transactions\n')


def test_ac03_without_yes_and_no_standard_input_at_all_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    add(tmp_path, "current", "GBP", capsys)
    monkeypatch.setattr("sys.stdin", None)
    code, out, err = run(["account", "forget", "current", "--data-dir", str(tmp_path)], capsys)
    assert (code, out) == (2, "")
    assert err == 'forgetting "current" needs --yes when standard input is not a terminal\n'
    assert counts(tmp_path) == {"current": (0, 0)}


def test_ac06_the_configuration_places_the_data_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "conf" / "tally.toml"
    config.parent.mkdir()
    config.write_text('data_dir = "ledger"\n', encoding="utf-8")
    code, out, err = run(
        ["account", "add", "current", "--currency", "GBP", "--config", str(config)], capsys
    )
    assert (code, out, err) == (0, "", "")
    assert stat.S_IMODE((tmp_path / "conf" / "ledger" / "tally.sqlite").stat().st_mode) == 0o600


@pytest.mark.parametrize("separator", [chr(0x2028), chr(0x2029)])
def test_ac34_an_account_name_holding_a_line_separator_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], separator: str
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["account", "add", f"a{separator}b", "--currency", "GBP", "--data-dir", str(tmp_path)])
    assert raised.value.code == 2
    assert "must not contain control characters or line breaks" in capsys.readouterr().err


def test_ac03_forget_leaves_no_forgotten_description_in_the_data_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = ["--data-dir", str(tmp_path)]
    assert main(["account", "add", "savings", "--currency", "GBP", *data]) == 0
    assert main(["import", str(SAMPLES / "savings.ofx"), "--account", "savings", *data]) == 0
    assert main(["account", "forget", "savings", "--yes", *data]) == 0
    capsys.readouterr()
    left = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.name.startswith("tally.sqlite"))
    assert b"TRANSFER TO CURRENT" not in left


def test_ac03_an_account_stored_with_a_line_separator_can_still_be_forgotten(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    name = "joint" + chr(0x2028) + "old"  # accepted by add before names refused it
    with Store.open(tmp_path) as store:
        store.add_account(name, "GBP")
    assert main(["account", "forget", name, "--yes", "--data-dir", str(tmp_path)]) == 0
    capsys.readouterr()
    with Store.open(tmp_path) as store:
        assert store.accounts() == []
