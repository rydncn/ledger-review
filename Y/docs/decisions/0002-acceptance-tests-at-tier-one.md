# 2. Acceptance tests run in-process against a real data file and real statement files

**Status:** accepted · **Date:** 2026-11-10 · **Replaces:** none

## Context

This records which tier the acceptance tests run at. Tally has no server, no device and no UI: its real interface is the command line, its real infrastructure is a SQLite file on disk and the statement files a bank produces.

## Decision

Tier 1. Every acceptance test calls `tally.cli.main(argv)` in the test process, with its own temporary data directory and configuration file, a real SQLite data file created by Tally's own schema code, and real statement files (the samples, plus files written by the test with the exact bytes under test: byte order marks, CRLF, Latin-1). Nothing of Tally's is faked. For each behaviour group, one test also runs the installed command in a real subprocess, which is the only way to observe the process-level contract: exit codes, standard error, `NO_COLOR`, output when standard output is not a terminal, and two copies running at once.

There is no tier 2 or 3: nothing in Tally is observable only on a device.

## Rejected options

- **Subprocess for every test:** truer to the user, but mutmut's trampolines only see tests that run the mutated code in the test process, so mutation testing would see nothing. Also slower by an interpreter start per test.
- **An in-memory SQLite database for speed:** the data file's permissions, its locking between processes and its path handling are part of what the brief asks for; a `:memory:` database can't show any of them.

## Consequences

Tests are fast enough to run on every change and in random order in parallel. Anything that only shows across processes has to be written as a subprocess test on purpose.
