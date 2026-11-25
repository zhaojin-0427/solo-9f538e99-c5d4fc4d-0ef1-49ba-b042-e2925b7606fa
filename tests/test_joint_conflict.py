"""Required scenario: documents that are each viable alone but conflict in one batch.

We prove:
  * either document settles successfully on its own (each on a fresh ledger);
  * submitting BOTH in one pristine batch is rejected as CONTRADICTION;
  * the batch is all-or-nothing: neither order persists;
  * after the rejected batch the ledger still accepts a singleton order.
"""

from tests.conftest import batch, order, quote


def test_rate_710_alone_is_acceptable(client):
    quote(client, lo="7.00", hi="7.20")
    r = batch(client, [order("SOLO-1", a="100", b="710")])  # 7.10
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] is True


def test_rate_720_endpoint_alone_is_acceptable(client):
    quote(client, lo="7.00", hi="7.20")
    r = batch(client, [order("SOLO-2", a="100", b="720")])  # 7.20 closed upper endpoint
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] is True


def test_joint_conflict_on_pristine_ledger(client):
    quote(client, lo="7.00", hi="7.20")

    payload = [
        order("JC-1", a="100.00", b="710.00"),  # 7.10 individually valid
        order("JC-2", a="100.00", b="720.00"),  # 7.20 individually valid
    ]
    r = batch(client, payload)
    assert r.status_code == 409
    body = r.json()
    assert body["accepted"] is False
    codes = {x["code"] for x in body["reasons"]}
    assert "CONTRADICTION" in codes
    msg = " ".join(x["message"] for x in body["reasons"])
    assert "JC-1" in msg and "JC-2" in msg

    # Whole batch unchanged: neither order can be read back.
    assert client.get("/orders/JC-1").status_code == 404
    assert client.get("/orders/JC-2").status_code == 404

    # No batch/order counters moved.
    meta = client.get("/health").json()
    assert meta["settled_order_count"] == 0
    assert meta["settled_batch_count"] == 0

    # Ledger still works: JC-1 alone now settles.
    ok = batch(client, [order("JC-1", a="100.00", b="710.00")])
    assert ok.status_code == 200, ok.text
    got = client.get("/orders/JC-1").json()
    assert got["exact_check"] is True
    assert got["applied_rate"] == "71/10"


def test_three_way_cycle_conflict_batch_rejected(client):
    quote(client, "Q1", "USD", "CNY", "0.5", "10")
    quote(client, "Q2", "EUR", "USD", "0.5", "10")
    quote(client, "Q3", "EUR", "CNY", "0.5", "10")

    payload = [
        order("T-1", "USD", "CNY", "1", "7"),
        order("T-2", "EUR", "USD", "1", "1.1"),
        order("T-3", "EUR", "CNY", "1", "7.0"),  # triangle inconsistent
    ]
    r = batch(client, payload)
    assert r.status_code == 409
    assert any(x["code"] == "CONTRADICTION" for x in r.json()["reasons"])
    for no in ("T-1", "T-2", "T-3"):
        assert client.get(f"/orders/{no}").status_code == 404
    assert client.get("/health").json()["settled_order_count"] == 0
