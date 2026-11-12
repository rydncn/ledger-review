# 4. Money is a Decimal in code and integer minor units in the data file

**Status:** accepted; replaced in part by [ADR 0010](0010-report-sums-in-python-integers.md), which sums report totals in Python, not SQL · **Date:** 2026-11-10 · **Replaces:** none

## Context

Amounts must be exact: no floating point anywhere money is held or summed. Statement files write amounts in several textual forms; SQLite has no decimal type and its `SUM` over `REAL` is floating point.

## Decision

In code, an amount is a `Money` value holding a `decimal.Decimal` and a currency code; parsing goes from text straight to `Decimal`, never through `float`. In the data file, an amount is an `INTEGER` of minor units (pence, cents) in a column whose row carries the account, and the account carries the currency and its number of decimal places (2 unless the currency says otherwise). Sums are integer sums in SQL and are turned back into `Decimal` at the edge. A rule check fails the build on the word `float` anywhere under `src/`.

## Rejected options

- **`Decimal` stored as text in SQLite:** exact, but `SUM` would have to happen in Python over every row, and `ORDER BY` on text amounts sorts wrongly.
- **SQLite `REAL`:** floating point; `0.1 + 0.2` is the whole problem.
- **`fractions.Fraction`:** exact, but amounts have fixed scale and nothing in the brief divides.

## Consequences

Every currency has a fixed scale, held with the account; a currency with three decimal places (BHD, KWD) works by setting it. Reports are integer arithmetic in SQL, which is also the fastest path for a large file.
