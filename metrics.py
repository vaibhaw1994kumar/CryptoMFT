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
