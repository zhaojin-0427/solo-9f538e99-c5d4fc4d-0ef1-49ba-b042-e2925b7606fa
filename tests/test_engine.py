from fractions import Fraction as F

from app.engine import (
    OrderConstraint,
    QuoteEdge,
    adjudicate_group,
    build_group,
    connected_components,
)
from app.exact import parse_rational_text


def q(no, s, t, lo, hi):
    return QuoteEdge(no, s, t, F(lo), F(hi))


def partition(codes):
    return {c: c for c in codes}, {c: F(1) for c in codes}


def test_quote_interval_builds_feasible_closure():
    ro, mo = partition(["CNY", "USD"])
    bg = build_group(["CNY", "USD"], ro, mo, [q("Q1", "USD", "CNY", 7, "7.2")])
    assert bg.feasible
    s = bg.summary
    i_cny = s.roots.index("CNY")
    i_usd = s.roots.index("USD")
    # U[CNY][USD] bounds y_USD/y_CNY <= 7.2
    assert s.u[i_cny][i_usd] == F("7.2")
    assert s.u[i_usd][i_cny] == F(1, 7)


def test_individual_orders_feasible_but_pair_conflicts():
    ro, mo = partition(["CNY", "USD"])
    bg = build_group(["CNY", "USD"], ro, mo, [q("Q1", "USD", "CNY", 7, "7.2")])
    o1 = OrderConstraint("A1", "USD", "CNY", F(100), F(710))   # 7.1
    o2 = OrderConstraint("A2", "USD", "CNY", F(100), F(720))   # 7.2

    from app.engine import order_individual_issue
    assert order_individual_issue(bg.summary, o1) is None
    assert order_individual_issue(bg.summary, o2) is None

    decision = adjudicate_group("g", bg.summary, [o1, o2])
    assert not decision.feasible
    assert any(i.code == "CONTRADICTION" for i in decision.issues)
    assert "A2" in decision.issues[0].message and "A1" in decision.issues[0].message


def test_consistent_batch_pins_exact_and_table_verifies():
    ro, mo = partition(["CNY", "USD"])
    bg = build_group(["CNY", "USD"], ro, mo, [q("Q1", "USD", "CNY", 7, "7.2")])
    o1 = OrderConstraint("A1", "USD", "CNY", F(100), F(710))
    o2 = OrderConstraint("A2", "USD", "CNY", F(200), F(1420))  # same 7.1
    d = adjudicate_group("g", bg.summary, [o1, o2])
    assert d.feasible
    assert d.rates["USD"] / d.rates["CNY"] == F("7.1")
    assert d.new_summary.roots == ["USD"] or d.new_summary.roots == ["CNY"]
    for o in (o1, o2):
        assert d.rates[o.source] / d.rates[o.target] == o.ratio


def test_triangle_pairwise_ok_but_triple_conflict():
    codes = ["CNY", "EUR", "USD"]
    ro, mo = partition(codes)
    quotes = [
        q("Q1", "USD", "CNY", "0.5", "10"),
        q("Q2", "EUR", "USD", "0.5", "10"),
        q("Q3", "EUR", "CNY", "0.5", "10"),
    ]
    bg = build_group(codes, ro, mo, quotes)
    assert bg.feasible
    orders = [
        OrderConstraint("A", "USD", "CNY", F(1), F(7)),
        OrderConstraint("B", "EUR", "USD", F(1), F("1.1")),
        OrderConstraint("C", "EUR", "CNY", F(1), F("7.0")),  # inconsistent: want 7.7
    ]
    from app.engine import order_individual_issue
    assert all(order_individual_issue(bg.summary, o) is None for o in orders)
    d = adjudicate_group("g", bg.summary, orders)
    assert not d.feasible
    assert "矛盾汇率环" in d.issues[0].message


def test_triangle_consistent_derives_cross_rate():
    codes = ["CNY", "EUR", "USD"]
    ro, mo = partition(codes)
    bg = build_group(codes, ro, mo, [
        q("Q1", "USD", "CNY", "0.5", "10"),
        q("Q2", "EUR", "USD", "0.5", "10"),
        q("Q3", "EUR", "CNY", "0.5", "10"),
    ])
    orders = [
        OrderConstraint("A", "USD", "CNY", F(1), F(7)),
        OrderConstraint("B", "EUR", "USD", F(1), F("1.1")),
        OrderConstraint("C", "EUR", "CNY", F(1), F("7.70")),
    ]
    d = adjudicate_group("g", bg.summary, orders)
    assert d.feasible
    assert d.rates["EUR"] / d.rates["CNY"] == F(77, 10)


def test_quote_arbitrage_cycle_is_infeasible():
    codes = ["CNY", "EUR", "USD"]
    ro, mo = partition(codes)
    # Impossible triangle: product forces a cycle below 1.
    bg = build_group(codes, ro, mo, [
        q("Q1", "USD", "CNY", 8, 8),      # 1 USD = 8 CNY
        q("Q2", "EUR", "USD", "1.2", "1.2"),  # 1 EUR = 1.2 USD
        q("Q3", "EUR", "CNY", 9, 9),      # 1 EUR = 9 CNY (should be 9.6)
    ])
    assert not bg.feasible
    assert bg.reason


def test_components_split_on_quote_removal_but_keep_exact_links():
    codes = ["CNY", "EUR", "JPY", "USD"]
    root_of = {c: c for c in codes}
    # Settle USD-CNY and EUR-JPY exactly.
    root_of.update({"CNY": "USD"})
    quotes = [("EUR", "USD")]  # quote-only bridge
    comps = connected_components(codes, root_of, quotes)
    assert sorted(map(sorted, comps)) == [["CNY", "EUR", "USD"], ["JPY"]]
    # Without bridge, exact CNY-USD stay together.
    comps2 = connected_components(codes, root_of, [])
    assert sorted(map(sorted, comps2)) == [["CNY", "USD"], ["EUR"], ["JPY"]]


def test_fraction_parser_accepts_decimal_and_slash():
    assert parse_rational_text("7.15") == F("143/20")
    assert parse_rational_text("143/20") == F("143/20")
    assert parse_rational_text("8") == 8
