import time

import pytest

from tally.config import parse_config
from tally.importing import import_file
from tally.report import by_category, top_payees
from tally.store import Store


@pytest.mark.slow
def test_large_csv_import_and_report(tmp_path):
    rows = 100_000
    csv = tmp_path / "huge.csv"
    with open(csv, "w", encoding="utf-8") as fh:
        fh.write("Date,Description,Money Out,Money In\n")
        for i in range(rows):
            month = (i % 12) + 1
            fh.write(
                f'{(i % 28) + 1:02d}/{month:02d}/2026,"PAYEE {i % 500}, TOWN",'
                f'"{(i % 2000) + 1:,}.{i % 100:02d}",\n'
            )
    config = parse_config(
        '[layouts.l]\ndate = "Date"\ndescription = "Description"\nmoney_out = "Money Out"\n'
        'money_in = "Money In"\n'
        '[[rules]]\ncategory = "Low"\npattern = "PAYEE [0-9]$"\n'
        '[[rules]]\ncategory = "Mid"\npattern = "PAYEE [0-9][0-9]$"\n'
    )
    with Store(tmp_path / "t.db") as store:
        store.add_account("a", "GBP", 2)
        t0 = time.perf_counter()
        result = import_file(store, config, csv, "a", layout_name="l")
        import_seconds = time.perf_counter() - t0
        assert (result.added, result.duplicates, result.skipped) == (rows, 0, 0)
        t0 = time.perf_counter()
        lines = by_category(store.transactions(), config.rules)
        payees = top_payees(store.transactions(), config.rules, 10)
        report_seconds = time.perf_counter() - t0
        assert sum(ln.count for ln in lines) == rows
        assert len(payees) == 10
    # Generous bounds; the point is that nothing is quadratic.
    assert import_seconds < 60, import_seconds
    assert report_seconds < 60, report_seconds


@pytest.mark.slow
def test_large_ofx_import_is_linear(tmp_path):
    rows = 60_000
    ofx = tmp_path / "huge.ofx"
    with open(ofx, "w", encoding="ascii") as fh:
        fh.write("OFXHEADER:100\nDATA:OFXSGML\n\n<OFX><STMTRS><CURDEF>GBP<BANKTRANLIST>\n")
        for i in range(rows):
            fh.write(
                f"<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>2026{(i % 12) + 1:02d}01"
                f"<TRNAMT>-{i % 300}.{i % 100:02d}"
                f"<FITID>F{i}<NAME>PAYEE {i % 400}<MEMO>Ref {i}\n"
            )
        fh.write("</BANKTRANLIST></STMTRS></OFX>\n")
    config = parse_config("")
    with Store(tmp_path / "t.db") as store:
        store.add_account("a", "GBP", 2)
        t0 = time.perf_counter()
        result = import_file(store, config, ofx, "a")
        seconds = time.perf_counter() - t0
    assert (result.added, result.skipped) == (rows, 0)
    assert seconds < 30, seconds
