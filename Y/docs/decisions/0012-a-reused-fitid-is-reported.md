# 12. A fingerprint already stored for a different transaction is reported, not counted

**Status:** accepted · **Date:** 2026-11-11 · **Amends:** [ADR 0008](0008-what-counts-as-the-same-transaction.md), where "a row whose fingerprint already exists is already there"

## Context

ADR 0008 makes an OFX transaction's identity its FITID, which the bank promises is unique, and counts any row whose fingerprint is already stored as already there. The whole-codebase audit showed what that trusts: a statement file's author chooses its FITIDs. A forged file imported first, with a FITID the bank will use later (the sample's are the date and a sequence number), made the genuine statement's -500.00 transfer "already there" and left a -0.01 row in its place; two different transactions sharing a FITID in one file kept only the first. Both were silent.

## Decision

A row whose fingerprint is already stored counts as already there only when the stored row has the same date and the same amount. Otherwise it is an unreadable row with the reason "its FITID is already used by a different transaction", so the summary and standard error say so and nothing is dropped unseen. The description is not compared: a bank's re-export may rename a payee under the same FITID. A CSV fingerprint already holds the date and the amount, so this can only happen to OFX.

## Rejected options

- **Compare the description too:** a renamed payee would turn an honest overlap into unreadable rows.
- **Put the date and amount into the OFX fingerprint:** a bank correcting an amount under the same FITID would then add a second row, which is the double count ADR 0008 exists to prevent.
- **Refuse the whole file:** one reused FITID would stop every other row of a genuine statement.

## Consequences

One extra indexed lookup per duplicate row. A forger can still match a genuine transaction's date and amount exactly and change only its description; the forged row then stands, as any row a hostile file adds does.
