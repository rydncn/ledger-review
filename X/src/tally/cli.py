"""The ``tally`` command."""

from __future__ import annotations

import argparse
import contextlib
import os
import sqlite3
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from tally import __version__
from tally.config import (
    STARTER_CONFIG,
    Config,
    default_config_path,
    default_data_path,
    load_config,
)
from tally.errors import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_USAGE,
    TallyError,
    UsageError,
)
from tally.importing import ImportResult, import_file
from tally.money import default_scale, format_minor, validate_currency
from tally.output import (
    Style,
    colour_enabled,
    error,
    message,
    render_table,
    warn,
    write_csv,
    write_json,
)
from tally.report import by_category, date_range, parse_month, top_payees, totals
from tally.rules import UNCATEGORISED, categorise, explain
from tally.store import Store, Transaction

FORMATS = ("table", "csv", "json")


class _Parser(argparse.ArgumentParser):
    def error(self, msg: str):
        self.print_usage(sys.stderr)
        error(msg)
        raise SystemExit(EXIT_USAGE)


def _global_options(p: argparse.ArgumentParser, *, suppress: bool) -> None:
    """Options accepted both before and after the command name."""
    default = argparse.SUPPRESS if suppress else None
    p.add_argument(
        "--config",
        type=Path,
        metavar="PATH",
        default=default,
        help="configuration file (default: $TALLY_CONFIG or ~/.config/tally/config.toml)",
    )
    p.add_argument(
        "--data",
        type=Path,
        metavar="PATH",
        default=default,
        help="data file (default: $TALLY_DATA, data_file in the configuration, "
        "or ~/.local/share/tally/tally.db)",
    )
    p.add_argument(
        "--color",
        "--colour",
        choices=("auto", "always", "never"),
        default=argparse.SUPPRESS if suppress else "auto",
        help="colour output (default auto: only on a terminal, never with NO_COLOR)",
    )


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(
        prog="tally",
        description="See where your money goes, from your bank's statement files. "
        "Everything stays on this computer.",
        epilog="Exit codes: 0 success, 1 error, 2 usage, "
        "3 finished but some rows could not be read.",
    )
    p.add_argument("--version", action="version", version=f"tally {__version__}")
    _global_options(p, suppress=False)
    common = argparse.ArgumentParser(add_help=False)
    _global_options(common, suppress=True)
    sub = p.add_subparsers(dest="command", metavar="COMMAND", required=True)

    s = sub.add_parser("init", parents=[common], help="write a starting configuration file")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("account", parents=[common], help="manage accounts")
    acc = s.add_subparsers(dest="account_command", metavar="ACTION", required=True)
    a = acc.add_parser("add", parents=[common], help="create an account")
    a.add_argument("name")
    a.add_argument("--currency", required=True, metavar="CODE", help="ISO code, for example GBP")
    a.add_argument(
        "--decimals",
        type=int,
        metavar="N",
        help="decimal places the currency uses (default from the currency; 2 for most)",
    )
    a.set_defaults(func=cmd_account_add)
    a = acc.add_parser("list", parents=[common], help="list accounts")
    _format_option(a)
    a.set_defaults(func=cmd_account_list)

    s = sub.add_parser("import", parents=[common], help="import a statement file into an account")
    s.add_argument("file", type=Path)
    s.add_argument("--account", required=True, metavar="NAME")
    s.add_argument("--layout", metavar="NAME", help="CSV layout from the configuration")
    s.add_argument("--type", choices=("csv", "ofx"), help="file type (default: detected)")
    s.add_argument("--strict", action="store_true", help="import nothing if any row cannot be read")
    s.add_argument(
        "--show-rows",
        action="store_true",
        help="include the unreadable values when reporting rows that were skipped",
    )
    s.set_defaults(func=cmd_import)

    s = sub.add_parser(
        "transactions", parents=[common], help="list transactions with their categories"
    )
    _range_options(s)
    s.add_argument(
        "--uncategorised",
        "--uncategorized",
        action="store_true",
        help="only transactions no rule matched",
    )
    s.add_argument("--category", metavar="NAME", help="only transactions in this category")
    _format_option(s)
    s.set_defaults(func=cmd_transactions)

    s = sub.add_parser(
        "uncategorised",
        parents=[common],
        aliases=["uncategorized"],
        help="list transactions no rule matched",
    )
    _range_options(s)
    _format_option(s)
    s.set_defaults(func=cmd_uncategorised)

    s = sub.add_parser("explain", parents=[common], help="show why a transaction got its category")
    s.add_argument("id", type=int, help="transaction id, from `tally transactions`")
    s.set_defaults(func=cmd_explain)

    s = sub.add_parser(
        "report", parents=[common], help="spending and income by category for each month"
    )
    _range_options(s)
    s.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="CATEGORY",
        help="leave out a category, for example Transfers (repeatable)",
    )
    _format_option(s)
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("payees", parents=[common], help="the payees taking the most money")
    _range_options(s)
    s.add_argument(
        "--top", type=int, default=10, metavar="N", help="how many (default 10; 0 for all)"
    )
    s.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="CATEGORY",
        help="leave out payees in a category (repeatable)",
    )
    _format_option(s)
    s.set_defaults(func=cmd_payees)

    s = sub.add_parser(
        "imports",
        parents=[common],
        help="list imports, with what each added",
    )
    s.add_argument("--account", metavar="NAME", help="only this account")
    _format_option(s)
    s.set_defaults(func=cmd_imports)

    s = sub.add_parser(
        "undo",
        parents=[common],
        help="remove the transactions one import added",
    )
    s.add_argument("id", type=int, help="import id, from `tally imports`")
    s.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    s.set_defaults(func=cmd_undo)

    s = sub.add_parser(
        "forget", parents=[common], help="delete an account and everything imported into it"
    )
    s.add_argument("name")
    s.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    s.set_defaults(func=cmd_forget)

    s = sub.add_parser("check", parents=[common], help="check the configuration file for mistakes")
    s.set_defaults(func=cmd_check)
    return p


def _range_options(s: argparse.ArgumentParser) -> None:
    s.add_argument("--account", metavar="NAME", help="only this account")
    s.add_argument("--month", metavar="YYYY-MM", help="only this month")
    s.add_argument("--from", dest="start", metavar="YYYY-MM", help="first month")
    s.add_argument("--to", dest="end", metavar="YYYY-MM", help="last month")


def _format_option(s: argparse.ArgumentParser) -> None:
    s.add_argument(
        "--format",
        choices=FORMATS,
        default="table",
        help="table for people, csv or json for machines (default table)",
    )
    s.add_argument(
        "--output", type=Path, metavar="PATH", help="write to this file instead of standard output"
    )


# -- helpers -----------------------------------------------------------------


class Context:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.style = Style(colour_enabled(args.color, sys.stdout))
        self._config: Config | None = None
        self._store: Store | None = None

    @property
    def config_path(self) -> Path:
        return self.args.config or default_config_path()

    @property
    def config(self) -> Config:
        if self._config is None:
            self._config = load_config(self.config_path)
        return self._config

    @property
    def store(self) -> Store:
        if self._store is None:
            path = self.args.data or default_data_path(self.config)
            self._store = Store(path)
        return self._store

    def close(self) -> None:
        if self._store is not None:
            self._store.close()


def _resolve_range(args: argparse.Namespace) -> tuple[str | None, str | None]:
    if args.month and (args.start or args.end):
        raise UsageError("use --month on its own, or --from and --to")
    if args.month:
        month = parse_month(args.month, "--month")
        return date_range(month, month)
    start = parse_month(args.start, "--from") if args.start else None
    end = parse_month(args.end, "--to") if args.end else None
    return date_range(start, end)


def _emit(
    ctx: Context,
    header: list[str],
    rows: list[list[object]],
    json_rows: list[dict] | dict,
    *,
    align_right=(),
    emphasis=(),
    table_footer: str | None = None,
) -> None:
    args = ctx.args
    fmt = args.format
    if args.output:
        out = open(args.output, "w", encoding="utf-8", newline="")  # noqa: SIM115
        style = Style(False)
    else:
        out = sys.stdout
        style = ctx.style
    try:
        if fmt == "csv":
            write_csv(out, header, rows)
        elif fmt == "json":
            write_json(out, json_rows)
        else:
            if not rows:
                message(out, "Nothing to show.")
                return
            text_rows = [["" if c is None else str(c) for c in r] for r in rows]
            out.write(
                render_table(
                    header, text_rows, align_right=align_right, style=style, emphasis=emphasis
                )
            )
            if table_footer:
                message(out, table_footer)
    finally:
        if out is not sys.stdout:
            out.close()


def _confirm(prompt: str) -> bool | None:
    """Ask a yes/no question. None means no answer could be had.

    Standard input not being a terminal is one reason; end-of-input is
    another. Windows reports the NUL device as a terminal, so the second
    check matters there for scheduled jobs.
    """
    if not sys.stdin.isatty():
        return None
    try:
        answer = input(f"{prompt} [y/N] ")
    except EOFError:
        return None
    return answer.strip().lower() in {"y", "yes"}


def _require_confirmation(ctx: Context, question: str, consequence: str) -> bool:
    """True to go ahead. False when the user said no. Raises when no answer could be had."""
    if ctx.args.yes:
        return True
    answer = _confirm(question)
    if answer is None:
        raise UsageError(
            f"{consequence}, and no confirmation could be read",
            hint="Add --yes to confirm when not running interactively.",
        )
    return answer


def _local_date(timestamp: str) -> str:
    """The calendar date, in this computer's time zone, of a stored UTC timestamp."""
    try:
        return datetime.fromisoformat(timestamp).astimezone().date().isoformat()
    except ValueError:
        return timestamp[:10]


# -- commands ----------------------------------------------------------------


def cmd_init(ctx: Context) -> int:
    path = ctx.config_path
    if path.exists():
        message(sys.stdout, f"Configuration already exists at {path}; leaving it alone.")
        return EXIT_OK
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(STARTER_CONFIG, encoding="utf-8")
    message(sys.stdout, f"Wrote a starting configuration to {path}")
    message(sys.stdout, "Edit the layouts to match your bank's exports and add your own rules.")
    return EXIT_OK


def cmd_account_add(ctx: Context) -> int:
    try:
        currency = validate_currency(ctx.args.currency)
    except ValueError as exc:
        raise UsageError(str(exc)) from None
    name = ctx.args.name.strip()
    if not name:
        raise UsageError("the account name must not be blank")
    scale = ctx.args.decimals if ctx.args.decimals is not None else default_scale(currency)
    if not 0 <= scale <= 4:
        raise UsageError("--decimals must be between 0 and 4")
    account = ctx.store.add_account(name, currency, scale)
    message(sys.stdout, f"Created account {account.name} in {account.currency}.")
    return EXIT_OK


def cmd_account_list(ctx: Context) -> int:
    summaries = ctx.store.account_summaries()
    header = ["Account", "Currency", "Transactions", "First", "Last"]
    rows = [
        [s["name"], s["currency"], s["transactions"], s["first"] or "", s["last"] or ""]
        for s in summaries
    ]
    json_rows = [
        {
            "account": s["name"],
            "currency": s["currency"],
            "decimals": s["scale"],
            "transactions": s["transactions"],
            "first": s["first"],
            "last": s["last"],
        }
        for s in summaries
    ]
    if not rows and ctx.args.format == "table":
        message(
            sys.stdout, "No accounts yet. Create one with `tally account add NAME --currency CODE`."
        )
        return EXIT_OK
    _emit(ctx, header, rows, json_rows, align_right=[2])
    return EXIT_OK


def _report_import(result: ImportResult, show_rows: bool, style: Style) -> None:
    out = sys.stdout
    if result.previous:
        message(
            out,
            f"Note: this file was imported into {result.account} before, on "
            f"{_local_date(result.previous.imported_at)}.",
        )
    if result.rolled_back:
        message(
            out,
            f"Imported nothing from {result.file_name}: {result.skipped} row(s) could not "
            "be read and --strict was given.",
        )
    else:
        message(
            out,
            f"Imported {result.file_name} into {result.account}: "
            f"{result.added} added, {result.duplicates} already there, "
            f"{result.skipped} skipped.",
        )
    for note in result.notes:
        where = f"line {note.line}: " if note.line else ""
        message(out, f"  note: {where}{note.text}")
    for problem in result.problems:
        text = f"  skipped line {problem.line}: {problem.reason}"
        if show_rows and problem.values:
            text += " (value: " + ", ".join(repr(v) for v in problem.values) + ")"
        message(out, text)
    if result.problems and not show_rows:
        message(out, "  Run with --show-rows to see the values that could not be read.")


def cmd_import(ctx: Context) -> int:
    args = ctx.args
    try:
        result = import_file(
            ctx.store,
            ctx.config,
            args.file,
            args.account,
            layout_name=args.layout,
            file_type=args.type,
            strict=args.strict,
        )
    except TallyError as exc:
        # Details quote the file, so they are shown only when asked for.
        if args.show_rows and exc.details:
            exc.hint = "\n  ".join(filter(None, [exc.hint, *exc.details]))
        raise
    _report_import(result, args.show_rows, ctx.style)
    if result.rolled_back:
        return EXIT_ERROR
    return EXIT_PARTIAL if result.problems else EXIT_OK


def _amount(txn: Transaction) -> str:
    return format_minor(txn.amount_minor, txn.scale)


def _list_transactions(ctx: Context, *, only_uncategorised: bool, category: str | None) -> int:
    start, end = _resolve_range(ctx.args)
    rules = ctx.config.rules
    header = ["Id", "Date", "Account", "Amount", "Currency", "Category", "Description"]
    rows: list[list[object]] = []
    json_rows: list[dict] = []
    for txn in ctx.store.transactions(account=ctx.args.account, start=start, end=end):
        cat, rule = categorise(rules, txn)
        if only_uncategorised and rule is not None:
            continue
        if category is not None and cat.casefold() != category.casefold():
            continue
        rows.append(
            [txn.id, txn.date, txn.account, _amount(txn), txn.currency, cat, txn.description]
        )
        json_rows.append(
            {
                "id": txn.id,
                "date": txn.date,
                "account": txn.account,
                "amount": _amount(txn),
                "currency": txn.currency,
                "category": cat if rule else None,
                "rule": rule.index if rule else None,
                "description": txn.description,
                "memo": txn.memo,
            }
        )
    _emit(ctx, header, rows, json_rows, align_right=[0, 3])
    return EXIT_OK


def cmd_transactions(ctx: Context) -> int:
    return _list_transactions(
        ctx, only_uncategorised=ctx.args.uncategorised, category=ctx.args.category
    )


def cmd_uncategorised(ctx: Context) -> int:
    return _list_transactions(ctx, only_uncategorised=True, category=None)


def cmd_explain(ctx: Context) -> int:
    txn = ctx.store.transaction(ctx.args.id)
    rules = ctx.config.rules
    verdicts = explain(rules, txn)
    out = sys.stdout
    style = ctx.style
    message(out, style.bold(f"Transaction {txn.id}"))
    message(out, f"  Account:     {txn.account} ({txn.currency})")
    message(out, f"  Date:        {txn.date}")
    message(out, f"  Amount:      {_amount(txn)}")
    message(out, f"  Description: {txn.description}")
    if txn.memo:
        message(out, f"  Memo:        {txn.memo}")
    matched = next((v for v in verdicts if v.matched), None)
    if matched:
        message(
            out, style.bold(f"Category: {matched.rule.category}, from rule {matched.rule.index}")
        )
        message(out, f"  Rule {matched.rule.index} matched because every condition held:")
        for condition in matched.rule.conditions():
            message(out, f"    yes: {condition}")
    elif not rules:
        message(out, style.bold(f"Category: {UNCATEGORISED}"))
        message(out, "  The configuration has no rules. Add [[rules]] tables to it.")
    else:
        message(out, style.bold(f"Category: {UNCATEGORISED}, because no rule matched"))
    tried = [v for v in verdicts if not v.matched]
    if tried:
        message(out, "  Rules tried before that, and the first condition that failed:")
        for v in tried:
            message(out, f"    rule {v.rule.index} ({v.rule.category}): no, {v.failed_condition}")
    if matched and matched.rule.index < len(rules):
        message(out, "  Later rules were not tried; the first match wins.")
    return EXIT_OK


def cmd_report(ctx: Context) -> int:
    start, end = _resolve_range(ctx.args)
    txns = ctx.store.transactions(account=ctx.args.account, start=start, end=end)
    lines = by_category(txns, ctx.config.rules, set(ctx.args.exclude))
    total_lines = totals(lines)
    header = ["Month", "Currency", "Category", "Spending", "Income", "Net", "Count"]

    def row(ln) -> list[object]:
        return [
            ln.month,
            ln.currency,
            ln.category,
            format_minor(ln.spending, ln.scale),
            format_minor(ln.income, ln.scale),
            format_minor(ln.net, ln.scale),
            ln.count,
        ]

    def json_row(ln, is_total: bool) -> dict:
        d = {
            "month": ln.month,
            "currency": ln.currency,
            "category": None if is_total else ln.category,
            "spending": format_minor(ln.spending, ln.scale),
            "income": format_minor(ln.income, ln.scale),
            "net": format_minor(ln.net, ln.scale),
            "count": ln.count,
        }
        if is_total:
            d["total"] = True
        return d

    rows: list[list[object]] = []
    json_rows: list[dict] = []
    emphasis: list[int] = []
    if ctx.args.format == "table":
        by_month: dict[tuple[str, str], list] = {}
        for ln in lines:
            by_month.setdefault((ln.month, ln.currency), []).append(ln)
        for total in total_lines:
            for ln in by_month.get((total.month, total.currency), []):
                rows.append(row(ln))
            emphasis.append(len(rows))
            rows.append(row(total))
    else:
        # CSV carries the category lines only, so a spreadsheet can total
        # them without double counting. JSON carries both, separately.
        rows = [row(ln) for ln in lines]
        json_rows = [json_row(ln, False) for ln in lines]
    _emit(
        ctx,
        header,
        rows,
        {"rows": json_rows, "totals": [json_row(t, True) for t in total_lines]},
        align_right=[3, 4, 5, 6],
        emphasis=emphasis,
        table_footer="Spending and income are shown as positive amounts; "
        "Net is income minus spending.",
    )
    return EXIT_OK


def cmd_payees(ctx: Context) -> int:
    start, end = _resolve_range(ctx.args)
    if ctx.args.top < 0:
        raise UsageError("--top must be 0 or more")
    txns = ctx.store.transactions(account=ctx.args.account, start=start, end=end)
    lines = top_payees(txns, ctx.config.rules, ctx.args.top, set(ctx.args.exclude))
    header = ["Rank", "Currency", "Payee", "Spending", "Count"]
    rows: list[list[object]] = []
    json_rows: list[dict] = []
    rank = 0
    last_currency = None
    for ln in lines:
        if ln.currency != last_currency:
            rank, last_currency = 0, ln.currency
        rank += 1
        rows.append([rank, ln.currency, ln.payee, format_minor(ln.spending, ln.scale), ln.count])
        json_rows.append(
            {
                "rank": rank,
                "currency": ln.currency,
                "payee": ln.payee,
                "spending": format_minor(ln.spending, ln.scale),
                "count": ln.count,
            }
        )
    _emit(ctx, header, rows, json_rows, align_right=[0, 3, 4])
    return EXIT_OK


def cmd_imports(ctx: Context) -> int:
    records = ctx.store.imports(ctx.args.account)
    header = ["Id", "When", "Account", "File", "Added", "Already there", "Skipped"]
    rows: list[list[object]] = [
        [r.id, _local_date(r.imported_at), r.account, r.file_name, r.added, r.duplicates, r.skipped]
        for r in records
    ]
    json_rows = [
        {
            "id": r.id,
            "imported_at": r.imported_at,
            "account": r.account,
            "file": r.file_name,
            "added": r.added,
            "duplicates": r.duplicates,
            "skipped": r.skipped,
        }
        for r in records
    ]
    _emit(ctx, header, rows, json_rows, align_right=[0, 4, 5, 6])
    return EXIT_OK


def cmd_undo(ctx: Context) -> int:
    record = ctx.store.import_record(ctx.args.id)
    what = (
        f"import {record.id} ({record.file_name} into {record.account} on "
        f"{_local_date(record.imported_at)}, {record.added} transaction(s) added)"
    )
    later = [r for r in ctx.store.imports(record.account) if r.id > record.id and r.duplicates]
    if later:
        message(
            sys.stdout,
            "Note: later imports into this account found rows already there, some of which "
            "this import may have added. Undoing it removes those too; import the later "
            "statements again to restore them.",
        )
    if not _require_confirmation(
        ctx, f"Undo {what}?", f"undoing {what} deletes the transactions it added"
    ):
        message(sys.stdout, "Nothing undone.")
        return EXIT_OK
    count = ctx.store.undo_import(record.id)
    message(sys.stdout, f"Undid import {record.id}: removed {count} transaction(s).")
    return EXIT_OK


def cmd_forget(ctx: Context) -> int:
    account = ctx.store.account(ctx.args.name)
    if not _require_confirmation(
        ctx,
        f"Delete account {account.name!r} and every transaction in it?",
        f"forgetting {account.name!r} deletes everything imported into it",
    ):
        message(sys.stdout, "Nothing deleted.")
        return EXIT_OK
    count = ctx.store.forget_account(account.name)
    message(sys.stdout, f"Forgot account {account.name} and {count} transaction(s).")
    return EXIT_OK


def cmd_check(ctx: Context) -> int:
    config = load_config(ctx.config_path, required=True)
    message(sys.stdout, f"Configuration at {config.path} is well-formed.")
    message(
        sys.stdout,
        f"  {len(config.layouts)} CSV layout(s): " + (", ".join(sorted(config.layouts)) or "none"),
    )
    message(sys.stdout, f"  {len(config.rules)} rule(s).")
    categories = sorted({r.category for r in config.rules}, key=str.casefold)
    if categories:
        message(sys.stdout, "  Categories: " + ", ".join(categories))
    return EXIT_OK


# -- entry point -------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    # Everything Tally writes is the user's financial data; keep it private.
    if hasattr(os, "umask"):
        os.umask(0o077)
    parser = build_parser()
    args = parser.parse_args(argv)
    ctx = Context(args)
    try:
        return args.func(ctx)
    except TallyError as exc:
        error(str(exc))
        return exc.exit_code
    except KeyboardInterrupt:
        warn("interrupted")
        return 130
    except BrokenPipeError:
        # The reader went away (e.g. `tally report | head`). Nothing to say,
        # and nothing to flush at exit either: point the descriptor at the
        # null device so the interpreter's final flush cannot fail again.
        with contextlib.suppress(OSError):
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        return EXIT_OK
    except sqlite3.Error as exc:
        error(f"the data file could not be read or written: {exc}")
        return EXIT_ERROR
    except OSError as exc:
        error(f"{exc.strerror or exc}" + (f": {exc.filename}" if exc.filename else ""))
        return EXIT_ERROR
    finally:
        ctx.close()
