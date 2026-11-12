"""Metric dictionary (requirement GAP02): every reported metric defined once.

Each metric has one entry in ``DICTIONARY`` (formula, unit, numerator, denominator,
sign convention, and which report carries it) and, where it is computed, one pure
function here that every report calls. Two reports printing the same name therefore
mean the same thing, and two different quantities never share a name.

Sign conventions are part of the definition and differ by design:

* **cost metrics** (slippage, implementation shortfall): positive = cost to us;
* **markouts**: positive = favourable (the price moved our way after the fill);
* **P&L**: positive = profit.

Several "net P&L" quantities exist and are kept apart (``plan_net_pnl``,
``strategy_equity_pnl``, ``maker_mark_to_market_pnl``): they differ in what they
include, and the economics reconciliation relies on that difference.

``metrics_report(...)`` gives the run-level values no other report carried (quantity
fill rate, implementation shortfall, time-weighted exposure and return); the runner
writes it to ``metrics_report.json``, a separate file, so ``summary.json`` and its
``deterministic_result_sha256`` are unchanged.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping

METRICS_SCHEMA_VERSION = "1.0.0"
BPS = Decimal("10000")


@dataclass(frozen=True)
class Metric:
    name: str
    unit: str
    definition: str
    numerator: str
    denominator: str
    sign: str
    reports: tuple[str, ...]
    notes: str = ""


DICTIONARY: dict[str, Metric] = {m.name: m for m in (
    # -- P&L ---------------------------------------------------------------------
    Metric("plan_gross_pnl", "quote currency",
           "sum over parent plans of gross_pnl (fill cash flows of every child, residual marked)",
           "sum(plan.gross_pnl)", "none", "positive = profit",
           ("summary.json gross_pnl", "pipeline_monitoring.json pnl.gross")),
    Metric("plan_fees", "quote currency", "sum over parent plans of fees (rebates negative)",
           "sum(plan.fees)", "none", "positive = cost",
           ("summary.json fees", "pipeline_monitoring.json pnl.fees")),
    Metric("plan_net_pnl", "quote currency",
           "sum over parent plans of net_pnl: trade P&L after fees and unwind cost, open residual "
           "marked at the mid. Excludes funding, borrow and transfer costs",
           "sum(plan.net_pnl)", "none", "positive = profit",
           ("summary.json net_pnl", "tca_report.json summary.net_pnl",
            "pipeline_monitoring.json pnl.net", "economic_reconciliation.json plan_net_pnl"),
           "named net_pnl in those reports; it is not account equity P&L"),
    Metric("strategy_equity_pnl", "reporting currency",
           "closing equity - opening equity from the economic ledger: includes fees, funding, "
           "borrow and transfer costs", "closing equity - opening equity", "none", "positive = profit",
           ("economic_reconciliation.json consolidated.strategy_equity_pnl",),
           "computed by economics.reconcile; differs from plan_net_pnl by the financing costs"),
    Metric("maker_mark_to_market_pnl", "quote currency",
           "maker harness: cash + inventory x last mid - initial inventory x first mid - fees",
           "see definition", "none", "positive = profit",
           ("maker report venues.*.mark_to_market_pnl_quote",),
           "separate harness; not booked in the ledger"),
    # -- execution cost ----------------------------------------------------------
    Metric("slippage_quote", "quote currency",
           "per child fill: (average fill - benchmark) x filled for a BUY, "
           "(benchmark - average fill) x filled for a SELL; benchmark = child limit price at dispatch",
           "see definition", "none", "positive = adverse; negative = price improvement",
           ("tca_report.json",)),
    Metric("weighted_slippage_bps", "basis points",
           "sum(slippage_quote) / sum(filled x benchmark) x 10000",
           "sum(slippage_quote)", "sum(filled_quantity x limit_price)",
           "positive = adverse; negative = price improvement", ("tca_report.json", "summary.json tca"),
           "benchmark is the limit price, so a limit child can only show <= 0: this measures "
           "price improvement against the limit, not implementation shortfall"),
    Metric("implementation_shortfall", "quote currency",
           "paper P&L of the decision - realized plan net_pnl. Paper P&L = quantity x (decision sell "
           "price - decision buy price). Split into execution cost (filled primary quantity vs decision "
           "price), fees, and the remainder (unfilled quantity, unwinds and residual marked)",
           "paper_pnl - plan.net_pnl", "none", "positive = cost", ("metrics_report.json",),
           "risk-flatten plans (inherited exposure) have no decision price and are excluded"),
    Metric("implementation_shortfall_bps", "basis points",
           "implementation_shortfall / decision notional x 10000",
           "sum(implementation_shortfall)", "sum(quantity x decision buy price)", "positive = cost",
           ("metrics_report.json",)),
    Metric("markout_bps", "basis points",
           "per fill: (mid at fill time + horizon - fill price) / fill price x 10000, negated for a SELL",
           "mid(t + h) - fill price", "fill price", "positive = favourable (adverse selection is negative)",
           ("maker report venues.*.markout_bps",), "mean over fills per horizon"),
    # -- fill rates --------------------------------------------------------------
    Metric("quantity_fill_rate", "ratio",
           "filled quantity / ordered quantity, PRIMARY children only",
           "sum(child.filled_quantity)", "sum(child.quantity)", "higher = more filled",
           ("metrics_report.json",), "unwind children are excluded: they are sized to the residual"),
    Metric("order_fill_rate", "ratio",
           "PRIMARY children with any fill / PRIMARY children", "children with filled_quantity > 0",
           "children", "higher = more filled", ("metrics_report.json",)),
    Metric("tca_fills", "count", "children (any role) with filled_quantity > 0", "count", "none", "count",
           ("tca_report.json summary.fills", "summary.json tca.fills")),
    Metric("monitoring_order_fills", "count", "children (any role) in state FILLED (fully filled)",
           "count", "none", "count", ("pipeline_monitoring.json orders.fills",),
           "differs from tca_fills by partially filled children"),
    Metric("maker_fill_ratio", "ratio", "maker harness: fill events / quotes sent",
           "fill events", "quotes", "higher = more filled", ("maker report venues.*.fill_ratio",),
           "counts fill events, not orders or quantity: partial fills can push it above 1"),
    # -- time-weighted -----------------------------------------------------------
    Metric("time_weighted_gross_exposure_base", "base units",
           "integral over the run of sum over (venue, instrument) of |position| dt / run duration; "
           "positions are step functions rebuilt from ledger FILL events",
           "integral sum|position| dt", "end - start", "none", ("metrics_report.json",)),
    Metric("time_weighted_net_exposure_base", "base units",
           "integral over the run of sum over instruments of |position summed across venues| dt / run "
           "duration (the cross-venue hedged exposure)", "integral |net position| dt", "end - start",
           "none", ("metrics_report.json",)),
    # -- linked pair economics (the 09 Nov markout note, section 2: three outputs, this is the third) ----------------------------
    Metric("pair_gross_bps", "bps of the maker fill price",
           "one row per maker fill: the price the hedge got against the price the maker leg got, on the hedged quantity (a maker buy hedged by a sell: hedge - maker; a maker sell: maker - hedge)",
           "side * (hedge_avg_price - maker_price) with side +1 for a maker buy", "maker fill price", "positive = the pair locked a gain before fees",
           ("session_pair_economics.json",), "the hedge price is the simulated fill at hedge arrival; where the venue data has only the best bid and ask the touch is assumed to fit the size"),
    Metric("pair_net_bps", "bps of the maker fill price",
           "pair_gross_bps plus the fees of both legs (a rebate is positive); only for a fully hedged maker fill",
           "pair_gross_bps - 1e4 * (maker_fee + hedge_fee) / (maker_price * qty)", "maker fill price", "positive = gain",
           ("session_pair_economics.json",), "no funding, no unwind cost: a maker fill whose hedge did not complete is counted apart"),
    Metric("unhedged_interval_s", "seconds",
           "from the maker fill to the last fill of its hedge", "t(last hedge fill) - t(maker fill)", "none", "seconds of open exposure for that fill",
           ("session_pair_economics.json",), "only fully hedged maker fills; the rest are counted as unhedged or partly hedged"),
    Metric("time_weighted_return", "ratio",
           "chain-linked daily returns: product(1 + daily plan net_pnl / equity at day start) - 1; "
           "equity at day start = initial capital + earlier plan net_pnl",
           "see definition", "see definition", "positive = profit", ("metrics_report.json",),
           "assumes no external flows; days are UTC dates of the plan's decision"),
)}


def definitions() -> dict[str, dict[str, Any]]:
    return {name: asdict(metric) for name, metric in sorted(DICTIONARY.items())}


# -- P&L ------------------------------------------------------------------------
def plan_total(plans: Iterable[Any], field: str) -> Decimal:
    """``sum(plan.<field>)``: plan_gross_pnl, plan_fees, plan_net_pnl, unwind cost."""
    return sum((getattr(plan, field) for plan in plans), Decimal("0"))


# -- execution cost -------------------------------------------------------------
def slippage_per_unit(side: str, benchmark: Decimal, average_price: Decimal) -> Decimal:
    """Positive = adverse: paid more on a BUY, received less on a SELL."""
    return average_price - benchmark if side == "BUY" else benchmark - average_price


def weighted_slippage_bps(slippage_quote: Decimal, benchmark_notional: Decimal) -> Decimal:
    return slippage_quote / benchmark_notional * BPS if benchmark_notional else Decimal("0")


def markout_bps(side: str, fill_price: float, later_mid: float) -> float:
    """Positive = favourable: the mid rose after a BUY or fell after a SELL."""
    move = (later_mid - fill_price) / fill_price * 10_000
    return move if side == "BUY" else -move


# -- distribution statistics and their support (the 09 Nov markout note, section 7) ---------------------------------------
# Every result carries: how many rows were kept, how many left out and why, the signed mean, the median, p5 / p95 and a block-bootstrap
# interval (blocks = days by default, so rows of one day are not treated as independent). A statistic that cannot be given says WHY:
# "unavailable" (no rows) or "insufficient_support" (too few rows or blocks for the interval). Plain floats: these are report numbers.
def quantile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolation quantile of an already sorted, non-empty list."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def block_bootstrap_ci(values: list[float], block_ids: list, *, reps: int = 1000, seed: int = 0, level: float = 0.95,
                       min_blocks: int = 2) -> list[float] | None:
    """Percentile interval of the mean, resampling whole BLOCKS (all rows of one block id together) with replacement; deterministic for a given seed.
    None when there are fewer than `min_blocks` blocks (one day of data cannot give a day-block interval)."""
    import random
    sums: dict = {}
    for v, b in zip(values, block_ids):
        s_n = sums.setdefault(b, [0.0, 0])
        s_n[0] += v
        s_n[1] += 1
    blocks = list(sums.values())
    if len(blocks) < min_blocks:
        return None
    rng = random.Random(seed)
    k = len(blocks)
    means = []
    for _ in range(reps):
        tot = cnt = 0
        for _ in range(k):
            sm, n = blocks[rng.randrange(k)]
            tot += sm
            cnt += n
        means.append(tot / cnt)
    means.sort()
    tail = (1.0 - level) / 2.0
    return [quantile(means, tail), quantile(means, 1.0 - tail)]


def distribution_stats(values: list[float], block_ids: list | None = None, *, reps: int = 1000, seed: int = 0, min_n: int = 1,
                       min_blocks: int = 2) -> dict[str, Any]:
    """n, signed mean, median, p5, p95 and the block-bootstrap interval of the mean, with the reason when something is missing.
    status: "ok" | "unavailable" (no rows) | "insufficient_support" (fewer than min_n rows). ci95 is None with ci_status
    "insufficient_blocks" when there are fewer than min_blocks blocks (or no block ids were given)."""
    n = len(values)
    if n == 0:
        return {"status": "unavailable", "n": 0, "reason": "no rows"}
    ordered = sorted(values)
    out: dict[str, Any] = {"status": "ok" if n >= min_n else "insufficient_support", "n": n, "mean": sum(values) / n,
                           "median": quantile(ordered, 0.5), "p5": quantile(ordered, 0.05), "p95": quantile(ordered, 0.95)}
    if n < min_n:
        out["reason"] = f"{n} rows, {min_n} needed"
    ci = block_bootstrap_ci(values, block_ids, reps=reps, seed=seed, min_blocks=min_blocks) if block_ids is not None else None
    out["blocks"] = len(set(block_ids)) if block_ids is not None else 0
    out["ci95"] = ci
    out["ci_status"] = "ok" if ci is not None else "insufficient_blocks"
    return out


def metadata_block(*, denominator: str, total: int, kept: int, excluded: dict[str, int] | None = None, splits: dict[str, Any] | None = None,
                   dependence: str = "", horizon_support: dict[str, Any] | None = None, notes: str = "") -> dict[str, Any]:
    """The table every result carries (09 Nov markout note, section 7): coverage (kept of total), the denominator in words, the rows left out by
    reason, horizon support where horizons exist, the split definitions and how dependent rows were treated."""
    return {"coverage": {"rows_total": total, "rows_kept": kept, "share_kept": kept / total if total else None},
            "denominator": denominator, "excluded_by_reason": dict(excluded or {}), "horizon_support": horizon_support, "splits": splits,
            "dependence": dependence, "notes": notes}


# ---- stage trace (owner 12 Nov, EMS note section 6): the evidence file of an acceptance case ----------------------------------------------------------------------------------------------------------------
_STAGE_OF_LIFECYCLE = {"SENT": "request_sent", "ACKNOWLEDGED": "exchange_effective_notified", "REPLACED": "amend_confirmed_notified", "CANCEL_REQUESTED": "cancel_requested", "AMEND_REQUESTED": "amend_requested",
                       "AMEND_ACCEPTED": "amend_accepted_not_confirmed", "CANCELLED": "cancel_notified", "PARTIAL": "fill_notified", "FILLED": "fill_notified", "REJECTED": "reject_notified", "TIMED_OUT": "timeout"}
_STAGE_OF_EVENT = {"OPPORTUNITY": "decision", "FILL": "fill_booked", "HEDGE_SUBMIT": "hedge_sent", "HEDGE_CAP_SIZE_AWARE": "hedge_cap_decided", "QUOTE_WITHDRAWN": "quote_withdrawn", "QUOTE_URGENT_CANCEL": "urgent_cancel",
                   "AMEND_REQUESTED": "amend_requested", "AMEND_ACCEPTED_NOT_CONFIRMED": "amend_accepted_not_confirmed", "AMEND_CONFIRMED": "amend_confirmed", "AMEND_REJECTED": "amend_rejected", "AMEND_TIMEOUT": "amend_timeout",
                   "AMEND_FALLBACK_CANCEL": "amend_fallback_cancel", "AMEND_LINEAGE_COMPLETE_CANCEL": "lineage_complete_cancel", "ORDER_UPDATE_DUPLICATE_SUPPRESSED": "duplicate_report_suppressed",
                   "NEW_RISK_HELD_RESTART_RECONCILIATION": "new_risk_held_restart", "HEDGE_ALARM": "alert", "UNWIND_EXHAUSTED_ALARM": "alert", "BREAKER_TRIP": "alert", "KILL_SWITCH_STATE_TRANSITION": "alert",
                   "MARGIN_PRICE_PAUSE": "alert", "HEDGE_CHAIN_OPENED": "hedge_chain_opened", "VENUE_ORDER_QUERY": "venue_queried"}


def stage_trace(events: Iterable[Mapping[str, Any]], market: Iterable[tuple[float, str, str]] = ()) -> list[dict[str, Any]]:
    """The timestamped stages of a case, one row each, in time order: market event received (`market` rows: (ts_s, venue, kind), supplied by the case), decision, request sent, exchange-effective and notified,
    fills in between, notification received, hedge sent, plus the alerts and the amend / withdrawal steps. Built from the ledger's events only: nothing is invented, a stage the run did not reach is simply absent."""
    rows: list[dict[str, Any]] = [{"ts": float(ts), "stage": "market_event_received", "venue": venue, "kind": kind} for ts, venue, kind in market]
    for e in events:
        kind = e["event_type"]
        stage = _STAGE_OF_LIFECYCLE.get(str(e.get("lifecycle_event"))) if kind == "ORDER_LIFECYCLE" else _STAGE_OF_EVENT.get(kind)
        if stage is None:
            continue
        row = {"ts": e["ts_ns"] / 1e9, "stage": stage, "event": kind}
        for k in ("order_id", "venue", "source", "side", "reason", "fill_quantity", "fill_price", "cumulative_filled_quantity", "replacement_order_id", "cap", "touch", "touch_covers", "net_edge_bps",
                  "confirmed_residual_base", "pending_hedge_qty", "oldest_exposure_age_s", "execution_blocked_reason"):
            if e.get(k) is not None:
                row[k] = str(e[k]) if not isinstance(e[k], (int, float, bool)) else e[k]
        rows.append(row)
    return sorted(rows, key=lambda r: (r["ts"], r["stage"]))


# ---- EMS path coverage (owner 12 Nov, v1_9_TOLERANCES.md section J) -----------------------------------------------------------------------------------------------------------------------------------------
def ems_path_coverage(events: list[Mapping[str, Any]], journal: list[Mapping[str, Any]], children: list[Any], hedge_order_ids: set[str], executor_diag: Mapping[str, float], clip_binding: Mapping[str, int],
                      ticks: Mapping[str, Any], lifecycle_rows: list[Mapping[str, Any]], layer_config: Mapping[str, Any] | None) -> dict[str, Any]:
    """The count of every EMS path that occurred in a run, from the ledger, the layer journal and the orders themselves, plus the per-order checks (tactic and placement J2, tick and non-loosening rounding J3).
    Every path is listed; a path that did not occur has count 0 and the note "not exercised in this run" (never omitted). `children` are the plans' ChildOrder objects."""
    from collections import Counter
    from decimal import Decimal
    ev = Counter(e["event_type"] for e in events)
    states = Counter(r["to"] for r in journal if r.get("event") == "state")
    hedge_to = Counter(e.get("to_state") for e in events if e["event_type"] == "HEDGE_STATE")
    makers = [c for c in children if c.post_only and c.order_role == "PRIMARY"]
    hedges = [c for c in children if c.order_id in hedge_order_ids]
    unwinds = [c for c in children if c.order_role == "UNWIND"]
    full = sum(1 for h in hedges if h.filled_quantity >= h.quantity > 0)
    zero = sum(1 for h in hedges if h.filled_quantity == 0)
    cap_events = ev.get("HEDGE_CAP_SIZE_AWARE", 0)
    paths: dict[str, Any] = {
        "intent_states": {s: states.get(s, 0) for s in ("accepted", "working", "partially_done", "done", "escalated", "failed", "unknown")},
        "maker_placed": len(makers), "maker_clipped_by_hedge_depth": clip_binding.get("depth", 0), "maker_sized_by_inventory": clip_binding.get("inventory", 0), "maker_sized_by_clip": clip_binding.get("clip", 0),
        "maker_withdrawn_on_event": ev.get("QUOTE_WITHDRAWN", 0), "maker_expired_at_lifetime": ev.get("ORDER_LIFETIME_EXPIRED", 0),
        "maker_cancel_while_submitted": sum(1 for r in lifecycle_rows if r.get("cancel_while_sent")),
        "hedge_ioc_full": full, "hedge_ioc_partial": len(hedges) - full - zero, "hedge_ioc_zero": zero, "hedge_retry": hedge_to.get("RETRYING", 0),
        "query_before_replace": ev.get("VENUE_ORDER_QUERY", 0), "unwind_intents": ev.get("UNWIND_INTENT", 0), "unwind_orders": len(unwinds), "flatten_market_banded": ev.get("BREAKER_FLATTEN", 0),
        "gate_refused_or_would_refuse": ev.get("RISK_GATE_REFUSED", 0) + ev.get("RISK_GATE_WOULD_REFUSE", 0), "breaker_trip": ev.get("BREAKER_TRIP", 0), "breaker_alarm": ev.get("BREAKER_ALARM", 0),
        "margin_pause": ev.get("MARGIN_PRICE_PAUSE", 0), "margin_resume": ev.get("MARGIN_PRICE_RESUME", 0), "lot_residual": ev.get("HEDGE_LOT_RESIDUAL", 0), "lot_residual_alarm": ev.get("HEDGE_LOT_RESIDUAL_ALARM", 0),
        "rate_limit_deferral": ev.get("CHILD_DISPATCH_THROTTLED", 0) + ev.get("PACKAGE_DISPATCH_THROTTLED", 0), "quote_skipped_rate_limit": ev.get("QUOTE_SKIPPED_RATE_LIMIT", 0), "duplicate_updates_suppressed": ev.get("ORDER_UPDATE_DUPLICATE_SUPPRESSED", 0),
        "late_fills_after_cancel": sum(1 for r in lifecycle_rows for f in r.get("fills", []) if r.get("cancel_conf_ts") is not None and f[0] >= r["cancel_conf_ts"]),
        "fills_during_cancel_pending": sum(1 for r in lifecycle_rows for f in r.get("fills", []) if r.get("cancel_req_ts") is not None and f[0] >= r["cancel_req_ts"] and (r.get("cancel_conf_ts") is None or f[0] < r["cancel_conf_ts"])),
        "participation_cap_would_bind": int(executor_diag.get("participation_cap_would_bind", 0)), "hedge_cap_size_aware": cap_events, "hedge_cap_touch_slack": max(0, ev.get("HEDGE_SUBMIT", 0) - cap_events),
        "amend_requested": ev.get("AMEND_REQUESTED", 0), "restart_gate_held": ev.get("NEW_RISK_HELD_RESTART_RECONCILIATION", 0), "reconciliation_disagreement": ev.get("RECONCILIATION_DISAGREEMENT", 0),
        "kill_switch_transitions": ev.get("KILL_SWITCH_STATE_TRANSITION", 0), "hedge_alarm": ev.get("HEDGE_ALARM", 0), "unwind_exhausted_alarm": ev.get("UNWIND_EXHAUSTED_ALARM", 0),
    }
    out = {"paths": paths, "not_exercised": sorted(k for k, v in paths.items() if v == 0 or (isinstance(v, dict) and not any(v.values()))), "note_zero": "count 0 = not exercised in this run (listed, never omitted)"}
    # J2: tactic and placement of every order
    tactic_rows, tactic_violations = [], []
    for c in children:
        if c.order_id in hedge_order_ids:
            tactic = "IOC_CAPPED"; ok = c.time_in_force == "IOC" and c.order_type == "LIMIT"
        elif c.order_role == "UNWIND":
            tactic = "MARKET_BANDED"; ok = True
        elif c.post_only and c.order_role == "PRIMARY":
            tactic = "PASSIVE"; ok = c.time_in_force == "GTC"
        else:
            tactic, ok = "OTHER", c.order_role != "PRIMARY"
        tactic_rows.append(tactic)
        if not ok:
            tactic_violations.append({"order_id": c.order_id, "tactic": tactic, "role": c.order_role, "tif": c.time_in_force, "type": c.order_type})
    out["tactic_by_order"] = dict(Counter(tactic_rows))
    out["tactic_violations"] = tactic_violations
    out["placement"] = (layer_config or {}).get("primary", {}).get("placement")
    out["tactic_choice"] = "refused for the arb pairing maker_taker (ExecutionConfig: tactic_choice is never set with maker_taker)"
    # J3: no off-tick price; the size-aware caps equal the non-loosening grid rule
    off_tick = []
    for c in children:
        if c.limit_price is None:
            continue
        tick = Decimal(str(ticks.get(c.venue.lower(), ticks.get(c.venue, 0)) or 0))
        if tick > 0 and Decimal(str(c.limit_price)) % tick != 0:
            off_tick.append({"order_id": c.order_id, "venue": c.venue, "price": str(c.limit_price), "tick": str(tick)})
    loosened = []
    for e in events:
        if e["event_type"] == "HEDGE_CAP_SIZE_AWARE" and e.get("cap") is not None and e.get("touch") is not None:
            cap, touch = Decimal(str(e["cap"])), Decimal(str(e["touch"]))
            if (e["side"] == "BUY" and cap < touch) or (e["side"] == "SELL" and cap > touch):
                loosened.append({"event": "cap on the wrong side of the touch", "cap": str(cap), "touch": str(touch)})
    out["checks"] = {"orders_checked": len(children), "off_tick": off_tick, "cap_on_the_wrong_side_of_touch": loosened, "pass": not off_tick and not loosened and not tactic_violations}
    return out


# ---- lifecycle report by regime (owner 12 Nov, EMS note section 6; definitions in out/integration/v1_9_TOLERANCES.md section D) ----------------------------------------------------------------------------
REGIME_DIMS = ("volatility", "spread", "liquidity")
_TERCILE = ("low", "mid", "high")


def regime_sample(prev_mid: float | None, bid: float, ask: float, bid_size: float, ask_size: float) -> tuple[float, float, float, float]:
    """One second of the maker venue's book as (volatility, spread, liquidity, mid): volatility = |mid change since the previous sample| in bps (0 for the first), spread = touch spread in bps, liquidity = bid plus ask size at the touch."""
    mid = (bid + ask) / 2.0
    vol = abs(mid - prev_mid) / prev_mid * 1e4 if prev_mid else 0.0
    return vol, (ask - bid) / mid * 1e4, bid_size + ask_size, mid


def tercile_cuts(values: list[float]) -> list[float]:
    """The two cut points (33.3rd and 66.7th percentile) of the training-set values: a value at or below the first is "low", at or below the second "mid", else "high"."""
    ordered = sorted(values)
    return [quantile(ordered, 1 / 3), quantile(ordered, 2 / 3)]


def regime_bucket(value: float | None, cuts: list[float] | None) -> str:
    if value is None or cuts is None:
        return "unbucketed"
    return _TERCILE[0] if value <= cuts[0] else _TERCILE[1] if value <= cuts[1] else _TERCILE[2]


def lifecycle_report(orders: list[Mapping[str, Any]], pairs: list[Mapping[str, Any]], markouts: list[Mapping[str, Any]], chains: list[Mapping[str, Any]], per_second: Mapping[int, tuple[float, float, float]],
                     boundaries: Mapping[str, Any] | None, *, min_bucket: int = 30) -> dict[str, Any]:
    """The lifecycle report, in full and split by regime. Every split is a PARTITION of the run (the cells add up to the totals; checked and reported in `partition_check`); a cell with fewer than `min_bucket`
    quotes prints its counts and no rate.
    orders      maker quotes: {order_id, sent_ts, ack_ts, cancel_req_ts, cancel_conf_ts, fills: [[ts, qty]...], withdrawn, cancel_while_sent, amends}
    pairs       linked pair rows (pair_economics rows): maker_fill_ts, unhedged_interval_s, fully_hedged, hedge_children
    markouts    decomposed markout rows (maker role): ts, horizon_s, m_bps
    chains      hedge chain records (state at the end, retries)
    per_second  second -> (volatility, spread, liquidity) of the maker venue's book
    boundaries  {"volatility": [c1, c2], "spread": [...], "liquidity": [...]} from the TRAINING days only, or None (then only the hour split exists)"""
    def regimes_at(ts: float | None) -> dict[str, str]:
        out = {d: "unbucketed" for d in REGIME_DIMS}
        if ts is None:
            return {**out, "utc_hour": "unbucketed"}
        sample = per_second.get(int(ts))
        if sample is not None and boundaries:
            for d, v in zip(REGIME_DIMS, sample):
                out[d] = regime_bucket(v, boundaries.get(d))
        hour = int(ts // 3600) % 24
        return {**out, "utc_hour": f"{hour:02d}"}

    def cell_of(rows: list[Mapping[str, Any]], ts_of) -> dict[str, dict[str, list]]:
        cells: dict[str, dict[str, list]] = {d: {} for d in (*REGIME_DIMS, "utc_hour")}
        for r in rows:
            for d, b in regimes_at(ts_of(r)).items():
                cells[d].setdefault(b, []).append(r)
        return cells

    def quote_stats(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
        n = len(rows)
        filled = sum(1 for r in rows if r.get("fills"))
        races_a = sum(1 for r in rows for f in r.get("fills", []) if r.get("cancel_req_ts") is not None and f[0] >= r["cancel_req_ts"] and (r.get("cancel_conf_ts") is None or f[0] < r["cancel_conf_ts"]))
        races_c = sum(1 for r in rows for f in r.get("fills", []) if r.get("cancel_conf_ts") is not None and f[0] >= r["cancel_conf_ts"])
        spreads = sorted(float(r["entry_spread_bps"]) for r in rows if r.get("entry_spread_bps") is not None)
        pnl = [float(r["net_pnl_usdt"]) for r in rows if r.get("net_pnl_usdt") is not None]
        extra = {"matched_entry_spread_bps": ({"n": len(spreads), "p50": quantile(spreads, 0.5), "min": spreads[0], "max": spreads[-1]} if spreads else {"n": 0}),
                 "lifecycle_net_pnl_usdt": {"n_quotes_with_pnl": len(pnl), "sum": sum(pnl) if pnl else None}, "label_pnl": "EXPLORATORY, mechanics only: mark = mid, no funding"}
        return {**extra, "quotes_placed": n, "quotes_filled": filled, "fill_rate": (filled / n) if n >= min_bucket else None, "withdrawn": sum(1 for r in rows if r.get("withdrawn")),
                "fills_during_cancel_pending": races_a, "cancel_while_submitted": sum(1 for r in rows if r.get("cancel_while_sent")), "late_fills_after_cancel": races_c,
                **({} if n >= min_bucket else {"note": f"n < {min_bucket}: counts only, no rate"})}

    def dist(values: list[float]) -> dict[str, Any]:
        if not values:
            return {"n": 0}
        o = sorted(values)
        return {"n": len(o), "p50": quantile(o, 0.5), "p99": quantile(o, 0.99), "max": o[-1]}

    # quotes and races, by the regime at placement
    q_cells = cell_of(list(orders), lambda r: r.get("sent_ts"))
    # residual exposure and hedge completion, by the regime at the maker fill
    p_cells = cell_of(list(pairs), lambda r: r.get("maker_fill_ts"))
    # adverse tails, by the regime at the fill
    m_cells = cell_of([m for m in markouts if m.get("role", "maker") == "maker"], lambda r: r.get("ts"))
    # message load per minute, the regime of a minute = the regime of its mean second
    minute_events: dict[int, dict[str, int]] = {}
    for r in orders:
        if r.get("sent_ts") is not None:
            minute_events.setdefault(int(r["sent_ts"] // 60), {"orders": 0, "cancels": 0, "amends": 0})["orders"] += 1
        if r.get("cancel_req_ts") is not None:
            minute_events.setdefault(int(r["cancel_req_ts"] // 60), {"orders": 0, "cancels": 0, "amends": 0})["cancels"] += 1
        for a_ts in r.get("amend_ts", []):
            minute_events.setdefault(int(a_ts // 60), {"orders": 0, "cancels": 0, "amends": 0})["amends"] += 1
    minute_rows = [{"minute": m, "ts": m * 60.0, **v} for m, v in sorted(minute_events.items())]

    def message_load(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
        return {k: dist([float(r[k]) for r in rows]) for k in ("orders", "cancels", "amends")}

    def block(q_rows, p_rows, m_rows, load_rows) -> dict[str, Any]:
        unhedged = [float(p["unhedged_interval_s"]) for p in p_rows if p.get("unhedged_interval_s") is not None]
        by_h = {}
        for h in sorted({float(m["horizon_s"]) for m in m_rows}):
            vals = sorted(float(m["m_bps"]) for m in m_rows if float(m["horizon_s"]) == h and m.get("m_bps") is not None)
            by_h[f"{h:g}s"] = {"n": len(vals), **({"p5": quantile(vals, 0.05), "p95": quantile(vals, 0.95)} if len(vals) >= 2 else {})}
        return {"quotes": quote_stats(q_rows), "message_load_per_minute": message_load(load_rows),
                "residual_exposure": {"unhedged_interval_s": dist(unhedged), "oldest_exposure_age_s": max(unhedged) if unhedged else None},
                "hedge_completion": {"maker_fills": len(p_rows), "fully_hedged": sum(1 for p in p_rows if p.get("fully_hedged")), "with_retry_or_split": sum(1 for p in p_rows if int(p.get("hedge_children") or 0) > 1)},
                "adverse_tails_markout_bps": by_h}

    # minutes get the regime of their mean second
    minute_cell: dict[str, dict[str, list]] = {d: {} for d in (*REGIME_DIMS, "utc_hour")}
    for row in minute_rows:
        secs = [per_second[s] for s in range(int(row["ts"]), int(row["ts"]) + 60) if s in per_second]
        mean = tuple(sum(x[i] for x in secs) / len(secs) for i in range(3)) if secs else None
        out = {d: "unbucketed" for d in REGIME_DIMS}
        if mean is not None and boundaries:
            for d, v in zip(REGIME_DIMS, mean):
                out[d] = regime_bucket(v, boundaries.get(d))
        out["utc_hour"] = f"{int(row['ts'] // 3600) % 24:02d}"
        for d, b in out.items():
            minute_cell[d].setdefault(b, []).append(row)
    total = block(list(orders), list(pairs), [m for m in markouts if m.get("role", "maker") == "maker"], minute_rows)
    by_regime: dict[str, dict[str, Any]] = {}
    check: dict[str, Any] = {}
    for d in (*REGIME_DIMS, "utc_hour"):
        keys = sorted(set(q_cells[d]) | set(p_cells[d]) | set(m_cells[d]) | set(minute_cell[d]))
        by_regime[d] = {k: block(q_cells[d].get(k, []), p_cells[d].get(k, []), m_cells[d].get(k, []), minute_cell[d].get(k, [])) for k in keys}
        check[d] = {"quotes": sum(len(v) for v in q_cells[d].values()) == len(orders), "maker_fills": sum(len(v) for v in p_cells[d].values()) == len(pairs),
                    "markouts": sum(len(v) for v in m_cells[d].values()) == sum(1 for m in markouts if m.get("role", "maker") == "maker"),
                    "minutes": sum(len(v) for v in minute_cell[d].values()) == len(minute_rows)}
    hedge_end = {}
    for c in chains:
        hedge_end[str(c.get("state"))] = hedge_end.get(str(c.get("state")), 0) + 1
    return {"label": "EXPLORATORY: lifecycle report by regime (definitions: v1_9_TOLERANCES.md section D)", "total": total, "by_regime": by_regime, "hedge_chains_by_final_state": hedge_end,
            "regime_boundaries": dict(boundaries) if boundaries else None, "partition_check": check, "partition_ok": all(all(v.values()) for v in check.values()), "min_bucket": min_bucket}


def _fill_fee(fill: Mapping[str, Any]) -> Any:
    """The fee of THIS fill: the ledger row carries `fee` (cumulative on the order) and `fee_delta` (this fill); a row without fee_delta is taken as a single fill."""
    value = fill.get("fee_delta")
    return value if value not in (None, "") else (fill.get("fee") or 0.0)


def pair_economics(fills: list[Mapping[str, Any]], journal: list[Mapping[str, Any]], *, block_s: float = 86_400.0, reps: int = 1000, seed: int = 0,
                   hedge_price_basis: str = "simulated fill price at hedge arrival") -> dict[str, Any]:
    """Linked pair economics, one row per maker fill: the Bybit-style maker fill against the hedge fills of the intent opened for it, from the
    ledger FILL rows (`ts_ns`, `order_id`, `venue`, `side`, `fill_quantity`, `fill_price`, `fee_delta`, else `fee`) and the intent journal (`intent` rows with role, parent_id,
    qty, created_ts, reason; `child` rows with order_id, ts). Nothing here changes a run: it reads what the run wrote.
    A maker fill with no hedge intent, or whose hedge did not fill, is a row with hedged_qty 0; one hedged only in part has fully_hedged False and no net."""
    intents = {r["intent_id"]: r for r in journal if r.get("event") == "intent"}
    children: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for r in journal:
        if r.get("event") == "child":
            children[r["intent_id"]].append((r["order_id"], float(r["ts"])))
    fills_by_order: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for f in fills:
        fills_by_order[f["order_id"]].append(f)
    hedges_of: dict[str, list[dict]] = defaultdict(list)
    for iid, it in intents.items():
        if it.get("role") == "hedge":
            hedges_of[it["parent_id"]].append(it)
    rows: list[dict[str, Any]] = []
    for iid, it in intents.items():
        if it.get("role") != "primary" or it.get("reason") != "maker leg":
            continue
        used: set[str] = set()
        maker_fills = sorted((f for oid, _ in children.get(iid, ()) for f in fills_by_order.get(oid, ())), key=lambda f: int(f["ts_ns"]))
        for mf in maker_fills:
            ts = int(mf["ts_ns"]) / 1e9
            qty, price, fee = float(mf["fill_quantity"]), float(mf["fill_price"]), float(_fill_fee(mf))
            hedge = next((h for h in sorted(hedges_of.get(iid, ()), key=lambda h: h["created_ts"])
                          if h["intent_id"] not in used and abs(float(h["created_ts"]) - ts) < 1e-4), None)
            row: dict[str, Any] = {"signal_id": it.get("signal_id"), "maker_intent_id": iid, "maker_venue": mf["venue"], "maker_side": str(mf["side"]).lower(), "maker_fill_ts": ts,
                                   "qty": qty, "maker_price": price, "maker_fee": fee, "hedge_intent_id": None, "hedge_venue": None, "hedged_qty": 0.0,
                                   "fully_hedged": False, "hedge_avg_price": None, "hedge_cap": None, "hedge_children": 0, "hedge_send_delay_s": None,
                                   "first_hedge_fill_delay_s": None, "unhedged_interval_s": None, "pair_gross_bps": None, "pair_net_bps": None, "hedge_price_basis": hedge_price_basis}
            if hedge is not None:
                used.add(hedge["intent_id"])
                hid = hedge["intent_id"]
                kids = children.get(hid, [])
                hf = sorted((f for oid, _ in kids for f in fills_by_order.get(oid, ())), key=lambda f: int(f["ts_ns"]))
                hq = sum(float(f["fill_quantity"]) for f in hf)
                row.update({"hedge_intent_id": hid, "hedge_venue": hedge["venues"][0] if hedge.get("venues") else None, "hedge_cap": hedge.get("worst_price"),
                            "hedge_children": len(kids), "hedged_qty": hq, "hedge_send_delay_s": (min(t for _, t in kids) - ts) if kids else None})
                if hf and hq > 0:
                    avg = sum(float(f["fill_quantity"]) * float(f["fill_price"]) for f in hf) / hq
                    hfee = sum(float(_fill_fee(f)) for f in hf)
                    row.update({"hedge_avg_price": avg, "first_hedge_fill_delay_s": int(hf[0]["ts_ns"]) / 1e9 - ts,
                                "fully_hedged": hq >= qty - 1e-9,
                                # a maker BUY is hedged by a SELL: the gain is hedge - maker; a maker SELL: maker - hedge
                                "pair_gross_bps": (avg - price) / price * BPS_F * (1.0 if row["maker_side"] == "buy" else -1.0)})
                    if row["fully_hedged"]:
                        row["unhedged_interval_s"] = int(hf[-1]["ts_ns"]) / 1e9 - ts
                        row["pair_net_bps"] = row["pair_gross_bps"] - (fee + hfee) / (price * qty) * BPS_F
            rows.append(row)
    blocks = [int(r["maker_fill_ts"] // block_s) for r in rows]
    def col(name, only_full=False):
        idx = [i for i, r in enumerate(rows) if r[name] is not None and (r["fully_hedged"] or not only_full)]
        return [rows[i][name] for i in idx], [blocks[i] for i in idx]
    stats = {}
    for name, full in (("pair_gross_bps", False), ("pair_net_bps", True), ("unhedged_interval_s", True), ("hedge_send_delay_s", False), ("first_hedge_fill_delay_s", False)):
        v, b = col(name, full)
        stats[name] = distribution_stats(v, b, reps=reps, seed=seed)
    n_hedge_intent = sum(1 for r in rows if r["hedge_intent_id"] is not None)
    excluded = {"no_hedge_intent": len(rows) - n_hedge_intent, "hedge_intent_but_no_fill": sum(1 for r in rows if r["hedge_intent_id"] and not r["hedged_qty"]),
                "partly_hedged": sum(1 for r in rows if r["hedged_qty"] and not r["fully_hedged"])}
    kept = sum(1 for r in rows if r["fully_hedged"])
    meta = metadata_block(denominator="maker fills (one row per maker fill; bps over the maker fill price); net and interval only for fully hedged fills", total=len(rows), kept=kept,
                          excluded=excluded, splits={"block_s": block_s, "blocks": len(set(blocks)), "days": sorted({time_day(r["maker_fill_ts"]) for r in rows})},
                          dependence="rows of one block (a day by default) are resampled together in the bootstrap interval; no fill is treated as independent of its day",
                          notes=hedge_price_basis + "; the pair leaves out funding and the cost of an unwind")
    return {"rows": rows, "summary": stats, "metadata": meta}


BPS_F = 10_000.0


def time_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def implementation_shortfall(plan: Any) -> dict[str, Decimal] | None:
    """Shortfall of one parent plan against its decision prices; None for a risk-flatten plan."""
    if plan.inherited_base:
        return None
    opportunity = plan.opportunity
    paper_pnl = opportunity.quantity * (opportunity.sell_price - opportunity.buy_price)
    execution = Decimal("0")
    for order in plan.children:
        if order.order_role != "PRIMARY" or not order.filled_quantity:
            continue
        decision = opportunity.buy_price if order.side == "BUY" else opportunity.sell_price
        execution += slippage_per_unit(order.side, decision, order.average_price) * order.filled_quantity
    shortfall = paper_pnl - plan.net_pnl
    return {
        "paper_pnl": paper_pnl,
        "realized_net_pnl": plan.net_pnl,
        "implementation_shortfall": shortfall,
        "execution_cost": execution,
        "fees": plan.fees,
        "unfilled_unwind_and_residual_cost": shortfall - execution - plan.fees,
        "decision_notional": opportunity.quantity * opportunity.buy_price,
    }


# -- fill rates -----------------------------------------------------------------
def ratio(numerator: Decimal | int, denominator: Decimal | int) -> Decimal | None:
    return Decimal(numerator) / Decimal(denominator) if denominator else None


def fill_rates(plans: Iterable[Any]) -> dict[str, Any]:
    primary = [order for plan in plans for order in plan.children if order.order_role == "PRIMARY"]
    ordered = sum((order.quantity for order in primary), Decimal("0"))
    filled = sum((order.filled_quantity for order in primary), Decimal("0"))
    with_fill = sum(bool(order.filled_quantity) for order in primary)
    return {
        "primary_children": len(primary), "primary_children_with_fill": with_fill,
        "ordered_quantity": ordered, "filled_quantity": filled,
        "quantity_fill_rate": ratio(filled, ordered),
        "order_fill_rate": ratio(with_fill, len(primary)),
    }


# -- time-weighted --------------------------------------------------------------
def time_weighted_average(steps: Iterable[tuple[int, Decimal]], start_ns: int, end_ns: int) -> Decimal | None:
    """Average of a step function over [start, end]: 0 before the first step, each
    ``(ts, value)`` holding until the next. None for an empty interval."""
    if end_ns <= start_ns:
        return None
    area = Decimal("0")
    current, since = Decimal("0"), start_ns
    for ts_ns, value in sorted(steps, key=lambda step: step[0]):
        ts_ns = min(max(ts_ns, start_ns), end_ns)
        area += current * (ts_ns - since)
        current, since = value, ts_ns
    area += current * (end_ns - since)
    return area / (end_ns - start_ns)


def exposure_steps(fills: Iterable[Mapping[str, Any]]) -> tuple[list[tuple[int, Decimal]], list[tuple[int, Decimal]]]:
    """(gross, net) exposure step functions from ledger FILL rows, in base units."""
    positions: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    gross, net = [], []
    for row in sorted(fills, key=lambda row: int(row["ts_ns"])):
        quantity = Decimal(str(row["fill_quantity"]))
        positions[(row["venue"], row["instrument_id"])] += quantity if row["side"] == "BUY" else -quantity
        by_instrument: dict[str, Decimal] = defaultdict(Decimal)
        for (_, instrument_id), position in positions.items():
            by_instrument[instrument_id] += position
        ts_ns = int(row["ts_ns"])
        gross.append((ts_ns, sum((abs(p) for p in positions.values()), Decimal("0"))))
        net.append((ts_ns, sum((abs(p) for p in by_instrument.values()), Decimal("0"))))
    return gross, net


def time_weighted_return(daily_pnl: Mapping[str, Decimal], initial_capital: Decimal) -> Decimal | None:
    """Chain-linked daily returns; None without positive starting equity."""
    if initial_capital <= 0:
        return None
    equity, growth = initial_capital, Decimal("1")
    for _, pnl in sorted(daily_pnl.items()):
        if equity <= 0:
            return None
        growth *= 1 + pnl / equity
        equity += pnl
    return growth - 1


def _utc_date(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / 1_000_000_000, timezone.utc).date().isoformat()


def _text(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _text(item) for key, item in value.items()}
    return value


def metrics_report(
    *, plans: Iterable[Any], ledger_events: Iterable[Mapping[str, Any]], start_ns: int, end_ns: int,
    initial_capital: Decimal,
) -> dict[str, Any]:
    plans = sorted(plans, key=lambda plan: (plan.opportunity.observed_ts_ns, plan.parent_id))
    shortfalls = {plan.parent_id: implementation_shortfall(plan) for plan in plans}
    measured = [row for row in shortfalls.values() if row is not None]
    total_shortfall = sum((row["implementation_shortfall"] for row in measured), Decimal("0"))
    decision_notional = sum((row["decision_notional"] for row in measured), Decimal("0"))
    fills = [row for row in ledger_events if row.get("event_type") == "FILL"]
    gross_steps, net_steps = exposure_steps(fills)
    daily: dict[str, Decimal] = defaultdict(Decimal)
    for plan in plans:
        daily[_utc_date(plan.opportunity.observed_ts_ns)] += plan.net_pnl
    return _text({
        "schema_version": METRICS_SCHEMA_VERSION,
        "period": {"start_ts_ns": start_ns, "end_ts_ns": end_ns},
        "pnl": {"plan_gross_pnl": plan_total(plans, "gross_pnl"), "plan_fees": plan_total(plans, "fees"),
                "plan_net_pnl": plan_total(plans, "net_pnl")},
        "fill_rates": fill_rates(plans),
        "implementation_shortfall": {
            "plans_measured": len(measured),
            "plans_excluded_risk_flatten": len(shortfalls) - len(measured),
            "implementation_shortfall": total_shortfall,
            "decision_notional": decision_notional,
            "implementation_shortfall_bps": (total_shortfall / decision_notional * BPS
                                             if decision_notional else None),
            "execution_cost": sum((row["execution_cost"] for row in measured), Decimal("0")),
            "fees": sum((row["fees"] for row in measured), Decimal("0")),
            "unfilled_unwind_and_residual_cost": sum(
                (row["unfilled_unwind_and_residual_cost"] for row in measured), Decimal("0")),
            "by_plan": {parent_id: row for parent_id, row in shortfalls.items() if row is not None},
        },
        "time_weighted": {
            "time_weighted_gross_exposure_base": time_weighted_average(gross_steps, start_ns, end_ns),
            "time_weighted_net_exposure_base": time_weighted_average(net_steps, start_ns, end_ns),
            "time_weighted_return": time_weighted_return(daily, initial_capital),
            "initial_capital": initial_capital,
            "daily_plan_net_pnl": dict(sorted(daily.items())),
        },
        "definitions": definitions(),
    })
