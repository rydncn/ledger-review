# Configuration

Tally's configuration is one TOML file, edited by hand. `tally init` writes a
commented starting point to `~/.config/tally/config.toml`
(`$XDG_CONFIG_HOME/tally/config.toml` if that is set). Point Tally elsewhere
with `--config PATH` or the `TALLY_CONFIG` environment variable. `tally check`
reports mistakes with the layout or rule they are in.

Only three things may appear at the top level: `data_file`, `[layouts.*]`
tables and `[[rules]]` tables. Anything else is an error, so a typo cannot
silently do nothing.

## `data_file`

```toml
data_file = "~/Documents/money/tally.db"
```

Where the SQLite data file lives. Optional. `--data PATH` and `TALLY_DATA`
override it; the default is `~/.local/share/tally/tally.db`.

## CSV layouts

Each bank export format gets one `[layouts.NAME]` table, then
`tally import FILE --account A --layout NAME`.

```toml
[layouts.current-account]
date = "Date"
description = "Description"
money_out = "Money Out"
money_in = "Money In"
date_format = "%d/%m/%Y"
decimal_separator = "."
thousands_separator = ","
```

| Setting | Required | Default | Meaning |
| --- | --- | --- | --- |
| `date` | yes | | Column holding the date. |
| `description` | yes | | Column holding the payee or description. |
| `amount` | one of these | | Column holding a signed amount: negative is money out. |
| `money_out`, `money_in` | two forms | | Two columns, one for each direction. Each holds a magnitude; a minus sign or brackets in either is ignored and noted, because the column already says which way the money went. If both are filled on one row, the row's amount is in minus out. |
| `memo` | no | | An extra text column kept alongside the description. Rules match on description and memo together; payee reports group by description only. |
| `balance` | no | | The running balance column, if the export has one. Tally then checks that each row's balance follows from the previous row's balance and the amount it read (statements may run oldest-first or newest-first; the first pair of rows that can tell decides). A row that does not follow is noted by line number, which catches a layout with the wrong sign or column and statements with rows missing. Rows are imported either way. |
| `date_format` | no | `%d/%m/%Y` | `strftime` codes. `%m/%d/%Y` for US-style dates, `%Y-%m-%d` for ISO, `%d %b %Y` for `05 Oct 2026`. |
| `decimal_separator` | no | `.` | One character. |
| `thousands_separator` | no | `,` | One character, or `""` for none. Grouping is checked: with `,` as thousands separator, `4,20` is rejected rather than read as 420. |
| `delimiter` | no | `,` | Field separator: `;` for many European exports, `"\t"` for tab. |
| `quotechar` | no | `"` | Quote character. |
| `encoding` | no | `auto` | Normally detected (byte order mark, then UTF-8, then Windows-1252, then Latin-1). Name one, such as `"latin-1"` or `"utf-16"`, only if a file reads wrongly. |
| `header` | no | `true` | Whether the first line names the columns. With `header = false`, every column must be given as a 0-based number. |
| `skip_lines` | no | `0` | Lines to ignore before the header, for exports with a preamble. |
| `negate` | no | `false` | Flip the sign of every amount, for banks that show spending as positive. |

Columns may be header names (matched after trimming, then case-insensitively)
or 0-based column numbers. Give either `amount` or both `money_out` and
`money_in`, not both forms.

Amounts may carry a currency mark or code (`£12.50`, `12.50 GBP`), brackets for
negatives (`(12.50)`), a trailing minus (`12.50-`), or `CR`/`DR` suffixes.
More decimal places than the account's currency allows is an error for that
row, not a rounding.

A row with more fields than the header is repaired by folding the surplus into
the description, since the description is the only free-text column; the import
notes the line. A row with fewer fields than the layout needs is skipped and
reported.

## Rules

Rules are `[[rules]]` tables, tried in file order; the first that matches
decides the category. A transaction no rule matches is `Uncategorised`.

```toml
[[rules]]
category = "Eating out"
pattern = "CAF[EÉ]|NERO|PRET"
amount_max = "0"

[[rules]]
category = "Transfers"
description = "TRANSFER TO"
account = "savings"
```

| Setting | Meaning |
| --- | --- |
| `category` | Required. The category to assign. |
| `description` | Text that must appear in the description (or memo), ignoring case. |
| `pattern` | A regular expression (Python syntax) that must match somewhere in the description (or memo), ignoring case. |
| `amount_min`, `amount_max` | Inclusive bounds on the signed amount, in the account's currency. Money out is negative, so `amount_max = "0"` means "spending only" and `amount_min = "0.01"` means "income only". Write amounts as strings so they stay exact; up to four decimal places are accepted, and the comparison is exact whatever the account's currency uses. |
| `account` | The account name the transaction must belong to (case-insensitive). |

Every rule needs at least one condition; all its conditions must hold for it to
match. `tally explain ID` shows, for one transaction, each rule tried and the
first condition that failed.

Changing rules changes every report immediately: categories are worked out
when you ask, not stored.
