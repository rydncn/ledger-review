import pytest

from tally.errors import ImportError_
from tally.importers import Note, ParsedRow, RowProblem
from tally.importers.ofx import parse_ofx_text, read_ofx


def rows(items):
    return [i for i in items if isinstance(i, ParsedRow)]


def test_sgml_sample(samples):
    items = list(read_ofx(samples / "savings.ofx", 2))
    got = [
        (r.line, r.date, r.amount_minor, r.description, r.external_id, r.currency)
        for r in rows(items)
    ]
    assert got == [
        (11, "2026-10-01", 1234, "INTEREST", "2026100101", "GBP"),
        (12, "2026-10-15", -50000, "TRANSFER TO CURRENT", "2026101501", "GBP"),
    ]
    assert not [i for i in items if isinstance(i, RowProblem)]


XML = """<?xml version="1.0" encoding="UTF-8"?>
<?OFX OFXHEADER="200" VERSION="211" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>
<OFX>
  <BANKMSGSRSV1><STMTTRNRS><STMTRS>
    <CURDEF>EUR</CURDEF>
    <BANKACCTFROM><ACCTID>123</ACCTID></BANKACCTFROM>
    <BANKTRANLIST>
      <STMTTRN>
        <TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20261003120000.000[+1:CET]</DTPOSTED>
        <TRNAMT>-23,99</TRNAMT><FITID>A1</FITID>
        <NAME>Caf&amp;eacute; &amp; Co</NAME><MEMO>Card 1234</MEMO>
      </STMTTRN>
      <STMTTRN>
        <TRNTYPE>CREDIT</TRNTYPE><DTPOSTED>20261004</DTPOSTED>
        <TRNAMT>150.00</TRNAMT><FITID>A2</FITID><MEMO>Only a memo</MEMO>
      </STMTTRN>
    </BANKTRANLIST>
  </STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""


def test_xml_form_with_closed_tags_entities_and_comma_decimal():
    got = rows(parse_ofx_text(XML, 2))
    assert len(got) == 2
    a, b = got
    assert (a.date, a.amount_minor, a.description, a.memo, a.external_id, a.currency) == (
        "2026-10-03",
        -2399,
        "Caf&eacute; & Co",
        "Card 1234",
        "A1",
        "EUR",
    )
    assert (b.description, b.memo, b.amount_minor) == ("Only a memo", None, 15000)


def test_xml_declared_encoding_is_used(tmp_path):
    p = tmp_path / "s.ofx"
    p.write_bytes(XML.replace("UTF-8", "ISO-8859-1").replace("&amp;eacute;", "é").encode("latin-1"))
    assert rows(read_ofx(p, 2))[0].description == "Café & Co"


def test_sgml_charset_1252_header(tmp_path):
    text = (
        "OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nENCODING:USASCII\nCHARSET:1252\n\n"
        "<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>GBP<BANKTRANLIST>\n"
        "<STMTTRN><DTPOSTED>20261001<TRNAMT>1.00<FITID>1<NAME>CAF\xc9 €\n"
        "</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>\n"
    )
    p = tmp_path / "s.ofx"
    p.write_bytes(text.encode("cp1252"))
    assert rows(read_ofx(p, 2))[0].description == "CAFÉ €"


def test_credit_card_statement_and_multiple_statements():
    text = (
        "<OFX><CREDITCARDMSGSRSV1><CCSTMTTRNRS><CCSTMTRS><CURDEF>GBP<BANKTRANLIST>"
        "<STMTTRN><DTPOSTED>20261001<TRNAMT>-1.00<FITID>c1<NAME>ONE"
        "</BANKTRANLIST></CCSTMTRS></CCSTMTTRNRS>"
        "<CCSTMTTRNRS><CCSTMTRS><CURDEF>USD<BANKTRANLIST>"
        "<STMTTRN><DTPOSTED>20261002<TRNAMT>-2.00<FITID>c2<NAME>TWO"
        "</BANKTRANLIST></CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1></OFX>"
    )
    got = rows(parse_ofx_text(text, 2))
    assert [(r.description, r.currency) for r in got] == [("ONE", "GBP"), ("TWO", "USD")]


def test_problems_have_lines_and_no_values():
    text = (
        "<OFX><STMTRS><CURDEF>GBP<BANKTRANLIST>\n"
        "<STMTTRN><DTPOSTED>2026<TRNAMT>1.00<FITID>1<NAME>SECRET A\n"
        "<STMTTRN><DTPOSTED>20261001<TRNAMT>abc<FITID>2<NAME>SECRET B\n"
        "<STMTTRN><DTPOSTED>20261001<TRNAMT>1.00<FITID>3\n"
        "<STMTTRN><DTPOSTED>20261001<TRNAMT>1.00<NAME>NO FITID\n"
        "</BANKTRANLIST></STMTRS></OFX>"
    )
    items = list(parse_ofx_text(text, 2))
    probs = [i for i in items if isinstance(i, RowProblem)]
    assert [(p.line, p.reason.split(" ")[-1] != "") for p in probs] == [
        (2, True),
        (3, True),
        (4, True),
    ]
    assert "DTPOSTED" in probs[0].reason
    assert "TRNAMT" in probs[1].reason
    assert "NAME" in probs[2].reason
    assert all("SECRET" not in p.reason for p in probs)
    ok = rows(items)
    assert len(ok) == 1 and ok[0].external_id is None


def test_not_ofx():
    with pytest.raises(ImportError_, match="<OFX>"):
        list(parse_ofx_text("Date,Description\n01/01/2026,x\n", 2))


def test_no_transactions_is_a_note():
    items = list(
        parse_ofx_text("<OFX><STMTRS><CURDEF>GBP<BANKTRANLIST></BANKTRANLIST></STMTRS></OFX>", 2)
    )
    assert items == [Note(None, "the file contains no transactions")]


def test_lowercase_tags_and_whitespace():
    text = (
        "<ofx>\n<stmtrs>\n<curdef>GBP\n<banktranlist>\n<stmttrn>\n<dtposted>20261001\n"
        "<trnamt>  5.00 \n<fitid>x\n<name>  Spaced   out \n</banktranlist>\n</stmtrs>\n</ofx>"
    )
    got = rows(parse_ofx_text(text, 2))
    assert got[0].description == "Spaced out" and got[0].amount_minor == 500


def test_lowercase_ofx_after_text_whose_uppercase_is_longer():
    text = (
        "Bank: Straße\n<ofx><stmtrs><curdef>EUR<banktranlist>\n"
        "<stmttrn><dtposted>20261001<trnamt>-5.00<fitid>a<name>Straßenbahn\n"
        "</banktranlist></stmtrs></ofx>"
    )
    got = rows(parse_ofx_text(text, 2))
    assert len(got) == 1 and got[0].description == "Straßenbahn" and got[0].line == 3


def test_line_numbers_stay_right_across_many_transactions():
    body = "".join(
        f"<STMTTRN><DTPOSTED>20261001<TRNAMT>-1.00<FITID>{i}<NAME>N{i}\n" for i in range(500)
    )
    text = f"<OFX><STMTRS><CURDEF>GBP<BANKTRANLIST>\n{body}</BANKTRANLIST></STMTRS></OFX>"
    got = rows(parse_ofx_text(text, 2))
    assert [r.line for r in got] == list(range(2, 502))
