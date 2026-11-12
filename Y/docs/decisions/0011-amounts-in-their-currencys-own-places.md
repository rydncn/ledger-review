# 11. An amount is written with its currency's own decimal places

**Status:** accepted · **Date:** 2026-11-11 · **Amends:** AC32 and AC35 in `docs/design.md`

## Context

AC32 said an amount cell in CSV matches `^-?[0-9]+\.[0-9]{2}$`, AC35 that amounts in JSON are strings "with two decimals". Tally stores amounts as integer minor units with each currency's scale (ADR 0004, `money.scale_of`): a yen amount has no decimals and a Bahraini dinar amount has three. The code has always written each amount with its own currency's places (`from_minor(minor, scale)`), exactly; the criteria did not say so. The slice 4 review, the slice 5 audit and the slice 5 review each raised it and recommended amending the criteria; the call was to make the reasonable choice.

## Decision

An amount, in a table, CSV or JSON, has exactly its currency's number of decimal places: `-?[0-9]+` for a currency with none, `-?[0-9]+\.[0-9]{n}` for one with `n`. It is never padded to two places and never rounded. In CSV it is a plain number, never prefixed; in JSON a string, never a number. AC32 and AC35 now say this, and `tests/test_reports.py` asserts it for JPY (0 places) and BHD (3) in every CSV and JSON command that writes an amount.

## Rejected options

- **Always two places:** pads yen to `1200.00`, which no bank shows, and cannot write a dinar's third place without rounding, which breaks "money is exact".
- **A separate scale column:** the currency already says it, and the amount's own text carries its places to a spreadsheet and to JSON readers alike.

## Consequences

The amount cell's shape still never starts with `=`, `+`, `@`, a tab or a carriage return, so the spreadsheet rule holds: a leading `-` is followed only by digits. A reader of the CSV or JSON who assumed two places must read the currency; no output that met the old criteria changes, since every currency in the samples has two places.
