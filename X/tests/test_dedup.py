from __future__ import annotations

from pathlib import Path

from tally.config import parse_config
from tally.importers import ParsedRow
from tally.importers.dedup import fingerprint, normalise_description, with_fingerprints
from tally.importing import import_file
from tally.store import Store

CONFIG = parse_config(
    '[layouts.l]\ndate = "Date"\ndescription = "Description"\namount = "Amount"\n'
    'date_format = "%Y-%m-%d"\n'
)


def test_fingerprint_ignores_whitespace_and_case_but_not_occurrence():
    a = ParsedRow(1, "2026-10-01", -300, "Costa  Coffee")
    b = ParsedRow(9, "2026-10-01", -300, "costa coffee ")
    assert fingerprint(a, 1) == fingerprint(b, 1)
    assert fingerprint(a, 1) != fingerprint(a, 2)
    assert normalise_description("  A   b\tc ") == "a b c"


def test_external_id_wins():
    a = ParsedRow(1, "2026-10-01", -300, "X", external_id="F1")
    b = ParsedRow(2, "2026-10-02", -999, "Y", external_id="F1 ")
    assert fingerprint(a, 1) == fingerprint(b, 1) == "id:F1"


def test_identical_rows_in_one_file_get_distinct_fingerprints():
    rows = [ParsedRow(i, "2026-10-01", -300, "COFFEE") for i in range(3)]
    fps = [t.fingerprint for t in with_fingerprints(rows)]
    assert len(set(fps)) == 3


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "t.db")
    store.add_account("a", "GBP", 2)
    return store


def _write(path: Path, body: str) -> Path:
    path.write_text("Date,Description,Amount\n" + body)
    return path


def test_same_file_twice(tmp_path):
    store = _store(tmp_path)
    f = _write(tmp_path / "a.csv", "2026-10-01,COFFEE,-3.00\n2026-10-01,COFFEE,-3.00\n")
    r1 = import_file(store, CONFIG, f, "a", layout_name="l")
    r2 = import_file(store, CONFIG, f, "a", layout_name="l")
    assert (r1.added, r1.duplicates) == (2, 0)
    assert (r2.added, r2.duplicates) == (0, 2)
    assert r2.previous is not None and r1.previous is None
    assert len(list(store.transactions())) == 2


def test_overlapping_statements(tmp_path):
    store = _store(tmp_path)
    sept = _write(
        tmp_path / "sept.csv", "2026-09-28,A,-1.00\n2026-09-30,B,-2.00\n2026-10-01,COFFEE,-3.00\n"
    )
    octo = _write(
        tmp_path / "oct.csv",
        "2026-09-30,B,-2.00\n2026-10-01,COFFEE,-3.00\n2026-10-01,COFFEE,-3.00\n"
        "2026-10-02,C,-4.00\n",
    )
    import_file(store, CONFIG, sept, "a", layout_name="l")
    r = import_file(store, CONFIG, octo, "a", layout_name="l")
    # B and the first COFFEE were already there; the second COFFEE is new.
    assert (r.added, r.duplicates) == (2, 2)
    amounts = sorted(t.amount_minor for t in store.transactions())
    assert amounts == [-400, -300, -300, -200, -100]


def test_same_transaction_in_two_accounts_is_not_a_duplicate(tmp_path):
    store = _store(tmp_path)
    store.add_account("b", "GBP", 2)
    f = _write(tmp_path / "a.csv", "2026-10-01,COFFEE,-3.00\n")
    import_file(store, CONFIG, f, "a", layout_name="l")
    r = import_file(store, CONFIG, f, "b", layout_name="l")
    assert r.added == 1


def test_ofx_fitid_dedup_even_when_text_differs(tmp_path, samples):
    store = _store(tmp_path)
    import_file(store, CONFIG, samples / "savings.ofx", "a")
    changed = tmp_path / "s2.ofx"
    changed.write_text(
        (samples / "savings.ofx").read_text().replace("<NAME>INTEREST", "<NAME>INTEREST PAID")
    )
    r = import_file(store, CONFIG, changed, "a")
    assert (r.added, r.duplicates) == (0, 2)
    assert r.previous is None  # a different file, same transactions


def test_undo_removes_only_what_that_import_added(tmp_path):
    store = _store(tmp_path)
    sept = _write(tmp_path / "sept.csv", "2026-09-30,B,-2.00\n2026-10-01,COFFEE,-3.00\n")
    octo = _write(tmp_path / "oct.csv", "2026-10-01,COFFEE,-3.00\n2026-10-02,C,-4.00\n")
    import_file(store, CONFIG, sept, "a", layout_name="l")
    import_file(store, CONFIG, octo, "a", layout_name="l")
    records = store.imports("a")
    assert [(r.added, r.duplicates) for r in records] == [(2, 0), (1, 1)]
    # Undo October: COFFEE stays, because September added it.
    assert store.undo_import(records[1].id) == 1
    assert sorted(t.description for t in store.transactions()) == ["B", "COFFEE"]
    assert [r.id for r in store.imports()] == [records[0].id]


def test_undo_of_an_earlier_import_removes_rows_later_imports_relied_on(tmp_path):
    store = _store(tmp_path)
    sept = _write(tmp_path / "sept.csv", "2026-09-30,B,-2.00\n2026-10-01,COFFEE,-3.00\n")
    octo = _write(tmp_path / "oct.csv", "2026-10-01,COFFEE,-3.00\n2026-10-02,C,-4.00\n")
    import_file(store, CONFIG, sept, "a", layout_name="l")
    import_file(store, CONFIG, octo, "a", layout_name="l")
    first = store.imports("a")[0]
    assert store.undo_import(first.id) == 2
    # COFFEE went with September even though October's statement had it;
    # importing October again brings it back as a new row.
    assert sorted(t.description for t in store.transactions()) == ["C"]
    r = import_file(store, CONFIG, octo, "a", layout_name="l")
    assert (r.added, r.duplicates) == (1, 1)


def test_undo_unknown_import_is_an_error_even_inside_the_write(tmp_path):
    import pytest

    from tally.errors import TallyError

    store = _store(tmp_path)
    with pytest.raises(TallyError, match="no import with id 42"):
        store.undo_import(42)
