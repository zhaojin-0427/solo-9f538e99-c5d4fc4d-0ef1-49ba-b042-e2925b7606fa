from tests.conftest import batch, order, quote


def test_docs_and_health(client):
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["max_currencies"] == 32


def test_missing_chain_explained_and_nothing_written(client):
    r = batch(client, [order("NOCHAIN")])
    assert r.status_code == 409
    body = r.json()
    assert body["accepted"] is False
    assert body["reasons"][0]["code"] == "MISSING_CHAIN"
    assert client.get("/orders/NOCHAIN").status_code == 404


def test_bad_decimal_and_nonpositive_are_422(client):
    quote(client)
    r = batch(client, [order("BAD", a="abc")])
    assert r.status_code == 422
    assert any(x["code"] == "INVALID_AMOUNT" for x in r.json()["reasons"])

    r2 = batch(client, [order("ZERO", a="0", b="7")])
    assert r2.status_code == 422
    assert any(x["code"] == "INVALID_AMOUNT" for x in r2.json()["reasons"])


def test_same_currency_rejected(client):
    r = batch(client, [order("SAME", s="USD", t="USD")])
    assert r.status_code == 422
    assert any(x["code"] == "SAME_CURRENCY" for x in r.json()["reasons"])


def test_duplicate_inside_batch_rejects_whole_batch(client):
    quote(client)
    r = batch(client, [order("DUP"), order("DUP", b="711")])
    assert r.status_code == 409
    assert any(x["code"] == "DUPLICATE_ORDER_NO" for x in r.json()["reasons"])
    assert client.get("/health").json()["settled_order_count"] == 0


def test_duplicate_against_ledger_rejected(client):
    quote(client)
    assert batch(client, [order("D1")]).status_code == 200
    r = batch(client, [order("D1")])
    assert r.status_code == 409
    assert any(x["code"] == "DUPLICATE_ORDER_NO" for x in r.json()["reasons"])
    # only one D1 exists
    assert client.get("/health").json()["settled_order_count"] == 1


def test_successful_settle_and_order_readback_table(client):
    quote(client, lo="7.10", hi="7.10")  # exact point interval
    r = batch(client, [order("OK1", a="100.00", b="710.00")])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] and body["seq"] >= 1
    o = client.get("/orders/OK1").json()
    assert o["applied_rate"] == "71/10"
    assert o["exact_check"] is True
    tbl = o["conversion_snapshot"]
    assert tbl["anchor"] in ("CNY", "USD")
    assert tbl["pairs"]["USD/CNY"] == "71/10"
    assert tbl["pairs"]["CNY/USD"] == "10/71"
    assert "pairs_decimal" in tbl


def test_repeating_decimal_rate_is_exact_fraction(client):
    # 30 USD -> 100 CNY pins 10/3 exactly despite repeating decimal display.
    quote(client, lo="3.33", hi="3.34")
    r = batch(client, [order("FRAC", a="30.00", b="100.00")])
    assert r.status_code == 200, r.text
    o = client.get("/orders/FRAC").json()
    assert o["applied_rate"] == "10/3"
    assert o["exact_check"] is True


def test_fraction_quote_endpoints_accepted(client):
    r = quote(client, no="QF", lo="143/20", hi="7.20")
    assert r.status_code == 200, r.text
    q = client.get("/quotes/QF").json()
    assert q["lower"] == "143/20"


def test_quote_conflict_suspends_then_update_recovers(client):
    quote(client, lo="7.0", hi="7.2")
    assert batch(client, [order("P1", b="710")]).status_code == 200

    bad = quote(client, no="Q2", lo="7.3", hi="7.4")
    assert bad.status_code == 200  # filed anyway
    assert bad.json()["groups"][0]["status"] == "suspended"

    blocked = batch(client, [order("P2", b="711")])
    assert blocked.status_code == 409
    assert any(x["code"] == "GROUP_SUSPENDED" for x in blocked.json()["reasons"])
    assert client.get("/orders/P2").status_code == 404

    fix = client.put("/quotes/Q2", json={"lower": "7.05", "upper": "7.15"})
    assert fix.status_code == 200
    assert fix.json()["groups"][0]["status"] == "active"

    ok = batch(client, [order("P2", a="100", b="710")])
    assert ok.status_code == 200, ok.text


def test_withdraw_quote_creates_missing_chain_again(client):
    quote(client)  # Q1 USD/CNY
    assert batch(client, [order("W1")]).status_code == 200
    # With the only quote withdrawn but the exact order present, currencies
    # stay grouped by the settled order itself; a NEW different ratio fails.
    w = client.post("/quotes/Q1/withdraw")
    assert w.status_code == 200 and w.json()["changed"] is True
    r = batch(client, [order("W2", b="700")])
    assert r.status_code == 409
    assert r.json()["reasons"][0]["code"] == "CONTRADICTION"
    # same exact ratio still settles even without any active quote
    ok = batch(client, [order("W3", a="50", b="355")])
    assert ok.status_code == 200, ok.text


def test_quote_only_bridge_withdraw_splits_groups(client):
    quote(client, "Q1", "USD", "CNY", "7", "7.2")
    quote(client, "Q2", "EUR", "JPY", "160", "162")
    assert batch(client, [order("U1", "USD", "CNY", "1", "7.1")]).status_code == 200
    assert batch(client, [order("E1", "EUR", "JPY", "1", "161")]).status_code == 200
    quote(client, "BR", "EUR", "USD", "1.08", "1.10")

    w = client.post("/quotes/BR/withdraw").json()
    pieces = sorted(tuple(g["currencies"]) for g in w["groups"])
    assert pieces == [("CNY", "USD"), ("EUR", "JPY")]

    r = batch(client, [order("X", "EUR", "USD", "1", "1.09")])
    assert r.status_code == 409
    assert r.json()["reasons"][0]["code"] == "MISSING_CHAIN"


def test_one_batch_across_two_groups_atomic(client):
    quote(client, "Q1", "USD", "CNY", "7", "7.2")
    quote(client, "Q2", "EUR", "JPY", "160", "162")
    r = batch(client, [
        order("M1", "USD", "CNY", "100", "710"),
        order("M2", "EUR", "JPY", "1", "161"),
    ])
    assert r.status_code == 200, r.text
    assert len(r.json()["groups"]) == 2
    bid = r.json()["batch_id"]
    gb = client.get(f"/batches/{bid}").json()
    assert set(gb["order_nos"]) == {"M1", "M2"}
    assert len(gb["snapshots"]) == 2


def test_failed_second_group_rolls_back_first_group(client):
    quote(client, "Q1", "USD", "CNY", "7", "7.2")
    quote(client, "Q2", "EUR", "JPY", "160", "162")
    r = batch(client, [
        order("R1", "USD", "CNY", "100", "710"),   # good
        order("R2", "EUR", "JPY", "1", "999"),      # outside interval -> contradiction
    ])
    assert r.status_code == 409
    assert client.get("/orders/R1").status_code == 404
    assert client.get("/orders/R2").status_code == 404
    assert client.get("/health").json()["settled_order_count"] == 0


def test_currency_cap_32(client):
    # 16 disjoint pairs register exactly 32 currencies.
    for i in range(16):
        a = f"C{i:02d}A"
        b = f"C{i:02d}B"
        rr = quote(client, f"Q{i}", a, b, "1", "2")
        assert rr.status_code == 200, rr.text
    assert client.get("/health").json()["currency_count"] == 32

    over = quote(client, "QX", "NEW", "C00A", "1", "2")
    assert over.status_code == 422
    assert over.json()["reasons"][0]["code"] == "CURRENCY_LIMIT"


def test_quote_not_found_update_404(client):
    r = client.put("/quotes/NOPE", json={"lower": "1", "upper": "2"})
    assert r.status_code == 404


def test_numeric_json_inputs_are_leniently_accepted(client):
    # Some clients send JSON numbers rather than exact strings; these must work
    # for ordinary decimal literals (strings remain the fully-exact path).
    rq = client.post("/quotes", json={
        "quote_no": 7, "source": "USD", "target": "CNY",
        "lower": 7, "upper": 7.2})
    assert rq.status_code == 200, rq.text

    r = client.post("/batches", json={"orders": [{
        "order_no": 9001, "source": "USD", "target": "CNY",
        "original_amount": 100, "declared_amount": 710.0}]})
    assert r.status_code == 200, r.text
    assert r.json()["orders"][0]["rate"] == "71/10"
    assert client.get("/orders/9001").status_code == 200
