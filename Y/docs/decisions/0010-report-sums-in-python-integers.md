# 10. Report totals are Python integers over streamed rows, not SQL sums

**Status:** accepted · **Date:** 2026-11-11 · **Replaces:** the part of [ADR 0004](0004-money-as-decimal-and-integer-minor-units.md) that says sums are integer sums in SQL

## Context

ADR 0004 planned report totals as SQLite integer sums. Two things found since make that wrong for Tally. Categories are derived by `rules.categorise` and never stored (design § Lifecycles), so SQL cannot group by them. And SQLite's integer `sum` raises `integer overflow` once a total passes 2^63 minor units (probed), which a valid data file can reach: two transactions of 2^63 − 1 each.

## Decision

`report.py` reads the transactions in SQL, filtered by month (`substr(date, 1, 7) BETWEEN ? AND ?`) and, for payees, ordered by currency, scale and description, and sums `amount_minor` as Python `int`s, which are exact at any size. The total becomes a `Decimal` only through `from_minor`. Memory is one total per month, currency and category, and `--top` payees per currency (`heapq.nsmallest`), whatever the number of rows.

## Rejected options

- **SQL `sum` for payees, which need no category:** fails on overflow, and two ways of summing would be two answers to check.
- **SQLite's `total()`:** a floating-point sum.
- **Storing the category:** the design keeps one definition of a category, so editing the rules re-categorises everything.

## Consequences

A report reads every transaction in its period once, in Python: 1,000,000 rows took about 2.5 s and 27 MB in the slice 5 review's probe. Still integer arithmetic, still exact; nothing about storage changes.
