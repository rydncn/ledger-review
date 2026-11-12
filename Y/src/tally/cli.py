"""The ``tally`` command line.

The only module that prints. Standard output carries data, standard error
carries messages; every refusal is one line on standard error and a non-zero
exit code: 1 when the data said no, 2 for a usage or configuration error, 3
when another copy of Tally holds the data file.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import logging
import os
import re
import sqlite3
import sys
import unicodedata
from collections.abc import Sequence
from itertools import groupby
from pathlib import Path
from typing import NoReturn, TextIO, TypedDict

from tally import __version__
from tally.config import Config, ConfigError, data_dir_for, load_config
from tally.importers import NotAStatementError, kind, ofx
from tally.importers.csv_layout import Layout, read
from tally.report import monthly, payees
from tally.rules import Match, categorise
from tally.rules import load as load_rules
from tally.store import Entry, LockedError, RefusedError, Store

UNREADABLE_LINES_SHOWN = 50
ENTRY_FIELDS = ("id", "date", "account", "amount", "currency", "description")
# What a terminal would act on rather than show (AC38): C0 controls become their control
# pictures (ESC is ␛), DEL is ␡, and every other control, format character (the bidi
# overrides), unassigned code point or line separator is "?".
_PICTURES = {chr(code): chr(0x2400 + code) for code in range(0x20)} | {"\x7f": "␡"}
_HIDDEN = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
# A CSV text cell starting with one of these runs as a formula in a spreadsheet.
_FORMULA = ("=", "+", "-", "@", "\t", "\r")
# ASCII digits only: strptime reads "2026-1" as January, and \d takes Arabic-Indic digits.
_MONTH = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")
_TOP = re.compile(r"[1-9][0-9]{0,2}|1000")
REPORT_FIELDS = ("month", "currency", "category", "income", "spending", "net")
PAYEE_FIELDS = ("currency", "description", "total", "count")
_WIDEST = 60  # a description in the payees table; the CSV and JSON keep it whole
# The only escape sequences Tally writes: red, and back to normal (AC33).
_RED = f"{chr(0x1B)}[31m"
_RESET = f"{chr(0x1B)}[0m"


# Every parser: no abbreviated options, or every accepted prefix is part of the command line.
class _Strict(TypedDict):
    allow_abbrev: bool


STRICT: _Strict = {"allow_abbrev": False}

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_LOCKED = 3


class UsageError(Exception):
    """Options that each parse but do not go together: exit code 2."""


def month(text: str) -> str:
    if not _MONTH.fullmatch(text):
        raise argparse.ArgumentTypeError("not a month like 2026-10")
    return text


def top_count(text: str) -> int:
    if not _TOP.fullmatch(text):
        raise argparse.ArgumentTypeError("not a number from 1 to 1000")
    return int(text)


def account_name(text: str) -> str:
    name = text.strip()
    if not name:
        raise argparse.ArgumentTypeError("must not be empty")
    if printable(name) != name:  # what the terminal would show otherwise, a line break too
        raise argparse.ArgumentTypeError("must not contain control characters or line breaks")
    return name


def existing_name(text: str) -> str:
    """An account to look up: any name ``add`` once took, so none is left unreachable."""
    name = text.strip()
    if not name:
        raise argparse.ArgumentTypeError("must not be empty")
    return name


def transaction_id(text: str) -> int:
    # ASCII digits, never int() alone: it reads " 4", "4_0" and Arabic-Indic digits too. The
    # length bounds int()'s cost; no mutate: 20 digits are always refused by 2**63 as well.
    short = len(text) <= 19  # pragma: no mutate
    if not (text.isascii() and text.isdigit() and short) or int(text) >= 2**63:
        raise argparse.ArgumentTypeError("not a transaction id")
    return int(text)


def printable(text: str) -> str:
    """The text with every control character replaced, so a terminal shows it and runs none."""
    return "".join(
        _PICTURES.get(c) or ("?" if unicodedata.category(c) in _HIDDEN else c) for c in text
    )


def currency_code(text: str) -> str:
    if len(text) != 3 or not text.isascii() or not text.isalpha():
        raise argparse.ArgumentTypeError(f"{text!r} is not a three-letter currency code")
    return text.upper()


class _Parser(argparse.ArgumentParser):
    """argparse, with its own messages made printable: they quote the command line (AC38)."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {printable(message)}\n")


def build_parser() -> argparse.ArgumentParser:
    """The top-level parser, with one subparser per command (each a ``_Parser`` too)."""
    parser = _Parser(
        prog="tally",
        description="See where your money goes, from your bank's statement files.",
        **STRICT,
    )
    parser.add_argument("--version", action="version", version=f"tally {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    common = _Parser(add_help=False)  # pragma: no mutate (None is False here)
    common.add_argument("--config", metavar="FILE", help="the tally.toml to read")
    common.add_argument("--data-dir", metavar="DIR", help="where the data file lives")

    account = commands.add_parser("account", **STRICT, help="add, list and forget accounts")
    actions = account.add_subparsers(metavar="<action>", required=True)
    add = actions.add_parser("add", parents=[common], **STRICT, help="add an account")
    add.add_argument("name", type=account_name)
    add.add_argument(
        "--currency", required=True, type=currency_code, help="three letters, e.g. GBP"
    )
    add.set_defaults(run=account_add)
    listing = actions.add_parser("list", parents=[common], **STRICT, help="list the accounts")
    listing.add_argument("--format", choices=["table", "csv", "json"], default="table")
    listing.set_defaults(run=account_list)
    forget = actions.add_parser(
        "forget",
        parents=[common],
        **STRICT,
        help="delete an account and everything imported into it",
    )
    forget.add_argument("name", type=existing_name)
    forget.add_argument("--yes", action="store_true", help="do not ask first")
    forget.set_defaults(run=account_forget)

    importing = commands.add_parser(
        "import",
        parents=[common],
        **STRICT,
        help="import a statement file into an account",
    )
    importing.add_argument("file", help="a CSV or OFX statement")
    importing.add_argument(
        "--account", required=True, type=existing_name, help="the account the statement belongs to"
    )
    importing.add_argument(
        "--layout", metavar="NAME", help="the [layouts.NAME] table that describes the CSV"
    )
    importing.add_argument("--format", choices=["table", "json"], default="table")
    importing.add_argument(
        "--verbose", action="store_true", help="say what is being read, on standard error"
    )
    importing.set_defaults(run=import_run)

    unmatched = commands.add_parser(
        "uncategorised", parents=[common], **STRICT, help="list the transactions no rule matched"
    )
    unmatched.add_argument(
        "--format",
        choices=["table", "csv", "json"],
        default="table",  # pragma: no mutate (any other default also reaches the table branch)
    )
    unmatched.set_defaults(run=uncategorised)
    explain = commands.add_parser(
        "explain", parents=[common], **STRICT, help="say why a transaction got its category"
    )
    explain.add_argument("id", type=transaction_id, help="the transaction's id, as listed")
    explain.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",  # pragma: no mutate (any other default also reaches the table branch)
    )
    explain.set_defaults(run=explain_run)

    period = _Parser(add_help=False, parents=[common])  # pragma: no mutate
    period.add_argument("--month", type=month, help="one month, e.g. 2026-10")
    period.add_argument(
        "--from", dest="first", type=month, metavar="MONTH", help="the first month, inclusive"
    )
    period.add_argument(
        "--to", dest="last", type=month, metavar="MONTH", help="the last month, inclusive"
    )
    period.add_argument(
        "--format",
        choices=["table", "csv", "json"],
        default="table",  # pragma: no mutate (any other default also reaches the table branch)
    )
    reporting = commands.add_parser(
        "report", parents=[period], **STRICT, help="money in and out by category, each month"
    )
    reporting.set_defaults(run=report_run)
    paying = commands.add_parser(
        "payees", parents=[period], **STRICT, help="the payees taking the most money"
    )
    paying.add_argument(
        "--top", type=top_count, default=10, metavar="N", help="how many per currency, 1 to 1000"
    )
    paying.set_defaults(run=payees_run)
    return parser


def open_store(args: argparse.Namespace, config: Config) -> Store:
    """Open the data file where the flag, the variable or the configuration puts it."""
    store = Store.open(data_dir_for(args.data_dir, config))
    if store.tightened:
        print("tightened the data file's permissions to 0600", file=sys.stderr)
    return store


def account_add(args: argparse.Namespace, config: Config) -> int:
    with open_store(args, config) as store:
        store.add_account(args.name, args.currency)
    return EXIT_OK


def account_list(args: argparse.Namespace, config: Config) -> int:
    with open_store(args, config) as store:
        accounts = store.accounts()
    if args.format == "table":
        for a in accounts:
            print(printable(f"{a.name} {a.currency}"))
    elif args.format == "csv":
        writer = csv.writer(_csv_stdout())
        writer.writerow(("name", "currency"))
        writer.writerows([_cell(a.name), _cell(a.currency)] for a in accounts)
    else:
        print(json.dumps([{"name": a.name, "currency": a.currency} for a in accounts]))
    return EXIT_OK


def account_forget(args: argparse.Namespace, config: Config) -> int:
    with open_store(args, config) as store:
        return _forget(args, store)


def _forget(args: argparse.Namespace, store: Store) -> int:
    account = store.account(args.name)
    if account is None:
        raise RefusedError(f'no account named "{args.name}"')
    count = store.transaction_count(account)
    shown = printable(account.name)  # names refuse controls, but a line separator is not one
    if not args.yes:
        if sys.stdin is None or not sys.stdin.isatty():
            print(
                f'forgetting "{shown}" needs --yes when standard input is not a terminal',
                file=sys.stderr,
            )
            return EXIT_USAGE
        sys.stderr.write(
            f'Forget account "{shown}" and its {count} transactions? Type yes to continue: '
        )
        sys.stderr.flush()
        if sys.stdin.readline().strip() != "yes":
            raise RefusedError("nothing forgotten")
    count = store.forget_account(account, expected=None if args.yes else count)
    print(f'forgot account "{shown}" and its {count} transactions', file=sys.stderr)
    return EXIT_OK


class _Printable(logging.Formatter):
    """Log lines made printable, as every other message is."""

    def format(self, record: logging.LogRecord) -> str:
        return printable(super().format(record))


def import_run(args: argparse.Namespace, config: Config) -> int:
    """Logging on for this run only when ``--verbose`` asks (AC19), then the import."""
    if not args.verbose:
        return _import(args, config)
    log = logging.getLogger("tally")  # pragma: no mutate (the root logger carries the same lines)
    handler = logging.StreamHandler(sys.stderr)  # pragma: no mutate (None is standard error too)
    handler.setFormatter(_Printable())  # a logged path is the file's own name (AC38)
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        return _import(args, config)
    finally:
        log.removeHandler(handler)
        log.setLevel(logging.NOTSET)


def _import(args: argparse.Namespace, config: Config) -> int:
    """The import run of design § Lifecycles: refusals before the lock, rows under it."""
    layout = None
    if args.layout is not None:
        if args.layout not in config.layouts:
            raise ConfigError(f'no layout named "{args.layout}"')
        layout = Layout.from_config(args.layout, config.layouts[args.layout])
    path = Path(args.file)
    # The path as text the data file and JSON can hold: bytes that are not UTF-8 become U+FFFD.
    shown = os.fsencode(args.file).decode("utf-8", "replace")  # pragma: no mutate (codec case)
    file_kind = kind(path)
    if file_kind == "csv" and layout is None:
        print("--layout is needed for a CSV file", file=sys.stderr)
        return EXIT_USAGE
    if file_kind != "csv" and layout is not None:
        print("--layout is for CSV files, and this is an OFX file", file=sys.stderr)
        return EXIT_USAGE
    with open_store(args, config) as store:
        account = store.account(args.account)
        if account is None:
            raise RefusedError(f'no account named "{args.account}"')
        if layout is None:
            outcomes = ofx.read(path, account.currency, account.scale)
        else:
            outcomes = read(path, layout, account.scale)
        summary = store.import_transactions(account, shown, outcomes)
    where = "line" if layout is not None else "transaction"  # what a row's number counts
    for unreadable in summary.unreadable[:UNREADABLE_LINES_SHOWN]:
        print(printable(f"{where} {unreadable.line}: {unreadable.reason}"), file=sys.stderr)
    if summary.unreadable_count > UNREADABLE_LINES_SHOWN:
        print(f"and {summary.unreadable_count - UNREADABLE_LINES_SHOWN} more", file=sys.stderr)
    if args.format == "table":
        print(
            f"added {summary.added}, already there {summary.duplicates},"
            f" unreadable {summary.unreadable_count}"
        )
    else:
        print(
            json.dumps(
                {
                    "account": account.name,
                    "file": shown,
                    "added": summary.added,
                    "duplicates": summary.duplicates,
                    "unreadable_count": summary.unreadable_count,
                    "unreadable": [{where: u.line, "reason": u.reason} for u in summary.unreadable],
                }
            )
        )
    return EXIT_OK


def _fields(entry: Entry) -> dict[str, object]:
    """A transaction as JSON holds it: the original text, the amount a string with its places."""
    return {
        "id": entry.id,
        "date": entry.date,
        "account": entry.account,
        "amount": str(entry.amount),
        "currency": entry.currency,
        "description": entry.description,
    }


def _line(entry: Entry) -> str:
    """A transaction as the terminal shows it; the description last, so no width is needed."""
    return printable(
        f"{entry.id}  {entry.date}  {entry.account}"
        f"  {entry.amount} {entry.currency}  {entry.description}"
    )


def _cell(text: str) -> str:
    """A CSV text cell: replaced as on the terminal, and quoted so a spreadsheet never runs it."""
    shown = printable(text)
    return f"'{shown}" if text.startswith(_FORMULA) else shown  # decided by the original (AC32)


def _csv_stdout() -> TextIO:
    """Standard output with no newline translation, so each row ends ``\\r\\n`` everywhere.

    The csv module writes ``\\r\\n`` itself; a text stream on Windows would make it
    ``\\r\\r\\n`` (AC32).
    """
    if isinstance(sys.stdout, io.TextIOWrapper):
        # no mutate: newline=None writes \r\n as it is everywhere but Windows, which no test
        # run here is; the Windows-style stream test kills every other change
        sys.stdout.reconfigure(newline="")  # pragma: no mutate
    return sys.stdout


def _colour() -> bool:
    """Colour only on a terminal, and never when NO_COLOR is set to anything (AC33)."""
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def _right(text: str, width: int, *, colour: bool) -> str:
    """Right-aligned; a negative amount red on a terminal, its ``-`` there either way."""
    shown = f"{_RED}{text}{_RESET}" if colour and text.startswith("-") else text
    return " " * (width - len(text)) + shown  # padded by the text, so colour moves nothing


def _section(title: str, header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    """A title, then lines with the first column left-aligned and the rest right-aligned.

    Each cell is made printable before it is measured: a replacement is one character.
    """
    colour = _colour()
    lines = [[printable(cell) for cell in line] for line in [header, *rows]]
    first, *rest = [max(len(line[i]) for line in lines) for i in range(len(header))]

    def joined(line: list[str]) -> str:
        # no mutate: every line has the header's number of cells, so strict changes nothing
        pairs = zip(line[1:], rest, strict=True)  # pragma: no mutate
        return "  ".join([line[0].ljust(first), *(_right(c, w, colour=colour) for c, w in pairs)])

    return [printable(title), *map(joined, lines)]


def _period(args: argparse.Namespace) -> tuple[str, str]:
    """The first and last month asked for, both inclusive, or ``UsageError`` (AC37)."""
    if args.month is not None:
        if args.first is not None or args.last is not None:
            raise UsageError("--month is not given with --from or --to")
        return args.month, args.month
    if args.first is None or args.last is None:
        raise UsageError("give --month, or --from and --to")
    if args.first > args.last:  # YYYY-MM text sorts as the months do
        raise UsageError(f"--from {args.first} is after --to {args.last}")
    return args.first, args.last


def _shortened(text: str) -> str:
    """At most 60 characters, so one hostile description cannot widen every line it pads."""
    return text if len(text) <= _WIDEST else text[: _WIDEST - 1] + "…"


def _print_sections(sections: Sequence[Sequence[str]]) -> None:
    """Each section's lines (made printable by ``_section``), a blank line between sections."""
    print("\n\n".join("\n".join(s) for s in sections), end="")
    if sections:
        print()


def report_run(args: argparse.Namespace, config: Config) -> int:
    """Money in and out by category, per month and currency (AC30, AC31, AC36)."""
    period = _period(args)
    rules = load_rules(config.rules)  # before the data file opens (AC29)
    with open_store(args, config) as store:
        totals = monthly(store.entries(period), rules)
    if args.format == "json":
        print(
            json.dumps(
                [
                    {
                        "month": t.month,
                        "currency": t.currency,
                        "category": t.category,
                        "income": str(t.income),
                        "spending": str(t.spending),
                        "net": str(t.net),
                    }
                    for t in totals
                ]
            )
        )
    elif args.format == "csv":
        writer = csv.writer(_csv_stdout())
        writer.writerow(REPORT_FIELDS)
        writer.writerows(
            [
                _cell(t.month),
                _cell(t.currency),
                _cell(t.category or ""),
                t.income,
                t.spending,
                t.net,
            ]
            for t in totals
        )
    else:
        sections = [
            _section(
                f"{when} {currency}",
                ("category", "income", "spending", "net"),
                [
                    [t.category or "uncategorised", str(t.income), str(t.spending), str(t.net)]
                    for t in group
                ],
            )
            for (when, currency), group in groupby(totals, key=lambda t: (t.month, t.currency))
        ]
        _print_sections(sections)
    return EXIT_OK


def payees_run(args: argparse.Namespace, config: Config) -> int:
    """The payees with the most money out in the period, per currency (AC31)."""
    period = _period(args)
    with open_store(args, config) as store:
        found = payees(store.payments(period), args.top)
    if args.format == "json":
        print(
            json.dumps(
                [
                    {
                        "currency": p.currency,
                        "description": p.description,
                        "total": str(p.total),
                        "count": p.count,
                    }
                    for p in found
                ]
            )
        )
    elif args.format == "csv":
        writer = csv.writer(_csv_stdout())
        writer.writerow(PAYEE_FIELDS)
        writer.writerows([_cell(p.currency), _cell(p.description), p.total, p.count] for p in found)
    else:
        sections = [
            _section(
                currency,
                ("description", "total", "count"),
                [[_shortened(p.description), str(p.total), str(p.count)] for p in group],
            )
            for currency, group in groupby(found, key=lambda p: p.currency)
        ]
        _print_sections(sections)
    return EXIT_OK


def uncategorised(args: argparse.Namespace, config: Config) -> int:
    """Every transaction no rule matches (AC27), the rules read before the data file (AC29)."""
    rules = load_rules(config.rules)
    with open_store(args, config) as store:
        # Collected before printing, so a data file that fails part way prints nothing (AC34).
        found = [e for e in store.entries() if not isinstance(categorise(e, rules), Match)]
    if args.format == "json":
        print(json.dumps([_fields(e) for e in found]))
    elif args.format == "csv":
        writer = csv.writer(_csv_stdout())
        writer.writerow(ENTRY_FIELDS)
        writer.writerows(
            [
                e.id,
                _cell(e.date),
                _cell(e.account),
                e.amount,
                _cell(e.currency),
                _cell(e.description),
            ]
            for e in found
        )
    else:
        for e in found:
            print(_line(e))
    return EXIT_OK


def explain_run(args: argparse.Namespace, config: Config) -> int:
    """Why one transaction has its category: each rule tried and what it found (AC28)."""
    rules = load_rules(config.rules)
    with open_store(args, config) as store:
        entry = store.entry(args.id)
    if entry is None:
        raise RefusedError(f"no transaction {args.id}")
    result = categorise(entry, rules)
    if args.format == "json":
        tried = [
            {
                "rule": v.rule.position,
                "name": v.rule.name,
                "matched": v.matched,
                "conditions": list(v.conditions),
            }
            for v in result.verdicts
        ]
        category = result.rule.name if isinstance(result, Match) else None
        print(json.dumps({**_fields(entry), "category": category, "rules": tried}))
        return EXIT_OK
    print(_line(entry))
    if isinstance(result, Match):
        print(printable(f"category {result.rule.name}, from rule {result.rule.position}"))
    else:
        print("no rule matched")
    for v in result.verdicts:
        said = " and ".join(v.conditions) or "it has no conditions"
        verb = "matched" if v.matched else "did not match"
        print(printable(f"rule {v.rule.position} {v.rule.name} {verb}: {said}"))
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return the exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_usage(sys.stderr)
        return EXIT_USAGE
    try:
        code = _run(args)
        sys.stdout.flush()  # here, so a closed pipe is met inside the try and not at exit
    except BrokenPipeError:
        # Whoever read standard output went away (``| head``): nothing to say, and nothing
        # more may be written, or the interpreter reports the pipe again as it exits.
        # no mutate: only a real process flushes at exit, and mutmut sees no subprocess; the
        # closed-pipe subprocess test fails without these two lines
        quiet = contextlib.suppress(OSError, ValueError)  # pragma: no mutate
        with quiet:
            devnull = os.open(os.devnull, os.O_WRONLY)  # pragma: no mutate
            os.dup2(devnull, sys.stdout.fileno())  # pragma: no mutate
        return EXIT_REFUSED
    return code


def _run(args: argparse.Namespace) -> int:
    """The command, with each refusal made one line on standard error and its exit code."""
    try:
        config = load_config(args.config)
        code: int = args.run(args, config)
    # Every message goes through printable: keys, paths and names can come from the user's
    # own configuration or environment, and may hold controls (AC38).
    except (ConfigError, UsageError) as error:
        print(printable(str(error)), file=sys.stderr)
        return EXIT_USAGE
    except (RefusedError, NotAStatementError, sqlite3.Error) as error:
        failed = isinstance(error, sqlite3.Error)  # a damaged file, a full disk: no data in it
        print(
            printable(f"the data file failed: {error}" if failed else str(error)), file=sys.stderr
        )
        return EXIT_REFUSED
    except LockedError as error:
        print(printable(str(error)), file=sys.stderr)
        return EXIT_LOCKED
    except BrokenPipeError:
        raise  # standard output, not the data file: ``main`` ends quietly
    except OSError as error:
        message = f"cannot open the data file: {error.strerror}: {error.filename}"
        print(printable(message), file=sys.stderr)
        return EXIT_REFUSED
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
