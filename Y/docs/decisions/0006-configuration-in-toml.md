# 6. Configuration, layouts and rules in one TOML file the user edits by hand

**Status:** accepted · **Date:** 2026-11-10 · **Replaces:** none

## Context

The brief says the user describes CSV layouts once and writes categorisation rules by hand in a configuration file. The format has to be readable and writable by a person, parsed by the standard library, and able to hold ordered lists of rules (first match wins).

## Decision

One file, `tally.toml`, read with `tomllib`: a `[layouts.<name>]` table per CSV layout and an ordered `[[rules]]` array of tables. Tally never writes this file. A configuration error names the table or rule and the problem and exits 2 before touching the data file.

## Rejected options

- **YAML:** no standard-library parser, and a dependency for configuration alone is against ADR 1.
- **JSON:** no comments, and rules are the kind of file people annotate.
- **INI (`configparser`):** no lists or nesting; rules would need numbered sections.
- **Rules in the database, edited through commands:** the brief says a file the user edits by hand, and a file can be versioned and shared between machines by the user.

## Consequences

Rule order is the array order, which is visible in the file. `tomllib` cannot write TOML, which matches "Tally never writes this file".
