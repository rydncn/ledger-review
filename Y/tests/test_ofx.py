"""AC21, AC22, AC25, AC39, AC41, ADR 0008: OFX statements, SGML and XML, read as hostile bytes."""

from __future__ import annotations

import codecs
import time
import tracemalloc
from pathlib import Path

import pytest

from tally.importers import NotAStatementError, Transaction, Unreadable
from tally.importers.ofx import MAX_PIECE, read
from tests import SAMPLES

SGML_HEADER = (
    "OFXHEADER:100\r\nDATA:OFXSGML\r\nVERSION:102\r\nENCODING:USASCII\r\nCHARSET:1252\r\n\r\n"
)
XML_HEADER = (
    '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\r\n'
    '<?OFX OFXHEADER="200" VERSION="220" SECURITY="NONE"'
    ' OLDFILEUID="NONE" NEWFILEUID="NONE"?>\r\n'
)
# The sample statement as OFX 2.x XML: the prolog, the OFX processing instruction, closed tags.
XML_SAVINGS = (
    XML_HEADER
    + """<OFX>
  <!-- the same statement as samples/savings.ofx -->
  <BANKMSGSRSV1>
    <STMTTRNRS>
      <STMTRS>
        <CURDEF>GBP</CURDEF>
        <BANKTRANLIST>
          <STMTTRN>
            <TRNTYPE>CREDIT</TRNTYPE>
            <DTPOSTED>20261001</DTPOSTED>
            <TRNAMT>12.34</TRNAMT>
            <FITID>2026100101</FITID>
            <NAME>INTEREST</NAME>
          </STMTTRN>
          <STMTTRN>
            <TRNTYPE>DEBIT</TRNTYPE>
            <DTPOSTED>20261015120000.000[0:GMT]</DTPOSTED>
            <TRNAMT>-500.00</TRNAMT>
            <FITID>2026101501</FITID>
            <NAME>TRANSFER TO CURRENT</NAME>
          </STMTTRN>
        </BANKTRANLIST>
      </STMTRS>
    </STMTTRNRS>
  </BANKMSGSRSV1>
</OFX>
"""
)
SAVINGS = [
    Transaction(1, "2026-10-01", "INTEREST", 1234, "ofx:2026100101"),
    Transaction(2, "2026-10-15", "TRANSFER TO CURRENT", -50000, "ofx:2026101501"),
]
UTF8_HEADER = SGML_HEADER.replace("USASCII", "UTF-8")
GOOD_READ = Transaction(1, "2026-10-01", "GOOD", -100, "ofx:F1")
GOOD = "<TRNTYPE>DEBIT<DTPOSTED>20261001<TRNAMT>-1.00<FITID>F1<NAME>GOOD"


def statement(*transactions: str, currency: str = "GBP") -> str:
    """An SGML statement body holding each transaction's leaves in its own ``STMTTRN``."""
    return (
        f"<OFX>\r\n<BANKMSGSRSV1><STMTTRNRS><STMTRS>\r\n<CURDEF>{currency}\r\n<BANKTRANLIST>\r\n"
        + "".join(f"<STMTTRN>{t}\r\n" for t in transactions)
        + "</BANKTRANLIST>\r\n</STMTRS></STMTTRNRS></BANKMSGSRSV1>\r\n</OFX>\r\n"
    )


def ofx_file(tmp_path: Path, content: str | bytes, name: str = "statement.ofx") -> Path:
    path = tmp_path / name
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    return path


def read_all(path: Path, currency: str = "GBP") -> list[Transaction | Unreadable]:
    return list(read(path, currency, 2))


def refusal(path: Path, currency: str = "GBP") -> str:
    with pytest.raises(NotAStatementError) as raised:
        read_all(path, currency)
    return str(raised.value)


def test_ac21_the_sgml_sample_is_two_transactions_fingerprinted_by_fitid() -> None:
    assert read_all(SAMPLES / "savings.ofx") == SAVINGS


def test_ac22_the_same_statement_as_xml_is_the_same_two_transactions(tmp_path: Path) -> None:
    assert read_all(ofx_file(tmp_path, XML_SAVINGS)) == SAVINGS
    with_bom = ofx_file(tmp_path, b"\xef\xbb\xbf" + XML_SAVINGS.encode(), name="bom.ofx")
    assert read_all(with_bom) == SAVINGS
    bare = XML_SAVINGS.split("?>\r\n", 2)[2].replace("\r\n", "\n")
    assert bare.startswith("<OFX>")
    assert read_all(ofx_file(tmp_path, bare, name="bare.ofx")) == SAVINGS


def test_ac25_a_broken_transaction_is_reported_by_position_and_the_rest_read(
    tmp_path: Path,
) -> None:
    path = ofx_file(
        tmp_path,
        SGML_HEADER
        + statement(
            GOOD,
            "<TRNTYPE>DEBIT<DTPOSTED>20261002<TRNAMT>12,34<FITID>F2<NAME>SECOND",
            "<TRNTYPE>DEBIT<TRNAMT>-3.00<FITID>F3<NAME>THIRD",
        ),
    )
    assert read_all(path) == [
        Transaction(1, "2026-10-01", "GOOD", -100, "ofx:F1"),
        Unreadable(2, "amount is not a number"),
        Unreadable(3, "no DTPOSTED"),
    ]


@pytest.mark.parametrize(
    ("leaves", "outcome"),
    [
        ("<DTPOSTED>20261001<TRNAMT>1.00<NAME>X", Unreadable(1, "no FITID")),
        ("<FITID>A<TRNAMT>1.00<NAME>X", Unreadable(1, "no DTPOSTED")),
        ("<FITID>A<DTPOSTED>20261001<NAME>X", Unreadable(1, "no TRNAMT")),
        ("<FITID>A<DTPOSTED>2026111<TRNAMT>1.00", Unreadable(1, "DTPOSTED is not a date")),
        ("<FITID>A<DTPOSTED>20260231<TRNAMT>1.00", Unreadable(1, "DTPOSTED is not a date")),
        ("<FITID>A<DTPOSTED>00001001<TRNAMT>1.00", Unreadable(1, "DTPOSTED is not a date")),
        ("<FITID>A<DTPOSTED>2026-10-01<TRNAMT>1.00", Unreadable(1, "DTPOSTED is not a date")),
        ("<FITID>A<DTPOSTED>٢٠٢٦١٠٠١<TRNAMT>1", Unreadable(1, "DTPOSTED is not a date")),
        (
            "<FITID>A<DTPOSTED>20261001<TRNAMT>12.345",
            Unreadable(1, "amount has more than 2 decimal places"),
        ),
        (
            "<FITID>A<DTPOSTED>20261001<TRNAMT>99999999999999999999",
            Unreadable(1, "amount is too large to store"),
        ),
        (
            "<FITID>  A 1  <DTPOSTED>20261231235959.999[-5:EST]<TRNAMT>+12.5<NAME>  N  ",
            Transaction(1, "2026-12-31", "N", 1250, "ofx:A 1"),
        ),
        (
            "<FITID>A<DTPOSTED>20261001<TRNAMT>-.5<MEMO>ONLY A MEMO",
            Transaction(1, "2026-10-01", "ONLY A MEMO", -50, "ofx:A"),
        ),
        (
            "<FITID>A<DTPOSTED>20261001<TRNAMT>12.<NAME>NAME<MEMO>MEMO",
            Transaction(1, "2026-10-01", "NAME", 1200, "ofx:A"),
        ),
        (
            "<FITID>A<DTPOSTED>20261001<TRNAMT>0",
            Transaction(1, "2026-10-01", "", 0, "ofx:A"),
        ),
        (
            "<FITID>FIRST<FITID>SECOND<DTPOSTED>20261001<TRNAMT>1<NAME>ONE<NAME>TWO",
            Transaction(1, "2026-10-01", "ONE", 100, "ofx:FIRST"),
        ),
    ],
)
def test_ac25_each_reason_a_transaction_cannot_be_read(
    tmp_path: Path, leaves: str, outcome: Transaction | Unreadable
) -> None:
    assert read_all(ofx_file(tmp_path, UTF8_HEADER + statement(leaves))) == [outcome]


@pytest.mark.parametrize(
    "amount",
    [
        "12,34",
        "1,234.56",
        "1,234",
        "(12.50)",
        "£12.50",
        "12.50-",
        "--1",
        "+-1",
        "1e3",
        "NaN",
        "Infinity",
        "١٢",
        "12.3.4",
        "-",
        ".",
        "1 000",
        "0x10",
    ],
)
def test_ac25_an_amount_that_is_not_ofxs_one_number_shape_is_not_a_number(
    tmp_path: Path, amount: str
) -> None:
    leaves = f"<FITID>A<DTPOSTED>20261001<TRNAMT>{amount}"
    path = ofx_file(tmp_path, "OFXHEADER:100\r\nENCODING:UTF-8\r\n\r\n" + statement(leaves))
    assert read_all(path) == [Unreadable(1, "amount is not a number")]


def test_ac41_a_transaction_without_a_fitid_is_unreadable_and_the_rest_read(
    tmp_path: Path,
) -> None:
    no_fitid = "<TRNTYPE>DEBIT<DTPOSTED>20261001<TRNAMT>-1.00<NAME>X"
    path = ofx_file(tmp_path, SGML_HEADER + statement(no_fitid, GOOD, no_fitid))
    assert read_all(path) == [
        Unreadable(1, "no FITID"),
        Transaction(2, "2026-10-01", "GOOD", -100, "ofx:F1"),
        Unreadable(3, "no FITID"),
    ]


def nested(depth: int) -> str:
    """``<OFX>`` and then ``depth - 1`` distinct aggregates, each inside the one before."""
    return "<OFX>" + "".join(f"<T{i}>" for i in range(depth - 1)) + "<LEAF>value"


def test_ac41_ten_thousand_nested_tags_are_refused_and_sixty_four_are_not(
    tmp_path: Path,
) -> None:
    deep = ofx_file(tmp_path, SGML_HEADER + nested(10_000))
    assert refusal(deep) == "not a statement: tags nested deeper than 64"
    assert read_all(ofx_file(tmp_path, SGML_HEADER + nested(64), name="64.ofx")) == []
    over = ofx_file(tmp_path, SGML_HEADER + nested(65), name="65.ofx")
    assert refusal(over) == "not a statement: tags nested deeper than 64"


def test_ac41_repeated_tags_are_siblings_and_cost_bounded_memory(tmp_path: Path) -> None:
    many_leaves = "".join(f"<X{i}>v" for i in range(100_000))
    body = statement(GOOD + many_leaves, *["<FITID>F1"] * 20_000).replace(
        "<OFX>", "<OFX>" + "<A>" * 100_000 + "</A>", 1
    )
    path = ofx_file(tmp_path, SGML_HEADER + body)
    tracemalloc.start()
    try:
        outcomes = read_all(path)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert outcomes[0] == Transaction(1, "2026-10-01", "GOOD", -100, "ofx:F1")
    assert outcomes[1:] == [Unreadable(n, "no DTPOSTED") for n in range(2, 20_002)]
    assert peak < 16 * 1024 * 1024


@pytest.mark.parametrize(
    ("body", "outcomes"),
    [
        (  # an explicit close, a stray close, and the end of the file closing the last
            "<OFX><CURDEF>GBP<STMTTRN>" + GOOD + "</STMTTRN></NOSUCH><STMTTRN><FITID>F2"
            "<DTPOSTED>20261002<TRNAMT>2<NAME>LAST",
            [
                Transaction(1, "2026-10-01", "GOOD", -100, "ofx:F1"),
                Transaction(2, "2026-10-02", "LAST", 200, "ofx:F2"),
            ],
        ),
        (  # PAYEE's NAME and ORIGCURRENCY's inner leaves are not the transaction's own
            "<OFX><STMTTRN><FITID>F1<DTPOSTED>20261001<PAYEE><NAME>PAYEE NAME<ADDR1>1 HIGH ST"
            "</PAYEE><ORIGCURRENCY><CURRATE>1.1<CURSYM>EUR<TRNAMT>9</ORIGCURRENCY>"
            "<TRNAMT>-1<MEMO>THE MEMO"
            "</STMTTRN></OFX>",
            [Transaction(1, "2026-10-01", "THE MEMO", -100, "ofx:F1")],
        ),
        (  # empty elements are empty aggregates, in both forms
            "<OFX><STMTTRN></STMTTRN><STMTTRN><FITID>F1<DTPOSTED>20261001<TRNAMT>1"
            "<NAME></NAME><MEMO/></STMTTRN></OFX>",
            [Unreadable(1, "no FITID"), Transaction(2, "2026-10-01", "", 100, "ofx:F1")],
        ),
        (  # comments are skipped, whatever they hold, and a value runs on across one
            "<OFX><!-- <STMTTRN><FITID>HIDDEN<DTPOSTED>20261001<TRNAMT>1 -->"
            "<STMTTRN><FITID>F1<DTPOSTED>20261001<TRNAMT>-1<!---->000.00"
            "<NAME>A<!-- > -->B</STMTTRN></OFX>",
            [Transaction(1, "2026-10-01", "AB", -100000, "ofx:F1")],
        ),
        (  # tag names in any case, as SGML reads them
            "<ofx><Stmttrn><fitid>F1<DtPosted>20261001<trnamt>1<name>LOWER</stmttrn></OFX>",
            [Transaction(1, "2026-10-01", "LOWER", 100, "ofx:F1")],
        ),
        (  # CURRENCY says the amounts are in CURSYM; ORIGCURRENCY's are the account's already
            "<OFX><CURDEF>GBP<STMTTRN><FITID>F1<DTPOSTED>20261001<TRNAMT>-1"
            "<CURRENCY><CURRATE>0.8<CURSYM>USD</CURRENCY></STMTTRN>"
            "<STMTTRN><FITID>F2<DTPOSTED>20261001<TRNAMT>-2"
            "<ORIGCURRENCY><CURRATE>0.8<CURSYM>USD</ORIGCURRENCY></STMTTRN>"
            "<STMTTRN><FITID>F3<DTPOSTED>20261001<TRNAMT>-3"
            "<CURRENCY><CURRATE>1<CURSYM>gbp</CURRENCY></STMTTRN>"
            "<STMTTRN><FITID>F4<DTPOSTED>20261001<TRNAMT>-4"
            "<CURRENCY><CURSYM>US$</CURRENCY></STMTTRN></OFX>",
            [
                Unreadable(1, "the transaction is in another currency"),
                Transaction(2, "2026-10-01", "", -200, "ofx:F2"),
                Transaction(3, "2026-10-01", "", -300, "ofx:F3"),
                Unreadable(4, "the transaction is in another currency"),
            ],
        ),
        (  # no CURDEF: the account's currency, as for a CSV
            "<OFX><STMTTRN><FITID>F1<DTPOSTED>20261001<TRNAMT>1</OFX>",
            [Transaction(1, "2026-10-01", "", 100, "ofx:F1")],
        ),
        (  # a credit card statement, and a CURDEF in lower case
            "<OFX><CREDITCARDMSGSRSV1><CCSTMTTRNRS><CCSTMTRS><CURDEF>gbp<BANKTRANLIST>"
            "<STMTTRN><FITID>F1<DTPOSTED>20261001<TRNAMT>1</CCSTMTRS></OFX>",
            [Transaction(1, "2026-10-01", "", 100, "ofx:F1")],
        ),
        ("<OFX></OFX>", []),
    ],
)
def test_the_structure_is_read_in_sgmls_terms(
    tmp_path: Path, body: str, outcomes: list[Transaction | Unreadable]
) -> None:
    assert read_all(ofx_file(tmp_path, SGML_HEADER + body)) == outcomes
    xml = ofx_file(tmp_path, XML_HEADER + body, name="statement.xml")
    assert read_all(xml) == outcomes


def test_processing_instructions_are_skipped_in_xml_and_refused_in_sgml(tmp_path: Path) -> None:
    body = "<OFX><?PI <STMTTRN> ?><STMTTRN><FITID>F1<DTPOSTED>20261001<TRNAMT>1<?x?>0</OFX>"
    assert read_all(ofx_file(tmp_path, XML_HEADER + body)) == [
        Transaction(1, "2026-10-01", "", 1000, "ofx:F1")
    ]
    # SGML ends a processing instruction at the first ">", so "?>" would hide a CURDEF.
    sgml = ofx_file(tmp_path, SGML_HEADER + "<?pi > <CURDEF>USD ?>" + statement(GOOD), "s.ofx")
    assert refusal(sgml) == "not a statement: a processing instruction, which SGML never holds"


def test_entities_are_decoded_and_references_to_what_cannot_be_stored_stay_literal(
    tmp_path: Path,
) -> None:
    names = [
        "M&amp;S &lt;LEEDS&gt; &quot;Q&quot; &apos;A&apos; NO&nbsp;BREAK &#38; &#x26; &#X26;",
        "&#0; &#xD800; &#xdfff; &#x110000; &#99999999; &copy; &ampfoo &amp &#١٢; A&B",
        "&#65;&#x42;&#x0043;",
    ]
    path = ofx_file(
        tmp_path,
        UTF8_HEADER
        + statement(
            *[f"<FITID>F{n}<DTPOSTED>20261001<TRNAMT>1<NAME>{name}" for n, name in enumerate(names)]
        ),
    )
    descriptions = [o.description for o in read_all(path) if isinstance(o, Transaction)]
    assert descriptions == [
        "M&S <LEEDS> \"Q\" 'A' NO\xa0BREAK & & &",
        names[1],
        "ABC",
    ]


@pytest.mark.parametrize(
    ("header", "name", "description"),
    [
        (SGML_HEADER, b"CAF\xc9 \x80", "CAFÉ €"),
        (SGML_HEADER.replace("1252", "NONE"), b"\x80", "€"),
        (SGML_HEADER.replace("CHARSET:1252\r\n", ""), b"\x80", "€"),
        (SGML_HEADER.replace("1252", "ISO-8859-1"), b"\xa3\x80", "£\x80"),
        (SGML_HEADER.replace("1252", "8859-1"), b"\xa3", "£"),
        (SGML_HEADER.replace("USASCII", "UNICODE"), "CAFÉ €".encode(), "CAFÉ €"),
        ('<?xml version="1.0" encoding="us-ascii"?>', b"CAFE", "CAFE"),
        (SGML_HEADER.replace("USASCII", "UTF-8"), "CAFÉ €".encode(), "CAFÉ €"),
        (XML_HEADER, "CAFÉ €".encode(), "CAFÉ €"),
        ('<?xml version="1.0"?>', "CAFÉ €".encode(), "CAFÉ €"),
        ("<?xml version='1.0' encoding='ISO-8859-1'?>", b"\xa3", "£"),
        ('<?xml version="1.0" encoding="windows-1252"?>', b"\x80", "€"),
    ],
)
def test_the_encoding_is_the_headers_once(
    tmp_path: Path, header: str, name: bytes, description: str
) -> None:
    body = statement("<FITID>F1<DTPOSTED>20261001<TRNAMT>1<NAME>@").encode().split(b"@")
    path = ofx_file(tmp_path, header.encode() + body[0] + name + body[1])
    (transaction,) = read_all(path)
    assert isinstance(transaction, Transaction)
    assert transaction.description == description


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (
            SGML_HEADER.replace("1252", "1251").encode() + statement(GOOD).encode(),
            "not a text file: CHARSET is not 1252, ISO-8859-1 or NONE",
        ),
        (
            SGML_HEADER.encode() + statement(GOOD + "\x81").encode("latin-1"),
            "not a text file: not valid cp1252",
        ),
        (
            SGML_HEADER.replace("USASCII", "UTF-8").encode()
            + statement(GOOD + "\xff").encode("latin-1"),
            "not a text file: not valid utf-8",
        ),
        (
            XML_HEADER.encode() + statement(GOOD + "\xff").encode("latin-1"),
            "not a text file: not valid utf-8",
        ),
        (
            b'<?xml version="1.0" encoding="ascii"?>' + statement(GOOD + "\xa3").encode("latin-1"),
            "not a text file: not valid ascii",
        ),
        (
            b'<?xml version="1.0" encoding="no-such-codec"?>' + statement(GOOD).encode(),
            "not a text file: the declared encoding is unknown",
        ),
        (
            SGML_HEADER.encode() + statement(*[GOOD] * 100, GOOD + "\0").encode(),
            "not a text file",
        ),
    ],
)
def test_a_file_that_is_not_text_in_its_declared_encoding_is_refused_whole(
    tmp_path: Path, content: bytes, reason: str
) -> None:
    assert refusal(ofx_file(tmp_path, content)) == reason


@pytest.mark.parametrize(
    ("declared", "body"),
    [
        ("UTF-7", "<NAME>+2AA-"),  # a lone surrogate, which SQLite cannot store
        ("unicode_escape", "<NAME>\\ud800"),
        ("raw_unicode_escape", "<NAME>\\u0000"),
        ("idna", "<NAME>X"),
        ("iso2022_jp", "<NAME>X"),
        ("hz", "<NAME>X"),
        ("base64", "<NAME>X"),
        ("utf-16", "<NAME>X"),
        ("cp500", "<NAME>X"),
        ("cp1250", "<NAME>X"),
    ],
)
def test_only_the_encodings_ofx_uses_may_be_declared(
    tmp_path: Path, declared: str, body: str
) -> None:
    content = f'<?xml version="1.0" encoding="{declared}"?>' + statement(GOOD + body)
    canonical = codecs.lookup(declared).name
    assert refusal(ofx_file(tmp_path, content)) == (
        f'not a text file: encoding "{canonical}" is not supported'
    )


DOCTYPE = "the file declares a DOCTYPE, which statements never do"
BILLION_LAUGHS = (
    '<?xml version="1.0"?>\n<!DOCTYPE OFX [\n <!ENTITY lol "lol">\n'
    + "".join(
        f' <!ENTITY lol{i} "{f"&lol{i - 1};" * 10 if i > 1 else "&lol;" * 10}">\n'
        for i in range(1, 10)
    )
    + "]>\n<OFX><STMTTRN><FITID>F1<DTPOSTED>20261001<TRNAMT>1<NAME>&lol9;</OFX>\n"
)


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (BILLION_LAUGHS, DOCTYPE),
        ('<?xml version="1.0"?><!DOCTYPE OFX SYSTEM "file:///etc/passwd"><OFX></OFX>', DOCTYPE),
        ("<OFX><!-- " + "x" * 5000 + " --><!doctype ofx><CURDEF>GBP</OFX>", DOCTYPE),
        (SGML_HEADER + "<!DOCTYPE OFX>" + statement(GOOD), DOCTYPE),
        (
            SGML_HEADER + statement(GOOD) + "<!DOCTYPE OFX>",
            DOCTYPE,
        ),
        (
            '<?xml version="1.0"?><OFX><!ENTITY x "y"></OFX>',
            "not a statement: a <! declaration, which statements never hold",
        ),
        (
            '<?xml version="1.0"?><OFX><NAME><![CDATA[x]]></NAME></OFX>',
            "not a statement: a <! declaration, which statements never hold",
        ),
        ('<?xml version="1.0"?><svg></svg>', "not a statement: the first tag is not <OFX>"),
        (SGML_HEADER + "<HTML><BODY>", "not a statement: the first tag is not <OFX>"),
        (SGML_HEADER + "no tags at all", "not a statement: the first tag is not <OFX>"),
        (SGML_HEADER + "<OFX><STMTTRN><FITID", "not a statement: the file ends inside a tag"),
        (SGML_HEADER + "<OFX><!-- never closed >", "not a statement: the file ends inside a tag"),
        (XML_HEADER + "<OFX><?never closed >", "not a statement: the file ends inside a tag"),
        (SGML_HEADER + statement(GOOD) + "<STMTTRN>" + GOOD, "not a statement: tags after </OFX>"),
        (XML_HEADER + "<OFX></OFX><OFX></OFX>", "not a statement: tags after </OFX>"),
        (SGML_HEADER + "<OFX><CURDEF>GBP<OFX><STMTTRN>", "not a statement: tags after </OFX>"),
        (
            SGML_HEADER + "<OFX><STMTRS><CURDEF>GBP</STMTRS><STMTRS><CURDEF>GBP</STMTRS></OFX>",
            "the file holds more than one statement",
        ),
        (
            SGML_HEADER + "<OFX><STMTRS></STMTRS><CCSTMTRS></CCSTMTRS></OFX>",
            "the file holds more than one statement",
        ),
        (
            SGML_HEADER + "<OFX><STMTRS></STMTRS><INVSTMTRS><INVTRANLIST><INVBANKTRAN>"
            "<STMTTRN>" + GOOD + "</INVSTMTRS></OFX>",
            "the file holds more than one statement",
        ),
        (
            SGML_HEADER + "<OFX><LOANSTMTRS></LOANSTMTRS><stmtrs></stmtrs></OFX>",
            "the file holds more than one statement",
        ),
        (
            SGML_HEADER + statement(GOOD, currency="GBPX"),
            "not a statement: CURDEF is not a currency code",
        ),
        (
            SGML_HEADER + statement(GOOD, currency="12£"),
            "not a statement: CURDEF is not a currency code",
        ),
    ],
)
def test_ac39_a_file_that_is_not_an_ofx_statement_is_refused_whole(
    tmp_path: Path, content: str, reason: str
) -> None:
    assert refusal(ofx_file(tmp_path, content.encode("utf-8"))) == reason


def test_ac24_a_currency_that_is_not_the_accounts_is_refused_before_any_row(
    tmp_path: Path,
) -> None:
    outcomes = read(SAMPLES / "savings.ofx", "EUR", 2)
    with pytest.raises(NotAStatementError, match=r"^the file is in GBP but the account is in EUR$"):
        next(outcomes)
    late = ofx_file(tmp_path, SGML_HEADER + "<OFX><STMTTRN>" + GOOD + "<CURDEF>USD</OFX>")
    assert refusal(late) == "the file is in USD but the account is in GBP"
    lower = ofx_file(tmp_path, SGML_HEADER + "<ofx><curdef>usd</ofx>", name="lower.ofx")
    assert refusal(lower) == "the file is in USD but the account is in GBP"


def test_a_value_or_tag_longer_than_one_megabyte_is_refused_and_one_megabyte_is_not(
    tmp_path: Path,
) -> None:
    def with_name(length: int) -> Path:
        leaves = "<FITID>F1<DTPOSTED>20261001<TRNAMT>1<NAME>" + "n" * length
        return ofx_file(tmp_path, SGML_HEADER + statement(leaves).replace("\r\n</BANK", "</BANK"))

    (exact,) = read_all(with_name(MAX_PIECE))
    assert isinstance(exact, Transaction)
    assert len(exact.description) == MAX_PIECE
    too_long = "not a statement: a value or tag longer than 1 MB"
    assert refusal(with_name(MAX_PIECE + 1)) == too_long
    tail = ofx_file(tmp_path, SGML_HEADER + "<OFX>" + "t" * 3_000_000, name="tail.ofx")
    assert refusal(tail) == too_long
    tag = ofx_file(tmp_path, SGML_HEADER + "<OFX><" + "T" * 3_000_000 + ">", name="tag.ofx")
    assert refusal(tag) == too_long
    comment = ofx_file(tmp_path, SGML_HEADER + "<OFX><!--" + "c" * 3_000_000, name="c.ofx")
    assert refusal(comment) == too_long
    split = "<OFX><STMTTRN><NAME>" + ("n" * 600_000 + "<!---->") * 2 + "</OFX>"
    assert refusal(ofx_file(tmp_path, SGML_HEADER + split, name="split.ofx")) == too_long


def test_a_million_digit_amount_is_refused_without_the_cost_of_converting_it(
    tmp_path: Path,
) -> None:
    leaves = "<FITID>A<DTPOSTED>20261001<TRNAMT>" + "9" * 1_000_000
    path = ofx_file(tmp_path, SGML_HEADER + statement(leaves).replace("\r\n</BANK", "</BANK"))
    started = time.monotonic()
    assert read_all(path) == [Unreadable(1, "amount is too large to store")]
    assert time.monotonic() - started < 2  # converting it to an int takes about 20 s


def test_a_file_that_vanished_between_the_first_bytes_and_the_read_is_refused(
    tmp_path: Path,
) -> None:
    assert refusal(tmp_path / "gone.ofx").startswith("cannot read the statement file: No such file")


# What the slice 3 mutation run found untested: each test below names the behaviour it pins.


def padded_to_chunk(prefix: str, before: str) -> str:
    """``prefix``, a comment and ``before``, exactly one 64 KB chunk long (ASCII)."""
    pad = 65_536 - len(prefix) - len(before) - len("<!---->")
    return prefix + "<!--" + "p" * pad + "-->" + before


def test_a_file_is_decoded_to_its_last_byte_in_its_named_encoding(tmp_path: Path) -> None:
    truncated = (UTF8_HEADER + statement(GOOD)).encode() + b"\xc3"
    assert refusal(ofx_file(tmp_path, truncated)) == "not a text file: not valid utf-8"
    bom = b"\xef\xbb\xbf" + XML_SAVINGS.encode() + b"\xff"
    assert refusal(ofx_file(tmp_path, bom, "bom.ofx")) == "not a text file: not valid utf-8"
    bare = b"<OFX>" + statement(GOOD).encode() + b"\xff"
    assert refusal(ofx_file(tmp_path, bare, "bare.ofx")) == "not a text file: not valid utf-8"
    odd = SGML_HEADER.replace("1252", "\xe91252").encode("latin-1") + statement(GOOD).encode()
    assert refusal(ofx_file(tmp_path, odd, "odd.ofx")) == (
        "not a text file: CHARSET is not 1252, ISO-8859-1 or NONE"
    )
    colon = SGML_HEADER.replace("1252", "1252:X") + statement(GOOD)
    assert refusal(ofx_file(tmp_path, colon, "colon.ofx")) == (
        "not a text file: CHARSET is not 1252, ISO-8859-1 or NONE"
    )


def test_the_header_is_only_what_comes_before_the_first_tag(tmp_path: Path) -> None:
    leaves = "<FITID>F1<DTPOSTED>20261001<TRNAMT>1<NAME>€\r\nENCODING:UTF-8\r\n"  # € is 0x80
    path = ofx_file(tmp_path, (SGML_HEADER + statement(leaves)).encode("cp1252"))
    (transaction,) = read_all(path)
    assert isinstance(transaction, Transaction)
    assert transaction.description == "€\r\nENCODING:UTF-8"
    spaced = b"\r\n  <?xml version='1.0' encoding='ISO-8859-1'?>" + statement(
        "<FITID>F1<DTPOSTED>20261001<TRNAMT>1<NAME>\xa3"
    ).encode("latin-1")
    (latin,) = read_all(ofx_file(tmp_path, spaced, "x.ofx"))
    assert isinstance(latin, Transaction)
    assert latin.description == "£"


def test_a_character_split_across_two_chunks_is_read_whole(tmp_path: Path) -> None:
    prefix = (UTF8_HEADER + "<OFX><STMTTRN><FITID>F1<DTPOSTED>20261001<TRNAMT>1<NAME>").encode()
    content = prefix + b"n" * (65_536 - 1 - len(prefix)) + "€</OFX>".encode()
    assert content[65_535:65_538] == "€".encode()  # the euro's three bytes straddle the chunk
    (transaction,) = read_all(ofx_file(tmp_path, content))
    assert isinstance(transaction, Transaction)
    assert transaction.description.endswith("n€")


def test_a_large_file_is_read_in_chunks(tmp_path: Path) -> None:
    path = ofx_file(tmp_path, SGML_HEADER + "<OFX>" + ("<A>" + " " * 1000) * 20_000 + "</OFX>")
    assert path.stat().st_size > 20_000_000
    tracemalloc.start()
    try:
        assert read_all(path) == []
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024


def test_markup_split_across_chunks_is_read_whole(tmp_path: Path) -> None:
    prefix = SGML_HEADER + "<OFX><NAME>"
    doctype = prefix + "q" * (65_536 - 5 - len(prefix)) + "<!DOCTYPE OFX>" + statement(GOOD)
    assert refusal(ofx_file(tmp_path, doctype)) == DOCTYPE
    no_newline = ofx_file(tmp_path, XML_SAVINGS.rstrip(), "end.ofx")
    assert no_newline.read_bytes().endswith(b"</OFX>")
    assert read_all(no_newline) == SAVINGS


def test_a_tag_or_value_of_exactly_one_megabyte_on_a_chunk_boundary_is_read(
    tmp_path: Path,
) -> None:
    tag = padded_to_chunk(SGML_HEADER + "<OFX>", "")
    exact_tag = ofx_file(tmp_path, tag + "<" + "T" * (MAX_PIECE - 2) + "></OFX>")
    assert read_all(exact_tag) == []
    over_tag = ofx_file(tmp_path, tag + "<" + "T" * MAX_PIECE + "></OFX>", "over.ofx")
    assert refusal(over_tag) == "not a statement: a value or tag longer than 1 MB"
    value = padded_to_chunk(
        SGML_HEADER + "<OFX><STMTTRN><FITID>F1<DTPOSTED>20261001<TRNAMT>1", "<NAME>"
    )
    (exact,) = read_all(ofx_file(tmp_path, value + "n" * MAX_PIECE + "</OFX>", "value.ofx"))
    assert isinstance(exact, Transaction)
    assert len(exact.description) == MAX_PIECE


@pytest.mark.parametrize(
    ("body", "outcomes"),
    [
        ("<OFX>NOT A STATEMENT", []),
        ("<OFX><CURDEF>GBP", []),
        (
            statement(GOOD) + "<NAME>a trailing leaf",
            [GOOD_READ],
        ),
        ("<OFX><STMTTRN/></OFX>", [Unreadable(1, "no FITID")]),
        (
            "<OFX><STMTTRN><FITID>F1<DTPOSTED>20261001<TRNAMT>1<NAME/>STRAY<MEMO>M</OFX>",
            [Transaction(1, "2026-10-01", "M", 100, "ofx:F1")],
        ),
        (
            "<OFX><STMTTRN><FITID>F1<DTPOSTED>20261001<TRNAMT>1<NAME>X--><!-- c --></OFX>",
            [Transaction(1, "2026-10-01", "X-->", 100, "ofx:F1")],
        ),
        ('<OFX a="1" b="2"><STMTTRN>' + GOOD + "</OFX>", [GOOD_READ]),
    ],
)
def test_edges_of_the_structure(
    tmp_path: Path, body: str, outcomes: list[Transaction | Unreadable]
) -> None:
    assert read_all(ofx_file(tmp_path, XML_HEADER + body)) == outcomes


def test_sixty_four_deep_is_allowed_after_an_xml_prolog_too(tmp_path: Path) -> None:
    assert read_all(ofx_file(tmp_path, XML_HEADER + nested(64))) == []


@pytest.mark.parametrize("tag", ["<>", "</>", "< >", "<//>"])
def test_a_tag_with_no_name_is_refused(tmp_path: Path, tag: str) -> None:
    """SGML reads ``<>`` as "the last element again" and XML as an error: Tally guesses neither."""
    path = ofx_file(tmp_path, SGML_HEADER + "<OFX>" + tag + "<STMTTRN>" + GOOD + "</OFX>")
    assert refusal(path) == "not a statement: a tag with no name"


def test_references_to_every_storable_code_point_are_decoded(tmp_path: Path) -> None:
    leaves = "<FITID>F1<DTPOSTED>20261001<TRNAMT>1<NAME>&#xE000;&#x10FFFF;&#xD7FF;"
    (transaction,) = read_all(ofx_file(tmp_path, SGML_HEADER + statement(leaves)))
    assert isinstance(transaction, Transaction)
    assert transaction.description == "\U0010ffff퟿"
