"""Application service: quote ledger, atomic batch write-off, queries.

Every mutation runs in ONE ``BEGIN IMMEDIATE`` transaction under a process
write lock, and business records are inserted/updated in the same transaction
as the compact group summary.  Any rejection raises :class:`Rejection` before
commit so the whole batch / mutation leaves the database unchanged.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from fractions import Fraction
from typing import Any, Optional

from .db import Database
from .engine import (
    Issue,
    OrderConstraint,
    QuoteEdge,
    Summary,
    adjudicate_group,
    build_group,
    connected_components,
)
from .exact import (
    ExactParseError,
    frac_from_pair,
    frac_to_decimal_approx,
    frac_to_text,
    parse_positive_decimal,
    parse_rational_text,
)

MAX_CURRENCIES = 32
CURRENCY_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,15}$")
ONE = Fraction(1, 1)


class Rejection(Exception):
    def __init__(self, status_code: int, error: str, reasons: list[dict[str, Any]]):
        super().__init__(error)
        self.status_code = status_code
        self.error = error
        self.reasons = reasons


def _ph(n: int) -> str:
    return ",".join("?" * max(1, n))


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def normalize_ccy(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("币种代码必须是字符串")
    code = value.strip().upper()
    if not CURRENCY_RE.match(code):
        raise ValueError(f"非法币种代码 {value!r}（1-16 位字母/数字，可含 _-）")
    return code


def _reason(code: str, message: str, order_no: Optional[str] = None,
            **extra) -> dict[str, Any]:
    d: dict[str, Any] = {"code": code, "message": message}
    if order_no is not None:
        d["order_no"] = order_no
    if extra:
        d["extra"] = extra
    return d


def _issue_to_reason(i: Issue) -> dict[str, Any]:
    return _reason(i.code, i.message, i.order_no, **(i.extra or {}))


class Service:
    def __init__(self, db: Database, pricing_date: str):
        self.db = db
        self.pricing_date = pricing_date

    # ================================================================== #
    # Currency registry
    # ================================================================== #

    def _currency_bits(self, conn) -> dict[str, int]:
        rows = self.db.all(conn, "SELECT code, bit FROM currency",
                           stat="currency_registry")
        return {r["code"]: int(r["bit"]) for r in rows}

    def _ensure_currencies(self, conn, codes: set[str]) -> dict[str, int]:
        bits = self._currency_bits(conn)
        missing = sorted(c for c in codes if c not in bits)
        if len(bits) + len(missing) > MAX_CURRENCIES:
            raise Rejection(422, "CURRENCY_LIMIT", [_reason(
                "CURRENCY_LIMIT",
                f"同一计价日最多 {MAX_CURRENCIES} 种币，当前已有 {len(bits)} 种，"
                f"登记 {missing} 后将超限")])
        next_bit = (max(bits.values()) + 1) if bits else 0
        for code in missing:
            conn.execute("INSERT INTO currency(code, bit) VALUES(?, ?)",
                         (code, next_bit))
            bits[code] = next_bit
            next_bit += 1
        return bits

    # ================================================================== #
    # Quotes
    # ================================================================== #

    def create_quote(self, data) -> dict[str, Any]:
        try:
            source = normalize_ccy(data.source)
            target = normalize_ccy(data.target)
            low = parse_rational_text(data.lower, field="lower")
            high = parse_rational_text(data.upper, field="upper")
        except ValueError as e:
            raise Rejection(422, "INVALID_QUOTE",
                            [_reason("INVALID_INPUT", str(e))])
        if source == target:
            raise Rejection(422, "INVALID_QUOTE", [_reason(
                "SAME_CURRENCY", "报价的原币与目标币必须不同")])
        if low > high:
            raise Rejection(422, "INVALID_QUOTE", [_reason(
                "BAD_INTERVAL", f"区间下界 {frac_to_text(low)} 大于上界 {frac_to_text(high)}")])

        quote_no = data.quote_no.strip()
        if not quote_no:
            raise Rejection(422, "INVALID_QUOTE", [_reason(
                "INVALID_QUOTE_NO", "报价号不能为空或纯空白")])
        with self.db.transaction() as conn:
            seq = self.db.next_seq(conn)
            exists = self.db.one(conn, "SELECT quote_no FROM quote WHERE quote_no=?",
                                 (quote_no,), stat="quote_pk")
            if exists:
                raise Rejection(409, "QUOTE_NO_EXISTS", [_reason(
                    "QUOTE_NO_EXISTS",
                    f"报价号 {quote_no} 已存在；改区间请用 PUT /quotes/{quote_no}")])

            self._ensure_currencies(conn, {source, target})
            conn.execute(
                "INSERT INTO quote(quote_no,source,target,low_n,low_d,high_n,high_d,"
                "low_text,high_text,status,created_seq,updated_seq) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (quote_no, source, target, low.numerator, low.denominator,
                 high.numerator, high.denominator, data.lower.strip(),
                 data.upper.strip(), "active", seq, seq),
            )
            groups = self._recompute_region(conn, {source, target}, seq)
            row = self.db.one(conn, "SELECT * FROM quote WHERE quote_no=?",
                               (quote_no,))
            quote = self._quote_dict(row)
        return {"quote": quote, "groups": groups}

    def update_quote(self, quote_no: str, data) -> dict[str, Any]:
        try:
            low = parse_rational_text(data.lower, field="lower")
            high = parse_rational_text(data.upper, field="upper")
        except ValueError as e:
            raise Rejection(422, "INVALID_QUOTE",
                            [_reason("INVALID_INPUT", str(e))])
        if low > high:
            raise Rejection(422, "INVALID_QUOTE", [_reason(
                "BAD_INTERVAL", f"区间下界 {frac_to_text(low)} 大于上界 {frac_to_text(high)}")])

        with self.db.transaction() as conn:
            seq = self.db.next_seq(conn)
            row = self.db.one(conn, "SELECT * FROM quote WHERE quote_no=?",
                               (quote_no,), stat="quote_pk")
            if row is None:
                raise Rejection(404, "NOT_FOUND", [_reason(
                    "QUOTE_NOT_FOUND", f"报价号 {quote_no} 不存在")])
            # PUT 改区间并使报价有效；对已撤回报价相当于以新区间重新生效。
            conn.execute(
                "UPDATE quote SET low_n=?,low_d=?,high_n=?,high_d=?,low_text=?,"
                "high_text=?,status='active',updated_seq=? WHERE quote_no=?",
                (low.numerator, low.denominator, high.numerator, high.denominator,
                 data.lower.strip(), data.upper.strip(), seq, quote_no),
            )
            groups = self._recompute_region(conn, {row["source"], row["target"]}, seq)
            row = self.db.one(conn, "SELECT * FROM quote WHERE quote_no=?", (quote_no,))
            quote = self._quote_dict(row)
        return {"quote": quote, "groups": groups}

    def withdraw_quote(self, quote_no: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            seq = self.db.next_seq(conn)
            row = self.db.one(conn, "SELECT * FROM quote WHERE quote_no=?",
                               (quote_no,), stat="quote_pk")
            if row is None:
                raise Rejection(404, "NOT_FOUND", [_reason(
                    "QUOTE_NOT_FOUND", f"报价号 {quote_no} 不存在")])
            changed = row["status"] != "withdrawn"
            if changed:
                conn.execute(
                    "UPDATE quote SET status='withdrawn', updated_seq=? WHERE quote_no=?",
                    (seq, quote_no))
                groups = self._recompute_region(
                    conn, {row["source"], row["target"]}, seq)
            else:
                groups = self._groups_focus(conn, {row["source"], row["target"]})
            row = self.db.one(conn, "SELECT * FROM quote WHERE quote_no=?", (quote_no,))
            quote = self._quote_dict(row)
        return {"quote": quote, "changed": changed, "groups": groups}

    def list_quotes(self, status_filter: Optional[str] = None) -> list[dict]:
        sql = "SELECT * FROM quote"
        params: tuple = ()
        if status_filter:
            sql += " WHERE status=?"
            params = (status_filter,)
        sql += " ORDER BY quote_no"
        with self.db.reader() as conn:
            rows = self.db.all(conn, sql, params)
        return [self._quote_dict(r) for r in rows]

    def get_quote(self, quote_no: str) -> Optional[dict]:
        with self.db.reader() as conn:
            row = self.db.one(conn, "SELECT * FROM quote WHERE quote_no=?", (quote_no,))
        return self._quote_dict(row) if row else None

    def _quote_dict(self, r) -> dict[str, Any]:
        low = Fraction(r["low_n"], r["low_d"])
        high = Fraction(r["high_n"], r["high_d"])
        return {
            "quote_no": r["quote_no"],
            "source": r["source"],
            "target": r["target"],
            "lower": frac_to_text(low),
            "upper": frac_to_text(high),
            "lower_input": r["low_text"],
            "upper_input": r["high_text"],
            "status": r["status"],
            "updated_seq": r["updated_seq"],
        }

    # ================================================================== #
    # Group recomputation on quote changes (touches only involved groups)
    # ================================================================== #

    def _recompute_region(self, conn, focus: set[str], seq: int) -> list[dict]:
        focus = set(focus)

        state_rows = self.db.all(
            conn,
            f"SELECT code, group_id FROM currency_state WHERE code IN ({_ph(len(focus))})",
            tuple(sorted(focus)), stat="currency_state")
        old_gids = sorted({r["group_id"] for r in state_rows})

        region = set(focus)
        if old_gids:
            members = self.db.all(
                conn,
                f"SELECT code FROM currency_state WHERE group_id IN ({_ph(len(old_gids))})",
                tuple(old_gids), stat="currency_state_members")
            region.update(r["code"] for r in members)

        # Inherit exact partition (settled orders) from old summaries.
        root_of: dict[str, str] = {}
        mul_of: dict[str, Fraction] = {}
        if old_gids:
            sums = self.db.all(
                conn,
                f"SELECT summary_json FROM group_summary WHERE group_id IN ({_ph(len(old_gids))})",
                tuple(old_gids), stat="group_summary")
            for srow in sums:
                data = json.loads(srow["summary_json"])
                for c in data["currencies"]:
                    root_of[c] = data["root"][c]
                    mul_of[c] = frac_from_pair(data["mul"][c])

        bit_rows = self.db.all(
            conn,
            f"SELECT code, bit FROM currency WHERE code IN ({_ph(len(region))})",
            tuple(sorted(region)), stat="currency_bits_region")
        bit_map = {r["code"]: int(r["bit"]) for r in bit_rows}
        for c in region:
            root_of.setdefault(c, c)
            mul_of.setdefault(c, ONE)

        # Active quotes whose endpoints lie inside this region only.
        qrows = self.db.all(
            conn,
            "SELECT * FROM quote WHERE status='active' AND "
            f"(source IN ({_ph(len(region))}) OR target IN ({_ph(len(region))}))",
            tuple(sorted(region)) + tuple(sorted(region)),
            stat="quote_region")
        quotes: list[QuoteEdge] = []
        for q in qrows:
            if q["source"] in region and q["target"] in region:
                quotes.append(QuoteEdge(
                    q["quote_no"], q["source"], q["target"],
                    Fraction(q["low_n"], q["low_d"]),
                    Fraction(q["high_n"], q["high_d"])))

        components = connected_components(
            sorted(region), root_of, [(q.source, q.target) for q in quotes])

        # Replace old summary rows + state mapping for exactly this region.
        if old_gids:
            conn.execute(
                f"DELETE FROM group_summary WHERE group_id IN ({_ph(len(old_gids))})",
                tuple(old_gids))
        conn.execute(
            f"DELETE FROM currency_state WHERE code IN ({_ph(len(region))})",
            tuple(sorted(region)))

        result: list[dict] = []
        for codes in components:
            cset = set(codes)
            qc = [q for q in quotes if q.source in cset and q.target in cset]
            built = build_group(codes, root_of, mul_of, qc)
            gid = _new_id("G")
            mask = 0
            for c in codes:
                mask |= 1 << int(bit_map[c])
            conn.execute(
                "INSERT INTO group_summary(group_id,mask,version,status,reason,"
                "currencies_json,summary_json,created_seq,updated_seq) "
                "VALUES(?,?,1,?,?,?,?,?,?)",
                (gid, mask,
                 "active" if built.feasible else "suspended",
                 built.reason, json.dumps(codes),
                 json.dumps(built.summary.to_json()), seq, seq))
            for c in codes:
                conn.execute(
                    "INSERT INTO currency_state(code,group_id) VALUES(?,?)", (c, gid))
            result.append({
                "group_id": gid,
                "mask": mask,
                "currencies": codes,
                "status": "active" if built.feasible else "suspended",
                "reason": built.reason,
                "issues": [_issue_to_reason(i) for i in built.issues],
            })
        return result

    def _groups_focus(self, conn, focus: set[str]) -> list[dict]:
        rows = self.db.all(
            conn,
            f"SELECT DISTINCT gs.* FROM currency_state cs "
            f"JOIN group_summary gs ON gs.group_id=cs.group_id "
            f"WHERE cs.code IN ({_ph(len(focus))})",
            tuple(sorted(focus)), stat="group_summary")
        return [self._group_dict(r) for r in rows]

    # ================================================================== #
    # Batch write-off
    # ================================================================== #

    def submit_batch(self, data) -> dict[str, Any]:
        # ---- Phase 0: exact parse / format (no database writes) ----------
        parsed: list[dict[str, Any]] = []
        format_issues: list[dict[str, Any]] = []
        for i, o in enumerate(data.orders):
            no = (o.order_no or "").strip()
            if not no:
                format_issues.append(_reason(
                    "INVALID_ORDER_NO", f"第 {i + 1} 行单号不能为空或纯空白"))
            label = no or f"#第{i + 1}行"
            entry: dict[str, Any] = {"index": i, "order_no": label, "raw": o}
            try:
                source = normalize_ccy(o.source)
                target = normalize_ccy(o.target)
                entry["source"] = source
                entry["target"] = target
                if source == target:
                    format_issues.append(_reason(
                        "SAME_CURRENCY",
                        f"单据 {label} 的两种币必须不同（{source}）", label))
            except ValueError as e:
                format_issues.append(_reason("INVALID_CURRENCY", str(e), label))

            try:
                a = parse_positive_decimal(o.original_amount, field=f"单据 {label} 原币额")
                entry["original"] = a
            except ExactParseError as e:
                format_issues.append(_reason("INVALID_AMOUNT", str(e), label))
            try:
                b = parse_positive_decimal(o.declared_amount, field=f"单据 {label} 申报折算额")
                entry["declared"] = b
            except ExactParseError as e:
                format_issues.append(_reason("INVALID_AMOUNT", str(e), label))
            parsed.append(entry)

        if format_issues:
            raise Rejection(422, "INVALID_INPUT", format_issues)

        no_counts = Counter(p["order_no"] for p in parsed)
        intra_dup = {no for no, c in no_counts.items() if c > 1}

        with self.db.transaction() as conn:
            seq = self.db.next_seq(conn)  # rolled back if the batch is rejected
            reasons: list[dict[str, Any]] = []

            for no in sorted(intra_dup):
                reasons.append(_reason(
                    "DUPLICATE_ORDER_NO",
                    f"单号 {no} 在本批中重复出现 {no_counts[no]} 次，整批不得核销", no))

            # ---- duplicate against the ledger (indexed PK point reads) ---
            for no in no_counts:
                row = self.db.one(
                    conn, "SELECT 1 FROM ord_item WHERE order_no=?", (no,),
                    stat="ord_item_lookup")
                if row is not None:
                    reasons.append(_reason(
                        "DUPLICATE_ORDER_NO",
                        f"单号 {no} 已核销过，不能重复核销（重号）", no))

            # ---- chain / current group for every order -------------------
            ccys = sorted({p["source"] for p in parsed}
                          | {p["target"] for p in parsed})
            state_rows = self.db.all(
                conn,
                f"SELECT code, group_id FROM currency_state WHERE code IN ({_ph(len(ccys))})",
                tuple(ccys), stat="currency_state")
            state = {r["code"]: r["group_id"] for r in state_rows}

            order_group: dict[int, str] = {}
            for p in parsed:
                no, s, t = p["order_no"], p["source"], p["target"]
                gs, gt = state.get(s), state.get(t)
                if gs is None or gt is None:
                    missing = [c for c in (s, t) if c not in state]
                    reasons.append(_reason(
                        "MISSING_CHAIN",
                        f"单据 {no} 缺换算链：币 {', '.join(missing)} 尚无任何有效报价/已核销单连接",
                        no))
                elif gs != gt:
                    reasons.append(_reason(
                        "MISSING_CHAIN",
                        f"单据 {no} 缺换算链：{s} 与 {t} 分属不同币组，"
                        f"需先有有效报价把它们连起来", no))
                else:
                    order_group[p["index"]] = gs

            # ---- load ONLY the involved group summaries ------------------
            gids = sorted(set(order_group.values()))
            grows: dict[str, Any] = {}
            summaries: dict[str, Summary] = {}
            if gids:
                rows = self.db.all(
                    conn,
                    f"SELECT * FROM group_summary WHERE group_id IN ({_ph(len(gids))})",
                    tuple(gids), stat="group_summary")
                for r in rows:
                    grows[r["group_id"]] = r
                    summaries[r["group_id"]] = Summary.from_json(
                        json.loads(r["summary_json"]))
            for gid in gids:
                if gid not in grows:  # defensive: state points at missing summary
                    for idx, g in list(order_group.items()):
                        if g == gid:
                            reasons.append(_reason(
                                "MISSING_SUMMARY", f"币组 {gid} 汇总缺失",
                                parsed[idx]["order_no"]))

            by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for idx, gid in order_group.items():
                by_group[gid].append(parsed[idx])

            decisions: dict[str, Any] = {}
            for gid, items in by_group.items():
                grow = grows.get(gid)
                if grow is None:
                    continue
                if grow["status"] == "suspended":
                    clist = ",".join(json.loads(grow["currencies_json"]))
                    why = grow["reason"] or "约束矛盾"
                    reasons.append(_reason(
                        "GROUP_SUSPENDED",
                        f"币组 {clist} 因「{why}」暂停核销；请先改正或撤回冲突报价",
                        extra={"group_id": gid}))
                    continue
                summary = summaries[gid]
                ocs = [OrderConstraint(p["order_no"], p["source"], p["target"],
                                        p["original"], p["declared"]) for p in items]
                decision = adjudicate_group(gid, summary, ocs)
                decisions[gid] = decision
                for iss in decision.issues:
                    reasons.append(_issue_to_reason(iss))

            if reasons:
                # Raising inside `with transaction()` triggers ROLLBACK:
                # seq bump and nothing else is persisted.
                raise Rejection(409, "BATCH_REJECTED", reasons)

            # ===================== COMMIT-TIME WRITES =====================
            batch_id = _new_id("B")
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO batch(batch_id,seq,order_count,created_at) VALUES(?,?,?,?)",
                (batch_id, seq, len(data.orders), now))

            order_results: list[dict[str, Any]] = []
            group_results: list[dict[str, Any]] = []

            for gid, decision in decisions.items():
                grow = grows[gid]
                summary = summaries[gid]
                snapshot_id = _new_id("S")
                table = self._build_table(summary.roots[0], decision.rates)
                conn.execute(
                    "INSERT INTO conversion_snapshot(snapshot_id,batch_id,group_id,"
                    "mask,anchor,table_json,seq) VALUES(?,?,?,?,?,?,?)",
                    (snapshot_id, batch_id, gid, int(grow["mask"]),
                     summary.roots[0], json.dumps(table), seq))

                ns: Summary = decision.new_summary
                conn.execute(
                    "UPDATE group_summary SET version=version+1, status='active', "
                    "reason=NULL, currencies_json=?, summary_json=?, updated_seq=? "
                    "WHERE group_id=?",
                    (json.dumps(ns.currencies), json.dumps(ns.to_json()), seq, gid))

                for p in by_group[gid]:
                    s, t = p["source"], p["target"]
                    rate = decision.rates[s] / decision.rates[t]
                    conn.execute(
                        "INSERT INTO ord_item(order_no,batch_id,seq,group_id,snapshot_id,"
                        "source,target,orig_text,decl_text,orig_n,orig_d,decl_n,decl_d,"
                        "rate_n,rate_d,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (p["order_no"], batch_id, seq, gid, snapshot_id, s, t,
                         o_original_text(p), o_declared_text(p),
                         p["original"].numerator, p["original"].denominator,
                         p["declared"].numerator, p["declared"].denominator,
                         rate.numerator, rate.denominator, now))
                    expected = p["original"] * rate
                    order_results.append({
                        "order_no": p["order_no"],
                        "group_id": gid,
                        "snapshot_id": snapshot_id,
                        "source": s, "target": t,
                        "original_amount": frac_to_text(p["original"]),
                        "declared_amount": frac_to_text(p["declared"]),
                        "rate": frac_to_text(rate),
                        "rate_decimal": frac_to_decimal_approx(rate),
                        "exact_check": expected == p["declared"],
                    })

                group_results.append({
                    "group_id": gid,
                    "snapshot_id": snapshot_id,
                    "currencies": json.loads(grow["currencies_json"]),
                    "table": self._table_response(table),
                })

        return {
            "accepted": True,
            "batch_id": batch_id,
            "seq": seq,
            "order_count": len(order_results),
            "orders": order_results,
            "groups": group_results,
        }

    # ================================================================== #
    # Conversion table
    # ================================================================== #

    def _build_table(self, anchor: str, rates: dict[str, Fraction]) -> dict[str, Any]:
        """Precise all-pairs table. ``rates[c]`` is in units where anchor=1."""
        codes = sorted(rates)
        unit_rates = {c: frac_to_text(rates[c]) for c in codes}
        pairs: dict[str, str] = {}
        for s in codes:
            for t in codes:
                if s != t:
                    pairs[f"{s}/{t}"] = frac_to_text(rates[s] / rates[t])
        return {"anchor": anchor, "unit_rates": unit_rates, "pairs": pairs}

    def _table_response(self, table: dict[str, Any]) -> dict[str, Any]:
        # unit_rates / pairs are canonical exact "n/d" text; add approximate
        # decimals purely for human reading in /docs and API responses.
        unit_rates_decimal = {
            c: frac_to_decimal_approx(Fraction(v)) for c, v in table["unit_rates"].items()
        }
        pairs_decimal = {
            k: frac_to_decimal_approx(Fraction(v)) for k, v in table["pairs"].items()
        }
        return {
            "anchor": table["anchor"],
            "unit_rates": table["unit_rates"],
            "unit_rates_decimal": unit_rates_decimal,
            "pairs": table["pairs"],
            "pairs_decimal": pairs_decimal,
        }

    # ================================================================== #
    # Read-back
    # ================================================================== #

    def get_order(self, order_no: str) -> Optional[dict[str, Any]]:
        with self.db.reader() as conn:
            r = self.db.one(conn, "SELECT * FROM ord_item WHERE order_no=?",
                            (order_no,), stat="ord_item_pk")
            if r is None:
                return None
            snap = self.db.one(
                conn, "SELECT * FROM conversion_snapshot WHERE snapshot_id=?",
                (r["snapshot_id"],))
            batch = self.db.one(conn, "SELECT * FROM batch WHERE batch_id=?",
                                (r["batch_id"],))
        rate = Fraction(r["rate_n"], r["rate_d"])
        orig = Fraction(r["orig_n"], r["orig_d"])
        decl = Fraction(r["decl_n"], r["decl_d"])
        table = json.loads(snap["table_json"]) if snap else None
        return {
            "order_no": r["order_no"],
            "batch_id": r["batch_id"],
            "seq": r["seq"],
            "group_id": r["group_id"],
            "source": r["source"],
            "target": r["target"],
            "original_amount": r["orig_text"],
            "declared_amount": r["decl_text"],
            "original_amount_exact": frac_to_text(orig),
            "declared_amount_exact": frac_to_text(decl),
            "applied_rate": frac_to_text(rate),
            "applied_rate_decimal": frac_to_decimal_approx(rate, 18),
            "exact_check": orig * rate == decl,
            "expected_declared_exact": frac_to_text(orig * rate),
            "submitted_at": r["created_at"],
            "batch_created_at": batch["created_at"] if batch else None,
            "conversion_snapshot": {
                "snapshot_id": r["snapshot_id"],
                "seq": snap["seq"] if snap else None,
                **self._table_response(table),
            } if table else None,
        }

    def get_batch(self, batch_id: str) -> Optional[dict[str, Any]]:
        with self.db.reader() as conn:
            b = self.db.one(conn, "SELECT * FROM batch WHERE batch_id=?", (batch_id,))
            if b is None:
                return None
            orders = self.db.all(
                conn, "SELECT order_no FROM ord_item WHERE batch_id=? ORDER BY rowid",
                (batch_id,))
            snaps = self.db.all(
                conn, "SELECT * FROM conversion_snapshot WHERE batch_id=?", (batch_id,))
        return {
            "batch_id": b["batch_id"],
            "seq": b["seq"],
            "order_count": b["order_count"],
            "created_at": b["created_at"],
            "order_nos": [r["order_no"] for r in orders],
            "snapshots": [
                {"snapshot_id": s["snapshot_id"], "group_id": s["group_id"],
                 **self._table_response(json.loads(s["table_json"]))}
                for s in snaps
            ],
        }

    def list_groups(self) -> list[dict[str, Any]]:
        with self.db.reader() as conn:
            rows = self.db.all(conn, "SELECT * FROM group_summary ORDER BY rowid")
        return [self._group_dict(r) for r in rows]

    def get_group(self, group_id: str) -> Optional[dict[str, Any]]:
        with self.db.reader() as conn:
            r = self.db.one(conn, "SELECT * FROM group_summary WHERE group_id=?",
                            (group_id,))
        if r is None:
            return None
        d = self._group_dict(r)
        d["summary"] = json.loads(r["summary_json"])
        return d

    def _group_dict(self, r) -> dict[str, Any]:
        return {
            "group_id": r["group_id"],
            "mask": r["mask"],
            "version": r["version"],
            "status": r["status"],
            "reason": r["reason"],
            "currencies": json.loads(r["currencies_json"]),
            "updated_seq": r["updated_seq"],
        }

    def meta(self) -> dict[str, Any]:
        with self.db.reader() as conn:
            ccys = self.db.one(conn, "SELECT COUNT(*) AS n FROM currency")["n"]
            quotes = self.db.one(conn, "SELECT COUNT(*) AS n FROM quote")["n"]
            orders = self.db.one(conn, "SELECT COUNT(*) AS n FROM ord_item")["n"]
            batches = self.db.one(conn, "SELECT COUNT(*) AS n FROM batch")["n"]
            seq = self.db.one(conn, "SELECT n FROM seq WHERE name='submission'")["n"]
        return {
            "pricing_date": self.pricing_date,
            "max_currencies": MAX_CURRENCIES,
            "currency_count": ccys,
            "quote_count": quotes,
            "settled_order_count": orders,
            "settled_batch_count": batches,
            "last_seq": seq,
        }


def o_original_text(p: dict[str, Any]) -> str:
    return p["raw"].original_amount.strip()


def o_declared_text(p: dict[str, Any]) -> str:
    return p["raw"].declared_amount.strip()
