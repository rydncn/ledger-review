# Decisions

Judgement calls made while building Tally, with the reasoning, so they can be
revisited deliberately rather than rediscovered.

## Money is integer minor units

Amounts are stored and summed as integers of pence or cents, with the number of
decimal places fixed per account from its currency (2 for most, 0 for JPY, 3
for KWD). Parsing goes straight from text to an integer with no intermediate
`float` or even `Decimal`, and totals are integer sums. A value with more
decimal places than the currency allows is a skipped row, not a rounding.

## Categories are computed, not stored

Rules are the single source of truth. Applying them at report time means
editing a rule re-categorises history with no extra command and no stale
state. At personal scale (a hundred thousand transactions and tens of rules)
this takes a second or two, which is acceptable. If it ever is not, a cache
keyed by a hash of the rules file is the obvious next step.

## Duplicates: bank id first, then content with an occurrence counter

OFX carries `FITID`, which the standard says is unique within an account, so
that is used directly. CSV has nothing of the kind, so a transaction is
identified by date, amount, normalised description, and its position among
identical rows in the same file. The counter is what lets two identical
coffees on one day both survive while a second import of either statement adds
neither. The constraint is enforced by a `UNIQUE` index in SQLite so that even
two processes racing cannot double-count. The known blind spot is a bank
rewording a description between exports; this is documented in the README
rather than guessed around.

## Brackets and minus signs in dedicated money-in/money-out columns

In the sample current-account file, `(3.40)` sits in the Money Out column and
the Balance column drops by 3.40. So in a two-column layout the column decides
the direction and signs or brackets are the bank's presentation. Tally uses
the magnitude and notes the line so the user can see it happened. In a single
signed `amount` column, brackets and minus signs mean negative as usual.

## Over-wide CSV rows are folded into the description

The sample file's `=HYPERLINK("http://example.invalid","Refund")` row contains
an unquoted comma, so a strict reader sees six fields under a five-column
header. The Balance column shows it is a real credit of 12.50. Since the
description is the only free-text column in any layout, surplus fields are
rejoined into it and the import notes the line. The alternative, skipping the
row, loses a real transaction silently. A row with too few fields is still
skipped and reported, since there is no safe repair.

## Formula injection: apostrophe prefix, numbers exempt

CSV exports prefix any cell whose first non-blank character is `=`, `+`, `-`,
`@`, `|`, tab or carriage return with an apostrophe, following OWASP's
guidance. Cells that are plain numbers (`-42.17`) are exempt so that amounts
stay numeric in the spreadsheet; the risk is in text, not numbers.

## Privacy of messages

Error and skipped-row messages name the file, line, column and reason, never
the row's contents. `--show-rows` is the explicit opt-in. Configuration
contents (rule text, layout names) can appear in messages, since they are the
user's own settings rather than statement data.

## The running-balance check warns, it does not reject

When a layout names the balance column, each row is checked against its
neighbour. A mismatch is reported by line number only, and the row is still
imported, because the amount is what the bank said and the mismatch is more
often a missing row or a wrong layout setting than a wrong amount. The import
summary ends with a count so a scheduled job's log shows it. Statement order
(oldest-first or newest-first) is inferred from the first pair of rows that
can tell the two apart, rather than configured, since banks do not say.

## Undo removes what an import added, nothing more

`tally undo ID` deletes the rows whose `import_id` is that import. Rows the
import found already present belong to the earlier import that added them
and stay. So undoing a re-import of the same file removes nothing, and
undoing the later of two overlapping statements leaves the overlap in place.
After an undo the same file imports as new, which is the point: a wrong
layout or a wrong account is corrected by undo, fix, import again.

The other direction is the subtle one: undoing an *earlier* import removes
rows that a later overlapping statement also contained and was told were
"already there". Tally does not record which later import saw which row, so
it warns before the undo when later imports into the account reported
duplicates, and says to import those statements again to restore the rows.

## Process-wide `umask 077`

Every file Tally writes (database, journal, exports via `--output`) is created
readable only by the user. That includes exports, which seems right for
financial data; a user who wants a shared export can `chmod` it.

## Report CSV omits totals

A spreadsheet user will total the rows themselves; including `Total` lines
would double-count under a pivot. JSON carries `rows` and `totals` separately.
Tables show totals per month and currency in bold.

## Exit code 3 for partial imports

An import that skipped rows still commits the good rows (unless `--strict`),
but a scheduled job should notice, so it exits 3 rather than 0. Exit 1 always
means nothing was changed.

## Licence

The project is MIT licensed. This was a reasonable default for a personal tool
published on GitHub; it is the owner's to change.

## Not done, deliberately

- OFX files are read whole into memory. Bank OFX exports are small; CSV, which
  can be very large, streams.
- No manual category overrides. The brief asked for rules; a rule on the exact
  description does the same job.
