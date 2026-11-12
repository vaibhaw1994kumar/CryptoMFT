"""Independent reconciliation of the engine's books against the execution layer's fill journal (strategy_contract 4h.5).

The journal is written by `_on_fill` for every fill the engine books, with the intent that sent it (or an explicit kind). This module
rebuilds cash, base, fees and per-intent quantities from the journal ALONE and compares them with the engine's inventory ledger,
`trade_log` and fee schedule. It cannot see a wrong fill price (the fill model is validated elsewhere); it catches a fill that is
mis-booked, mis-fee'd, lost, double-counted, unattributed or booked outside the cap it was sent with.
"""
from __future__ import annotations

from collections import defaultdict

EXPLICIT_KINDS = ("instant_unwind", "margin_liquidation", "margin_flatten", "synthetic")


class ExecutionReconciliationError(AssertionError):
    """The fill journal and the engine's books disagree (see the `checks` of the reconciliation document)."""


def _close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


def reconcile(*, fills: list[dict], intents: dict, fee_bps: dict[str, tuple[float, float]], liquidation_fee_bps: dict[str, float] | None,
              inventory_base: dict[str, float], inventory_cash: dict[str, float], fees_paid: float, funding_by_venue: dict[str, float],
              trade_log: list[dict], cash_pnl: float, truncated: bool = False, mode: str = "count") -> dict:
    """`fee_bps[venue] = (maker_bps, taker_bps)`; `funding_by_venue` = funding PAID per venue (positive = cost)."""
    liq = liquidation_fee_bps or {}
    problems: dict[str, list] = defaultdict(list)

    more: dict[str, int] = defaultdict(int)

    def add(name: str, /, **kw) -> None:
        if len(problems[name]) < 20:
            problems[name].append(kw)
        else:
            more[name] += 1

    base_v: dict[str, float] = defaultdict(float)
    cash_v: dict[str, float] = defaultdict(float)
    fee_sum = 0.0
    filled_by_intent: dict[str, float] = defaultdict(float)
    kinds: dict[str, int] = defaultdict(int)
    notional = 0.0
    for r in fills:
        v, side, q, px = r["venue"], r["side"], r["qty"], r["price"]
        kind = r.get("kind", "child")
        kinds[kind] += 1
        # 1. attribution
        if kind == "child":
            it = intents.get(r.get("intent_id", ""))
            if it is None:
                add("unattributed", seq=r["seq"], ts=r["ts"], venue=v, side=side, qty=q, order_id=r.get("order_id"))
            else:
                if side != it.side or v not in it.venues:
                    add("intent_mismatch", seq=r["seq"], intent=it.intent_id, fill_side=side, fill_venue=v)
                filled_by_intent[it.intent_id] += q
        elif kind not in EXPLICIT_KINDS:
            add("unknown_kind", seq=r["seq"], kind=kind)
        # 2. fee recomputed from the schedule
        maker_bps, taker_bps = fee_bps[v]
        bps = (taker_bps if r["taker"] else maker_bps) + (liq.get(v, 0.0) if kind == "margin_liquidation" else 0.0)
        expected = q * px * bps / 10_000.0
        if abs(expected - r["fee"]) > 1e-9 * max(1.0, q * px):
            add("fee", seq=r["seq"], venue=v, kind=kind, booked=r["fee"], recomputed=expected)
        fee_sum += expected
        # 3. shadow ledger
        signed = q if side == "buy" else -q
        base_v[v] += signed
        cash_v[v] += -signed * px - expected
        notional += q * px
        # 5. cap compliance (taker children only; a cap of None is a legacy unbounded unwind)
        cap = r.get("cap")
        if kind == "child" and r["taker"] and cap is not None:
            tol = abs(cap) * 1e-9
            if (side == "buy" and px > cap + tol) or (side == "sell" and px < cap - tol):
                add("cap_breach", seq=r["seq"], intent=r.get("intent_id"), side=side, price=px, cap=cap)

    tol_cash = lambda n: 1e-9 * max(1.0, n)
    venues = sorted(set(inventory_base) | set(base_v) | set(inventory_cash))
    per_venue = {}
    for v in venues:
        nv = sum(r["qty"] * r["price"] for r in fills if r["venue"] == v)
        cash_shadow = cash_v.get(v, 0.0) - funding_by_venue.get(v, 0.0)
        ok_c = _close(cash_shadow, inventory_cash.get(v, 0.0), tol_cash(nv))
        ok_b = _close(base_v.get(v, 0.0), inventory_base.get(v, 0.0), 1e-9)
        per_venue[v] = {"base_journal": base_v.get(v, 0.0), "base_ledger": inventory_base.get(v, 0.0),
                        "cash_journal": cash_shadow, "cash_ledger": inventory_cash.get(v, 0.0), "ok": ok_c and ok_b}
        if not ok_c:
            add("cash", venue=v, journal=cash_shadow, ledger=inventory_cash.get(v, 0.0))
        if not ok_b:
            add("base", venue=v, journal=base_v.get(v, 0.0), ledger=inventory_base.get(v, 0.0))
    total_shadow = sum(cash_v.values()) - sum(funding_by_venue.values())
    if not _close(total_shadow, cash_pnl, tol_cash(notional)):
        add("cash_pnl", journal=total_shadow, engine=cash_pnl)
    if not _close(fee_sum, fees_paid, tol_cash(notional)):
        add("fees_paid", journal=fee_sum, engine=fees_paid)

    # 4. journal vs trade_log, row by row (order of booking is the same)
    if not truncated:
        if len(trade_log) != len(fills):
            add("trade_log_count", journal=len(fills), trade_log=len(trade_log))
        else:
            for i, (r, t) in enumerate(zip(fills, trade_log)):
                if (r["venue"], r["side"]) != (t["venue"], t["side"]) or not _close(r["qty"], t["qty"], 1e-12) \
                        or not _close(r["price"], t["price"], 1e-9 * max(1.0, t["price"])):
                    add("trade_log_row", index=i, journal=[r["venue"], r["side"], r["qty"], r["price"]],
                        trade_log=[t["venue"], t["side"], t["qty"], t["price"]])
                    break

    # 6. quantities
    overfills = []
    for iid, q in filled_by_intent.items():
        it = intents[iid]
        if q > it.qty + 1e-9:
            overfills.append({"intent": iid, "qty": it.qty, "filled": q})
    if overfills and mode == "enforce":
        add("overfill", n=len(overfills), first=overfills[:3])

    hard = [k for k, v in problems.items() if v]
    return {"checked": not truncated, "ok": truncated or not hard, "fills_journaled": len(fills), "fill_kinds": dict(kinds),
            "notional": notional, "problems": {k: v for k, v in problems.items()}, "problems_more": dict(more), "overfills": overfills[:20],
            "overfill_count": len(overfills), "per_venue": per_venue, "cash_pnl_journal": total_shadow, "cash_pnl_engine": cash_pnl,
            "fees_journal": fee_sum, "fees_engine": fees_paid, "mode": mode}


def reconcile_session(session, *, fee_bps: dict[str, tuple[float, float]], mode: str = "count") -> dict:
    """E0 step (f): the shadow reconciliation on the session runtime, the same `reconcile` as the old path. The fill journal is rebuilt from the core ledger's FILL events with the intent each order
    was sent under (the layer's journal), and cash, base and fees are recomputed from those fills and the fee schedule ALONE, then compared with the ledger's own balances, positions and fees.
    `mode` "count" records a mismatch; "enforce" raises ExecutionReconciliationError on any problem or over-fill. Observation: it reads the session after the run and writes nothing back.
    `fee_bps[venue]` = (maker_bps, taker_bps) as a positive cost (a rebate is negative)."""
    layer = session.intent_observer.layer
    ledger = session.ledger
    fills: list[dict] = []
    log: list[dict] = []
    fees_paid = 0.0
    for e in ledger.events:
        if e["event_type"] != "FILL":
            continue
        order = e["order"]
        q, px, fee = float(e["fill_quantity"]), float(e["fill_price"]), float(e["fee"])
        side = str(e["side"]).lower()
        oid = e["order_id"]
        fills.append({"seq": len(fills), "ts": int(e["ts_ns"]) / 1e9, "order_id": oid, "child_id": oid, "intent_id": layer._child_intent.get(oid, ""), "kind": "child",
                      "venue": e["venue"], "side": side, "qty": q, "price": px, "fee": fee, "taker": str(order.get("liquidity", "")).upper() == "TAKER", "cap": layer._child_cap.get(oid)})
        log.append({"venue": e["venue"], "side": side, "qty": q, "price": px})
        fees_paid += fee
    venues = sorted({f["venue"] for f in fills} | set(fee_bps))
    inst = session.cfg.instrument_id
    inv_base = {v: float(ledger.positions.get((v, inst), 0)) for v in venues}
    inv_cash = {v: float(ledger.cash_deltas.get(v, 0)) for v in venues}
    doc = reconcile(fills=fills, intents=layer.intents, fee_bps=fee_bps, liquidation_fee_bps=None, inventory_base=inv_base, inventory_cash=inv_cash, fees_paid=fees_paid,
                    funding_by_venue={}, trade_log=log, cash_pnl=sum(inv_cash.values()), truncated=bool(layer.counts.get("fills_truncated")), mode=mode)
    doc["reservations_leaked"] = layer.leaked_reservations()
    doc["funding_not_modelled"] = True
    if mode == "enforce" and (not doc["ok"] or doc["reservations_leaked"]):
        raise ExecutionReconciliationError(f"session fill journal and ledger disagree: {sorted(k for k, v in doc['problems'].items() if v)}; leaked reservations {doc['reservations_leaked']}")
    return doc
