"""Two copies of Tally running at once must not count anything twice."""

from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path

from tally.config import parse_config
from tally.importing import import_file
from tally.store import Store

CONFIG_TEXT = (
    '[layouts.l]\ndate = "Date"\ndescription = "Description"\namount = "Amount"\n'
    'date_format = "%Y-%m-%d"\n'
)


def _worker(db: str, csv: str, barrier, results):
    config = parse_config(CONFIG_TEXT)
    with Store(Path(db)) as store:
        barrier.wait()
        result = import_file(store, config, Path(csv), "a", layout_name="l")
        results.put((result.added, result.duplicates))


def test_parallel_imports_of_the_same_file(tmp_path):
    db = tmp_path / "t.db"
    with Store(db) as store:
        store.add_account("a", "GBP", 2)
    csv = tmp_path / "big.csv"
    with open(csv, "w") as fh:
        fh.write("Date,Description,Amount\n")
        for i in range(3000):
            fh.write(f"2026-10-{(i % 28) + 1:02d},PAYEE {i},-{i}.{i % 100:02d}\n")

    ctx = multiprocessing.get_context("spawn" if sys.platform != "linux" else "fork")
    barrier = ctx.Barrier(2)
    results = ctx.Queue()
    procs = [
        ctx.Process(target=_worker, args=(str(db), str(csv), barrier, results)) for _ in range(2)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
        assert p.exitcode == 0
    outcomes = sorted([results.get(timeout=5), results.get(timeout=5)])
    assert outcomes == [(0, 3000), (3000, 0)]
    with Store(db) as store:
        assert len(list(store.transactions())) == 3000
