# 8. What counts as the same transaction

**Status:** accepted · **Date:** 2026-11-10 · **Replaces:** none

## Context

Importing the same file twice, or two statements whose dates overlap, must never count a transaction twice; two genuinely separate transactions that look alike (two coffees, same day, same price, same till) must count twice. Banks' CSV rows carry no identifier; OFX rows carry a `FITID` that the bank promises is unique per account.

## Decision

Every transaction has a fingerprint, unique per account:

- **OFX:** `ofx:<FITID>`.
- **CSV:** `csv:<date>|<amount in minor units>|<normalised description>|<n>`, where the description is trimmed, whitespace-collapsed and case-folded, and `n` is the 1-based count of rows with the same date, amount and description earlier in the same file. Two identical rows in one file get `n` of 1 and 2; the same two rows in an overlapping statement get the same `n` values and are duplicates.

Rows whose fingerprint already exists for the account are reported as already there. The balance column, when a layout has one, is not part of the fingerprint.

## Rejected options

- **A hash of the whole file, skipping a file seen before:** catches only the exact same file, not an overlapping statement.
- **A hash of the raw line:** two statements can render the same transaction differently (a running balance column, a different date format in a re-exported file).
- **Date, amount and description only, without `n`:** two real transactions that match in all three would be counted once.
- **Fuzzy matching across accounts (a transfer appearing in both):** a transfer out of one account and into another are two transactions in two accounts; the brief's reports are per account and currency.

## Consequences

A bank that re-exports with a changed description (a payee renamed) produces a new transaction, which the user sees as a duplicate in the report. A CSV from one bank and an OFX from the same bank for the same period are not deduplicated against each other; the user imports one format per account.
