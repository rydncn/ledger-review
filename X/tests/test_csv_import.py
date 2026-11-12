from __future__ import annotations

from pathlib import Path

import pytest

from tally.config import Layout, parse_config
from tally.errors import ImportError_
from tally.importers import Note, ParsedRow, RowProblem
from tally.importers.csvfile import read_csv

CURRENT = Layout(
    name="current",
    date="Date",
    description="Description",
    money_out="Money Out",
    money_in="Money In",
    date_format="%d/%m/%Y",
)
CARD = Layout(
    name="card",
    date="Transaction Date",
    description="Payee",
    amount="Amount",
    date_format="%m/%d/%Y",
    delimiter=";",
    decimal_separator=",",
    thousands_separator=".",
)


def rows(items):
    return [i for i in items if isinstance(i, ParsedRow)]


def problems(items):
    return [i for i in items if isinstance(i, RowProblem)]


def notes(items):
    return [i for i in items if isinstance(i, Note)]


def test_current_account_sample(samples):
    items = list(read_csv(samples / "current-account.csv", CURRENT, 2))
    got = [(r.line, r.date, r.amount_minor, r.description) for r in rows(items)]
    assert got == [
        (2, "2026-10-01", -4217, "TESCO STORES 2045, LEEDS"),
        (3, "2026-10-02", 245000, "SALARY ACME LTD"),
        (4, "2026-10-03", 1250, '=HYPERLINK("http://example.invalid",Refund)'),
        (5, "2026-10-03", -340, "TFL TRAVEL CH"),
        (6, "2026-10-05", -15600, "DIRECT DEBIT COUNCIL TAX"),
    ]
    assert problems(items) == []
    assert [n.line for n in notes(items)] == [4, 5]


def test_credit_card_sample_latin1_semicolons(samples):
    items = list(read_csv(samples / "credit-card.csv", CARD, 2))
    got = [(r.date, r.amount_minor, r.description) for r in rows(items)]
    assert got == [
        ("2026-10-02", -420, "CAFÉ NERO"),
        ("2026-10-03", -2399, "AMAZON MARKETPLACE"),
        ("2026-10-03", 15000, "PAYMENT RECEIVED - THANK YOU"),
    ]
    assert problems(items) == []


def write(tmp_path: Path, text: str, name="f.csv", encoding="utf-8") -> Path:
    p = tmp_path / name
    p.write_bytes(text.encode(encoding))
    return p


def test_bad_rows_reported_with_line_and_reason_but_not_values(tmp_path):
    p = write(
        tmp_path,
        "Date,Description,Money Out,Money In\n"
        "01/10/2026,OK,1.00,\n"
        "not a date,SECRET PAYEE,1.00,\n"
        "02/10/2026,SECRET TWO,1.0.0,\n"
        "03/10/2026,SECRET THREE,,\n"
        "04/10/2026,,1.00,\n"
        "05/10/2026,SHORT\n"
        "\n"
        "06/10/2026,OK TOO,,2.00\n",
    )
    items = list(read_csv(p, CURRENT, 2))
    assert [r.description for r in rows(items)] == ["OK", "OK TOO"]
    probs = problems(items)
    assert [pr.line for pr in probs] == [3, 4, 5, 6, 7]
    assert "date" in probs[0].reason and "%d/%m/%Y" in probs[0].reason
    assert "money-out" in probs[1].reason
    assert "empty" in probs[2].reason
    assert "description is empty" in probs[3].reason
    assert "fields" in probs[4].reason
    for pr in probs:
        assert "SECRET" not in pr.reason
    # The offending values are available separately, for --show-rows.
    assert probs[0].values == ("not a date",)
    assert probs[1].values == ("1.0.0",)


def test_multiline_quoted_field_keeps_line_numbers(tmp_path):
    p = write(
        tmp_path,
        'Date,Description,Amount\n01/10/2026,"TWO\nLINES",1.00\n02/10/2026,bad,x\n',
    )
    layout = Layout(name="x", date="Date", description="Description", amount="Amount")
    items = list(read_csv(p, layout, 2))
    assert rows(items)[0].description == "TWO LINES"
    assert problems(items)[0].line == 4


def test_missing_columns_is_a_file_error_that_quotes_the_header_only_on_request(tmp_path):
    p = write(tmp_path, "Datum,Beschreibung,Betrag\n01/10/2026,x,1.00\n")
    with pytest.raises(ImportError_) as exc:
        list(read_csv(p, CURRENT, 2))
    text = str(exc.value)
    assert "'Date'" in text and "'Money Out'" in text and "3 column(s)" in text
    # The file's own words are kept out of the message, available on request.
    assert "Datum" not in text
    assert exc.value.details == [
        "Columns found in the file's header: 'Datum', 'Beschreibung', 'Betrag'"
    ]


def test_header_matching_is_tolerant_of_case_and_spaces(tmp_path):
    p = write(tmp_path, " date , DESCRIPTION ,money out,Money In\n01/10/2026,x,1.00,\n")
    assert rows(read_csv(p, CURRENT, 2))[0].amount_minor == -100


def test_empty_file(tmp_path):
    p = write(tmp_path, "")
    with pytest.raises(ImportError_, match="empty"):
        list(read_csv(p, CURRENT, 2))


def test_not_a_statement_binary(tmp_path):
    p = tmp_path / "image.csv"
    p.write_bytes(bytes(range(256)) * 20)
    with pytest.raises(ImportError_):
        list(read_csv(p, CURRENT, 2))


def test_nul_bytes_mark_a_row_as_binary(tmp_path):
    p = tmp_path / "nul.csv"
    p.write_bytes(b"Date,Description,Money Out,Money In\n01/10/2026,x\x00y,1.00,\n")
    items = list(read_csv(p, CURRENT, 2))
    assert rows(items) == []
    assert problems(items)[0].reason == "the row contains binary data"


def test_header_less_layout_by_index_and_skip_lines(tmp_path):
    p = write(tmp_path, "Statement for account\nexported today\n2026-10-01;Coffee;-3.00\n")
    layout = Layout(
        name="x",
        date=0,
        description=1,
        amount=2,
        header=False,
        skip_lines=2,
        date_format="%Y-%m-%d",
        delimiter=";",
    )
    got = rows(read_csv(p, layout, 2))
    assert len(got) == 1 and got[0].amount_minor == -300 and got[0].line == 3


def test_negate_flips_sign(tmp_path):
    p = write(tmp_path, "Date,Description,Amount\n01/10/2026,x,3.00\n")
    layout = Layout(name="x", date="Date", description="Description", amount="Amount", negate=True)
    assert rows(read_csv(p, layout, 2))[0].amount_minor == -300


def test_memo_column(tmp_path):
    p = write(tmp_path, "Date,Description,Amount,Ref\n01/10/2026,x,3.00,  ref  1 \n")
    layout = Layout(name="x", date="Date", description="Description", amount="Amount", memo="Ref")
    assert rows(read_csv(p, layout, 2))[0].memo == "ref 1"


def test_utf8_bom_and_crlf_and_quoted_commas(tmp_path):
    p = tmp_path / "f.csv"
    p.write_bytes(b'\xef\xbb\xbfDate,Description,Amount\r\n01/10/2026,"A, B",1.00\r\n')
    layout = Layout(name="x", date="Date", description="Description", amount="Amount")
    assert rows(read_csv(p, layout, 2))[0].description == "A, B"


def test_utf16_file(tmp_path):
    p = tmp_path / "f.csv"
    p.write_bytes("Date,Description,Amount\n01/10/2026,CAFÉ,1.00\n".encode("utf-16"))
    layout = Layout(name="x", date="Date", description="Description", amount="Amount")
    assert rows(read_csv(p, layout, 2))[0].description == "CAFÉ"


def test_invalid_bytes_under_declared_encoding_is_file_error(tmp_path):
    p = tmp_path / "f.csv"
    p.write_bytes(b"Date,Description,Amount\n01/10/2026,CAF\xc9,1.00\n")
    layout = Layout(
        name="x", date="Date", description="Description", amount="Amount", encoding="utf-8"
    )
    with pytest.raises(ImportError_, match="not valid utf-8"):
        list(read_csv(p, layout, 2))


def test_both_in_and_out_present_nets_them(tmp_path):
    p = write(tmp_path, "Date,Description,Money Out,Money In\n01/10/2026,x,1.00,0.25\n")
    assert rows(read_csv(p, CURRENT, 2))[0].amount_minor == -75


def test_too_wide_row_without_description_repair_still_parses_other_columns(tmp_path):
    # Extra field lands in the description; the amounts are untouched.
    p = write(tmp_path, "Date,Description,Amount\n01/10/2026,A,B,1.00\n")
    layout = Layout(name="x", date="Date", description="Description", amount="Amount")
    items = list(read_csv(p, layout, 2))
    assert rows(items)[0].description == "A,B"
    assert notes(items)[0].line == 2


def test_large_field_limit(tmp_path):
    p = write(tmp_path, "Date,Description,Amount\n01/10/2026," + "x" * (2 << 20) + ",1.00\n")
    layout = Layout(name="x", date="Date", description="Description", amount="Amount")
    with pytest.raises(ImportError_, match="not readable as CSV"):
        list(read_csv(p, layout, 2))


def test_layout_from_toml_matches_dataclass():
    config = parse_config(
        '[layouts.card]\ndate = "Transaction Date"\ndescription = "Payee"\namount = "Amount"\n'
        'date_format = "%m/%d/%Y"\ndelimiter = ";"\ndecimal_separator = ","\n'
        'thousands_separator = "."\n'
    )
    assert config.layouts["card"] == CARD


BALANCED = Layout(
    name="b",
    date="Date",
    description="Description",
    amount="Amount",
    balance="Balance",
    date_format="%Y-%m-%d",
    thousands_separator="",
)


def balance_notes(items):
    return [(n.line, n.text) for n in notes(items) if "balance" in n.text]


def test_balance_check_passes_oldest_first(tmp_path):
    p = write(
        tmp_path,
        "Date,Description,Amount,Balance\n"
        "2026-10-01,A,-10.00,90.00\n2026-10-02,B,25.00,115.00\n2026-10-03,C,-0.01,114.99\n",
    )
    items = list(read_csv(p, BALANCED, 2))
    assert len(rows(items)) == 3 and balance_notes(items) == []


def test_balance_check_passes_newest_first(tmp_path):
    p = write(
        tmp_path,
        "Date,Description,Amount,Balance\n"
        "2026-10-03,C,-0.01,114.99\n2026-10-02,B,25.00,115.00\n2026-10-01,A,-10.00,90.00\n",
    )
    assert balance_notes(read_csv(p, BALANCED, 2)) == []


def test_balance_check_notes_mismatch_by_line_without_values(tmp_path):
    p = write(
        tmp_path,
        "Date,Description,Amount,Balance\n"
        "2026-10-01,A,-10.00,90.00\n2026-10-02,B,25.00,115.00\n"
        "2026-10-03,SECRET,-0.01,200.00\n2026-10-04,D,1.00,201.00\n",
    )
    items = list(read_csv(p, BALANCED, 2))
    noted = balance_notes(items)
    assert noted[0][0] == 4 and "does not follow" in noted[0][1]
    assert noted[-1][0] is None and noted[-1][1].startswith("1 row(s) did not follow")
    assert len(noted) == 2
    for _, text in noted:
        assert "200" not in text and "SECRET" not in text
    # The rows are still imported; the check only warns.
    assert len(rows(items)) == 4


def test_balance_check_with_wrong_sign_layout_flags_every_row(tmp_path):
    # Spending shown as positive but no negate: the balances fall while the
    # amounts rise, in neither order. (Amounts vary, because a flipped sign
    # on identical amounts is indistinguishable from a newest-first file.)
    p = write(
        tmp_path,
        "Date,Description,Amount,Balance\n"
        "2026-10-01,A,10.00,90.00\n2026-10-02,B,20.00,70.00\n2026-10-03,C,30.00,40.00\n",
    )
    noted = balance_notes(read_csv(p, BALANCED, 2))
    assert [ln for ln, _ in noted] == [3, 4, None]
    assert noted[-1][1].startswith("2 row(s)")


def test_balance_check_skips_unreadable_or_empty_balances(tmp_path):
    p = write(
        tmp_path,
        "Date,Description,Amount,Balance\n"
        "2026-10-01,A,-10.00,90.00\n2026-10-02,B,25.00,n/a\n2026-10-03,C,-1.00,\n"
        "2026-10-04,D,-1.00,500.00\n2026-10-05,E,-1.00,499.00\n",
    )
    items = list(read_csv(p, BALANCED, 2))
    noted = balance_notes(items)
    assert noted == [(3, "the balance could not be read, so this row was not checked")]
    assert len(rows(items)) == 5


def test_balance_check_waits_for_an_unambiguous_pair(tmp_path):
    # Zero-amount rows cannot tell oldest-first from newest-first; the check
    # decides on the first pair that can, then applies that order.
    p = write(
        tmp_path,
        "Date,Description,Amount,Balance\n"
        "2026-10-01,A,0.00,90.00\n2026-10-02,B,0.00,90.00\n2026-10-03,C,-5.00,85.00\n"
        "2026-10-04,D,-5.00,80.00\n",
    )
    assert balance_notes(read_csv(p, BALANCED, 2)) == []


def test_sample_balance_column_confirms_sign_decisions(samples):
    layout = Layout(
        name="current",
        date="Date",
        description="Description",
        money_out="Money Out",
        money_in="Money In",
        balance="Balance",
        date_format="%d/%m/%Y",
    )
    items = list(read_csv(samples / "current-account.csv", layout, 2))
    assert balance_notes(items) == []
    assert len(rows(items)) == 5


def test_read_csv_with_known_encoding_skips_detection(tmp_path):
    p = write(tmp_path, "Date,Description,Amount\n01/10/2026,CAFÉ,1.00\n", encoding="latin-1")
    layout = Layout(name="x", date="Date", description="Description", amount="Amount")
    assert rows(read_csv(p, layout, 2, encoding="latin-1"))[0].description == "CAFÉ"
