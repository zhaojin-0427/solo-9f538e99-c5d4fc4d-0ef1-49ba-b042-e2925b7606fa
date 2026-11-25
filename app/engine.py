"""Exact exchange-rate constraint engine.

Model
-----
Let ``r_c > 0`` be the value of one unit of currency ``c`` against an
arbitrary ledger numeraire.  Only *ratios* matter.

* A quote ``1 S = [L, H] T`` is the interval constraint
  ``L <= r_S / r_T <= H``.
* A settled reimbursement order with original amount ``A`` (S) and declared
  amount ``B`` (T) pins the exact ratio ``r_S / r_T = B / A``.

Settled orders are always mutually consistent (every successful batch is
checked as a whole), so they induce an *exact potential partition*:

    r_c = mul[c] * y[root[c]]

where currencies sharing a ``root`` are pinned exactly and ``mul[root]=1``.
The remaining variables ``y_i`` (one per exact root) are bounded by quotes.

Quote intervals become multiplicative difference constraints encoded in an
all-pairs tight upper-bound matrix ``U`` with the convention

    y_j <= U[i][j] * y_i      (U[i][j] is the tightest upper bound on y_j/y_i).

Floyd-style min-product closure propagates every constraint; a diagonal entry
below 1 marks an impossible cycle (contradiction / arbitrage).

Everything here is deterministic and uses :class:`fractions.Fraction` only.
The engine never touches the database; the service feeds it the *group
summary*, so adjudication reads a constant number of summary rows per group
and zero historical order rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional

from .exact import frac_from_pair, frac_to_pair

ONE = Fraction(1, 1)


# --------------------------------------------------------------------------- #
# Issues / results
# --------------------------------------------------------------------------- #


@dataclass
class Issue:
    code: str
    message: str
    order_no: Optional[str] = None
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Weighted union-find: value[x] = pot[x] * value[parent[x]]
# --------------------------------------------------------------------------- #


class WeightedDSU:
    def __init__(self, items):
        self.parent = {x: x for x in items}
        self.size = {x: 1 for x in items}
        self.pot = {x: ONE for x in items}  # value[x] / value[parent[x]]

    def add(self, x):
        if x not in self.parent:
            self.parent[x] = x
            self.size[x] = 1
            self.pot[x] = ONE

    def find(self, x):
        """Return (root, value[x]/value[root]) with path compression."""
        p = self.parent[x]
        if p == x:
            return x, ONE
        root, pot_to_root = self.find(p)
        # old pot[x] = value[x]/value[p]; pot_to_root = value[p]/value[root]
        self.pot[x] = self.pot[x] * pot_to_root
        self.parent[x] = root
        return root, self.pot[x]

    def ratio(self, a, b):
        """Existing exact value[a]/value[b] (None if disconnected)."""
        ra, pa = self.find(a)
        rb, pb = self.find(b)
        if ra != rb:
            return None
        return pa / pb  # (value[a]/value[root]) / (value[b]/value[root])

    def union(self, a, b, e: Fraction):
        """Impose value[a]/value[b] = e exactly.

        Returns ``(ok, existing_ratio)``.  If the nodes are already joined
        with a different ratio, ``ok`` is False and the contradiction is
        returned.
        """
        ra, pa = self.find(a)  # pa = value[a]/value[ra]
        rb, pb = self.find(b)  # pb = value[b]/value[rb]
        if ra == rb:
            existing = pa / pb  # value[a]/value[b]
            return existing == e, existing

        # value[ra]/value[rb] = e * pb / pa
        root_a_over_b = e * pb / pa
        if self.size[ra] < self.size[rb]:
            # attach ra under rb: pot[ra] = value[ra]/value[rb]
            self.parent[ra] = rb
            self.pot[ra] = root_a_over_b
            self.size[rb] += self.size[ra]
        else:
            # attach rb under ra: pot[rb] = value[rb]/value[ra]
            self.parent[rb] = ra
            self.pot[rb] = ONE / root_a_over_b
            self.size[ra] += self.size[rb]
        return True, None

    def potentials(self):
        """After all unions: {x: (root, value[x]/value[root])}."""
        out = {}
        for x in self.parent:
            r, p = self.find(x)
            out[x] = (r, p)
        return out


# --------------------------------------------------------------------------- #
# Matrix helpers
# --------------------------------------------------------------------------- #


def floyd_close(u: list[list[Optional[Fraction]]]) -> list[list[Optional[Fraction]]]:
    """In-place min-product Floyd closure. Returns the same matrix."""
    m = len(u)
    for k in range(m):
        uk = u[k]
        for i in range(m):
            uik = u[i][k]
            if uik is None:
                continue
            ui = u[i]
            for j in range(m):
                ukj = uk[j]
                if ukj is None:
                    continue
                cand = uik * ukj
                cur = ui[j]
                if cur is None or cand < cur:
                    ui[j] = cand
    return u


def is_feasible_matrix(u: list[list[Optional[Fraction]]]) -> bool:
    """A closed system is feasible iff no diagonal dropped below 1."""
    for i in range(len(u)):
        di = u[i][i]
        if di is None or di < ONE:
            return False
    return True


def add_equality_arcs(u, ia: int, ib: int, e: Fraction) -> None:
    """Add exact constraint y_a/y_b = e as two tight upper arcs."""
    # y_a <= e * y_b  -> U[b][a] = e
    cur = u[ib][ia]
    if cur is None or e < cur:
        u[ib][ia] = e
    # y_b <= (1/e) * y_a -> U[a][b] = 1/e
    inv = ONE / e
    cur = u[ia][ib]
    if cur is None or inv < cur:
        u[ia][ib] = inv


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #


@dataclass
class Summary:
    """Canonical compact summary of one current currency group."""

    currencies: list[str]                 # canonical order
    root: dict[str, str]                  # currency -> exact root currency
    mul: dict[str, Fraction]              # r_c = mul[c] * y[root[c]]
    roots: list[str]                      # canonical exact-root currencies
    u: list[list[Optional[Fraction]]]     # closed upper-bound matrix over roots

    def root_index(self) -> dict[str, int]:
        return {r: i for i, r in enumerate(self.roots)}

    def to_json(self) -> dict:
        m = len(self.roots)
        return {
            "v": 1,
            "currencies": list(self.currencies),
            "root": dict(self.root),
            "mul": {c: frac_to_pair(self.mul[c]) for c in self.currencies},
            "roots": list(self.roots),
            "u": [
                [frac_to_pair(self.u[i][j]) if self.u[i][j] is not None else None
                 for j in range(m)]
                for i in range(m)
            ],
        }

    @classmethod
    def from_json(cls, data: dict) -> "Summary":
        currencies = list(data["currencies"])
        root = dict(data["root"])
        mul = {c: frac_from_pair(data["mul"][c]) for c in currencies}
        roots = list(data["roots"])
        m = len(roots)
        u: list[list[Optional[Fraction]]] = [[None] * m for _ in range(m)]
        raw = data.get("u")
        if raw is not None:
            for i in range(m):
                for j in range(m):
                    cell = raw[i][j]
                    u[i][j] = frac_from_pair(cell) if cell is not None else None
        return cls(currencies=currencies, root=root, mul=mul, roots=roots, u=u)


def identity_matrix(m: int) -> list[list[Optional[Fraction]]]:
    u: list[list[Optional[Fraction]]] = [[None] * m for _ in range(m)]
    for i in range(m):
        u[i][i] = ONE
    return u


# --------------------------------------------------------------------------- #
# Connectivity (used when quotes change and groups may merge / split)
# --------------------------------------------------------------------------- #


def connected_components(currencies, exact_root_of, quote_edges):
    """Return connected components (sorted code lists) of quotes + exact links.

    quote_edges: iterable of (source, target, ...). Only endpoints are used.
    exact_root_of: {currency: root_currency}; currencies sharing a root are
    connected by settled orders.
    """
    dsu = WeightedDSU(currencies)
    roots_seen = {}
    for c in currencies:
        r = exact_root_of.get(c, c)
        roots_seen.setdefault(r, []).append(c)
    for members in roots_seen.values():
        first = members[0]
        for c in members[1:]:
            dsu.union(first, c, ONE)  # connectivity only; ratio irrelevant here
    for e in quote_edges:
        s, t = e[0], e[1]
        dsu.add(s)
        dsu.add(t)
        dsu.union(s, t, ONE)

    comps: dict[str, list[str]] = {}
    for c in currencies:
        r, _ = dsu.find(c)
        comps.setdefault(r, []).append(c)
    return [sorted(members) for members in comps.values()]


# --------------------------------------------------------------------------- #
# Build a fresh summary from a component's active quotes + exact partition
# --------------------------------------------------------------------------- #


@dataclass
class QuoteEdge:
    quote_no: str
    source: str
    target: str
    low: Fraction
    high: Fraction


@dataclass
class BuiltGroup:
    summary: Summary
    feasible: bool
    reason: Optional[str] = None
    issues: list[Issue] = field(default_factory=list)


def build_group(codes: list[str], root_of: dict[str, str], mul_of: dict[str, Fraction],
                quotes: list[QuoteEdge]) -> BuiltGroup:
    """Recompute one group's closed summary from its active quotes."""
    codes = sorted(codes)
    root = {}
    mul = {}
    for c in codes:
        r = root_of.get(c, c)
        root[c] = r
        # Root multiplier is exactly 1; trust supplied mul otherwise.
        mul[c] = ONE if c == r else mul_of.get(c, ONE)

    roots = sorted(set(root[c] for c in codes))
    ridx = {r: i for i, r in enumerate(roots)}
    m = len(roots)
    u = identity_matrix(m)

    issues: list[Issue] = []

    # Stable insertion order makes diagnostics deterministic.
    for q in sorted(quotes, key=lambda x: x.quote_no):
        s, t = q.source, q.target
        if s not in root or t not in root:
            continue  # quote outside this component (should not happen)
        ms, mt = mul[s], mul[t]
        ia, ib = ridx[root[s]], ridx[root[t]]
        if ia == ib:
            # Exact pin already fixes r_s/r_t = ms/mt; interval must contain it.
            exact_ratio = ms / mt
            if not (q.low <= exact_ratio <= q.high):
                issues.append(Issue(
                    "QUOTE_CONFLICTS_ORDERS",
                    f"报价 {q.quote_no} 区间 [{q.low}, {q.high}] 不含已核销单锁定的 "
                    f"{s}/{t} 精确汇率 {frac_text(exact_ratio)}，该币组暂停核销",
                    extra={"quote_no": q.quote_no, "pinned_rate": frac_text(exact_ratio)},
                ))
            continue

        # y_a/y_b in [low*mt/ms, high*mt/ms]
        u[ib][ia] = _min(u[ib][ia], q.high * mt / ms)
        u[ia][ib] = _min(u[ia][ib], ms / (mt * q.low))

    floyd_close(u)
    matrix_bad = not is_feasible_matrix(u)

    reason = None
    if issues:
        reason = "；".join(i.message for i in issues)
    elif matrix_bad:
        # Diagnose the first quote that closes an impossible cycle.
        diag = _diagnose_quote_cycle(root, mul, ridx, quotes)
        if diag:
            issues.append(diag)
            reason = diag.message
        else:
            reason = "报价区间互相矛盾，闭合后存在乘积小于 1 的汇率环，该币组暂停核销"

    feasible = not issues and not matrix_bad
    summary = Summary(currencies=codes, root=root, mul=mul, roots=roots, u=u)
    return BuiltGroup(summary=summary, feasible=feasible, reason=reason, issues=issues)


def _min(a: Optional[Fraction], b: Fraction) -> Fraction:
    return b if a is None or b < a else a


def _diagnose_quote_cycle(root, mul, ridx, quotes) -> Optional[Issue]:
    """Incrementally add cross-root quotes; name the first that breaks closure."""
    m = len(ridx)
    u = identity_matrix(m)
    for q in sorted(quotes, key=lambda x: x.quote_no):
        ia, ib = ridx.get(root[q.source]), ridx.get(root[q.target])
        if ia is None or ib is None or ia == ib:
            continue
        ms, mt = mul[q.source], mul[q.target]
        u[ib][ia] = _min(u[ib][ia], q.high * mt / ms)
        u[ia][ib] = _min(u[ia][ib], ms / (mt * q.low))
        trial = [row[:] for row in u]
        floyd_close(trial)
        if not is_feasible_matrix(trial):
            return Issue(
                "QUOTE_CYCLE_CONFLICT",
                f"报价 {q.quote_no} 与组内先前报价构成矛盾汇率环（区间无法同时满足），该币组暂停核销",
                extra={"quote_no": q.quote_no},
            )
    return None


# --------------------------------------------------------------------------- #
# Batch adjudication against a stored summary only
# --------------------------------------------------------------------------- #


@dataclass
class OrderConstraint:
    order_no: str
    source: str
    target: str
    original: Fraction   # A
    declared: Fraction   # B

    @property
    def ratio(self) -> Fraction:
        return self.declared / self.original  # r_source / r_target = B/A


@dataclass
class GroupDecision:
    group_id: str
    feasible: bool
    issues: list[Issue] = field(default_factory=list)
    rates: dict[str, Fraction] = field(default_factory=dict)
    new_summary: Optional[Summary] = None
    involved_roots: list[str] = field(default_factory=list)


def order_individual_issue(summary: Summary, o: OrderConstraint) -> Optional[Issue]:
    """Check one order against the existing summary (old quotes + old orders)."""
    ridx = summary.root_index()
    rs, rt = summary.root[o.source], summary.root[o.target]
    ia, ib = ridx[rs], ridx[rt]
    ms, mt = summary.mul[o.source], summary.mul[o.target]
    q = o.ratio

    if ia == ib:
        pinned = ms / mt
        if q != pinned:
            return Issue(
                "CONTRADICTION",
                f"单据 {o.order_no} 申报汇率 {frac_text(q)} 与该币已核销单锁定的精确汇率 "
                f"{frac_text(pinned)} 不一致",
                order_no=o.order_no,
                extra={"declared_rate": frac_text(q), "pinned_rate": frac_text(pinned)},
            )
        return None

    e = q * mt / ms  # required y_a/y_b
    u = summary.u
    lower = ONE / u[ia][ib] if u[ia][ib] is not None else None  # y_a/y_b >= 1/U[a][b]
    upper = u[ib][ia]
    ok_low = lower is None or e >= lower
    ok_high = upper is None or e <= upper
    if not (ok_low and ok_high):
        rng = f"[{frac_text(lower) if lower is not None else '-∞'}, " \
              f"{frac_text(upper) if upper is not None else '+∞'}]（精确根币变量允许区间）"
        return Issue(
            "CONTRADICTION",
            f"单据 {o.order_no} 申报汇率 {frac_text(q)} 超出当前有效报价与旧单共同允许范围 {rng}",
            order_no=o.order_no,
            extra={"declared_rate": frac_text(q)},
        )
    return None


def adjudicate_group(group_id: str, summary: Summary,
                     orders: list[OrderConstraint]) -> GroupDecision:
    """Check the group's subset of a batch and compute the new summary/table."""
    decision = GroupDecision(group_id=group_id, feasible=False,
                             involved_roots=list(summary.roots))
    ridx = summary.root_index()

    # 1) Each order must be feasible on its own against stored constraints.
    prepared = []  # (order, ia, ib, e or None for same-root)
    for o in orders:
        issue = order_individual_issue(summary, o)
        if issue is not None:
            decision.issues.append(issue)
            continue
        rs, rt = summary.root[o.source], summary.root[o.target]
        ia, ib = ridx[rs], ridx[rt]
        ms, mt = summary.mul[o.source], summary.mul[o.target]
        if ia == ib:
            prepared.append((o, ia, ib, None))
        else:
            prepared.append((o, ia, ib, o.ratio * mt / ms))

    if decision.issues:
        return decision

    cross = [(o, ia, ib, e) for (o, ia, ib, e) in prepared if e is not None]

    # 2) Joint feasibility: add every exact equality, close once.
    u = [row[:] for row in summary.u]
    for _o, ia, ib, e in cross:
        add_equality_arcs(u, ia, ib, e)
    floyd_close(u)

    if not is_feasible_matrix(u):
        issue = _joint_diagnosis(summary, prepared)
        decision.issues.append(issue)
        return decision

    # 3) Feasible -> merge exact roots via weighted DSU.
    dsu = WeightedDSU(summary.roots)
    for o, ia, ib, e in cross:
        ra_code, rb_code = summary.roots[ia], summary.roots[ib]
        ok, existing = dsu.union(ra_code, rb_code, e)  # value[ra]/value[rb]=e
        if not ok:
            # Should have been caught by matrix feasibility; defensive.
            decision.issues.append(Issue(
                "CONTRADICTION",
                f"单据 {o.order_no} 与已核销精确约束矛盾: {frac_text(existing)} != {frac_text(e)}",
                order_no=o.order_no))
            return decision

    potentials = dsu.potentials()  # old_root -> (new_root, base=value[oldroot]/value[newroot])

    new_root: dict[str, str] = {}
    new_mul: dict[str, Fraction] = {}
    for c in summary.currencies:
        old_r = summary.root[c]
        new_r, base = potentials[old_r]
        new_root[c] = new_r
        new_mul[c] = summary.mul[c] * base

    new_roots = sorted(set(new_root[c] for c in summary.currencies))

    # Quotient matrix on new representatives, then re-close for canonical U.
    un = identity_matrix(len(new_roots))
    for i, ri in enumerate(new_roots):
        oi = ridx[ri]
        for j, rj in enumerate(new_roots):
            oj = ridx[rj]
            un[i][j] = u[oi][oj]
    floyd_close(un)
    if not is_feasible_matrix(un):
        decision.issues.append(Issue("CONTRADICTION", "合并精确币根后汇率矩阵不可行（内部错误）"))
        return decision

    # 4) Choose one concrete exact rate vector: anchor root y=1, y_r = U[s][r].
    anchor_root = summary.roots[0]
    s = ridx[anchor_root]
    rates: dict[str, Fraction] = {}
    for c in summary.currencies:
        rcode = summary.root[c]
        ri = ridx[rcode]
        y_r = u[s][ri]
        if y_r is None:
            decision.issues.append(Issue("CONTRADICTION", f"缺少到币 {rcode} 的换算界"))
            return decision
        rates[c] = summary.mul[c] * y_r

    # Exact verification against original submitted values.
    for o in orders:
        got = rates[o.source] / rates[o.target]
        if got != o.ratio:
            decision.issues.append(Issue(
                "CONTRADICTION",
                f"单据 {o.order_no} 折算核对失败: 表内汇率 {frac_text(got)} != 申报 {frac_text(o.ratio)}",
                order_no=o.order_no))
            return decision

    new_summary = Summary(
        currencies=list(summary.currencies),
        root=new_root,
        mul=new_mul,
        roots=new_roots,
        u=un,
    )
    decision.feasible = True
    decision.rates = rates
    decision.new_summary = new_summary
    return decision


def _joint_diagnosis(summary: Summary, prepared) -> Issue:
    """Find the first order that breaks joint feasibility and a likely partner."""
    u = [row[:] for row in summary.u]
    culprit = None
    culprit_e = None
    for o, ia, ib, e in prepared:
        if e is None:
            continue
        add_equality_arcs(u, ia, ib, e)
        trial = [row[:] for row in u]
        floyd_close(trial)
        if not is_feasible_matrix(trial):
            culprit = o
            culprit_e = e
            break

    if culprit is None:
        return Issue("CONTRADICTION", "本批折算比例与所涉币组约束合批后矛盾，整批驳回")

    # Try to name a single earlier batch order that forms a pair conflict.
    partners = []
    ridx = summary.root_index()
    for o2, ia2, ib2, e2 in prepared:
        if o2.order_no == culprit.order_no or e2 is None:
            continue
        pair = [row[:] for row in summary.u]
        add_equality_arcs(pair, ia2, ib2, e2)
        ia_c, ib_c = ridx[summary.root[culprit.source]], ridx[summary.root[culprit.target]]
        add_equality_arcs(pair, ia_c, ib_c, culprit_e)
        floyd_close(pair)
        if not is_feasible_matrix(pair):
            partners.append(o2.order_no)

    if partners:
        return Issue(
            "CONTRADICTION",
            f"单据 {culprit.order_no} 与本批单据 {', '.join(partners[:3])}"
            f" 的折算比例不能同时满足（单独均可、合批矛盾），整批驳回",
            order_no=culprit.order_no,
            extra={"conflicts_with": partners},
        )
    return Issue(
        "CONTRADICTION",
        f"单据 {culprit.order_no} 与本批先前多单及币组报价/旧单构成矛盾汇率环，"
        f"一套汇率无法同时满足，整批驳回",
        order_no=culprit.order_no,
    )


def frac_text(fr: Fraction) -> str:
    from .exact import frac_to_text
    return frac_to_text(fr)
