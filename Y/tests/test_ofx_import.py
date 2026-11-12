"""AC21 to AC25, AC39, AC41, AC34, AC35: ``tally import`` on OFX files."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests import SAMPLES
from tests.test_import import Place, console_script
from tests.test_ofx import BILLION_LAUGHS, GOOD, SGML_HEADER, XML_SAVINGS, nested, statement

SAMPLE = SAMPLES / "savings.ofx"
SAVINGS_ROWS = [
    ("2026-10-01", "INTEREST", 1234, "ofx:2026100101"),
    ("2026-10-15", "TRANSFER TO CURRENT", -50000, "ofx:2026101501"),
]
BROKEN = SGML_HEADER + statement(
    GOOD,
    "<TRNTYPE>DEBIT<DTPOSTED>20261002<TRNAMT>12,34<FITID>F2<NAME>SECOND",
    "<TRNTYPE>DEBIT<TRNAMT>-3.00<FITID>F3<NAME>THIRD",
)


def ofx_file(tmp_path: Path, content: str, name: str = "statement.ofx") -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8", newline="")
    return path


def import_ofx(place: Place, file: Path, *argv: str) -> tuple[int, str, str]:
    return place.run("import", "--account", "savings", str(file), *argv)


def rows(place: Place) -> list[tuple[object, ...]]:
    return place.query(
        "SELECT t.date, t.description, t.amount_minor, t.fingerprint FROM transactions t"
        " JOIN accounts a ON a.id = t.account_id WHERE a.name = 'savings' ORDER BY t.id"
    )


def test_ac21_the_sgml_sample_imports_two_exact_transactions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    assert import_ofx(place, SAMPLE) == (0, "added 2, already there 0, unreadable 0\n", "")
    assert rows(place) == SAVINGS_ROWS
    assert place.imports() == [(str(SAMPLE), 2, 0, 0)]


def test_ac22_the_same_statement_as_xml_then_the_sgml_adds_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    xml = ofx_file(tmp_path, XML_SAVINGS)
    assert import_ofx(place, xml) == (0, "added 2, already there 0, unreadable 0\n", "")
    assert rows(place) == SAVINGS_ROWS
    assert import_ofx(place, SAMPLE) == (0, "added 0, already there 2, unreadable 0\n", "")
    assert rows(place) == SAVINGS_ROWS


def test_ac23_the_same_ofx_file_twice_adds_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    import_ofx(place, SAMPLE)
    assert import_ofx(place, SAMPLE) == (0, "added 0, already there 2, unreadable 0\n", "")
    assert [row[1:] for row in place.imports()] == [(2, 0, 0), (0, 2, 0)]


def test_ac23_a_fitid_twice_in_one_file_is_one_transaction(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    twice = ofx_file(tmp_path, SGML_HEADER + statement(GOOD, GOOD))
    assert import_ofx(place, twice) == (0, "added 1, already there 1, unreadable 0\n", "")


def test_ac24_a_currency_that_is_not_the_accounts_is_refused_and_nothing_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    assert place.run("account", "forget", "savings", "--yes")[0] == 0
    assert place.run("account", "add", "savings", "--currency", "EUR") == (0, "", "")
    before = place.data_file.read_bytes()
    assert import_ofx(place, SAMPLE) == (
        1,
        "",
        "the file is in GBP but the account is in EUR\n",
    )
    assert place.data_file.read_bytes() == before
    assert place.transactions() == []
    assert place.imports() == []


def test_ac25_a_broken_transaction_is_reported_by_position_and_the_rest_imported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    assert import_ofx(place, ofx_file(tmp_path, BROKEN)) == (
        0,
        "added 1, already there 0, unreadable 2\n",
        "transaction 2: amount is not a number\ntransaction 3: no DTPOSTED\n",
    )
    assert rows(place) == [("2026-10-01", "GOOD", -100, "ofx:F1")]


def test_ac35_the_ofx_summary_has_a_json_form_keyed_by_transaction(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    broken = ofx_file(tmp_path, BROKEN)
    code, out, err = import_ofx(place, broken, "--format", "json")
    assert (code, err) == (
        0,
        "transaction 2: amount is not a number\ntransaction 3: no DTPOSTED\n",
    )
    assert out.count("\n") == 1
    assert json.loads(out) == {
        "account": "savings",
        "file": str(broken),
        "added": 1,
        "duplicates": 0,
        "unreadable_count": 2,
        "unreadable": [
            {"transaction": 2, "reason": "amount is not a number"},
            {"transaction": 3, "reason": "no DTPOSTED"},
        ],
    }


def test_ac39_a_doctype_is_refused_with_its_own_message_and_nothing_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    laughs = ofx_file(tmp_path, BILLION_LAUGHS)
    assert import_ofx(place, laughs) == (
        1,
        "",
        "the file declares a DOCTYPE, which statements never do\n",
    )
    assert place.transactions() == []
    assert place.imports() == []


def test_ac41_ten_thousand_nested_tags_are_refused_and_nothing_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    deep = ofx_file(tmp_path, SGML_HEADER + nested(10_000))
    assert import_ofx(place, deep) == (1, "", "not a statement: tags nested deeper than 64\n")
    assert place.imports() == []


def test_ac41_transactions_without_a_fitid_are_reported_and_the_import_completes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    no_fitid = "<TRNTYPE>DEBIT<DTPOSTED>20261001<TRNAMT>-1.00<NAME>X"
    path = ofx_file(tmp_path, SGML_HEADER + statement(no_fitid, GOOD, no_fitid))
    assert import_ofx(place, path) == (
        0,
        "added 1, already there 0, unreadable 2\n",
        "transaction 1: no FITID\ntransaction 3: no FITID\n",
    )
    assert place.imports() == [(str(path), 1, 0, 2)]


def test_ac34_an_ofx_file_with_layout_is_a_usage_error_and_a_non_ofx_xml_file_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    assert import_ofx(place, SAMPLE, "--layout", "bank") == (
        2,
        "",
        "--layout is for CSV files, and this is an OFX file\n",
    )
    svg = ofx_file(tmp_path, '<?xml version="1.0"?><svg></svg>', name="picture.svg")
    assert import_ofx(place, svg) == (1, "", "not a statement: the first tag is not <OFX>\n")
    assert place.imports() == []


def test_ac19_nothing_personal_in_ofx_messages_with_or_without_verbose(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    markers = ["PAYEE-A7F3K9", "MEMO-Q2M8ZT", "FITID-55RX", "4242.42", "PAYEE-UNREAD-9QW", "13,13"]
    personal = ofx_file(
        tmp_path,
        SGML_HEADER
        + statement(
            f"<DTPOSTED>20261001<TRNAMT>{markers[3]}<FITID>{markers[2]}<NAME>{markers[0]}"
            f"<MEMO>{markers[1]}",
            f"<DTPOSTED>20261002<TRNAMT>{markers[5]}<FITID>F2<NAME>{markers[4]}",
        ),
    )
    for flags in ([], ["--verbose"], ["--verbose", "--format", "json"]):
        code, out, err = import_ofx(place, personal, *flags)
        assert code == 0
        for marker in markers:
            assert marker not in err
            assert marker not in out
    assert "reading" in err


def test_ac34_a_statement_path_that_is_not_utf_8_is_imported_and_recorded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    raw = os.fsencode(tmp_path) + b"/st\xffmt.ofx"
    with open(raw, "wb") as handle:
        handle.write(SAMPLE.read_bytes())
    code, out, err = import_ofx(place, Path(os.fsdecode(raw)), "--format", "json")
    assert (code, err) == (0, "")
    shown = str(tmp_path) + "/st�mt.ofx"
    assert json.loads(out)["file"] == shown
    assert place.imports() == [(shown, 2, 0, 0)]


def test_formula_looking_descriptions_are_stored_as_the_file_has_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    names = ['=HYPERLINK("http://example.invalid","Refund")', "+1", "-SUM(A1)", "@x"]
    path = ofx_file(
        tmp_path,
        SGML_HEADER
        + statement(
            *[
                f"<DTPOSTED>20261001<TRNAMT>-1<FITID>F{n}<NAME>{name}"
                for n, name in enumerate(names)
            ]
        ),
    )
    assert import_ofx(place, path) == (0, "added 4, already there 0, unreadable 0\n", "")
    assert [row[1] for row in rows(place)] == names


def test_verbose_says_what_is_read_for_an_ofx_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    code, _, err = import_ofx(place, SAMPLE, "--verbose")
    assert code == 0
    assert err.splitlines() == [f"reading {SAMPLE} as OFX SGML in cp1252", "read 2 transactions"]
    code, _, err = import_ofx(place, ofx_file(tmp_path, XML_SAVINGS), "--verbose")
    assert code == 0
    assert err.splitlines()[0].endswith(" as OFX XML in utf-8")


def test_the_installed_command_imports_ofx_and_never_opens_a_doctypes_system_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    common = ["--config", str(place.config), "--data-dir", str(place.data_dir)]

    def tally_import(file: Path, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(console_script()), "import", "--account", "savings", str(file), *argv, *common],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,  # opening the FIFO would block forever; this turns it into a failure
        )

    imported = tally_import(SAMPLE, "--format", "json")
    assert (imported.returncode, imported.stderr) == (0, "")
    assert json.loads(imported.stdout)["added"] == 2
    fifo = tmp_path / "entity"
    os.mkfifo(fifo)
    external = ofx_file(
        tmp_path,
        f'<?xml version="1.0"?>\n<!DOCTYPE OFX [<!ENTITY x SYSTEM "file://{fifo}">]>\n'
        "<OFX><STMTTRN><FITID>F9<DTPOSTED>20261001<TRNAMT>1<NAME>&x;</OFX>\n",
    )
    refused = tally_import(external)
    assert (refused.returncode, refused.stdout, refused.stderr) == (
        1,
        "",
        "the file declares a DOCTYPE, which statements never do\n",
    )
    assert rows(place) == SAVINGS_ROWS


FORGED = "<TRNTYPE>DEBIT<DTPOSTED>20261015<TRNAMT>-0.01<FITID>2026101501<NAME>FORGED"
TAKEN = "its FITID is already used by a different transaction"


def test_ac22_a_fitid_already_used_by_a_different_transaction_is_reported_not_dropped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    forged = ofx_file(tmp_path, SGML_HEADER + statement(FORGED), name="forged.ofx")
    assert import_ofx(place, forged)[0] == 0
    assert import_ofx(place, SAMPLE) == (
        0,
        "added 1, already there 0, unreadable 1\n",
        f"transaction 2: {TAKEN}\n",
    )
    assert import_ofx(place, forged) == (0, "added 0, already there 1, unreadable 0\n", "")


def test_ac22_two_different_transactions_sharing_a_fitid_in_one_file_are_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    other = GOOD.replace("-1.00", "-999.00")
    file = ofx_file(tmp_path, SGML_HEADER + statement(GOOD, other, GOOD))
    assert import_ofx(place, file) == (
        0,
        "added 1, already there 1, unreadable 1\n",
        f"transaction 2: {TAKEN}\n",
    )
