# 5. Two copies at once are kept apart by SQLite's own locking, not a lock file

**Status:** accepted · **Date:** 2026-11-10 · **Replaces:** none

## Context

Two copies of Tally may run at once, for example an import started twice by a scheduled job. Neither may corrupt the data file, and a transaction imported by both must count once.

## Decision

One SQLite data file. Every command that writes opens one `BEGIN IMMEDIATE` transaction for the whole import, with a busy timeout of 10 seconds, and a `UNIQUE` constraint on the transaction fingerprint per account. The second copy waits for the first; when the first commits, the second's rows are duplicates against the committed ones and it reports them as already there. If the wait runs out, the second copy stops with "another copy of tally is using the data file", exit code 3, having written nothing. Journal mode is WAL, so a report can read while an import writes.

## Rejected options

- **A separate lock file (`fcntl.flock`, `msvcrt.locking`):** a second mechanism beside SQLite's, with its own platform differences and stale-lock cases; SQLite already serialises writers correctly.
- **Dedupe in Python before the transaction (read existing fingerprints, then insert the rest):** check-then-act with the import in between lets two copies both decide a row is new. The constraint and `INSERT ... ON CONFLICT DO NOTHING` inside the one transaction make the database decide.
- **Letting the second copy fail immediately:** a scheduled double start would then alternate between success and failure for no reason the user can see; waiting briefly is what they expect.

## Consequences

Imports are all-or-nothing: a file that fails half way leaves nothing. A very long import holds the write lock for its whole run, so the busy timeout bounds how long a second copy waits before it gives up and says why.
