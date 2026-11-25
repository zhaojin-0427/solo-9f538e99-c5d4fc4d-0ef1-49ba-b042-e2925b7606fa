"""Seeded property checks: a hidden true rate vector witnesses all generated
quote intervals and consistent batches, so adjudication must stay feasible
through many sequential batches and JSON summary round-trips.
"""

import json
import random
from fractions import Fraction as F

from app.engine import (
    OrderConstraint,
    QuoteEdge,
    Summary,
    adjudicate_group,
    build_group,
    order_individual_issue,
)


def test_random_consistent_world_always_settles():
    rng = random.Random(20261125)
    for _it in range(80):
        n = rng.randint(2, 7)
        codes = [f"C{i}" for i in range(n)]
        rates = {c: F(rng.randint(1, 80), rng.randint(1, 8)) for c in codes}
        root_of = {c: c for c in codes}
        mul_of = {c: F(1) for c in codes}

        quotes = []
        for i in range(1, n):  # spanning tree keeps the group connected
            s, t = codes[i], codes[rng.randint(0, i - 1)]
            ratio = rates[s] / rates[t]
            width = F(rng.randint(1, 25), 100)
            quotes.append(QuoteEdge(f"Q{i}", s, t,
                                    ratio * (1 - width), ratio * (1 + width)))

        built = build_group(codes, root_of, mul_of, quotes)
        assert built.feasible, built.reason
        summary = Summary.from_json(json.loads(json.dumps(built.summary.to_json())))

        for b in range(3):
            orders = []
            for k in range(rng.randint(1, 5)):
                i, j = rng.sample(range(n), 2)
                s, t = codes[i], codes[j]
                amount = F(rng.randint(1, 200), rng.randint(1, 10))
                declared = amount * rates[s] / rates[t]
                orders.append(OrderConstraint(f"o{_it}-{b}-{k}", s, t, amount, declared))

            decision = adjudicate_group("g", summary, orders)
            assert decision.feasible, [x.message for x in decision.issues]
            summary = Summary.from_json(
                json.loads(json.dumps(decision.new_summary.to_json())))

        # A rate 1000x off must never be accepted.
        bad = OrderConstraint(
            "BAD", codes[0], codes[1], F(1),
            rates[codes[0]] / rates[codes[1]] * 1000)
        assert order_individual_issue(summary, bad) is not None or \
               not adjudicate_group("g", summary, [bad]).feasible
