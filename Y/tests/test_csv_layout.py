"""AC43, AC10 to AC13, AC17, AC40, ADR 0008: a bank's CSV, described once, read as hostile bytes."""

from __future__ import annotations

import json
import tracemalloc
from collections.abc import Mapping
from pathlib import Path

import pytest

from tally.cli import main
from tally.config import ConfigError
from tally.importers import NotAStatementError, Transaction, Unreadable, kind
from tally.importers.csv_layout import MAX_FIELD, MAX_LINE, Layout, read
from tests import SAMPLES

HEADER = "Date,Description,Money Out,Money In,Balance\r\n"
BANK: dict[str, str] = {
    "date": "Date",
    "description": "Description",
    "money_out": "Money Out",
    "money_in": "Money In",
    "date_format": "%d/%m/%Y",
    "decimal": ".",
    "thousands": ",",
}
CARD: dict[str, str] = {
    "delimiter": ";",
    "date": "Transaction Date",
    "description": "Payee",
    "amount": "Amount",
    "date_format": "%m/%d/%Y",
    "decimal": ",",
    "thousands": ".",
    "encoding": "latin-1",
}


def toml_layout(name: str, table: Mapping[str, object]) -> str:
    """A ``[layouts.<name>]`` table; JSON strings are valid TOML basic strings."""
    return f"[layouts.{name}]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in table.items())


def without(table: dict[str, str], *keys: str) -> dict[str, str]:
    return {k: v for k, v in table.items() if k not in keys}


def test_ac43_the_sample_layouts_build_with_their_defaults() -> None:
    bank = Layout.from_config("bank", BANK)
    assert (bank.name, bank.date, bank.description) == ("bank", "Date", "Description")
    assert (bank.amount, bank.money_in, bank.money_out) == (None, "Money In", "Money Out")
    assert (bank.delimiter, bank.decimal, bank.thousands, bank.encoding) == (",", ".", ",", "utf-8")
    assert bank.date_format == "%d/%m/%Y"
    card = Layout.from_config("card", CARD)
    assert (card.amount, card.money_in, card.money_out) == ("Amount", None, None)
    assert (card.delimiter, card.decimal, card.thousands, card.encoding) == (
        ";",
        ",",
        ".",
        "latin-1",
    )
    assert (card.date, card.description, card.date_format) == (
        "Transaction Date",
        "Payee",
        "%m/%d/%Y",
    )


@pytest.mark.parametrize(
    ("table", "problem"),
    [
        (without(BANK, "date"), 'no "date" column named'),
        (without(BANK, "description"), 'no "description" column named'),
        (without(BANK, "date_format"), "no date_format"),
        ({**BANK, "date_format": "%Q"}, 'date_format "%Q" is not a date format'),
        (
            {**BANK, "date_format": "%H:%M"},
            'date_format "%H:%M" does not give a day, month and year',
        ),
        ({**BANK, "thousands": "."}, "decimal and thousands must differ"),
        ({**BANK, "decimal": ".."}, "decimal must be one character"),
        ({**BANK, "thousands": ""}, "thousands must be one character"),
        ({**CARD, "delimiter": ";;"}, "delimiter must be one character"),
        ({**CARD, "encoding": "latin-9x"}, 'unknown encoding "latin-9x"'),
        (
            {**BANK, "amount": "Amount"},
            "amount cannot be named together with money_in or money_out",
        ),
        (
            without({**BANK, "amount": "Amount"}, "money_out"),
            "amount cannot be named together with money_in or money_out",
        ),
        (
            without({**BANK, "amount": "Amount"}, "money_in"),
            "amount cannot be named together with money_in or money_out",
        ),
        (
            without(BANK, "money_in"),
            'no "amount" column named, nor both "money_in" and "money_out"',
        ),
        ({**BANK, "date_fromat": "%d"}, 'unknown key "date_fromat"'),
        ({**BANK, "date": 1}, "date must be a string"),
        ({**BANK, "balance": "Balance"}, 'unknown key "balance"'),
        ({**BANK, "delimiter": "\n"}, "delimiter cannot be a quote or a line ending"),
        ({**BANK, "delimiter": "\r"}, "delimiter cannot be a quote or a line ending"),
        ({**BANK, "delimiter": '"'}, "delimiter cannot be a quote or a line ending"),
        *[
            ({**BANK, "encoding": name}, f'encoding "{name}" is not supported')
            for name in ("utf-16", "UTF-32-LE", "utf-7", "unicode_escape", "raw_unicode_escape")
        ],
    ],
)
def test_ac43_an_invalid_layout_names_itself_and_the_problem(
    table: dict[str, object], problem: str
) -> None:
    with pytest.raises(ConfigError) as raised:
        Layout.from_config("bank", table)
    assert str(raised.value) == f"layouts.bank: {problem}"


@pytest.mark.parametrize(
    ("layout", "config_text", "message"),
    [
        ("nosuch", toml_layout("bank", BANK), 'no layout named "nosuch"'),
        (
            "bank",
            toml_layout("bank", without(BANK, "date")),
            'layouts.bank: no "date" column named',
        ),
        ("bank", toml_layout("bank", {**BANK, "date_format": "%Q"}), "is not a date format"),
        ("bank", toml_layout("bank", {**BANK, "thousands": "."}), "must differ"),
    ],
)
def test_ac43_an_unknown_or_invalid_layout_is_exit_2_before_the_data_file_opens(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    layout: str,
    config_text: str,
    message: str,
) -> None:
    config = tmp_path / "tally.toml"
    config.write_text(config_text, encoding="utf-8")
    data_dir = tmp_path / "data"
    argv = ["import", "--account", "current", "--layout", layout, "samples/current-account.csv"]
    code = main([*argv, "--config", str(config), "--data-dir", str(data_dir)])
    captured = capsys.readouterr()
    assert (code, captured.out) == (2, "")
    assert message in captured.err
    assert captured.err.count("\n") == 1
    assert not data_dir.exists()


def bank_file(tmp_path: Path, *rows: str, header: str = HEADER) -> Path:
    path = tmp_path / "statement.csv"
    path.write_bytes((header + "".join(row + "\r\n" for row in rows)).encode("utf-8"))
    return path


def read_bank(path: Path) -> list[Transaction | Unreadable]:
    return list(read(path, Layout.from_config("bank", BANK), 2))


def test_ac09_ac10_ac12_the_current_account_sample_is_five_exact_rows() -> None:
    rows = read_bank(SAMPLES / "current-account.csv")
    assert [
        (r.line, r.date, r.description, r.amount_minor) for r in rows if isinstance(r, Transaction)
    ] == [
        (2, "2026-10-01", "TESCO STORES 2045, LEEDS", -4217),
        (3, "2026-10-02", "SALARY ACME LTD", 245000),
        (4, "2026-10-03", '=HYPERLINK("http://example.invalid","Refund")', 1250),
        (5, "2026-10-03", "TFL TRAVEL CH", -340),
        (6, "2026-10-05", "DIRECT DEBIT COUNCIL TAX", -15600),
    ]
    assert len(rows) == 5
    assert [r.fingerprint for r in rows if isinstance(r, Transaction)] == [
        "csv:2026-10-01|-4217|tesco stores 2045, leeds|1",
        "csv:2026-10-02|245000|salary acme ltd|1",
        'csv:2026-10-03|1250|=hyperlink("http://example.invalid","refund")|1',
        "csv:2026-10-03|-340|tfl travel ch|1",
        "csv:2026-10-05|-15600|direct debit council tax|1",
    ]


@pytest.mark.parametrize(
    ("given", "accepted"),
    [
        ("utf-8-sig", True),  # what Excel writes; read() strips the mark anyway
        ("UTF8", True),
        ("windows-1252", True),
        ("latin-1", True),
        ("iso8859-15", True),  # Latin-9: Latin-1 with the euro sign
        ("us-ascii", True),
        ("cp1250", False),
    ],
)
def test_ac43_the_encodings_a_layout_may_name(given: str, accepted: bool) -> None:
    table = {**BANK, "encoding": given}
    if accepted:
        assert Layout.from_config("bank", table).encoding == given
    else:
        with pytest.raises(
            ConfigError, match=f'^layouts.bank: encoding "{given}" is not supported$'
        ):
            Layout.from_config("bank", table)


def test_ac11_latin_1_semicolons_decimal_commas_and_us_dates() -> None:
    rows = list(read(SAMPLES / "credit-card.csv", Layout.from_config("card", CARD), 2))
    assert [
        (r.line, r.date, r.description, r.amount_minor) for r in rows if isinstance(r, Transaction)
    ] == [
        (2, "2026-10-02", "CAFÉ NERO", -420),
        (3, "2026-10-03", "AMAZON MARKETPLACE", -2399),
        (4, "2026-10-03", "PAYMENT RECEIVED - THANK YOU", 15000),
    ]
    assert len(rows) == 3


def test_ac13_rows_that_cannot_be_read_are_reported_by_line_and_reason(tmp_path: Path) -> None:
    path = bank_file(
        tmp_path,
        "01/10/2026,FIRST,1.00,,",
        "31/02/2026,SECOND,1.00,,",
        "01/10/2026,THIRD,twelve,,",
        "01/10/2026,FOURTH,1.00",
        "01/10/2026,FIFTH,1.00,,,extra",
        "01/10/2026,SIXTH,,,",
        "01/10/2026,SEVENTH,1.005,,",
        "",
        '01/10/2026,"EIGHTH',
        'ACROSS LINES",2.00,,',
        "01/10/2026,NINTH,(3.40),,",
        "01/10/2026,TENTH,-3.40,,",
        "01/10/2026,ELEVENTH,,-5.00,",
        "01/10/2026,TWELFTH,1.00,2.50,",
        '01/10/2026,"OPEN,1.00,,',
    )
    outcomes = read_bank(path)
    assert outcomes[1:8] == [
        Unreadable(3, "date does not match %d/%m/%Y"),
        Unreadable(4, "amount is not a number"),
        Unreadable(5, "expected 5 columns, found 3"),
        Unreadable(6, "expected 5 columns, found 6"),
        Unreadable(7, "no amount"),
        Unreadable(8, "amount has more than 2 decimal places"),
        Transaction(
            10,  # the line the record starts on
            "2026-10-01",
            "EIGHTH\r\nACROSS LINES",
            -200,
            "csv:2026-10-01|-200|eighth across lines|1",
        ),
    ]
    assert [(r.line, r.amount_minor) for r in outcomes[8:12] if isinstance(r, Transaction)] == [
        (12, -340),
        (13, -340),
        (14, 500),
        (15, 150),
    ]
    assert outcomes[12] == Unreadable(16, "expected 5 columns, found 2")
    assert len(outcomes) == 13
    for outcome in outcomes:
        if isinstance(outcome, Unreadable):
            assert "/" not in outcome.reason.replace("%d/%m/%Y", "")
            assert not any(word in outcome.reason for word in ("SECOND", "THIRD", "twelve", "OPEN"))


def test_ac16_identical_rows_in_one_file_are_numbered_and_descriptions_normalised(
    tmp_path: Path,
) -> None:
    path = bank_file(
        tmp_path,
        "03/10/2026,COSTA COFFEE,2.50,,",
        "03/10/2026,COSTA COFFEE,2.50,,",
        '03/10/2026,"  Costa   coffee ",2.50,,',
        "03/10/2026,COSTA COFFEE,2.51,,",
    )
    rows = read_bank(path)
    assert [r.fingerprint for r in rows if isinstance(r, Transaction)] == [
        "csv:2026-10-03|-250|costa coffee|1",
        "csv:2026-10-03|-250|costa coffee|2",
        "csv:2026-10-03|-250|costa coffee|3",
        "csv:2026-10-03|-251|costa coffee|1",
    ]
    assert [r.description for r in rows if isinstance(r, Transaction)][2] == "Costa   coffee"


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256)) * 4,
            "not a text file: not valid utf-8",
        ),
        (b"", "empty file"),
        (
            b"\xef\xbb\xbfDay,Payee,Out,In\r\n01/10/2026,X,1.00,,\r\n",
            'header has no column named "Date"',
        ),
        (b"Date,Description,Money Out,Balance\r\n", 'header has no column named "Money In"'),
        (b"\n", 'header has no column named "Date"'),
        (
            HEADER.encode() + b"01/10/2026,X,1.00,,\r\n" * 60 + b"01/10/2026,BAD\x00NUL,1.00,,\r\n",
            "not a text file",
        ),
        (
            b"Date,Description,Money Out,Money In\r\n01/10/2026,X,1.00,\r\n"
            b"01/10/2026,Y\xff,1.00,\r\n",
            "not a text file: not valid utf-8",
        ),
        (
            b'Date,Description,Money Out,Money In,Balance,"' + b"x" * 3_000_000 + b'"\r\n',
            "line 1: field longer than 1 MB",
        ),
    ],
)
def test_ac17_a_file_that_is_not_a_statement_is_refused_whole(
    tmp_path: Path, content: bytes, reason: str
) -> None:
    path = tmp_path / "statement.csv"
    path.write_bytes(content)
    with pytest.raises(NotAStatementError) as raised:
        list(read(path, Layout.from_config("bank", BANK), 2))
    assert str(raised.value) == reason


def test_ac17_a_header_alone_imports_nothing_and_refuses_nothing(tmp_path: Path) -> None:
    assert read_bank(bank_file(tmp_path)) == []
    assert read_bank(bank_file(tmp_path, "", "")) == []


def test_which_importer_reads_a_file_is_decided_from_its_first_bytes(tmp_path: Path) -> None:
    assert kind(SAMPLES / "current-account.csv") == "csv"
    assert kind(SAMPLES / "credit-card.csv") == "csv"
    assert kind(SAMPLES / "savings.ofx") == "ofx-sgml"
    for first in (b"<?xml version='1.0'?>", b'\xef\xbb\xbf\n<?OFX OFXHEADER="200"?>', b"  <OFX>"):
        path = tmp_path / "s.csv"
        path.write_bytes(first + b"\n<OFX></OFX>\n")
        assert kind(path) == "ofx-xml"
    (tmp_path / "empty").write_bytes(b"")
    with pytest.raises(NotAStatementError, match=r"^empty file$"):
        kind(tmp_path / "empty")
    (tmp_path / "image.csv").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    with pytest.raises(NotAStatementError, match=r"^not a text file$"):
        kind(tmp_path / "image.csv")
    with pytest.raises(NotAStatementError, match=r"^cannot read the statement file: No such file"):
        kind(tmp_path / "missing.csv")
    with pytest.raises(
        NotAStatementError, match=r"^cannot read the statement file: Is a directory$"
    ):
        kind(tmp_path)


def test_ac40_a_monstrous_field_is_one_unreadable_row_and_costs_bounded_memory(
    tmp_path: Path,
) -> None:
    path = bank_file(
        tmp_path,
        "01/10/2026,FIRST,1.00,,",
        "02/10/2026,SECOND,2.00,,",
        '03/10/2026,"' + "x" * 50_000_000 + '",3.00,,',
        "04/10/2026,FOURTH,4.00,,",
    )
    tracemalloc.start()
    try:
        outcomes = read_bank(path)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert [(o.line, type(o).__name__) for o in outcomes] == [
        (2, "Transaction"),
        (3, "Transaction"),
        (4, "Unreadable"),
        (5, "Transaction"),
    ]
    assert outcomes[2] == Unreadable(4, "field longer than 1 MB")
    assert peak < 32 * 1024 * 1024


def test_ac13_an_unclosed_quote_is_reported_where_it_opened_and_how_far_it_ran(
    tmp_path: Path,
) -> None:
    path = bank_file(
        tmp_path,
        "01/10/2026,FIRST,1.00,,",
        '02/10/2026,"OPENED HERE,2.00,,',
        "03/10/2026,SWALLOWED,3.00,,",
        "04/10/2026,SWALLOWED,4.00,,",
        '05/10/2026,CLOSED",5.00',
        "06/10/2026,AFTER,6.00,,",
    )
    assert read_bank(path)[1:] == [
        Unreadable(3, "expected 5 columns, found 3, in a record that runs to line 6"),
        Transaction(7, "2026-10-06", "AFTER", -600, "csv:2026-10-06|-600|after|1"),
    ]


def test_ac13_a_date_in_digits_that_are_not_ascii_is_unreadable(tmp_path: Path) -> None:
    arabic = "".join(chr(0x0660 + int(c)) if c.isdigit() else c for c in "01/10/2026")
    path = bank_file(tmp_path, f"{arabic},X,1.00,,")
    assert read_bank(path) == [Unreadable(2, "date does not match %d/%m/%Y")]


def test_ac40_the_field_limit_is_exactly_one_megabyte(tmp_path: Path) -> None:
    path = bank_file(
        tmp_path,
        '01/10/2026,"' + "y" * 1_048_576 + '",1.00,,',
        '01/10/2026,"' + "y" * 1_048_577 + '",1.00,,',
        '01/10/2026,"' + "y\r\n" * 400_000 + '",1.00,,',
    )
    outcomes = read_bank(path)
    assert isinstance(outcomes[0], Transaction)
    assert len(outcomes[0].description) == 1_048_576
    assert outcomes[1] == Unreadable(3, "field longer than 1 MB")
    # The csv module stops at the line where the field passed 1 MB; the rest of that field's
    # lines are each a short row of their own, bounded in cost and reported like any other.
    assert isinstance(outcomes[2], Unreadable)
    assert outcomes[2].line == 4
    reason, _, end = outcomes[2].reason.rpartition(" ")
    assert reason == "field longer than 1 MB, in a record that runs to line"
    assert 4 < int(end) < 400_004
    assert {o.reason for o in outcomes[3:] if isinstance(o, Unreadable)} == {
        "expected 5 columns, found 1"
    }
    assert outcomes[-1].line == 400_004
    assert len(outcomes) == 3 + 400_004 - int(end)


def test_the_briefs_unquoted_formula_cell_is_six_columns_as_any_csv_reader_sees_it(
    tmp_path: Path,
) -> None:
    """The brief wrote the sample's line 4 unquoted; its comma splits the cell, so the sample
    in ``samples/`` quotes it (recorded in the plan). No reader guesses otherwise."""
    path = bank_file(
        tmp_path, '03/10/2026,=HYPERLINK("http://example.invalid","Refund"),,12.50,"4,420.33"'
    )
    assert read_bank(path) == [Unreadable(2, "expected 5 columns, found 6")]


def test_a_file_that_vanished_between_the_first_bytes_and_the_read_is_refused(
    tmp_path: Path,
) -> None:
    with pytest.raises(NotAStatementError, match=r"^cannot read the statement file: No such file"):
        list(read(tmp_path / "gone.csv", Layout.from_config("bank", BANK), 2))


def exact_line(ending: str) -> str:
    """A row whose physical line is exactly MAX_LINE characters, terminator included."""
    description = "d" * MAX_FIELD
    rest = '01/10/2026,"",1.00,,' + ending
    return f'01/10/2026,"{description}",1.00,,' + "b" * (MAX_LINE - MAX_FIELD - len(rest)) + ending


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
def test_ac40_the_line_guard_is_exact_and_leaves_the_next_row_alone(
    tmp_path: Path, ending: str
) -> None:
    path = tmp_path / "statement.csv"
    path.write_text(
        HEADER.replace("\r\n", ending)
        + exact_line(ending)
        + '02/10/2026,"'
        + "x" * 3_000_000
        + '",2.00,,'
        + ending
        + "03/10/2026,THIRD,3.00,,"
        + ending
        + "04/10/2026,LAST WITHOUT A LINE ENDING,4.00,,",
        encoding="utf-8",
        newline="",
    )
    outcomes = read_bank(path)
    assert [(o.line, type(o).__name__) for o in outcomes] == [
        (2, "Transaction"),
        (3, "Unreadable"),
        (4, "Transaction"),
        (5, "Transaction"),
    ]
    assert isinstance(outcomes[0], Transaction)
    assert len(outcomes[0].description) == MAX_FIELD
    assert outcomes[1] == Unreadable(3, "field longer than 1 MB")
    assert [o.description for o in outcomes[2:] if isinstance(o, Transaction)] == [
        "THIRD",
        "LAST WITHOUT A LINE ENDING",
    ]


def test_ac13_a_compact_date_format_reads_only_its_own_width(tmp_path: Path) -> None:
    # strptime reads "2026111" under %Y%m%d as 1 November: a guess, not a date.
    layout = Layout.from_config("compact", {**BANK, "date_format": "%Y%m%d"})
    path = bank_file(tmp_path, "2026111,SHORT,1.00,,", "20261101,WHOLE,1.00,,")
    short, whole = read(path, layout, 2)
    assert short == Unreadable(2, "date does not match %Y%m%d")
    assert isinstance(whole, Transaction)
    assert whole.date == "2026-11-01"


def test_ac13_a_compact_format_reads_a_month_name_in_any_case(tmp_path: Path) -> None:
    layout = Layout.from_config("named", {**BANK, "date_format": "%d%b%Y"})
    path = bank_file(tmp_path, "01NOV2026,UPPER,1.00,,", "01Nov2026,TITLE,1.00,,")
    assert [r.date for r in read(path, layout, 2) if isinstance(r, Transaction)] == [
        "2026-11-01",
        "2026-11-01",
    ]
