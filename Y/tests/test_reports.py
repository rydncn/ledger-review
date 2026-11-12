"""AC3, AC26, AC29 to AC32, AC34 to AC38: ``tally report`` and ``tally payees``."""

from __future__ import annotations

import csv
import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tally.cli import main
from tally.importers import Transaction
from tally.store import Store
from tests.test_categorise import RULES, samples
from tests.test_import import CONFIG, Place, bank_file, console_script

OCTOBER = ("--month", "2026-10")
REPORT_TABLE = """\
2026-10 EUR
category       income  spending     net
Card payments  150.00      0.00  150.00
uncategorised    0.00    -28.19  -28.19

2026-10 GBP
category        income  spending      net
Big               0.00   -156.00  -156.00
Groceries         0.00    -42.17   -42.17
Travel            0.00     -3.40    -3.40
uncategorised  2474.84   -500.00  1974.84
"""
REPORT_ROWS = [
    ("2026-10", "EUR", "Card payments", "150.00", "0.00", "150.00"),
    ("2026-10", "EUR", None, "0.00", "-28.19", "-28.19"),
    ("2026-10", "GBP", "Big", "0.00", "-156.00", "-156.00"),
    ("2026-10", "GBP", "Groceries", "0.00", "-42.17", "-42.17"),
    ("2026-10", "GBP", "Travel", "0.00", "-3.40", "-3.40"),
    ("2026-10", "GBP", None, "2474.84", "-500.00", "1974.84"),
]
REPORT_FIELDS = ("month", "currency", "category", "income", "spending", "net")
PAYEES_TABLE = """\
EUR
description          total  count
AMAZON MARKETPLACE  -23.99      1
CAFÉ NERO            -4.20      1

GBP
description                 total  count
TRANSFER TO CURRENT       -500.00      1
DIRECT DEBIT COUNCIL TAX  -156.00      1
"""
PAYEES_FIELDS = ("currency", "description", "total", "count")
RLO = chr(0x202E)  # right-to-left override
FULLWIDTH = "".join(chr(0xFF10 + int(d)) for d in "2026") + "-10"  # fullwidth year digits
ARABIC = "-".join("".join(chr(0x660 + int(d)) for d in part) for part in ("2026", "10"))
AMOUNT = re.compile(r"^-?[0-9]+\.[0-9]{2}$")


def add(place: Place, account: str, *rows: tuple[str, str, int]) -> None:
    """Rows stored as they are, past the importers, which would read a tab or CR differently."""
    with Store.open(place.data_dir) as store:
        found = store.account(account)
        assert found is not None
        store.import_transactions(
            found,
            "added by the test",
            [
                Transaction(n, date, description, minor, f"test:{n}")
                for n, (date, description, minor) in enumerate(rows, start=1)
            ],
        )


def as_json(place: Place, *argv: str) -> list[dict[str, object]]:
    code, out, err = place.run(*argv, "--format", "json")
    assert (code, err) == (0, "")
    rows: list[dict[str, object]] = json.loads(out)
    return rows


def as_csv(place: Place, *argv: str) -> list[list[str]]:
    code, out, err = place.run(*argv, "--format", "csv")
    assert (code, err) == (0, "")
    assert out.endswith("\r\n")
    assert "\n" not in out.replace("\r\n", "")  # every line ends \r\n, and no bare \n or \r
    return list(csv.reader(io.StringIO(out, newline="")))


def test_ac30_ac36_the_report_by_category_with_one_section_per_currency(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = samples(tmp_path, capsys)
    assert place.run("report", *OCTOBER) == (0, REPORT_TABLE, "")


def test_ac26_ac35_the_report_as_json_has_the_rules_categories(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = samples(tmp_path, capsys)
    assert as_json(place, "report", *OCTOBER) == [
        dict(zip(REPORT_FIELDS, row, strict=True)) for row in REPORT_ROWS
    ]
    # TESCO at -150.00 is Groceries, not Big: first match wins.
    assert place.import_(bank_file(tmp_path, "20/10/2026,TESCO EXTRA,150.00,,"))[0] == 0
    rows = {(r["currency"], r["category"]): r for r in as_json(place, "report", *OCTOBER)}
    assert rows["GBP", "Groceries"]["spending"] == "-192.17"
    assert rows["GBP", "Big"]["spending"] == "-156.00"  # the savings transfer is not Big
    assert rows["GBP", None]["spending"] == "-500.00"


def test_ac30_a_categorys_total_is_the_exact_sum(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = RULES + '\n[[rules]]\nname = "Small"\ndescription_contains = "SMALL"\n'
    place = samples(tmp_path, capsys, rules=rules)
    small = bank_file(
        tmp_path,
        "06/10/2026,SMALL A,,0.10,",
        "06/10/2026,SMALL B,,0.20,",
        "06/10/2026,SMALL C,0.30,,",
    )
    assert place.import_(small)[0] == 0
    (row,) = [r for r in as_json(place, "report", *OCTOBER) if r["category"] == "Small"]
    assert (row["income"], row["spending"], row["net"]) == ("0.30", "-0.30", "0.00")
    _, out, _ = place.run("report", *OCTOBER)
    assert "Small             0.30     -0.30     0.00\n" in out


def test_ac30_a_total_past_sqlites_integer_is_exact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    big = 2**63 - 1
    add(
        place,
        "current",
        ("2026-10-01", "A", big),
        ("2026-10-02", "A", big),
        ("2026-10-03", "B", -big),
    )
    add(place, "savings", ("2026-10-01", "B", -big))
    assert as_json(place, "report", *OCTOBER) == [
        {
            "month": "2026-10",
            "currency": "GBP",
            "category": None,
            "income": "184467440737095516.14",
            "spending": "-184467440737095516.14",
            "net": "0.00",
        }
    ]
    assert as_json(place, "payees", *OCTOBER) == [
        {"currency": "GBP", "description": "B", "total": "-184467440737095516.14", "count": 2}
    ]


def test_ac31_a_range_of_months_and_the_payees_taking_the_most(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = samples(tmp_path, capsys)
    other_months = bank_file(
        tmp_path,
        "15/09/2026,DIRECT DEBIT COUNCIL TAX,156.00,,",
        "30/09/2026,BOOKS,10.00,,",
        "01/11/2026,BIG SHOP,900.00,,",
    )
    assert place.import_(other_months)[0] == 0
    period = ("--from", "2026-09", "--to", "2026-10")
    assert {(r["month"], r["currency"]) for r in as_json(place, "report", *period)} == {
        ("2026-09", "GBP"),
        ("2026-10", "EUR"),
        ("2026-10", "GBP"),
    }
    _, out, _ = place.run("report", *period)
    assert out.startswith("2026-09 GBP\ncategory ")
    assert "2026-11" not in out
    assert as_json(place, "payees", *period, "--top", "3") == [
        {"currency": "EUR", "description": "AMAZON MARKETPLACE", "total": "-23.99", "count": 1},
        {"currency": "EUR", "description": "CAFÉ NERO", "total": "-4.20", "count": 1},
        {"currency": "GBP", "description": "TRANSFER TO CURRENT", "total": "-500.00", "count": 1},
        {
            "currency": "GBP",
            "description": "DIRECT DEBIT COUNCIL TAX",
            "total": "-312.00",
            "count": 2,
        },
        {
            "currency": "GBP",
            "description": "TESCO STORES 2045, LEEDS",
            "total": "-42.17",
            "count": 1,
        },
    ]
    assert len(as_json(place, "payees", *period)) == 7  # every one, under the default of 10
    one_month = ("--from", "2026-10", "--to", "2026-10")  # a range of one month is allowed
    assert as_json(place, "report", *one_month) == as_json(place, "report", *OCTOBER)
    add(place, "savings", *[("2026-10-25", f"SHOP {n:02}", -1) for n in range(12)])
    assert sum(r["currency"] == "GBP" for r in as_json(place, "payees", *OCTOBER)) == 10


def test_ac31_the_payees_table(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    place = samples(tmp_path, capsys)
    assert place.run("payees", *OCTOBER, "--top", "2") == (0, PAYEES_TABLE, "")


def test_ac35_csv_and_json_have_the_same_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = samples(tmp_path, capsys)
    report = as_csv(place, "report", *OCTOBER)
    assert report[0] == list(REPORT_FIELDS)
    assert report[1:] == [[cell or "" for cell in row] for row in REPORT_ROWS]
    payees = as_csv(place, "payees", *OCTOBER)
    assert payees[0] == list(PAYEES_FIELDS)
    assert payees[1:] == [
        [str(r[k]) for k in PAYEES_FIELDS] for r in as_json(place, "payees", *OCTOBER)
    ]
    assert as_csv(place, "account", "list") == [
        ["name", "currency"],
        ["card", "EUR"],
        ["current", "GBP"],
        ["savings", "GBP"],
    ]


HOSTILE = [
    '=HYPERLINK("http://example.invalid","x")',
    "+1",
    "-1",
    "@SUM(A1)",
    "\tTAB",
    "\rCR",
]


def test_ac32_csv_export_never_runs_as_a_formula(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = "".join(
        f'[[rules]]\nname = "{name}"\ndescription_contains = "{name}"\n'
        for name in ("=SUM(1)", "+44", "-X", "@A")
    )
    place = samples(tmp_path, capsys, rules=rules)
    add(place, "current", *[("2026-10-20", text, -(n + 1)) for n, text in enumerate(HOSTILE)])
    add(
        place,
        "savings",
        *[("2026-10-21", f"{name} SHOP", -1) for name in ("=SUM(1)", "+44", "-X", "@A")],
    )
    shown = ["'" + text.replace("\t", "␉").replace("\r", "␍") for text in HOSTILE]

    report = as_csv(place, "report", *OCTOBER)
    assert [row[2] for row in report[1:] if row[1] == "GBP"] == [
        "'+44",
        "'-X",
        "'=SUM(1)",
        "'@A",
        "",
    ]
    payees = as_csv(place, "payees", *OCTOBER, "--top", "1000")
    assert sorted(row[1] for row in payees[1:] if row[1].startswith("'")) == sorted(
        [*shown, "'=SUM(1) SHOP", "'+44 SHOP", "'-X SHOP", "'@A SHOP"]
    )
    listed = as_csv(place, "uncategorised")
    refund = '\'=HYPERLINK("http://example.invalid","Refund")'  # the sample's own
    assert sorted(row[5] for row in listed[1:] if row[5].startswith("'")) == sorted(
        [*shown, refund]
    )
    amounts = (
        [c for row in report[1:] for c in row[3:6]]
        + [row[2] for row in payees[1:]]
        + [row[3] for row in listed[1:]]
    )
    assert amounts
    assert all(AMOUNT.match(a) for a in amounts)


@pytest.mark.parametrize(
    "argv",
    [
        ["report", *OCTOBER],
        ["payees", *OCTOBER],
        ["uncategorised"],
        ["account", "list"],
    ],
)
def test_ac32_csv_lines_end_crlf_on_a_stream_that_translates_as_windows_does(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    place = samples(tmp_path, capsys)
    raw = io.BytesIO()
    windows = io.TextIOWrapper(raw, encoding="utf-8", newline="\r\n", write_through=True)
    monkeypatch.setattr(sys, "stdout", windows)
    common = ["--config", str(place.config), "--data-dir", str(place.data_dir)]
    assert main([*argv, "--format", "csv", *common]) == 0
    written = raw.getvalue()
    assert written.count(b"\r\n") >= 4
    assert b"\r\r" not in written


def test_ac32_an_account_name_in_csv_never_runs_as_a_formula(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    for name in ("=HYPERLINK(1)", "+1", "-1", "@A"):
        assert place.run("account", "add", name, "--currency", "GBP")[0] == 0
    rows = as_csv(place, "account", "list")
    assert rows[1:5] == [["'+1", "GBP"], ["'-1", "GBP"], ["'=HYPERLINK(1)", "GBP"], ["'@A", "GBP"]]


def test_ac03_the_report_after_forget_has_none_of_the_accounts_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = samples(tmp_path, capsys)
    assert place.run("account", "forget", "current", "--yes")[0] == 0
    assert as_json(place, "report", *OCTOBER) == [
        dict(zip(REPORT_FIELDS, row, strict=True))
        for row in [
            ("2026-10", "EUR", "Card payments", "150.00", "0.00", "150.00"),
            ("2026-10", "EUR", None, "0.00", "-28.19", "-28.19"),
            ("2026-10", "GBP", None, "12.34", "-500.00", "-487.66"),
        ]
    ]
    assert [r["currency"] for r in as_json(place, "payees", *OCTOBER)] == ["EUR", "EUR", "GBP"]


@pytest.mark.parametrize("command", ["report", "payees"])
@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--from", "2026-10", "--to", "2026-09"], "--from 2026-10 is after --to 2026-09"),
        (["--month", "2026-10", "--from", "2026-09"], "--month is not given with --from or --to"),
        (["--month", "2026-10", "--to", "2026-11"], "--month is not given with --from or --to"),
        (["--from", "2026-09"], "give --month, or --from and --to"),
        (["--to", "2026-09"], "give --month, or --from and --to"),
        ([], "give --month, or --from and --to"),
    ],
)
def test_ac37_an_impossible_range_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str, argv: list[str], message: str
) -> None:
    config = tmp_path / "tally.toml"
    config.write_text(CONFIG + RULES, encoding="utf-8")
    data_dir = tmp_path / "data"
    code = main([command, *argv, "--config", str(config), "--data-dir", str(data_dir)])
    captured = capsys.readouterr()
    assert (code, captured.out, captured.err) == (2, "", message + "\n")
    assert not data_dir.exists()


@pytest.mark.parametrize("command", ["report", "payees"])
@pytest.mark.parametrize(
    ("argv", "option"),
    [
        (["--month", "2026-13"], "--month"),
        (["--month", "2026-00"], "--month"),
        (["--month", "October"], "--month"),
        (["--month", "2026-1"], "--month"),  # strptime would read January
        (["--month", "2026-10-01"], "--month"),
        (["--month", FULLWIDTH], "--month"),
        (["--month", ARABIC], "--month"),
        (["--month", " 2026-10"], "--month"),
        (["--month", "2026-10\n"], "--month"),
        (["--from", "2026/09", "--to", "2026-10"], "--from"),
        (["--from", "2026-09", "--to", "10"], "--to"),
    ],
)
def test_ac37_a_month_that_is_not_one_is_a_usage_error_naming_the_option(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str, argv: list[str], option: str
) -> None:
    data_dir = tmp_path / "data"
    with pytest.raises(SystemExit) as exited:
        main([command, *argv, "--data-dir", str(data_dir)])
    assert exited.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"argument {option}: not a month like 2026-10" in captured.err
    assert not data_dir.exists()


@pytest.mark.parametrize("top", ["0", "1001", "-1", "x", chr(0x661), " 3", "3.0", "9" * 30])
def test_ac37_top_is_one_to_a_thousand(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], top: str
) -> None:
    with pytest.raises(SystemExit) as exited:
        main(["payees", *OCTOBER, "--top", top, "--data-dir", str(tmp_path / "data")])
    assert exited.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "argument --top: not a number from 1 to 1000" in captured.err


def test_ac37_the_top_bounds_are_accepted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = samples(tmp_path, capsys)
    assert len(as_json(place, "payees", *OCTOBER, "--top", "1")) == 2  # one per currency
    assert len(as_json(place, "payees", *OCTOBER, "--top", "1000")) == 6


def test_ac29_a_broken_rule_stops_the_report_before_the_data_file_opens(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "tally.toml"
    config.write_text(CONFIG + '[[rules]]\nname = "A"\namount_below = "ten"\n', encoding="utf-8")
    data_dir = tmp_path / "data"
    code = main(["report", *OCTOBER, "--config", str(config), "--data-dir", str(data_dir)])
    captured = capsys.readouterr()
    assert (code, captured.out) == (2, "")
    assert captured.err == 'rules[1]: amount_below is not an amount like "-100.00"\n'
    assert not data_dir.exists()


def test_ac38_control_characters_never_reach_the_terminal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = samples(tmp_path, capsys)
    evil = f"EVIL\x1b]0;evil\x07 X\x08Y \x9b31m {RLO}RTL"
    add(place, "current", ("2026-10-09", evil, -100000))
    for argv in (
        ["payees", *OCTOBER],
        ["payees", *OCTOBER, "--format", "csv"],
        ["report", *OCTOBER],
    ):
        code, out, _ = place.run(*argv)
        assert code == 0
        assert not any(c in out for c in "\x1b\x07\x08\x9b" + RLO)
    _, out, _ = place.run("payees", *OCTOBER)
    assert "EVIL␛]0;evil␇ X␈Y ?31m ?RTL  -1000.00      1\n" in out
    assert as_json(place, "payees", *OCTOBER)[2]["description"] == evil


def test_ac34_a_period_with_nothing_in_it_prints_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = samples(tmp_path, capsys)
    for command in ("report", "payees"):
        assert place.run(command, "--month", "2026-09") == (0, "", "")
        assert place.run(command, "--month", "2026-09", "--format", "json") == (0, "[]\n", "")
        assert as_csv(place, command, "--month", "2026-09") == [
            list(REPORT_FIELDS if command == "report" else PAYEES_FIELDS)
        ]


def test_payees_reads_no_rules(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    place = samples(tmp_path, capsys)
    place.config.write_text(CONFIG + '[[rules]]\nname = ""\n', encoding="utf-8")
    assert place.run("payees", *OCTOBER, "--top", "2") == (0, PAYEES_TABLE, "")


def test_the_installed_command_reports_and_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = samples(tmp_path, capsys)
    common = ["--config", str(place.config), "--data-dir", str(place.data_dir)]

    def tally(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(console_script()), *argv, *common],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LC_ALL": "C.UTF-8"},
        )

    report = tally("report", *OCTOBER)
    assert (report.returncode, report.stdout, report.stderr) == (0, REPORT_TABLE, "")
    payees = tally("payees", *OCTOBER, "--top", "2")
    assert (payees.returncode, payees.stdout, payees.stderr) == (0, PAYEES_TABLE, "")
    exported = subprocess.run(
        [str(console_script()), "report", *OCTOBER, "--format", "csv", *common],
        capture_output=True,
        check=False,
        timeout=60,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LC_ALL": "C.UTF-8"},
    )  # bytes, so nothing translates the line endings on the way in
    assert exported.stdout.startswith(b"month,currency,category,income,spending,net\r\n")
    assert b"\r\r" not in exported.stdout
    backwards = tally("report", "--from", "2026-10", "--to", "2026-09")
    assert (backwards.returncode, backwards.stdout, backwards.stderr) == (
        2,
        "",
        "--from 2026-10 is after --to 2026-09\n",
    )
    month = tally("report", "--month", "2026-13")
    assert (month.returncode, month.stdout) == (2, "")
    assert "argument --month: not a month like 2026-10" in month.stderr


def test_ac31_a_huge_description_costs_its_own_line_not_every_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = samples(tmp_path, capsys)
    add(place, "current", ("2026-10-09", "X" * 1_000_000, -999900))
    code, out, _ = place.run("payees", *OCTOBER, "--top", "1000")
    assert code == 0
    assert len(out) < 2_000  # was 3,000,000: every line padded to the widest description
    assert f"{'X' * 59}…  -9999.00      1\n" in out
    assert as_json(place, "payees", *OCTOBER)[2]["description"] == "X" * 1_000_000
    add(place, "savings", ("2026-10-09", "Y" * 60, -1), ("2026-10-09", "Z" * 61, -1))
    _, out, _ = place.run("payees", *OCTOBER, "--top", "1000")
    assert f"\n{'Y' * 60}  " in out
    assert f"\n{'Z' * 59}…  " in out


# AC32 and AC35 as amended by ADR 0011: an amount has its currency's own decimal places.
PLACES = {"JPY": re.compile(r"^-?[0-9]+$"), "BHD": re.compile(r"^-?[0-9]+\.[0-9]{3}$")}


def test_ac32_ac35_amounts_have_their_currencys_own_places_in_csv_and_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = samples(tmp_path, capsys, rules="")
    for name, currency in (("yen", "JPY"), ("dinar", "BHD")):
        assert place.run("account", "add", name, "--currency", currency)[0] == 0
    add(place, "yen", ("2026-10-20", "RAMEN", -1200), ("2026-10-21", "REFUND", 300))
    add(place, "dinar", ("2026-10-20", "SOUQ", -1234), ("2026-10-21", "PAY", 5))

    report = [r for r in as_csv(place, "report", *OCTOBER)[1:] if r[1] in PLACES]
    payees = [r for r in as_csv(place, "payees", *OCTOBER)[1:] if r[0] in PLACES]
    listed = [r for r in as_csv(place, "uncategorised")[1:] if r[4] in PLACES]
    cells = (
        [(r[1], a) for r in report for a in r[3:6]]
        + [(r[0], r[2]) for r in payees]
        + [(r[4], r[3]) for r in listed]
    )
    report_json = [r for r in as_json(place, "report", *OCTOBER) if r["currency"] in PLACES]
    payees_json = [r for r in as_json(place, "payees", *OCTOBER) if r["currency"] in PLACES]
    listed_json = [r for r in as_json(place, "uncategorised") if r["currency"] in PLACES]
    cells += [
        (str(r["currency"]), str(r[k])) for r in report_json for k in ("income", "spending", "net")
    ]
    cells += [(str(r["currency"]), str(r["total"])) for r in payees_json]
    cells += [(str(r["currency"]), str(r["amount"])) for r in listed_json]
    assert {c for c, _ in cells} == set(PLACES)
    assert len(cells) == 2 * (6 + 2 + 4)
    assert all(PLACES[currency].match(amount) for currency, amount in cells), cells
    assert ("JPY", "-1200") in cells
    assert ("BHD", "-1.234") in cells
    assert ("BHD", "0.005") in cells
