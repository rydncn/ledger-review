#!/usr/bin/env python3
"""Probes of findings from reading the code, run identically against X and Y.

Usage: python3 review/probes.py --build X --tally X/.venv/bin/tally [--only P1,P2]

Each probe tries to prove (or disprove) one suspected defect with a command.
It reuses the adapter in hostile.py so the two builds get the same inputs.
Output: printed, and review/probes-<build>.json.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hostile as h  # noqa: E402

SIMPLE = h.LAYOUT_SIMPLE


def imp(e, path, account="a", layout="s"):
    return e.run("import", str(path), "--account", account, *(["--layout", layout] if layout else []))


def fresh(ad, layouts=None, rules=None, currency="GBP"):
    e = h.Env(ad, layouts if layouts is not None else {"s": SIMPLE}, rules or [])
    e.run("account", "add", "a", "--currency", currency)
    return e


def stored(e):
    return [(t["date"], h.amt(t), t["description"]) for t in e.all_txns()]


def p1(ad):
    """An unquoted thousands separator in the amount column."""
    e = fresh(ad)
    f = e.file("t.csv", b"Date,Description,Amount\n05/10/2026,SHOP,-1,234.56\n06/10/2026,OK,-1.00\n")
    r = imp(e, f)
    s = stored(e)
    ev = [r.show(), f"stored: {s}"]
    wrong = [x for x in s if x[2].startswith("SHOP")]
    verdict = "defect: imported a wrong amount" if wrong and wrong[0][1] != "-1234.56" else "ok: not misread"
    e2 = fresh(ad, {"cur": h.LAYOUT_CURRENT})
    f2 = e2.file("c.csv", b"Date,Description,Money Out,Money In,Balance\n02/10/2026,SALARY ACME LTD,,2,450.00,\"4,407.83\"\n")
    r2 = e2.run("import", str(f2), "--account", "a", "--layout", "cur")
    s2 = stored(e2)
    ev += [r2.show(), f"stored: {s2}"]
    if s2 and s2[0][1] != "2450.00":
        verdict = "defect: imported a wrong amount"
    return verdict, ev


def p2(ad):
    """Terminal control characters in a description."""
    e = fresh(ad)
    f = e.file("t.csv", b"Date,Description,Amount\n05/10/2026,\"EVIL\x1b]0;pwned\x07\x1b[2J\x08X\",-1.00\n")
    imp(e, f)
    ev, bad = [], False
    for c in (["uncategorised"], ["payees", "--month", "2026-10"], ["explain", "1"]):
        r = e.run(*c)
        raw = "\x1b" in r.out or "\x07" in r.out
        bad |= raw
        ev.append(f"tally {' '.join(c)}: raw ESC/BEL on stdout: {raw}; stdout={r.out[:200]!r}")
    return ("defect: raw control characters reach the terminal" if bad else "ok: replaced"), ev


def p3(ad):
    """A date_format without a year."""
    lay = dict(SIMPLE, date_format="%d/%m")
    e = fresh(ad, {"s": lay})
    f = e.file("t.csv", b"Date,Description,Amount\n05/10,SHOP,-1.00\n")
    r = imp(e, f)
    s = stored(e) if e.txn_count() else []
    ev = [r.show(), f"stored: {s}"]
    if s and s[0][0].startswith("1900"):
        return "defect: dates silently stored in 1900", ev
    return ("ok: refused" if r.rc != 0 else "unclear"), ev


def p4(ad):
    """A currency code in the amount text that is not the account's."""
    e = fresh(ad)
    f = e.file("t.csv", b"Date,Description,Amount\n05/10/2026,SHOP,-12.50 USD\n06/10/2026,SHOP2,$-3.00\n")
    r = imp(e, f)
    s = stored(e)
    ev = [r.show(), f"stored in a GBP account: {s}"]
    return ("defect: a foreign-currency amount stored as GBP" if s else "ok: refused"), ev


def p5(ad):
    """OFX encodings: a correct CHARSET:1252 declaration, and a wrong UTF-8 one."""
    ev, out = [], []
    cases = {
        "declared 1252, cp1252 bytes (correct)": h.SAVINGS_OFX.replace(b"<NAME>INTEREST", b"<NAME>CAF\xc9 \x80 INTEREST"),
        "declared UTF-8, cp1252 bytes (wrong)": h.SAVINGS_OFX.replace(b"ENCODING:USASCII", b"ENCODING:UTF-8").replace(b"<NAME>INTEREST", b"<NAME>CAF\xc9 \x80 INTEREST"),
        "declared 1252, UTF-8 bytes (wrong)": h.SAVINGS_OFX.replace(b"<NAME>INTEREST", "<NAME>CAFÉ € INTEREST".encode()),
    }
    for name, data in cases.items():
        e = fresh(ad)
        f = e.file("s.ofx", data)
        r = imp(e, f, layout=None)
        s = [x[2] for x in stored(e)] if e.txn_count() else []
        ev.append(f"[{name}] {r.show(200)}\n  descriptions: {s!r}")
        out.append((name, r.rc, s))
    correct = out[0][2] and "CAFÉ € INTEREST" in out[0][2]
    silent_wrong = [n for n, rc, s in out[1:] if rc == 0 and s and not any("CAFÉ € INTEREST" == d for d in s)]
    verdict = []
    verdict.append("correct 1252 file read right" if correct else "DEFECT: a correctly declared 1252 file is not read right")
    if silent_wrong:
        verdict.append(f"silently stored wrong text for: {silent_wrong}")
    return "; ".join(verdict), ev


def p6(ad):
    """A flood of unterminated processing instructions / comments in OFX."""
    ev, worst = [], 0.0
    for name, body in {
        "SGML + 200k '<?'": h.SAVINGS_OFX.replace(b"<BANKTRANLIST>", b"<BANKTRANLIST>" + b"<?" * 100_000),
        "XML + 200k '<?'": h.SAVINGS_OFX_XML.replace(b"<BANKTRANLIST>", b"<BANKTRANLIST>" + b"<?" * 100_000),
        "XML + 100k '<!--'": h.SAVINGS_OFX_XML.replace(b"<BANKTRANLIST>", b"<BANKTRANLIST>" + b"<!--" * 50_000),
    }.items():
        e = fresh(ad)
        f = e.file("s.ofx", body)
        t = time.time()
        try:
            r = e.run("import", str(f), "--account", "a", timeout=300)
            el = time.time() - t
            ev.append(f"[{name}] {el:.1f}s rc={r.rc} stderr={r.err.strip()[:150]!r} stdout={r.out.strip()[:150]!r}")
        except subprocess.TimeoutExpired:
            el = 300
            ev.append(f"[{name}] still running after 300 s (killed)")
        worst = max(worst, el)
    return (f"defect: {worst:.0f}s for a 200 KB file" if worst > 30 else f"ok: worst {worst:.1f}s"), ev


def p7(ad):
    """A FITID reused by the bank for a different transaction."""
    e = fresh(ad)
    f1 = e.file("a.ofx", h.SAVINGS_OFX)
    f2 = e.file("b.ofx", h.SAVINGS_OFX.replace(b"<DTPOSTED>20261015<TRNAMT>-500.00", b"<DTPOSTED>20261115<TRNAMT>-75.00").replace(b"TRANSFER TO CURRENT", b"NEW PAYMENT"))
    r1, r2 = imp(e, f1, layout=None), imp(e, f2, layout=None)
    ev = [r1.show(), r2.show(), f"stored: {stored(e)}"]
    said = any(w in (r2.out + r2.err).lower() for w in ("fitid", "unreadable 1", "different"))
    return ("ok: reported" if said else "defect: the different transaction is silently counted as already there"), ev


def p8(ad):
    """forget while another program holds the data file open (an idle connection)."""
    e = fresh(ad)
    e.run("account", "add", "keep", "--currency", "GBP")
    rows = "".join(f"{1 + i % 28:02d}/10/2026,ZQXSECRET{i:05d} PAYEE,-{i}.00\n" for i in range(3000))
    f = e.file("a.csv", ("Date,Description,Amount\n" + rows).encode())
    held = sqlite3.connect(e.db())  # e.g. a backup tool or a second tally reading
    held.execute("select count(*) from sqlite_master").fetchone()
    r1 = imp(e, f)
    r2 = e.run(*ad.forget("a"))

    def scan():
        found = {}
        for p in sorted(e.data.iterdir()):
            n = p.read_bytes().count(b"ZQXSECRET")
            if n:
                found[p.name] = n
        return found

    during = scan()
    held.close()
    after = scan()
    ev = [r1.show(150), r2.show(150), f"files: {[p.name for p in e.data.iterdir()]}",
          f"marker count while the other connection is open: {during}", f"after it closes: {after}"]
    return ("defect: forgotten data left in the file" if after else ("transient: left while another connection was open" if during else "ok")), ev


def p9(ad):
    """The same transactions imported as CSV and as OFX."""
    e = fresh(ad)
    c = e.file("s.csv", b"Date,Description,Amount\n01/10/2026,INTEREST,12.34\n15/10/2026,TRANSFER TO CURRENT,-500.00\n")
    o = e.file("s.ofx", h.SAVINGS_OFX)
    ev = [imp(e, c).show(150), imp(e, o, layout=None).show(150), f"transactions: {e.txn_count()}"]
    return ("counted twice (no cross-format dedup)" if e.txn_count() == 4 else "deduplicated"), ev


def p10(ad):
    """A large OFX file: peak memory."""
    e = fresh(ad)
    f = e.root / "big.ofx"
    n = 300_000
    with f.open("w") as out:
        out.write("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nENCODING:USASCII\nCHARSET:1252\n\n<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>GBP<BANKTRANLIST>\n")
        for i in range(n):
            out.write(f"<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>2026{1 + i % 12:02d}{1 + i % 28:02d}<TRNAMT>-{i % 900}.{i % 100:02d}<FITID>F{i:09d}<NAME>CARD PAYMENT MERCHANT {i:08d}\n")
        out.write("</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>\n")
    wrapper = ("import resource,subprocess,sys,time;t=time.time();p=subprocess.run(sys.argv[1:],capture_output=True);"
               "print(p.returncode, resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss, round(time.time()-t,1));print(p.stdout.decode()[:200])")
    p = subprocess.run([sys.executable, "-c", wrapper, *e.argv("import", str(f), "--account", "a")], capture_output=True, text=True, env=e.env(), timeout=3600)
    rc, rss, secs = p.stdout.split("\n")[0].split()
    ev = [f"{f.stat().st_size / 1e6:.0f} MB OFX, {n} transactions: rc={rc}, peak RSS {int(rss) // 1024} MB, {secs} s, stored {e.txn_count()}", p.stdout.split("\n", 1)[1].strip()]
    return f"peak {int(rss) // 1024} MB for {f.stat().st_size / 1e6:.0f} MB", ev


def p11(ad):
    """Random corruptions of the sample files: any traceback?"""
    rng = random.Random(20261112)
    seeds = [("current-account.csv", h.CURRENT_ACCOUNT, "cur"), ("savings.ofx", h.SAVINGS_OFX, None), ("x.ofx", h.SAVINGS_OFX_XML, None), ("credit-card.csv", h.CREDIT_CARD, "card")]
    e = fresh(ad, {"cur": h.LAYOUT_CURRENT, "card": h.LAYOUT_CARD})
    tracebacks, ev, n = [], [], 0
    for i in range(150):
        name, data, layout = seeds[i % len(seeds)]
        b = bytearray(data)
        for _ in range(rng.randint(1, 8)):
            op = rng.random()
            pos = rng.randrange(len(b))
            if op < 0.4:
                b[pos] = rng.randrange(256)
            elif op < 0.7:
                del b[pos : pos + rng.randint(1, 20)]
            else:
                b[pos:pos] = bytes(rng.choice([b"<", b">", b"\"", b",", b";", b"\r", b"\n", b"\x00", b"<STMTTRN>", b"</OFX>", b"&#0;", b"&#xD800;", b"(", b")", b"-", b"\xff\xfe"]))
        f = e.file(f"fuzz{i}{Path(name).suffix}", bytes(b))
        r = imp(e, f, layout=layout)
        n += 1
        if h.traceback_in(r):
            tracebacks.append((i, name, r.err.strip().splitlines()[-1][:200]))
        f.unlink()
    ev.append(f"{n} corrupted files imported; tracebacks: {len(tracebacks)}")
    ev += [f"  case {i} from {nm}: {last}" for i, nm, last in tracebacks[:10]]
    ev.append(f"integrity_check: {e.integrity()}")
    return (f"defect: {len(tracebacks)} tracebacks" if tracebacks else "ok: no tracebacks"), ev


def p12(ad):
    """Another copy holds the data file for longer than the busy timeout."""
    e = fresh(ad)
    f = e.file("t.csv", b"Date,Description,Amount\n05/10/2026,SHOP,-1.00\n")
    holder = subprocess.Popen([sys.executable, "-c", "import sqlite3,sys,time;c=sqlite3.connect(sys.argv[1],isolation_level=None);c.execute('BEGIN IMMEDIATE');print('held',flush=True);time.sleep(45)", str(e.db())], stdout=subprocess.PIPE, text=True)
    holder.stdout.readline()
    t = time.time()
    r = imp(e, f)
    el = time.time() - t
    holder.kill()
    ev = [r.show(), f"waited {el:.0f}s; transactions afterwards: {e.txn_count()}"]
    ok = r.rc != 0 and r.err.strip() and not h.traceback_in(r) and e.txn_count() == 0
    return (f"ok: refused after {el:.0f}s, exit {r.rc}" if ok else "defect"), ev


def p13(ad):
    """One field longer than 1 MB in a CSV."""
    e = fresh(ad)
    f = e.file("t.csv", b"Date,Description,Amount\n01/10/2026,FIRST,-1.00\n02/10/2026,\"" + b"x" * 2_000_000 + b"\",-2.00\n03/10/2026,THIRD,-3.00\n")
    r = imp(e, f)
    s = stored(e) if e.txn_count() else []
    return f"rc={r.rc}, stored {len(s)} of 2 good rows", [r.show(300), f"stored: {[x[2] for x in s]}"]


def p14(ad):
    """A Latin-1 CSV whose layout does not name an encoding."""
    lay = {k: v for k, v in h.LAYOUT_CARD.items() if k != "encoding"}
    e = fresh(ad, {"card": lay}, currency="EUR")
    f = e.file("c.csv", h.CREDIT_CARD)
    r = imp(e, f, layout="card")
    s = stored(e) if e.txn_count() else []
    return f"rc={r.rc}, stored {[x[2] for x in s]}", [r.show(300)]


def p15(ad):
    """A data directory other users can write (e.g. a shared folder)."""
    e = h.Env(ad, {"s": SIMPLE})
    e.data.mkdir(mode=0o777)
    e.data.chmod(0o777)
    r = e.run("account", "add", "a", "--currency", "GBP")
    return f"rc={r.rc}: {(r.err or r.out).strip()[:120]!r}", [r.show(), f"directory mode after: {oct(e.data.stat().st_mode & 0o777)}"]


def p16(ad):
    """Exit code when some rows could not be read (what a scheduled job sees)."""
    e = fresh(ad)
    f = e.file("t.csv", b"Date,Description,Amount\n05/10/2026,SHOP,-1.00\n31/02/2026,BAD,-1.00\n")
    r = imp(e, f)
    return f"exit {r.rc} with 1 unreadable row; the reason went to {'stderr' if 'line 3' in r.err else 'stdout' if 'line 3' in r.out else '?'}", [r.show()]


def p17(ad):
    """Export written with the build's own option vs a shell redirect, under umask 022."""
    import os

    old = os.umask(0o022)
    try:
        e = fresh(ad)
        f = e.file("t.csv", b"Date,Description,Amount\n05/10/2026,SHOP,-1.00\n")
        imp(e, f)
        out = e.root / "redirect.csv"
        with out.open("w") as fh:
            subprocess.run(e.argv("report", "--month", "2026-10", "--format", "csv"), stdout=fh, env=e.env())
        own = e.root / "own.csv"
        r = e.run("report", "--month", "2026-10", "--format", "csv", "--output", str(own))
        ev = [f"shell redirect: {oct(out.stat().st_mode & 0o777)}",
              f"--output: {oct(own.stat().st_mode & 0o777) if own.exists() else 'no such option: ' + r.err.strip()[:100]}"]
    finally:
        os.umask(old)
    return ev[1], ev


def p18(ad):
    """An XML OFX whose declaration names an encoding that does not exist (found by P11)."""
    e = fresh(ad)
    f = e.file("s.ofx", h.SAVINGS_OFX_XML.replace(b'encoding="UTF-8"', b'encoding="no-such-codec"'))
    r = imp(e, f, layout=None)
    return ("defect: traceback" if h.traceback_in(r) else f"ok: rc={r.rc} {r.err.strip()[:80]!r}"), [r.show(600)]


PROBES = {k: v for k, v in globals().items() if k[:1] == "p" and k[1:].isdigit()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", required=True)
    ap.add_argument("--tally", required=True)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    ad = h.Adapter(a.build, str(Path(a.tally).resolve()))
    only = {x.strip().lower() for x in a.only.split(",") if x.strip()}
    path = Path(__file__).resolve().parent / f"probes-{a.build}.json"
    results = json.loads(path.read_text()) if path.exists() and only else {}
    for name, fn in sorted(PROBES.items(), key=lambda kv: int(kv[0][1:])):
        if only and name not in only:
            continue
        t = time.time()
        try:
            verdict, ev = fn(ad)
        except Exception as exc:
            verdict, ev = "can't tell", [f"harness error: {type(exc).__name__}: {exc}"]
        results[name.upper()] = {"probe": fn.__doc__, "result": verdict, "evidence": ev}
        print(f"\n=== {a.build} {name.upper()}: {fn.__doc__}\n  -> {verdict} ({time.time() - t:.0f}s)")
        for line in ev:
            print("  " + str(line).replace("\n", "\n  "))
        sys.stdout.flush()
    path.write_text(json.dumps(dict(sorted(results.items(), key=lambda kv: int(kv[0][1:]))), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
