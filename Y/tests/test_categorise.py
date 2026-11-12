"""AC26 to AC29, AC34, AC35, AC38: ``tally uncategorised`` and ``tally explain``."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tally.cli import _cell, main
from tests import SAMPLES
from tests.test_import import CONFIG, Place, bank_file, console_script

RULES = """
[[rules]]
name = "Groceries"
description_contains = "TESCO"

[[rules]]
name = "Travel"
description_matches = "^TFL"

[[rules]]
name = "Big"
account = "current"
amount_below = "-100.00"

[[rules]]
name = "Card payments"
account = "card"
amount_above = "0"
"""

# Ids follow the import order: the current account's five, the card's three, the savings two.
UNCATEGORISED_TABLE = """\
9  2026-10-01  savings  12.34 GBP  INTEREST
2  2026-10-02  current  2450.00 GBP  SALARY ACME LTD
6  2026-10-02  card  -4.20 EUR  CAFÉ NERO
3  2026-10-03  current  12.50 GBP  =HYPERLINK("http://example.invalid","Refund")
7  2026-10-03  card  -23.99 EUR  AMAZON MARKETPLACE
10  2026-10-15  savings  -500.00 GBP  TRANSFER TO CURRENT
"""
UNCATEGORISED_ROWS = [
    (9, "2026-10-01", "savings", "12.34", "GBP", "INTEREST"),
    (2, "2026-10-02", "current", "2450.00", "GBP", "SALARY ACME LTD"),
    (6, "2026-10-02", "card", "-4.20", "EUR", "CAFÉ NERO"),
    (3, "2026-10-03", "current", "12.50", "GBP", '=HYPERLINK("http://example.invalid","Refund")'),
    (7, "2026-10-03", "card", "-23.99", "EUR", "AMAZON MARKETPLACE"),
    (10, "2026-10-15", "savings", "-500.00", "GBP", "TRANSFER TO CURRENT"),
]
FIELDS = ("id", "date", "account", "amount", "currency", "description")
TFL_EXPLAINED = """\
4  2026-10-03  current  -3.40 GBP  TFL TRAVEL CH
category Travel, from rule 2
rule 1 Groceries did not match: description does not contain TESCO
rule 2 Travel matched: description matches ^TFL
"""
SALARY_EXPLAINED = """\
2  2026-10-02  current  2450.00 GBP  SALARY ACME LTD
no rule matched
rule 1 Groceries did not match: description does not contain TESCO
rule 2 Travel did not match: description does not match ^TFL
rule 3 Big did not match: amount is not below -100.00
rule 4 Card payments did not match: account is not card
"""


def samples(tmp_path: Path, capsys: pytest.CaptureFixture[str], rules: str = RULES) -> Place:
    """A place with AC26's rules and the three samples imported."""
    place = Place(tmp_path, capsys)
    place.config.write_text(CONFIG + rules, encoding="utf-8")
    assert place.import_(SAMPLES / "current-account.csv")[0] == 0
    assert place.import_(SAMPLES / "credit-card.csv", account="card")[0] == 0
    assert place.run("import", "--account", "savings", str(SAMPLES / "savings.ofx"))[0] == 0
    return place


def explained(place: Place, transaction_id: int) -> dict[str, object]:
    code, out, err = place.run("explain", str(transaction_id), "--format", "json")
    assert (code, err) == (0, "")
    result: dict[str, object] = json.loads(out)
    return result


def test_ac27_the_user_can_see_what_no_rule_matched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = samples(tmp_path, capsys)
    assert place.run("uncategorised") == (0, UNCATEGORISED_TABLE, "")


def test_ac27_ac35_uncategorised_as_json_has_the_same_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = samples(tmp_path, capsys)
    code, out, err = place.run("uncategorised", "--format", "json")
    assert (code, err) == (0, "")
    assert json.loads(out) == [dict(zip(FIELDS, row, strict=True)) for row in UNCATEGORISED_ROWS]
    assert '"amount": "2450.00"' in out  # a string with the decimal places, never a number


def test_ac27_ac35_uncategorised_as_csv_has_the_same_rows_with_formulas_escaped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = samples(tmp_path, capsys)
    code, out, err = place.run("uncategorised", "--format", "csv")
    assert (code, err) == (0, "")
    assert out == (
        "id,date,account,amount,currency,description\r\n"
        "9,2026-10-01,savings,12.34,GBP,INTEREST\r\n"
        "2,2026-10-02,current,2450.00,GBP,SALARY ACME LTD\r\n"
        "6,2026-10-02,card,-4.20,EUR,CAFÉ NERO\r\n"
        '3,2026-10-03,current,12.50,GBP,"\'=HYPERLINK(""http://example.invalid"",""Refund"")"\r\n'
        "7,2026-10-03,card,-23.99,EUR,AMAZON MARKETPLACE\r\n"
        "10,2026-10-15,savings,-500.00,GBP,TRANSFER TO CURRENT\r\n"
    )


def test_ac35_every_formula_prefix_is_escaped_and_no_amount_is(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = samples(tmp_path, capsys, rules="")
    file = bank_file(
        tmp_path,
        "06/10/2026,@SUM(A1),1.00,,",
        "06/10/2026,+44 PHONE,2.00,,",
        "06/10/2026,-CASH,3.00,,",
        "06/10/2026,PLAIN,4.00,,",
    )
    assert place.import_(file)[0] == 0
    code, out, _ = place.run("uncategorised", "--format", "csv")
    assert code == 0
    lines = out.splitlines()
    assert lines[1:5] == [
        '1,2026-10-01,current,-42.17,GBP,"TESCO STORES 2045, LEEDS"',
        "9,2026-10-01,savings,12.34,GBP,INTEREST",
        "2,2026-10-02,current,2450.00,GBP,SALARY ACME LTD",
        "6,2026-10-02,card,-4.20,EUR,CAFÉ NERO",
    ]
    assert lines[-5:-1] == [
        "11,2026-10-06,current,-1.00,GBP,'@SUM(A1)",
        "12,2026-10-06,current,-2.00,GBP,'+44 PHONE",
        "13,2026-10-06,current,-3.00,GBP,'-CASH",
        "14,2026-10-06,current,-4.00,GBP,PLAIN",
    ]


def test_ac27_with_no_rules_everything_is_uncategorised_and_with_none_left_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = samples(tmp_path, capsys, rules="")
    code, out, _ = place.run("uncategorised", "--format", "json")
    assert (code, len(json.loads(out))) == (0, 10)
    place.config.write_text(CONFIG + '[[rules]]\nname = "Other"\n', encoding="utf-8")
    assert place.run("uncategorised") == (0, "", "")
    assert place.run("uncategorised", "--format", "json") == (0, "[]\n", "")
    assert place.run("uncategorised", "--format", "csv") == (
        0,
        "id,date,account,amount,currency,description\r\n",
        "",
    )


def test_ac27_editing_the_rules_recategorises_everything(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = samples(tmp_path, capsys)
    extra = '[[rules]]\nname = "Income"\namount_above = "1000"\n'
    place.config.write_text(CONFIG + RULES + extra, encoding="utf-8")
    code, out, _ = place.run("uncategorised")
    assert code == 0
    assert "SALARY" not in out
    assert out.count("\n") == 5


def test_ac28_the_user_can_see_why_a_transaction_got_its_category(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = samples(tmp_path, capsys)
    assert place.run("explain", "4") == (0, TFL_EXPLAINED, "")
    assert place.run("explain", "2") == (0, SALARY_EXPLAINED, "")


def test_ac28_ac35_explain_as_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    place = samples(tmp_path, capsys)
    assert explained(place, 4) == {
        "id": 4,
        "date": "2026-10-03",
        "account": "current",
        "amount": "-3.40",
        "currency": "GBP",
        "description": "TFL TRAVEL CH",
        "category": "Travel",
        "rules": [
            {
                "rule": 1,
                "name": "Groceries",
                "matched": False,
                "conditions": ["description does not contain TESCO"],
            },
            {
                "rule": 2,
                "name": "Travel",
                "matched": True,
                "conditions": ["description matches ^TFL"],
            },
        ],
    }
    salary = explained(place, 2)
    assert salary["category"] is None
    assert [r["conditions"] for r in salary["rules"]] == [  # type: ignore[attr-defined]
        ["description does not contain TESCO"],
        ["description does not match ^TFL"],
        ["amount is not below -100.00"],
        ["account is not card"],
    ]


def test_ac28_a_rule_with_several_conditions_or_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    both = '[[rules]]\nname = "Coffee"\naccount = "card"\ndescription_contains = "nero"\n'
    place = samples(tmp_path, capsys, rules=both + '[[rules]]\nname = "Other"\n')
    assert place.run("explain", "6") == (
        0,
        "6  2026-10-02  card  -4.20 EUR  CAFÉ NERO\n"
        "category Coffee, from rule 1\n"
        "rule 1 Coffee matched: account is card and description contains nero\n",
        "",
    )
    assert place.run("explain", "7") == (
        0,
        "7  2026-10-03  card  -23.99 EUR  AMAZON MARKETPLACE\n"
        "category Other, from rule 2\n"
        "rule 1 Coffee did not match: description does not contain nero\n"
        "rule 2 Other matched: it has no conditions\n",
        "",
    )


def test_ac28_with_no_rules_no_rule_matched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = samples(tmp_path, capsys, rules="")
    assert place.run("explain", "9") == (
        0,
        "9  2026-10-01  savings  12.34 GBP  INTEREST\nno rule matched\n",
        "",
    )
    assert explained(place, 9)["rules"] == []


@pytest.mark.parametrize(
    ("transaction_id", "category"),
    [(1, "Groceries"), (4, "Travel"), (5, "Big"), (8, "Card payments"), (11, "Groceries")],
)
def test_ac26_categories_on_the_samples(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], transaction_id: int, category: str
) -> None:
    place = samples(tmp_path, capsys)
    assert place.import_(bank_file(tmp_path, "06/10/2026,TESCO EXTRA,150.00,,"))[0] == 0
    assert explained(place, transaction_id)["category"] == category


def test_ac26_the_savings_transfer_is_not_big_because_the_rule_names_another_account(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = samples(tmp_path, capsys)
    transfer = explained(place, 10)
    assert (transfer["description"], transfer["amount"], transfer["category"]) == (
        "TRANSFER TO CURRENT",
        "-500.00",
        None,
    )
    assert transfer["rules"][2] == {  # type: ignore[index]
        "rule": 3,
        "name": "Big",
        "matched": False,
        "conditions": ["account is not current"],
    }


def test_ac28_an_unknown_id_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    place = samples(tmp_path, capsys)
    assert place.run("explain", "999") == (1, "", "no transaction 999\n")
    assert place.run("explain", "0") == (1, "", "no transaction 0\n")
    assert place.run("explain", "9223372036854775807") == (
        1,
        "",
        "no transaction 9223372036854775807\n",
    )


@pytest.mark.parametrize(
    "text", ["\u0663", "-1", "1.0", "", " 4", "four", "9223372036854775808", "1" * 20]
)
def test_ac28_an_id_that_is_not_one_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str
) -> None:
    place = samples(tmp_path, capsys)
    with pytest.raises(SystemExit) as exited:
        place.run("explain", text)
    assert exited.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "argument id: not a transaction id" in captured.err


@pytest.mark.parametrize("command", [["uncategorised"], ["explain", "1"]])
@pytest.mark.parametrize(
    ("rules", "message"),
    [
        (
            '[[rules]]\nname = "A"\n[[rules]]\nname = "B"\ndescription_matches = "("\n',
            "rules[2]: description_matches is not a valid pattern:"
            " missing ), unterminated subpattern",
        ),
        (
            '[[rules]]\nname = "A"\namount_below = "ten"\n',
            'rules[1]: amount_below is not an amount like "-100.00"',
        ),
        ('[[rules]]\ndescription_contains = "X"\n', "rules[1]: no name"),
        (
            '[[rules]]\nname = "A"\namount_below = -100.0\n',
            "rules[1]: amount_below must be a string",
        ),
    ],
)
def test_ac29_a_broken_rule_is_a_configuration_error_before_the_data_file_opens(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: list[str], rules: str, message: str
) -> None:
    config = tmp_path / "tally.toml"
    config.write_text(CONFIG + rules, encoding="utf-8")
    data_dir = tmp_path / "data"
    code = main([*command, "--config", str(config), "--data-dir", str(data_dir)])
    captured = capsys.readouterr()
    assert (code, captured.out, captured.err) == (2, "", message + "\n")
    assert not data_dir.exists()


def test_ac29_commands_that_do_not_read_rules_are_not_stopped_by_a_broken_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = samples(tmp_path, capsys, rules='[[rules]]\nname = "A"\namount_below = "ten"\n')
    assert place.run("account", "list")[0] == 0
    assert place.import_(SAMPLES / "current-account.csv")[0] == 0


def test_ac38_control_characters_in_descriptions_never_reach_the_terminal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = samples(tmp_path, capsys, rules="")
    evil = "EVIL\x1b]0;evil\x07 X\x08Y \x9b31m \u202eRTL \u2028NEXT\x7f"
    path = tmp_path / "evil.csv"
    path.write_bytes(
        (f'Date,Description,Money Out,Money In,Balance\r\n06/10/2026,"{evil}",1.00,,\r\n').encode()
    )
    assert place.import_(path)[0] == 0
    shown = "EVIL␛]0;evil␇ X␈Y ?31m ?RTL ?NEXT␡"
    for argv in (["uncategorised"], ["uncategorised", "--format", "csv"], ["explain", "11"]):
        code, out, _ = place.run(*argv)
        assert code == 0
        assert shown in out
        assert not any(c in out for c in "\x1b\x07\x08\x9b\u202e\u2028\x7f")
    code, out, _ = place.run("uncategorised", "--format", "json")
    assert [r["description"] for r in json.loads(out) if r["id"] == 11] == [evil]
    assert "\x1b" not in out
    assert explained(place, 11)["description"] == evil


def test_ac38_a_control_character_in_a_data_directory_error_is_replaced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "afile").write_text("", encoding="utf-8")
    config = tmp_path / "tally.toml"
    config.write_text('data_dir = "afile/\\u001b]0;pwned\\u0007"\n', encoding="utf-8")
    code = main(["uncategorised", "--config", str(config)])
    captured = capsys.readouterr()
    assert (code, captured.out) == (1, "")
    assert "␛]0;pwned␇" in captured.err
    assert "\x1b" not in captured.err
    assert "\x07" not in captured.err


@pytest.mark.parametrize(
    ("text", "cell"),
    [("\tX", "'␉X"), ("\rX", "'␍X"), ("=1", "'=1"), ("X\x1b", "X␛"), ("2026-10-01", "2026-10-01")],
)
def test_ac32_a_csv_cell_is_prefixed_by_what_the_text_starts_with(text: str, cell: str) -> None:
    assert _cell(text) == cell


def test_ac38_a_control_character_in_a_configuration_error_is_replaced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "tally.toml"
    config.write_text('[[rules]]\nname = "A"\n"\\u001b[31m" = "x"\n', encoding="utf-8")
    code = main(["uncategorised", "--config", str(config), "--data-dir", str(tmp_path / "d")])
    captured = capsys.readouterr()
    assert (code, captured.out, captured.err) == (2, "", 'rules[1]: unknown key "␛[31m"\n')


def test_ac38_a_rules_own_text_is_replaced_on_the_terminal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = samples(
        tmp_path, capsys, rules='[[rules]]\nname = "A"\ndescription_contains = "\\u001b"\n'
    )
    assert place.run("explain", "1") == (
        0,
        "1  2026-10-01  current  -42.17 GBP  TESCO STORES 2045, LEEDS\n"
        "no rule matched\n"
        "rule 1 A did not match: description does not contain ␛\n",
        "",
    )
    assert explained(place, 1)["rules"] == [
        {
            "rule": 1,
            "name": "A",
            "matched": False,
            "conditions": ["description does not contain \x1b"],
        }
    ]


@pytest.mark.parametrize(
    "command",
    [
        ["uncategorised"],
        ["explain", "4"],
        ["report", "--month", "2026-10", "--format", "json"],
        ["payees", "--month", "2026-10"],
    ],
)
def test_the_data_directory_comes_from_the_configuration_too(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: list[str]
) -> None:
    place = samples(tmp_path, capsys)
    config = tmp_path / "with-data-dir.toml"
    config.write_text(f'data_dir = "data"\n{CONFIG}{RULES}', encoding="utf-8")
    code = main([*command, "--config", str(config)])
    captured = capsys.readouterr()
    assert (code, captured.err) == (0, "")
    assert any(text in captured.out for text in ("TFL TRAVEL CH", "SALARY", '"Travel"'))
    assert place.data_dir == tmp_path / "data"


def test_ac34_an_empty_data_file_lists_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    place = Place(tmp_path, capsys)
    assert place.run("uncategorised") == (0, "", "")
    assert place.run("explain", "1") == (1, "", "no transaction 1\n")


def test_the_installed_command_lists_explains_and_refuses(
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

    listed = tally("uncategorised")
    assert (listed.returncode, listed.stdout, listed.stderr) == (0, UNCATEGORISED_TABLE, "")
    explained_ = tally("explain", "4")
    assert (explained_.returncode, explained_.stdout, explained_.stderr) == (0, TFL_EXPLAINED, "")
    unknown = tally("explain", "999", "--format", "json")
    assert (unknown.returncode, unknown.stdout, unknown.stderr) == (1, "", "no transaction 999\n")
    place.config.write_text(CONFIG + '[[rules]]\nname = ""\n', encoding="utf-8")
    broken = tally("uncategorised")
    assert (broken.returncode, broken.stdout, broken.stderr) == (
        2,
        "",
        "rules[1]: name must not be empty\n",
    )
