# Blind review of two Tally builds, X and Y

Both builds were judged against the Tally brief using the same commands, the same files, the same order of work and the same standard of evidence. I worked on X and Y alternately: X's run came first and Y's straight after, step by step.

What's in `review/`:

- [`hostile.py`](hostile.py) is the section 2 script. I wrote it before reading either build's code, using only the READMEs, `--help` and the docs. It builds every input in a fresh temporary directory, with HOME and XDG pointed inside it.
- `results-X.json` and `results-Y.json` hold its raw output.
- [`probes.py`](probes.py) holds 18 probes of findings from reading the code. Each one ran identically on both builds; the output is in `probes-X.json` and `probes-Y.json`.
- [`mutations.py`](mutations.py) breaks the same six behaviours in each build, one at a time, and runs that build's own test suite.

The environment was Python 3.15.2 (installed by uv 0.13.7) on Linux with 4 cores and 16 GB of memory. Wherever this report makes a claim, it gives the command or the code line behind it.

## 1. Their own tests

| | **X** | **Y** |
| --- | --- | --- |
| Install as the README says | `uv tool install git+https://github.com/<owner>/tally` fails, because the URL is a placeholder (it may be one of the redacted lines). The README's other route, `uv sync` then `uv run tally --help`, works. `uv tool install ./X` also works. | `uv tool install .` works. `scripts/setup-dev.sh` (`uv sync --locked --no-build`) works. |
| README quick start | All of it works as written: `tally init`, three `account add`, three imports of the samples, `report`, `payees`, `uncategorised`. | No quick start. The README never says how to write a configuration (layouts, rules); that is only in `docs/design.md`'s acceptance criteria. |
| Tests ran / passed | 184 collected, **184 passed**, 0 failed. | 619 collected, **618 passed, 1 skipped** (it needs a non-root user), 0 failed. |
| Time | 15.9 s, run as `uv run pytest` (16.8 s wall). The README says "about 15 seconds". | 78.1 s, run as `uv run pytest`. 56.1 s run as `uv run pytest -n auto`, which is how CI runs it. |
| Would CI run them? | Yes, as a standalone repository: on push to main and on pull requests, `ruff check`, `ruff format --check` and `pytest` run on Ubuntu, macOS and Windows. The lint and format steps pass locally. | Yes, the "Tests" job runs `pytest -n auto` and then checks that tests passed. But the separate "Format, lint and types" job **would fail**: `ruff check .` reports `E501` at `scripts/check_imports.py:1`. The other steps in that job pass: `ruff format`, `mypy`, the rule check, the import check and `check_lock.py`. I didn't run the mutation and security jobs (they need GitHub and downloads). |
| Note for both | The workflows sit in `X/.github/` and `Y/.github/`, not at the root of this repository, so GitHub won't run either of them here. | (same) |

## 2. The same hostile inputs for both

The commands are in `hostile.py`, and the full output, item by item, is in `results-*.json`. Three corrections to what the script first reported:

- During the first run I fixed three checks of my own that were wrong in the same way for both builds:
  - Item 2 used the brief's malformed HYPERLINK row, so it was measuring item 4's problem.
  - Item 6 demanded a total row.
  - Item 8 flagged `|`, which isn't in the brief's list.
- I added one OFX case to item 10 (a truncation at a tag boundary).
- For item 12 I override the script's "pass" on Y, for the reason given in the table.

| # | Input | **X** | **Y** |
| --- | --- | --- | --- |
| 1 | Same file twice | **pass**. First import: `5 added, 0 already there`. Second: `Note: this file was imported into cur before … 0 added, 5 already there`. 5 rows in the data file. | **pass**. `added 4 … unreadable 1`, then `added 0, already there 4, unreadable 1`. 4 rows: the 5th is the HYPERLINK row, refused (item 4). |
| 2 | Overlapping statements | **pass**. `3 added`, then `2 added, 2 already there`. 5 rows. | **pass**. `added 3`, then `added 2, already there 2`. 5 rows. |
| 3 | Two imports of one 2,000-row file started together (3 trials) | **pass**. Each trial: both exit 0, `added=[0, 2000]`, 2,000 rows, `integrity_check=ok`. | **pass**. Each trial: both exit 0, `added=[2000, 0]` or `[0, 2000]`, 2,000 rows, `integrity_check=ok`. |
| 4 | The brief's three samples, byte for byte | **pass**. All 10 transactions are exact, including `(3.40)` under Money Out read as −3.40, `CAFÉ NERO` −4.20 on 2026-10-02 and the HYPERLINK refund of +12.50. That row's description is stored as `=HYPERLINK("http://example.invalid",Refund)`: the inner quotes are lost, because the cell is not valid CSV. The output notes `line 4: the row had more fields than the header`. | **fail**. 9 of 10. `line 4: expected 5 columns, found 6`, so the £12.50 refund is not imported. Y's own `samples/current-account.csv` was edited to quote that cell (its test docstring says "recorded in the plan"). The other 9 are exact. |
| 5 | `01/02/2026` with `%d/%m/%Y` | **pass**. Stored as 2026-02-01. `02/13/2026` is refused: `skipped line 3: could not read the date`. | **pass**. Stored as 2026-02-01. `line 3: date does not match %d/%m/%Y`. |
| 6 | 100 × 0.10 | **pass**. `10.00` in every report, in table, CSV and JSON forms (category and payee), with no float artefacts. The table and JSON also give a total of `20.00`. | **pass**. `-10.00` / `10.00` in every form. There is no total row in any form. |
| 7 | GBP and EUR accounts | **pass**. Each currency gets its own rows and totals. No `15.00`, `3.70` or `11.30` (the cross-currency sums) appears in any output. | **pass**. Same check: separate sections, no cross-currency figure. |
| 8 | `=`, `+`, `-`, `@`, tab and CR at the start of a description, exported to CSV | **pass**. Every dangerous cell is prefixed with `'`. Numbers like `-1.00` are left alone. A leading tab or CR is stripped at import, so it never reaches the export. `\|calc` is also prefixed. | **pass**. Every dangerous cell is prefixed, and a leading tab or CR is stripped at import. `\|calc` is not prefixed, which the brief doesn't ask for. |
| 9 | 210 MB CSV (2,917,101 rows) | **fail**. Exit 0, all rows stored, 97 s, **peak RSS 994 MB**. | **fail**. Exit 0, all rows stored, 78 s, **peak RSS 503 MB**. |
| 9 (cause) | | The cause is the per-file occurrence `Counter` (`importers/dedup.py:72`). For the same 49 MB import: 265 MB with all rows distinct, 23 MB with all rows identical. | The cause is the per-file `seen` dict (`importers/csv_layout.py:291`). Same test: 143 MB distinct, 23 MB identical. |
| 10 | Empty, binary, JSON, truncated OFX ×2, XML OFX, wrong declared encoding ×2 | **fail**. Empty, binary and JSON are refused with exit 1 and a clear stderr message. The XML form is read exactly. A wrong declared encoding is read correctly, but only by luck (see probe P5). The truncated OFX gives exit 3 with `skipped line 11: the transaction has neither NAME nor MEMO`, on **stdout**, and never says the file is truncated. The cut at a tag boundary imports 1, again with the reason on stdout. Probe P18: an XML OFX declaring an unknown encoding gives a **traceback** (`ValueError: unknown encoding 'no-such-codec' in layout`). | **fail**. Everything is refused with exit 1 and one stderr line: `empty file`, `not a text file`, `header has no column named "Date"`, `not a statement: the file ends inside a tag`, `not a text file: not valid utf-8`. The XML form is read exactly. But the cut at a tag boundary **exits 0**, imports 1, and says only `transaction 2: no FITID`. |
| 11 | Piped, `NO_COLOR`, stderr, machine-readable output | **pass**. No escapes when piped or with `NO_COLOR`, checked on a real pty. On a tty only bold is used, and the text is identical once escapes are stripped. Errors go to stderr with a non-zero exit and an empty stdout. JSON exists for `report`, `payees`, `uncategorised` and `account list`; `explain` has no JSON. | **pass**. Same results. On a tty only red is used, and only on amounts that already carry `-`. JSON exists for every command, including `import` and `explain`. |
| 12 | Permissions | **pass**. Under umask 022: `tally.db` is 0600, the directory 0700, a `--output` export 0600 (because `main` sets umask 077). A file loosened to 644 is tightened back to 600. | **fail (export)**. The data file is 0600, the directory 0700, and a file loosened to 644 is tightened with a message. But Y has no export option, so the only way to export is a shell redirect, and that file is **0644** (probe P17). |
| 13 | `forget` | **pass**. No marker bytes in the file, and `strings` finds none. The other account is untouched. | **pass**. Same, including no `-wal` left behind. |
| 14 | No network | **pass**. Every command works under `unshare -n`. There's no network import or socket call in `src/`, and `dependencies = []`. | **pass**. Same. |
| 15 | Nothing personal in messages | **pass**. Eight kinds of error, with no description or amount marker on stderr or stdout and no log files. X has no verbose mode; `--show-rows` shows values only when asked. | **pass**. Same eight errors, with `--verbose`: no markers. The verbose log names the file path and its encoding. |
| 16 | Two matching rules | **pass**. `Category: Groceries, from rule 1 … Later rules were not tried`. With the rules reversed, it's `Shopping`. An uncategorised transaction lists each rule's failing condition. | **pass**. `category Groceries, from rule 1`. Reversed, `Shopping`. Uncategorised: `no rule matched` plus each rule's failing condition. |

**Tally:** X passes 14 and fails 2 (items 9 and 10). Y passes 12 and fails 4 (items 4, 9, 10 and 12).

## 3. The code

I tried to disprove each finding below, and each was proved by a command (the probe is named) or checked against the line cited. A few suspicions didn't survive:

- X does not store dates in 1900 for a `date_format` with no year (P3): each row is reported.
- X's VACUUM-after-forget held up with another connection open (P8).

**Must fix** means wrong results the user relies on, or hostile input reaching something outside Tally. **Should fix** is everything else worth doing.

### X

#### Correctness: 3 / 5

- **Must fix.** Folding surplus fields into the description silently corrupts amounts (`importers/csvfile.py:119-123`, `158-171`). This is P1:
  - `05/10/2026,SHOP,-1,234.56` is stored as **+234.56** with the description `SHOP,-1`.
  - `…,SALARY ACME LTD,,2,450.00,…` is stored as **448.00**.
  - Both exit 0, with only a "note" on stdout.

  The feature exists to read the brief's HYPERLINK row, and its own tests assert the folding. So a spending of £1,234.56 becomes income of £234.56.
- **Should fix.** A FITID that the bank reused for a different transaction is silently counted as "already there", because the fingerprint is `id:`+FITID alone (`importers/dedup.py:65-66`). P7: a second file with the same FITID but a different date and amount reports `0 added, 2 already there`, and −75.00 on 2026-11-15 is lost.
- **Should fix.** A truncated OFX isn't detected: at end of file, `finish()` closes whatever was open (`importers/ofx.py:166-169`). It exits 3 with a misleading reason (item 10).
- **Should fix.** An XML OFX declaring an unknown encoding crashes with a traceback (P18). The `ValueError` raised in `encoding.py:139` isn't caught on the OFX path (`importers/ofx.py:76`), and its message says "in layout".
- **Should fix.** When the declared encoding fails to decode, X silently falls back to Latin-1 (`importers/ofx.py:77-80`). P5: an OFX declared UTF-8 but holding cp1252 `€` is stored as `'\x80'`, exit 0, no message.
- **Should fix.** A currency code or symbol in an amount is stripped without checking it against the account (`money.py:319-323`). P4: `-12.50 USD` and `$-3.00` are imported into a GBP account as GBP.
- **Should fix.** One field over 1 MB rejects the whole file (`csvfile.py:23`, `146-150`). P13: `line 3: the file is not readable as CSV`, and the two good rows aren't imported.

#### Privacy and security: 3 / 5

What X does well:

- Integer minor units throughout (`money.py`, `store.py`).
- Data file 0600 and directory 0700, tightened on open, and umask 077 for exports (`cli.py:722`).
- `forget` runs VACUUM.
- No network.
- No values in messages unless `--show-rows`.
- No XML parser, so no entity expansion.

The findings:

- **Must fix.** Descriptions are printed to the terminal raw. Nothing in `output.py` or `cli.py` replaces control characters. P2: a statement description holding `ESC ] 0 ; … BEL` and `ESC [ 2 J` reaches stdout unchanged in `uncategorised`, `payees` and `explain`. A hostile statement can clear the screen, retitle the window, hide text from a screen reader, or on some terminals write to the clipboard.
- **Should fix.** The OFX tokeniser is quadratic (`importers/ofx.py:22`). Its alternatives `<\?.*?\?>` and `<!--.*?-->` rescan to the end of the file for every unterminated opener. P6: a 200 KB OFX holding 100,000 `<?` takes **84 s**, and the time grows with the square of the size.
- **Should fix.** Memory grows with the number of distinct rows, at about 5× the file's size (item 9: 994 MB for 210 MB).
- **Should fix.** An OFX file is read whole into memory (`importers/ofx.py:71`, then a full decode). P10: 169 MB peak for a 33 MB OFX.

#### Usability and accessibility: 4 / 5

What X does well:

- The help is clear, and exit codes are documented in `--help` and the README.
- `init` writes a commented configuration that matches the samples, and `check` validates it.
- `transactions` lists ids for `explain`.
- `--color auto|always|never` and `NO_COLOR` are honoured, `TERM=dumb` too, and only bold is used.
- Errors carry a hint for what to do next.
- `--output` and `--format csv|json` cover the reports.
- `| head` exits quietly.

The findings:

- **Should fix.** Unreadable-row reasons go to **stdout** inside the import summary (`cli.py:413-444`). The exit code (3) is right, but the brief puts errors on stderr, and a script reading stdout gets them mixed in.
- **Should fix.** `explain` has no `--format json` (item 11).

#### Maintainability: 3 / 5

What X does well:

- Small modules with one job each.
- `docs/decisions.md` explains the choices, and `docs/configuration.md` is a complete reference.
- 184 tests in 16 s.
- CI on three operating systems.

The findings:

- **Should fix.** The end-to-end sample test relies on the fold (`tests/test_cli.py:111-118` expects the folded HYPERLINK description), and no test feeds it a split amount, so the corruption in P1 is invisible to the suite. Nothing tests terminal sanitisation, OFX truncation or a reused FITID.
- **Should fix.** In `mutations.py`, the suite caught **5 of 6** regressions. It did not notice when `forget` stopped compacting the file (`store.py:249-250` removed): the brief's "nothing left" property is untested.

**X: 2 must fix, 13 should fix.**

### Y

#### Correctness: 4 / 5

- **Must fix.** `explain` needs an id, and only `uncategorised` prints ids (`cli.py`: the commands are `account`, `import`, `uncategorised`, `explain`, `report` and `payees`; the report and payee JSON carry no ids). For any transaction a rule *did* match, there's no way to find the id to ask why it got its category, short of guessing the integers. The brief asks for exactly that.
- **Should fix.** The brief's own sample row 4 is refused (item 4). The exact column-count check (`importers/csv_layout.py:266`) is safe, and it's reported clearly, but the brief says to read that file. Y edited its copy of the sample instead, so its tests never see the brief's bytes. A repair that re-parses the joined cell, accepted only when the amount columns still parse and the balance follows, would read it without X's failure.
- **Should fix.** A truncated OFX exits 0 (`importers/ofx.py:258-262`). `finish()` closes the open aggregates without checking `ended`. Item 10: it imported 1 and said only `transaction 2: no FITID`.
- **Should fix.** Unreadable rows still exit 0 (`cli.py:363`). P16: a scheduled job can't tell a partial import from exit codes alone.
- **Should fix.** A `$` (any Unicode currency symbol) in an amount is accepted in a GBP account (`money.py:439`). P4: `$-3.00` is stored as −3.00 GBP. Narrower than X, which also strips a trailing code.

#### Privacy and security: 4 / 5

What Y does well:

- `Decimal` built from text, integer minor units, and an overflow guard (`money.py`).
- Control characters become pictures everywhere, including argparse errors and logs (`cli.py:111-115`). P2 passes.
- OFX is a streaming scanner with a 1 MB cap per piece, a depth of 64, and DOCTYPE refused. P6 runs in 0.1 s; P10 peaks at 22 MB.
- CSV lines and records are capped (`csv_layout.py:200-239`). P13 skips only the long row.
- The data directory is refused if it's another user's or writable by others (P15). Side files are checked as regular files with one link.
- `secure_delete=ON`.
- A reused FITID is reported (P7). Mis-declared OFX encodings are refused (P5). No tracebacks in 150 fuzzed files (P11).

The findings:

- **Should fix.** Memory grows with the number of distinct rows, at about 2.4× the file's size (item 9: 503 MB for 210 MB).
- **Should fix.** After `forget`, the forgotten rows stay in `tally.sqlite-wal` for as long as another connection holds the file open. P8: 3,000 marker copies while it was open, none once it closed. A `wal_checkpoint(TRUNCATE)` after the delete would end that.
- **Should fix.** Exports can't be made private, because there's no `--output` and Y doesn't set its own umask (item 12, P17).

#### Usability and accessibility: 3 / 5

What Y does well:

- A strict split between stdout (data) and stderr (messages).
- One-line refusals with precise reasons.
- JSON for every command.
- Colour only adds red to a `-` that is already there.
- Long payee names are shortened in the table.

The findings:

- **Should fix.** The README tells a user nothing about configuration: no layout or rule keys, no example, no list of commands, and no `init`. The keys only appear inside acceptance criteria in `docs/design.md`.
- **Should fix.** A Latin-1 file whose layout doesn't name an encoding is refused whole with `not a text file: not valid utf-8` (`csv_layout.py:154`, `236`; P14). The message misdescribes the file and doesn't say to set `encoding`. X detects the encoding.
- **Should fix.** OFX problems are reported by transaction position, not line number (`cli.py:340`), where the brief asks for "the line number".
- Not counted as findings: `account add` prints nothing on success; the report has no monthly total; there's no `--color` override.

#### Maintainability: 4 / 5

What Y does well:

- A design with numbered acceptance criteria, each traced to a test named `test_acNN_…`.
- ADRs for each decision.
- mypy strict.
- Random-order and parallel tests.
- Mutation testing in CI.
- In `mutations.py`, the suite caught **6 of 6** regressions.

The findings:

- **Should fix.** CI's lint job is red (`scripts/check_imports.py:1`, E501).
- **Should fix.** The process markup costs readability: dozens of `# pragma: no mutate` comments on SQL strings and constants, and a `rules.json`/`check-rules.js` grep layer in Node.js for a Python project.

**Y: 1 must fix, 12 should fix.**

### The four questions

**X**

- **What does another parser do with this input?**
  - Spreadsheet: formula starters, including `|`, are prefixed with `'`; numbers are left as numbers (item 8). CSV line endings are `\n`.
  - Python's `csv`: rows wider than the header are re-joined into the description. That reads the brief's row 4 but turns `-1,234.56` into +234.56 (P1). The field limit is 1 MB, and a longer field rejects the file (P13).
  - OFX: a regex tokeniser, no XML parser, so a DOCTYPE is inert. Unknown declared encodings crash (P18), and failed decodes fall back to Latin-1 silently (P5).
  - Terminal: raw control characters pass through (P2).
  - SQLite: every statement is parameterised (`store.py`).
- **What happens when two copies run at once?** Writes run inside `BEGIN IMMEDIATE` with a 30 s busy timeout, so the second copy waits and then finds every row already there (item 3, three trials). Past 30 s it exits 1 with `another copy of Tally is using the data file` (P12). There's no WAL, so readers can block a commit for up to 30 s.
- **What does a flood cost, and what caps it?**
  - CSV memory is about 5× the file, uncapped (item 9).
  - OFX memory is the whole file plus its decoded text (P10).
  - OFX time is quadratic in unterminated `<?` or `<!--` (P6: 84 s for 200 KB).
  - The only cap is the 1 MB CSV field limit. Many files cost one import record each.
- **What can one input do to results the user relies on?**
  - One unquoted thousands separator flips the sign and magnitude of an amount, and the import still succeeds (P1).
  - A reused FITID hides a real transaction (P7).
  - A `USD` amount lands in a GBP account (P4).
  - Currencies are never summed across accounts (item 7), and `forget` touches only its own account (item 13).

**Y**

- **What does another parser do with this input?**
  - Spreadsheet: the brief's six starters are prefixed and amounts are bare numbers. Line endings are `\r\n` (item 8).
  - Python's `csv`: an exact column count, with lines and records capped at 2 MB. Malformed rows are refused, never repaired, including the brief's row 4 (item 4).
  - OFX: a streaming scanner with no XML parser. A DOCTYPE, `<!…>` or a processing instruction in SGML is refused; unknown or invalid encodings are refused (P5, P18).
  - Terminal: control characters are replaced (P2).
  - SQLite: parameterised statements, and refuses linked or foreign-owned side files.
- **What happens when two copies run at once?** WAL plus `BEGIN IMMEDIATE` with a 10 s timeout. Two imports serialise and count each row once (item 3). The loser of a long wait exits **3** with `another copy of tally is using the data file` and writes nothing (P12). Reports read during writes. `forget` re-checks the account under the lock (`store.py:152-163`).
- **What does a flood cost, and what caps it?**
  - CSV memory is about 2.4× the file, uncapped (item 9).
  - OFX memory is flat (P10: 22 MB for 33 MB).
  - Pieces are capped at 1 MB, nesting at 64, unreadable rows shown at 50 then counted, and regex matching at 1,000 characters (`rules.py:37`).
- **What can one input do to results the user relies on?**
  - A malformed row is dropped, though reported by line on stderr. Because the exit code stays 0, a scheduled job won't notice (item 4, P16).
  - A truncated OFX imports what it read and exits 0.
  - A `$` amount lands in a GBP account (P4).
  - A reused FITID is reported, not hidden (P7). Currencies are never summed (item 7), and `forget` removes exactly one account (item 13).

## 4. Verdict

| Axis | X | Y | Stronger |
| --- | --- | --- | --- |
| Correctness | 3 | 4 | **Y**, by one point. X's silent amount corruption (P1) is the worst defect in either build. Y's worst is a feature that's hard to reach (`explain` ids) plus a visible refusal of the brief's sample row. |
| Privacy and security | 3 | 4 | **Y**, by one point. Y's hostile-input handling is thorough: bounded, streamed, sanitised and fuzz-clean. X lets control characters reach the terminal and has a quadratic parser. X does better on exports (0600 `--output`) and on not leaving forgotten rows in a WAL. |
| Usability and accessibility | 4 | 3 | **X**, by one point. A user can start from X's README and `tally init` alone. Y is clean and accessible in output, but undocumented for users, and harder to drive (ids, encodings, exit 0 on partial imports). |
| Maintainability | 3 | 4 | **Y**, by one point. Its tests catch every regression tried and trace to criteria. X's tests are lighter, and one of them locks in a defect. X's docs help the next person more directly. |
| **Overall** | **13 / 20** (2 must, 13 should) | **15 / 20** (1 must, 12 should) | **Y**, modestly. |

Y is the safer tool to trust with real statements: when it's wrong, it says so. X is the easier tool to pick up: its documentation, `init`, `transactions` and `--output` are what Y lacks.

What would most improve each:

- **X:** remove the surplus-field repair, or accept it only when the joined row's amounts still parse and the balance check confirms them. Replace control characters before printing. Those two fix both must-fix findings. After that, stream the OFX reader with a non-backtracking scan, and drop the in-memory occurrence map (count occurrences in SQL, or key them by line).
- **Y:** add a `transactions` listing with ids, plus a README covering configuration, with an example layout and rules (or an `init`). Then exit non-zero when rows are unreadable or an OFX ends without `</OFX>`, and add an `--output` that writes 0600.

## How the builds were made

This doesn't affect any score above. The cloud environment this review ran in has a setup script whose comments call it "the control build, without the playbook", and say the two builds' environments "differ only in the plugins". Y's repository bears the marks of a prescribed process:

- acceptance criteria numbered AC1–AC44 with matching test names;
- ADRs, plus "slice 0 review", "whole-codebase audit" and "recorded in the plan" references;
- mutation-testing pragmas;
- a supply-chain CI with pinned action SHAs, `exclude-newer`, gitleaks and zizmor;
- a `templates/npmrc` reference in `pyproject.toml`.

X reads like a direct build from the brief. So I believe Y was built with a playbook plugin and X was the plain control build, but I can't confirm it from the code alone.
