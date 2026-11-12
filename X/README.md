# Tally

Tally is a command-line tool for people who want to see where their money goes
without handing their bank statements to anyone. It imports the statement files
banks let you download, removes duplicates, sorts transactions into categories
by rules you write, and reports spending by month.

It runs entirely on your own computer and never makes a network connection.

## Install

Tally needs Python 3.15 and installs as a single command, `tally`.

```bash
uv tool install git+https://github.com/<owner>/tally
```

Or, from a clone of this repository:

```bash
uv sync
uv run tally --help
```

## Quick start

```bash
tally init
```

That writes a commented configuration to `~/.config/tally/config.toml` with two
CSV layouts matching the files in `samples/` and a handful of example rules.
Then:

```bash
tally account add current --currency GBP
tally account add card --currency EUR
tally account add savings --currency GBP

tally import samples/current-account.csv --account current --layout current-account
tally import samples/credit-card.csv --account card --layout credit-card
tally import samples/savings.ofx --account savings

tally report
tally payees --top 5
tally uncategorised
```

Every account has one currency. Tally never converts between currencies;
reports show each currency separately.

## Commands

| Command | What it does |
| --- | --- |
| `tally init` | Write a starting configuration file, if there is not one already. |
| `tally check` | Check the configuration file for mistakes and list its layouts, rules and categories. |
| `tally account add NAME --currency CODE` | Create an account. `--decimals N` overrides the currency's usual number of decimal places. |
| `tally account list` | Accounts, with how many transactions each holds and the date range. |
| `tally import FILE --account NAME [--layout NAME]` | Import a statement. CSV files need a layout from the configuration; OFX files describe themselves. |
| `tally transactions` | List transactions with their categories and ids. `--uncategorised`, `--category NAME`. |
| `tally uncategorised` | Transactions that no rule matched. |
| `tally explain ID` | Why a transaction got its category: which rule matched and why the earlier ones did not. |
| `tally report` | Spending and income by category for each month. `--exclude CATEGORY` leaves a category out, for example transfers between your own accounts. |
| `tally payees` | The payees taking the most money. `--top N` (default 10, 0 for all). |
| `tally imports` | Import history: when, which file, into which account, and how many rows were added, already there or skipped. |
| `tally undo ID` | Remove the transactions one import added, leaving everything else. Asks first; `--yes` skips the question. |
| `tally forget NAME` | Delete an account and everything imported into it. Asks first; `--yes` skips the question. |

`transactions`, `uncategorised`, `report` and `payees` take `--account NAME`,
`--month YYYY-MM`, or `--from YYYY-MM` and `--to YYYY-MM`.

Global options, accepted before or after the command: `--config PATH`,
`--data PATH`, `--color auto|always|never`.

## Importing

Tally reads:

- **CSV** in whichever layout your bank uses. You describe the layout once in
  the configuration: which columns hold the date, the description and the
  amount (or separate money-in and money-out columns), the date format and the
  decimal and thousands separators. See [docs/configuration.md](docs/configuration.md).
- **OFX**, both the older SGML form with unclosed tags (`.ofx`, `.qfx`, OFX 1.x)
  and the XML form (OFX 2.x).

The file type is taken from the extension, or from the content when the
extension is unfamiliar; `--type csv|ofx` overrides it.

Byte order marks, Windows line endings, UTF-8, UTF-16, Windows-1252 and Latin-1
are all handled without being told. Amounts such as `1,234.56`, `(12.50)`,
`-4,20`, `£12.50`, `12.50 CR` and `12.50-` are read exactly, as whole numbers of
pence or cents. Floating point is never used for money.

### What the import reports

```
Imported current-account.csv into current: 5 added, 0 already there, 0 skipped.
  note: line 4: the row had more fields than the header; the extra text was treated as part of the description
  skipped line 9: could not read the date (expected the format %d/%m/%Y)
  Run with --show-rows to see the values that could not be read.
```

Rows that cannot be read are skipped and reported with their line number and
the reason. The offending values are not shown unless you ask with
`--show-rows`, so nothing from your statement appears on screen or in a
scheduled job's log by accident. With `--strict`, a single unreadable row means
nothing at all is imported.

If the layout names the export's running `balance` column, Tally also checks
that every row's balance follows from the previous one and the amount it
read, and notes any row that does not. That is the quickest way to find a
layout with the wrong sign or column, or a statement with rows missing.

Problems with the whole file (not a statement, wrong columns for the layout,
wrong currency for the account, not readable text) stop the import before
anything is written.

### Duplicates

Importing the same file twice, or two statements whose dates overlap, never
counts a transaction twice. OFX files carry the bank's own transaction id
(`FITID`), which Tally uses directly. For CSV, a transaction is recognised by
its date, amount, description (ignoring case and spacing) and position among
identical rows in the file, so two genuine identical purchases on the same day
are both kept while a re-import of either statement adds neither. All of this
is enforced inside the data file, so two copies of Tally importing at once
cannot slip a duplicate through.

The one case Tally cannot see through is a bank changing the wording of a
transaction's description between two exports of the same period. Check the
import summary when that happens; `tally imports` shows what each import
added, and `tally undo ID` removes exactly those rows.

## Categories

Rules live in the configuration file and are applied when you look, so editing
a rule re-categorises everything at once. Each rule names a category and
matches on any combination of the description (text or a regular expression),
the amount (a range) and the account. The first matching rule wins.

```toml
[[rules]]
category = "Groceries"
pattern = "TESCO|SAINSBURY|ALDI"
amount_max = "0"

[[rules]]
category = "Transfers"
description = "TRANSFER TO"
account = "savings"
```

`tally uncategorised` shows what no rule matched. `tally explain ID` shows,
for one transaction, every rule that was tried and the first condition that
failed, up to the rule that matched.

## Output

Tables are for people. `--format csv` and `--format json` are for machines
and spreadsheets, and every report has them. `--output PATH` writes to a file
instead of standard output.

- Meaning never depends on colour: negative amounts carry a minus sign, totals
  are labelled `Total`. Colour is only used on a terminal, and never when
  `NO_COLOR` is set or `--color never` is given.
- Errors go to standard error, with a non-zero exit code.
- CSV exports protect you from spreadsheet formula injection: a cell that
  starts with `=`, `+`, `-`, `@`, `|`, a tab or a carriage return is prefixed
  with an apostrophe, which spreadsheets treat as "this is text". Plain numbers
  are left alone so they stay numbers.
- Amounts in CSV and JSON are plain decimal strings such as `-42.17`, never
  floats.

Exit codes: `0` success, `1` error (nothing was changed), `2` wrong usage,
`3` the command finished but some rows could not be read.

## Where things live

| What | Default | Override |
| --- | --- | --- |
| Configuration | `~/.config/tally/config.toml` (`$XDG_CONFIG_HOME/tally/`) | `--config PATH` or `TALLY_CONFIG` |
| Data | `~/.local/share/tally/tally.db` (`$XDG_DATA_HOME/tally/`) | `--data PATH`, `TALLY_DATA`, or `data_file` in the configuration |

The data file is a single SQLite database created readable only by you
(mode `0600`, in a directory of mode `0700`), and Tally tightens the
permissions if it finds them loosened. `tally forget` removes an account's
data and compacts the file so the deleted rows do not linger in it.

Two copies of Tally may run at once, for instance an import started twice by a
scheduled job. The second waits for the first (up to thirty seconds) and then
reports the rows as already there.

## Development

```bash
uv sync
uv run pytest            # about 15 seconds, including a 100,000-row import
uv run ruff check src tests
uv run ruff format src tests
```

Design decisions and the reasoning behind them are in
[docs/decisions.md](docs/decisions.md).

## Out of scope

A graphical interface, bank connections, investments, budgets, multiple users,
and syncing between computers.
