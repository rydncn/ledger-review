import pytest

from tally.config import STARTER_CONFIG, Layout, parse_config
from tally.errors import ConfigError


def test_starter_config_parses():
    config = parse_config(STARTER_CONFIG)
    assert set(config.layouts) == {"current-account", "credit-card"}
    assert [r.category for r in config.rules][:2] == ["Groceries", "Transport"]
    assert config.rules[2].amount_min == 100  # 0.01 at RULE_SCALE (4 decimals)


def test_layout_defaults():
    config = parse_config('[layouts.x]\ndate = "D"\ndescription = "N"\namount = "A"\n')
    layout = config.layouts["x"]
    assert layout == Layout(name="x", date="D", description="N", amount="A")
    assert layout.uses_two_columns is False


def test_layout_two_columns_by_index():
    config = parse_config(
        "[layouts.x]\ndate = 0\ndescription = 1\nmoney_out = 2\nmoney_in = 3\nheader = false\n"
    )
    assert config.layouts["x"].uses_two_columns


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ('date = "D"\namount = "A"\n', "description is required"),
        ('date = "D"\ndescription = "N"\n', "either amount, or both"),
        ('date = "D"\ndescription = "N"\namount = "A"\nmoney_in = "I"\n', "not both"),
        (
            'date = "D"\ndescription = "N"\namount = "A"\ndecimal_separator = ",,"\n',
            "one character",
        ),
        (
            'date = "D"\ndescription = "N"\namount = "A"\ndecimal_separator = ","\n'
            'thousands_separator = ","\n',
            "must differ",
        ),
        ('date = "D"\ndescription = "N"\namount = "A"\ncolour = "red"\n', "unknown setting"),
        ('date = "D"\ndescription = "N"\namount = "A"\ndate_format = "dd/mm/yyyy"\n', "strftime"),
        ('date = "D"\ndescription = "N"\namount = "A"\nheader = false\n', "column number"),
        ("date = -1\ndescription = 1\namount = 2\nheader = false\n", "start at 0"),
        ('date = "D"\ndescription = "N"\namount = "A"\nskip_lines = -2\n', "whole number"),
    ],
)
def test_layout_errors(body, fragment):
    with pytest.raises(ConfigError, match=fragment):
        parse_config(f"[layouts.x]\n{body}")


def test_rule_fields():
    config = parse_config(
        '[[rules]]\ncategory = "Eating out"\npattern = "cafe|nero"\namount_min = "-50"\n'
        'amount_max = -0.01\naccount = "card"\n'
    )
    rule = config.rules[0]
    assert rule.pattern.search("CAFE NERO")
    assert rule.amount_min == -500000
    assert rule.amount_max == -100
    assert rule.account == "card"
    assert len(rule.conditions()) == 4


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ('description = "x"\n', "category is required"),
        ('category = "x"\n', "at least one of"),
        ('category = "x"\npattern = "("\n', "regular expression"),
        ('category = "x"\namount_min = "lots"\n', "amount_min"),
        ('category = "x"\namount_min = "10"\namount_max = "5"\n', "greater than"),
        ('category = "x"\ndescription = ""\n', "non-empty"),
        ('category = "x"\ndescription = "y"\ncolour = "red"\n', "unknown setting"),
    ],
)
def test_rule_errors(body, fragment):
    with pytest.raises(ConfigError, match=fragment):
        parse_config(f"[[rules]]\n{body}")


def test_rule_error_names_rule_number():
    with pytest.raises(ConfigError, match="rule 2"):
        parse_config('[[rules]]\ncategory = "a"\ndescription = "b"\n[[rules]]\ncategory = "c"\n')


def test_top_level_errors():
    with pytest.raises(ConfigError, match="not valid TOML"):
        parse_config("[[[")
    with pytest.raises(ConfigError, match="unknown top-level"):
        parse_config("colour = true\n")
    with pytest.raises(ConfigError, match=r"\[\[rules\]\]"):
        parse_config("[rules]\ncategory = 'x'\n")


def test_missing_layout_message():
    config = parse_config('[layouts.a]\ndate = "D"\ndescription = "N"\namount = "A"\n')
    with pytest.raises(ConfigError, match="no CSV layout named 'b'") as exc:
        config.layout("b")
    assert "a" in str(exc.value)


def test_rule_amounts_allow_up_to_four_decimals():
    config = parse_config('[[rules]]\ncategory = "x"\namount_min = "0.0001"\n')
    assert config.rules[0].amount_min == 1
    with pytest.raises(ConfigError, match="decimal places"):
        parse_config('[[rules]]\ncategory = "x"\namount_min = "0.00001"\n')
