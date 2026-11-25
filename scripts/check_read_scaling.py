#!/usr/bin/env python3
"""Prove adjudication reads only involved group summaries.

It grows the ``ord_item`` history and, at several history sizes, instruments
the read row/call counts of one probe adjudication.  The constraint read set
must stay constant: exactly one ``group_summary`` row per involved group and
indexed point lookups -- never a scan of historical orders.

Run:  python scripts/check_read_scaling.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Database  # noqa: E402
from app.schemas import BatchCreate, OrderIn, QuoteCreate  # noqa: E402
from app.service import Rejection, Service  # noqa: E402


def batch(no: str, declared: str = "7.1", original: str = "1") -> BatchCreate:
    return BatchCreate(orders=[OrderIn(
        order_no=no, source="USD", target="CNY",
        original_amount=original, declared_amount=declared)])


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="readscale_")
    db = Database(str(Path(tmpdir) / "scale.db"))
    svc = Service(db, "2026-11-25")
    svc.create_quote(QuoteCreate(
        quote_no="Q1", source="USD", target="CNY", lower="7", upper="7.2"))
    svc.submit_batch(batch("PIN", "7.1"))

    targets = [10, 100, 400, 1000]
    history = 1
    print(f"{'history_orders':>14} | {'summary calls/rows':>20} | "
          f"{'state rows':>10} | {'order PK calls/rows':>20}")
    print("-" * 78)

    ok = True
    for target in targets:
        while history < target:
            history += 1
            svc.submit_batch(batch(f"H{history}", "7.1"))

        with db.collect_stats() as stats:
            try:
                # Contradictory probe -> adjudicated then rolled back, so the
                # history size stays exactly `target` across measurements.
                svc.submit_batch(batch(f"PROBE-{target}", "1"))
            except Rejection:
                pass

        gs = stats.get("group_summary", {"calls": 0, "rows": 0})
        cs = stats.get("currency_state", {"calls": 0, "rows": 0})
        lk = stats.get("ord_item_lookup", {"calls": 0, "rows": 0})
        total = svc.meta()["settled_order_count"]
        print(f"{total:>14} | {str(gs['calls']) + ' / ' + str(gs['rows']):>20} | "
              f"{cs['rows']:>10} | {str(lk['calls']) + ' / ' + str(lk['rows']):>20}")

        ok &= gs == {"calls": 1, "rows": 1}
        ok &= lk["calls"] == 1          # one PK point lookup for the one probe order
        ok &= lk["rows"] == 0           # unique new order_no -> miss, 0 rows fetched
        ok &= cs["rows"] == 2           # two currency_state point rows

    # A successful probe shows the same constant read profile.
    with db.collect_stats() as stats:
        svc.submit_batch(batch("SUCCESS-PROBE", "14.2", original="2"))
    gs = stats["group_summary"]
    print("-" * 78)
    print("successful probe group_summary read:", gs)
    ok &= gs == {"calls": 1, "rows": 1}

    print("\nRESULT:", "PASS — adjudication reads 1 summary row/group, "
                       "independent of history length" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
