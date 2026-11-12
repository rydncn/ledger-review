# Tally design

**Status:** accepted · **Date:** 2026-11-10

## Goal

Tally is a command-line tool for people who want to see where their money goes without giving their bank statements to anyone. It imports the statement files banks let users download (CSV in each bank's layout, OFX in SGML and XML), removes duplicates, sorts transactions into categories by rules the user writes, and reports spending by month. It runs entirely on the user's computer and never makes a network connection.

Out of scope: a graphical interface, bank connections, investments, budgets, multiple users, syncing between computers, and currency conversion.

## Decisions

| Decision | ADR |
| --- | --- |
| Python 3.15 with uv, standard library only, SQLite | [0001](decisions/0001-standard-library-python-on-sqlite.md) |
| Acceptance tests in-process against a real data file and real statement files (tier 1) | [0002](decisions/0002-acceptance-tests-at-tier-one.md) |
| Installs refuse source builds, except the mutation testing job | [0003](decisions/0003-source-builds-refused-except-for-mutation-testing.md) |
| Money is a `Decimal` in code and integer minor units in the data file | [0004](decisions/0004-money-as-decimal-and-integer-minor-units.md) |
| Two copies at once are kept apart by SQLite's locking, not a lock file | [0005](decisions/0005-one-data-file-under-sqlite-locking.md) |
| Configuration, layouts and rules in one TOML file | [0006](decisions/0006-configuration-in-toml.md) |
| What counts as the same transaction | [0008](decisions/0008-what-counts-as-the-same-transaction.md) |
| Report totals are Python integers over streamed rows | [0010](decisions/0010-report-sums-in-python-integers.md) |

## Shape

```
tally.toml (user-written)        statement files (bank-written, hostile)
   layouts, rules                     CSV / OFX SGML / OFX XML
        │                                      │
        ▼                                      ▼
   config.py ──────────────► importers/  (bytes → rows + rejections, streamed)
                                   │
                                   ▼
                      store.py (SQLite data file, 0600, one writer at a time)
                        accounts · transactions(fingerprint UNIQUE) · imports
                                   │
                    ┌──────────────┴──────────────┐
                    ▼                             ▼
               rules.py                      report.py
         categorise(txn) → (category, why)   monthly totals, top payees (Python ints)
                    └──────────────┬──────────────┘
                                   ▼
                                cli.py
          argparse · table or CSV or JSON · colour only on a TTY without NO_COLOR
          stdout = data, stderr = messages, exit 0/1/2/3
```

- **Config** (`tally.toml`): `data_dir` (optional), `[layouts.<name>]` and `[[rules]]`. Found at `--config`, else `$TALLY_CONFIG`, else `~/.config/tally/tally.toml`.
- **Data directory:** `--data-dir`, else `$TALLY_DATA_DIR`, else `~/.local/share/tally/` (`%LOCALAPPDATA%\tally\` on Windows); created `0700`, data file `tally.sqlite` created `0600`.
- **Boundaries in tests:** everything is real: the data file, the files, the config. Nothing is faked; the only seam is `argv`, the environment and the current directory, which each test owns.
- **Exit codes:** 0 done; 1 refused or the data said no (unknown account, not a statement, currency mismatch); 2 usage or configuration error; 3 another copy of Tally holds the data file.
- **Money**: `Money(amount: Decimal, currency: str)`; parsing accepts `1,234.56`, `1.234,56`, `(12.50)`, `-12.50`, `12.50-`, `£12.50` and a sign after a currency symbol, per the layout's separators; `12.50 CR`, `12.50 DR`, `1,2,3.4` and a bare separator are unreadable (AC44). Amount sign: in a single signed `amount` column, parentheses and a leading or trailing minus mean negative. In separate `money_in` and `money_out` columns the column decides the sign and the value's magnitude is what counts: banks write debits as `(3.40)` or `-3.40` in a money-out column, and the sample's own balance column shows `(3.40)` under Money Out is 3.40 out. A transaction's amount is money in minus money out.
- **Which importer reads a file** is decided from its first bytes, never its name: `OFXHEADER:` means OFX SGML; `<?xml` or `<?OFX` or `<OFX>` (after an optional byte order mark) means OFX XML; anything else is CSV and needs `--layout`. `--layout` with an OFX file is a usage error (exit 2).

## Lifecycles

### An account

| State | `add` | `import` | `forget` |
| --- | --- | --- | --- |
| absent | → present (AC1) | refused: unknown account (AC5) | refused: unknown account (AC4) |
| present | refused: name taken (AC2) | → present, with more transactions (AC9…) | → absent, with its transactions and import records (AC3) |

An account's currency is fixed at `add`. An import whose file states another currency (OFX `CURDEF`) is refused (AC24); CSV files state none and take the account's. "Present" is one query, `store.account(name)`, and nothing else reads the accounts table by name.

### An import run

| State | What moves it | Next |
| --- | --- | --- |
| started | the file opens and the first bytes read as a statement | reading |
| started | the layout is unknown or invalid (exit 2), the account is unknown or the file is not a statement (exit 1), or another copy holds the data file past the timeout (exit 3) | failed (nothing written) |
| reading | each row becomes added, already there, or unreadable, inside one `BEGIN IMMEDIATE` | committed |
| reading | any error other than a row's own: the data file cannot be written, the disk fails, the process is interrupted | failed (rolled back; nothing written: AC42) |
| committed | counts printed; the import record written in the same transaction | done |

A row's outcome is one of `added`, `duplicate`, `unreadable(line, reason)`; the three are decided in one function per importer and counted once. A failed import writes nothing, including the import record (AC17, AC8).

### A transaction's category (derived, never stored)

`rules.categorise(transaction, rules) -> Match | NoMatch` is the one definition. "Uncategorised" means `NoMatch`; the report, the `uncategorised` list and `explain` all call it. Nothing stores a category, so editing the rules re-categorises everything (AC26 to AC28).

### The data file lock

| State | Open for writing by a second copy | After `busy_timeout` |
| --- | --- | --- |
| free | → held by it | |
| held by another copy | waits | refused: "another copy of tally is using the data file", exit 3, nothing written (AC8) |

Readers (reports) never wait on writers: WAL journal mode.

## Acceptance criteria

Each becomes a test named `test_acNN_...`. "Refused" always means: a message on standard error that names the problem without any of the file's content, nothing on standard output, nothing written, and the exit code given.

### Accounts and the data file

#### AC1. Add an account
- **Given** no data directory exists
- **When** `tally account add current --currency GBP`
- **Then** `tally account list` shows `current GBP`, the data directory exists with mode `0700` and the data file with mode `0600`

#### AC2. A name is taken once
- **Given** account `current`
- **When** `tally account add current --currency EUR`
- **Then** refused, exit 1, and the account still has currency GBP

#### AC3. Forget deletes the account and everything imported into it
- **Given** accounts `current` and `savings`, each with imported transactions
- **When** `tally account forget current --yes`
- **Then** `account list` shows only `savings`; `report` shows only savings' transactions; the import records for `current` are gone, and no forgotten description remains in the data file or its WAL (SQLite `secure_delete`; review 2). Without `--yes` on a terminal, a prompt names the account and the count of transactions and refuses on anything but `yes`; without `--yes` and not on a terminal, refused with exit 2

#### AC4. Forgetting an unknown account is refused
- **Given** no account `old`
- **When** `tally account forget old --yes`
- **Then** refused, exit 1

#### AC5. Importing into an unknown account is refused
- **Given** no account `old`
- **When** `tally import --account old --layout bank samples/current-account.csv`
- **Then** refused, exit 1, the data file unchanged

#### AC6. The data file is the user's alone, wherever it is
- **Given** `TALLY_DATA_DIR` set to a fresh path (and, in another case, `--data-dir`)
- **When** any writing command runs
- **Then** the directory is created `0700` and the file `0600`; a pre-existing data file with wider permissions is tightened to `0600` and the user is told on standard error; a pre-existing data directory that another user owns or can write is refused (exit 1) and left as it is, and so is a data, `-wal`, `-shm` or `-journal` file in it that is a link, has a second hard link or belongs to another user (the whole-codebase audit)

#### AC7. Two copies at once count each transaction once
- **Given** account `current` and a statement of 2,000 rows
- **When** two `tally import` processes start on the same file within the same second
- **Then** both exit 0 (or one exits 3 if it waited past the timeout), the data file holds exactly 2,000 transactions, `PRAGMA integrity_check` is `ok`, and the two reported `added` counts sum to 2,000

#### AC8. A copy that cannot get the file in time says so and writes nothing
- **Given** another process holding a write transaction on the data file for longer than the busy timeout
- **When** `tally import ...`
- **Then** refused with "another copy of tally is using the data file", exit 3, and no transaction or import record from it exists

### CSV import

#### AC9. A layout describes a bank's CSV once
- **Given** `[layouts.bank]` with `date = "Date"`, `description = "Description"`, `money_out = "Money Out"`, `money_in = "Money In"`, `date_format = "%d/%m/%Y"`, `decimal = "."`, `thousands = ","`
- **When** `tally import --account current --layout bank samples/current-account.csv`
- **Then** 5 added, 0 already there, 0 unreadable, and the amounts are −42.17, +2450.00, +12.50, −3.40, −156.00 on the dates 2026-10-01 to 2026-10-05 (the `(3.40)` under Money Out is 3.40 out, as the balance column confirms)

#### AC10. A byte order mark and Windows line endings are not data
- **Given** the sample above (UTF-8 with a BOM, CRLF)
- **When** imported
- **Then** the header `Date` is matched (no BOM in the column name) and no description ends in a carriage return

#### AC11. Latin-1, semicolons, decimal commas and US dates
- **Given** `[layouts.card]` with `delimiter = ";"`, `amount = "Amount"`, `date_format = "%m/%d/%Y"`, `decimal = ","`, `thousands = "."`, `encoding = "latin-1"`
- **When** `tally import --account card --layout card samples/credit-card.csv`
- **Then** 3 added; the description `CAFÉ NERO` is stored with its É; amounts −4.20, −23.99, +150.00; dates 2026-10-02, 2026-10-03, 2026-10-03

#### AC12. Quoted fields with commas and thousands separators are exact
- **Given** the current-account sample
- **When** imported
- **Then** `TESCO STORES 2045, LEEDS` is one description, `"2,450.00"` is 2450.00 exactly (as minor units 245000), and the balance column is ignored

#### AC13. Rows that cannot be read are reported by line and reason, the rest imported
- **Given** a file whose line 3 has a date `31/02/2026`, line 4 an amount `twelve`, and line 5 too few columns
- **When** imported
- **Then** the other rows are added; standard error lists `line 3: date does not match %d/%m/%Y`, `line 4: amount is not a number`, `line 5: expected 5 columns, found 3`, none quoting the row's content; exit 0; the summary says 3 unreadable. With 500 unreadable rows, standard error lists the first 50 and then `and 450 more`; the JSON form (AC20) lists the first 1,000 and counts them all, so a file of nothing but unreadable rows costs bounded memory. A record that an open quote carries over several lines is reported on the line it starts on, with `in a record that runs to line N`

#### AC14. The same file twice adds nothing
- **Given** the current-account sample imported
- **When** imported again
- **Then** 0 added, 5 already there, and the transaction count is unchanged

#### AC15. Overlapping statements add only the new rows
- **Given** the sample imported, and a second file with its last three rows plus two new ones
- **When** the second file is imported
- **Then** 2 added, 3 already there

#### AC16. Identical rows in one file are separate transactions
- **Given** a file with two identical rows (same date, description, amount)
- **When** imported, then the same file imported again
- **Then** the first import adds 2; the second adds 0 and reports 2 already there

#### AC17. A file that is not a statement is refused whole
- **Given** a PNG renamed `.csv`, an empty file, and a CSV whose header has none of the layout's columns
- **When** each is imported
- **Then** each is refused with a reason (`not a text file`, `empty file`, `header has no column named "Date"`), exit 1, nothing written, no import record

#### AC18. A very large file is streamed
- **Given** a 300,000-row statement (about 20 MB)
- **When** imported
- **Then** it succeeds, the process's peak memory stays under 100 MB (`tracemalloc`), and the count is right

#### AC19. Nothing personal in messages
- **Given** a file whose descriptions and amounts are unique marker strings, including a row that cannot be read
- **When** imported with and without `--verbose`
- **Then** no marker appears on standard error; the summary and the unreadable-row messages carry only counts, line numbers and reasons

#### AC20. The import summary is also machine-readable
- **Given** any import
- **When** run with `--format json`
- **Then** standard output is one JSON object `{"account", "file", "added", "duplicates", "unreadable_count", "unreadable": [{"line", "reason"}]}`, with at most 1,000 entries in `unreadable` (AC13), and nothing else, where an OFX file's entries carry `"transaction"` (its position, as AC25 counts it) in place of `"line"`; the human form goes to standard output without `--format`

#### AC40. A monstrous field does not crash the import
- **Given** a CSV with a 50 MB single quoted field on line 4
- **When** imported
- **Then** line 4 is reported unreadable (`field longer than 1 MB`) and the other rows are added

### OFX import

#### AC21. OFX 1.x SGML with unclosed tags
- **Given** `samples/savings.ofx` and account `savings` in GBP
- **When** `tally import --account savings samples/savings.ofx`
- **Then** 2 added: 2026-10-01 +12.34 `INTEREST`, 2026-10-15 −500.00 `TRANSFER TO CURRENT`, with fingerprints from their `FITID`s

#### AC22. OFX 2.x XML
- **Given** the same statement as XML (an `<?xml?>` header, `<?OFX ...?>`, closed tags)
- **When** imported
- **Then** the same 2 transactions, and importing the SGML file afterwards adds 0

#### AC23. The same OFX file twice adds nothing
- **Given** the sample imported
- **When** imported again
- **Then** 0 added, 2 already there

#### AC24. A currency that is not the account's is refused
- **Given** account `savings` in EUR
- **When** `samples/savings.ofx` (`CURDEF` GBP) is imported
- **Then** refused: `the file is in GBP but the account is in EUR`, exit 1, nothing written

#### AC25. A broken transaction is reported, the rest imported
- **Given** an OFX file whose second `STMTTRN` has `TRNAMT` of `12,34` and whose third has no `DTPOSTED`
- **When** imported
- **Then** the first is added; standard error names the two by their position (`transaction 2: amount is not a number`, `transaction 3: no DTPOSTED`); exit 0

#### AC39. An OFX XML file with a DOCTYPE or entities is refused
- **Given** an XML OFX file with a `<!DOCTYPE` declaration (external entity, or a billion-laughs expansion)
- **When** imported
- **Then** refused: `the file declares a DOCTYPE, which statements never do`, exit 1, by Tally's scanner wherever the declaration appears, with no XML parser anywhere (ADR 0009) (observed: the message is the DOCTYPE one, not a parse or file error, and the entity's `SYSTEM` path names a FIFO the test created, which would block the test if anything opened it)

#### AC41. Tags nested or repeated beyond reason are refused
- **Given** an SGML OFX file with 10,000 nested opening tags, and one with `STMTTRN` elements missing `FITID` entirely
- **When** imported
- **Then** the first is refused as `not a statement` (depth over 64); in the second each `STMTTRN` without a `FITID` is reported unreadable (`no FITID`) and the import completes

### Categorisation

#### AC26. Rules match on description, amount and account; first match wins
- **Given** rules in order: `{name="Groceries", description_contains="TESCO"}`, `{name="Travel", description_matches="^TFL"}`, `{name="Big", account="current", amount_below="-100.00"}`, `{name="Card payments", account="card", amount_above="0"}`
- **When** `tally report --month 2026-10 --format json` on the samples
- **Then** TESCO is `Groceries`, TFL is `Travel`, COUNCIL TAX is `Big`, the card's `PAYMENT RECEIVED` is `Card payments`, a TESCO row for −150.00 is `Groceries`, not `Big`, and the savings `TRANSFER TO CURRENT` (−500.00) is not `Big` because the rule names another account

#### AC27. The user can see what no rule matched
- **Given** the rules above
- **When** `tally uncategorised`
- **Then** the salary, the refund, CAFÉ NERO, AMAZON and the two savings rows are listed with id, date, account and amount; with `--format csv` and `--format json` the same rows

#### AC28. The user can see why a transaction got its category
- **Given** the rules above and the id of the TFL transaction
- **When** `tally explain <id>`
- **Then** output names rule `Travel`, the condition that matched (`description matches ^TFL`), and the earlier rule that did not (`Groceries: description does not contain TESCO`); for an uncategorised transaction it says `no rule matched` and lists each rule's failing condition

#### AC29. A broken rule is a configuration error before anything else happens
- **Given** a rule with `description_matches = "("`, and in another case `amount_below = "ten"`, and in another a rule with no `name`
- **When** any command that reads rules runs
- **Then** refused with the rule's position and problem (`rules[2]: description_matches is not a valid pattern: missing )`), exit 2, the data file not opened

### Reports

#### AC30. Spending and income by category for a month, exactly
- **Given** the samples imported and categorised, plus three transactions of 0.10, 0.20 and −0.30 in a category
- **When** `tally report --month 2026-10`
- **Then** each category's total is the exact sum (the 0.10 + 0.20 − 0.30 category shows 0.00, never 0.0000000001), income and spending are separate columns, and the table lists totals per currency, never summing GBP with another currency

#### AC31. A range of months, and the payees taking the most
- **Given** transactions across 2026-09 to 2026-11
- **When** `tally report --from 2026-09 --to 2026-10` and `tally payees --from 2026-09 --to 2026-10 --top 3`
- **Then** the report has one section per month, both inclusive, and the payees list has the three descriptions with the largest money out, in order, with their totals and counts

#### AC32. CSV export never runs as a formula
- **Given** descriptions `=HYPERLINK(...)`, `+1`, `-1`, `@SUM`, one starting with a tab and one with a carriage return
- **When** `tally report --month 2026-10 --format csv` and `tally uncategorised --format csv`
- **Then** each such text cell is prefixed with `'`; amount cells have their currency's own decimal places, matching `^-?[0-9]+$` for a currency with none and `^-?[0-9]+\.[0-9]{n}$` for one with `n` (ADR 0011), and are not prefixed; the output parses back with the `csv` module to the same rows; line endings are `\r\n`

#### AC33. Colour never carries meaning and turns off
- **Given** a report with negative totals
- **When** run on a terminal, piped, and with `NO_COLOR=1` on a terminal
- **Then** the piped and `NO_COLOR` outputs contain no escape sequence; negative amounts carry a leading `-` in all three; the three outputs are identical once escape sequences are stripped

#### AC34. Errors go to standard error with a non-zero exit, and standard output stays clean
- **Given** each refusal in this document
- **When** it happens
- **Then** standard output is empty, standard error has one message, and the exit code is the one stated; `--format json` output on success is valid JSON with nothing before or after it

#### AC35. Every report has a plain, machine-readable form
- **Given** `report`, `payees`, `uncategorised`, `explain`, `account list`, `import`
- **When** run with `--format json`, and with `--format csv` for the tabular ones
- **Then** the JSON has the documented field names and the CSV the documented headers, both the same rows as the table; amounts are strings with their currency's own decimal places, `1200` for JPY, `1.234` for BHD, `-4.20` for GBP (never JSON numbers; ADR 0011)

#### AC36. Nothing converts between currencies
- **Given** a GBP account and an EUR account with transactions in the same month
- **When** `tally report --month 2026-10`
- **Then** the output has a GBP section and an EUR section, and no grand total

#### AC37. Impossible ranges and dates are usage errors
- **Given** `--from 2026-10 --to 2026-09`, `--month 2026-13`, `--month October`
- **When** `tally report ...`
- **Then** refused, exit 2, with the option named

#### AC38. Control characters in descriptions never reach the terminal
- **Given** a description containing an escape sequence (`\x1b]0;evil\x07`) and a backspace
- **When** shown by `report`, `uncategorised` or `explain`
- **Then** each control character is shown as `␛`-style replacement (`␛`) or `?`, and the raw byte is absent from standard output; the JSON form carries the original, escaped as JSON requires

### Added after the slice 0 review

#### AC42. An import that fails part way writes nothing
- **Given** account `current` and a data file made read-only (`0400`) after the account exists
- **When** `tally import --account current --layout bank samples/current-account.csv`
- **Then** refused: `the data file cannot be written`, exit 1; the data file is unchanged (same bytes), no transaction and no import record exists; the file's permissions are reported, not changed

#### AC43. An unknown or invalid layout is a configuration error
- **Given** `--layout nosuch`, and in another case `[layouts.bank]` missing `date`, and in another `date_format = "%Q"`, and in another `decimal = ","` with `thousands = ","`
- **When** `tally import ...` with that layout
- **Then** refused with the layout's name and the problem (`layouts.bank: no "date" column named`), exit 2, the data file not opened. A layout's `encoding` is one of UTF-8 (with or without a byte order mark), cp1252, Latin-1, Latin-9 and ASCII; any other is `encoding "..." is not supported`

#### AC44. Amounts in every form a statement uses, exactly
- **Given** a layout with `decimal = "."` and `thousands = ","`
- **When** these amount texts are read: `1,234.56`, `(12.50)`, `-12.50`, `12.50-`, `£12.50`, `-£12.50`, `£-12.50`, `0`, `.5`, `1,234`
- **Then** they are 1234.56, −12.50, −12.50, −12.50, 12.50, −12.50, −12.50, 0.00, 0.50, 1234.00 as exact decimals; and `12.50 CR`, `12.50 DR`, `1,2,3.4`, `12.5.0`, `,`, `--12.50`, `(12.50` and an empty string are unreadable with the reason `amount is not a number`. With `decimal = ","` and `thousands = "."`, `1.234,56` is 1234.56 and `1,234.56` is unreadable

## Three checks of the criteria

**From the security boundaries:** money is exact: AC30 and the rule check; no network: the rule check and ADR 1 (no runtime dependency), with AC39 covering the one parser that could fetch; nothing personal in messages: AC19 and AC34; the data file is the user's alone: AC1 and AC6; statement files are hostile: AC10 to AC13, AC17, AC18, AC25, AC32, AC38 to AC41; two copies at once: AC7 and AC8; a failed import writes nothing: AC42; configuration errors stop before the data file opens: AC29 and AC43; money parsing: AC44. No boundary is without a criterion.

**The four questions, asked of the design:**

- *What does another parser do with this input?* A spreadsheet opening the export (AC32: formula prefix on text cells, amounts constrained to a number shape). The terminal (AC38: control characters). SQLite (every statement is parameterised; a review item, since no grep proves it). The XML parser (AC39: no DOCTYPE, so no entity expansion or external fetch). Python's `csv` module (AC12, AC40: quoted fields and a field-size limit so one field can't take all memory). The regex engine on the user's own patterns (AC29: compiled once, errors named; a pathological pattern costs the user their own CPU, accepted; a pattern searches only a description's first 1,000 characters, because an ordinary unanchored pattern such as `TESCO.*EXPRESS` backtracks quadratically over a hostile 1 MB description, minutes per row on every report, found by the whole-codebase audit). `strptime` (AC13: `31/02/2026` is unreadable, not February 31st normalised). The date in a report's own `--month` (AC37).
- *What happens when two of these run at once?* Two imports (AC7). A copy that cannot get in (AC8). A report during an import reads the committed state (WAL). Two `account add` of the same name: the `UNIQUE` constraint, so one is refused (AC2).
- *What does a flood cost?* A huge file: streamed, memory bounded (AC18). One huge field (AC40). A deeply nested OFX (AC41). Rows that are all unreadable: each costs one message, and the summary is bounded; standard error lists at most 50 line reasons and then a count (AC13). A rules file with thousands of rules: linear per transaction, accepted.
- *What can one account do to a result other people rely on?* Nothing shared exists: one user, one machine, no network. The analogue inside Tally is one account's figures leaking into another's: currencies are never summed across accounts (AC36), and `forget` removes exactly one account's rows (AC3).

**From the lifecycles:** every refused move is a criterion: add a taken name (AC2), import into or forget an absent account (AC5, AC4), import a wrong currency (AC24), a failed import writes nothing (AC17, AC8, AC42), an unknown layout is a configuration error (AC43), a second copy waits then stops (AC8), a category is derived in one place (AC26 to AC28).

## Test tiers

Tier 1 only, in-process against the real data file and real files, with one subprocess test per behaviour group for the process contract (exit codes, standard error, `NO_COLOR`, two processes). Recorded in [ADR 0002](decisions/0002-acceptance-tests-at-tier-one.md). No device checks: nothing in Tally is observable only on a device.

## Slices

| Slice | Delivers | Criteria |
| --- | --- | --- |
| 0 | Foundation: this design, CI, samples | none (gates proved by breaking) |
| 1 | Accounts and the data file: config loading, `account add/list/forget`, store with permissions and locking, `Money` | AC1 to AC8, AC44, AC34 (for these), AC35 (`account list`) |
| 2 | CSV import with layouts, dedupe, unreadable rows, the import summary | AC9 to AC20, AC40, AC42, AC43 |
| 3 | OFX import, SGML and XML | AC21 to AC25, AC39, AC41 |
| 4 | Categorisation: rules, `uncategorised`, `explain` | AC26 to AC29, AC38 |
| 5 | Reports: `report`, `payees`, CSV and JSON export, colour | AC30 to AC33, AC35 to AC37 |
