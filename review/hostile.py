#!/usr/bin/env python3
"""Hostile-input checks for a Tally build, run identically against X and Y.

Usage:
    python3 review/hostile.py --build X --tally X/.venv/bin/tally [--big-mb 200] [--only 1,2]
    python3 review/hostile.py --build Y --tally Y/.venv/bin/tally

Every item builds its inputs in a fresh temporary directory, with HOME and the
XDG directories pointed inside it, and runs the build's own `tally` command.
The two builds differ only in their configuration keys and flag spellings
(learned from their --help and documentation), which the Adapter below hides;
the inputs, the commands run and the checks are the same for both.

Results are printed and written as JSON to review/results-<build>.json.
Each item is "pass", "fail" or "can't tell", with the evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import pty
import re
import resource
import select
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------
# The brief's three sample files, byte for byte.
# --------------------------------------------------------------------------

CURRENT_ACCOUNT = (
    "﻿"
    + "\r\n".join(
        [
            "Date,Description,Money Out,Money In,Balance",
            '01/10/2026,"TESCO STORES 2045, LEEDS",42.17,,"1,957.83"',
            '02/10/2026,SALARY ACME LTD,,"2,450.00","4,407.83"',
            '03/10/2026,=HYPERLINK("http://example.invalid","Refund"),,12.50,"4,420.33"',
            '03/10/2026,TFL TRAVEL CH,(3.40),,"4,416.93"',
            '05/10/2026,DIRECT DEBIT COUNCIL TAX,156.00,,"4,260.93"',
        ]
    )
    + "\r\n"
).encode("utf-8")

CREDIT_CARD = (
    "\n".join(
        [
            "Transaction Date;Payee;Amount",
            "10/02/2026;CAFÉ NERO;-4,20",
            "10/03/2026;AMAZON MARKETPLACE;-23,99",
            "10/03/2026;PAYMENT RECEIVED - THANK YOU;150,00",
        ]
    )
    + "\n"
).encode("latin-1")

SAVINGS_OFX = b"""OFXHEADER:100
DATA:OFXSGML
VERSION:102
ENCODING:USASCII
CHARSET:1252

<OFX>
<BANKMSGSRSV1><STMTTRNRS><STMTRS>
<CURDEF>GBP
<BANKTRANLIST>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20261001<TRNAMT>12.34<FITID>2026100101<NAME>INTEREST
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261015<TRNAMT>-500.00<FITID>2026101501<NAME>TRANSFER TO CURRENT
</BANKTRANLIST>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""

SAVINGS_OFX_XML = b"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<?OFX OFXHEADER="200" VERSION="220" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>
<OFX>
  <BANKMSGSRSV1>
    <STMTTRNRS>
      <STMTRS>
        <CURDEF>GBP</CURDEF>
        <BANKTRANLIST>
          <STMTTRN><TRNTYPE>CREDIT</TRNTYPE><DTPOSTED>20261001</DTPOSTED><TRNAMT>12.34</TRNAMT><FITID>X2026100101</FITID><NAME>INTEREST</NAME></STMTTRN>
          <STMTTRN><TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20261015</DTPOSTED><TRNAMT>-500.00</TRNAMT><FITID>X2026101501</FITID><NAME>TRANSFER TO CURRENT</NAME></STMTTRN>
        </BANKTRANLIST>
      </STMTRS>
    </STMTTRNRS>
  </BANKMSGSRSV1>
</OFX>
"""

# Layouts, in the brief's own terms; the adapter renders them per build.
LAYOUT_CURRENT = {
    "date": "Date",
    "description": "Description",
    "money_out": "Money Out",
    "money_in": "Money In",
    "date_format": "%d/%m/%Y",
    "decimal": ".",
    "thousands": ",",
}
LAYOUT_CARD = {
    "date": "Transaction Date",
    "description": "Payee",
    "amount": "Amount",
    "date_format": "%m/%d/%Y",
    "decimal": ",",
    "thousands": ".",
    "delimiter": ";",
    "encoding": "latin-1",
}
LAYOUT_SIMPLE = {  # Date,Description,Amount ; day-first ; signed amount
    "date": "Date",
    "description": "Description",
    "amount": "Amount",
    "date_format": "%d/%m/%Y",
    "decimal": ".",
    "thousands": ",",
}


def toml_str(v: str) -> str:
    return json.dumps(v, ensure_ascii=False)


class Adapter:
    """Per-build spelling of configuration keys and flags. Nothing else differs."""

    def __init__(self, build: str, tally: str) -> None:
        self.build = build
        self.tally = tally

    # configuration ------------------------------------------------------
    def config(self, layouts: dict[str, dict], rules: list[dict]) -> str:
        out = []
        for name, lay in layouts.items():
            out.append(f"[layouts.{name}]")
            for k, v in lay.items():
                if self.build == "X":
                    k = {"decimal": "decimal_separator", "thousands": "thousands_separator"}.get(k, k)
                out.append(f"{k} = {toml_str(v)}")
            out.append("")
        for r in rules:
            out.append("[[rules]]")
            for k, v in r.items():
                if self.build == "X":
                    k = {
                        "name": "category",
                        "contains": "description",
                        "matches": "pattern",
                        "min": "amount_min",
                        "max": "amount_max",
                    }.get(k, k)
                else:
                    k = {
                        "contains": "description_contains",
                        "matches": "description_matches",
                        "min": "amount_above",
                        "max": "amount_below",
                    }.get(k, k)
                out.append(f"{k} = {toml_str(v)}")
            out.append("")
        return "\n".join(out)

    # data location -----------------------------------------------------
    def data_args(self, data: Path) -> list[str]:
        return ["--data", str(data / "tally.db")] if self.build == "X" else ["--data-dir", str(data)]

    def db_file(self, data: Path) -> Path:
        return data / ("tally.db" if self.build == "X" else "tally.sqlite")

    # commands ----------------------------------------------------------
    def forget(self, name: str) -> list[str]:
        return ["forget", name, "--yes"] if self.build == "X" else ["account", "forget", name, "--yes"]


class Env:
    """A fresh temporary directory with a config, a data directory and HOME."""

    def __init__(self, ad: Adapter, layouts=None, rules=None) -> None:
        self.ad = ad
        self.root = Path(tempfile.mkdtemp(prefix=f"tally-{ad.build}-"))
        self.home = self.root / "home"
        self.home.mkdir()
        self.data = self.root / "data"
        self.cfg = self.root / "tally.toml"
        self.write_config(layouts or {}, rules or [])

    def write_config(self, layouts, rules) -> None:
        self.cfg.write_text(self.ad.config(layouts, rules), encoding="utf-8")

    def env(self, extra: dict | None = None) -> dict:
        e = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_DATA_HOME": str(self.home / ".local/share"),
            "XDG_STATE_HOME": str(self.home / ".local/state"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "LANG": "C.UTF-8",
            "TERM": "xterm-256color",
        }
        if extra:
            e.update(extra)
        return e

    def argv(self, *args: str) -> list[str]:
        return [self.ad.tally, *args, "--config", str(self.cfg), *self.ad.data_args(self.data)]

    def run(self, *args: str, extra_env=None, stdin=None, timeout=600, prefix=()):
        p = subprocess.run(
            [*prefix, *self.argv(*args)],
            capture_output=True,
            env=self.env(extra_env),
            cwd=self.root,
            input=stdin,
            timeout=timeout,
        )
        return Res(args, p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace"))

    def file(self, name: str, data: bytes) -> Path:
        p = self.root / name
        p.write_bytes(data)
        return p

    # database inspection (generic: any table whose name says transaction) --
    def db(self) -> Path:
        return self.ad.db_file(self.data)

    def txn_count(self) -> int | None:
        if not self.db().exists():
            return None
        con = sqlite3.connect(f"file:{self.db()}?mode=ro", uri=True)
        try:
            tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
            t = [x for x in tables if "transaction" in x.lower() or x.lower() in ("txn", "txns")]
            if not t:
                return None
            return con.execute(f'select count(*) from "{t[0]}"').fetchone()[0]
        finally:
            con.close()

    def integrity(self) -> str:
        con = sqlite3.connect(f"file:{self.db()}?mode=ro", uri=True)
        try:
            return con.execute("pragma integrity_check").fetchone()[0]
        finally:
            con.close()

    def all_txns(self) -> list[dict]:
        """Every transaction, via `uncategorised --format json` under a config with no rules."""
        saved = self.cfg.read_bytes()
        txt = saved.decode()
        txt = txt.split("[[rules]]")[0]
        self.cfg.write_text(txt, encoding="utf-8")
        try:
            r = self.run("uncategorised", "--format", "json")
        finally:
            self.cfg.write_bytes(saved)
        if r.rc != 0:
            raise RuntimeError(f"uncategorised failed: {r}")
        return json.loads(r.out)

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class Res:
    def __init__(self, args, rc, out, err):
        self.args, self.rc, self.out, self.err = args, rc, out, err

    def show(self, n=400) -> str:
        o = self.out if len(self.out) <= n else self.out[:n] + f"…[{len(self.out)} chars]"
        e = self.err if len(self.err) <= n else self.err[:n] + f"…[{len(self.err)} chars]"
        return f"$ tally {' '.join(map(str, self.args))}\n  rc={self.rc}\n  stdout: {o!r}\n  stderr: {e!r}"

    def __repr__(self) -> str:
        return self.show()


def traceback_in(r: Res) -> bool:
    return "Traceback (most recent call last)" in r.err or "Traceback (most recent call last)" in r.out


def counts(text: str) -> tuple[int | None, int | None]:
    """(added, already there) from either build's import summary."""
    a = re.search(r"(\d+) added|added (\d+)", text)
    d = re.search(r"(\d+) already there|already there (\d+)", text)
    pick = lambda m: int(m.group(1) or m.group(2)) if m else None  # noqa: E731
    return pick(a), pick(d)


def amt(t: dict) -> str:
    return str(t.get("amount"))


def summary_line(t: dict) -> str:
    return f"{t.get('date')} {t.get('amount')} {t.get('description')!r}"


# --------------------------------------------------------------------------
# Items
# --------------------------------------------------------------------------


def item1(ad: Adapter):
    e = Env(ad, {"cur": LAYOUT_CURRENT})
    f = e.file("current-account.csv", CURRENT_ACCOUNT)
    ev = [e.run("account", "add", "cur", "--currency", "GBP").show()]
    r1 = e.run("import", str(f), "--account", "cur", "--layout", "cur")
    n1 = e.txn_count()
    r2 = e.run("import", str(f), "--account", "cur", "--layout", "cur")
    n2 = e.txn_count()
    ev += [r1.show(), f"transactions in the data file: {n1}", r2.show(), f"transactions in the data file: {n2}"]
    a1, _ = counts(r1.out + r1.err)
    a2, d2 = counts(r2.out + r2.err)
    ok = n1 is not None and n1 == n2 and n1 > 0 and a2 == 0 and d2 == n1
    e.cleanup()
    return ("pass" if ok else "fail"), ev


def item2(ad: Adapter):
    # Clean rows (the brief's HYPERLINK row is malformed CSV and is item 4's business).
    header = "Date,Description,Money Out,Money In,Balance"
    body = [
        '28/09/2026,"TESCO STORES 2045, LEEDS",42.17,,"1,957.83"',
        '30/09/2026,SALARY ACME LTD,,"2,450.00","4,407.83"',
        '01/10/2026,TFL TRAVEL CH,3.40,,"4,404.43"',
        '02/10/2026,CAFE,3.40,,"4,401.03"',
        '05/10/2026,DIRECT DEBIT COUNCIL TAX,156.00,,"4,245.03"',
    ]
    # Statement A: rows 1-3; statement B: rows 2-5 (rows 2 and 3 overlap).
    a = ("﻿" + "\r\n".join([header, *body[0:3]]) + "\r\n").encode()
    b = ("﻿" + "\r\n".join([header, *body[1:5]]) + "\r\n").encode()
    e = Env(ad, {"cur": LAYOUT_CURRENT})
    fa, fb = e.file("sept.csv", a), e.file("oct.csv", b)
    e.run("account", "add", "cur", "--currency", "GBP")
    ra = e.run("import", str(fa), "--account", "cur", "--layout", "cur")
    rb = e.run("import", str(fb), "--account", "cur", "--layout", "cur")
    n = e.txn_count()
    txns = e.all_txns()
    ev = [ra.show(), rb.show(), f"transactions in the data file: {n} (expected 5)"]
    ev += [summary_line(t) for t in txns]
    added_b, dup_b = counts(rb.out + rb.err)
    ok = n == 5 and added_b == 2 and dup_b == 2
    e.cleanup()
    return ("pass" if ok else "fail"), ev


def big_csv(n: int, seed: str = "") -> bytes:
    lines = ["Date,Description,Amount"]
    for i in range(n):
        d = 1 + (i % 28)
        m = 1 + (i // 28) % 12
        lines.append(f"{d:02d}/{m:02d}/2026,SHOP {seed}{i:08d},-{1 + i % 997}.{i % 100:02d}")
    return ("\n".join(lines) + "\n").encode()


def item3(ad: Adapter):
    ev, ok = [], True
    for trial in range(3):
        e = Env(ad, {"s": LAYOUT_SIMPLE})
        f = e.file("big.csv", big_csv(2000))
        e.run("account", "add", "cur", "--currency", "GBP")
        argv = e.argv("import", str(f), "--account", "cur", "--layout", "s")
        procs = [
            subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e.env(), cwd=e.root)
            for _ in range(2)
        ]
        outs = [p.communicate(timeout=300) for p in procs]
        n = e.txn_count()
        integ = e.integrity()
        added = []
        tb = False
        for p, (o, er) in zip(procs, outs, strict=True):
            o, er = o.decode(errors="replace"), er.decode(errors="replace")
            tb |= "Traceback" in o + er
            added.append(counts(o + er)[0])
            ev.append(f"trial {trial + 1}: rc={p.returncode} stdout={o.strip()[:200]!r} stderr={er.strip()[:300]!r}")
        ev.append(f"trial {trial + 1}: transactions={n} (expected 2000), integrity_check={integ}, added={added}")
        good_rc = all(p.returncode == 0 for p in procs) or sorted(p.returncode for p in procs)[0] == 0
        trial_ok = n == 2000 and integ == "ok" and not tb and good_rc and sum(a or 0 for a in added) == 2000
        ok &= trial_ok
        e.cleanup()
    return ("pass" if ok else "fail"), ev


def item4(ad: Adapter):
    e = Env(ad, {"cur": LAYOUT_CURRENT, "card": LAYOUT_CARD})
    fc = e.file("current-account.csv", CURRENT_ACCOUNT)
    fk = e.file("credit-card.csv", CREDIT_CARD)
    fs = e.file("savings.ofx", SAVINGS_OFX)
    for name, cur in (("cur", "GBP"), ("card", "EUR"), ("sav", "GBP")):
        e.run("account", "add", name, "--currency", cur)
    rs = [
        e.run("import", str(fc), "--account", "cur", "--layout", "cur"),
        e.run("import", str(fk), "--account", "card", "--layout", "card"),
        e.run("import", str(fs), "--account", "sav"),
    ]
    txns = e.all_txns()
    got = {(t["account"], t["date"], amt(t), t["description"]) for t in txns}
    expected = {
        ("cur", "2026-10-01", "-42.17", "TESCO STORES 2045, LEEDS"),
        ("cur", "2026-10-02", "2450.00", "SALARY ACME LTD"),
        ("cur", "2026-10-03", "12.50", '=HYPERLINK("http://example.invalid","Refund")'),
        ("cur", "2026-10-03", "-3.40", "TFL TRAVEL CH"),
        ("cur", "2026-10-05", "-156.00", "DIRECT DEBIT COUNCIL TAX"),
        ("card", "2026-10-02", "-4.20", "CAFÉ NERO"),
        ("card", "2026-10-03", "-23.99", "AMAZON MARKETPLACE"),
        ("card", "2026-10-03", "150.00", "PAYMENT RECEIVED - THANK YOU"),
        ("sav", "2026-10-01", "12.34", "INTEREST"),
        ("sav", "2026-10-15", "-500.00", "TRANSFER TO CURRENT"),
    }
    ev = [r.show() for r in rs]
    ev.append("read back:")
    ev += [f"{t['account']} " + summary_line(t) for t in txns]
    missing = expected - got
    extra = got - expected
    if missing:
        ev.append(f"MISSING or different: {sorted(missing)}")
    if extra:
        ev.append(f"UNEXPECTED: {sorted(extra)}")
    # The brief's HYPERLINK row is not valid CSV (unquoted field with quotes and a comma);
    # if that is the only difference and the amount/date are right, record it separately.
    only_hyperlink = (
        len(missing) == 1
        and next(iter(missing))[3].startswith("=HYPERLINK")
        and any(x[:3] == ("cur", "2026-10-03", "12.50") for x in extra)
        and len(extra) == 1
    )
    if not missing and not extra:
        verdict = "pass"
    elif only_hyperlink:
        verdict = "pass"
        ev.append("note: the HYPERLINK row's amount and date are right; only its description text differs")
    else:
        verdict = "fail"
    e.cleanup()
    return verdict, ev


def item5(ad: Adapter):
    body = "Date,Description,Amount\n01/02/2026,AMBIGUOUS,-1.00\n02/13/2026,MONTH THIRTEEN,-2.00\n"
    e = Env(ad, {"s": LAYOUT_SIMPLE})
    f = e.file("ambiguous.csv", body.encode())
    e.run("account", "add", "cur", "--currency", "GBP")
    r = e.run("import", str(f), "--account", "cur", "--layout", "s")
    txns = e.all_txns()
    ev = [r.show(), *[summary_line(t) for t in txns]]
    dates = {t["description"]: t["date"] for t in txns}
    ok = dates.get("AMBIGUOUS") == "2026-02-01" and "MONTH THIRTEEN" not in dates
    ev.append("expect 01/02/2026 -> 2026-02-01, and 02/13/2026 refused (not swapped to 13 Feb) under %d/%m/%Y")
    e.cleanup()
    return ("pass" if ok else "fail"), ev


def item6(ad: Adapter):
    lines = ["Date,Description,Amount"] + [f"{1 + i % 28:02d}/10/2026,COFFEE {i:03d},-0.10" for i in range(100)]
    lines_in = [f"{1 + i % 28:02d}/10/2026,REFUND {i:03d},0.10" for i in range(100)]
    lines_same = [f"{1 + i % 28:02d}/10/2026,BAKERY,-0.10" for i in range(100)]
    e = Env(ad, {"s": LAYOUT_SIMPLE}, [{"name": "Coffee", "contains": "COFFEE"}, {"name": "Bakery", "contains": "BAKERY"}])
    f = e.file("pennies.csv", ("\n".join(lines + lines_in + lines_same) + "\n").encode())
    e.run("account", "add", "cur", "--currency", "GBP")
    ri = e.run("import", str(f), "--account", "cur", "--layout", "s")
    ev = [ri.show(), f"transactions in the data file: {e.txn_count()} (expected 300: 100 COFFEE -0.10, 100 REFUND +0.10, 100 BAKERY -0.10)"]
    ok = e.txn_count() == 300
    for cmd in (["report", "--month", "2026-10"], ["payees", "--month", "2026-10"]):
        for fmt in ("table", "csv", "json"):
            r = e.run(*cmd, *([] if fmt == "table" else ["--format", fmt]))
            ev.append(r.show(1200))
            text = r.out
            if r.rc != 0:
                ok = False
                continue
            # a float sum shows up as 9.99999.. or 10.000000000000002 or 9.9
            bad = re.findall(r"9\.9{3,}\d*|10\.0{3,}\d+|\b9\.9\b|\b0\.30000\d+", text)
            if bad:
                ok = False
                ev.append(f"  INEXACT: {bad}")
            if "10.00" not in text:
                ok = False
                ev.append("  10.00 not found")
            if cmd[0] == "report":
                ev.append(f"  (a total row of 20.00 spending is {'present' if '20.00' in text else 'absent'})")
    e.cleanup()
    return ("pass" if ok else "fail"), ev


def item7(ad: Adapter):
    e = Env(ad, {"s": LAYOUT_SIMPLE}, [{"name": "Shops", "contains": "SHOP"}])
    g = e.file("gbp.csv", b"Date,Description,Amount\n05/10/2026,SHOP,-10.00\n06/10/2026,PAY,1.30\n")
    u = e.file("eur.csv", b"Date,Description,Amount\n05/10/2026,SHOP,-5.00\n06/10/2026,PAY,2.40\n")
    e.run("account", "add", "uk", "--currency", "GBP")
    e.run("account", "add", "eu", "--currency", "EUR")
    ev = [
        e.run("import", str(g), "--account", "uk", "--layout", "s").show(),
        e.run("import", str(u), "--account", "eu", "--layout", "s").show(),
    ]
    ok = True
    for cmd in (["report", "--month", "2026-10"], ["payees", "--month", "2026-10"], ["uncategorised"]):
        for fmt in ("table", "csv", "json"):
            r = e.run(*cmd, *([] if fmt == "table" else ["--format", fmt]))
            ev.append(r.show(1500))
            # GBP: SHOP -10.00, PAY +1.30 (net -8.70); EUR: SHOP -5.00, PAY +2.40 (net -2.60).
            # Summed across currencies: 15.00 spending, 3.70 income, -11.30 net.
            bad = re.findall(r"(?<![\d.])(15\.00|3\.70|11\.30)(?![\d])", r.out)
            if bad:
                ok = False
                ev.append(f"  SUMMED ACROSS CURRENCIES? found {bad}")
            if r.rc != 0:
                ok = False
    e.cleanup()
    return ("pass" if ok else "fail"), ev


def item8(ad: Adapter):
    descs = ["=1+2", "+SUM(A1)", "-2+3", "@SUM(A1)", "\tTAB", "\rCR", "=HYPERLINK(\"http://x.invalid\",\"y\")", "|calc"]
    import csv as _csv
    import io

    buf = io.StringIO()
    w = _csv.writer(buf, lineterminator="\n")
    w.writerow(["Date", "Description", "Amount"])
    for i, d in enumerate(descs):
        w.writerow([f"{i + 1:02d}/10/2026", d, f"-{i + 1}.00"])
    e = Env(ad, {"s": LAYOUT_SIMPLE}, [{"name": "=cmd|' /C calc'!A0", "contains": "TAB"}])
    f = e.file("formulae.csv", buf.getvalue().encode())
    e.run("account", "add", "cur", "--currency", "GBP")
    ri = e.run("import", str(f), "--account", "cur", "--layout", "s")
    ev = [ri.show()]
    stored = e.all_txns()
    ev.append("stored descriptions: " + repr([t["description"] for t in stored]))
    ok = len(stored) == len(descs)
    for cmd in (["payees", "--month", "2026-10", "--format", "csv"], ["uncategorised", "--format", "csv"], ["report", "--month", "2026-10", "--format", "csv"]):
        r = e.run(*cmd)
        ev.append(r.show(1500))
        if r.rc != 0:
            ok = False
            continue
        cells = [c for row in _csv.reader(io.StringIO(r.out, newline="")) for c in row]
        dangerous = []
        for c in cells:
            if c[:1] in ("=", "+", "@", "\t", "\r") or (c[:1] == "-" and not re.fullmatch(r"-\d+(\.\d+)?", c)):
                dangerous.append(c)
            if c[:1] == "|":
                ev.append(f"  note: {c!r} is exported unprefixed ('|' is not in the brief's list)")
        if dangerous:
            ok = False
            ev.append(f"  cells a spreadsheet would treat as formulae: {dangerous!r}")
    e.cleanup()
    return ("pass" if ok else "fail"), ev


def item9(ad: Adapter, mb: int):
    e = Env(ad, {"s": LAYOUT_SIMPLE})
    f = e.root / "huge.csv"
    t0 = time.time()
    with f.open("wb") as out:
        out.write(b"Date,Description,Amount\n")
        i = 0
        target = mb * 1024 * 1024
        chunk = []
        size = 0
        while size < target:
            d = 1 + (i % 28)
            m = 1 + (i // 28) % 12
            line = f"{d:02d}/{m:02d}/2026,CARD PAYMENT TO MERCHANT NUMBER {i:010d} LONDON GB,-{1 + i % 997}.{i % 100:02d}\n".encode()
            chunk.append(line)
            size += len(line)
            i += 1
            if len(chunk) >= 100000:
                out.write(b"".join(chunk))
                chunk = []
        out.write(b"".join(chunk))
    rows = i
    gen = time.time() - t0
    e.run("account", "add", "cur", "--currency", "GBP")
    wrapper = (
        "import resource,subprocess,sys,time;t=time.time();"
        "p=subprocess.run(sys.argv[1:],capture_output=True);"
        "el=time.time()-t;"
        "print(p.returncode);print(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss);print(el);"
        "sys.stdout.write(p.stdout.decode(errors='replace')[:400]+'\\n---\\n'+p.stderr.decode(errors='replace')[:600])"
    )
    argv = e.argv("import", str(f), "--account", "cur", "--layout", "s")
    p = subprocess.run([sys.executable, "-c", wrapper, *argv], capture_output=True, env=e.env(), timeout=3600, cwd=e.root)
    lines = p.stdout.decode().split("\n", 3)
    rc, rss_kb, elapsed = int(lines[0]), int(lines[1]), float(lines[2])
    n = e.txn_count()
    dbsize = e.db().stat().st_size if e.db().exists() else 0
    ev = [
        f"file: {f.stat().st_size / 1e6:.0f} MB, {rows} rows (generated in {gen:.0f}s)",
        f"import rc={rc}, peak RSS {rss_kb / 1024:.0f} MB, {elapsed:.0f} s, transactions stored {n}, data file {dbsize / 1e6:.0f} MB",
        "output: " + lines[3][:900],
    ]
    # Report on it too: how long does a month report take, how much memory?
    p2 = subprocess.run([sys.executable, "-c", wrapper, *e.argv("report", "--month", "2026-03", "--format", "json")], capture_output=True, env=e.env(), timeout=3600, cwd=e.root)
    l2 = p2.stdout.decode().split("\n", 3)
    ev.append(f"report --month 2026-03: rc={l2[0]}, peak RSS {int(l2[1]) / 1024:.0f} MB, {float(l2[2]):.1f} s")
    ok = rc == 0 and n == rows and rss_kb < 200 * 1024 and elapsed < 1800
    e.cleanup()
    return ("pass" if ok else "fail"), ev


def item10(ad: Adapter):
    sgml_wrong_enc = (
        SAVINGS_OFX.replace(b"ENCODING:USASCII", b"ENCODING:UTF-8").replace(b"CHARSET:1252", b"CHARSET:NONE").replace(b"<NAME>INTEREST", b"<NAME>CAF\xc9 INTEREST")
    )
    xml_wrong_enc = SAVINGS_OFX_XML.replace(b"<NAME>INTEREST", b"<NAME>CAF\xc9 INTEREST")
    cases = [
        ("empty.csv", b"", "s", "refuse"),
        ("binary.csv", b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 8, "s", "refuse"),
        ("data.json", b'{"transactions": [{"date": "2026-10-01", "amount": -1.0}]}\n', "s", "refuse"),
        ("truncated.ofx", SAVINGS_OFX[: len(SAVINGS_OFX) // 2 + 20], None, "refuse_or_partial"),
        ("truncated-at-tag.ofx", SAVINGS_OFX[: SAVINGS_OFX.index(b"<TRNAMT>-500.00") + len(b"<TRNAMT>-500.00")], None, "refuse_or_partial"),
        ("savings-xml.ofx", SAVINGS_OFX_XML, None, "read"),
        ("wrong-encoding-sgml.ofx", sgml_wrong_enc, None, "refuse_or_cafe"),
        ("wrong-encoding-xml.ofx", xml_wrong_enc, None, "refuse_or_cafe"),
    ]
    ev, ok, unsure = [], True, False
    for name, data, layout, want in cases:
        e = Env(ad, {"s": LAYOUT_SIMPLE})
        f = e.file(name, data)
        e.run("account", "add", "a", "--currency", "GBP")
        args = ["import", str(f), "--account", "a"] + (["--layout", layout] if layout else [])
        r = e.run(*args)
        n = e.txn_count()
        txns = e.all_txns() if n else []
        ev.append(f"[{name}] " + r.show(500) + f"\n  stored: {[summary_line(t) for t in txns]}")
        tb = traceback_in(r)
        clear_refusal = r.rc != 0 and r.err.strip() != "" and not tb and n in (0, None)
        if tb:
            ok = False
            ev.append("  TRACEBACK")
        elif want == "refuse":
            if not clear_refusal:
                ok = False
                ev.append("  expected a refusal with a message on stderr and nothing written")
        elif want == "refuse_or_partial":
            # Truncated at the first transaction: refusing, or reading the complete
            # first transaction and reporting the rest, are both acceptable.
            got = {(t["date"], amt(t)) for t in txns}
            if clear_refusal:
                pass
            elif r.rc != 0 and got <= {("2026-10-01", "12.34")} and r.err.strip() and not r.out.strip():
                ev.append("  read the complete part and said so on stderr, non-zero exit")
            else:
                ok = False
                ev.append("  neither refused nor read correctly with a message")
        elif want == "read":
            got = {(t["date"], amt(t), t["description"]) for t in txns}
            if got != {("2026-10-01", "12.34", "INTEREST"), ("2026-10-15", "-500.00", "TRANSFER TO CURRENT")}:
                ok = False
                ev.append("  XML form not read correctly")
        elif want == "refuse_or_cafe":
            descs = [t["description"] for t in txns]
            if clear_refusal:
                pass
            elif r.rc == 0 and "CAFÉ INTEREST" in descs:
                ev.append("  read with the right characters despite the wrong declaration")
            elif r.rc == 0 and any("�" in d or "Ã" in d for d in descs):
                ok = False
                ev.append("  MOJIBAKE stored silently")
            else:
                unsure = True
                ev.append("  unclear outcome")
        e.cleanup()
    return ("pass" if ok and not unsure else "fail" if not ok else "can't tell"), ev


ESC = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def run_pty(e: Env, args, extra_env=None) -> tuple[int, str]:
    master, slave = pty.openpty()
    p = subprocess.Popen(e.argv(*args), stdout=slave, stderr=slave, stdin=slave, env=e.env(extra_env), cwd=e.root)
    os.close(slave)
    out = b""
    while True:
        r, _, _ = select.select([master], [], [], 10)
        if not r:
            break
        try:
            b = os.read(master, 65536)
        except OSError:
            break
        if not b:
            break
        out += b
    p.wait(timeout=30)
    os.close(master)
    return p.returncode, out.decode(errors="replace").replace("\r\n", "\n")


def item11(ad: Adapter):
    e = Env(ad, {"s": LAYOUT_SIMPLE}, [{"name": "Shops", "contains": "SHOP"}])
    f = e.file("m.csv", b"Date,Description,Amount\n05/10/2026,SHOP,-10.00\n06/10/2026,PAY,3.00\n07/10/2026,OTHER,-1.00\n")
    e.run("account", "add", "cur", "--currency", "GBP")
    e.run("import", str(f), "--account", "cur", "--layout", "s")
    ev, ok = [], True
    cmds = [["report", "--month", "2026-10"], ["payees", "--month", "2026-10"], ["uncategorised"], ["account", "list"], ["explain", "1"]]
    for c in cmds:
        piped = e.run(*c)
        piped_nc = e.run(*c, extra_env={"NO_COLOR": "1"})
        rc_t, tty = run_pty(e, c)
        rc_tn, tty_nc = run_pty(e, c, {"NO_COLOR": "1"})
        esc_piped = bool(ESC.search(piped.out + piped.err))
        esc_nc = bool(ESC.search(tty_nc))
        esc_tty = bool(ESC.search(tty))
        same = ESC.sub("", tty).strip() == piped.out.strip() + ("\n" + piped.err.strip() if piped.err.strip() else "")
        ev.append(
            f"tally {' '.join(c)}: piped escapes={esc_piped}; tty+NO_COLOR escapes={esc_nc}; tty escapes={esc_tty}; "
            f"tty text == piped text once escapes stripped: {same}"
        )
        if esc_tty:
            ev.append(f"  tty output: {tty[:500]!r}")
        if esc_piped or esc_nc or piped_nc.out != piped.out:
            ok = False
        if esc_tty and not same:
            ok = False
            ev.append("  colour carries something the plain text does not")
    # Errors on stderr, non-zero exit, stdout clean.
    for c in (["import", str(f), "--account", "nosuch", "--layout", "s"], ["report", "--month", "2026-13"], ["explain", "999999"], ["import", str(e.root / "missing.csv"), "--account", "cur", "--layout", "s"]):
        r = e.run(*c)
        ev.append(r.show(300))
        if r.rc == 0 or r.out.strip() or not r.err.strip() or traceback_in(r):
            ok = False
            ev.append("  error not cleanly on stderr with a non-zero exit")
    # Machine-readable output for every report.
    for c in (["report", "--month", "2026-10"], ["payees", "--month", "2026-10"], ["uncategorised"], ["account", "list"], ["explain", "1"]):
        r = e.run(*c, "--format", "json")
        try:
            json.loads(r.out)
            ev.append(f"tally {' '.join(c)} --format json: valid JSON")
        except Exception:
            ev.append(f"tally {' '.join(c)} --format json: NOT AVAILABLE/VALID ({r.rc}: {r.err.strip()[:160]!r})")
            if c[0] in ("report", "payees", "uncategorised"):
                ok = False
    e.cleanup()
    return ("pass" if ok else "fail"), ev


def mode(p: Path) -> str:
    return oct(stat.S_IMODE(p.stat().st_mode))


def item12(ad: Adapter):
    ev, ok = [], True
    old = os.umask(0o022)
    try:
        e = Env(ad, {"s": LAYOUT_SIMPLE})
        f = e.file("m.csv", b"Date,Description,Amount\n05/10/2026,SHOP,-10.00\n")
        e.run("account", "add", "cur", "--currency", "GBP")
        e.run("import", str(f), "--account", "cur", "--layout", "s")
        ev.append("umask 022")
        for p in sorted(e.data.iterdir()):
            ev.append(f"{p.name}: {mode(p)}")
            if stat.S_IMODE(p.stat().st_mode) & 0o077:
                ok = False
        ev.append(f"data directory {e.data.name}/: {mode(e.data)}")
        # An export: --output if the build offers it, else the shell redirect a user would use.
        out = e.root / "export.csv"
        r = e.run("report", "--month", "2026-10", "--format", "csv", "--output", str(out))
        if r.rc == 0 and out.exists():
            ev.append(f"report --output export.csv: {mode(out)}")
            if stat.S_IMODE(out.stat().st_mode) & 0o077:
                ok = False
        else:
            ev.append(f"no --output option ({r.err.strip()[:120]!r}); an export is a shell redirect, which takes the shell's umask, not Tally's")
        # Loosened later: does it tighten?
        os.chmod(e.db(), 0o644)
        e.run("report", "--month", "2026-10")
        ev.append(f"after chmod 644 and a report: {mode(e.db())}")
        r = e.run("account", "add", "two", "--currency", "GBP")
        ev.append(f"after chmod 644 and a write ({r.err.strip()[:120]!r}): {mode(e.db())}")
        if stat.S_IMODE(e.db().stat().st_mode) & 0o077:
            ev.append("  not tightened")
            ok = False
        e.cleanup()
    finally:
        os.umask(old)
    return ("pass" if ok else "fail"), ev


def item13(ad: Adapter):
    e = Env(ad, {"s": LAYOUT_SIMPLE})
    a = e.file("a.csv", b"Date,Description,Amount\n05/10/2026,ZQXFORGETMEALPHA,-11.11\n06/10/2026,ZQXFORGETMEBRAVO,-22.22\n")
    b = e.file("b.csv", b"Date,Description,Amount\n05/10/2026,KEEPMEKILO,-33.33\n")
    e.run("account", "add", "secret", "--currency", "GBP")
    e.run("account", "add", "keep", "--currency", "GBP")
    ev = [e.run("import", str(a), "--account", "secret", "--layout", "s").show(), e.run("import", str(b), "--account", "keep", "--layout", "s").show()]
    r = e.run(*ad.forget("secret"))
    ev.append(r.show())
    files = sorted(p for p in e.data.iterdir())
    leaks = []
    for p in files:
        blob = p.read_bytes()
        for m in (b"ZQXFORGETME", b"a.csv", b"secret"):
            if m in blob:
                leaks.append((p.name, m.decode()))
        s = subprocess.run(["strings", str(p)], capture_output=True, text=True).stdout
        if "ZQXFORGETME" in s:
            ev.append(f"strings {p.name}: " + ", ".join(sorted({x for x in s.split() if 'ZQXFORGETME' in x})))
    ev.append(f"files in the data directory after forget: {[p.name for p in files]}")
    ev.append(f"marker bytes still present: {leaks}")
    rest = e.all_txns()
    ev.append("remaining: " + repr([(t['account'], t['description'], amt(t)) for t in rest]))
    ok = r.rc == 0 and not any(m == "ZQXFORGETME" for _, m in leaks) and [(t['account'], t['description']) for t in rest] == [("keep", "KEEPMEKILO")]
    if any(m in ("a.csv", "secret") for _, m in leaks):
        ev.append("  (the account name or file name of the forgotten account also remains)")
    e.cleanup()
    return ("pass" if ok else "fail"), ev


def item14(ad: Adapter):
    e = Env(ad, {"cur": LAYOUT_CURRENT}, [{"name": "Shops", "contains": "TESCO"}])
    fc = e.file("current-account.csv", CURRENT_ACCOUNT)
    fo = e.file("savings.ofx", SAVINGS_OFX)
    fx = e.file("x.ofx", SAVINGS_OFX_XML)
    pre = ("unshare", "-n")
    ev, ok = [], True
    steps = [
        ("account", "add", "cur", "--currency", "GBP"),
        ("account", "add", "sav", "--currency", "GBP"),
        ("account", "list"),
        ("import", str(fc), "--account", "cur", "--layout", "cur"),
        ("import", str(fo), "--account", "sav"),
        ("import", str(fx), "--account", "sav"),
        ("report", "--month", "2026-10"),
        ("report", "--month", "2026-10", "--format", "csv"),
        ("payees", "--month", "2026-10"),
        ("uncategorised",),
        ("explain", "1"),
        tuple(ad.forget("sav")),
    ]
    for s in steps:
        r = e.run(*s, prefix=pre)
        ev.append(f"unshare -n tally {' '.join(s)} -> rc={r.rc}{' TRACEBACK' if traceback_in(r) else ''}")
        if r.rc != 0 or traceback_in(r):
            ok = False
            ev.append("  " + r.show(300))
    src = REPO / ad.build / "src"
    pat = r"^\s*(import|from)\s+(socket|ssl|http|urllib|requests|httpx|aiohttp|ftplib|smtplib|poplib|imaplib|telnetlib|xmlrpc|asyncio|websocket|urllib3)\b"
    g = subprocess.run(["grep", "-rnE", pat, str(src)], capture_output=True, text=True).stdout
    g2 = subprocess.run(["grep", "-rnwE", "socket|urlopen|create_connection|getaddrinfo", str(src)], capture_output=True, text=True).stdout
    ev.append(f"network imports under {ad.build}/src: {g.strip() or 'none'}")
    ev.append(f"socket/urlopen/getaddrinfo references: {g2.strip() or 'none'}")
    deps = subprocess.run(["grep", "-n", "^dependencies", str(REPO / ad.build / "pyproject.toml")], capture_output=True, text=True).stdout.strip()
    ev.append(f"runtime dependencies: {deps}")
    if g.strip() or g2.strip():
        ok = False
    e.cleanup()
    return ("pass" if ok else "fail"), ev


def item15(ad: Adapter):
    markers = ["PRIVMARKERONE", "PRIVMARKERTWO", "PRIVMARKERTHREE", "PRIVMARKERFOUR", "4817.29", "9273.61", "6392.18"]
    e = Env(ad, {"s": LAYOUT_SIMPLE, "bad": dict(LAYOUT_SIMPLE, date="Nope")}, [{"name": "Shops", "contains": "SHOP"}])
    good = e.file("good.csv", b"Date,Description,Amount\n05/10/2026,PRIVMARKERONE,-4817.29\n")
    messy = e.file(
        "messy.csv",
        b"Date,Description,Amount\n"
        b"05/10/2026,PRIVMARKERONE,-4817.29\n"
        b"31/02/2026,PRIVMARKERTWO,-9273.61\n"
        b"06/10/2026,PRIVMARKERTHREE,NOTANUMBER6392.18\n"
        b"07/10/2026,PRIVMARKERFOUR\n"
        b"08/10/2026,PRIVMARKERFOUR,-1.234.5\n"
        b'09/10/2026,"PRIVMARKERFOUR unterminated,-1.00\n',
    )
    ofx_bad = e.file("bad.ofx", SAVINGS_OFX.replace(b"<TRNAMT>12.34", b"<TRNAMT>PRIVMARKERTWO").replace(b"<CURDEF>GBP", b"<CURDEF>GBP"))
    ofx_eur = e.file("eur.ofx", SAVINGS_OFX.replace(b"<CURDEF>GBP", b"<CURDEF>EUR").replace(b"INTEREST", b"PRIVMARKERONE"))
    e.run("account", "add", "cur", "--currency", "GBP")
    verbose = ["--verbose"] if "--verbose" in e.run("import", "--help").out else []
    ev = [f"most verbose option found in `import --help`: {verbose or 'none'}"]
    runs = [
        e.run("import", str(good), "--account", "cur", "--layout", "s", *verbose),
        e.run("import", str(messy), "--account", "cur", "--layout", "s", *verbose),
        e.run("import", str(messy), "--account", "cur", "--layout", "bad", *verbose),
        e.run("import", str(ofx_bad), "--account", "cur", *verbose),
        e.run("import", str(ofx_eur), "--account", "cur", *verbose),
        e.run("import", str(good), "--account", "nosuch", "--layout", "s", *verbose),
        e.run("explain", "999999"),
        e.run("report", "--month", "2026-13"),
    ]
    # a broken rule
    e.cfg.write_text(e.cfg.read_text() + '\n[[rules]]\n' + ('category = "X"\npattern = "("\n' if ad.build == "X" else 'name = "X"\ndescription_matches = "("\n'))
    runs.append(e.run("report", "--month", "2026-10"))
    leaks = []
    for r in runs:
        ev.append(r.show(600))
        for m in markers:
            if m in r.err:
                leaks.append((" ".join(map(str, r.args))[:60], m))
    # Anything written outside the data directory and the inputs (log files)?
    written = [str(p.relative_to(e.root)) for p in e.root.rglob("*") if p.is_file() and e.data not in p.parents and p.parent != e.root]
    ev.append(f"other files written under HOME/XDG: {written or 'none'}")
    for p in e.root.rglob("*"):
        if p.is_file() and p.parent == e.home or (p.is_file() and e.home in p.parents):
            blob = p.read_bytes()
            for m in markers:
                if m.encode() in blob:
                    leaks.append((str(p), m))
    ev.append(f"markers on standard error / in logs: {leaks or 'none'}")
    e.cleanup()
    return ("pass" if not leaks else "fail"), ev


def item16(ad: Adapter):
    rules = [{"name": "Groceries", "contains": "TESCO"}, {"name": "Shopping", "matches": "^TES"}]
    e = Env(ad, {"cur": LAYOUT_CURRENT}, rules)
    f = e.file("current-account.csv", CURRENT_ACCOUNT)
    e.run("account", "add", "cur", "--currency", "GBP")
    e.run("import", str(f), "--account", "cur", "--layout", "cur")
    tesco = [t for t in e.all_txns() if t["description"].startswith("TESCO")]
    ev = []
    if not tesco:
        return "fail", ["TESCO row not imported"]
    tid = str(tesco[0]["id"])
    r = e.run("explain", tid)
    rep = e.run("report", "--month", "2026-10", "--format", "json")
    ev += [r.show(800), rep.show(800)]
    ok = r.rc == 0 and "Groceries" in r.out and "Shopping" not in rep.out
    # The same rules in the other order must give the other answer.
    e.write_config({"cur": LAYOUT_CURRENT}, list(reversed(rules)))
    r2 = e.run("explain", tid)
    rep2 = e.run("report", "--month", "2026-10", "--format", "json")
    ev += ["rules reversed:", r2.show(800)]
    ok &= "Shopping" in r2.out and "Shopping" in rep2.out and "Groceries" not in rep2.out
    # And for an uncategorised one, explain says no rule matched and why.
    others = [t for t in e.all_txns() if t["description"].startswith("SALARY")]
    r3 = e.run("explain", str(others[0]["id"]))
    ev += ["an uncategorised transaction:", r3.show(800)]
    ok &= r3.rc == 0
    e.cleanup()
    return ("pass" if ok else "fail"), ev


ITEMS = {
    1: ("The same file imported twice", item1),
    2: ("Two statements whose dates overlap", item2),
    3: ("Two imports of one file at the same moment", item3),
    4: ("The three sample files", item4),
    5: ("An ambiguous date", item5),
    6: ("Exact money: 100 x 0.10", item6),
    7: ("Two currencies never added together", item7),
    8: ("Formula injection in CSV export", item8),
    9: ("A 200 MB CSV", None),
    10: ("Things that aren't statements", item10),
    11: ("Piping, NO_COLOR, stderr, machine-readable output", item11),
    12: ("Permissions of the data file and exports", item12),
    13: ("forget leaves nothing behind", item13),
    14: ("No network", item14),
    15: ("Nothing personal in logs or errors", item15),
    16: ("Categorisation: first rule wins, and why", item16),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", required=True, choices=["X", "Y"])
    ap.add_argument("--tally", required=True)
    ap.add_argument("--big-mb", type=int, default=200)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    ad = Adapter(a.build, str(Path(a.tally).resolve()))
    only = {int(x) for x in a.only.split(",") if x}
    out_path = REPO / "review" / f"results-{a.build}.json"
    results = json.loads(out_path.read_text()) if out_path.exists() and only else {}
    for n, (title, fn) in ITEMS.items():
        if only and n not in only:
            continue
        t0 = time.time()
        try:
            verdict, ev = item9(ad, a.big_mb) if n == 9 else fn(ad)
        except Exception as exc:  # the harness itself failed: say so, don't guess
            verdict, ev = "can't tell", [f"harness error: {type(exc).__name__}: {exc}"]
        results[str(n)] = {"title": title, "verdict": verdict, "evidence": ev, "seconds": round(time.time() - t0, 1)}
        print(f"\n=== {a.build} item {n}: {title}: {verdict.upper()} ({time.time() - t0:.0f}s)")
        for line in ev:
            print("  " + line.replace("\n", "\n  "))
        sys.stdout.flush()
    out_path.write_text(json.dumps(dict(sorted(results.items(), key=lambda kv: int(kv[0]))), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
