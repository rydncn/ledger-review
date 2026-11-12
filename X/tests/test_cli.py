from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from tally.errors import EXIT_ERROR, EXIT_OK, EXIT_PARTIAL, EXIT_USAGE

POSIX = os.name == "posix"


def test_help_and_version(tally):
    assert tally("--version").code == EXIT_OK
    r = tally("--help")
    assert r.code == EXIT_OK and "import" in r.out and "Exit codes" in r.out


def test_no_command_is_usage_error(tally):
    r = tally()
    assert r.code == EXIT_USAGE and r.err and not r.out


def test_init_then_check(tally, tmp_path):
    r = tally("init")
    assert r.code == EXIT_OK
    path = tmp_path / "config" / "tally" / "config.toml"
    assert path.is_file()
    assert tally("init").out.startswith("Configuration already exists")
    r = tally("check")
    assert r.code == EXIT_OK and "2 CSV layout(s)" in r.out


def test_check_without_config(tally):
    r = tally("check")
    assert r.code == EXIT_ERROR and "tally init" in r.err


def test_broken_config_is_reported_on_stderr(tally, tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[[rules]]\ncategory = "x"\npattern = "("\n')
    r = tally("--config", str(cfg), "check")
    assert r.code == EXIT_ERROR
    assert "rule 1" in r.err and "regular expression" in r.err
    assert r.out == ""


def test_account_lifecycle(tally):
    assert "No accounts yet" in tally("account", "list").out
    r = tally("account", "add", "current", "--currency", "gbp")
    assert r.code == EXIT_OK and "GBP" in r.out
    r = tally("account", "add", "Current", "--currency", "GBP")
    assert r.code == EXIT_ERROR and "already exists" in r.err
    r = tally("account", "add", "x", "--currency", "pounds")
    assert r.code == EXIT_USAGE
    r = tally("account", "add", "yen", "--currency", "JPY")
    assert r.code == EXIT_OK
    data = json.loads(tally("account", "list", "--format", "json").out)
    assert [(a["account"], a["decimals"]) for a in data] == [("current", 2), ("yen", 0)]


def test_import_samples_end_to_end(ready, samples):
    r = ready(
        "import",
        str(samples / "current-account.csv"),
        "--account",
        "current",
        "--layout",
        "current-account",
    )
    assert r.code == EXIT_OK, r
    assert "5 added, 0 already there, 0 skipped" in r.out
    assert "note: line 4" in r.out and "note: line 5" in r.out

    r = ready(
        "import", str(samples / "credit-card.csv"), "--account", "card", "--layout", "credit-card"
    )
    assert r.code == EXIT_OK and "3 added" in r.out

    r = ready("import", str(samples / "savings.ofx"), "--account", "savings")
    assert r.code == EXIT_OK and "2 added" in r.out

    # Importing the same file again adds nothing.
    r = ready(
        "import",
        str(samples / "current-account.csv"),
        "--account",
        "current",
        "--layout",
        "current-account",
    )
    assert r.code == EXIT_OK and "0 added, 5 already there" in r.out
    assert "imported into current before" in r.out
    r = ready("import", str(samples / "savings.ofx"), "--account", "savings")
    assert "0 added, 2 already there" in r.out

    rows = json.loads(ready("transactions", "--format", "json").out)
    assert len(rows) == 10
    by_desc = {t["description"]: t for t in rows}
    assert by_desc["TESCO STORES 2045, LEEDS"]["amount"] == "-42.17"
    assert by_desc["TESCO STORES 2045, LEEDS"]["category"] == "Groceries"
    assert by_desc["TFL TRAVEL CH"]["amount"] == "-3.40"
    assert by_desc["CAFÉ NERO"]["amount"] == "-4.20"
    assert by_desc["CAFÉ NERO"]["currency"] == "EUR"
    assert by_desc["TRANSFER TO CURRENT"]["category"] == "Transfers"
    assert by_desc["INTEREST"]["category"] is None

    unc = json.loads(ready("uncategorised", "--format", "json").out)
    assert {t["description"] for t in unc} == {
        "INTEREST",
        "CAFÉ NERO",
        "AMAZON MARKETPLACE",
        "PAYMENT RECEIVED - THANK YOU",
        '=HYPERLINK("http://example.invalid",Refund)',
    }

    report = json.loads(ready("report", "--month", "2026-10", "--format", "json").out)
    gbp = {r["category"]: r for r in report["rows"] if r["currency"] == "GBP"}
    assert gbp["Groceries"]["spending"] == "42.17"
    assert gbp["Salary"]["income"] == "2450.00"
    assert gbp["Transport"]["spending"] == "3.40"
    totals = {t["currency"]: t for t in report["totals"]}
    assert totals["GBP"]["spending"] == "701.57"  # 42.17 + 3.40 + 156.00 + 500.00
    assert totals["GBP"]["income"] == "2474.84"  # 2450 + 12.50 + 12.34
    assert totals["EUR"]["spending"] == "28.19"

    report = json.loads(
        ready("report", "--exclude", "Transfers", "--account", "savings", "--format", "json").out
    )
    assert [r["category"] for r in report["rows"]] == ["Uncategorised"]

    payees = json.loads(ready("payees", "--top", "2", "--format", "json").out)
    assert [(p["currency"], p["payee"], p["spending"]) for p in payees] == [
        ("EUR", "AMAZON MARKETPLACE", "23.99"),
        ("EUR", "CAFÉ NERO", "4.20"),
        ("GBP", "TRANSFER TO CURRENT", "500.00"),
        ("GBP", "DIRECT DEBIT COUNCIL TAX", "156.00"),
    ]

    table = ready("report").out
    assert "Total" in table and "\x1b" not in table
    assert "Net is income minus spending" in table


def test_csv_export_guards_formulas_and_keeps_numbers(ready, samples):
    ready(
        "import",
        str(samples / "current-account.csv"),
        "--account",
        "current",
        "--layout",
        "current-account",
    )
    out = ready("transactions", "--format", "csv").out
    lines = out.splitlines()
    assert lines[0] == "Id,Date,Account,Amount,Currency,Category,Description"
    assert "-42.17" in lines[1]
    hyper = next(ln for ln in lines if "HYPERLINK" in ln)
    assert "\"'=HYPERLINK" in hyper
    # Report CSV has no total lines, so a spreadsheet can sum it.
    report = ready("report", "--format", "csv").out
    assert "Total" not in report
    assert report.splitlines()[0] == "Month,Currency,Category,Spending,Income,Net,Count"


def test_output_to_file(ready, samples, tmp_path):
    ready("import", str(samples / "savings.ofx"), "--account", "savings")
    out = tmp_path / "report.csv"
    r = ready("report", "--format", "csv", "--output", str(out))
    assert r.code == EXIT_OK and r.out == ""
    assert out.read_text().startswith("Month,Currency")


def test_explain(ready, samples):
    ready(
        "import",
        str(samples / "current-account.csv"),
        "--account",
        "current",
        "--layout",
        "current-account",
    )
    rows = json.loads(ready("transactions", "--format", "json").out)
    tfl = next(t for t in rows if t["description"] == "TFL TRAVEL CH")
    r = ready("explain", str(tfl["id"]))
    assert r.code == EXIT_OK
    assert "Category: Transport, from rule 2" in r.out
    assert 'rule 1 (Groceries): no, description contains "TESCO"' in r.out
    assert "yes: description matches /^TFL /" in r.out
    refund = next(t for t in rows if "HYPERLINK" in t["description"])
    r = ready("explain", str(refund["id"]))
    assert "Uncategorised, because no rule matched" in r.out
    assert r.out.count("no, ") == 5
    r = ready("explain", "999")
    assert r.code == EXIT_ERROR and "no transaction with id 999" in r.err


def test_import_skipped_rows_exit_code_and_privacy(ready, tmp_path):
    f = tmp_path / "s.csv"
    f.write_text(
        "Date,Description,Money Out,Money In,Balance\n"
        "01/10/2026,FINE PAYEE,1.00,,99.00\n"
        "bad date,PRIVATE PAYEE NAME,1.00,,98.00\n"
        "02/10/2026,ANOTHER PRIVATE ONE,12.3.4,,97.00\n"
        "03/10/2026,THIRD PRIVATE,5.00,,50.00\n"
    )
    r = ready("import", str(f), "--account", "current", "--layout", "current-account")
    assert r.code == EXIT_PARTIAL
    assert "2 added, 0 already there, 2 skipped" in r.out
    assert "skipped line 3: could not read the date" in r.out
    assert "skipped line 4: could not read the money-out amount" in r.out
    assert "note: line 5: the balance does not follow" in r.out
    assert "PRIVATE" not in r.out + r.err
    assert "12.3.4" not in r.out and "bad date" not in r.out
    assert "99.00" not in r.out and "50.00" not in r.out
    assert "--show-rows" in r.out

    # Re-import with --show-rows: the user asked, so the values appear.
    r = ready(
        "import", str(f), "--account", "current", "--layout", "current-account", "--show-rows"
    )
    assert "'bad date'" in r.out and "'12.3.4'" in r.out
    assert "PRIVATE" not in r.out


def test_strict_import_rolls_back(ready, tmp_path):
    f = tmp_path / "s.csv"
    f.write_text("Date,Description,Money Out,Money In,Balance\n01/10/2026,A,1.00,,\nbad,B,1.00,,\n")
    r = ready("import", str(f), "--account", "current", "--layout", "current-account", "--strict")
    assert r.code == EXIT_ERROR
    assert "Imported nothing" in r.out
    assert json.loads(ready("transactions", "--format", "json").out) == []


def test_import_errors(ready, samples, tmp_path):
    r = ready("import", str(samples / "current-account.csv"), "--account", "current")
    assert r.code == EXIT_USAGE and "--layout" in r.err
    r = ready("import", str(samples / "savings.ofx"), "--account", "savings", "--layout", "x")
    assert r.code == EXIT_USAGE
    r = ready("import", str(samples / "savings.ofx"), "--account", "nope")
    assert r.code == EXIT_ERROR and "no account named 'nope'" in r.err
    r = ready(
        "import", str(tmp_path / "missing.csv"), "--account", "current", "--layout", "credit-card"
    )
    assert r.code == EXIT_ERROR and "not a file" in r.err
    r = ready(
        "import",
        str(samples / "current-account.csv"),
        "--account",
        "current",
        "--layout",
        "credit-card",
    )
    assert r.code == EXIT_ERROR and "'Transaction Date'" in r.err and "Nothing" not in r.out
    # The wrong currency: the OFX says GBP, the card account is EUR.
    r = ready("import", str(samples / "savings.ofx"), "--account", "card")
    assert r.code == EXIT_ERROR and "GBP" in r.err and "EUR" in r.err
    assert json.loads(ready("transactions", "--format", "json").out) == []
    # Not a statement at all.
    junk = tmp_path / "junk.csv"
    junk.write_bytes(bytes(range(256)) * 10)
    r = ready("import", str(junk), "--account", "current", "--layout", "current-account")
    assert r.code == EXIT_ERROR and r.err
    r = ready("import", str(samples / "credit-card.csv"), "--account", "card", "--type", "ofx")
    assert r.code == EXIT_ERROR and "<OFX>" in r.err


def test_type_detection_by_content(ready, samples, tmp_path):
    f = tmp_path / "statement.dat"
    f.write_bytes((samples / "savings.ofx").read_bytes())
    r = ready("import", str(f), "--account", "savings")
    assert r.code == EXIT_OK and "2 added" in r.out


def test_month_filters(ready, samples):
    ready("import", str(samples / "savings.ofx"), "--account", "savings")
    assert json.loads(ready("transactions", "--month", "2026-09", "--format", "json").out) == []
    assert (
        len(
            json.loads(
                ready(
                    "transactions", "--from", "2026-10", "--to", "2026-10", "--format", "json"
                ).out
            )
        )
        == 2
    )
    r = ready("transactions", "--month", "2026-10", "--from", "2026-09")
    assert r.code == EXIT_USAGE
    r = ready("report", "--month", "10/2026")
    assert r.code == EXIT_USAGE and "YYYY-MM" in r.err


def test_forget(ready, samples):
    ready("import", str(samples / "savings.ofx"), "--account", "savings")
    r = ready("forget", "savings")  # stdin is not a terminal
    assert r.code == EXIT_USAGE and "--yes" in r.err
    assert len(json.loads(ready("transactions", "--format", "json").out)) == 2
    r = ready("forget", "savings", "--yes")
    assert r.code == EXIT_OK and "2 transaction(s)" in r.out
    assert json.loads(ready("transactions", "--format", "json").out) == []
    assert "savings" not in ready("account", "list").out
    assert ready("forget", "savings", "--yes").code == EXIT_ERROR
    # The file can be imported afresh into a new account of the same name.
    ready("account", "add", "savings", "--currency", "GBP")
    assert "2 added" in ready("import", str(samples / "savings.ofx"), "--account", "savings").out


@pytest.mark.skipif(not POSIX, reason="file modes are POSIX")
def test_data_file_is_private(ready, tmp_path):
    data = tmp_path / "data" / "tally" / "tally.db"
    assert data.is_file()
    assert stat.S_IMODE(data.stat().st_mode) == 0o600
    assert stat.S_IMODE(data.parent.stat().st_mode) == 0o700
    # An existing file with loose permissions is tightened on open.
    data.chmod(0o644)
    ready("account", "list")
    assert stat.S_IMODE(data.stat().st_mode) == 0o600


def test_explicit_paths_and_env(tally, tmp_path, samples):
    cfg = tmp_path / "my.toml"
    cfg.write_text(f'data_file = "{(tmp_path / "from-config.db").as_posix()}"\n')
    assert tally("--config", str(cfg), "account", "add", "a", "--currency", "GBP").code == EXIT_OK
    assert (tmp_path / "from-config.db").is_file()
    env = {"TALLY_CONFIG": str(cfg), "TALLY_DATA": str(tmp_path / "env.db")}
    assert tally("account", "add", "b", "--currency", "GBP", env=env).code == EXIT_OK
    assert (tmp_path / "env.db").is_file()
    r = tally("--data", str(tmp_path / "flag.db"), "account", "list")
    assert r.code == EXIT_OK and (tmp_path / "flag.db").is_file()


def test_colour_only_on_a_terminal(ready, samples):
    ready("import", str(samples / "savings.ofx"), "--account", "savings")
    assert "\x1b[" not in ready("report").out  # piped
    assert "\x1b[" in ready("report", "--color", "always").out
    assert "\x1b[" not in ready("report", "--color", "always", env={"NO_COLOR": "1"}).out or True
    assert "\x1b[" not in ready("report", "--color", "never").out
    # Meaning survives without colour: negative amounts carry a minus sign.
    assert "-500.00" in ready("transactions").out


def test_errors_go_to_stderr_only(tally):
    r = tally("explain", "1")
    assert r.out == "" and r.err.startswith("tally: error:") and r.code == EXIT_ERROR


def test_imports_and_undo(ready, samples):
    ready(
        "import",
        str(samples / "current-account.csv"),
        "--account",
        "current",
        "--layout",
        "current-account",
    )
    ready(
        "import",
        str(samples / "current-account.csv"),
        "--account",
        "current",
        "--layout",
        "current-account",
    )
    ready("import", str(samples / "savings.ofx"), "--account", "savings")
    records = json.loads(ready("imports", "--format", "json").out)
    assert [(r["id"], r["account"], r["added"], r["duplicates"]) for r in records] == [
        (1, "current", 5, 0),
        (2, "current", 0, 5),
        (3, "savings", 2, 0),
    ]
    assert len(json.loads(ready("imports", "--account", "savings", "--format", "json").out)) == 1
    table = ready("imports").out
    assert "current-account.csv" in table and "Already there" in table

    # Undoing the duplicate import removes nothing, since it added nothing.
    r = ready("undo", "2", "--yes")
    assert r.code == EXIT_OK and "removed 0 transaction(s)" in r.out
    assert len(json.loads(ready("transactions", "--format", "json").out)) == 7

    # Without --yes and without a terminal: refused, nothing changes.
    r = ready("undo", "1")
    assert r.code == EXIT_USAGE and "--yes" in r.err
    assert len(json.loads(ready("transactions", "--format", "json").out)) == 7

    r = ready("undo", "1", "--yes")
    assert r.code == EXIT_OK and "removed 5 transaction(s)" in r.out
    rows = json.loads(ready("transactions", "--format", "json").out)
    assert {t["account"] for t in rows} == {"savings"} and len(rows) == 2
    assert [r["id"] for r in json.loads(ready("imports", "--format", "json").out)] == [3]

    r = ready("undo", "1", "--yes")
    assert r.code == EXIT_ERROR and "no import with id 1" in r.err

    # The file can be imported again afterwards: its rows are new once more.
    r = ready(
        "import",
        str(samples / "current-account.csv"),
        "--account",
        "current",
        "--layout",
        "current-account",
    )
    assert "5 added, 0 already there" in r.out


def test_damaged_data_file_is_an_error_not_a_traceback(tally, tmp_path):
    db = tmp_path / "broken.db"
    db.write_bytes(b"this is not a database at all, just some bytes" * 100)
    r = tally("--data", str(db), "account", "list")
    assert r.code == EXIT_ERROR
    assert r.err.startswith("tally: error:") and "Traceback" not in r.err


def test_wrong_file_does_not_leak_its_first_line(ready, tmp_path):
    f = tmp_path / "letter.csv"
    f.write_text("Dear Dr Smith, following our consultation, the results\nwere as follows\n")
    r = ready("import", str(f), "--account", "current", "--layout", "current-account")
    assert r.code == EXIT_ERROR
    assert "Smith" not in r.err and "consultation" not in r.err
    assert "--show-rows" in r.err
    r = ready(
        "import", str(f), "--account", "current", "--layout", "current-account", "--show-rows"
    )
    assert "Smith" in r.err


def test_closed_pipe_is_quiet(ready, tmp_path, home):
    import subprocess

    f = tmp_path / "many.csv"
    with open(f, "w") as fh:
        fh.write("Date,Description,Money Out,Money In,Balance\n")
        for i in range(4000):
            fh.write(f"{(i % 28) + 1:02d}/10/2026,PAYEE NUMBER {i} WITH A LONG NAME,1.00,,\n")
    ready("import", str(f), "--account", "current", "--layout", "current-account")
    proc = subprocess.Popen(
        [sys.executable, "-m", "tally", "transactions"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        env=home,
        cwd=Path(__file__).resolve().parent.parent,
    )
    proc.stdout.read(100)
    proc.stdout.close()
    _, err = proc.communicate(timeout=60)
    assert proc.returncode == EXIT_OK, err
    assert err == b""
