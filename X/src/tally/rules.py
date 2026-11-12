"""Apply the user's categorisation rules. First matching rule wins."""

from __future__ import annotations

from dataclasses import dataclass

from tally.config import RULE_SCALE, Rule
from tally.store import Transaction

UNCATEGORISED = "Uncategorised"


@dataclass(frozen=True)
class Verdict:
    rule: Rule
    matched: bool
    failed_condition: str | None  # the first condition that did not hold


def _check(rule: Rule, txn: Transaction) -> str | None:
    """Return the first failing condition, or None if the rule matches."""
    if rule.account is not None and rule.account.casefold() != txn.account.casefold():
        return f'account is "{rule.account}"'
    text = txn.searchable_text
    if rule.description is not None and rule.description.casefold() not in text.casefold():
        return f'description contains "{rule.description}"'
    if rule.pattern is not None and not rule.pattern.search(text):
        return f"description matches /{rule.pattern_text}/"
    # Rule amounts and transaction amounts may use different numbers of
    # decimal places; compare at the rule's scale, exactly, in integers.
    amount = txn.amount_minor * 10 ** (RULE_SCALE - txn.scale)
    if rule.amount_min is not None and amount < rule.amount_min:
        return "amount is at least the minimum"
    if rule.amount_max is not None and amount > rule.amount_max:
        return "amount is at most the maximum"
    return None


def categorise(rules: list[Rule], txn: Transaction) -> tuple[str, Rule | None]:
    for rule in rules:
        if _check(rule, txn) is None:
            return rule.category, rule
    return UNCATEGORISED, None


def explain(rules: list[Rule], txn: Transaction) -> list[Verdict]:
    """Every rule up to and including the one that matched, with why."""
    out = []
    for rule in rules:
        failed = _check(rule, txn)
        out.append(Verdict(rule, failed is None, failed))
        if failed is None:
            break
    return out
