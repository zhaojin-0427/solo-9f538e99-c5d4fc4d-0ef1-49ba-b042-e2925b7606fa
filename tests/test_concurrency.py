"""Concurrent submissions must be totally ordered; records + summary commit together."""

import threading

from app.schemas import BatchCreate, OrderIn, QuoteCreate
from app.service import Rejection


def _quote(service):
    service.create_quote(QuoteCreate(
        quote_no="Q1", source="USD", target="CNY", lower="7", upper="7.2"))


def _batch(no, b="710"):
    return BatchCreate(orders=[OrderIn(
        order_no=no, source="USD", target="CNY",
        original_amount="100", declared_amount=b)])


def test_parallel_consistent_batches_all_commit(service):
    _quote(service)
    n = 16
    barrier = threading.Barrier(n)
    results, errors = [], []

    def worker(i):
        barrier.wait()
        try:
            results.append(service.submit_batch(_batch(f"P{i:03d}")))
        except Exception as e:  # pragma: no cover - failure reporting
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(results) == n
    assert service.meta()["settled_order_count"] == n
    seqs = sorted(r["seq"] for r in results)
    assert len(set(seqs)) == n  # unique monotonic adjudication seq

    groups = service.list_groups()
    assert len(groups) == 1 and groups[0]["version"] == 1 + n


def test_same_order_no_race_has_single_winner(service):
    _quote(service)
    n = 8
    barrier = threading.Barrier(n)
    wins = 0
    dup = 0
    other = []
    lock = threading.Lock()

    def worker():
        nonlocal wins, dup
        barrier.wait()
        try:
            service.submit_batch(_batch("RACE-DUP"))
            with lock:
                wins += 1
        except Rejection as e:
            if any(r["code"] == "DUPLICATE_ORDER_NO" for r in e.reasons):
                with lock:
                    dup += 1
            else:  # pragma: no cover
                other.append(e)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not other
    assert wins == 1
    assert dup == n - 1
    assert service.meta()["settled_order_count"] == 1


def test_conflicting_rates_race_decided_by_commit_order(service):
    _quote(service)
    barrier = threading.Barrier(2)
    outcomes = {}

    def worker(name, declared):
        barrier.wait()
        try:
            r = service.submit_batch(_batch(name, declared))
            outcomes[name] = ("win", r["orders"][0]["rate"])
        except Rejection as e:
            outcomes[name] = ("reject", e.reasons[0]["code"])

    t1 = threading.Thread(target=worker, args=("RATE-71", "710"))
    t2 = threading.Thread(target=worker, args=("RATE-72", "720"))
    t1.start(); t2.start(); t1.join(); t2.join()

    vals = list(outcomes.values())
    wins = [v for v in vals if v[0] == "win"]
    rejects = [v for v in vals if v[0] == "reject"]
    assert len(wins) == 1 and len(rejects) == 1
    assert rejects[0][1] == "CONTRADICTION"
    assert service.meta()["settled_order_count"] == 1

    # The persisted exact rate is whichever batch won the ordering race.
    no = "RATE-71" if outcomes["RATE-71"][0] == "win" else "RATE-72"
    expected = "71/10" if no == "RATE-71" else "36/5"
    assert service.get_order(no)["applied_rate"] == expected
