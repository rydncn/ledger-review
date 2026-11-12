import io

import pytest

from tally.output import Style, colour_enabled, guard_cell, render_table, write_csv


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("=HYPERLINK(1)", "'=HYPERLINK(1)"),
        ("+1+1", "'+1+1"),
        ("-2+3", "'-2+3"),
        ("@SUM(A1)", "'@SUM(A1)"),
        ("\t=1", "'\t=1"),
        ("\r=1", "'\r=1"),
        ("|calc", "'|calc"),
        ("  =1", "'  =1"),
        ("-42.17", "-42.17"),
        ("2450.00", "2450.00"),
        ("0", "0"),
        ("TESCO", "TESCO"),
        ("", ""),
    ],
)
def test_guard_cell(value, expected):
    assert guard_cell(value) == expected


def test_write_csv_guards_and_quotes():
    buf = io.StringIO()
    write_csv(buf, ["Description", "Amount"], [['=HYPERLINK("x")', "-1.00"], ["A, B", None]])
    assert buf.getvalue() == ('Description,Amount\n"\'=HYPERLINK(""x"")",-1.00\n"A, B",\n')


def test_colour_rules(monkeypatch):
    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert colour_enabled("auto", Tty()) is True
    assert colour_enabled("auto", io.StringIO()) is False
    assert colour_enabled("never", Tty()) is False
    assert colour_enabled("always", io.StringIO()) is True
    monkeypatch.setenv("NO_COLOR", "1")
    assert colour_enabled("auto", Tty()) is False
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("TERM", "dumb")
    assert colour_enabled("auto", Tty()) is False


def test_render_table_alignment_and_no_colour_by_default():
    text = render_table(["Name", "Amount"], [["a", "1.00"], ["bbb", "-10.00"]], align_right=[1])
    assert text == "Name  Amount\n----  ------\na       1.00\nbbb   -10.00\n"
    assert "\x1b" not in text


def test_render_table_with_style_keeps_text_meaningful():
    text = render_table(["N"], [["x"]], style=Style(True), emphasis=[0])
    assert "\x1b[1m" in text and "x" in text
