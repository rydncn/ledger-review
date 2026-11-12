"""The configuration file: CSV layouts and categorisation rules.

The file is TOML, edited by hand, at ``$XDG_CONFIG_HOME/tally/config.toml``
(``~/.config/tally/config.toml`` by default), or wherever ``--config`` or
``TALLY_CONFIG`` points. ``tally init`` writes a commented starting point.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from tally.errors import ConfigError
from tally.money import AmountError, parse_decimal_string

APP_DIR_NAME = "tally"
# Rule amounts are kept at this many decimal places whatever the account's
# currency uses, and transactions are rescaled to it for comparison. Accounts
# allow at most this many decimals (see `tally account add --decimals`).
RULE_SCALE = 4
CONFIG_FILE_NAME = "config.toml"
DATA_FILE_NAME = "tally.db"


def default_config_path() -> Path:
    env = os.environ.get("TALLY_CONFIG")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / APP_DIR_NAME / CONFIG_FILE_NAME


def default_data_path(config: Config | None = None) -> Path:
    env = os.environ.get("TALLY_DATA")
    if env:
        return Path(env).expanduser()
    if config and config.data_file:
        return Path(config.data_file).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / APP_DIR_NAME / DATA_FILE_NAME


@dataclass(frozen=True)
class Layout:
    """How to read one bank's CSV export."""

    name: str
    date: str | int
    description: str | int
    amount: str | int | None = None
    money_in: str | int | None = None
    money_out: str | int | None = None
    memo: str | int | None = None
    balance: str | int | None = None
    date_format: str = "%d/%m/%Y"
    decimal_separator: str = "."
    thousands_separator: str = ","
    delimiter: str = ","
    quotechar: str = '"'
    encoding: str = "auto"
    header: bool = True
    skip_lines: int = 0
    negate: bool = False

    @property
    def uses_two_columns(self) -> bool:
        return self.amount is None


@dataclass(frozen=True)
class Rule:
    """One categorisation rule. The first rule that matches wins."""

    index: int  # 1-based position in the file, for messages
    category: str
    description: str | None = None
    pattern: re.Pattern[str] | None = None
    pattern_text: str | None = None
    amount_min: int | None = None  # in units of 10**-RULE_SCALE, not the account's minor units
    amount_max: int | None = None
    account: str | None = None

    def conditions(self) -> list[str]:
        out = []
        if self.description is not None:
            out.append(f'description contains "{self.description}"')
        if self.pattern is not None:
            out.append(f"description matches /{self.pattern_text}/")
        if self.amount_min is not None:
            out.append("amount is at least the minimum")
        if self.amount_max is not None:
            out.append("amount is at most the maximum")
        if self.account is not None:
            out.append(f'account is "{self.account}"')
        return out


@dataclass
class Config:
    layouts: dict[str, Layout] = field(default_factory=dict)
    rules: list[Rule] = field(default_factory=list)
    data_file: str | None = None
    path: Path | None = None

    def layout(self, name: str) -> Layout:
        try:
            return self.layouts[name]
        except KeyError:
            known = ", ".join(sorted(self.layouts)) or "none defined"
            raise ConfigError(
                f"no CSV layout named {name!r} in the configuration",
                hint=f"Layouts available: {known}. Add one under [layouts.{name}].",
            ) from None


_LAYOUT_KEYS = {
    "date",
    "description",
    "amount",
    "money_in",
    "money_out",
    "memo",
    "balance",
    "date_format",
    "decimal_separator",
    "thousands_separator",
    "delimiter",
    "quotechar",
    "encoding",
    "header",
    "skip_lines",
    "negate",
}
_RULE_KEYS = {"category", "description", "pattern", "amount_min", "amount_max", "account"}
_TOP_KEYS = {"layouts", "rules", "data_file"}


def _column(value: object, where: str, key: str) -> str | int:
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise ConfigError(f"{where}: {key} must be a column name or a column number")
    if isinstance(value, int) and value < 0:
        raise ConfigError(f"{where}: {key} column numbers start at 0")
    if isinstance(value, str) and not value.strip():
        raise ConfigError(f"{where}: {key} must not be blank")
    return value


def _optional_column(table: dict, where: str, key: str) -> str | int | None:
    return _column(table[key], where, key) if key in table else None


def _str(table: dict, where: str, key: str, default: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{where}: {key} must be text")
    return value


def _bool(table: dict, where: str, key: str, default: bool) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: {key} must be true or false")
    return value


def parse_layout(name: str, table: object) -> Layout:
    where = f"[layouts.{name}]"
    if not isinstance(table, dict):
        raise ConfigError(f"{where} must be a table")
    unknown = set(table) - _LAYOUT_KEYS
    if unknown:
        raise ConfigError(f"{where}: unknown setting(s) {', '.join(sorted(unknown))}")
    for required in ("date", "description"):
        if required not in table:
            raise ConfigError(f"{where}: {required} is required")
    amount = _optional_column(table, where, "amount")
    money_in = _optional_column(table, where, "money_in")
    money_out = _optional_column(table, where, "money_out")
    if amount is None and (money_in is None or money_out is None):
        raise ConfigError(f"{where}: give either amount, or both money_in and money_out")
    if amount is not None and (money_in is not None or money_out is not None):
        raise ConfigError(f"{where}: give either amount, or money_in and money_out, not both")
    decimal = _str(table, where, "decimal_separator", ".")
    thousands = _str(table, where, "thousands_separator", ",")
    if len(decimal) != 1:
        raise ConfigError(f"{where}: decimal_separator must be one character")
    if len(thousands) > 1:
        raise ConfigError(f"{where}: thousands_separator must be one character or empty")
    if decimal == thousands:
        raise ConfigError(f"{where}: decimal_separator and thousands_separator must differ")
    delimiter = _str(table, where, "delimiter", ",")
    if len(delimiter) != 1:
        raise ConfigError(f"{where}: delimiter must be one character")
    quotechar = _str(table, where, "quotechar", '"')
    if len(quotechar) != 1:
        raise ConfigError(f"{where}: quotechar must be one character")
    skip_lines = table.get("skip_lines", 0)
    if isinstance(skip_lines, bool) or not isinstance(skip_lines, int) or skip_lines < 0:
        raise ConfigError(f"{where}: skip_lines must be a whole number")
    header = _bool(table, where, "header", True)
    date = _column(table["date"], where, "date")
    description = _column(table["description"], where, "description")
    memo = _optional_column(table, where, "memo")
    balance = _optional_column(table, where, "balance")
    if not header:
        for key, value in (
            ("date", date),
            ("description", description),
            ("amount", amount),
            ("money_in", money_in),
            ("money_out", money_out),
            ("memo", memo),
            ("balance", balance),
        ):
            if isinstance(value, str):
                raise ConfigError(f"{where}: {key} must be a column number when header = false")
    date_format = _str(table, where, "date_format", "%d/%m/%Y")
    if "%" not in date_format:
        raise ConfigError(f"{where}: date_format must use strftime codes such as %d/%m/%Y")
    return Layout(
        name=name,
        date=date,
        description=description,
        amount=amount,
        money_in=money_in,
        money_out=money_out,
        memo=memo,
        balance=balance,
        date_format=date_format,
        decimal_separator=decimal,
        thousands_separator=thousands,
        delimiter=delimiter,
        quotechar=quotechar,
        encoding=_str(table, where, "encoding", "auto"),
        header=header,
        skip_lines=skip_lines,
        negate=_bool(table, where, "negate", False),
    )


def _amount_bound(table: dict, where: str, key: str) -> int | None:
    if key not in table:
        return None
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise ConfigError(f'{where}: {key} must be an amount such as "-50.00"')
    if isinstance(value, float):
        value = repr(value)
    try:
        return parse_decimal_string(str(value), scale=RULE_SCALE)
    except AmountError as exc:
        raise ConfigError(f"{where}: {key}: {exc}") from None


def parse_rule(index: int, table: object) -> Rule:
    where = f"rule {index}"
    if not isinstance(table, dict):
        raise ConfigError(f"{where} must be a table ([[rules]])")
    unknown = set(table) - _RULE_KEYS
    if unknown:
        raise ConfigError(f"{where}: unknown setting(s) {', '.join(sorted(unknown))}")
    category = table.get("category")
    if not isinstance(category, str) or not category.strip():
        raise ConfigError(f"{where}: category is required")
    description = table.get("description")
    if description is not None and (not isinstance(description, str) or not description.strip()):
        raise ConfigError(f"{where}: description must be non-empty text")
    pattern_text = table.get("pattern")
    pattern = None
    if pattern_text is not None:
        if not isinstance(pattern_text, str):
            raise ConfigError(f"{where}: pattern must be text")
        try:
            pattern = re.compile(pattern_text, re.IGNORECASE)
        except re.error as exc:
            raise ConfigError(
                f"{where}: pattern is not a valid regular expression: {exc}"
            ) from None
    amount_min = _amount_bound(table, where, "amount_min")
    amount_max = _amount_bound(table, where, "amount_max")
    if amount_min is not None and amount_max is not None and amount_min > amount_max:
        raise ConfigError(f"{where}: amount_min is greater than amount_max")
    account = table.get("account")
    if account is not None and (not isinstance(account, str) or not account.strip()):
        raise ConfigError(f"{where}: account must be an account name")
    rule = Rule(
        index=index,
        category=category.strip(),
        description=description,
        pattern=pattern,
        pattern_text=pattern_text,
        amount_min=amount_min,
        amount_max=amount_max,
        account=account,
    )
    if not rule.conditions():
        raise ConfigError(
            f"{where}: a rule needs at least one of description, pattern, "
            "amount_min, amount_max or account"
        )
    return rule


def parse_config(text: str, path: Path | None = None) -> Config:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"configuration file is not valid TOML: {exc}") from None
    unknown = set(raw) - _TOP_KEYS
    if unknown:
        raise ConfigError(f"unknown top-level setting(s): {', '.join(sorted(unknown))}")
    config = Config(path=path)
    data_file = raw.get("data_file")
    if data_file is not None:
        if not isinstance(data_file, str) or not data_file.strip():
            raise ConfigError("data_file must be a path")
        config.data_file = data_file
    layouts = raw.get("layouts", {})
    if not isinstance(layouts, dict):
        raise ConfigError("[layouts] must be a table of layouts")
    for name, table in layouts.items():
        config.layouts[name] = parse_layout(name, table)
    rules = raw.get("rules", [])
    if not isinstance(rules, list):
        raise ConfigError("rules must be written as [[rules]] tables")
    for i, table in enumerate(rules, start=1):
        config.rules.append(parse_rule(i, table))
    return config


def load_config(path: Path | None = None, *, required: bool = False) -> Config:
    """Load the configuration. A missing file is an empty configuration unless required."""
    path = path or default_config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if required:
            raise ConfigError(
                f"no configuration file at {path}",
                hint="Run `tally init` to create one.",
            ) from None
        return Config(path=path)
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file {path}: {exc.strerror}") from None
    except UnicodeDecodeError:
        raise ConfigError(f"configuration file {path} is not UTF-8 text") from None
    return parse_config(text, path)


STARTER_CONFIG = """\
# Tally configuration. Edit by hand. See docs/configuration.md for every setting.
#
# Where the data file lives (optional; the default is under ~/.local/share/tally).
# data_file = "~/.local/share/tally/tally.db"

# ---------------------------------------------------------------------------
# CSV layouts: describe each bank's export once, then `tally import FILE
# --account NAME --layout NAME`. Columns may be header names or 0-based numbers.

[layouts.current-account]
date = "Date"
description = "Description"
money_out = "Money Out"
money_in = "Money In"
balance = "Balance"          # optional: lets Tally check each row against the running balance
date_format = "%d/%m/%Y"
decimal_separator = "."
thousands_separator = ","

[layouts.credit-card]
date = "Transaction Date"
description = "Payee"
amount = "Amount"            # negative is money out
date_format = "%m/%d/%Y"     # US-style dates
delimiter = ";"
decimal_separator = ","
thousands_separator = "."
# encoding = "latin-1"       # usually detected; set it if a file reads wrongly
# negate = true              # if the bank shows spending as positive numbers

# ---------------------------------------------------------------------------
# Rules: the first matching rule wins, so put specific rules before general
# ones. Match on description (text, case-insensitive), pattern (a regular
# expression), amount_min/amount_max (inclusive; negative is money out) and
# account. Amounts are quoted so they stay exact.

[[rules]]
category = "Groceries"
description = "TESCO"

[[rules]]
category = "Transport"
pattern = "^TFL "

[[rules]]
category = "Salary"
description = "SALARY"
amount_min = "0.01"

[[rules]]
category = "Council tax"
description = "COUNCIL TAX"

[[rules]]
category = "Transfers"
description = "TRANSFER TO"
account = "savings"
"""
