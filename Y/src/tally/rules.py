"""The user's categorisation rules, ``[[rules]]`` in ``tally.toml`` (ADR 0006).

``categorise`` is the one definition of a transaction's category (design
§ Lifecycles): the first rule whose conditions all hold wins, and nothing
stores the result, so editing the rules re-categorises everything. The rules
are validated by ``load`` and raised as ``ConfigError``, naming the rule's
1-based position, before the data file opens (AC29).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from tally.config import ConfigError
from tally.store import Entry

_KEYS = frozenset(
    {
        "name",
        "account",
        "description_contains",
        "description_matches",
        "amount_above",
        "amount_below",
    }
)
# A sign, ASCII digits that fit an amount, and no more places than any currency has. Decimal
# alone also reads "1_000", " 5 ", "NaN", "1e5" and Arabic-Indic digits.
_AMOUNT = re.compile(r"[+-]?[0-9]{1,19}(?:\.[0-9]{1,3})?")

# A pattern searches this much of a description: more than any bank writes, and a bound on
# what an unanchored pattern costs over a hostile one, which backtracks quadratically.
MATCHED = 1000


@dataclass(frozen=True)
class Rule:
    """One ``[[rules]]`` table, validated; ``None`` is a condition the rule does not have."""

    position: int
    name: str
    account: str | None = None
    contains: str | None = None
    matches: re.Pattern[str] | None = None
    above: Decimal | None = None
    below: Decimal | None = None

    @property
    def conditions(self) -> tuple[str, ...]:
        """What the rule asks of a transaction, as ``explain`` says it when it holds."""
        said = (
            (self.account, "account is {}"),
            (self.contains, "description contains {}"),
            (self.matches and self.matches.pattern, "description matches {}"),
            (self.above, "amount is above {}"),
            (self.below, "amount is below {}"),
        )
        return tuple(text.format(value) for value, text in said if value is not None)

    def failed(self, entry: Entry) -> str | None:
        """The first condition the transaction fails, or None when it meets them all.

        Cheap conditions first, so the pattern runs only on what the others let through.
        Amounts compare as exact ``Decimal`` values, which never round.
        """
        if self.account is not None and entry.account != self.account:
            return f"account is not {self.account}"
        if self.contains is not None and (
            self.contains.casefold() not in entry.description.casefold()
        ):
            return f"description does not contain {self.contains}"
        if self.matches is not None and self.matches.search(entry.description[:MATCHED]) is None:
            return f"description does not match {self.matches.pattern}"
        if self.above is not None and not entry.amount > self.above:
            return f"amount is not above {self.above}"
        if self.below is not None and not entry.amount < self.below:
            return f"amount is not below {self.below}"
        return None


@dataclass(frozen=True)
class Verdict:
    """One rule tried: every condition when it matched, else the one that failed."""

    rule: Rule
    matched: bool
    conditions: tuple[str, ...]


@dataclass(frozen=True)
class Match:
    """The transaction's category is ``rule.name``; ``verdicts`` ends with that rule's."""

    rule: Rule
    verdicts: tuple[Verdict, ...]


@dataclass(frozen=True)
class NoMatch:
    """No rule matched: the transaction is uncategorised. ``verdicts`` has every rule's."""

    verdicts: tuple[Verdict, ...]


def categorise(entry: Entry, rules: Sequence[Rule]) -> Match | NoMatch:
    """The transaction's category and why: the first rule whose conditions all hold."""
    verdicts: list[Verdict] = []
    for rule in rules:
        failed = rule.failed(entry)
        if failed is None:
            verdicts.append(Verdict(rule, matched=True, conditions=rule.conditions))
            return Match(rule, tuple(verdicts))
        verdicts.append(Verdict(rule, matched=False, conditions=(failed,)))
    return NoMatch(tuple(verdicts))


def load(tables: Sequence[Mapping[str, object]]) -> list[Rule]:
    """The rules in file order, or ``ConfigError`` for the first that is broken."""
    return [_rule(position, table) for position, table in enumerate(tables, start=1)]


class _RuleError(Exception):
    """A problem with one rule; ``_rule`` adds the rule's position."""


def _rule(position: int, table: Mapping[str, object]) -> Rule:
    try:
        text = _strings(table)
        above, below = _range(text)
        return Rule(
            position,
            text["name"],
            account=text.get("account"),
            contains=text.get("description_contains"),
            matches=_pattern(text.get("description_matches")),
            above=above,
            below=below,
        )
    except _RuleError as problem:
        raise ConfigError(f"rules[{position}]: {problem}") from None


def _hidden(c: str) -> bool:
    """What a terminal would act on or show otherwise: controls and line or paragraph breaks."""
    return unicodedata.category(c) in {"Zl", "Zp"} or unicodedata.category(c).startswith("C")


def _strings(table: Mapping[str, object]) -> dict[str, str]:
    """The rule's keys as strings, with a name a terminal can show and no empty text."""
    text: dict[str, str] = {}
    for key, value in table.items():
        if key not in _KEYS:
            raise _RuleError(f'unknown key "{key}"')
        if not isinstance(value, str):  # amounts too: a TOML number arrives binary, not exact
            raise _RuleError(f"{key} must be a string")
        text[key] = value
    if "name" not in text:
        raise _RuleError("no name")
    if not text["name"].strip():
        raise _RuleError("name must not be empty")
    if any(_hidden(c) for c in text["name"]):
        raise _RuleError("name must not contain control characters or line breaks")
    if text["name"] == "uncategorised":  # the report's table shows no match so
        raise _RuleError('name must not be "uncategorised", which the report gives no match')
    for key in ("description_contains", "description_matches"):
        if text.get(key) == "":  # would match everything; a rule with only a name says so
            raise _RuleError(f"{key} must not be empty")
    return text


def _pattern(text: str | None) -> re.Pattern[str] | None:
    if text is None:
        return None
    problem = "description_matches is not a valid pattern"
    try:
        return re.compile(text)
    except re.error as error:
        raise _RuleError(f"{problem}: {error.msg}") from None  # msg: without the position
    except OverflowError as error:  # a repeat count past C's int: a{4294967296}
        raise _RuleError(f"{problem}: {error}") from None
    except RecursionError:  # the parser recurses once per nested group
        raise _RuleError(f"{problem}: nested too deeply") from None


def _range(text: dict[str, str]) -> tuple[Decimal | None, Decimal | None]:
    above, below = _amount(text, "amount_above"), _amount(text, "amount_below")
    if above is not None and below is not None and not above < below:  # it could never match
        raise _RuleError("amount_above must be less than amount_below")
    return above, below


def _amount(text: dict[str, str], key: str) -> Decimal | None:
    if key not in text:
        return None
    if not _AMOUNT.fullmatch(text[key]):
        raise _RuleError(f'{key} is not an amount like "-100.00"')
    return Decimal(text[key])  # from text in one shape: exact
