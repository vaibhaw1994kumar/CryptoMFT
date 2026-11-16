from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from .economics import EconomicEventType
from .funding import is_perpetual
from .fees import Liquidity
from .instruments import InstrumentMaster
from .ledger import Ledger
from .models import (
    ChildOrder,
    Opportunity,
    OrderState,
    ParentPlan,
    PlanState,
    Side,
    TradeEvent,
    stable_id,
)
from .orderbook import BookState, OrderBook
from .ems.layer import hedge_cap_on_grid, hedge_cap_size_aware, hedge_retry_decision, limit_on_grid      # the hedge cap and retry rules live once, in the execution layer (E0 step b)
from .hedge_lifecycle import HedgeLifecycle, HedgePolicy, HedgeState
from .venue_semantics_perp import BINANCE as _MKT_BINANCE, BYBIT as _MKT_BYBIT, classify_reject
from .risk import (
    PreTradeDecision, ReconciliationReport, RiskEngine, VenueRiskSnapshot,
)
from .alignment import CrossVenueEligibilityPolicy
from .plan_store import ParentPlanStore, VenueAccountSnapshot, VenueOrderSnapshot
from .execution import (
    CanonicalOrderUpdate,
    CanonicalOrderRequest,
    ChildDispatchMeasurement,
    DEFERRED_ACTIONS,
    DispatchPolicy,
    OrderTimeoutPolicy,
    PendingRoute,
    ReleasePolicy,
    VenueOrderStatus,
    ExecutionCallback,
    ExecutionEventType,
    OrderAction,
    OrderUpdateSource,
    OrderType,
    MakerTakerIntent,
    RoutingInstruction,
    RoutingResult,
    ResidualPolicy,
    StrategyDirectedRouter,
    ThrottleAction,
    TimeInForce,
    UnwindUrgency,
    VenueRouteStatus,
)


ORDER_STATE_BY_EVENT = {
    ExecutionEventType.ACKNOWLEDGED: OrderState.ACKNOWLEDGED,
    ExecutionEventType.PARTIAL: OrderState.PARTIAL,
    ExecutionEventType.FILLED: OrderState.FILLED,
    ExecutionEventType.REJECTED: OrderState.REJECTED,
    ExecutionEventType.CANCELLED: OrderState.CANCELLED,
    ExecutionEventType.REPLACED: OrderState.ACKNOWLEDGED,
    ExecutionEventType.TIMED_OUT: OrderState.TIMED_OUT,
    ExecutionEventType.SENT: OrderState.SENT,
    ExecutionEventType.CANCEL_REQUESTED: OrderState.CANCEL_PENDING,
    ExecutionEventType.AMEND_REQUESTED: OrderState.AMEND_PENDING,
    ExecutionEventType.AMEND_ACCEPTED: OrderState.SENT,        # a request accepted is not a confirmation: the replacement order stays "sent" until the venue says it is live
}
# Only picks which update's reason is reported; the state itself is resolved from the
# facts (_resolve_order_state).
ORDER_STATE_RANK = {
    OrderState.CREATED: 0,
    OrderState.SENT: 0,
    OrderState.CANCEL_PENDING: 1,
    OrderState.AMEND_PENDING: 1,
    OrderState.ACKNOWLEDGED: 1,
    OrderState.PARTIAL: 2,
    OrderState.TIMED_OUT: 3,
    OrderState.REJECTED: 4,
    OrderState.CANCELLED: 5,
    OrderState.FILLED: 6,
}
SOURCE_RANK = {
    OrderUpdateSource.INTERNAL.value: 0,
    OrderUpdateSource.TIMER.value: 1,
    OrderUpdateSource.ADAPTER_CALLBACK.value: 2,
    OrderUpdateSource.PRIVATE_STREAM.value: 3,
    OrderUpdateSource.REST_QUERY.value: 4,
}
TERMINAL_ORDER_STATES = {
    OrderState.FILLED, OrderState.REJECTED, OrderState.TIMED_OUT, OrderState.CANCELLED
}
# Orders that may still be live at the venue (risk, cancel sweeps, unwind sizing). A cancel
# in flight does not make an order dead: CANCEL_PENDING counts until confirmed.
LIVE_ORDER_STATES = frozenset({
    OrderState.CREATED, OrderState.SENT, OrderState.ACKNOWLEDGED, OrderState.PARTIAL,
    OrderState.CANCEL_PENDING, OrderState.AMEND_PENDING,
})
# Live with nothing filled yet.
UNFILLED_LIVE_ORDER_STATES = frozenset({OrderState.CREATED, OrderState.SENT, OrderState.ACKNOWLEDGED})
# Confirmed by the venue and working: a quiet GTC order here is resting, not unknown.
RESTING_ORDER_STATES = frozenset({OrderState.ACKNOWLEDGED, OrderState.PARTIAL})
# OMS02. The lifecycle matches src/new_backtest/order_state_machine.py:
#   CREATED (OMS only: built, not sent)   SENT = SUBMITTED   ACKNOWLEDGED = ACKNOWLEDGED/RESTING
#   PARTIAL = PARTIALLY_FILLED   CANCEL_PENDING = CANCEL_REQUESTED   TIMED_OUT = UNKNOWN
#   FILLED / REJECTED / CANCELLED as there.
# A step outside this table is recorded (ORDER_TRANSITION_UNEXPECTED) but never raised:
# a venue quirk must not stop the session.
ALLOWED_ORDER_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.CREATED: frozenset(set(OrderState) - {OrderState.CREATED}),   # reports may overtake SENT
    OrderState.SENT: frozenset({
        OrderState.ACKNOWLEDGED, OrderState.PARTIAL, OrderState.FILLED, OrderState.REJECTED,
        OrderState.CANCELLED, OrderState.CANCEL_PENDING, OrderState.TIMED_OUT,
    }),
    OrderState.ACKNOWLEDGED: frozenset({
        OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELLED, OrderState.CANCEL_PENDING,
        OrderState.TIMED_OUT, OrderState.AMEND_PENDING,
    }),
    OrderState.PARTIAL: frozenset({
        OrderState.FILLED, OrderState.CANCELLED, OrderState.CANCEL_PENDING, OrderState.TIMED_OUT, OrderState.AMEND_PENDING,
    }),
    OrderState.AMEND_PENDING: frozenset({       # the old order stays live: it can fill, be cancelled, be replaced (CANCELLED) or, after a venue query, be found unchanged
        OrderState.ACKNOWLEDGED, OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELLED, OrderState.CANCEL_PENDING, OrderState.TIMED_OUT,
    }),
    OrderState.CANCEL_PENDING: frozenset({
        OrderState.CANCELLED, OrderState.FILLED, OrderState.PARTIAL, OrderState.ACKNOWLEDGED,
        OrderState.REJECTED,       # a cancel raced the submit and the venue refuses the submit (R5; 11 Nov: no longer an unexpected transition)
        OrderState.TIMED_OUT,      # PARTIAL / ACKNOWLEDGED: a venue query says the cancel did not take
    }),
    OrderState.TIMED_OUT: frozenset({      # later venue evidence clears an unknown state (D-04)
        OrderState.ACKNOWLEDGED, OrderState.PARTIAL, OrderState.FILLED, OrderState.REJECTED,
        OrderState.CANCELLED, OrderState.CANCEL_PENDING,
    }),
    OrderState.CANCELLED: frozenset({OrderState.FILLED}),     # a late fill completing the order
    OrderState.FILLED: frozenset(),
    OrderState.REJECTED: frozenset(),
}
VENUE_SOURCES = frozenset({
    OrderUpdateSource.ADAPTER_CALLBACK.value, OrderUpdateSource.PRIVATE_STREAM.value,
    OrderUpdateSource.REST_QUERY.value,
})
ALLOWED_PLAN_TRANSITIONS = {
    PlanState.CREATED: {PlanState.RISK_RESERVED, PlanState.REJECTED},
    PlanState.RISK_RESERVED: {PlanState.DISPATCHED, PlanState.REJECTED},
    PlanState.DISPATCHED: {
        PlanState.ACKNOWLEDGED, PlanState.PARTIAL, PlanState.FILLED,
        PlanState.REJECTED, PlanState.TIMED_OUT,
    },
    PlanState.ACKNOWLEDGED: {
        PlanState.PARTIAL, PlanState.FILLED, PlanState.REJECTED, PlanState.TIMED_OUT,
    },
    PlanState.PARTIAL: {
        PlanState.FILLED, PlanState.REJECTED, PlanState.TIMED_OUT,
        PlanState.HEDGE_UNWIND,
    },
    PlanState.FILLED: {PlanState.RECONCILED},
    PlanState.REJECTED: {
        PlanState.FILLED, PlanState.HEDGE_UNWIND, PlanState.RECONCILED,
    },
    PlanState.TIMED_OUT: {
        PlanState.FILLED, PlanState.REJECTED, PlanState.HEDGE_UNWIND,
        PlanState.RECONCILED,
    },
    PlanState.HEDGE_UNWIND: {PlanState.RECONCILED},
    PlanState.RECONCILED: {PlanState.CLOSED},
    PlanState.CLOSED: set(),
}


class DeterministicMatcher:
    """Simulated fill engine with intra-sequence liquidity overlay.

    Two fill calls within the same book sequence cannot draw against the same
    resting size. The overlay resets automatically when ``book.sequence`` advances,
    and the canonical ``OrderBook`` is never mutated.
    """

    def __init__(self) -> None:
        self._overlay: dict[tuple[str, str, int | None], dict[Decimal, Decimal]] = {}

    def fill(self, order: ChildOrder, book: OrderBook) -> None:
        seq_key = (book.venue, book.instrument_id, book.sequence)
        consumed = self._overlay.setdefault(seq_key, {})
        # Evict stale sequences for this (venue, instrument_id) pair.
        for stale in [
            k for k in list(self._overlay)
            if k[0] == book.venue and k[1] == book.instrument_id and k != seq_key
        ]:
            del self._overlay[stale]

        levels = book.asks if order.side == Side.BUY else book.bids
        prices = sorted(levels) if order.side == Side.BUY else sorted(levels, reverse=True)
        remaining = order.quantity
        notional = Decimal("0")
        filled = Decimal("0")
        for price in prices:
            if order.side == Side.BUY and price > order.limit_price:
                break
            if order.side == Side.SELL and price < order.limit_price:
                break
            available = levels[price] - consumed.get(price, Decimal("0"))
            if available <= 0:
                continue
            take = min(remaining, available)
            consumed[price] = consumed.get(price, Decimal("0")) + take
            filled += take
            notional += take * price
            remaining -= take
            if remaining == 0:
                break

        order.filled_quantity = filled
        order.average_price = notional / filled if filled else Decimal("0")
        if filled == 0:
            order.state = OrderState.REJECTED
            order.reject_reason = "no executable liquidity"
        elif filled < order.quantity:
            order.state = OrderState.PARTIAL
        else:
            order.state = OrderState.FILLED


def instrument_assets(instruments: InstrumentMaster, venue: str, instrument_id: str) -> tuple[str, str]:
    """(position asset, quote asset) a fill moves: base for spot, the contract for a perp.

    Raises ValueError for instruments absent from the master so that a fill
    against an unknown or misconfigured instrument blocks accounting rather than
    silently posting to a wrong asset.
    """
    try:
        spec = instruments.get(venue, instrument_id)
    except (KeyError, ValueError):
        raise ValueError(
            f"instrument {venue}/{instrument_id} is not in the instrument master; "
            "register it before accepting fills"
        )
    if is_perpetual(spec.product_type):
        # A perp position is a contract balance, never spot base.
        return spec.instrument_id, spec.settlement_asset or spec.quote
    return spec.base, spec.quote


class OMS:
    def __init__(
        self,
        instruments: InstrumentMaster,
        risk: RiskEngine,
        ledger: Ledger,
        matcher: DeterministicMatcher | None = None,
        eligibility_policy: CrossVenueEligibilityPolicy | None = None,
        router: StrategyDirectedRouter | None = None,
        plan_store: ParentPlanStore | None = None,
        dispatch_policy: DispatchPolicy = DispatchPolicy.DETERMINISTIC_SEQUENTIAL,
        execution_clock_ns: Callable[[], int] | None = None,
        residual_policy: ResidualPolicy | None = None,
        order_owner_guard: Callable[[], None] | None = None,
        new_risk_guard: Callable[[], None] | None = None,
        order_timeout_policy: OrderTimeoutPolicy | None = None,
        hedge_policy: HedgePolicy | None = None,
        release_policy: ReleasePolicy | None = None,
    ):
        self.instruments = instruments
        self.risk = risk
        self.ledger = ledger
        if getattr(risk, "ledger", None) is None:
            risk.ledger = ledger  # live balances for inventory admission
        # Economic ledger: which assets an instrument's fills and fees move.
        ledger.asset_resolver = self._assets
        if getattr(risk, "instruments", None) is None:
            risk.instruments = instruments   # tiered margin admission (margin.py)
        if getattr(risk, "locked_provider", "absent") is None:
            risk.locked_provider = self.locked_balances   # ECO05: open orders lock funds
        # Fee pricing per fill delta (ECO01): what has been priced already,
        # the liquidity of each order's latest fill, and reporting-currency
        # prices for fees charged in a third token (e.g. BNB).
        self._fee_priced: dict[str, tuple[Decimal, Decimal]] = {}
        self._order_liquidity: dict[str, str] = {}
        self.asset_prices: dict[tuple[str, str], Decimal] = {}
        self.matcher = matcher or DeterministicMatcher()
        self.router = router or StrategyDirectedRouter(instruments)
        self.release_policy = release_policy or ReleasePolicy()
        self.router.release_revalidator = self._revalidate_queued       # EMS01: every queued request is re-checked when it is released
        self.plan_store = plan_store
        self.dispatch_policy = dispatch_policy
        self.execution_clock_ns = execution_clock_ns
        self.residual_policy = residual_policy or ResidualPolicy(
            max_residual_base=risk.limits.max_residual_base,
            max_residual_age_ns=5_000_000_000,
        )
        self.order_owner_guard = order_owner_guard
        self.new_risk_guard = new_risk_guard
        self.order_timeout_policy = order_timeout_policy or OrderTimeoutPolicy()
        # order_id -> [next_check_ts_ns, queries_sent, parent_id]
        self._report_deadlines: dict[str, list] = {}
        self._requery_orders: set[str] = set()
        self._reject_after_fill_alarmed: set[str] = set()
        # UNWIND_EXHAUSTED_ALARM: parent_id -> alarm payload. Persistent: blocks new risk until reset_unwind_exhausted_alarm().
        self.unwind_exhausted_alarms: dict[str, dict[str, object]] = {}
        self._late_fill_alerted: set[str] = set()
        self._residual_breach_since: dict[str, int] = {}
        self._residual_next_check: dict[str, int] = {}
        self._reconciliation_retry: set[str] = set()
        self._deferred_close: dict[str, tuple[str, tuple[str, ...]]] = {}
        self.hedge_policy = hedge_policy or HedgePolicy()
        if self.hedge_policy.dust_carry not in ("off", "next_hedge"):
            raise ValueError(f"hedge dust_carry must be off or next_hedge, got {self.hedge_policy.dust_carry!r}")
        # Owner 16 Nov (hedge dust carry; hedge_faults.dust_carry next_hedge, OFF by default: every structure below stays empty and no rule reads it). _dust: (hedge venue, instrument) -> the signed carried EXPOSURE
        # (base, + long) that was too small to hedge, with the time it became non-zero and its alarm flag. _dust_plan_adj: plan -> signed exposure moved out of the plan (carried) or into it (taken into its hedge):
        # plan.residual_base = raw residual + adj, and the adjustments sum to MINUS the balance, so the dust is counted once, in the balance, and every residual rule reads a plan without it.
        self.dust_on = self.hedge_policy.dust_carry == "next_hedge"
        self._dust: dict[tuple[str, str], dict[str, Any]] = {}
        self._dust_plan_adj: dict[str, Decimal] = {}
        self._dust_host_plans: set[str] = set()
        self._dust_last_plan: dict[tuple[str, str], str] = {}
        self._dust_breach_recorded: set[str] = set()
        self.dust_stats: dict[str, Any] = {"carried_events": 0, "max_abs_base": Decimal("0"), "oldest_age_s": 0.0, "hedges_with_dust": 0, "host_plans": 0, "alarms": 0}
        self.expired_at_release: list[dict[str, Any]] = []
        self.route_mode = "count"             # E0 step (d): "count" counts a layer decision that differs from the order's venue and sends the order's own venue; "enforce" rejects the order back instead
        self.lot_residual_alarm: dict[str, Any] | None = None          # owner 12 Nov (a): a carried sub-lot residual that reaches one full hedge lot raises this PERSISTENT alarm (it stays set; there is no auto-clear)
        # Owner 12 Nov (amend semantics, CR-MQ part 2's order part): OFF by default; every caller before the requoting strategies behaves as before. See request_amend.
        self.amend_enabled = False
        self.amend_blocked: str | None = None               # owner 16 Nov: None = amends allowed; else the refusal text (real orders before case2i_passed and testnet_amend_drill_passed are recorded)
        self._lineage_of: dict[str, str] = {}              # order id -> the root order id of its amend lineage (only orders that were amended or replace one)
        self._amend_seq: dict[str, int] = {}
        self.hedge_short_vol_bps = None       # owner 12 Nov (cap size_aware): an optional callable(venue, ts_ns) -> the short-horizon volatility in bps per sqrt(second); None = no source (the volatility term is then 0, labelled in the cap event)
        self.hedge_gate = None                # E0 step (c): a callable(child, ts_ns) the session sets; it only OBSERVES the hedge child against the risk gate (count: what it would refuse); a hedge is never blocked, it reduces exposure
        self.observer = None                  # E0 step (a): an ems IntentObserver (observation only: the OMS never reads it back); None = off
        self.restart_default_lookback_ns = 24 * 3_600 * 1_000_000_000      # the account snapshot's window when there is nothing persisted to start from (no plan, no execution): the last 24 hours; and the longest gap since the last persisted state that is reconciled automatically (a longer one keeps the gate closed)
        self._flatten_shadow: set[str] = set()                              # owner 13 Nov: breaker flatten plans whose inherited exposure is already carried by other plans: only their OWN fills count toward the residual
        self._unwind_dust: dict[str, dict[str, Any]] = {}                   # owner 13 Nov: plans whose unwind remainder is below every venue's minimum: DUST, recorded once, never retried
        self._unwind_skip: dict[tuple[str, str], dict[str, Any]] = {}      # (plan, venue) -> the unavailable-venue state: reason, next retry time, delay (1 s doubling to 30 s)
        self._quote_plans: set[str] = set()                                # CR-MQ part 2 (owner 13 Nov): plans that are a market-making quote (one post-only child, no hedge); marked at creation, rebuilt from the plan store
        self._hedge_specs: dict[str, dict[str, Any]] = {}                  # FILL_TRIGGERED: the declared hedge leg of a plan (no ChildOrder until a maker fill)
        self.hedge_chains: dict[str, dict[str, HedgeLifecycle]] = {}       # parent_id -> chain_id -> chain
        self._chain_of_child: dict[str, HedgeLifecycle] = {}
        self._chain_children: dict[str, list[str]] = {}
        self._hedge_retry_due: dict[str, int] = {}                          # chain_id -> due ts ns
        self._hedge_held: dict[str, str] = {}                               # chain_id -> why the dispatch is held (bad book, price cap): re-checked on every timer
        self._hedge_dispatching = 0
        self._hedge_finalizing = 0
        self.hedge_records: list[dict[str, Any]] = []
        self.reject_records: list[dict[str, object]] = []      # Phase C: one record per rejected child, classified by the venue code table
        self._rejects_classified: set[str] = set()
        self.realism_counters: dict[str, int] = {
            "unknown_state_orders": 0,
            "venue_queries": 0,
            "resolved_by_query": 0,
            "unresolved_after_queries": 0,
            "close_deferrals": 0,
            "rejected_cancels_requeried": 0,
            "unexpected_order_transitions": 0,
            "late_evidence_after_timeout": 0,
            "new_risk_held_residual_breach": 0,
            "residual_breach_kill_escalations": 0,
            "resting_fill_reports": 0,
            "ambiguous_dispatch_errors": 0,
        }
        self._books: dict[tuple[str, str], OrderBook] = {}
        self._flatten_book_marker: dict[str, tuple[int, str]] = {}
        self._legging_bound_codes: dict[str, tuple[str, ...]] = {}
        self._last_execution_ts_ns = 0
        self.dispatch_measurements: list[ChildDispatchMeasurement] = []
        self.eligibility_policy = eligibility_policy
        # Resting-order lifetime (Phase D, D5c; profile arb.maker_max_age_s, MANDATORY there): a post-only order is cancelled this long after the venue accepted it. None = no lifetime (every other caller).
        self.order_lifetime_ns: int | None = None
        self._lifetime_deadlines: dict[str, int] = {}
        # Report deadline for a sent post-only quote (case 2l, the maker gateway outage): off, a quote whose submit or ack is lost stays SENT with no deadline until some other event looks at it (565 s in the
        # 2l replay); on, it is watched from the moment it is sent, like any order whose state is unknown: queried after the report timeout, never resent, timed out and the kill switch after max_queries.
        self.maker_ack_deadline = self.order_timeout_policy.maker_ack_deadline      # owner 13 Nov: from the policy (config oms.timeouts.maker_ack_deadline); tests and scenarios may still set the attribute
        self.alignment_rejections = 0
        self.plans: dict[str, ParentPlan] = (
            self.plan_store.load_all() if self.plan_store is not None else {}
        )
        self.recovered_plans = sum(
            plan.state != PlanState.CLOSED for plan in self.plans.values()
        )
        self._recover_hedge_state()
        # Owner 12 Nov (restart against the venue): the recovered plans that are still open are UNRECONCILED until reconcile_plan has put the venue's own account of their orders through the OMS (executions missed
        # while down are booked once there, the hedge they trigger is sent there). With restart_reconcile_gate on (default off: no caller before the live runtime restarts anything) no NEW risk is taken while any is.
        self.restart_reconcile_gate = False
        self._restart_unreconciled: set[str] = {pid for pid, plan in self.plans.items() if plan.state != PlanState.CLOSED and not (pid in self._dust_host_plans and not plan.children)}      # a dust host with no order sent yet has nothing at the venue to reconcile
        self._restart_account_problems: dict[str, list[str]] = {}          # owner 13 Nov: venue -> what its account snapshot did not settle (fetch failed, unknown order, position or execution mismatch); non-empty keeps the gate closed
        # REJECT_AFTER_FILL_ALARM is once per order across restarts: the recovered reports show which were raised.
        self._reject_after_fill_alarmed.update(
            order.order_id for plan in self.plans.values() for order in plan.children
            if order.filled_quantity > 0
            and any(row.get("state") == ExecutionEventType.REJECTED.value for row in order.state_updates)
        )
        # An unwind-exhausted alarm survives a restart until an operator resets it (the plan store keeps both events).
        if self.plan_store is not None:
            for parent_id in self.plans:
                active: dict[str, object] | None = None
                for event in self.plan_store.events(parent_id):
                    if event["event_type"] == "UNWIND_EXHAUSTED_ALARM":
                        active = dict(event["payload"], ts_ns=event["ts_ns"])
                    elif event["event_type"] == "UNWIND_EXHAUSTED_ALARM_RESET":
                        active = None
                if active is not None:
                    self.unwind_exhausted_alarms[parent_id] = active
        # Registered before recovery replays fills, so rebuilt fills consume too;
        # reservations are only rehydrated (and then consumed) further below.
        ledger.trade_fill_listeners.append(self._consume_reservation)
        if self.plans:
            recovery_ts_ns = max(
                (
                    int(plan.transitions[-1]["ts_ns"])
                    if plan.transitions else plan.opportunity.observed_ts_ns
                )
                for plan in self.plans.values()
            ) + 1
            self._last_execution_ts_ns = recovery_ts_ns
            self.ledger.rebuild_from_plans(self.plans, recovery_ts_ns)
            for plan in self.plans.values():
                for order in plan.children:
                    # Recovered fees are already on the order: never price them twice.
                    self._fee_priced[order.order_id] = (
                        order.filled_quantity, order.filled_quantity * order.average_price
                    )
            for plan in self.plans.values():
                if plan.state in {PlanState.CREATED, PlanState.CLOSED} or plan.inherited_base or plan.parent_id in self._dust_host_plans:
                    continue        # a flatten plan reduces risk and holds no reservation; neither does a dust host (owner 16 Nov)
                try:
                    reservation = self.risk.rehydrate_reservation(plan.opportunity)
                    # Filled legs already moved the balances: hold only the remainder.
                    for order in plan.children:
                        if order.order_role == "PRIMARY" and order.filled_quantity and order.quantity:
                            self.risk.consume(
                                plan.opportunity.opportunity_id, order.venue,
                                order.filled_quantity / order.quantity,
                            )
                    self.ledger.record(
                        "RECOVERY_RISK_RESERVATION_REHYDRATED", recovery_ts_ns,
                        parent_id=plan.parent_id,
                        opportunity_id=plan.opportunity.opportunity_id,
                        reservation_id=reservation.reservation_id,
                    )
                except ValueError as exc:
                    self.risk.activate_kill_switch(
                        now_ns=recovery_ts_ns,
                        reason="risk reservation rehydration failed",
                        source="OMS_RECOVERY", ledger=self.ledger,
                    )
                    self.ledger.record(
                        "RECOVERY_RISK_RESERVATION_FAILED", recovery_ts_ns,
                        parent_id=plan.parent_id,
                        opportunity_id=plan.opportunity.opportunity_id,
                        reason=str(exc),
                    )
            self._update_exposure_control(recovery_ts_ns)
            # Reconstruct report deadlines for every order that was open at
            # shutdown.  _report_deadlines is in-memory only, so after a restart
            # any order that had not yet received a definitive venue report would
            # be silently unmonitored.  Schedule an immediate re-query using the
            # same filter as _unknown_state_children: non-terminal and not a GTC
            # order that is already resting acknowledged or partially filled.
            for plan in self.plans.values():
                if plan.state == PlanState.CLOSED:
                    continue
                for order in plan.children:
                    if order.state in TERMINAL_ORDER_STATES:
                        continue
                    if (
                        order.time_in_force == TimeInForce.GTC.value
                        and order.state in RESTING_ORDER_STATES
                    ):
                        continue
                    self._watch_order(order, recovery_ts_ns, immediate=True)
            for plan in self.plans.values():
                if plan.state == PlanState.HEDGE_UNWIND:
                    self._residual_breach_since.setdefault(plan.parent_id, recovery_ts_ns)
            for plan in self.plans.values():
                if plan.state == PlanState.CLOSED:
                    continue
                if plan.state == PlanState.HEDGE_UNWIND:
                    continue
                if all(order.state in TERMINAL_ORDER_STATES for order in plan.children):
                    self._reconciliation_retry.add(plan.parent_id)

    def _exposure_mark_prices(self) -> dict[tuple[str, str], Decimal]:
        return {
            key: (book.best_bid + book.best_ask) / Decimal("2")
            for key, book in self._books.items() if book.bids and book.asks
        }

    def _update_exposure_control(self, ts_ns: int) -> dict[str, object]:
        return self.risk.update_exposure_state(
            plans=self.plans, ledger=self.ledger, now_ns=ts_ns,
            mark_prices=self._exposure_mark_prices(),
        )

    def _next_execution_ts(self, base_ts_ns: int) -> int:
        candidate = (
            int(self.execution_clock_ns())
            if self.execution_clock_ns is not None
            else base_ts_ns
        )
        timestamp = max(candidate, self._last_execution_ts_ns + 1)
        self._last_execution_ts_ns = timestamp
        return timestamp

    def _persist(
        self,
        plan: ParentPlan,
        ts_ns: int,
        event_type: str,
        **payload: object,
    ) -> None:
        if self.plan_store is not None:
            self.plan_store.save(plan, ts_ns, event_type, **payload)

    def _record_order_action(
        self,
        plan: ParentPlan,
        order: ChildOrder,
        request: CanonicalOrderRequest,
        action: str,
        status: str,
        ts_ns: int,
        target_order_id: str = "",
    ) -> None:
        payload = {
            "action": action,
            "status": status,
            "parent_id": plan.parent_id,
            "opportunity_id": order.opportunity_id,
            "order_id": order.order_id,
            "target_order_id": target_order_id,
            "request_id": request.request_id,
            "idempotency_key": request.idempotency_key,
            "venue": request.venue,
            "instrument_id": request.instrument_id,
        }
        self.ledger.record_order_action(
            action=action, status=status, ts_ns=ts_ns,
            parent_id=plan.parent_id,
            opportunity_id=order.opportunity_id,
            order_id=order.order_id,
            target_order_id=target_order_id,
            request_id=request.request_id,
            idempotency_key=request.idempotency_key,
            venue=request.venue,
            instrument_id=request.instrument_id,
        )
        self._persist(plan, ts_ns, "ORDER_LIFECYCLE", **payload)
        if order in plan.children:
            if action == "SUBMIT" and status in ("DISPATCHED", "RELEASED"):
                self._record_internal_update(plan, order, ExecutionEventType.SENT, ts_ns, request.request_id)
                if self.maker_ack_deadline and order.post_only and order.state == OrderState.SENT:
                    self._watch_order(order, ts_ns, count_unknown=False)
            elif action == "CANCEL" and status == "DISPATCHED":
                if order.state == OrderState.SENT:
                    # 11 Nov (owner, R5): a cancel that leaves while the venue has not yet answered the submit. Counted and logged so its frequency can be read from any run's ledger;
                    # the outcome is the venue's: CANCELLED (cancel wins), REJECTED (submit refused), or a fill that raced it (booked once, the order ends FILLED).
                    self.realism_counters["cancel_while_submitted"] = self.realism_counters.get("cancel_while_submitted", 0) + 1
                    self.ledger.record("CANCEL_WHILE_SUBMITTED", ts_ns, parent_id=plan.parent_id, order_id=order.order_id, venue=order.venue, order_role=order.order_role)
                self._record_internal_update(
                    plan, order, ExecutionEventType.CANCEL_REQUESTED, ts_ns, request.request_id,
                )

    def _transition(
        self,
        plan: ParentPlan,
        state: PlanState,
        ts_ns: int,
        reason: str = "",
        *,
        event_type: str | None = None,
    ) -> None:
        if state == plan.state and plan.transitions:
            return
        if state != plan.state and state not in ALLOWED_PLAN_TRANSITIONS[plan.state]:
            raise ValueError(
                f"illegal parent transition {plan.state.value} -> {state.value}"
            )
        previous_state = plan.state.value
        plan.transition(state, ts_ns, reason)
        self.ledger.record(
            "PLAN_STATE_TRANSITION",
            ts_ns,
            parent_id=plan.parent_id,
            opportunity_id=plan.opportunity.opportunity_id,
            previous_state=previous_state,
            state=state.value,
            reason=reason,
        )
        self._persist(
            plan, ts_ns, event_type or f"PLAN_{state.value}", reason=reason
        )

    def _close_without_exposure(
        self, plan: ParentPlan, ts_ns: int, outcome: PlanState, reason: str
    ) -> ParentPlan:
        self._transition(plan, outcome, ts_ns, reason)
        plan.terminal_outcome = outcome.value
        return self._release_and_close(plan, ts_ns)

    def cancel_open_orders(self, reason: str, now_ns: int) -> int:
        return self._cancel_orders(
            reason, now_ns, venue=None, preserve_risk_reducing=False,
            ledger_event="BOOK_INTEGRITY_CANCEL_ALL",
        )

    def _cancel_orders(
        self, reason: str, now_ns: int, *, venue: str | None,
        preserve_risk_reducing: bool, ledger_event: str,
    ) -> int:
        if self.order_owner_guard is not None:
            self.order_owner_guard()
        cancelled = 0
        cancel_errors = 0
        for plan in self.plans.values():
            self._recompute_plan_economics(plan, now_ns)
            for order in plan.children:
                if venue is not None and order.venue != venue:
                    continue
                if preserve_risk_reducing and (
                    order.reduce_only or self._order_reduces_residual(plan, order)
                ):
                    continue
                if order.state in LIVE_ORDER_STATES:
                    pending = self.router.cancel_pending_order(
                        order.order_id, now_ns=now_ns, reason=reason
                    )
                    if pending is not None:
                        self.ingest_order_update(CanonicalOrderUpdate(
                            update_id=stable_id(
                                "kill-switch-pending-cancel", order.order_id, now_ns
                            ),
                            source=OrderUpdateSource.INTERNAL,
                            parent_order_id=plan.parent_id,
                            order_id=order.order_id,
                            venue=order.venue,
                            instrument_id=order.instrument_id,
                            state=ExecutionEventType.CANCELLED,
                            event_ts_ns=now_ns,
                            observed_ts_ns=now_ns,
                            cumulative_filled_quantity=order.filled_quantity,
                            average_price=order.average_price,
                            reason=reason,
                        ))
                        order.reject_reason = reason
                        cancelled += 1
                        continue
                    request = CanonicalOrderRequest.create(
                        action=OrderAction.CANCEL,
                        order_id=order.order_id,
                        target_order_id=order.order_id,
                        parent_opportunity_id=order.opportunity_id,
                        parent_order_id=order.parent_id,
                        instrument_id=order.instrument_id,
                        venue=order.venue,
                        side=order.side,
                        price=order.limit_price,
                        quantity=order.quantity,
                        order_type=OrderType.LIMIT,
                        time_in_force=TimeInForce.GTC,
                        created_ts_ns=now_ns,
                    )
                    try:
                        callbacks = self.router.route(request)
                    except Exception as exc:
                        # One venue failing must not stop the sweep: the other orders still have to be cancelled.
                        # This order stays as it was (not cancelled) and is re-queried immediately.
                        self._note_ambiguous_dispatch(plan, order, request.idempotency_key, "CANCEL", exc, now_ns)
                        cancel_errors += 1
                        continue
                    self._record_order_action(
                        plan, order, request, "CANCEL", "DISPATCHED", now_ns,
                        target_order_id=order.order_id,
                    )
                    for callback in callbacks:
                        self._ingest_request_callback(
                            plan, order, request, callback, now_ns
                        )
                    if any(
                        x.event_type == ExecutionEventType.CANCELLED for x in callbacks
                    ) or (not callbacks and self.router.has_pending_order(order.order_id)):
                        order.reject_reason = reason
                        cancelled += 1
        self.ledger.record(
            ledger_event,
            now_ns,
            reason=reason,
            venue=venue or "ALL",
            preserve_risk_reducing=preserve_risk_reducing,
            cancelled_orders=cancelled,
            **({"cancel_errors": cancel_errors} if cancel_errors else {}),
        )
        return cancelled

    def apply_feed_control(
        self, venue: str, valid: bool, reason: str, now_ns: int
    ) -> int:
        """Converge market-data validity into venue execution eligibility."""
        status = self.router.route_status.setdefault(venue, VenueRouteStatus())
        previous = status.venue_enabled
        status.venue_enabled = valid
        mode = "ACTIVE" if valid else "RISK_REDUCING_ONLY"
        self.ledger.record(
            "FEED_CONTROL_STATE_TRANSITION", now_ns,
            venue=venue,
            previous_mode=("ACTIVE" if previous else "RISK_REDUCING_ONLY"),
            mode=mode, reason=reason,
            new_risk_allowed=valid, risk_reducing_allowed=True,
        )
        if valid:
            return 0
        return self._cancel_orders(
            reason, now_ns, venue=venue, preserve_risk_reducing=True,
            ledger_event="FEED_CONTROL_VENUE_CANCEL",
        )

    def route_order(
        self,
        request: CanonicalOrderRequest,
        book: OrderBook | None = None,
    ) -> tuple[ExecutionCallback, ...]:
        """Common EMS entry point used unchanged by backtest and paper adapters."""
        if self.order_owner_guard is not None:
            self.order_owner_guard()
        return self.router.route(request, book)

    def route_strategy_order(
        self,
        instruction: RoutingInstruction,
        books: dict[tuple[str, str], OrderBook],
    ) -> RoutingResult:
        if self.order_owner_guard is not None:
            self.order_owner_guard()
        return self.router.route_controlled(instruction, books)

    def write_execution_trace(self, path: Path) -> dict[str, object]:
        return self.router.write_trace(path)

    def recalculate_account_state(
        self,
        *,
        mark_prices: dict[tuple[str, str], Decimal] | None = None,
        initial_capital_by_venue: dict[str, Decimal] | None = None,
    ) -> dict[str, object]:
        """Derive position, PnL, balances, and capacity from the event ledger."""
        return self.ledger.recalculate_account_state(
            mark_prices=mark_prices,
            initial_capital_by_venue=initial_capital_by_venue,
            reserved_capacity=self.risk.capacity_snapshot(),
        )

    def write_account_state(
        self,
        path: Path,
        *,
        mark_prices: dict[tuple[str, str], Decimal] | None = None,
        initial_capital_by_venue: dict[str, Decimal] | None = None,
    ) -> dict[str, object]:
        state = self.recalculate_account_state(
            mark_prices=mark_prices,
            initial_capital_by_venue=initial_capital_by_venue,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        return state

    def reconcile_risk_state(
        self,
        *,
        now_ns: int,
        venue_orders: dict[str, VenueOrderSnapshot] | None = None,
        venue_risk: tuple[VenueRiskSnapshot, ...] = (),
        require_venue_orders: bool = True,
        require_complete_opportunities: bool = False,
        position_tolerance: Decimal = Decimal("0"),
        balance_tolerance: Decimal = Decimal("0"),
        report_path: Path | None = None,
        venue_balances: tuple = (),
    ) -> ReconciliationReport:
        """Run the RMS release gate against testnet REST/private-stream state."""
        was_active = self.risk.kill_switch
        report = self.risk.reconcile(
            plans=self.plans, ledger=self.ledger, now_ns=now_ns,
            venue_orders=venue_orders, venue_risk=venue_risk,
            require_venue_orders=require_venue_orders,
            require_complete_opportunities=require_complete_opportunities,
            position_tolerance=position_tolerance,
            balance_tolerance=balance_tolerance, venue_balances=venue_balances,
            instruments=self.instruments,
        )

        payload = report.to_dict()
        self.ledger.record(
            "RMS_RECONCILIATION", now_ns,
            reconciliation_id=report.reconciliation_id,
            decision=report.decision.value,
            issue_count=len(report.issues),
            report=payload,
        )
        for plan in self.plans.values():
            self._persist(
                plan, now_ns, "RMS_RECONCILIATION",
                reconciliation_id=report.reconciliation_id,
                decision=report.decision.value,
                issues=list(report.issues),
            )
        if report_path is not None:
            report.write(report_path)
        if self.risk.kill_switch and not was_active:
            cancelled = self.cancel_open_orders(
                "RMS reconciliation rejected", now_ns
            )
            self.ledger.record(
                "KILL_SWITCH_CANCEL_ALL_COMPLETE", now_ns,
                source="RMS_RECONCILIATION", cancelled_orders=cancelled,
                risk_reducing_orders_allowed=(
                    self.residual_policy.allow_risk_reducing_under_stop
                ),
            )
        return report

    FLATTEN_STRATEGY_ID = "risk-flatten"
    FLATTEN_MAX_ATTEMPTS = 20

    def retry_flatten(self, venue: str, now_ns: int) -> int:
        """A new book on ``venue``: retry its open flatten plans now, not on the timer."""
        retried = 0
        for plan in list(self.plans.values()):
            if plan.inherited_base and plan.state != PlanState.CLOSED and plan.opportunity.buy_venue == venue and plan.parent_id not in self._unwind_dust:
                self._finalize_plan(plan, now_ns, self._books)
                retried += 1
        return retried

    def open_flatten_plan(
        self, venue: str, instrument_id: str, exposure: Decimal, now_ns: int, reason: str, *, shadow: bool = False,
    ) -> ParentPlan:
        """Close ``exposure`` (signed base) on ``venue`` with reduce-only unwinds.

        Used after a venue liquidation leaves the other leg of a hedge open. The
        plan reuses the residual-unwind machinery (retries, escalation to
        aggressive, risk-reducing orders allowed under the kill switch) but
        targets flat on the position's own venue. It is not a strategy
        opportunity and holds no risk reservation.
        """
        book = self._books.get((venue, instrument_id))
        mid = (book.best_bid + book.best_ask) / Decimal("2") if book is not None and book.bids and book.asks else Decimal("0")
        sequence = sum(1 for p in self.plans.values() if p.inherited_base) + 1
        opportunity = Opportunity(
            opportunity_id=stable_id("flatten-opportunity", venue, instrument_id, now_ns, sequence),
            strategy_id=self.FLATTEN_STRATEGY_ID, instrument_id=instrument_id, observed_ts_ns=now_ns,
            buy_venue=venue, sell_venue=venue, buy_price=mid, sell_price=mid, quantity=abs(exposure),
            gross_edge_bps=Decimal("0"), estimated_net_edge_bps=Decimal("0"),
            source_event_ids=(reason[:80], str(now_ns)),
        )
        plan = ParentPlan(
            parent_id=stable_id("flatten-parent", opportunity.opportunity_id), opportunity=opportunity,
            children=[], state=PlanState.HEDGE_UNWIND, inherited_base=exposure,
            residual_since_ts_ns=now_ns,
            transitions=[{"ts_ns": now_ns, "state": PlanState.HEDGE_UNWIND.value, "reason": reason}],
        )
        self.plans[plan.parent_id] = plan
        if shadow:
            self._flatten_shadow.add(plan.parent_id)
            self._persist(plan, now_ns, "FLATTEN_SHADOW")
        self._recompute_plan_economics(plan, now_ns)
        self.ledger.record(
            "RISK_FLATTEN_OPENED", now_ns, parent_id=plan.parent_id, venue=venue,
            instrument_id=instrument_id, exposure=exposure, reason=reason,
        )
        self._persist(plan, now_ns, "RISK_FLATTEN_OPENED", venue=venue, exposure=exposure, reason=reason)
        return self._finalize_plan(plan, now_ns, self._books)

    def locked_balances(self) -> dict[tuple[str, str], Decimal]:
        """Funds held by open hedge/unwind orders at the venue (ECO05 "locked").

        A sell locks base, a buy locks quote (remaining x limit price, plus the
        taker fee when it is charged in quote). Primary legs are covered by
        their package reservation instead, so nothing is counted twice.
        """
        out: dict[tuple[str, str], Decimal] = {}
        for (venue, instrument_id), (buys, sells) in self.open_risk_orders().items():
            base, quote = self._assets(venue, instrument_id)
            if sells:
                out[(venue, base)] = out.get((venue, base), Decimal("0")) + sells
            if buys:
                price = max((
                    order.limit_price for plan in self.plans.values() for order in plan.children
                    if order.venue == venue and order.instrument_id == instrument_id and order.side == Side.BUY
                    and order.order_role != "PRIMARY"
                    and order.state in LIVE_ORDER_STATES
                ), default=Decimal("0"))
                fee, fee_asset = self.risk._reserve_fee(venue, instrument_id, 0) if hasattr(self.risk, "_reserve_fee") else (Decimal("0"), "QUOTE")
                amount = buys * price * (Decimal("1") + (fee if fee_asset == "QUOTE" else Decimal("0")))
                out[(venue, quote)] = out.get((venue, quote), Decimal("0")) + amount
        return out

    def _consume_reservation(self, event) -> None:
        """A primary leg filled (partly): its reservation now holds only the remainder."""
        if event.event_type != EconomicEventType.TRADE_FILL:
            return
        plan = self.plans.get(event.reference_id)
        if plan is None:
            return
        order = next((o for o in plan.children if o.order_id == event.order_id), None)
        if order is None or order.order_role != "PRIMARY" or not order.quantity:
            return
        self.risk.consume(plan.opportunity.opportunity_id, order.venue, order.filled_quantity / order.quantity)

    def open_risk_orders(self) -> dict[tuple[str, str], tuple[Decimal, Decimal]]:
        """Remaining (buy, sell) quantity of open hedge/unwind orders per venue and instrument.

        Primary package legs are covered by their risk reservation; hedges and
        unwinds are not, so margin counts them here (ECO04).
        """
        out: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
        for plan in self.plans.values():
            for order in plan.children:
                if order.order_role == "PRIMARY" or order.state not in LIVE_ORDER_STATES:
                    continue
                remaining = order.quantity - order.filled_quantity
                buys, sells = out.get((order.venue, order.instrument_id), (Decimal("0"), Decimal("0")))
                if order.side == Side.BUY:
                    buys += remaining
                else:
                    sells += remaining
                out[(order.venue, order.instrument_id)] = (buys, sells)
        return out

    def evaluate_margin(
        self, *, now_ns: int,
        mark_prices: dict[tuple[str, str], Decimal] | None = None,
        initial_capital_by_venue: dict[str, Decimal] | None = None,
        venue_risk: tuple[VenueRiskSnapshot, ...] = (),
    ):
        """Evaluate margin now; a new breach cancels open orders through the kill switch."""
        was_active = self.risk.kill_switch
        report = self.risk.evaluate_margin(
            ledger=self.ledger, now_ns=now_ns, mark_prices=mark_prices,
            initial_capital_by_venue=initial_capital_by_venue,
            instruments=self.instruments, venue_risk=venue_risk,
            open_orders=self.open_risk_orders(),
        )
        if self.risk.kill_switch and not was_active:
            cancelled = self.cancel_open_orders("RMS margin breach", now_ns)
            self.ledger.record(
                "KILL_SWITCH_CANCEL_ALL_COMPLETE", now_ns,
                source="RMS_MARGIN", cancelled_orders=cancelled,
                risk_reducing_orders_allowed=(
                    self.residual_policy.allow_risk_reducing_under_stop
                ),
            )
        return report

    def write_margin_status(
        self,
        path: Path,
        *,
        now_ns: int,
        mark_prices: dict[tuple[str, str], Decimal] | None = None,
        initial_capital_by_venue: dict[str, Decimal] | None = None,
        venue_risk: tuple[VenueRiskSnapshot, ...] = (),
    ) -> dict[str, object]:
        report = self.evaluate_margin(
            now_ns=now_ns, mark_prices=mark_prices,
            initial_capital_by_venue=initial_capital_by_venue, venue_risk=venue_risk,
        )
        report.write(path)
        return report.to_dict()

    def kill_switch_snapshot(self) -> dict[str, object]:
        return self.risk.kill_switch_snapshot(
            allow_risk_reducing_orders=(
                self.residual_policy.allow_risk_reducing_under_stop
            )
        )

    def activate_kill_switch(
        self, *, reason: str, now_ns: int, operator_id: str,
        source: str = "PM", cancel_all: bool = True,
    ) -> dict[str, object]:
        self.risk.activate_kill_switch(
            now_ns=now_ns, reason=reason, source=source,
            ledger=self.ledger, operator_id=operator_id,
        )
        cancelled = self.cancel_open_orders(reason, now_ns) if cancel_all else 0
        self.ledger.record(
            "KILL_SWITCH_CANCEL_ALL_COMPLETE", now_ns,
            source=source, operator_id=operator_id,
            cancel_all=cancel_all, cancelled_orders=cancelled,
            risk_reducing_orders_allowed=(
                self.residual_policy.allow_risk_reducing_under_stop
            ),
        )
        for plan in self.plans.values():
            if plan.state != PlanState.CLOSED:
                self._finalize_plan(
                    plan, self._next_execution_ts(now_ns), self._books
                )
        return self.kill_switch_snapshot()

    def recover_kill_switch(
        self, *, now_ns: int, operator_id: str,
        venue_orders: dict[str, VenueOrderSnapshot] | None = None,
        venue_risk: tuple[VenueRiskSnapshot, ...] = (),
        require_venue_orders: bool = False,
        report_path: Path | None = None,
    ) -> dict[str, object]:
        report = self.reconcile_risk_state(
            now_ns=now_ns, venue_orders=venue_orders, venue_risk=venue_risk,
            require_venue_orders=require_venue_orders,
            require_complete_opportunities=True,
            report_path=report_path,
        )
        self.risk.reset_kill_switch(
            now_ns=now_ns, reconciliation=report,
            ledger=self.ledger, operator_id=operator_id,
        )
        return self.kill_switch_snapshot()

    def dispatch_stats_snapshot(self) -> dict[str, object]:
        durations = [item.dispatch_duration_ns for item in self.dispatch_measurements]
        return {
            "policy": self.dispatch_policy.value,
            "children_dispatched": len(self.dispatch_measurements),
            "callbacks_observed": sum(
                item.callback_count for item in self.dispatch_measurements
            ),
            "min_dispatch_duration_ns": min(durations) if durations else 0,
            "max_dispatch_duration_ns": max(durations) if durations else 0,
            "measurements": [item.to_dict() for item in self.dispatch_measurements],
        }

    def state_machine_stats_snapshot(self) -> dict[str, object]:
        updates = [
            update
            for plan in self.plans.values()
            for order in plan.children
            for update in order.state_updates
        ]
        outcomes: dict[str, int] = {}
        for plan in self.plans.values():
            key = plan.terminal_outcome or "OPEN"
            outcomes[key] = outcomes.get(key, 0) + 1
        return {
            "schema_version": "1.0.0",
            "terminal_parent_state": PlanState.CLOSED.value,
            "plans": len(self.plans),
            "closed_plans": sum(
                plan.state == PlanState.CLOSED for plan in self.plans.values()
            ),
            "order_updates": len(updates),
            "updates_by_source": {
                source.value: sum(
                    str(update["source"]) == source.value for update in updates
                )
                for source in OrderUpdateSource
            },
            "terminal_outcomes": dict(sorted(outcomes.items())),
            "terminal_conflicts_resolved": sum(
                event["event_type"] == "ORDER_TERMINAL_CONFLICT_RESOLVED"
                for event in self.ledger.events
            ),
            "duplicates_suppressed": sum(
                event["event_type"] == "ORDER_UPDATE_DUPLICATE_SUPPRESSED"
                for event in self.ledger.events
            ),
        }

    def residual_risk_snapshot(self) -> dict[str, object]:
        policy = self.residual_policy
        open_residuals = [
            plan for plan in self.plans.values()
            if plan.state != PlanState.CLOSED and plan.residual_base != 0 and plan.parent_id not in self._quote_plans
        ]
        return {
            "schema_version": "1.0.0",
            "policy": {
                "max_residual_base": str(policy.max_residual_base),
                "max_residual_age_ns": policy.max_residual_age_ns,
                "hedge_venue_priority": list(policy.hedge_venue_priority),
                "unwind_urgency": policy.unwind_urgency.value,
                "unwind_price_slippage_bps": str(
                    policy.unwind_price_slippage_bps
                ),
                "allow_risk_reducing_under_stop": (
                    policy.allow_risk_reducing_under_stop
                ),
                "max_unwind_attempts": policy.max_unwind_attempts,
                "max_one_sided_notional": str(policy.max_one_sided_notional),
            },
            "open_residual_plans": len(open_residuals),
            "lot_residual_alarm": self.lot_residual_alarm,
            "carried_residual_base": str(self.carried_residual_base()),
            # Net unhedged base across open and closed plans: what the account
            # actually holds because legs did not pair up.
            "total_residual_base": str(abs(
                self.carried_residual_base()
                + sum((self._counted_residual(plan) for plan in open_residuals), Decimal("0"))
                + (self.dust_balance_total() if self.dust_on else Decimal("0"))          # carried hedge dust: in the balance once, never in a plan
            )),
            "open_residual_base": str(sum(
                (abs(self._counted_residual(plan)) for plan in open_residuals), Decimal("0")
            )),
            "unwind_children": sum(
                order.order_role == "UNWIND"
                for plan in self.plans.values() for order in plan.children
            ),
            "limit_breaches": sum(
                event["event_type"] == "RESIDUAL_LIMIT_BREACH"
                for event in self.ledger.events
            ),
            "legging_limit_bindings": sum(
                event["event_type"] == "LEGGING_LIMIT_BOUND"
                for event in self.ledger.events
            ),
            "legging_control_overrides": sum(
                event["event_type"] == "LEGGING_CONTROL_OVERRIDE"
                for event in self.ledger.events
            ),
            **({"carried_dust_base": str(self.dust_balance_total())} if self.dust_on else {}),
        }

    def _recompute_plan_economics(
        self, plan: ParentPlan, ts_ns: int | None = None
    ) -> None:
        raw_residual = plan.inherited_base + sum(
            order.filled_quantity if order.side == Side.BUY else -order.filled_quantity
            for order in plan.children
        )
        dust_adj = self._dust_plan_adj.get(plan.parent_id)
        plan.residual_base = raw_residual if not dust_adj else raw_residual + dust_adj      # dust carry: what the limits read; the MARK below stays on the raw residual so plan P&L equals the equity P&L
        plan.gross_pnl = sum(
            -order.filled_quantity * order.average_price
            if order.side == Side.BUY
            else order.filled_quantity * order.average_price
            for order in plan.children
        )
        plan.fees = sum(order.fee for order in plan.children)
        if raw_residual:
            plan.residual_mark_price = self._residual_mark_price(plan)
            plan.residual_mark_value = raw_residual * plan.residual_mark_price
        else:
            plan.residual_mark_price = Decimal("0")
            plan.residual_mark_value = Decimal("0")
        inherited_value = (
            plan.inherited_base * self._residual_mark_price(plan) if plan.inherited_base else Decimal("0")
        )
        plan.net_pnl = (
            plan.gross_pnl + plan.residual_mark_value - inherited_value - plan.fees - plan.unwind_cost
        )

        if plan.residual_base == 0:
            plan.residual_since_ts_ns = None
        elif plan.residual_since_ts_ns is None and ts_ns is not None:
            plan.residual_since_ts_ns = ts_ns

    def _residual_mark_price(self, plan: ParentPlan) -> Decimal:
        """Mid across the instrument's valid books (falls back to the signal prices)."""
        mids = [
            (book.best_bid + book.best_ask) / Decimal("2")
            for (venue, instrument_id), book in (self._books or {}).items()
            if instrument_id == plan.opportunity.instrument_id and book.bids and book.asks
        ]
        if mids:
            return sum(mids, start=Decimal("0")) / len(mids)
        return (plan.opportunity.buy_price + plan.opportunity.sell_price) / Decimal("2")

    def _counted_residual(self, plan: ParentPlan) -> Decimal:
        """What a plan adds to the residual totals. A breaker flatten plan (`_flatten_shadow`) inherited an exposure that other plans already carry (or that the ledger holds as dust): counting its inherited base
        again double-counted it (0.0076 SOL read as 0.0152 in the 2c run), so only its own fills count. A liquidation flatten inherits exposure no plan holds and counts in full."""
        return plan.residual_base - plan.inherited_base if plan.parent_id in self._flatten_shadow else plan.residual_base

    def open_plan_position_by_venue(self) -> dict[str, Decimal]:
        """Signed base per venue that the OPEN plans (not closed, not a breaker flatten) still manage: their own fills, plus the exposure a liquidation flatten inherited. The breaker's flatten leaves it to them
        (a hedge chain that is retrying or unwinding closes it once): flattening it too exited the same exposure twice in the 2c rehearsal."""
        out: dict[str, Decimal] = {}
        for plan in self.plans.values():
            if plan.state == PlanState.CLOSED or plan.parent_id in self._flatten_shadow:
                continue
            for order in plan.children:
                if order.filled_quantity:
                    out[order.venue] = out.get(order.venue, Decimal("0")) + (order.filled_quantity if order.side == Side.BUY else -order.filled_quantity)
            if plan.inherited_base:
                venue = plan.opportunity.buy_venue
                out[venue] = out.get(venue, Decimal("0")) + plan.inherited_base
        return out

    def escalate_open_chains(self, now_ns: int, reason: str) -> list[str]:
        """On a breaker trip: every hedge chain that has no order in flight (not sent, rejected, waiting to retry, partly filled) is escalated NOW, so its own deterministic unwind closes the exposure once instead of
        waiting out the retries. A chain with an order in flight or unknown stays as it is (the order may still fill). Returns the chain ids escalated."""
        moved: list[str] = []
        for parent_id, chains in list(self.hedge_chains.items()):
            plan = self.plans.get(parent_id)
            if plan is None or plan.state == PlanState.CLOSED:
                continue
            for chain in list(chains.values()):
                if chain.state in (HedgeState.MAKER_FILLED, HedgeState.HEDGE_PENDING, HedgeState.REJECTED, HedgeState.RETRYING, HedgeState.PARTIALLY_FILLED) and chain.exposure_closed_ts is None:
                    self._hedge_retry_due.pop(chain.chain_id, None)
                    self._hedge_held.pop(chain.chain_id, None)
                    self._hedge_move(plan, chain, HedgeState.ESCALATED, now_ns, reason)
                    moved.append(chain.chain_id)
        if moved:
            self.ledger.record("HEDGE_CHAINS_ESCALATED", now_ns, chain_ids=moved, reason=reason)
            self._hedge_progress_all(now_ns)
        return moved

    def carried_residual_base(self, exclude: ParentPlan | None = None) -> Decimal:
        """Signed residual left open by closed plans (derived, so restart-safe)."""
        return sum(
            (
                self._counted_residual(plan) for plan in self.plans.values()
                if plan.state == PlanState.CLOSED and plan is not exclude and plan.parent_id not in self._quote_plans      # a quote's fills are inventory (the pair cap), not an arb residual
            ),
            Decimal("0"),
        )

    def _residual_event(
        self, plan: ParentPlan, event_type: str, ts_ns: int, **payload: object
    ) -> None:
        common = {
            "parent_id": plan.parent_id,
            "opportunity_id": plan.opportunity.opportunity_id,
            "residual_base": plan.residual_base,
            **payload,
        }
        self.ledger.record(event_type, ts_ns, **common)
        self._persist(plan, ts_ns, event_type, **common)

    def _one_sided_notional(
        self, plan: ParentPlan,
        books: dict[tuple[str, str], OrderBook] | None = None,
    ) -> Decimal:
        if plan.residual_base == 0:
            return Decimal("0")
        source = books if books is not None else self._books
        prices = [
            (book.best_bid + book.best_ask) / Decimal("2")
            for (venue, instrument_id), book in source.items()
            if instrument_id == plan.opportunity.instrument_id
            and book.bids and book.asks
        ]
        fallback = max(plan.opportunity.buy_price, plan.opportunity.sell_price)
        return abs(plan.residual_base) * (max(prices) if prices else fallback)

    @staticmethod
    def _order_reduces_residual(plan: ParentPlan, order: ChildOrder) -> bool:
        if plan.residual_base == 0:
            return False
        signed_quantity = (
            order.quantity if order.side == Side.BUY else -order.quantity
        )
        return abs(plan.residual_base + signed_quantity) < abs(plan.residual_base)

    def _residual_within_bound(self, plan: ParentPlan) -> bool:
        """Exactly flat is always within bound, whatever the configured cap
        (including a cap of exactly zero, meaning "no residual tolerated").
        A nonzero residual breaches once it reaches the cap: the cap is a
        hard ceiling, not a threshold a plan may rest on indefinitely."""
        if plan.residual_base == 0:
            return True
        if plan.inherited_base:
            return False        # a flatten plan's target is flat, not "within bound"
        cap = self.residual_policy.max_residual_base
        if abs(plan.residual_base) >= cap:
            return False
        # The bound applies to the total left open, not to each plan alone:
        # residuals "within bound" one at a time must not pile up past it. A
        # residual that reduces the carried exposure is always acceptable.
        carried = self.carried_residual_base(exclude=plan)
        total = carried + plan.residual_base
        return abs(total) < cap or abs(total) <= abs(carried)

    def _assess_legging_risk(
        self, plan: ParentPlan, ts_ns: int,
        books: dict[tuple[str, str], OrderBook] | None = None,
    ) -> tuple[str, ...]:
        self._recompute_plan_economics(plan, ts_ns)
        policy = self.residual_policy
        notional = self._one_sided_notional(plan, books)
        elapsed_ns = (
            max(0, ts_ns - plan.residual_since_ts_ns)
            if plan.residual_since_ts_ns is not None else 0
        )
        codes: list[str] = []
        if not self._residual_within_bound(plan):
            codes.append("MAX_RESIDUAL_BASE")
        if notional > policy.max_one_sided_notional:
            codes.append("MAX_ONE_SIDED_NOTIONAL")
        if plan.residual_base != 0 and elapsed_ns >= policy.max_residual_age_ns:
            codes.append("MAX_RESIDUAL_DURATION")
        binding = tuple(codes)
        previous = self._legging_bound_codes.get(plan.parent_id, ())
        if binding and binding != previous:
            remaining_hedges = [
                order.order_id for order in plan.children
                if order.order_role == "PRIMARY"
                and order.state in UNFILLED_LIVE_ORDER_STATES
                and self._order_reduces_residual(plan, order)
            ]
            action = (
                "DISPATCH_REMAINING_HEDGE" if remaining_hedges
                else "HEDGE_UNWIND"
            )
            self._residual_event(
                plan, "LEGGING_LIMIT_BOUND", ts_ns,
                binding_codes=list(binding),
                one_sided_notional=notional,
                elapsed_ns=elapsed_ns,
                action=action,
                hedge_order_ids=remaining_hedges,
            )
            self._legging_bound_codes[plan.parent_id] = binding
        elif not binding and previous:
            self._residual_event(
                plan, "LEGGING_LIMIT_CLEARED", ts_ns,
                previous_binding_codes=list(previous),
                one_sided_notional=notional,
                elapsed_ns=elapsed_ns,
            )
            self._legging_bound_codes.pop(plan.parent_id, None)
        return binding

    def _record_legging_control_override(
        self, plan: ParentPlan, order: ChildOrder, routing: RoutingResult,
        ts_ns: int, *, global_stop_override: bool = False,
    ) -> None:
        controls = [
            f"{check.venue}:{control}"
            for check in routing.checks for control in check.control_overrides
        ]
        if global_stop_override:
            controls.append("GLOBAL:kill_switch")
        if controls:
            self._residual_event(
                plan, "LEGGING_CONTROL_OVERRIDE", ts_ns,
                order_id=order.order_id, venue=order.venue,
                order_role=order.order_role, risk_reducing=True,
                controls=controls,
                action="ALLOW_RISK_REDUCING_HEDGE",
            )

    def _unwind_price(
        self, venue: str, instrument_id: str, side: Side,
        book: OrderBook, urgency: UnwindUrgency, ts_ns: int,
    ) -> tuple[Decimal | None, OrderType, TimeInForce, bool]:
        if urgency == UnwindUrgency.AGGRESSIVE:
            return None, OrderType.MARKET, TimeInForce.IOC, False
        if urgency == UnwindUrgency.PASSIVE:
            price = book.best_bid if side == Side.BUY else book.best_ask
            return price, OrderType.LIMIT, TimeInForce.GTC, True
        # Round against the instrument version effective at the same
        # timestamp the resulting order will carry as created_ts_ns, so the
        # rounding here and the router's validation agree even when an
        # instrument-status change lands between plan creation and unwind.
        spec = self.instruments.get(venue, instrument_id, ts_ns)
        multiplier = self.residual_policy.unwind_price_slippage_bps / Decimal("10000")
        # Owner 12 Nov: a rounding to the grid never loosens the cap (a buy limit rounds down, a sell limit rounds up), the same rule as the hedge cap (hedge_cap_on_grid).
        raw_price = book.best_ask * (Decimal("1") + multiplier) if side == Side.BUY else book.best_bid * (Decimal("1") - multiplier)
        price = limit_on_grid(side.value, raw_price, spec.tick_size)
        return price, OrderType.LIMIT, TimeInForce.IOC, False

    def _unwind_venues(self, plan: ParentPlan) -> tuple[str, ...]:
        if plan.inherited_base:
            # Close the position where it is: a hedge elsewhere would open a new one.
            return (plan.opportunity.buy_venue,)
        configured = self.residual_policy.hedge_venue_priority
        if configured:
            return configured
        return tuple(dict.fromkeys((
            plan.opportunity.buy_venue, plan.opportunity.sell_venue
        )))

    @staticmethod
    def _open_unwind_orders(plan: ParentPlan) -> list[ChildOrder]:
        return [
            order for order in plan.children
            if order.order_role == "UNWIND"
            and order.state in LIVE_ORDER_STATES
        ]

    def _cancel_expired_unwinds(self, plan: ParentPlan, ts_ns: int) -> None:
        for order in self._open_unwind_orders(plan):
            request = CanonicalOrderRequest.create(
                action=OrderAction.CANCEL,
                order_id=order.order_id,
                target_order_id=order.order_id,
                parent_opportunity_id=order.opportunity_id,
                parent_order_id=order.parent_id,
                instrument_id=order.instrument_id,
                venue=order.venue,
                side=order.side,
                price=order.limit_price,
                quantity=order.quantity,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                reduce_only=True,
                created_ts_ns=ts_ns,
                idempotency_key=stable_id("expired-unwind-cancel", order.order_id),
            )
            self._residual_event(
                plan, "UNWIND_DEADLINE_CANCEL_INTENT", ts_ns,
                order_id=order.order_id, venue=order.venue,
            )
            callbacks = self.route_order(request)
            self._record_order_action(
                plan, order, request, "CANCEL", "DISPATCHED", ts_ns,
                target_order_id=order.order_id,
            )
            for callback in callbacks:
                callback_ts = self._next_execution_ts(ts_ns)
                self._ingest_request_callback(plan, order, request, callback, callback_ts)
                self._residual_event(
                    plan, "UNWIND_DEADLINE_CANCEL_CALLBACK", callback_ts,
                    order_id=order.order_id, venue=order.venue,
                    callback_type=callback.event_type.value,
                )

    def _unwind_attempts_allowed(self, plan: ParentPlan) -> int:
        # A flatten plan keeps trying (bounded) until flat: it is closing exposure
        # a venue liquidation left behind, not a strategy residual.
        policy = self.residual_policy
        return (
            max(policy.max_unwind_attempts, self.FLATTEN_MAX_ATTEMPTS) if plan.inherited_base
            else policy.max_unwind_attempts
        )

    def _raise_unwind_exhausted_alarm(self, plan: ParentPlan, ts_ns: int) -> None:
        """Every unwind attempt is spent and the residual is still out of bound: a persistent alarm, never a success.

        Raised once per plan, only when no unwind order is still working (a live or pending one may yet reduce the residual).
        New risk is refused while any alarm is active; only reset_unwind_exhausted_alarm() clears it."""
        if plan.parent_id in self.unwind_exhausted_alarms:
            return
        allowed = self._unwind_attempts_allowed(plan)
        if plan.unwind_attempts < allowed or self._open_unwind_orders(plan) or self.router.has_pending_parent(plan.parent_id):
            return
        payload: dict[str, object] = {
            "residual_base": str(plan.residual_base), "unwind_attempts": plan.unwind_attempts,
            "attempts_allowed": allowed, "max_residual_base": str(self.residual_policy.max_residual_base),
            **{**self.alert_context(ts_ns), "execution_blocked_reason": self.alert_context(ts_ns)["execution_blocked_reason"] or "unwind_exhausted_alarm"},      # this alarm is itself what blocks new risk from here on
        }
        self.unwind_exhausted_alarms[plan.parent_id] = dict(payload, ts_ns=ts_ns)
        self.realism_counters["unwind_exhausted_alarms"] = self.realism_counters.get("unwind_exhausted_alarms", 0) + 1
        self.ledger.record("UNWIND_EXHAUSTED_ALARM", ts_ns, parent_id=plan.parent_id, **payload)
        self._persist(plan, ts_ns, "UNWIND_EXHAUSTED_ALARM", **payload)

    def alert_context(self, now_ns: int) -> dict[str, object]:
        """What every alert carries (owner 12 Nov): the confirmed residual, the hedge quantity still pending, the age of the oldest exposure, and why execution is blocked. Read-only.
        confirmed_residual_base  the net unhedged base the OMS holds (open plans plus carried residual of closed ones)
        pending_hedge_qty        hedge quantity of the open chains not yet hedged or unwound
        oldest_exposure_age_s    seconds since the oldest still-open hedge chain opened or the oldest open residual formed (None when nothing is open)
        execution_blocked_reason why new risk is refused now: kill switch (with its reason), an unwind-exhausted alarm, the one-lot residual alarm, or '' when nothing blocks"""
        snap = self.residual_risk_snapshot()
        pending, starts = Decimal("0"), []
        for chains in self.hedge_chains.values():
            for c in chains.values():
                if c.state in self._CHAIN_OPEN and c.exposure_closed_ts is None:
                    pending += Decimal(str(max(0.0, c.exposure_qty - c.hedged_qty - c.unwound_qty)))
                    starts.append(c.opened_ts)
        starts += [plan.residual_since_ts_ns / 1e9 for plan in self.plans.values() if plan.residual_since_ts_ns is not None and plan.state != PlanState.CLOSED]
        if self.risk.kill_switch:
            blocked = f"kill_switch:{self.risk.kill_switch_reason}"
        elif self.unwind_exhausted_alarms:
            blocked = "unwind_exhausted_alarm"
        elif self.lot_residual_alarm is not None:
            blocked = "lot_residual_alarm"
        else:
            blocked = ""
        return {"confirmed_residual_base": snap["total_residual_base"], "pending_hedge_qty": str(pending),
                "oldest_exposure_age_s": (now_ns / 1e9 - min(starts)) if starts else None, "execution_blocked_reason": blocked}

    def reset_unwind_exhausted_alarm(self, ts_ns: int, operator: str, parent_id: str | None = None) -> list[str]:
        """Operator action: clear the unwind-exhausted alarm of one plan (or all). The residual itself is not touched."""
        if not operator:
            raise ValueError("an operator must be named to reset an unwind-exhausted alarm")
        cleared = [parent_id] if parent_id is not None else sorted(self.unwind_exhausted_alarms)
        for pid in cleared:
            if self.unwind_exhausted_alarms.pop(pid, None) is None:
                raise KeyError(pid)
            self.ledger.record("UNWIND_EXHAUSTED_ALARM_RESET", ts_ns, parent_id=pid, operator=operator)
            if pid in self.plans:
                self._persist(self.plans[pid], ts_ns, "UNWIND_EXHAUSTED_ALARM_RESET", operator=operator)
        return cleared

    def _execute_residual_unwind(
        self, plan: ParentPlan, ts_ns: int,
        books: dict[tuple[str, str], OrderBook],
    ) -> bool:
        """Dispatch reduce-only children until residual is bounded or work is pending."""
        policy = self.residual_policy
        if self._residual_within_bound(plan):
            return True
        if plan.parent_id in self._unwind_dust:
            return True                                                    # DUST (owner 13 Nov): recorded once, accepted as carried, never retried
        if self.risk.kill_switch and not policy.allow_risk_reducing_under_stop:
            self._residual_event(
                plan, "UNWIND_BLOCKED_BY_STRATEGY_STOP", ts_ns,
                max_residual_base=policy.max_residual_base,
            )
            return False
        if self.risk.kill_switch:
            self._residual_event(
                plan, "RISK_REDUCING_STOP_OVERRIDE", ts_ns,
                policy_allows=True,
            )

        elapsed_ns = (
            max(0, ts_ns - plan.residual_since_ts_ns)
            if plan.residual_since_ts_ns is not None else 0
        )
        urgency = policy.unwind_urgency
        if elapsed_ns >= policy.max_residual_age_ns:
            urgency = UnwindUrgency.AGGRESSIVE
        venues = self._unwind_venues(plan)
        attempts_allowed = self._unwind_attempts_allowed(plan)
        dust_seen: list[tuple[str, Decimal]] = []
        for venue in venues:
            if plan.unwind_attempts >= attempts_allowed:
                break
            book = books.get((venue, plan.opportunity.instrument_id))
            skip_key = (plan.parent_id, venue)
            if book is None or not book.is_tradable:
                # owner 13 Nov: a venue that is unavailable is retried with a backoff (1 s, doubling to 30 s), not on every event, and logged once per STATE CHANGE (plus a counter), not once per event
                reason = "book_unavailable" if book is None else "book_not_tradable"
                state = self._unwind_skip.get(skip_key)
                if state is not None and ts_ns < state["next_ns"]:
                    self.realism_counters["unwind_venue_skip_retries_suppressed"] = self.realism_counters.get("unwind_venue_skip_retries_suppressed", 0) + 1
                    continue
                delay = 1_000_000_000 if state is None else min(30_000_000_000, state["delay_ns"] * 2)
                if state is None or state["reason"] != reason:
                    self.realism_counters["unwind_venue_skips"] = self.realism_counters.get("unwind_venue_skips", 0) + 1
                    self._residual_event(plan, "UNWIND_VENUE_SKIPPED", ts_ns, venue=venue, reason=reason, retry_after_ns=delay)
                self._unwind_skip[skip_key] = {"reason": reason, "delay_ns": delay, "next_ns": ts_ns + delay}
                continue
            if skip_key in self._unwind_skip:                                # the venue is back: one record of the state change
                del self._unwind_skip[skip_key]
                self._residual_event(plan, "UNWIND_VENUE_AVAILABLE", ts_ns, venue=venue)
            if plan.inherited_base:
                # Retrying a flatten against the same (already consumed) book
                # only burns attempts: wait for that venue's next update.
                marker = (book.sequence, book.snapshot_hash())
                if self._flatten_book_marker.get(plan.parent_id) == marker:
                    continue
                self._flatten_book_marker[plan.parent_id] = marker
            quantity = self.instruments.round_quantity(
                venue, plan.opportunity.instrument_id, abs(plan.residual_base), ts_ns
            )
            spec = self.instruments.get(venue, plan.opportunity.instrument_id, ts_ns)
            reference_price = book.best_ask if plan.residual_base < 0 else book.best_bid
            if quantity < spec.min_quantity or quantity * reference_price < spec.min_notional:
                if plan.inherited_base:
                    dust_seen.append((venue, quantity))                      # a flatten (a breaker trip, a liquidation): below this venue's minimum; if no venue can take it, it is DUST (below)
                else:
                    state = self._unwind_skip.get(skip_key)                  # an arb residual above the bound but below the minimum: still an alarm case (the 5 s hold and the kill switch), but logged ONCE per state change, not on every retry
                    if state is None or state["reason"] != "residual_below_venue_minimum":
                        self._unwind_skip[skip_key] = {"reason": "residual_below_venue_minimum", "delay_ns": 0, "next_ns": 0}
                        self.realism_counters["unwind_venue_skips"] = self.realism_counters.get("unwind_venue_skips", 0) + 1
                        self._residual_event(plan, "UNWIND_VENUE_SKIPPED", ts_ns, venue=venue, reason="residual_below_venue_minimum", quantity=quantity)
                continue
            side = Side.SELL if plan.residual_base > 0 else Side.BUY
            price, order_type, tif, post_only = self._unwind_price(
                venue, plan.opportunity.instrument_id, side, book, urgency, ts_ns,
            )
            plan.unwind_attempts += 1
            order_id = stable_id(
                "unwind", plan.parent_id, plan.unwind_attempts, venue, side.value
            )
            order = ChildOrder(
                order_id=order_id,
                parent_id=plan.parent_id,
                opportunity_id=plan.opportunity.opportunity_id,
                venue=venue,
                instrument_id=plan.opportunity.instrument_id,
                side=side,
                quantity=quantity,
                limit_price=price or reference_price,
                order_role="UNWIND",
                reduce_only=True,
                unwind_attempt=plan.unwind_attempts,
                order_type=order_type.value,
                time_in_force=tif.value,
                post_only=post_only,
            )
            plan.children.append(order)
            if self.observer is not None:
                self.observer.unwind_child(plan, order, ts_ns / 1e9)
            attempt_ts = self._next_execution_ts(ts_ns)
            self._residual_event(
                plan, "UNWIND_INTENT", attempt_ts,
                order_id=order_id, venue=venue, side=side.value,
                quantity=quantity, order_type=order_type.value,
                time_in_force=tif.value, price=price,
                urgency=urgency.value, reduce_only=True,
                attempt=plan.unwind_attempts, elapsed_ns=elapsed_ns,
            )
            try:
                routing = self.route_strategy_order(RoutingInstruction(
                    strategy_id=plan.opportunity.strategy_id,
                    eligible_venues=(venue,), route_priority=(venue,),
                    instrument_id=order.instrument_id, side=side,
                    order_type=order_type, price=price, quantity=quantity,
                    maker_taker_intent=(
                        MakerTakerIntent.MAKER if post_only else MakerTakerIntent.TAKER
                    ),
                    time_in_force=tif, post_only=post_only, reduce_only=True,
                    parent_opportunity_id=order.opportunity_id,
                    parent_order_id=plan.parent_id, order_id=order_id,
                    created_ts_ns=attempt_ts,
                    idempotency_key=stable_id("unwind-submit", order_id),
                    risk_reducing=True,
                ), books)
            except ValueError as exc:
                # The unwind price/quantity failed the venue's order-API
                # contract (for example, an instrument rule changed between
                # when this residual formed and when the unwind was built).
                # Unlike the primary dispatch this order was already appended
                # to the plan; mark it terminal here rather than letting an
                # unhandled exception abort the whole plan/session.
                order.state = OrderState.REJECTED
                order.reject_reason = str(exc)
                self._classify_reject(plan, order, attempt_ts)
                self._residual_event(
                    plan, "UNWIND_ORDER_REJECTED_INVALID", attempt_ts,
                    order_id=order_id, venue=venue, reason=str(exc),
                )
                continue
            self._record_legging_control_override(
                plan, order, routing, attempt_ts,
                global_stop_override=self.risk.kill_switch,
            )
            if routing.request is not None:
                self._record_order_action(
                    plan, order, routing.request, "SUBMIT",
                    (
                        routing.throttle_action.value
                        if routing.throttle_action in {
                            ThrottleAction.QUEUE, ThrottleAction.DELAY
                        } else "DISPATCHED"
                    ),
                    attempt_ts,
                )
            if routing.throttle_action in DEFERRED_ACTIONS:
                self._residual_event(
                    plan,
                    (
                        "UNWIND_IN_FLIGHT"
                        if routing.throttle_action == ThrottleAction.IN_FLIGHT
                        else "UNWIND_THROTTLED"
                    ),
                    attempt_ts,
                    order_id=order_id, venue=venue,
                    action=routing.throttle_action.value,
                    release_ts_ns=routing.release_ts_ns,
                )
                return False
            callback_ts = attempt_ts
            for callback in routing.callbacks:
                callback_ts = self._next_execution_ts(ts_ns)
                self._residual_event(
                    plan, "UNWIND_CALLBACK_OBSERVED", callback_ts,
                    order_id=order_id, venue=venue,
                    callback_id=callback.callback_id,
                    callback_type=callback.event_type.value,
                )
                self.ingest_execution_callback(callback, callback_ts)
            if routing.selected_venue is not None and routing.selected_venue != venue:      # E0 step (d) condition 2 for an unwind: its venue is the one that holds the position
                self.ledger.record("ROUTE_VENUE_MISMATCH", attempt_ts, parent_id=plan.parent_id, order_id=order_id, decided_venue=venue, routed_venue=routing.selected_venue)
                raise RuntimeError(f"the router selected {routing.selected_venue} but the unwind was decided for {venue}")
            if routing.selected_venue is None:
                reason = ";".join(
                    reason for check in routing.checks for reason in check.reasons
                ) or "no eligible unwind route"
                self.ingest_order_update(CanonicalOrderUpdate(
                    update_id=stable_id("unwind-route-rejection", order_id),
                    source=OrderUpdateSource.INTERNAL,
                    parent_order_id=plan.parent_id, order_id=order_id,
                    venue=venue, instrument_id=order.instrument_id,
                    state=ExecutionEventType.REJECTED,
                    event_ts_ns=callback_ts, observed_ts_ns=callback_ts,
                    reason=reason,
                ))
            if order.filled_quantity:
                self._price_fills(order, callback_ts)
                self.ledger.apply_fill(order, callback_ts)
                self._update_exposure_control(callback_ts)
            self._recompute_plan_economics(plan, callback_ts)
            self._assess_legging_risk(plan, callback_ts, books)
            self._residual_event(
                plan, "UNWIND_ATTEMPT_COMPLETED", callback_ts,
                order_id=order_id, venue=venue, state=order.state.value,
                filled_quantity=order.filled_quantity,
                attempt=plan.unwind_attempts,
            )
            if self._residual_within_bound(plan):
                return True
            if order.state in LIVE_ORDER_STATES:
                return False
        if plan.inherited_base and dust_seen and len(dust_seen) == len(venues):
            if plan.parent_id not in self._unwind_dust:                      # every venue refuses the quantity as below its minimum: DUST, recorded once and never retried
                self._unwind_dust[plan.parent_id] = {"residual_base": plan.residual_base, "venues": [v for v, _ in dust_seen], "ts_ns": ts_ns}
                self.realism_counters["unwind_dust_plans"] = self.realism_counters.get("unwind_dust_plans", 0) + 1
                self._residual_event(plan, "UNWIND_DUST", ts_ns, venues=[v for v, _ in dust_seen], tradeable_quantity=[str(q) for _, q in dust_seen],
                                     note="residual below the venue minimum quantity or notional: counted in the exposure, not retried")
            return True
        return self._residual_within_bound(plan)

    def _release_and_close(self, plan: ParentPlan, ts_ns: int) -> ParentPlan:
        internal_report = self.risk.reconcile(
            plans=self.plans, ledger=self.ledger, now_ns=ts_ns,
            require_venue_orders=False,
        )
        self.ledger.record(
            "RMS_INTERNAL_RECONCILIATION", ts_ns,
            parent_id=plan.parent_id,
            opportunity_id=plan.opportunity.opportunity_id,
            reconciliation_id=internal_report.reconciliation_id,
            decision=internal_report.decision.value,
            issues=list(internal_report.issues),
        )
        self._persist(
            plan, ts_ns, "RMS_INTERNAL_RECONCILIATION",
            reconciliation_id=internal_report.reconciliation_id,
            decision=internal_report.decision.value,
            issues=list(internal_report.issues),
        )
        if internal_report.issues:
            self._reconciliation_retry.add(plan.parent_id)
            return plan
        self._reconciliation_retry.discard(plan.parent_id)
        self._transition(plan, PlanState.RECONCILED, ts_ns)
        self._transition(plan, PlanState.CLOSED, ts_ns)
        if self.observer is not None:
            self.observer.plan_closed(plan, ts_ns / 1e9)
        self.risk.carried_residual_base = self.carried_residual_base()
        reservation = self.risk.reservations.get(plan.opportunity.opportunity_id)
        if reservation is not None and reservation.active:
            self.risk.release(reservation)
            self._persist(
                plan, ts_ns, "PACKAGE_RISK_RELEASED",
                reservation_id=reservation.reservation_id,
            )
            self.ledger.record(
                "PACKAGE_RISK_RELEASED", ts_ns, parent_id=plan.parent_id,
                reservation_id=reservation.reservation_id,
            )
        self.ledger.record_plan(plan, ts_ns)
        return plan

    def _finalize_plan(
        self, plan: ParentPlan, ts_ns: int,
        books: dict[tuple[str, str], OrderBook] | None = None,
    ) -> ParentPlan:
        if plan.parent_id not in self._hedge_specs:
            return self._finalize_plan_body(plan, ts_ns, books)
        # FILL_TRIGGERED: updates the unwind produces re-enter the hedge reaction; it must not finalize the plan from inside its own finalization
        self._hedge_finalizing += 1
        try:
            return self._finalize_plan_body(plan, ts_ns, books)
        finally:
            self._hedge_finalizing -= 1

    def _finalize_plan_body(
        self, plan: ParentPlan, ts_ns: int,
        books: dict[tuple[str, str], OrderBook] | None = None,
    ) -> ParentPlan:
        if plan.state == PlanState.CLOSED:
            return plan
        if plan.parent_id in self._hedge_specs and not self._fill_triggered_settled(plan):
            return plan                      # FILL_TRIGGERED: the maker still rests or a hedge chain is open; the plan closes when they settle
        if self.router.has_pending_parent(plan.parent_id):
            return plan
        unknown = self._unknown_state_children(plan, ts_ns)
        if unknown:
            # Never hedge or close on a leg whose venue state is unknown: the
            # venue is queried at the report deadline and the plan waits.
            self._defer_close(plan, ts_ns, "ORDER_STATE_UNKNOWN", unknown)
            return plan
        unconfirmed = tuple(
            order.order_id for order in plan.children
            if self._timed_out_unconfirmed(order)
        )
        if unconfirmed:
            self._defer_close(plan, ts_ns, "ORDER_TIMED_OUT_UNCONFIRMED", unconfirmed)
            return plan
        if plan.parent_id in self._quote_plans:
            return self._finalize_quote(plan, ts_ns)
        self._recompute_plan_economics(plan, ts_ns)
        self._assess_legging_risk(plan, ts_ns, books)
        primary = [order for order in plan.children if order.order_role == "PRIMARY" and not self._amend_bookkeeping_order(plan, order)] or [order for order in plan.children if order.order_role == "PRIMARY"]
        all_filled = all(
            order.state == OrderState.FILLED for order in primary
        )
        if plan.parent_id in self._hedge_specs and plan.residual_base != 0:
            all_filled = False               # FILL_TRIGGERED: a filled maker whose hedge did not cover it is a residual, not a filled plan
        if all_filled:
            if plan.state != PlanState.HEDGE_UNWIND:
                self._transition(plan, PlanState.FILLED, ts_ns)
        else:
            if plan.state not in {
                PlanState.PARTIAL, PlanState.REJECTED, PlanState.TIMED_OUT,
                PlanState.HEDGE_UNWIND,
            }:
                self._transition(
                    plan, PlanState.PARTIAL, ts_ns,
                    "one or more child orders incomplete",
                )
            if plan.state != PlanState.HEDGE_UNWIND:
                self._transition(
                    plan, PlanState.HEDGE_UNWIND, ts_ns,
                    "residual requires policy action",
                )
        plan.terminal_outcome = (
            PlanState.FILLED.value
            if all_filled
            else next(
                (state.value for state in (PlanState.REJECTED, PlanState.TIMED_OUT)
                 if any(order.state.value == state.value for order in primary)),
                PlanState.PARTIAL.value,
            )
        )
        if plan.residual_base != 0:
            self._residual_event(
                plan, "RESIDUAL_DETECTED", ts_ns,
                max_residual_base=self.residual_policy.max_residual_base,
                max_residual_age_ns=self.residual_policy.max_residual_age_ns,
            )
        if not self._residual_within_bound(plan):
            if plan.state != PlanState.HEDGE_UNWIND:
                self._transition(
                    plan, PlanState.HEDGE_UNWIND, ts_ns,
                    "residual exceeds approved bound",
                )
            if self.router.has_pending_parent(plan.parent_id):
                return plan
            elapsed_ns = (
                max(0, ts_ns - plan.residual_since_ts_ns)
                if plan.residual_since_ts_ns is not None else 0
            )
            open_unwinds = self._open_unwind_orders(plan)
            if open_unwinds and elapsed_ns < self.residual_policy.max_residual_age_ns:
                self._residual_event(
                    plan, "RESIDUAL_UNWIND_PENDING", ts_ns,
                    open_order_ids=[order.order_id for order in open_unwinds],
                    elapsed_ns=elapsed_ns,
                    max_residual_age_ns=self.residual_policy.max_residual_age_ns,
                )
                return plan
            if open_unwinds:
                self._cancel_expired_unwinds(plan, ts_ns)
                if self.router.has_pending_parent(plan.parent_id) or self._open_unwind_orders(plan):
                    return plan
            bounded = self._execute_residual_unwind(
                plan, ts_ns, books if books is not None else self._books
            )
            self._recompute_plan_economics(plan, ts_ns)
            self._assess_legging_risk(plan, ts_ns, books)
            if not bounded:
                elapsed_ns = (
                    max(0, ts_ns - plan.residual_since_ts_ns)
                    if plan.residual_since_ts_ns is not None else 0
                )
                if not self.dust_on or plan.parent_id not in self._dust_breach_recorded:      # dust carry (owner 16 Nov): once per breach episode, not on every tick (the older profiles keep the repeat)
                    self._dust_breach_recorded.add(plan.parent_id)
                    self._residual_event(
                        plan, "RESIDUAL_LIMIT_BREACH", ts_ns,
                        max_residual_base=self.residual_policy.max_residual_base,
                        elapsed_ns=elapsed_ns,
                        max_residual_age_ns=self.residual_policy.max_residual_age_ns,
                        unwind_attempts=plan.unwind_attempts,
                    )
                self._raise_unwind_exhausted_alarm(plan, ts_ns)
                self._escalate_residual_breach(plan, ts_ns)
                return plan
            if self.router.has_pending_parent(plan.parent_id):
                return plan
        self._residual_breach_since.pop(plan.parent_id, None)
        self._dust_breach_recorded.discard(plan.parent_id)
        working = [
            order for order in plan.children
            if order.state in LIVE_ORDER_STATES
        ]
        if working:
            self._cancel_working_orders(plan, working, ts_ns)
            still_working = tuple(
                order.order_id for order in plan.children
                if order.state in LIVE_ORDER_STATES
            )
            if still_working or self.router.has_pending_parent(plan.parent_id):
                self._defer_close(
                    plan, ts_ns, "WORKING_ORDERS_CANCEL_PENDING",
                    still_working or tuple(order.order_id for order in working),
                )
                return plan
            return self._finalize_plan(plan, ts_ns, books)
        if plan.residual_base != 0:
            self._residual_event(
                plan, "RESIDUAL_WITHIN_APPROVED_BOUND", ts_ns,
                max_residual_base=self.residual_policy.max_residual_base,
            )
        return self._release_and_close(plan, ts_ns)

    def _finalize_quote(self, plan: ParentPlan, ts_ns: int) -> ParentPlan:
        """A market-making quote closes when its order (the whole amend lineage) has no live member. There is no hedge to wait for, no residual to unwind and no legging risk: what the quote filled is
        inventory, bounded by the pair cap at the layer's gate, so none of the arb residual rules (bound, unwind, alarm, hold on breach) applies. A quote still working is left alone."""
        self._recompute_plan_economics(plan, ts_ns)
        if any(order.state in LIVE_ORDER_STATES for order in plan.children):
            return plan
        root = next((order for order in plan.children if not order.replaces_order_id), plan.children[0])
        lineage_filled = sum((order.filled_quantity for order in plan.children), Decimal("0"))
        filled = lineage_filled >= root.quantity                      # the quote's own quantity, over the whole amend lineage (an amended quote ends CANCELLED on its last order once the lineage is complete)
        plan.terminal_outcome = "QUOTE_FILLED" if filled else ("QUOTE_PARTIAL_THEN_ENDED" if any(o.filled_quantity > 0 for o in plan.children) else "QUOTE_ENDED_UNFILLED")
        if filled:
            if plan.state != PlanState.FILLED:
                self._transition(plan, PlanState.FILLED, ts_ns)
        elif plan.state not in (PlanState.REJECTED, PlanState.TIMED_OUT):
            self._transition(plan, PlanState.REJECTED, ts_ns, "quote ended")
        self.ledger.record("QUOTE_PLAN_CLOSED", ts_ns, parent_id=plan.parent_id, outcome=plan.terminal_outcome, filled_base=str(sum((o.filled_quantity for o in plan.children), Decimal("0"))))
        return self._release_and_close(plan, ts_ns)

    def enforce_residual_limits(
        self, now_ns: int,
        books: dict[tuple[str, str], OrderBook] | None = None,
        *, only_due: bool = False, interval_ns: int = 100_000_000,
    ) -> tuple[ParentPlan, ...]:
        """Retry open residual plans and enforce the configured size/time deadline."""
        if books is not None:
            self._books.update(books)
        affected = []
        for plan in list(self.plans.values()):
            if plan.state != PlanState.HEDGE_UNWIND:
                continue
            if only_due and self._residual_next_check.get(plan.parent_id, 0) > now_ns:
                continue
            self._residual_next_check[plan.parent_id] = now_ns + interval_ns
            affected.append(self._finalize_plan(plan, now_ns, self._books))
        for parent_id in list(self._reconciliation_retry):
            plan = self.plans.get(parent_id)
            if plan is None or plan.state == PlanState.CLOSED:
                self._reconciliation_retry.discard(parent_id)
                continue
            if self.router.has_pending_parent(parent_id):
                continue
            affected.append(self._finalize_plan(plan, now_ns, self._books))
        return tuple(affected)

    def _revalidate_queued(self, pending: PendingRoute, now_ns: int) -> str | None:
        """EMS01: a request queued for a rate limit is released against the market as it is NOW, not the book captured when it was queued. Returns why the request is no
        longer worth sending (it is then expired and rejected as `expired_at_release`, never sent), or None. Covers every queued route, not only hedges.
        A risk-reducing request is exempt (it closes exposure); its book is checked where it is built (hedge dispatch)."""
        if pending.risk_reducing:
            return None
        req = pending.request
        why: str | None = None
        if self.risk.kill_switch:
            why = "kill_switch_active"
        else:
            book = self._books.get((req.venue, req.instrument_id)) or pending.book
            limit = self.release_policy.max_queue_age_ns
            if book.state != BookState.VALID:
                why = f"book_{book.state.value.lower()}"
            elif limit is not None and now_ns - pending.enqueued_ts_ns > limit:
                why = "queue_age"
            elif (self.release_policy.check_price and req.time_in_force == TimeInForce.IOC and req.price is not None and book.bids and book.asks):
                touch = book.best_ask if req.side == Side.BUY else book.best_bid
                if (req.side == Side.BUY and touch > req.price) or (req.side == Side.SELL and touch < req.price):
                    why = "price_moved_past_limit"
        if why is not None:
            self.realism_counters["expired_at_release"] = self.realism_counters.get("expired_at_release", 0) + 1
            self.expired_at_release.append({"order_id": req.order_id, "venue": req.venue, "reason": why, "queued_ns": now_ns - pending.enqueued_ts_ns, "ts_ns": now_ns})
        return why

    def process_throttled_routes(self, now_ns: int) -> tuple[RoutingResult, ...]:
        """Release due EMS work, converge delivered reports, close completed parents."""
        if self.order_owner_guard is not None:
            self.order_owner_guard()
        released = self.router.process_throttled(now_ns)
        touched: set[str] = set()
        for routing in released:
            request = routing.request
            if request is None or request.parent_order_id not in self.plans:
                continue
            plan = self.plans[request.parent_order_id]
            order = next(
                (x for x in plan.children if x.order_id == request.order_id), None
            )
            if order is None:
                continue
            touched.add(plan.parent_id)
            if routing.throttle_action == ThrottleAction.DELIVERED:
                callback = routing.callbacks[0]
                observed_ts = self._next_execution_ts(routing.release_ts_ns or now_ns)
                payload = {
                    "parent_id": plan.parent_id, "order_id": order.order_id,
                    "venue": order.venue, "request_action": request.action.value,
                    "callback_id": callback.callback_id,
                    "callback_type": callback.event_type.value,
                    "venue_event_ts_ns": callback.event_ts_ns,
                    "observed_ts_ns": observed_ts,
                    "report_latency_ns": observed_ts - callback.event_ts_ns,
                }
                self.ledger.record("CHILD_REPORT_OBSERVED", observed_ts, **payload)
                self._persist(plan, observed_ts, "CHILD_REPORT_OBSERVED", **payload)
                if plan.state == PlanState.CLOSED:
                    # A late report can be a benign confirmation (a cancel
                    # rejected because the order was already terminal, or a
                    # duplicate of a fill already applied) or genuinely new
                    # exposure discovered after the plan was declared flat.
                    # Only the latter is a risk event, and even that is
                    # escalated once per plan, not once per duplicate report.
                    reveals_new_fill = (
                        callback.event_type in {ExecutionEventType.PARTIAL, ExecutionEventType.FILLED}
                        and callback.cumulative_filled_quantity > order.filled_quantity
                    )
                    payload["reveals_new_fill"] = reveals_new_fill
                    self.ledger.record("LATE_REPORT_FOR_CLOSED_PLAN", observed_ts, **payload)
                    if reveals_new_fill and plan.parent_id not in self._late_fill_alerted:
                        self._late_fill_alerted.add(plan.parent_id)
                        self.risk.activate_kill_switch(
                            now_ns=observed_ts,
                            reason="fill discovered after plan close",
                            source="OMS_LATE_REPORT", ledger=self.ledger,
                        )
                    continue
                if callback.native and "traded through" in callback.reason:
                    self.realism_counters["resting_fill_reports"] += 1
                self._ingest_request_callback(plan, order, request, callback, observed_ts)
                fill_ts = observed_ts
            else:
                self._record_order_action(
                    plan, order, request, "SUBMIT",
                    "EXPIRED" if routing.throttle_action == ThrottleAction.EXPIRED else "RELEASED", now_ns,
                )
                fill_ts = now_ns
                for callback_index, callback in enumerate(routing.callbacks):
                    callback_ts = self._next_execution_ts(now_ns)
                    fill_ts = callback_ts
                    payload = {
                        "parent_id": plan.parent_id, "order_id": order.order_id,
                        "venue": order.venue, "callback_index": callback_index,
                        "callback_id": callback.callback_id,
                        "callback_type": callback.event_type.value,
                        "adapter_event_ts_ns": callback.event_ts_ns,
                        "local_callback_ts_ns": callback_ts,
                        "route_id": routing.route_id,
                    }
                    self.ledger.record(
                        "THROTTLED_CHILD_CALLBACK_OBSERVED", callback_ts, **payload
                    )
                    self._persist(
                        plan, callback_ts, "THROTTLED_CHILD_CALLBACK_OBSERVED", **payload
                    )
                    self.ingest_execution_callback(callback, callback_ts)
            if order.filled_quantity:
                self._price_fills(order, fill_ts)
                self.ledger.apply_fill(order, self._next_execution_ts(fill_ts))
                self._update_exposure_control(fill_ts)
            self._assess_legging_risk(plan, self._next_execution_ts(now_ns), self._books)
            if routing.throttle_action != ThrottleAction.DELIVERED:
                release_ts = self._next_execution_ts(now_ns)
                self.ledger.record(
                    "OMS_THROTTLED_ROUTE_RELEASED", release_ts,
                    parent_id=plan.parent_id, order_id=order.order_id,
                    route_id=routing.route_id,
                )
                self._persist(
                    plan, release_ts, "OMS_THROTTLED_ROUTE_RELEASED",
                    order_id=order.order_id, route_id=routing.route_id,
                )
        for parent_id in sorted(touched):
            if not self.router.has_pending_parent(parent_id):
                self._finalize_plan(
                    self.plans[parent_id], self._next_execution_ts(now_ns), self._books
                )
        return released

    # ---- time progression, venue queries and execution realism ------------

    def _assets(self, venue: str, instrument_id: str) -> tuple[str, str]:
        return instrument_assets(self.instruments, venue, instrument_id)

    def _price_fills(self, order: ChildOrder, ts_ns: int) -> None:
        """Price only the newly filled delta, at that fill's liquidity (ECO01).

        A GTC order can fill partly as taker on arrival and later as maker from
        its resting remainder; each delta is charged at its own rate. Fees are
        booked in the rule's fee asset (QUOTE, BASE or a token); ``order.fee``
        stays the reporting-currency value (cost positive, rebate negative).
        """
        priced_qty, priced_notional = self._fee_priced.get(order.order_id, (Decimal("0"), Decimal("0")))
        notional = order.filled_quantity * order.average_price
        delta_qty = order.filled_quantity - priced_qty
        delta_notional = notional - priced_notional
        if delta_qty <= 0 and delta_notional == 0:
            return
        reported = self._order_liquidity.get(order.order_id)
        liquidity = reported or (
            Liquidity.MAKER.value if order.post_only else Liquidity.TAKER.value
        )
        rule = self.instruments.fee_rule(order.venue, order.instrument_id, ts_ns)
        bps, source = rule.effective_bps(Liquidity(liquidity))
        # Never substitute silently (audit P1): say when the rate came from the
        # instrument default rather than a dated schedule rule, or when the
        # venue did not report whether the fill was maker or taker.
        if self.instruments.fee_schedule.rule(order.venue, order.instrument_id, ts_ns) is None:
            source = f"{source}|NO_SCHEDULE_RULE"
        if not reported:
            source = f"{source}|LIQUIDITY_INFERRED"
        base, quote = self._assets(order.venue, order.instrument_id)
        value = delta_notional * bps / Decimal("10000")
        quantity: Decimal | None
        if rule.fee_asset == "QUOTE":
            asset, quantity = quote, value
        elif rule.fee_asset == "BASE":
            asset, quantity = base, max(delta_qty, Decimal("0")) * bps / Decimal("10000")
            if delta_qty > 0:
                value = quantity * (delta_notional / delta_qty)
        else:
            asset = rule.fee_asset
            price = self.asset_prices.get((order.venue, asset)) or self.asset_prices.get(("*", asset))
            quantity = value / price if price else None
            if quantity is None:
                source = f"{source}|FEE_ASSET_UNPRICED"
        order.fee += value
        if quantity is not None:
            order.fee_quantity += quantity
        order.fee_asset = asset
        order.fee_rate_bps = bps
        order.fee_source = source
        order.liquidity = (
            liquidity if order.liquidity in ("", liquidity) else "MIXED"
        )
        self._fee_priced[order.order_id] = (order.filled_quantity, notional)

    def _ingest_request_callback(
        self, plan: ParentPlan, order: ChildOrder, request: CanonicalOrderRequest,
        callback: ExecutionCallback, observed_ts_ns: int,
    ) -> None:
        if (
            request.action == OrderAction.CANCEL
            and callback.event_type == ExecutionEventType.REJECTED
        ):
            # A rejected cancel says nothing reliable about the order itself
            # (it may have filled with the report still travelling): query.
            self.realism_counters["rejected_cancels_requeried"] += 1
            self.ledger.record(
                "CANCEL_REJECTED_ORDER_REQUERIED", observed_ts_ns,
                parent_id=plan.parent_id, order_id=order.order_id,
                venue=order.venue, reason=callback.reason,
            )
            if order.state not in TERMINAL_ORDER_STATES:
                self._watch_order(order, observed_ts_ns, immediate=True)
            return
        self.ingest_execution_callback(callback, observed_ts_ns)

    def _watch_order(
        self, order: ChildOrder, ts_ns: int, *, immediate: bool = False, count_unknown: bool = True,
    ) -> None:
        if immediate:
            self._requery_orders.add(order.order_id)
        if order.order_id in self._report_deadlines:
            if immediate:
                self._report_deadlines[order.order_id][0] = ts_ns
            return
        deadline = ts_ns if immediate else ts_ns + self.order_timeout_policy.report_timeout_ns
        self._report_deadlines[order.order_id] = [deadline, 0, order.parent_id]
        if count_unknown:
            self.realism_counters["unknown_state_orders"] += 1
        self.ledger.record(
            "ORDER_REPORT_DEADLINE_SET", ts_ns,
            parent_id=order.parent_id, order_id=order.order_id,
            venue=order.venue, state=order.state.value, deadline_ts_ns=deadline,
        )

    def _unknown_state_children(self, plan: ParentPlan, ts_ns: int) -> tuple[str, ...]:
        unknown = []
        for order in plan.children:
            if order.state in TERMINAL_ORDER_STATES:
                continue
            if order.time_in_force == TimeInForce.GTC.value and order.state in RESTING_ORDER_STATES:
                continue
            unknown.append(order.order_id)
            self._watch_order(order, ts_ns)
        return tuple(unknown)

    @staticmethod
    def _timed_out_unconfirmed(order: ChildOrder) -> bool:
        return order.state == OrderState.TIMED_OUT and not any(
            str(update["source"]) in {
                OrderUpdateSource.REST_QUERY.value,
                OrderUpdateSource.PRIVATE_STREAM.value,
            }
            for update in order.state_updates
        )

    def _defer_close(
        self, plan: ParentPlan, ts_ns: int, code: str, order_ids: tuple[str, ...]
    ) -> None:
        marker = (code, tuple(order_ids))
        if self._deferred_close.get(plan.parent_id) == marker:
            return
        self._deferred_close[plan.parent_id] = marker
        self.realism_counters["close_deferrals"] += 1
        self.ledger.record(
            "PLAN_CLOSE_DEFERRED", ts_ns, parent_id=plan.parent_id,
            opportunity_id=plan.opportunity.opportunity_id,
            code=code, order_ids=list(order_ids), residual_base=plan.residual_base,
        )
        self._persist(plan, ts_ns, "PLAN_CLOSE_DEFERRED", code=code, order_ids=list(order_ids))

    def _cancel_working_orders(
        self, plan: ParentPlan, orders: list[ChildOrder], ts_ns: int
    ) -> None:
        for order in orders:
            if self.router.has_pending_order(order.order_id):
                continue
            if order.state == OrderState.CREATED:
                continue
            request = CanonicalOrderRequest.create(
                action=OrderAction.CANCEL, order_id=order.order_id,
                target_order_id=order.order_id,
                parent_opportunity_id=order.opportunity_id,
                parent_order_id=order.parent_id,
                instrument_id=order.instrument_id, venue=order.venue,
                side=order.side, price=order.limit_price, quantity=order.quantity,
                order_type=OrderType.LIMIT, time_in_force=TimeInForce.GTC,
                reduce_only=order.reduce_only, created_ts_ns=ts_ns,
                idempotency_key=stable_id("close-working-cancel", order.order_id),
            )
            callbacks = self.route_order(request)
            self._record_order_action(
                plan, order, request, "CANCEL", "DISPATCHED", ts_ns,
                target_order_id=order.order_id,
            )
            self.ledger.record(
                "WORKING_ORDER_CANCEL_BEFORE_CLOSE", ts_ns,
                parent_id=plan.parent_id, order_id=order.order_id, venue=order.venue,
            )
            for callback in callbacks:
                self._ingest_request_callback(
                    plan, order, request, callback, self._next_execution_ts(ts_ns)
                )

    def _escalate_residual_breach(self, plan: ParentPlan, ts_ns: int) -> None:
        since = self._residual_breach_since.setdefault(plan.parent_id, ts_ns)
        kill_after = self.residual_policy.kill_after_ns
        if kill_after is None or self.risk.kill_switch or ts_ns - since < kill_after:
            return
        self.realism_counters["residual_breach_kill_escalations"] += 1
        self.risk.activate_kill_switch(
            now_ns=ts_ns, reason="residual breach unresolved",
            source="OMS_RESIDUAL_BREACH", ledger=self.ledger,
        )
        cancelled = self._cancel_orders(
            "residual breach unresolved", ts_ns, venue=None,
            preserve_risk_reducing=True, ledger_event="KILL_SWITCH_CANCEL_ALL_COMPLETE",
        )
        self.ledger.record(
            "RESIDUAL_BREACH_KILL_ESCALATION", ts_ns, parent_id=plan.parent_id,
            breach_since_ts_ns=since, cancelled_orders=cancelled,
        )

    def _apply_venue_status(
        self, plan: ParentPlan, order: ChildOrder, status: VenueOrderStatus, ts_ns: int
    ) -> None:
        state = status.state
        reason = status.reason
        cumulative = status.cumulative_filled_quantity
        if state is None:
            if order.filled_quantity > 0:
                # Venue has no record of this order, but we already hold a confirmed
                # partial fill from the private stream.  Mapping to REJECTED would
                # overwrite PARTIAL in state arbitration (ORDER_STATE_RANK 4 > 2).
                # Defer: re-schedule an immediate re-query and preserve the known
                # fill rather than booking a contradictory rejection.
                self._watch_order(order, ts_ns, immediate=True)
                self.ledger.record(
                    "VENUE_QUERY_NOT_FOUND_DEFERRED", ts_ns,
                    parent_id=plan.parent_id, order_id=order.order_id,
                    venue=order.venue, known_fill=str(order.filled_quantity),
                )
                return
            state = ExecutionEventType.REJECTED
            reason = reason or "venue has no record of order"
            cumulative = Decimal("0")
        self.ingest_order_update(CanonicalOrderUpdate(
            update_id=stable_id(
                "venue-query", order.order_id, state.value, cumulative, ts_ns
            ),
            source=OrderUpdateSource.REST_QUERY,
            parent_order_id=plan.parent_id, order_id=order.order_id,
            venue=order.venue, instrument_id=order.instrument_id, state=state,
            event_ts_ns=status.venue_ts_ns or ts_ns, observed_ts_ns=ts_ns,
            cumulative_filled_quantity=cumulative,
            average_price=status.average_price, reason=reason,
            venue_order_id=status.venue_order_id,
        ))
        if order.filled_quantity:
            self._price_fills(order, ts_ns)
            self.ledger.apply_fill(order, ts_ns)
            self._update_exposure_control(ts_ns)

    def _note_ambiguous_dispatch(
        self, plan: ParentPlan, order: ChildOrder, idempotency_key: str, action: str, exc: BaseException, ts_ns: int,
    ) -> None:
        """A submit/cancel call raised: the venue may or may not have acted. Record it and make sure it is re-queried."""
        self.realism_counters["ambiguous_dispatch_errors"] += 1
        error = f"{type(exc).__name__}: {exc}"
        self.ledger.record(
            "DISPATCH_EXCEPTION_AMBIGUOUS", ts_ns, parent_id=plan.parent_id, order_id=order.order_id,
            venue=order.venue, action=action, idempotency_key=idempotency_key, error=error,
            local_state=order.state.value,
        )
        self._persist(
            plan, ts_ns, "DISPATCH_EXCEPTION_AMBIGUOUS", order_id=order.order_id, venue=order.venue,
            action=action, idempotency_key=idempotency_key, error=error,
        )
        self._watch_order(order, ts_ns, immediate=(action == "CANCEL"))

    def check_order_deadlines(self, now_ns: int) -> int:
        """Query venues for orders past their report deadline; never resend."""
        policy = self.order_timeout_policy
        handled = 0
        for order_id, entry in sorted(
            self._report_deadlines.items(), key=lambda item: (item[1][0], item[0])
        ):
            if entry[0] > now_ns:
                continue
            plan = self.plans.get(entry[2])
            order = (
                next((x for x in plan.children if x.order_id == order_id), None)
                if plan is not None else None
            )
            if plan is None or order is None or plan.state == PlanState.CLOSED:
                self._report_deadlines.pop(order_id, None)
                continue
            if order.state in TERMINAL_ORDER_STATES or (
                order.time_in_force == TimeInForce.GTC.value
                and order.state in RESTING_ORDER_STATES
                and order_id not in self._requery_orders
            ):
                self._report_deadlines.pop(order_id, None)
                continue
            if order.replaces_order_id and order.state in (OrderState.SENT, OrderState.CREATED):
                # An amend the venue has not confirmed in time: the replacement is UNKNOWN (TIMED_OUT, which later venue evidence clears: a late success is still honoured), the OLD order is queried,
                # and nothing is sent. The replacement itself is not queried: "not found" would only mean the venue has not processed the amend yet.
                self._report_deadlines.pop(order_id, None)
                handled += 1
                self.mark_order_timed_out(plan.parent_id, order_id, self._next_execution_ts(now_ns), reason="amend unconfirmed: the venue's answer did not arrive in time")
                continue
            if self.router.has_pending_order(order_id):
                entry[0] = now_ns + policy.query_interval_ns
                continue
            handled += 1
            entry[1] += 1
            self.realism_counters["venue_queries"] += 1
            query_ts = self._next_execution_ts(now_ns)
            status = self.router.query_order(order.venue, order_id, query_ts)
            self.ledger.record(
                "VENUE_ORDER_QUERY", query_ts, parent_id=plan.parent_id,
                order_id=order_id, venue=order.venue, attempt=entry[1],
                local_state=order.state.value, answered=status is not None,
                venue_state=(
                    (status.state.value if status.state else "UNKNOWN_TO_VENUE")
                    if status is not None else None
                ),
            )
            self._hedge_on_query(plan, order, status is not None, query_ts)
            if status is not None:
                self._apply_venue_status(plan, order, status, query_ts)
                if order.state in TERMINAL_ORDER_STATES or (
                    order.time_in_force == TimeInForce.GTC.value
                    and order.state in RESTING_ORDER_STATES
                ):
                    self.realism_counters["resolved_by_query"] += 1
                    self._report_deadlines.pop(order_id, None)
                    self._requery_orders.discard(order_id)
                    self._deferred_close.pop(plan.parent_id, None)
                    self._finalize_plan(plan, self._next_execution_ts(query_ts), self._books)
                    continue
            if entry[1] >= policy.max_queries:
                self._report_deadlines.pop(order_id, None)
                self._requery_orders.discard(order_id)
                self.realism_counters["unresolved_after_queries"] += 1
                self.mark_order_timed_out(
                    plan.parent_id, order_id, self._next_execution_ts(query_ts),
                    reason="venue report and queries unanswered",
                )
                if policy.kill_on_unresolved and not self.risk.kill_switch:
                    kill_ts = self._next_execution_ts(query_ts)
                    self.risk.activate_kill_switch(
                        now_ns=kill_ts, reason="unconfirmed order state",
                        source="OMS_ORDER_DEADLINE", ledger=self.ledger,
                    )
                    self.cancel_open_orders("unconfirmed order state", kill_ts)
                continue
            entry[0] = query_ts + policy.query_interval_ns
        return handled

    def next_timer_ts(self) -> int | None:
        candidates = [entry[0] for entry in self._report_deadlines.values()]
        router_due = self.router.next_due_ts()
        if router_due is not None:
            candidates.append(router_due)
        candidates.extend(self._hedge_timer_candidates())
        candidates.extend(self._lifetime_deadlines.values())
        return min(candidates) if candidates else None

    def _expire_due_orders(self, now_ns: int) -> None:
        """Cancel every resting post-only order whose lifetime ended at or before ``now_ns``, at its own deadline (the cancel then takes its cancel latency)."""
        for order_id, deadline in sorted(self._lifetime_deadlines.items(), key=lambda item: item[1]):
            if deadline > now_ns:
                break
            del self._lifetime_deadlines[order_id]
            for plan in self.plans.values():
                order = next((x for x in plan.children if x.order_id == order_id), None)
                if order is None:
                    continue
                if order.state in LIVE_ORDER_STATES and not self.router.has_pending_order(order_id):
                    self.ledger.record("ORDER_LIFETIME_EXPIRED", deadline, parent_id=plan.parent_id, order_id=order_id, venue=order.venue, lifetime_ns=self.order_lifetime_ns)
                    self.note_cancel_requested(order_id, deadline)
                    self._cancel_working_orders(plan, [order], deadline)
                break

    def advance_time(
        self, now_ns: int, books: dict[tuple[str, str], OrderBook] | None = None,
        *, max_steps: int = 1_000_000,
    ) -> int:
        """Run every EMS/OMS timer due up to ``now_ns`` at its own timestamp."""
        if books is not None:
            self._books.update(books)
        steps = 0
        while True:
            due = self.next_timer_ts()
            if due is None or due > now_ns:
                break
            self.process_throttled_routes(due)
            self.check_order_deadlines(due)
            self._hedge_progress_all(due)
            self._expire_due_orders(due)
            steps += 1
            if steps >= max_steps:
                raise RuntimeError("EMS timers did not converge")
        self.enforce_residual_limits(now_ns, only_due=True)
        self._hedge_progress_all(now_ns)
        return steps

    def on_book_update(self, venue: str, instrument_id: str, now_ns: int) -> int:
        book = self._books.get((venue, instrument_id))
        if book is None:
            return 0
        produced = self.router.on_book_update(venue, book, now_ns)
        if produced:
            self.process_throttled_routes(now_ns)
        self._hedge_progress_all(now_ns)            # a held hedge dispatch (bad book) is retried as soon as the book may be valid again
        return produced

    def on_trade(self, venue: str, trade: TradeEvent, now_ns: int) -> int:
        """Phase D (D2): route a public trade print to the venue simulator, like ``on_book_update``. No consumer yet: only an adapter with an ``on_trade`` hook reacts."""
        produced = self.router.on_trade(venue, trade, now_ns)
        if produced:
            self.process_throttled_routes(now_ns)
            self._hedge_progress_all(now_ns)
        return produced

    def drain(self, until_ns: int) -> dict[str, object]:
        """Deliver everything in flight and run deadlines up to ``until_ns``."""
        self.advance_time(until_ns)
        return self.execution_realism_snapshot()

    def execution_realism_snapshot(self) -> dict[str, object]:
        open_plans = [plan for plan in self.plans.values() if plan.state != PlanState.CLOSED]
        return {
            "schema_version": "1.0.0",
            "latency_model": (
                self.router.latency_model.source
                if self.router.latency_model is not None else "DISABLED_ZERO_LATENCY"
            ),
            "order_timeout_policy": {
                "report_timeout_ns": self.order_timeout_policy.report_timeout_ns,
                "query_interval_ns": self.order_timeout_policy.query_interval_ns,
                "max_queries": self.order_timeout_policy.max_queries,
                "kill_on_unresolved": self.order_timeout_policy.kill_on_unresolved,
            },
            **self.realism_counters,
            "open_report_deadlines": len(self._report_deadlines),
            "in_flight_or_undelivered": self.router.next_due_ts() is not None,
            "open_plans": len(open_plans),
            "open_plan_states": sorted({plan.state.value for plan in open_plans}),
            "residual_breaches_open": sorted(self._residual_breach_since),
        }

    @staticmethod
    def _resolve_order_state(order: ChildOrder) -> OrderState:
        """The order's state from every update held, in arrival order (OMS02).

        1. Terminal venue facts win: full fill, then cancel, then reject. A reject after a
           fill is contradictory: the fill is kept and the order reads PARTIAL (D-06); the
           reducer re-queries the venue.
        2. A timeout (TIMER) that arrived after every piece of venue evidence and after the
           last cancel request means the state is unknown: TIMED_OUT. Venue evidence that
           arrives after it clears it (D-04); the query loop's last-resort timeout of a
           quiet acknowledged order still applies.
        3. A cancel sent and not answered by a later venue query: CANCEL_PENDING (D-05).
        4. Otherwise the best venue evidence: a fill (PARTIAL), then an acknowledgement.
        5. Nothing heard back: SENT once dispatched, else CREATED.
        """
        rows = order.state_updates
        states = [OrderState(str(row["resolved_order_state"])) for row in rows]
        filled = order.filled_quantity

        def last(predicate) -> int:
            return max((i for i, row in enumerate(rows) if predicate(row, states[i])), default=-1)

        if filled >= order.quantity or OrderState.FILLED in states:
            return OrderState.FILLED
        if OrderState.CANCELLED in states:
            return OrderState.CANCELLED
        if OrderState.REJECTED in states:
            return OrderState.PARTIAL if filled > 0 else OrderState.REJECTED
        timeout = last(lambda row, state: state == OrderState.TIMED_OUT)
        evidence = last(lambda row, state: str(row["source"]) in VENUE_SOURCES
                        and state != OrderState.TIMED_OUT)
        cancel = last(lambda row, state: state == OrderState.CANCEL_PENDING)
        query = last(lambda row, state: str(row["source"]) == OrderUpdateSource.REST_QUERY.value)
        if timeout >= 0 and timeout > evidence and timeout > cancel:
            return OrderState.TIMED_OUT
        if cancel >= 0 and cancel > query:
            return OrderState.CANCEL_PENDING
        amend = last(lambda row, state: state == OrderState.AMEND_PENDING)
        if amend >= 0 and amend > query:             # an amend sent and not answered by a later venue query (or ended by the replacement's confirmation, which cancels this order): still live, fills count
            return OrderState.AMEND_PENDING
        if filled > 0 or OrderState.PARTIAL in states:
            return OrderState.PARTIAL
        if OrderState.ACKNOWLEDGED in states:
            return OrderState.ACKNOWLEDGED
        if timeout >= 0:
            return OrderState.TIMED_OUT
        if OrderState.SENT in states:
            return OrderState.SENT
        return OrderState.CREATED

    @staticmethod
    def _order_update_key(row: dict[str, object]) -> tuple[object, ...]:
        state = OrderState(str(row["resolved_order_state"]))
        return (
            ORDER_STATE_RANK[state],
            Decimal(str(row["cumulative_filled_quantity"])),
            int(row["event_ts_ns"]),
            int(row.get("sequence", 0)),
            SOURCE_RANK[str(row["source"])],
            str(row["update_id"]),
        )

    def _derive_execution_state(self, plan: ParentPlan, ts_ns: int) -> None:
        spec = self._hedge_specs.get(plan.parent_id)
        if spec is not None:
            # FILL_TRIGGERED: the plan is judged by the maker and the LAST child of each hedge chain (an earlier rejected attempt that was retried does not count),
            # and it is FILLED only when the hedges together cover the whole maker size
            relevant = [o for o in plan.children if o.order_id == spec["maker_order_id"]]
            for ids in self._chain_children.values():
                if ids and any(o.order_id == ids[-1] for o in plan.children):
                    relevant.append(next(o for o in plan.children if o.order_id == ids[-1]))
            states = {order.state for order in relevant}
            hedged = sum((o.filled_quantity for o in plan.children if o.order_role == "HEDGE"), Decimal("0"))
            if all(state == OrderState.FILLED for state in states) and hedged < spec["max_qty"]:
                states = states | {OrderState.PARTIAL}
            maker_head = next((o for o in relevant if o.order_id == spec["maker_order_id"]), None)
            if (maker_head is not None and maker_head.state in (OrderState.ACKNOWLEDGED, OrderState.AMEND_PENDING)
                    and self._lineage_filled(plan, maker_head) > 0):
                # a quote that has filled part of its size is PARTIAL whatever its head reads: after an amend the head is the replacement (no fill of its own yet),
                # during a pending amend it is the old order (AMEND_PENDING, which no plan state maps to), and a hedge child that is merely acknowledged must not
                # pull the plan back from PARTIAL to ACKNOWLEDGED, an illegal transition (found by the v1_10 case 2i / 2n replays, 16 Nov)
                states = states | {OrderState.PARTIAL}
        elif plan.parent_id in self._quote_plans:
            # a quote that was amended is judged by its live head, not by the old order the venue replaced (which reads CANCELLED)
            states = {order.state for order in plan.children if not self._amend_bookkeeping_order(plan, order)} or {order.state for order in plan.children}
        else:
            states = {order.state for order in plan.children}
        if all(state == OrderState.FILLED for state in states):
            target = PlanState.FILLED
        elif OrderState.REJECTED in states or OrderState.CANCELLED in states:
            target = PlanState.REJECTED
        elif OrderState.TIMED_OUT in states:
            target = PlanState.TIMED_OUT
        elif any(state in (OrderState.PARTIAL, OrderState.FILLED) for state in states) or any(
            order.state == OrderState.CANCEL_PENDING and order.filled_quantity > 0 for order in plan.children
        ):
            target = PlanState.PARTIAL
        elif OrderState.ACKNOWLEDGED in states or OrderState.CANCEL_PENDING in states:
            target = PlanState.ACKNOWLEDGED
        else:
            return
        if plan.state in {PlanState.REJECTED, PlanState.TIMED_OUT} and target in {
            PlanState.ACKNOWLEDGED, PlanState.PARTIAL
        }:
            return
        if plan.state in {
            PlanState.DISPATCHED, PlanState.ACKNOWLEDGED, PlanState.PARTIAL,
            PlanState.REJECTED, PlanState.TIMED_OUT,
        }:
            self._transition(plan, target, ts_ns, "derived from child order states")

    def ingest_order_update(self, update: CanonicalOrderUpdate) -> ParentPlan:
        """Apply adapter, private-stream, REST, or timer state through one reducer."""
        try:
            plan = self.plans[update.parent_order_id]
        except KeyError as exc:
            self.risk.activate_kill_switch(
                now_ns=update.observed_ts_ns, reason="unknown parent order update",
                source="OMS_ORDER_REDUCER", ledger=self.ledger,
            )
            self.ledger.record(
                "ORPHAN_ORDER_UPDATE_REJECTED", update.observed_ts_ns,
                parent_id=update.parent_order_id,
                order_id=update.order_id,
                update_id=update.update_id,
                venue=update.venue,
                instrument_id=update.instrument_id,
                source=update.source.value,
                reason="unknown_parent_plan",
            )
            raise ValueError(f"unknown parent plan {update.parent_order_id}") from exc
        try:
            order = next(x for x in plan.children if x.order_id == update.order_id)
        except StopIteration as exc:
            self.risk.activate_kill_switch(
                now_ns=update.observed_ts_ns, reason="unknown child order update",
                source="OMS_ORDER_REDUCER", ledger=self.ledger,
            )
            self.ledger.record(
                "ORPHAN_ORDER_UPDATE_REJECTED", update.observed_ts_ns,
                parent_id=plan.parent_id,
                opportunity_id=plan.opportunity.opportunity_id,
                order_id=update.order_id,
                update_id=update.update_id,
                venue=update.venue,
                instrument_id=update.instrument_id,
                source=update.source.value,
                reason="unknown_child_order",
            )
            self._persist(
                plan, update.observed_ts_ns, "ORPHAN_ORDER_UPDATE_REJECTED",
                order_id=update.order_id, update_id=update.update_id,
                venue=update.venue, instrument_id=update.instrument_id,
                source=update.source.value, reason="unknown_child_order",
            )
            raise ValueError(f"unknown child order {update.order_id}") from exc
        if update.venue != order.venue or update.instrument_id != order.instrument_id:
            raise ValueError(f"identity mismatch for order update {update.update_id}")
        if update.cumulative_filled_quantity < 0 or update.cumulative_filled_quantity > order.quantity:
            raise ValueError(f"invalid cumulative fill for order {order.order_id}")
        if any(row["update_id"] == update.update_id for row in order.state_updates):
            self.ledger.record(
                "ORDER_UPDATE_DUPLICATE_SUPPRESSED", update.observed_ts_ns,
                parent_id=plan.parent_id, order_id=order.order_id,
                update_id=update.update_id, source=update.source.value,
            )
            self._persist(
                plan, update.observed_ts_ns, "ORDER_UPDATE_DUPLICATE_SUPPRESSED",
                order_id=order.order_id, update_id=update.update_id,
                source=update.source.value,
            )
            return plan
        if plan.state == PlanState.CLOSED and any(
            row.get("resolved_order_state") == ORDER_STATE_BY_EVENT[update.state].value
            and Decimal(str(row.get("cumulative_filled_quantity"))) == update.cumulative_filled_quantity
            for row in order.state_updates
        ):
            # Owner 12 Nov (EMS note): a venue repeats a message (Bybit's duplicate "Filled" in a cancel/fill race) after the plan closed. It says nothing new (same state, same cumulative fill as an update already
            # applied): it is suppressed like any duplicate, not raised. Booked once, hedged once. An update that DOES say something new still takes the rejected path below.
            self.ledger.record(
                "ORDER_UPDATE_DUPLICATE_SUPPRESSED", update.observed_ts_ns,
                parent_id=plan.parent_id, order_id=order.order_id,
                update_id=update.update_id, source=update.source.value, same_content_as_applied=True, plan_closed=True,
            )
            return plan
        if plan.state == PlanState.CLOSED:
            self.ledger.record(
                "CLOSED_PLAN_UPDATE_REJECTED", update.observed_ts_ns,
                parent_id=plan.parent_id, order_id=order.order_id,
                update_id=update.update_id, source=update.source.value,
            )
            self._persist(
                plan, update.observed_ts_ns, "CLOSED_PLAN_UPDATE_REJECTED",
                order_id=order.order_id, update_id=update.update_id,
                source=update.source.value,
            )
            raise ValueError(f"closed plan {plan.parent_id} is immutable")

        row = update.to_dict()
        row["resolved_order_state"] = ORDER_STATE_BY_EVENT[update.state].value
        order.state_updates.append(row)
        previous_state = order.state
        winner = max(order.state_updates, key=self._order_update_key)
        fill_winner = max(
            order.state_updates,
            key=lambda item: (
                Decimal(str(item["cumulative_filled_quantity"])),
                int(item["event_ts_ns"]), int(item.get("sequence", 0)),
                SOURCE_RANK[str(item["source"])], str(item["update_id"]),
            ),
        )
        order.filled_quantity = Decimal(str(fill_winner["cumulative_filled_quantity"]))
        order.average_price = Decimal(str(fill_winner["average_price"]))
        order.state = self._resolve_order_state(order)
        # Fees never regress in magnitude: the OMS may have priced a fill
        # before a venue snapshot without fee data arrives. Compared by
        # magnitude so a rebate (negative fee) is not replaced by zero.
        reported = [Decimal(str(item["fee"])) for item in order.state_updates if Decimal(str(item["fee"]))]
        if reported:
            venue_fee = max(reported, key=abs)
            if abs(venue_fee) > abs(order.fee):
                order.fee = venue_fee
                order.fee_source = "VENUE_REPORTED"
        if update.liquidity:
            self._order_liquidity[order.order_id] = update.liquidity
        order.reject_reason = str(winner.get("reason") or "") or None
        if order.state == OrderState.REJECTED:
            self._classify_reject(plan, order, update.observed_ts_ns)

        terminal_claims = sorted({
            str(item["resolved_order_state"]) for item in order.state_updates
            if OrderState(str(item["resolved_order_state"])) in TERMINAL_ORDER_STATES
        })
        payload = {
            "parent_id": plan.parent_id, "order_id": order.order_id,
            "update": row, "resolved_order_state": order.state.value,
            "terminal_claims": terminal_claims,
        }
        self.ledger.record("ORDER_STATE_UPDATE_APPLIED", update.observed_ts_ns, **payload)
        self.ledger.record_order_update(
            lifecycle_event=update.state.value,
            ts_ns=update.observed_ts_ns,
            parent_id=plan.parent_id,
            opportunity_id=plan.opportunity.opportunity_id,
            order_id=order.order_id,
            venue=order.venue,
            instrument_id=order.instrument_id,
            source=update.source.value,
            update_id=update.update_id,
            cumulative_filled_quantity=order.filled_quantity,
            average_price=order.average_price,
            fee=order.fee,
            reason=update.reason,
        )
        self._persist(
            plan, update.observed_ts_ns, "ORDER_LIFECYCLE",
            lifecycle_event=update.state.value,
            source=update.source.value,
            parent_id=plan.parent_id,
            opportunity_id=plan.opportunity.opportunity_id,
            order_id=order.order_id,
            update_id=update.update_id,
            venue=order.venue,
            instrument_id=order.instrument_id,
            cumulative_filled_quantity=order.filled_quantity,
            average_price=order.average_price,
            fee=order.fee,
            reason=update.reason,
        )
        if order.filled_quantity > 0 and (plan.parent_id in self._hedge_specs or plan.parent_id in self._quote_plans):
            self._price_fills(order, update.observed_ts_ns)      # FILL_TRIGGERED: a maker fill arrives as a stream update, not as a dispatch callback: it is priced here (once per delta)
        self.ledger.apply_fill(order, update.observed_ts_ns)
        self._update_exposure_control(update.observed_ts_ns)
        self._persist(plan, update.observed_ts_ns, "ORDER_STATE_UPDATE_APPLIED", **payload)
        if len(terminal_claims) > 1:
            self.ledger.record(
                "ORDER_TERMINAL_CONFLICT_RESOLVED", update.observed_ts_ns,
                parent_id=plan.parent_id, order_id=order.order_id,
                claims=terminal_claims, winner=order.state.value,
            )
            self._persist(
                plan, update.observed_ts_ns, "ORDER_TERMINAL_CONFLICT_RESOLVED",
                order_id=order.order_id, claims=terminal_claims,
                winner=order.state.value,
            )
        self._after_order_state_change(plan, order, previous_state, update)
        if self.observer is not None:
            self.observer.order_changed(plan, order, order.state in TERMINAL_ORDER_STATES and order.state != OrderState.TIMED_OUT, order.state == OrderState.TIMED_OUT, update.observed_ts_ns / 1e9)
        if self.order_lifetime_ns is not None:
            if update.state == ExecutionEventType.ACKNOWLEDGED and order.post_only and order.state in LIVE_ORDER_STATES:
                self._lifetime_deadlines.setdefault(order.order_id, update.event_ts_ns + self.order_lifetime_ns)
            elif order.state in TERMINAL_ORDER_STATES:
                self._lifetime_deadlines.pop(order.order_id, None)
        self._derive_execution_state(plan, update.observed_ts_ns)
        if plan.parent_id in self._hedge_specs:
            self._fill_triggered_react(plan, order, update.observed_ts_ns)
        if self._lineage_of:
            self._amend_react(plan, order, update)
        if plan.parent_id in self._quote_plans and plan.state != PlanState.CLOSED and not any(o.state in LIVE_ORDER_STATES for o in plan.children):
            self._finalize_plan(plan, update.observed_ts_ns, self._books)      # a quote has no hedge to wait for: it closes as soon as its order (and every order of its amend lineage) has ended
        return plan

    @staticmethod
    def _market_for(venue: str) -> str | None:
        v = venue.lower()
        return _MKT_BYBIT if "bybit" in v else _MKT_BINANCE if "binance" in v else None

    def _classify_reject(self, plan: ParentPlan, order: ChildOrder, ts_ns: int) -> None:
        """Every reject goes through the one venue code table (venue_semantics_perp.classify_reject), once per order: the label and whether the same
        request may be sent again. An unclassified reason is not retryable (fail closed) and is counted. Nothing retries on this yet: the record is the input to retry-before-unwind."""
        if order.order_id in self._rejects_classified:
            return
        self._rejects_classified.add(order.order_id)
        verdict = classify_reject(self._market_for(order.venue), order.reject_reason or "")
        key = "reject_classified" if verdict.classified else "reject_unclassified"
        self.realism_counters[key] = self.realism_counters.get(key, 0) + 1
        record = {"parent_id": plan.parent_id, "order_id": order.order_id, "venue": order.venue, "role": order.order_role, "ts_ns": ts_ns,
                  "reason": order.reject_reason or "", "label": verdict.label, "retry_permitted": verdict.retry_permitted,
                  "basis": verdict.basis, "classified": verdict.classified}
        self.reject_records.append(record)
        self.ledger.record("ORDER_REJECT_CLASSIFIED", ts_ns, **{k: v for k, v in record.items() if k != "ts_ns"})

    def _after_order_state_change(
        self, plan: ParentPlan, order: ChildOrder, previous: OrderState, update: CanonicalOrderUpdate,
    ) -> None:
        """Checks and follow-ups once an update has changed the resolved state."""
        state, ts_ns = order.state, update.observed_ts_ns
        if update.state == ExecutionEventType.REJECTED and state == OrderState.PARTIAL:
            # D-06: a reject after a fill contradicts the fill (the state stays PARTIAL, so
            # this is checked before the no-change return). Keep the fill, ask the venue.
            self.ledger.record(
                "REJECT_AFTER_FILL_REQUERIED", ts_ns, parent_id=plan.parent_id, order_id=order.order_id,
                filled=str(order.filled_quantity), reason=update.reason,
            )
            self._watch_order(order, ts_ns, immediate=True)
        if (update.state == ExecutionEventType.REJECTED and order.filled_quantity > 0
                and order.order_id not in self._reject_after_fill_alarmed):
            # The re-query settles the state (a full fill needs none); the contradiction itself stays
            # on record for the operator, once per order.
            self._reject_after_fill_alarmed.add(order.order_id)
            self.ledger.record(
                "REJECT_AFTER_FILL_ALARM", ts_ns, parent_id=plan.parent_id, order_id=order.order_id,
                filled_quantity=str(order.filled_quantity), reason=update.reason,
            )
            self._persist(
                plan, ts_ns, "REJECT_AFTER_FILL_ALARM", order_id=order.order_id,
                filled_quantity=str(order.filled_quantity), reason=update.reason,
            )
        if state == previous:
            return
        if state not in ALLOWED_ORDER_TRANSITIONS[previous]:
            self.realism_counters["unexpected_order_transitions"] += 1
            self.ledger.record(
                "ORDER_TRANSITION_UNEXPECTED", ts_ns, parent_id=plan.parent_id, order_id=order.order_id,
                from_state=previous.value, to_state=state.value, update_id=update.update_id,
                source=update.source.value,
            )
        if previous == OrderState.TIMED_OUT and state in LIVE_ORDER_STATES:
            # D-04: the order was given up for unknown but the venue says it is live; it counts as
            # live for risk from here. If the plan is already unwinding against it (the unwind was
            # sized as if it were dead), pull it so it cannot fill on top of the unwind.
            self.realism_counters["late_evidence_after_timeout"] += 1
            self.ledger.record(
                "LATE_EVIDENCE_AFTER_TIMEOUT", ts_ns, parent_id=plan.parent_id, order_id=order.order_id,
                state=state.value, source=update.source.value, plan_state=plan.state.value,
            )
            if plan.state == PlanState.HEDGE_UNWIND and state != OrderState.CANCEL_PENDING:
                self._cancel_working_orders(plan, [order], ts_ns)
        if state == OrderState.CANCEL_PENDING and previous != OrderState.CANCEL_PENDING:
            # A cancel that is never answered must not stay pending: query on the report deadline.
            self._watch_order(order, ts_ns, count_unknown=False)

    def _record_internal_update(
        self, plan: ParentPlan, order: ChildOrder, state: ExecutionEventType, ts_ns: int, key: str,
    ) -> None:
        """SENT / CANCEL_REQUESTED, through the one reducer so the ledger records them too."""
        if plan.state == PlanState.CLOSED or order.state in TERMINAL_ORDER_STATES - {OrderState.TIMED_OUT}:
            return
        self.ingest_order_update(CanonicalOrderUpdate(
            update_id=stable_id("internal", state.value, order.order_id, key),
            source=OrderUpdateSource.INTERNAL,
            parent_order_id=plan.parent_id, order_id=order.order_id, venue=order.venue,
            instrument_id=order.instrument_id, state=state,
            cumulative_filled_quantity=order.filled_quantity, average_price=order.average_price,
            event_ts_ns=ts_ns, observed_ts_ns=ts_ns,
        ))

    def withdraw_quote(self, order_id: str, now_ns: int, reason: str, net_bps: float | None = None) -> bool:
        """Quote validity (owner 12 Nov): cancel one resting maker quote whose edge no longer clears the threshold, through the same cancel path as the order lifetime (the cancel tier of the rate limiter).
        Returns False when the order is not a live order with no cancel in flight. A fill that lands before the venue honours the cancel is booked by the normal fill path, once, and hedged."""
        for plan in self.plans.values():
            order = next((x for x in plan.children if x.order_id == order_id), None)
            if order is None:
                continue
            if order.state not in LIVE_ORDER_STATES or order.state == OrderState.CANCEL_PENDING or self.router.has_pending_order(order_id):
                return False
            if order.state == OrderState.AMEND_PENDING or self._lineage_of.get(order_id):
                self.ledger.record("QUOTE_WITHDRAWN", now_ns, parent_id=plan.parent_id, order_id=order_id, venue=order.venue, reason=reason, net_edge_bps=net_bps)
                return self.urgent_cancel(order_id, now_ns, reason)
            self.ledger.record("QUOTE_WITHDRAWN", now_ns, parent_id=plan.parent_id, order_id=order_id, venue=order.venue, reason=reason, net_edge_bps=net_bps)
            self.note_cancel_requested(order_id, now_ns)
            self._cancel_working_orders(plan, [order], now_ns)
            return True
        return False

    # ---- amend (owner 12 Nov; off unless amend_enabled) ------------------------------------------------------------------------------------------------------------------------------------------
    # A price amend is modelled as a lineage of two orders: the OLD order (AMEND_PENDING: still live at the OLD price, its fills count) and a REPLACEMENT order (sent with the new price, quantity = what was left).
    # The new price counts only when the venue CONFIRMS (a REPLACED report, or the replacement live / filled): the gateway accepting the request (AMEND_ACCEPTED) confirms nothing. On confirmation the old order
    # is recorded CANCELLED (the venue replaced it; every fill it had is already booked), the hedge spec follows to the replacement and the lineage continues. A reject sends a venue query about the OLD order; a
    # timeout leaves both alive and queries: the OMS never sends a second order for the same quote. Fills of the lineage count once each toward the hedge; the lineage never takes more than the quote's
    # quantity (a sibling is cancelled when it is complete).
    def request_amend(self, order_id: str, new_price: Decimal, now_ns: int, reason: str = "requote") -> str:
        """Ask the venue to move a resting maker quote to `new_price`. Returns REQUESTED, FALLBACK_CANCEL (the route cannot amend: the order is cancelled and NOTHING is sent in its place: the caller quotes again
        once the cancel is confirmed, so two live orders never exist), or REFUSED:<why>."""
        if not self.amend_enabled:
            return "REFUSED:amend_disabled"
        if self.amend_blocked:                                       # owner 16 Nov: the real-order amend guard (set by the session); the same refusal the session's own rule gives
            self.realism_counters["amend_refused_real_order_guard"] = self.realism_counters.get("amend_refused_real_order_guard", 0) + 1
            return self.amend_blocked
        found = next(((plan, x) for plan in self.plans.values() for x in plan.children if x.order_id == order_id), None)
        if found is None:
            return "REFUSED:unknown_order"
        plan, order = found
        if order.order_role != "PRIMARY" or not order.post_only or (plan.parent_id not in self._hedge_specs and plan.parent_id not in self._quote_plans):
            return "REFUSED:not_a_maker_quote"
        if order.state not in (OrderState.ACKNOWLEDGED, OrderState.PARTIAL):
            return f"REFUSED:state_{order.state.value.lower()}"            # no second amend while one is pending, none during a cancel, none before the venue acknowledged the order
        if self.router.has_pending_order(order_id) or new_price == order.limit_price:
            return "REFUSED:in_flight_or_same_price"
        if self.risk.kill_switch:
            return "REFUSED:kill_switch"
        status = self.router.route_status.get(order.venue)
        if status is not None and not status.supports_replace:
            self.ledger.record("AMEND_FALLBACK_CANCEL", now_ns, parent_id=plan.parent_id, order_id=order_id, venue=order.venue, new_price=str(new_price), reason="route_does_not_support_replace")
            self.note_cancel_requested(order_id, now_ns)
            self._cancel_working_orders(plan, [order], now_ns)
            return "FALLBACK_CANCEL"
        seq = self._amend_seq.get(order_id, 0) + 1
        remaining = order.quantity - order.filled_quantity
        new = ChildOrder(
            order_id=stable_id("amend", order_id, seq), parent_id=plan.parent_id, opportunity_id=order.opportunity_id, venue=order.venue, instrument_id=order.instrument_id, side=order.side,
            quantity=remaining, limit_price=new_price, order_role=order.order_role, reduce_only=order.reduce_only, order_type=order.order_type, time_in_force=order.time_in_force,
            post_only=order.post_only, intent_id=order.intent_id, replaces_order_id=order_id,
        )
        try:
            self.instruments.validate_order(new, now_ns)
        except ValueError as exc:
            return f"REFUSED:instrument_check:{exc}"
        self._amend_seq[order_id] = seq
        root = self._lineage_of.get(order_id, order_id)
        self._lineage_of[order_id] = root
        self._lineage_of[new.order_id] = root
        order.amend_to_price = new_price
        plan.children.append(new)
        if self.observer is not None:
            self.observer.amend_child(plan, order, new, now_ns / 1e9)
        self.ledger.record("AMEND_REQUESTED", now_ns, parent_id=plan.parent_id, order_id=order_id, replacement_order_id=new.order_id, venue=order.venue, old_price=str(order.limit_price), new_price=str(new_price),
                           remaining=str(remaining), reason=reason)
        self._record_internal_update(plan, order, ExecutionEventType.AMEND_REQUESTED, now_ns, f"amend:{new.order_id}")
        self._record_internal_update(plan, new, ExecutionEventType.SENT, now_ns, "amend-sent")
        request = CanonicalOrderRequest.create(
            action=OrderAction.REPLACE, order_id=new.order_id, target_order_id=order_id, parent_opportunity_id=order.opportunity_id, parent_order_id=plan.parent_id,
            instrument_id=order.instrument_id, venue=order.venue, side=order.side, price=new_price, quantity=order.quantity, order_type=OrderType.LIMIT, time_in_force=TimeInForce.GTC,    # the REPLACE carries the quote's TOTAL
            post_only=order.post_only, reduce_only=order.reduce_only, created_ts_ns=now_ns, idempotency_key=stable_id("amend", order_id, seq),      # quantity (Bybit amend semantics): the venue's new order has total less what the old one filled by then
        )
        callbacks = self.route_order(request)
        self._record_order_action(plan, new, request, "REPLACE", "DISPATCHED", now_ns, target_order_id=order_id)
        self._watch_order(new, now_ns)                                       # no answer in time: the venue is queried (about the replacement, and the old order on the amend timeout), never a second order sent
        for callback in callbacks:
            self._ingest_request_callback(plan, new, request, callback, self._next_execution_ts(now_ns))
        return "REQUESTED"

    def urgent_cancel(self, order_id: str, now_ns: int, reason: str = "urgent") -> bool:
        """Cancel a quote NOW, amend pending or not: every live order of its lineage gets a cancel, the old order and the replacement the venue may already have created (or will, when it processes the amend:
        the replacement is cancelled too, so no order survives the urgent cancel). Returns False when nothing was live."""
        found = next(((plan, x) for plan in self.plans.values() for x in plan.children if x.order_id == order_id), None)
        if found is None:
            return False
        plan, order = found
        root = self._lineage_of.get(order_id, order_id)
        live = [x for x in plan.children if self._lineage_of.get(x.order_id, x.order_id) == root and x.state in LIVE_ORDER_STATES and x.state != OrderState.CANCEL_PENDING]
        if not live:
            return False
        self.ledger.record("QUOTE_URGENT_CANCEL", now_ns, parent_id=plan.parent_id, order_id=order_id, live_members=[x.order_id for x in live], states=[x.state.value for x in live], reason=reason)
        for x in live:
            if x.state != OrderState.CREATED:
                self.note_cancel_requested(x.order_id, now_ns)
        self._cancel_working_orders(plan, live, now_ns)
        return True

    def _amend_bookkeeping_order(self, plan: ParentPlan, order: ChildOrder) -> bool:
        """An order of an amend lineage that is not the quote itself any more: the old order the venue replaced (its fills are booked, it is simply gone), or a replacement that never took anything (rejected or cancelled
        with no fill). The plan's "all primary orders filled" test leaves them out; a replacement or old order that is the live head of the quote counts as before."""
        if order.order_id not in self._lineage_of:
            return False
        if order.state in TERMINAL_ORDER_STATES - {OrderState.TIMED_OUT} and order.state != OrderState.FILLED:
            return any(x is not order and x.order_id in self._lineage_of and self._lineage_of[x.order_id] == self._lineage_of[order.order_id] and
                       (x.state == OrderState.FILLED or x.state not in TERMINAL_ORDER_STATES or x.filled_quantity > 0) for x in plan.children) or order.filled_quantity == 0
        return False

    def _lineage_members(self, plan: ParentPlan, order: ChildOrder) -> list[ChildOrder]:
        root = self._lineage_of.get(order.order_id)
        if root is None:
            return [order]
        return [x for x in plan.children if self._lineage_of.get(x.order_id) == root]

    def _lineage_filled(self, plan: ParentPlan, order: ChildOrder) -> Decimal:
        return sum((x.filled_quantity for x in self._lineage_members(plan, order)), Decimal("0"))

    def lineage_remaining(self, order_id: str) -> Decimal | None:
        """What is still to be filled of the quote the order belongs to: the quote's quantity less the fills of every order of its lineage (a late fill on the old order reduces the replacement's remaining)."""
        for plan in self.plans.values():
            order = next((x for x in plan.children if x.order_id == order_id), None)
            if order is None:
                continue
            root = self._lineage_of.get(order_id)
            total = next((x.quantity for x in plan.children if x.order_id == root), order.quantity) if root else order.quantity
            return total - self._lineage_filled(plan, order)
        return None

    def _amend_react(self, plan: ParentPlan, order: ChildOrder, update: CanonicalOrderUpdate) -> None:
        ts_ns = update.observed_ts_ns
        if order.replaces_order_id:                                            # an update about a REPLACEMENT order
            old = next((x for x in plan.children if x.order_id == order.replaces_order_id), None)
            if old is None:
                return
            if update.state == ExecutionEventType.AMEND_ACCEPTED:
                self.ledger.record("AMEND_ACCEPTED_NOT_CONFIRMED", ts_ns, parent_id=plan.parent_id, order_id=old.order_id, replacement_order_id=order.order_id, effective_price=str(old.limit_price),
                                   requested_price=str(order.limit_price))
            elif update.state in (ExecutionEventType.REPLACED, ExecutionEventType.ACKNOWLEDGED, ExecutionEventType.PARTIAL, ExecutionEventType.FILLED):
                if old.amend_to_price is not None or old.state not in TERMINAL_ORDER_STATES - {OrderState.TIMED_OUT}:
                    self._amend_confirmed(plan, old, order, ts_ns)
            elif update.state == ExecutionEventType.REJECTED and old.amend_to_price is not None:
                self._amend_failed(plan, old, order, ts_ns, update.reason or "amend rejected")
            elif update.state == ExecutionEventType.TIMED_OUT and old.amend_to_price is not None:
                self.ledger.record("AMEND_TIMEOUT", ts_ns, parent_id=plan.parent_id, order_id=old.order_id, replacement_order_id=order.order_id)
                self._watch_order(old, ts_ns, immediate=True, count_unknown=False)       # ask the venue about the old order too: nothing is sent, the amend may still succeed late
        if self._lineage_of.get(order.order_id) is not None and update.cumulative_filled_quantity > 0:
            self._lineage_guard(plan, order, ts_ns)

    def _amend_confirmed(self, plan: ParentPlan, old: ChildOrder, new: ChildOrder, ts_ns: int) -> None:
        new_price = old.amend_to_price if old.amend_to_price is not None else new.limit_price
        self.ledger.record("AMEND_CONFIRMED", ts_ns, parent_id=plan.parent_id, order_id=old.order_id, replacement_order_id=new.order_id, venue=old.venue, old_price=str(old.limit_price), new_price=str(new_price),
                           queue_position="LOST (any price amend is modelled as a loss of queue place: conservative)")
        old.amend_to_price = None
        spec = self._hedge_specs.get(plan.parent_id)
        if spec is not None and spec["maker_order_id"] == old.order_id:        # the head of the quote moves to the replacement BEFORE the old order is closed, so the plan never reads a cancelled maker
            spec["maker_order_id"] = new.order_id
            self._persist(plan, ts_ns, "HEDGE_SPEC", **spec)
        if old.order_id in self._lifetime_deadlines:                          # an amend does not restart the quote's lifetime watchdog
            self._lifetime_deadlines.setdefault(new.order_id, self._lifetime_deadlines.pop(old.order_id))
        if old.state not in TERMINAL_ORDER_STATES - {OrderState.TIMED_OUT}:
            self.ingest_order_update(CanonicalOrderUpdate(
                update_id=stable_id("amend-replaced", old.order_id, new.order_id), source=OrderUpdateSource.ADAPTER_CALLBACK, parent_order_id=plan.parent_id, order_id=old.order_id,
                venue=old.venue, instrument_id=old.instrument_id, state=ExecutionEventType.CANCELLED, event_ts_ns=ts_ns, observed_ts_ns=ts_ns,
                cumulative_filled_quantity=old.filled_quantity, average_price=old.average_price, reason=f"replaced by {new.order_id}",
            ))

    def _amend_failed(self, plan: ParentPlan, old: ChildOrder, new: ChildOrder, ts_ns: int, reason: str) -> None:
        self.ledger.record("AMEND_REJECTED", ts_ns, parent_id=plan.parent_id, order_id=old.order_id, replacement_order_id=new.order_id, venue=old.venue, reason=reason,
                           effective_price=str(old.limit_price), requested_price=str(old.amend_to_price))
        old.amend_to_price = None
        if old.state not in TERMINAL_ORDER_STATES:
            self._watch_order(old, ts_ns, immediate=True, count_unknown=False)      # the old order is queried (it may have filled, or been cancelled, with the report still travelling); no blind second order, no assumption

    def _lineage_guard(self, plan: ParentPlan, order: ChildOrder, ts_ns: int) -> None:
        members = self._lineage_members(plan, order)
        root = self._lineage_of.get(order.order_id)
        total = next((x.quantity for x in plan.children if x.order_id == root), order.quantity)
        filled = sum((x.filled_quantity for x in members), Decimal("0"))
        if filled > total:
            self.ledger.record("AMEND_LINEAGE_OVERFILL", ts_ns, parent_id=plan.parent_id, root_order_id=root, quote_quantity=str(total), lineage_filled=str(filled), alarm=True)
        if filled >= total:
            live = [x for x in members if x.state in LIVE_ORDER_STATES and x.state != OrderState.CANCEL_PENDING and x.state != OrderState.CREATED]
            if live:
                self.ledger.record("AMEND_LINEAGE_COMPLETE_CANCEL", ts_ns, parent_id=plan.parent_id, root_order_id=root, cancelled=[x.order_id for x in live])
                for x in live:
                    self.note_cancel_requested(x.order_id, ts_ns)
                self._cancel_working_orders(plan, live, ts_ns)

    def note_cancel_requested(self, order_id: str, ts_ns: int) -> None:
        """Record that a cancel was sent for `order_id` (the same internal CANCEL_REQUESTED update the cancel path records):
        the order reads CANCEL_PENDING, stays live and can still fill until the venue answers."""
        for plan in self.plans.values():
            for order in plan.children:
                if order.order_id == order_id:
                    self._record_internal_update(
                        plan, order, ExecutionEventType.CANCEL_REQUESTED, ts_ns, f"note:{ts_ns}",
                    )
                    return
        raise KeyError(order_id)

    def ingest_private_stream_update(self, update: CanonicalOrderUpdate) -> ParentPlan:
        if update.source != OrderUpdateSource.PRIVATE_STREAM:
            raise ValueError("private stream update must declare PRIVATE_STREAM source")
        return self.ingest_order_update(update)

    def ingest_execution_callback(
        self, callback: ExecutionCallback, observed_ts_ns: int
    ) -> ParentPlan:
        return self.ingest_order_update(
            CanonicalOrderUpdate.from_callback(callback, observed_ts_ns)
        )

    def mark_order_timed_out(
        self, parent_id: str, order_id: str, now_ns: int, reason: str = "order timeout"
    ) -> ParentPlan:
        plan = self.plans[parent_id]
        order = next(x for x in plan.children if x.order_id == order_id)
        result = self.ingest_order_update(CanonicalOrderUpdate(
            update_id=stable_id("timeout", parent_id, order_id, now_ns),
            source=OrderUpdateSource.TIMER,
            parent_order_id=parent_id, order_id=order_id, venue=order.venue,
            instrument_id=order.instrument_id, state=ExecutionEventType.TIMED_OUT,
            event_ts_ns=now_ns, observed_ts_ns=now_ns, reason=reason,
        ))
        if result.state == PlanState.TIMED_OUT:
            self._transition(
                result, PlanState.HEDGE_UNWIND, now_ns,
                "timeout activates deterministic hedge/unwind policy",
            )
        return result

    def _new_risk_hold(self, plan: ParentPlan, opportunity: Opportunity, now: int) -> ParentPlan | None:
        """The holds on new risk, ONE place for an arb package and a market-making quote (execute, execute_quote): a restart not yet reconciled, an open residual breach, an unknown hedge, an unwind-exhausted
        alarm. Returns the plan closed REJECTED when one holds, None when new risk may be admitted. Records exactly what execute recorded before the move."""
        parent_id = plan.parent_id
        if self.restart_reconcile_gate and (self._restart_unreconciled or self._restart_account_problems):
            self.ledger.record(
                "NEW_RISK_HELD_RESTART_RECONCILIATION", now,
                parent_id=parent_id, opportunity_id=opportunity.opportunity_id, unreconciled_parent_ids=sorted(self._restart_unreconciled),
                account_problems=sorted(self._restart_account_problems),
            )
            return self._close_without_exposure(plan, now, PlanState.REJECTED, "restart_reconciliation_pending")
        if self.residual_policy.hold_new_risk_on_breach and self._residual_breach_since:
            self.realism_counters["new_risk_held_residual_breach"] += 1
            self.ledger.record(
                "NEW_RISK_HELD_RESIDUAL_BREACH", now,
                parent_id=parent_id, opportunity_id=opportunity.opportunity_id,
                breached_parent_ids=sorted(self._residual_breach_since),
            )
            return self._close_without_exposure(
                plan, now, PlanState.REJECTED, "residual_breach_hold"
            )
        unknown_chains = sorted(c.chain_id for group in self.hedge_chains.values() for c in group.values() if c.state in (HedgeState.UNKNOWN, HedgeState.ESCALATED))
        if unknown_chains:
            # unknown block (the backtester's count_unknown_exposure): while a hedge is UNKNOWN (or escalated and waiting on one), the exposure it may or may not have closed is of
            # unknown size, so no new package is admitted until the venue has answered
            self.realism_counters["new_risk_held_hedge_unknown"] = self.realism_counters.get("new_risk_held_hedge_unknown", 0) + 1
            self.ledger.record("NEW_RISK_HELD_HEDGE_UNKNOWN", now, parent_id=parent_id, opportunity_id=opportunity.opportunity_id, chain_ids=unknown_chains)
            return self._close_without_exposure(plan, now, PlanState.REJECTED, "hedge_unknown_block")
        if self.unwind_exhausted_alarms:
            self.realism_counters["new_risk_held_unwind_exhausted"] = self.realism_counters.get("new_risk_held_unwind_exhausted", 0) + 1
            self.ledger.record(
                "NEW_RISK_HELD_UNWIND_EXHAUSTED", now,
                parent_id=parent_id, opportunity_id=opportunity.opportunity_id,
                alarmed_parent_ids=sorted(self.unwind_exhausted_alarms),
            )
            return self._close_without_exposure(
                plan, now, PlanState.REJECTED, "unwind_exhausted_alarm"
            )
        return None

    def execute(
        self,
        opportunity: Opportunity,
        books: dict[tuple[str, str], OrderBook],
        dispatch_ts_ns: int | None = None,
        maker_venue: str | None = None,
        *,
        quote_side: Side | None = None,
        exit_cap: Decimal | None = None,
    ) -> ParentPlan:
        """Admit and dispatch one arb package. `quote_side` (owner 13 Nov, CR-MQ part 2; only through `execute_quote`) makes it a market-making QUOTE: one post-only GTC child on that side, no hedge spec, no
        eligibility alignment (there is one venue), one-leg risk reservation. None (every arb package) is the unchanged path."""
        pairing = None
        if self.observer is not None:
            pairing = self.observer.pairing()
        if (pairing == "none") != (quote_side is not None):                  # a quote runs only under pairing none, an arb package never does: one OMS is one or the other
            raise ValueError("a market-making quote needs the execution layer with pairing none, and an arb package is never sent under pairing none")
        self._books.update(books)
        parent_id = stable_id("parent", opportunity.opportunity_id)
        if parent_id in self.plans:
            return self.plans[parent_id]
        if self.order_owner_guard is not None:
            self.order_owner_guard()
        if self.new_risk_guard is not None:
            self.new_risk_guard()
        children = [
            ChildOrder(
                order_id=stable_id("child", parent_id, opportunity.buy_venue, "BUY"),
                parent_id=parent_id,
                opportunity_id=opportunity.opportunity_id,
                venue=opportunity.buy_venue,
                instrument_id=opportunity.instrument_id,
                side=Side.BUY,
                quantity=opportunity.quantity,
                limit_price=opportunity.buy_price,
            ),
            ChildOrder(
                order_id=stable_id("child", parent_id, opportunity.sell_venue, "SELL"),
                parent_id=parent_id,
                opportunity_id=opportunity.opportunity_id,
                venue=opportunity.sell_venue,
                instrument_id=opportunity.instrument_id,
                side=Side.SELL,
                quantity=opportunity.quantity,
                limit_price=opportunity.sell_price,
            ),
        ]
        fill_triggered = self.dispatch_policy == DispatchPolicy.FILL_TRIGGERED and quote_side is None
        hedge_proto = None
        if quote_side is not None:
            children = [c for c in children if c.side == quote_side]
            if exit_cap is None:
                children[0].post_only, children[0].time_in_force = True, "GTC"
            else:                                                            # an exit IOC for inventory (CR-MQ part 3): a reduce-only UNWIND child, IOC, priced at the strategy's cap (its worst price)
                children[0].order_role, children[0].reduce_only, children[0].time_in_force, children[0].post_only = "UNWIND", True, "IOC", False
            plan = ParentPlan(parent_id, opportunity, children)
            self._quote_plans.add(parent_id)
        elif fill_triggered:
            if maker_venue not in (opportunity.buy_venue, opportunity.sell_venue):
                raise ValueError("FILL_TRIGGERED needs maker_venue to be one of the opportunity's venues")
            maker_child = next(c for c in children if c.venue == maker_venue)
            hedge_proto = next(c for c in children if c is not maker_child)
            maker_child.post_only, maker_child.time_in_force = True, "GTC"       # a resting post-only order: priced as maker, and a quiet GTC order is resting, not unknown
            plan = ParentPlan(parent_id, opportunity, [maker_child])
        else:
            plan = ParentPlan(parent_id, opportunity, children)
        self.plans[parent_id] = plan
        now = opportunity.observed_ts_ns
        self._transition(plan, PlanState.CREATED, now, event_type="PLAN_CREATED")
        if quote_side is not None:
            self._persist(plan, now, "QUOTE_PLAN", side=quote_side.value)         # the marker survives a restart (_recover_hedge_state rebuilds it)
        held = self._new_risk_hold(plan, opportunity, now) if exit_cap is None else None      # an exit reduces risk: no hold on new risk applies to it
        if held is not None:
            return held
        if self.eligibility_policy is not None and quote_side is None:      # a quote has one venue: the two-book alignment of a package does not apply (the book gate still blocks quoting on an invalid book)
            decision = self.eligibility_policy.evaluate(books, now)
            current_sources = (
                books[(opportunity.buy_venue, opportunity.instrument_id)].last_event_id or "",
                books[(opportunity.sell_venue, opportunity.instrument_id)].last_event_id or "",
            )
            route_reasons = list(decision.reasons)
            if current_sources != opportunity.source_event_ids:
                route_reasons.append("source_market_state_changed")
            if opportunity.eligibility_fingerprint != decision.fingerprint:
                route_reasons.append("eligibility_fingerprint_changed")
            self.ledger.record(
                "ROUTE_ELIGIBILITY",
                now,
                opportunity_id=opportunity.opportunity_id,
                eligible=not route_reasons,
                fingerprint=decision.fingerprint,
                reasons=route_reasons,
                source_event_ids=current_sources,
            )
            if route_reasons:
                self.alignment_rejections += 1
                return self._close_without_exposure(
                    plan, now, PlanState.REJECTED, ";".join(route_reasons)
                )
        pretrade = self.risk.validate_pretrade(
            opportunity=opportunity,
            orders=tuple(children),
            books=books,
            instruments=self.instruments,
            route_status=self.router.route_status,
            now_ns=now,
            plans=self.plans,
            ledger=self.ledger,
        )
        self.ledger.record(
            "PRE_TRADE_VALIDATION", now,
            parent_id=plan.parent_id,
            opportunity_id=opportunity.opportunity_id,
            decision=pretrade.decision.value,
            validation=pretrade.to_dict(),
        )
        self._persist(
            plan, now, "PRE_TRADE_VALIDATION",
            decision=pretrade.decision.value,
            validation=pretrade.to_dict(),
        )
        if pretrade.decision == PreTradeDecision.REJECTED:
            reason = ";".join(dict.fromkeys(
                check.code for check in pretrade.failures
            ))
            self.ledger.record(
                "PRE_TRADE_REJECTED", now,
                parent_id=plan.parent_id,
                opportunity_id=opportunity.opportunity_id,
                validation_id=pretrade.validation_id,
                failure_codes=[check.code for check in pretrade.failures],
                dispatched_children=0,
            )
            return self._close_without_exposure(
                plan, now, PlanState.REJECTED, reason
            )
        if self.observer is not None:
            refusal = self.observer.admit(plan, now / 1e9)         # E0 step (c), gate 1 of 2: the layer's base ceiling, checked before anything is reserved or written
            if refusal:
                self.ledger.record(
                    "LAYER_RISK_REFUSED", now, parent_id=parent_id, opportunity_id=opportunity.opportunity_id,
                    reason=refusal, dispatched_children=0,
                )
                return self._close_without_exposure(plan, now, PlanState.REJECTED, refusal)
        try:
            for order in children:
                self.instruments.validate_order(order, now)
            self.risk.carried_residual_base = self.carried_residual_base()
            reserved = self.risk.reserve(opportunity, None if quote_side is None else quote_side.value.lower())
        except ValueError as exc:
            self.ledger.record(
                "PACKAGE_RISK_REJECTED",
                now,
                parent_id=parent_id,
                opportunity_id=opportunity.opportunity_id,
                reason=str(exc),
                dispatched_children=0,
            )
            return self._close_without_exposure(
                plan, now, PlanState.REJECTED, str(exc)
            )
        self.ledger.record(
            "PACKAGE_RISK_RESERVED",
            now,
            parent_id=parent_id,
            opportunity_id=opportunity.opportunity_id,
            reservation=reserved.to_dict(),
        )
        self._persist(
            plan,
            now,
            "PACKAGE_RISK_RESERVED",
            reservation=reserved.to_dict(),
        )
        self._transition(plan, PlanState.RISK_RESERVED, now)
        if self.observer is not None:
            late_refusal = self.observer.plan_opened(plan, now / 1e9)
            if late_refusal:                                         # gate 2 reserved, then the layer refused: roll back, the plan closes REJECTED and _release_and_close returns the OMS reservation
                self.ledger.record(
                    "LAYER_RISK_REFUSED", now, parent_id=parent_id, opportunity_id=opportunity.opportunity_id,
                    reason=late_refusal, dispatched_children=0, after_reserve=True,
                )
                return self._close_without_exposure(plan, now, PlanState.REJECTED, late_refusal)
        self._transition(
            plan, PlanState.DISPATCHED, now, event_type="DISPATCH_INTENT"
        )
        dispatch_base = now if dispatch_ts_ns is None else max(now, dispatch_ts_ns)
        package_dispatch_ts = self._next_execution_ts(dispatch_base)
        package_throttled = False
        self.ledger.record(
            "PACKAGE_DISPATCH_STARTED",
            package_dispatch_ts,
            parent_id=parent_id,
            policy=self.dispatch_policy.value,
            child_count=len(children),
        )
        self._persist(
            plan,
            package_dispatch_ts,
            "PACKAGE_DISPATCH_STARTED",
            policy=self.dispatch_policy.value,
            child_count=len(children),
        )
        if fill_triggered:
            self._hedge_register(plan, hedge_proto, plan.children[0], opportunity, package_dispatch_ts)
        for leg_index, order in enumerate(list(plan.children)):
            if self._dispatch_leg(plan, order, leg_index, opportunity, books, dispatch_base, now, maker_leg=fill_triggered or (quote_side is not None and exit_cap is None)):
                package_throttled = True

        package_completed_ts = self._next_execution_ts(now)
        self.ledger.record(
            "PACKAGE_DISPATCH_COMPLETED",
            package_completed_ts,
            parent_id=parent_id,
            policy=self.dispatch_policy.value,
            child_count=len(children),
        )
        self._persist(
            plan,
            package_completed_ts,
            "PACKAGE_DISPATCH_COMPLETED",
            policy=self.dispatch_policy.value,
            child_count=len(children),
        )

        if package_throttled:
            self.ledger.record(
                "PACKAGE_DISPATCH_THROTTLED", package_completed_ts,
                parent_id=parent_id,
                pending_rate_limits=self.router.rate_limit_snapshot(),
            )
            self._persist(
                plan, package_completed_ts, "PACKAGE_DISPATCH_THROTTLED",
                pending_rate_limits=self.router.rate_limit_snapshot(),
            )
            return plan

        if fill_triggered:
            self._fill_triggered_settle(plan, self._hedge_specs[parent_id], package_completed_ts)
            return plan
        if quote_side is not None:
            return plan                              # the quote rests; it closes through _finalize_quote when its order has ended
        return self._finalize_plan(plan, package_completed_ts, books)

    def execute_quote(
        self, *, quote_id: str, strategy_id: str, instrument_id: str, venue: str, side: Side, price: Decimal, quantity: Decimal,
        books: dict[tuple[str, str], OrderBook], observed_ts_ns: int, dispatch_ts_ns: int | None = None,
    ) -> ParentPlan:
        """A market-making quote (owner 13 Nov, CR-MQ part 2): ONE post-only GTC child through the existing plan model, with no hedge spec, no hedge and no unwind. It needs the execution layer
        with execution.pairing none (the per-pair inventory cap is its admission gate beside the notional / margin reservation; an arb profile can never have that pairing). Fills book to inventory once;
        the quote is amended through request_amend (REPLACE, AMEND_PENDING) and cancelled like any resting order; the plan closes when its order has ended. The Opportunity the plan carries is the
        quote's record (one venue, the quote's price on both sides, edge fields zero: no edge is computed for a quote)."""
        book = books.get((venue, instrument_id))
        source = (book.last_event_id or "") if book is not None else ""
        opportunity = Opportunity(
            opportunity_id=quote_id, strategy_id=strategy_id, instrument_id=instrument_id, observed_ts_ns=observed_ts_ns, buy_venue=venue, sell_venue=venue,
            buy_price=price, sell_price=price, quantity=quantity, gross_edge_bps=Decimal("0"), estimated_net_edge_bps=Decimal("0"), source_event_ids=(source, source),
        )
        return self.execute(opportunity, books, dispatch_ts_ns, None, quote_side=side)

    def execute_exit(
        self, *, exit_id: str, strategy_id: str, instrument_id: str, venue: str, side: Side, cap_price: Decimal, quantity: Decimal,
        books: dict[tuple[str, str], OrderBook], observed_ts_ns: int, dispatch_ts_ns: int | None = None,
    ) -> ParentPlan:
        """An exit IOC for market-making inventory (owner 13 Nov, CR-MQ part 3): one reduce-only UNWIND child, IOC, limited at `cap_price` (the strategy's cap is the worst price, MARKET_BANDED in the layer's
        vocabulary), through the same plan model and the same pairing-none layer as a quote. It reduces risk, so no hold on new risk applies, and the plan counts as a quote-book plan (its fills are inventory moves,
        never an arb residual)."""
        book = books.get((venue, instrument_id))
        source = (book.last_event_id or "") if book is not None else ""
        opportunity = Opportunity(
            opportunity_id=exit_id, strategy_id=strategy_id, instrument_id=instrument_id, observed_ts_ns=observed_ts_ns, buy_venue=venue, sell_venue=venue,
            buy_price=cap_price, sell_price=cap_price, quantity=quantity, gross_edge_bps=Decimal("0"), estimated_net_edge_bps=Decimal("0"), source_event_ids=(source, source),
        )
        return self.execute(opportunity, books, dispatch_ts_ns, None, quote_side=side, exit_cap=cap_price)

    def _dispatch_leg(
        self, plan: ParentPlan, order: ChildOrder, leg_index: int, opportunity: Opportunity,
        books: dict[tuple[str, str], OrderBook], dispatch_base: int, now: int, *, maker_leg: bool = False,
    ) -> bool:
        """Persist, route and book the callbacks of one child (the body of the package loop in execute, shared with the FILL_TRIGGERED hedge child).
        Returns True when the venue throttled, delayed or left the order in flight. A maker leg is a post-only GTC limit; every other leg is a taker IOC limit."""
        parent_id = plan.parent_id
        package_throttled = False
        dispatch_started_ts = self._next_execution_ts(dispatch_base)
        venue = order.venue
        if self.observer is not None:
            venue = self.observer.route_venue(plan, order, maker_leg, dispatch_started_ts / 1e9)        # E0 step (d): the layer decides the venue; the router below only executes it
            if venue != order.venue and self.route_mode == "enforce":
                self.ledger.record("ROUTE_DECISION_MISMATCH", dispatch_started_ts, parent_id=parent_id, order_id=order.order_id, order_venue=order.venue, decided_venue=venue)
                self.ingest_order_update(CanonicalOrderUpdate(
                    update_id=stable_id("route-mismatch", order.order_id), source=OrderUpdateSource.INTERNAL, parent_order_id=parent_id, order_id=order.order_id,
                    venue=order.venue, instrument_id=order.instrument_id, state=ExecutionEventType.REJECTED, event_ts_ns=dispatch_started_ts,
                    observed_ts_ns=dispatch_started_ts, reason=f"layer_route_mismatch:{venue}"))
                return False
            venue = order.venue          # count mode: the order keeps its own venue (and the mismatch is counted); enforce already returned
        self.ledger.record(
            "CHILD_DISPATCH_STARTED",
            dispatch_started_ts,
            parent_id=parent_id,
            order_id=order.order_id,
            venue=order.venue,
            leg_index=leg_index,
            policy=self.dispatch_policy.value,
        )
        self._persist(
            plan,
            dispatch_started_ts,
            "CHILD_DISPATCH_STARTED",
            order_id=order.order_id,
            venue=order.venue,
            leg_index=leg_index,
        )
        self._recompute_plan_economics(plan, dispatch_started_ts)
        risk_reducing = self._order_reduces_residual(plan, order) or (order.order_role == "UNWIND" and plan.parent_id in self._quote_plans)      # a quote-book exit IOC reduces inventory: allowed under a stop
        instruction = RoutingInstruction(
            strategy_id=opportunity.strategy_id,
            eligible_venues=(venue,),
            route_priority=(venue,),
            order_id=order.order_id,
            parent_opportunity_id=order.opportunity_id,
            parent_order_id=order.parent_id,
            instrument_id=order.instrument_id,
            side=order.side,
            price=order.limit_price,
            quantity=order.quantity,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC if maker_leg else TimeInForce.IOC,
            maker_taker_intent=MakerTakerIntent.MAKER if maker_leg else MakerTakerIntent.TAKER,
            post_only=maker_leg,
            reduce_only=order.reduce_only,
            created_ts_ns=dispatch_started_ts,
            idempotency_key=stable_id("submit", order.order_id),
            risk_reducing=risk_reducing,
        )
        try:
            routing = self.route_strategy_order(instruction, books)
        except Exception as exc:
            # The adapter raised after the order may already have been handed to the venue, so the order is
            # neither accepted nor rejected as far as we know. Keep the reservation, never resend, and put the
            # order under the normal report-deadline recovery (venue query -> time out -> kill switch).
            self._note_ambiguous_dispatch(plan, order, instruction.idempotency_key, "SUBMIT", exc, dispatch_started_ts)
            raise
        if routing.selected_venue is not None and routing.selected_venue != venue:       # E0 step (d) condition 2: the router never sends anywhere but the layer's decision
            self.ledger.record("ROUTE_VENUE_MISMATCH", dispatch_started_ts, parent_id=parent_id, order_id=order.order_id, decided_venue=venue, routed_venue=routing.selected_venue)
            raise RuntimeError(f"the router selected {routing.selected_venue} but the layer decided {venue}")
        if risk_reducing:
            self._record_legging_control_override(
                plan, order, routing, dispatch_started_ts,
                global_stop_override=self.risk.kill_switch,
            )
        if routing.request is not None:
            self._record_order_action(
                plan, order, routing.request, "SUBMIT",
                (
                    routing.throttle_action.value
                    if routing.throttle_action in {
                        ThrottleAction.QUEUE, ThrottleAction.DELAY
                    } else "DISPATCHED"
                ),
                dispatch_started_ts,
            )
        if routing.throttle_action == ThrottleAction.IN_FLIGHT:
            package_throttled = True
            self.ledger.record(
                "CHILD_DISPATCH_IN_FLIGHT", dispatch_started_ts,
                parent_id=parent_id, order_id=order.order_id,
                venue=order.venue, leg_index=leg_index,
                arrival_ts_ns=routing.release_ts_ns,
            )
        if routing.throttle_action in {
            ThrottleAction.QUEUE, ThrottleAction.DELAY
        }:
            package_throttled = True
            throttle_payload = {
                "parent_id": parent_id, "order_id": order.order_id,
                "venue": order.venue, "leg_index": leg_index,
                "action": routing.throttle_action.value,
                "release_ts_ns": routing.release_ts_ns,
                "rate_limit": self.router.rate_limit_snapshot(),
            }
            self.ledger.record(
                "CHILD_DISPATCH_THROTTLED", dispatch_started_ts,
                **throttle_payload,
            )
            self._persist(
                plan, dispatch_started_ts, "CHILD_DISPATCH_THROTTLED",
                **throttle_payload,
            )
        callback_timestamps = []
        for callback_index, callback in enumerate(routing.callbacks):
            callback_ts = self._next_execution_ts(now)
            callback_timestamps.append(callback_ts)
            callback_payload = {
                "parent_id": parent_id,
                "order_id": order.order_id,
                "venue": order.venue,
                "leg_index": leg_index,
                "callback_index": callback_index,
                "callback_id": callback.callback_id,
                "callback_type": callback.event_type.value,
                "adapter_event_ts_ns": callback.event_ts_ns,
                "local_callback_ts_ns": callback_ts,
            }
            self.ledger.record(
                "CHILD_CALLBACK_OBSERVED", callback_ts, **callback_payload
            )
            self._persist(
                plan, callback_ts, "CHILD_CALLBACK_OBSERVED", **callback_payload
            )
            self.ingest_execution_callback(callback, callback_ts)
        dispatch_completed_ts = self._next_execution_ts(now)
        if routing.selected_venue is None:
            rejection_reason = ";".join(
                reason for check in routing.checks for reason in check.reasons
            ) or "no eligible route"
            # E0 step (d) condition 1: a failed route check goes back to the layer, never to another venue
            if self.observer is not None:
                self.observer.route_rejected(plan, order, rejection_reason, dispatch_completed_ts / 1e9)
            self.ingest_order_update(CanonicalOrderUpdate(
                update_id=stable_id("route-rejection", order.order_id),
                source=OrderUpdateSource.INTERNAL,
                parent_order_id=parent_id, order_id=order.order_id,
                venue=order.venue, instrument_id=order.instrument_id,
                state=ExecutionEventType.REJECTED,
                event_ts_ns=dispatch_completed_ts,
                observed_ts_ns=dispatch_completed_ts,
                reason=rejection_reason,
            ))
        measurement = ChildDispatchMeasurement(
            parent_order_id=parent_id,
            order_id=order.order_id,
            venue=order.venue,
            leg_index=leg_index,
            dispatch_started_ts_ns=dispatch_started_ts,
            dispatch_completed_ts_ns=dispatch_completed_ts,
            dispatch_duration_ns=dispatch_completed_ts - dispatch_started_ts,
            callback_count=len(callback_timestamps),
            first_callback_observed_ts_ns=(
                callback_timestamps[0] if callback_timestamps else None
            ),
            last_callback_observed_ts_ns=(
                callback_timestamps[-1] if callback_timestamps else None
            ),
        )
        self.dispatch_measurements.append(measurement)
        self.ledger.record(
            "CHILD_DISPATCH_COMPLETED",
            dispatch_completed_ts,
            **measurement.to_dict(),
        )
        self._persist(
            plan,
            dispatch_completed_ts,
            "CHILD_DISPATCH_COMPLETED",
            measurement=measurement.to_dict(),
            order_state=order.state.value,
            filled_quantity=order.filled_quantity,
        )
        if order.filled_quantity:
            self._price_fills(order, dispatch_completed_ts)
            fill_ts = callback_timestamps[-1] if callback_timestamps else dispatch_completed_ts
            self.ledger.apply_fill(order, fill_ts)
            self._update_exposure_control(fill_ts)
        self._assess_legging_risk(plan, dispatch_completed_ts, books)
        return package_throttled

    # ------------------------------------------------------------------------------------------------------------------
    # FILL_TRIGGERED (Phase C, matrix 4B): the hedge of a maker fill. The rules live here; a simulated or live venue only produces the events.
    #  * the package (maker + hedge, worst case) is reserved atomically at execute(); the hedge child does not exist until a maker fill
    #  * every maker fill delta opens its OWN hedge chain (one hedge intent per fill, as the backtester does; one live child per chain); the reservation bounds the
    #    sum at the maker size. A second partial fill while a hedge is in flight is NOT queued or netted: it is hedged by its own chain.
    #  * the hedge child id is the idempotent client order id (stable_id of parent, chain and attempt); it is recorded before the send (OMS03)
    #  * a hedge dispatched on a book that is not VALID, or missing or stale, is HELD (the chain waits in HEDGE_PENDING, re-checked on every timer and book update)
    #    until the book is valid or the residual age limit escalates the chain; it is never sent blind and never dropped
    #  * a reject goes through the venue code table: a retryable one is retried within the budget, anything else goes to the unwind path
    #  * no answer: the venue is queried (check_order_deadlines) and nothing is resent until the query says the order is not there
    # ------------------------------------------------------------------------------------------------------------------
    _CHAIN_OPEN = (HedgeState.MAKER_FILLED, HedgeState.HEDGE_PENDING, HedgeState.SUBMITTED, HedgeState.PARTIALLY_FILLED, HedgeState.REJECTED,
                   HedgeState.RETRYING, HedgeState.UNKNOWN)

    def _recover_hedge_state(self) -> None:
        """After a restart: rebuild the declared hedge leg and every hedge chain of the recovered plans from the plan store (the OMS persisted both before each send)."""
        if self.plan_store is None:
            return
        for parent_id in self.plans:
            spec = None
            chains: dict[str, dict[str, Any]] = {}
            for ev in self.plan_store.events(parent_id):
                if ev["event_type"] == "QUOTE_PLAN":
                    self._quote_plans.add(parent_id)
                elif ev["event_type"] == "FLATTEN_SHADOW":
                    self._flatten_shadow.add(parent_id)
                elif ev["event_type"] == "HEDGE_SPEC":
                    spec = ev["payload"]
                elif ev["event_type"] == "HEDGE_CHAIN":
                    pl = ev["payload"]
                    chains[pl["chain"]["chain_id"]] = pl
                    if spec is not None:
                        spec["requested_qty"], spec["chain_seq"] = pl["requested_qty"], pl["chain_seq"]
            if spec is None:
                continue
            if self.dust_on:
                for ev in self.plan_store.events(parent_id):
                    if ev["event_type"] == "DUST_ADJUST" and "requested_qty" in ev["payload"]:
                        spec["requested_qty"] = ev["payload"]["requested_qty"]          # the quantity handed to a hedge or to the balance (a carried chunk opens no chain, so no HEDGE_CHAIN carries it)
            self._hedge_specs[parent_id] = dict(spec, side=Side(spec["side"]), limit_price=Decimal(str(spec["limit_price"])), max_qty=Decimal(str(spec["max_qty"])),
                                                requested_qty=Decimal(str(spec["requested_qty"])), chain_seq=int(spec["chain_seq"]))
            for chain_id, pl in chains.items():
                chain = HedgeLifecycle.restore(pl["chain"])
                self.hedge_chains.setdefault(parent_id, {})[chain_id] = chain
                self._chain_children[chain_id] = list(pl["children"])
                for oid in pl["children"]:
                    self._chain_of_child[oid] = chain
                if chain.state is HedgeState.RETRYING:
                    self._hedge_retry_due[chain_id] = 0                 # due now: the next timer sends it (the retry was decided before the stop)
        if self.dust_on:
            self._recover_dust_state()

    # Reference values of the frozen backtester profile (perp_perp_maker_taker_v1_2) for the settings the core now has ONE value for. Not read by any rule: they are printed beside
    # the core's value so a run record shows both. The backtester keeps its own hedging until Phase D.
    _BACKTESTER_REFERENCE = {"unwind_price_slippage_bps": "15", "max_unwind_attempts": 3, "max_residual_age_s": 5.0, "residual_rule": "breaker net_exposure_s 5.0 (trips at once)",
                             "retry_limit": 2, "retry_backoff_s": 0.25}

    def decision_pending_settings(self) -> dict[str, dict[str, object]]:
        """The settings the owner left as DECISION_PENDING_CALIBRATION, one value each (the core's), with the backtester's value beside it. No production value is invented."""
        rp, hp, rl = self.residual_policy, self.hedge_policy, self.release_policy
        ref, label = self._BACKTESTER_REFERENCE, "DECISION_PENDING_CALIBRATION"
        cfg = rp.decision_status                       # the labels as the config carries them (a release artifact, so part of the release identity); empty when built in code
        return {
            "unwind_price_slippage_bps": {"core": str(rp.unwind_price_slippage_bps), "backtester_v1_2": ref["unwind_price_slippage_bps"], "label": label,
                                          "config_label": cfg.get("unwind_price_slippage_bps"), "note": "owner 8 Nov 2026: 15 bps in the core and the backtester (the core was 10)"},
            "max_unwind_attempts": {"core": rp.max_unwind_attempts, "backtester_v1_2": ref["max_unwind_attempts"], "label": label, "config_label": cfg.get("max_unwind_attempts"),
                                    "note": "owner 8 Nov 2026: 3. The core's code default has been 3 since 2 Nov; the core config copies said 2 until 8 Nov, so this changes live behaviour (one more unwind attempt before the alarm)"},
            "max_residual_age_ns": {"core": rp.max_residual_age_ns, "backtester_v1_2": ref["residual_rule"], "label": label, "config_label": cfg.get("max_residual_age_ms"),
                                    "action": "stage 1 of the core's two stages: hold new risk (owner 8 Nov 2026)"},
            "kill_after_ns": {"core": rp.kill_after_ns, "label": label, "config_label": cfg.get("kill_after_ms"), "action": "stage 2: kill switch if the breach is still open (owner 8 Nov 2026)"},
            "retry_limit": {"core": hp.retry_limit, "backtester_v1_2": ref["retry_limit"], "label": label, "note": "0 = retry off until a caller sets it"},
            "retry_backoff_ns": {"core": hp.retry_backoff_ns, "backtester_v1_2": ref["retry_backoff_s"], "label": label},
            "max_book_age_ns": {"core": hp.max_book_age_ns, "label": label, "note": "None = the book's own state decides"},
            "max_queue_age_ns": {"core": rl.max_queue_age_ns, "label": label, "note": "None = no age limit"},
            "unknown_query_budget": {"core": self.order_timeout_policy.max_queries, "label": label, "action": "H16, accepted by the owner 8 Nov 2026: chain FAILED (alarm) AND kill switch"},
        }

    def hedge_report(self) -> dict[str, object]:
        """Per event and per chain, what the specification asks for: retry intervals, reject reasons and labels, timeouts, partial fills, the recovery outcome and the residual-exposure
        duration. p_fail and loss_fail_bps are SCENARIO values (not calibrated): failure costs are reported per scenario by the caller, never as a measured rate."""
        chains = [dict(c.as_record(), parent_id=pid) for pid, group in self.hedge_chains.items() for c in group.values()]
        return {"settings": self.decision_pending_settings(), "chains": chains, "rejects": list(self.reject_records), "expired_at_release": list(self.expired_at_release),
                "counters": {k: v for k, v in self.realism_counters.items() if k.startswith(("hedge_", "reject_", "expired_"))},
                "p_fail_and_loss_fail_bps": "SCENARIO (not calibrated): no execution logs exist"}

    def _hedge_register(self, plan: ParentPlan, proto: ChildOrder, maker: ChildOrder, opportunity: Opportunity, ts_ns: int) -> None:
        self._hedge_specs[plan.parent_id] = {
            "venue": proto.venue, "side": proto.side, "instrument_id": proto.instrument_id, "limit_price": proto.limit_price,
            "max_qty": opportunity.quantity, "requested_qty": Decimal("0"), "maker_order_id": maker.order_id, "chain_seq": 0,
            "opportunity_id": opportunity.opportunity_id,
        }
        self._persist(plan, ts_ns, "HEDGE_SPEC", **self._hedge_specs[plan.parent_id])

    def _fill_triggered_settled(self, plan: ParentPlan) -> bool:
        spec = self._hedge_specs[plan.parent_id]
        chains = list(self.hedge_chains.get(plan.parent_id, {}).values())
        if any(c.state in (HedgeState.UNWINDING, HedgeState.ESCALATED, HedgeState.FAILED) for c in chains):
            return True
        maker = next((o for o in plan.children if o.order_id == spec["maker_order_id"]), None)
        maker_done = bool(spec.get("dust_host")) or (maker is not None and maker.state in TERMINAL_ORDER_STATES and maker.state != OrderState.TIMED_OUT)      # a dust host has no maker: its chain is all there is
        return maker_done and not any(c.state in self._CHAIN_OPEN for c in chains)

    def _hedge_move(self, plan: ParentPlan, chain: HedgeLifecycle, to: HedgeState, ts_ns: int, reason: str, **kw: Any) -> None:
        frm = chain.state
        chain.move(to, ts_ns / 1e9, reason, **kw)
        if to is not HedgeState.RETRYING:
            self._hedge_retry_due.pop(chain.chain_id, None)      # a retry that was waiting is cancelled by whatever ended the wait (a late success included): no stale timer, no second hedge
        if self.observer is not None and to in (HedgeState.ESCALATED, HedgeState.UNWINDING):
            self.observer.chain_escalated(chain.chain_id, ts_ns / 1e9, reason or to.value)
        self.ledger.record("HEDGE_STATE", ts_ns, parent_id=plan.parent_id, chain_id=chain.chain_id, from_state=frm.value, to_state=to.value, reason=reason,
                           hedged_qty=chain.hedged_qty, residual_qty=chain.residual_qty, retries=chain.retries)
        self._persist(plan, ts_ns, "HEDGE_CHAIN", chain=chain.snapshot(), children=list(self._chain_children.get(chain.chain_id, [])),
                      requested_qty=self._hedge_specs[plan.parent_id]["requested_qty"], chain_seq=self._hedge_specs[plan.parent_id]["chain_seq"])
        if chain.state in (HedgeState.RECONCILED, HedgeState.FAILED):
            self.hedge_records.append(dict(chain.as_record(), parent_id=plan.parent_id))

    def _fill_triggered_react(self, plan: ParentPlan, order: ChildOrder, ts_ns: int) -> None:
        spec = self._hedge_specs[plan.parent_id]
        lineage = self._lineage_of.get(order.order_id)
        if order.order_id == spec["maker_order_id"] or (lineage is not None and lineage == self._lineage_of.get(spec["maker_order_id"])):
            self._hedge_for_maker_fill(plan, spec, order, ts_ns)
        else:
            chain = self._chain_of_child.get(order.order_id)
            if chain is not None:
                self._hedge_child_changed(plan, spec, chain, order, ts_ns)
        self._fill_triggered_settle(plan, spec, ts_ns)

    def _hedge_for_maker_fill(self, plan: ParentPlan, spec: dict[str, Any], maker: ChildOrder, ts_ns: int) -> None:
        if self.dust_on:
            self._hedge_for_maker_fill_dust(plan, spec, maker, ts_ns)
            return
        filled = min(self._lineage_filled(plan, maker), spec["max_qty"])      # an amended quote: the fills of every order of its lineage (the old order's late fill included), once each
        # Lot rule (owner 11 Nov): the hedge is a whole number of the hedge venue's lots. The cumulative target is floored to the lot, so a part-lot
        # residual is carried to the next maker fill (never lost) and is on record while it is open; a quantity already on the lot is unchanged.
        target = self.instruments.round_quantity(spec["venue"], spec["instrument_id"], filled, ts_ns)
        residual = filled - target
        if residual != spec.get("lot_residual", Decimal("0")):
            spec["lot_residual"] = residual
            if residual > 0:
                self.realism_counters["hedge_lot_residual"] = self.realism_counters.get("hedge_lot_residual", 0) + 1
                lot = self.instruments.get(spec["venue"], spec["instrument_id"], ts_ns).quantity_step
                self.ledger.record("HEDGE_LOT_RESIDUAL", ts_ns, parent_id=plan.parent_id, maker_order_id=maker.order_id, venue=spec["venue"], maker_filled=str(maker.filled_quantity),
                                   hedged_target=str(target), residual=str(residual), lot=str(lot))
                total = abs(self.carried_residual_base(exclude=plan)) + residual         # what closed plans carry plus this plan's open part-lot
                if lot > 0 and total >= lot and self.lot_residual_alarm is None:
                    self.lot_residual_alarm = {"ts_ns": ts_ns, "venue": spec["venue"], "total_residual_base": str(total), "lot": str(lot), "parent_id": plan.parent_id}
                    self.ledger.record("HEDGE_LOT_RESIDUAL_ALARM", ts_ns, persistent=True, **{k: v for k, v in self.lot_residual_alarm.items() if k != "ts_ns"})
        delta = target - spec["requested_qty"]
        if delta <= 0:
            return
        spec["requested_qty"] += delta
        self._hedge_open_chain(plan, spec, maker, delta, ts_ns)

    def _hedge_open_chain(self, plan: ParentPlan, spec: dict[str, Any], maker: ChildOrder | None, delta: Decimal, ts_ns: int, dust_included: Decimal | None = None) -> None:
        """Open one hedge chain for `delta` and try to send it. `dust_included` (dust carry only) is the signed carried exposure taken into this hedge; `maker` is None for the hedge of the carried balance alone (the dust host plan)."""
        seq = spec["chain_seq"]
        spec["chain_seq"] = seq + 1
        chain = HedgeLifecycle(
            chain_id=stable_id("hedge-chain", plan.parent_id, seq), exposure_qty=float(delta), retry_limit=self.hedge_policy.retry_limit,
            unwind_attempt_limit=max(1, self.residual_policy.max_unwind_attempts), max_residual_age_s=self.residual_policy.max_residual_age_ns / 1e9,
            query_attempt_limit=self.order_timeout_policy.max_queries, opened_ts=ts_ns / 1e9)
        self.hedge_chains.setdefault(plan.parent_id, {})[chain.chain_id] = chain
        self._chain_children[chain.chain_id] = []
        extra = {"dust_included": str(dust_included)} if dust_included else {}
        self.ledger.record("HEDGE_CHAIN_OPENED", ts_ns, parent_id=plan.parent_id, chain_id=chain.chain_id, exposure_qty=str(delta),
                           maker_order_id=maker.order_id if maker is not None else "", maker_filled=str(maker.filled_quantity) if maker is not None else "0", **extra)
        self._hedge_move(plan, chain, HedgeState.HEDGE_PENDING, ts_ns, "maker fill" if maker is not None else "carried dust")
        if self.observer is not None:
            if dust_included:
                self.observer.chain_opened(plan, chain.chain_id, maker, spec, delta, ts_ns / 1e9, dust_included=float(abs(dust_included)))
            else:
                self.observer.chain_opened(plan, chain.chain_id, maker, spec, delta, ts_ns / 1e9)
        self._hedge_try_dispatch(plan, spec, chain, ts_ns)

    # ------------------------------------------------------------------------------------------------------------------
    # HEDGE DUST CARRY (owner 16 Nov; hedge_faults.dust_carry next_hedge; OFF by default).
    # A hedge chunk below the hedge venue's minimum quantity or notional is not sent and not unwound: it is carried in ONE signed balance per (hedge venue, instrument), shared by every plan, and the next hedge
    # on that venue is its own quantity plus the balance (an opposite balance reduces it; the lot rounding never loosens: what cannot be sent stays carried). When a hedge of the balance alone reaches the
    # minimum (a price move, or fills of several plans) it is sent at once as an ordinary hedge chain (size-aware IOC cap, retries, unwind and alarm path): on the plan whose fill lifted it over the
    # minimum, or on a small dust-host plan when only a price move did. The balance is exposure that sits on the maker venue; it never blocks new risk and never trips the breaker.
    # Accounting: see _dust_plan_adj (set in __init__). The balance, the adjustments and the requested quantity are persisted as DUST_ADJUST plan events and replayed on a restart.
    # ------------------------------------------------------------------------------------------------------------------
    def dust_balance_total(self) -> Decimal:
        """Signed carried dust exposure (base, + long) over every (venue, instrument); 0 when nothing is carried."""
        return sum((row["exposure"] for row in self._dust.values()), Decimal("0"))

    def dust_adj_closed(self) -> Decimal:
        """Sum of the dust adjustments of CLOSED plans: added to the net that no open plan manages, it leaves the carried dust out of what a breaker flatten would try to send."""
        return sum((adj for pid, adj in self._dust_plan_adj.items() if pid in self.plans and self.plans[pid].state == PlanState.CLOSED), Decimal("0"))

    def _dust_row(self, key: tuple[str, str]) -> dict[str, Any]:
        return self._dust.setdefault(key, {"exposure": Decimal("0"), "since_ns": None, "alarmed": False, "host": None})

    def _dust_sendable(self, venue: str, instrument_id: str, side: Side, qty: Decimal, ts_ns: int, planned_limit: Decimal) -> tuple[bool, str, Decimal | None]:
        """Would a hedge of `qty` pass the venue's own checks at the price the dispatch would use? The cap is the dispatch's (a dry run: no ledger event, no counter), so a quantity that passes here passes the
        dispatch's validate_order. Returns (sendable, why not, the cap). Only a minimum quantity or notional makes a chunk dust; any other refusal is left to the dispatch as before."""
        if qty <= 0:
            return False, "below_lot", None
        book = self._books.get((venue, instrument_id))
        usable = book is not None and book.state == BookState.VALID and bool(book.bids) and bool(book.asks)
        probe = {"venue": venue, "instrument_id": instrument_id, "side": side, "limit_price": planned_limit}
        if usable:
            touch = book.best_ask if side == Side.BUY else book.best_bid
            cap = self._hedge_cap_value(None, None, dict(probe, limit_price=planned_limit if planned_limit is not None else touch), book, touch, qty, ts_ns, 0, record=False)
        else:
            tick = self.instruments.get(venue, instrument_id, ts_ns).tick_size
            cap = planned_limit if not planned_limit % tick else limit_on_grid(side.value, planned_limit, tick)
        child = ChildOrder(order_id="dust-probe", parent_id="", opportunity_id="", venue=venue, instrument_id=instrument_id, side=side, quantity=qty, limit_price=cap, order_role="HEDGE")
        try:
            self.instruments.validate_order(child, ts_ns)
        except ValueError as exc:
            if str(exc) in ("quantity below minimum", "notional below minimum"):
                return False, str(exc), cap
        return True, "", cap

    def _dust_apply(self, plan: ParentPlan, key: tuple[str, str], adj_delta: Decimal, ts_ns: int, kind: str, spec: dict[str, Any] | None = None) -> tuple[Decimal, Decimal]:
        """Move `adj_delta` of exposure between the balance and `plan` (the balance falls by it, the plan's adjustment rises by it). Returns (balance before, after). Persisted; the alarm and the clock follow the balance."""
        row = self._dust_row(key)
        before = row["exposure"]
        after = before - adj_delta
        row["exposure"] = after
        if adj_delta:
            self._dust_plan_adj[plan.parent_id] = self._dust_plan_adj.get(plan.parent_id, Decimal("0")) + adj_delta
        if before == 0 and after != 0:
            row["since_ns"] = ts_ns
        cleared = after == 0 and before != 0
        age_s = (ts_ns - row["since_ns"]) / 1e9 if row["since_ns"] is not None else 0.0
        self.dust_stats["oldest_age_s"] = max(self.dust_stats["oldest_age_s"], age_s)
        self.dust_stats["max_abs_base"] = max(self.dust_stats["max_abs_base"], abs(after))
        self._dust_last_plan[key] = plan.parent_id
        self._recompute_plan_economics(plan, ts_ns)
        if cleared:
            if row["alarmed"]:
                self.ledger.record("HEDGE_DUST_ALARM_CLEARED", ts_ns, venue=key[0], instrument_id=key[1], age_s=age_s)
                self._persist(plan, ts_ns, "DUST_ALARM_CLEARED", key=list(key))
            self.ledger.record("HEDGE_DUST_CLEARED", ts_ns, parent_id=plan.parent_id, venue=key[0], instrument_id=key[1], age_s=age_s)
            row["since_ns"], row["alarmed"] = None, False
        self._persist(plan, ts_ns, "DUST_ADJUST", key=list(key), adj_delta=str(adj_delta), balance_after=str(after), since_ns=row["since_ns"], kind=kind,
                      **({"requested_qty": str(spec["requested_qty"])} if spec is not None else {}))
        return before, after

    def _hedge_for_maker_fill_dust(self, plan: ParentPlan, spec: dict[str, Any], maker: ChildOrder, ts_ns: int) -> None:
        filled = min(self._lineage_filled(plan, maker), spec["max_qty"])
        chunk = filled - spec["requested_qty"]                       # this plan's new quantity: handed to a hedge or to the balance (never floored here: the lot remainder is carried too)
        if chunk <= 0:
            return
        spec["requested_qty"] = filled
        key = (spec["venue"], spec["instrument_id"])
        e = chunk if spec["side"] == Side.SELL else -chunk           # exposure the chunk opened: a SELL hedge closes a long, a BUY hedge a short
        before = self._dust_row(key)["exposure"]
        total = before + e
        side = Side.SELL if total > 0 else Side.BUY
        send = self.instruments.round_quantity(spec["venue"], spec["instrument_id"], abs(total), ts_ns)
        ok, why, cap = (False, "side", None) if side != spec["side"] else self._dust_sendable(spec["venue"], spec["instrument_id"], side, send, ts_ns, spec["limit_price"])
        if not ok:
            why = why if why != "side" else "opposite side"
            if send <= 0:
                why = "below_lot"
            before, after = self._dust_apply(plan, key, -e, ts_ns, "carried", spec)
            self.dust_stats["carried_events"] += 1
            self.realism_counters["hedge_dust_carried"] = self.realism_counters.get("hedge_dust_carried", 0) + 1
            self.ledger.record("HEDGE_DUST_CARRIED", ts_ns, parent_id=plan.parent_id, maker_order_id=maker.order_id, venue=spec["venue"], instrument_id=spec["instrument_id"], hedge_side=side.value,
                               signed_exposure=str(e), price=str(cap if cap is not None else spec["limit_price"]), balance_before=str(before), balance_after=str(after), reason=why,
                               min_quantity=str(self.instruments.get(spec["venue"], spec["instrument_id"], ts_ns).min_quantity),
                               min_notional=str(self.instruments.get(spec["venue"], spec["instrument_id"], ts_ns).min_notional))
            self._fill_triggered_settle(plan, spec, ts_ns)
            return
        sent_e = send if total > 0 else -send
        before, after = self._dust_apply(plan, key, sent_e - e, ts_ns, "sent", spec)
        taken = before - after                                       # the carried exposure this hedge actually takes in (a sub-lot remainder that stays carried is not "included")
        if taken:
            self.dust_stats["hedges_with_dust"] += 1
            self.ledger.record("HEDGE_DUST_INCLUDED", ts_ns, parent_id=plan.parent_id, venue=spec["venue"], instrument_id=spec["instrument_id"], dust_included=str(taken), hedge_quantity=str(send),
                               own_quantity=str(chunk), balance_before=str(before), balance_after=str(after))
        if after != before and after != 0:
            self.dust_stats["carried_events"] += 1
            self.ledger.record("HEDGE_DUST_CARRIED", ts_ns, parent_id=plan.parent_id, maker_order_id=maker.order_id, venue=spec["venue"], instrument_id=spec["instrument_id"], hedge_side=side.value,
                               signed_exposure=str(after), price=str(cap), balance_before=str(before), balance_after=str(after), reason="lot_remainder")
        self._hedge_open_chain(plan, spec, maker, send, ts_ns, dust_included=taken if taken else None)

    def _dust_check(self, now_ns: int) -> None:
        """Every timer and book update: the age alarm, and the hedge of a balance that reached the minimum with no fill to carry it (a price move)."""
        for key, row in list(self._dust.items()):
            bal = row["exposure"]
            if bal == 0:
                continue
            if not row["alarmed"] and row["since_ns"] is not None and now_ns - row["since_ns"] > int(self.hedge_policy.dust_alarm_after_s * 1e9):
                row["alarmed"] = True
                self.dust_stats["alarms"] += 1
                age_s = (now_ns - row["since_ns"]) / 1e9
                self.dust_stats["oldest_age_s"] = max(self.dust_stats["oldest_age_s"], age_s)
                self.ledger.record("HEDGE_DUST_ALARM", now_ns, venue=key[0], instrument_id=key[1], balance=str(bal), age_s=age_s, alarm_after_s=self.hedge_policy.dust_alarm_after_s,
                                   label="ASSUMED", **self.alert_context(now_ns))
                last = self.plans.get(self._dust_last_plan.get(key, ""))
                if last is not None:
                    self._persist(last, now_ns, "DUST_ALARM", key=list(key))
            self._dust_try_host(key, row, now_ns)

    def _dust_try_host(self, key: tuple[str, str], row: dict[str, Any], now_ns: int) -> None:
        host = row.get("host")
        if host is not None:
            hp = self.plans.get(host)
            if hp is not None and hp.state != PlanState.CLOSED:
                return                                               # one own-dust hedge at a time
            row["host"] = None
        if self.restart_reconcile_gate and (self._restart_unreconciled or self._restart_account_problems):
            return
        ctx = self.plans.get(self._dust_last_plan.get(key, ""))
        if ctx is None:
            return
        bal = row["exposure"]
        side = Side.SELL if bal > 0 else Side.BUY
        send = self.instruments.round_quantity(key[0], key[1], abs(bal), now_ns)
        book = self._books.get(key)
        if send <= 0 or book is None or book.state != BookState.VALID or not book.bids or not book.asks:
            return
        touch = book.best_ask if side == Side.BUY else book.best_bid
        ok, _, _ = self._dust_sendable(key[0], key[1], side, send, now_ns, touch)
        if not ok:
            return
        opp0 = ctx.opportunity
        self.dust_stats["host_plans"] += 1
        opportunity = Opportunity(
            opportunity_id=stable_id("dust-host-opportunity", key[0], key[1], now_ns, self.dust_stats["host_plans"]), strategy_id=opp0.strategy_id, instrument_id=key[1], observed_ts_ns=now_ns,
            buy_venue=opp0.buy_venue, sell_venue=opp0.sell_venue, buy_price=opp0.buy_price, sell_price=opp0.sell_price, quantity=abs(bal), gross_edge_bps=Decimal("0"), estimated_net_edge_bps=Decimal("0"),
            source_event_ids=("hedge dust", str(now_ns)))
        plan = ParentPlan(parent_id=stable_id("dust-host-parent", opportunity.opportunity_id), opportunity=opportunity, children=[], state=PlanState.DISPATCHED,
                          transitions=[{"ts_ns": now_ns, "state": PlanState.DISPATCHED.value, "reason": "hedge of the carried dust"}])
        self.plans[plan.parent_id] = plan
        self._dust_host_plans.add(plan.parent_id)
        row["host"] = plan.parent_id
        spec = {"venue": key[0], "side": side, "instrument_id": key[1], "limit_price": touch, "max_qty": abs(bal), "requested_qty": Decimal("0"), "maker_order_id": "", "chain_seq": 0,
                "opportunity_id": opportunity.opportunity_id, "dust_host": True}
        self._hedge_specs[plan.parent_id] = spec
        self._persist(plan, now_ns, "DUST_HOST_PLAN", key=list(key), balance=str(bal))
        self._persist(plan, now_ns, "HEDGE_SPEC", **spec)
        self.ledger.record("HEDGE_DUST_HOST_OPENED", now_ns, parent_id=plan.parent_id, venue=key[0], instrument_id=key[1], balance=str(bal), hedge_quantity=str(send), hedge_side=side.value)
        sent_e = send if bal > 0 else -send
        before, after = self._dust_apply(plan, key, sent_e, now_ns, "sent_alone", spec)
        self.dust_stats["hedges_with_dust"] += 1
        self.ledger.record("HEDGE_DUST_INCLUDED", now_ns, parent_id=plan.parent_id, venue=key[0], instrument_id=key[1], dust_included=str(before), hedge_quantity=str(send), own_quantity="0",
                           balance_after=str(after))
        self._hedge_open_chain(plan, spec, None, send, now_ns, dust_included=before)

    def dust_report(self, now_ns: int | None = None) -> dict[str, Any]:
        """End-of-day figures of the dust carry: how often dust was carried, the largest balance, the oldest age (closed episodes and the one still open), and the balance at the end."""
        oldest = self.dust_stats["oldest_age_s"]
        if now_ns is not None:
            for row in self._dust.values():
                if row["exposure"] != 0 and row["since_ns"] is not None:
                    oldest = max(oldest, (now_ns - row["since_ns"]) / 1e9)
        return {"mode": self.hedge_policy.dust_carry, "alarm_after_s": self.hedge_policy.dust_alarm_after_s, "alarm_after_label": "ASSUMED", "carried_events": self.dust_stats["carried_events"],
                "max_abs_base": str(self.dust_stats["max_abs_base"]), "oldest_age_s": oldest, "hedges_with_dust": self.dust_stats["hedges_with_dust"], "host_hedges": self.dust_stats["host_plans"],
                "alarms": self.dust_stats["alarms"], "balance_end": str(self.dust_balance_total())}

    def _recover_dust_state(self) -> None:
        """After a restart: the balance, every plan's adjustment, the clock and the alarm flag from the DUST_* plan events (the sum of the deltas does not depend on the order; the clock and the flag follow the
        last event by time). Runs before the ledger is rebuilt, so the first residual computation already has the adjustments."""
        rows: list[tuple[int, int, str, dict[str, Any]]] = []
        for pid in self.plans:
            for ev in self.plan_store.events(pid):
                if ev["event_type"] in ("DUST_ADJUST", "DUST_ALARM", "DUST_ALARM_CLEARED"):
                    rows.append((ev["ts_ns"], ev["version"], pid, ev))
                elif ev["event_type"] == "DUST_HOST_PLAN":
                    self._dust_host_plans.add(pid)
        rows.sort(key=lambda r: (r[0], r[1]))
        for _, _, pid, ev in rows:
            pl = ev["payload"]
            key = (pl["key"][0], pl["key"][1])
            row = self._dust_row(key)
            if ev["event_type"] == "DUST_ADJUST":
                delta = Decimal(pl["adj_delta"])
                self._dust_plan_adj[pid] = self._dust_plan_adj.get(pid, Decimal("0")) + delta
                row["exposure"] -= delta
                row["since_ns"] = pl["since_ns"]
                if row["exposure"] == 0:
                    row["alarmed"] = False
                self._dust_last_plan[key] = pid
            else:
                row["alarmed"] = ev["event_type"] == "DUST_ALARM"
        for key, row in self._dust.items():
            hosts = [pid for pid in self._dust_host_plans if pid in self.plans and self.plans[pid].state != PlanState.CLOSED and self._hedge_specs.get(pid, {}).get("venue") == key[0]]
            row["host"] = hosts[-1] if hosts else None

    def _hedge_dispatch_refusal(self, spec: dict[str, Any], ts_ns: int) -> str | None:
        """EMS01 at dispatch: the hedge book must be usable now. Returns why not, or None. (The price is not a refusal: see _hedge_try_dispatch.)"""
        book = self._books.get((spec["venue"], spec["instrument_id"]))
        if book is None:
            return "hedge_book_missing"
        if book.state != BookState.VALID:
            return f"hedge_book_{book.state.value.lower()}"
        if not book.bids or not book.asks:
            return "hedge_book_one_sided"
        limit = self.hedge_policy.max_book_age_ns
        if limit is not None and book.last_receive_ts_ns is not None and ts_ns - book.last_receive_ts_ns > limit:
            return "hedge_book_stale"
        return None

    def _size_aware_cap(self, plan, chain, spec, book, touch: Decimal, qty: Decimal, ts_ns: int, attempt: int, record: bool = True) -> Decimal:
        """Cap rule size_aware (owner 12 Nov, EMS note price-cap construction), re-taken on every attempt from the bot's VIEW of the hedge book (`self._books`, never the venue's own truth book).
        Where the touch covers the size it is touch_slack exactly. Where the view book holds no level beyond the touch (best bid/ask only, as Binance in March 2024) and the touch cannot cover the
        size, the rule falls back to touch_slack and says so (HEDGE_CAP_SIZE_AWARE, fallback). Every decision is a ledger event; the absolute limit binding is counted."""
        policy, side = self.hedge_policy, spec["side"]
        tick = self.instruments.get(spec["venue"], spec["instrument_id"]).tick_size
        deepest, covered, n_levels = book.reach(side, qty) if qty > 0 else (touch, Decimal("0"), 0)
        fallback = n_levels <= 1 and covered < qty
        vol_bps = 0.0
        if policy.vol_k > 0:
            vol_bps = policy.vol_k * float(self.hedge_short_vol_bps(spec["venue"], ts_ns) if self.hedge_short_vol_bps is not None else 0.0) * policy.latency_s ** 0.5
        if fallback or deepest is None:
            cap, binds = hedge_cap_on_grid(side.value, touch, policy.touch_slack_bps, tick), False
        else:
            cap, binds = hedge_cap_size_aware(side.value, touch, deepest, policy.touch_slack_bps, vol_bps, policy.max_slip_bps, tick)
        if not record:                                            # a dry run (the dust sendability check): the same cap, no counter and no ledger event
            return cap
        if binds:
            self.realism_counters["hedge_cap_abs_limit_binds"] = self.realism_counters.get("hedge_cap_abs_limit_binds", 0) + 1
        if fallback:
            self.realism_counters["hedge_cap_depth_fallback"] = self.realism_counters.get("hedge_cap_depth_fallback", 0) + 1
        self.ledger.record(
            "HEDGE_CAP_SIZE_AWARE", ts_ns, parent_id=plan.parent_id, chain_id=chain.chain_id, attempt=attempt, side=side.value, qty=str(qty), touch=str(touch),
            deepest=None if deepest is None else str(deepest), covered_qty=str(covered), touch_covers=bool(deepest == touch and covered >= qty), levels=n_levels,
            cap=str(cap), abs_limit_binds=binds, vol_term_bps=vol_bps, vol_source="none" if vol_bps == 0.0 else "hedge_short_vol_bps",
            fallback="touch_slack: the view book holds no depth beyond the touch" if fallback else "", book="view",
        )
        return cap

    def _hedge_cap_value(self, plan, chain, spec, book, touch: Decimal, qty: Decimal, ts_ns: int, attempt: int, record: bool = True) -> Decimal:
        """The cap of one hedge attempt (the body of the dispatch's cap rule, shared with the dust sendability dry run, which calls it with record False: same price, no ledger event, no counter)."""
        cap = spec["limit_price"]
        tick = self.instruments.get(spec["venue"], spec["instrument_id"]).tick_size
        if cap % tick:                                            # a planned price off the grid is moved to the grid without loosening it (buy down, sell up), never sent as it is
            cap = limit_on_grid(spec["side"].value, cap, tick)
        if self.hedge_policy.size_aware and self.hedge_policy.touch_slack_bps is not None:
            cap = self._size_aware_cap(plan, chain, spec, book, touch, qty, ts_ns, attempt, record)
        elif self.hedge_policy.touch_slack_bps is not None:      # cap rule touch_slack: re-taken from the CURRENT hedge touch on every attempt, on the venue's price grid, never inside the cap
            basis = touch
            if self.hedge_policy.cap_basis == "size_vwap" and qty > 0:       # owner 12 Nov option: start from the executable VWAP of the hedge size (the touch when the book cannot cover it)
                filled, vwap = book.walk(spec["side"], qty, Decimal("Infinity") if spec["side"] == Side.BUY else Decimal("0"))
                if filled >= qty and vwap > 0:
                    basis = vwap
            cap = hedge_cap_on_grid(spec["side"].value, basis, self.hedge_policy.touch_slack_bps, self.instruments.get(spec["venue"], spec["instrument_id"]).tick_size)
        return cap

    def _hedge_try_dispatch(self, plan: ParentPlan, spec: dict[str, Any], chain: HedgeLifecycle, ts_ns: int) -> bool:
        reason = self._hedge_dispatch_refusal(spec, ts_ns)
        if reason is not None:
            if self._hedge_held.get(chain.chain_id) != reason:
                self._hedge_held[chain.chain_id] = reason
                self.realism_counters["hedge_dispatch_held"] = self.realism_counters.get("hedge_dispatch_held", 0) + 1
                self.ledger.record("HEDGE_DISPATCH_HELD", ts_ns, parent_id=plan.parent_id, chain_id=chain.chain_id, reason=reason)
            return False
        self._hedge_held.pop(chain.chain_id, None)
        book = self._books[(spec["venue"], spec["instrument_id"])]
        touch = book.best_ask if spec["side"] == Side.BUY else book.best_bid
        touch_past_planned = (spec["side"] == Side.BUY and touch > spec["limit_price"]) or (spec["side"] == Side.SELL and touch < spec["limit_price"])
        done = sum((o.filled_quantity for o in plan.children if o.order_id in self._chain_children[chain.chain_id]), Decimal("0"))
        qty = Decimal(str(chain.exposure_qty)) - done
        attempt = len(self._chain_children[chain.chain_id])
        cap = self._hedge_cap_value(plan, chain, spec, book, touch, qty, ts_ns, attempt)
        if touch_past_planned:
            # Track A (owner 11 Nov): the old name, HEDGE_CAP_BREACH_AT_DISPATCH, read like a breach of the order's price cap. It is not: the cap of the child that is sent (`child_cap`) is always taken from the CURRENT touch and
            # is never breached. What this records is that by dispatch time the market had moved past the hedge price the opportunity PLANNED (`planned_limit`). The hedge reduces exposure just taken, so it is still sent
            # at `child_cap` (it may find nothing: ioc_unfilled -> retry or unwind); the move is on record.
            self.realism_counters["hedge_touch_past_planned_limit"] = self.realism_counters.get("hedge_touch_past_planned_limit", 0) + 1
            self.ledger.record("HEDGE_TOUCH_PAST_PLANNED_LIMIT", ts_ns, parent_id=plan.parent_id, chain_id=chain.chain_id, touch=str(touch), planned_limit=str(spec["limit_price"]), child_cap=str(cap),
                               reason="touch_past_planned_limit")
        child = ChildOrder(
            order_id=stable_id("child", plan.parent_id, "HEDGE", chain.chain_id, attempt), parent_id=plan.parent_id, opportunity_id=spec["opportunity_id"],
            venue=spec["venue"], instrument_id=spec["instrument_id"], side=spec["side"], quantity=qty, limit_price=cap, order_role="HEDGE")
        try:
            self.instruments.validate_order(child, ts_ns)
        except ValueError as exc:
            self.ledger.record("HEDGE_CHILD_INVALID", ts_ns, parent_id=plan.parent_id, chain_id=chain.chain_id, reason=str(exc), quantity=str(qty))
            self._hedge_refused(plan, spec, chain, ts_ns, str(exc), "instrument_check")
            return False
        if self.hedge_gate is not None:
            self.hedge_gate(child, ts_ns)
        self._chain_children[chain.chain_id].append(child.order_id)
        self._chain_of_child[child.order_id] = chain
        plan.children.append(child)
        if self.observer is not None:
            self.observer.hedge_child(chain.chain_id, child, ts_ns / 1e9)
        was = chain.state
        self._hedge_move(plan, chain, HedgeState.SUBMITTED, ts_ns, f"send {child.order_id}" + (" (retry)" if was is HedgeState.RETRYING else ""))
        self.ledger.record("HEDGE_SUBMIT", ts_ns, parent_id=plan.parent_id, chain_id=chain.chain_id, client_order_id=child.order_id, quantity=str(qty), attempt=attempt)
        self._hedge_dispatching += 1
        try:
            self._dispatch_leg(plan, child, len(plan.children) - 1, plan.opportunity, self._books, ts_ns, ts_ns)
        finally:
            self._hedge_dispatching -= 1
        if child.state not in TERMINAL_ORDER_STATES:
            self._watch_order(child, ts_ns)               # the report deadline: no answer in time means the venue is queried, never that the hedge is resent
        return True

    def _hedge_refused(self, plan: ParentPlan, spec: dict[str, Any], chain: HedgeLifecycle, ts_ns: int, reason: str, code: str) -> None:
        """The hedge could not be built or sent (a lot, minimum or instrument rule): classified, never retried blind, goes to the unwind path."""
        verdict = classify_reject(self._market_for(spec["venue"]), code)
        chain.note_reject(ts_ns / 1e9, verdict.label, verdict.retry_permitted)
        if chain.state is HedgeState.HEDGE_PENDING:
            self._hedge_move(plan, chain, HedgeState.REJECTED, ts_ns, reason)
            self._hedge_after_reject(plan, spec, chain, ts_ns)
        elif chain.state is HedgeState.RETRYING:
            self._hedge_move(plan, chain, HedgeState.UNWINDING, ts_ns, f"retry could not be built: {reason}")

    def _hedge_child_changed(self, plan: ParentPlan, spec: dict[str, Any], chain: HedgeLifecycle, order: ChildOrder, ts_ns: int) -> None:
        mine = [o for o in plan.children if o.order_id in self._chain_children[chain.chain_id]]
        hedged = sum((o.filled_quantity for o in mine), Decimal("0"))
        exposure = Decimal(str(chain.exposure_qty))
        st = order.state
        if chain.state in (HedgeState.HEDGED, HedgeState.RECONCILED, HedgeState.UNWINDING, HedgeState.FAILED, HedgeState.ESCALATED):
            if order.filled_quantity > 0 and float(hedged) > chain.hedged_qty + 1e-12:
                self.ledger.record("HEDGE_LATE_FILL", ts_ns, parent_id=plan.parent_id, chain_id=chain.chain_id, order_id=order.order_id, chain_state=chain.state.value,
                                   hedged=str(hedged))
            return
        if chain.state is HedgeState.UNKNOWN:
            if st is OrderState.FILLED or (st is OrderState.CANCELLED and order.filled_quantity >= order.quantity):
                chain.note_query("found_executed" if hedged >= exposure else "found_partial")
            elif st in (OrderState.ACKNOWLEDGED, OrderState.SENT):
                chain.note_query("found_live")
            elif st is OrderState.PARTIAL:
                chain.note_query("found_partial")
            elif st is OrderState.REJECTED:
                chain.note_query("not_found")
            elif st is OrderState.CANCELLED:
                chain.note_query("found_partial" if order.filled_quantity > 0 else "not_found")
        if st is OrderState.TIMED_OUT:
            if chain.state in (HedgeState.SUBMITTED, HedgeState.RETRYING):
                self._hedge_move(plan, chain, HedgeState.UNKNOWN, ts_ns, "order timed out")
            if chain.state is HedgeState.UNKNOWN and chain.query_failures >= chain.query_attempt_limit:
                self._hedge_unknown_escalation(plan, chain, ts_ns)
            return
        if hedged >= exposure - Decimal("1e-12"):
            self._hedge_move(plan, chain, HedgeState.HEDGED, ts_ns, "fully hedged", filled_qty=float(hedged))
            return
        if st is OrderState.PARTIAL and hedged > Decimal(str(chain.hedged_qty)):
            if chain.state is HedgeState.UNKNOWN:
                self._hedge_move(plan, chain, HedgeState.PARTIALLY_FILLED, ts_ns, "query found a partial fill", filled_qty=float(hedged))
            elif chain.state in (HedgeState.SUBMITTED, HedgeState.PARTIALLY_FILLED):
                self._hedge_move(plan, chain, HedgeState.PARTIALLY_FILLED, ts_ns, "partial hedge fill", filled_qty=float(hedged))
            return
        if st in (OrderState.ACKNOWLEDGED, OrderState.SENT) and chain.state is HedgeState.UNKNOWN and chain._query_result == "found_live":
            self._hedge_move(plan, chain, HedgeState.SUBMITTED, ts_ns, "the venue has the order live")
            return
        terminal_unfilled = st is OrderState.CANCELLED or st is OrderState.FILLED or st is OrderState.REJECTED
        if not terminal_unfilled:
            return
        if st is OrderState.REJECTED:
            if chain.state is HedgeState.UNKNOWN:                                  # the venue says it does not know the order: a replacement is allowed
                self._hedge_after_query_not_found(plan, spec, chain, ts_ns)
                return
            record = next((r for r in reversed(self.reject_records) if r["order_id"] == order.order_id), None)
            chain.note_reject(ts_ns / 1e9, str(record["label"]) if record else "unclassified", bool(record and record["retry_permitted"]))
            if chain.state in (HedgeState.SUBMITTED, HedgeState.HEDGE_PENDING):
                self._hedge_move(plan, chain, HedgeState.REJECTED, ts_ns, f"rejected: {order.reject_reason}")
            self._hedge_after_reject(plan, spec, chain, ts_ns)
            return
        # CANCELLED (an IOC remainder the venue dropped) or FILLED short of the exposure: part may be hedged, the rest needs another child
        if hedged > Decimal(str(chain.hedged_qty)) and chain.state in (HedgeState.SUBMITTED, HedgeState.UNKNOWN, HedgeState.PARTIALLY_FILLED):
            self._hedge_move(plan, chain, HedgeState.PARTIALLY_FILLED, ts_ns, "IOC remainder not filled", filled_qty=float(hedged))
            self._hedge_after_reject(plan, spec, chain, ts_ns)
            return
        if chain.state is HedgeState.UNKNOWN:
            self._hedge_after_query_not_found(plan, spec, chain, ts_ns)
            return
        order.reject_reason = "ioc_expired_unfilled"                               # an IOC the venue dropped unfilled: an engine refusal, not a venue code
        self._classify_reject(plan, order, ts_ns)
        record = next((r for r in reversed(self.reject_records) if r["order_id"] == order.order_id), None)
        chain.note_reject(ts_ns / 1e9, str(record["label"]) if record else "ioc_unfilled", bool(record and record["retry_permitted"]))
        if chain.state is HedgeState.SUBMITTED:
            self._hedge_move(plan, chain, HedgeState.REJECTED, ts_ns, "IOC found nothing inside its cap")
        self._hedge_after_reject(plan, spec, chain, ts_ns)

    def _hedge_after_query_not_found(self, plan: ParentPlan, spec: dict[str, Any], chain: HedgeLifecycle, ts_ns: int) -> None:
        decision = hedge_retry_decision(chain.retries, chain.retry_limit, True, self.hedge_policy.retry_backoff_ns, ts_ns)
        if decision.retry:
            self._hedge_move(plan, chain, HedgeState.RETRYING, ts_ns, "the venue does not know the order: replacement allowed")
            self._hedge_retry_due[chain.chain_id] = decision.due_ns
        else:
            self._hedge_move(plan, chain, HedgeState.UNWINDING, ts_ns, "not at the venue and no retry budget")

    def _hedge_after_reject(self, plan: ParentPlan, spec: dict[str, Any], chain: HedgeLifecycle, ts_ns: int) -> None:
        """The reject table decides: a retryable reject (or an IOC remainder) with budget left is retried after the backoff; anything else is unwound."""
        permitted = chain.state is HedgeState.PARTIALLY_FILLED or bool(chain._last_reject_retry_permitted)
        decision = hedge_retry_decision(chain.retries, chain.retry_limit, permitted, self.hedge_policy.retry_backoff_ns, ts_ns)
        if decision.retry:
            self._hedge_move(plan, chain, HedgeState.RETRYING, ts_ns, "retry permitted")
            self._hedge_retry_due[chain.chain_id] = decision.due_ns
            return
        why = decision.why_not
        if chain.state in (HedgeState.REJECTED, HedgeState.PARTIALLY_FILLED):
            self._hedge_move(plan, chain, HedgeState.UNWINDING, ts_ns, why)

    def _hedge_unknown_escalation(self, plan: ParentPlan, chain: HedgeLifecycle, ts_ns: int) -> None:
        """H16, settled: when status queries keep failing the chain goes to FAILED (the persistent alarm, no new risk until an operator reset) AND the core's kill switch
        trips (the OMS does this on 'unconfirmed order state'). Both, not one: the exposure is of unknown size, so it can be neither hedged nor unwound blind, and
        resting maker orders must stop adding to it. The backtester raises only the alarm; that stays until Phase D."""
        context = self.alert_context(ts_ns)           # taken BEFORE the chain is marked FAILED: the hedge that is still unconfirmed is the pending hedge quantity this alert reports
        self._hedge_move(plan, chain, HedgeState.FAILED, ts_ns, "status queries exhausted")
        self.ledger.record("HEDGE_ALARM", ts_ns, parent_id=plan.parent_id, chain_id=chain.chain_id, reason="unknown_unresolved", kill_switch=self.risk.kill_switch,
                           **{**context, "execution_blocked_reason": context["execution_blocked_reason"] or "hedge_alarm:kill_switch_follows"})
        self.unwind_exhausted_alarms.setdefault(plan.parent_id, {"reason": "hedge_unknown_unresolved", "ts_ns": ts_ns, "chain_id": chain.chain_id})
        if not self.risk.kill_switch:
            self.risk.activate_kill_switch(now_ns=ts_ns, reason="hedge status unresolved", source="OMS_HEDGE_UNKNOWN", ledger=self.ledger)
            self.cancel_open_orders("hedge status unresolved", ts_ns)

    def _hedge_on_query(self, plan: ParentPlan, order: ChildOrder, answered: bool, ts_ns: int) -> None:
        chain = self._chain_of_child.get(order.order_id)
        if chain is None:
            return
        if chain.state in (HedgeState.SUBMITTED, HedgeState.RETRYING):
            self._hedge_move(plan, chain, HedgeState.UNKNOWN, ts_ns, "report deadline passed: the venue is queried")
        if not answered and chain.state is HedgeState.UNKNOWN:
            chain.note_query("failed")

    def _hedge_timer_candidates(self) -> list[int]:
        out = list(self._hedge_retry_due.values())
        if self.dust_on:
            out += [row["since_ns"] + int(self.hedge_policy.dust_alarm_after_s * 1e9) + 1 for row in self._dust.values()
                    if row["exposure"] != 0 and row["since_ns"] is not None and not row["alarmed"]]          # the age alarm fires on time, not at the next event
        for parent_id, chains in self.hedge_chains.items():
            for c in chains.values():
                if c.max_residual_age_s is not None and c.state in self._CHAIN_OPEN and c.exposure_closed_ts is None:
                    t = int((c.opened_ts + c.max_residual_age_s) * 1e9) + 1
                    while t / 1e9 - c.opened_ts <= c.max_residual_age_s and t < 1 << 62:      # the timer must land where the breach test (float seconds) is true: at today's epoch one float step is about 240 ns, so +1 ns can sit just before it and the timers never converge (found on the v1_10 2i run: a held hedge)
                        t += 64
                    out.append(t)
        return out

    def _hedge_progress_all(self, now_ns: int) -> None:
        if self._hedge_dispatching or self._hedge_finalizing:
            return
        if self.dust_on:
            self._dust_check(now_ns)
        for parent_id in list(self.hedge_chains):
            plan = self.plans.get(parent_id)
            if plan is not None and plan.state != PlanState.CLOSED:
                self._fill_triggered_settle(plan, self._hedge_specs[parent_id], now_ns)

    def _fill_triggered_settle(self, plan: ParentPlan, spec: dict[str, Any], ts_ns: int) -> None:
        if self._hedge_dispatching or self._hedge_finalizing:
            return
        for chain in list(self.hedge_chains.get(plan.parent_id, {}).values()):
            if chain.state is HedgeState.HEDGE_PENDING and chain.chain_id in self._hedge_held:
                self._hedge_try_dispatch(plan, spec, chain, ts_ns)
            elif chain.state is HedgeState.RETRYING and self._hedge_retry_due.get(chain.chain_id, 1 << 62) <= ts_ns:
                self._hedge_retry_due.pop(chain.chain_id, None)
                self._hedge_try_dispatch(plan, spec, chain, ts_ns)
            elif chain.state is HedgeState.RETRYING and chain.chain_id in self._hedge_held:
                self._hedge_try_dispatch(plan, spec, chain, ts_ns)
            if chain.state in self._CHAIN_OPEN and chain.residual_age_breached(ts_ns / 1e9):
                self.realism_counters["hedge_residual_age_breaches"] = self.realism_counters.get("hedge_residual_age_breaches", 0) + 1
                self._hedge_retry_due.pop(chain.chain_id, None)
                self._hedge_held.pop(chain.chain_id, None)
                self._hedge_move(plan, chain, HedgeState.ESCALATED, ts_ns, "residual age limit")
        for chain in list(self.hedge_chains.get(plan.parent_id, {}).values()):
            if chain.state is HedgeState.ESCALATED and not self._unknown_state_children(plan, ts_ns):
                self._hedge_move(plan, chain, HedgeState.UNWINDING, ts_ns, "escalated: deterministic unwind")
        if plan.state != PlanState.CLOSED and self._fill_triggered_settled(plan):
            self._finalize_plan(plan, ts_ns, self._books)
            self._hedge_after_finalize(plan, ts_ns)

    def _hedge_after_finalize(self, plan: ParentPlan, ts_ns: int) -> None:
        for chain in self.hedge_chains.get(plan.parent_id, {}).values():
            if chain.state is HedgeState.UNWINDING:
                if plan.parent_id in self.unwind_exhausted_alarms:
                    self._hedge_move(plan, chain, HedgeState.FAILED, ts_ns, "unwind attempts exhausted")
                elif plan.state == PlanState.CLOSED and plan.residual_base == 0:
                    self._hedge_move(plan, chain, HedgeState.RECONCILED, ts_ns, "unwound and reconciled", unwound_qty=chain.exposure_qty - chain.hedged_qty)
            elif chain.state is HedgeState.HEDGED and plan.state == PlanState.CLOSED and plan.residual_base == 0:
                self._hedge_move(plan, chain, HedgeState.RECONCILED, ts_ns, "positions and orders reconciled")

    def reconcile_account_after_restart(self, venues: Mapping[str, Any], now_ns: int, books: dict[tuple[str, str], OrderBook] | None = None) -> dict[str, Any]:
        """Owner 13 Nov (exchange-side restart reconciliation). After a restart the OMS asks every venue for its account (`venues[name].account_snapshot(since_ns)` -> VenueAccountSnapshot: orders open and
        closed, executions, positions), books what happened while it was down ONCE, and takes no new risk until the venue and the OMS agree. Closes the restart_reconcile_gate first, so a quote cannot go out
        while the fetch is under way.
        For each recovered open plan the venue's order state is put through reconcile_plan (cumulative, so a fill is booked once however often this runs; the hedge a missed maker fill triggers is sent there).
        Executions are de-duplicated by execution id and must add up, per order, to the venue's own order state. A venue order whose client id is no plan's is NOT cancelled (it may not be ours): it is flagged and
        holds new risk. A venue whose snapshot cannot be fetched stays unreconciled. The venue position is compared with the ledger last, less what this call itself sent. `books` are the live books (the hedge of a missed maker fill is priced on them). Returns what was found and done; every
        step is a ledger event."""
        self.restart_reconcile_gate = True
        self._restart_account_problems = {}
        if books:
            self._books.update(books)                                      # the hedge of a missed maker fill needs the hedge venue's book (a running session already has them)
        open_plans = [p for p in self.plans.values() if p.state != PlanState.CLOSED]
        since_ns = min((p.opportunity.observed_ts_ns for p in open_plans), default=self._last_execution_ts_ns)
        since_basis = "open_plans" if open_plans else "last_persisted_state"
        if since_ns <= 0:                                                  # nothing was ever persisted: ask for the default lookback, never from the epoch (a venue keeps no such history and the fetch would refuse)
            since_ns, since_basis = max(0, now_ns - self.restart_default_lookback_ns), "default_lookback"
        snapshots: dict[str, VenueAccountSnapshot] = {}
        gap_ns = now_ns - since_ns
        gap_exceeded = gap_ns > self.restart_default_lookback_ns
        if gap_exceeded:                                                   # owner 13 Nov: a gap longer than the lookback is not reconciled automatically; it is reported and the gate stays closed (an operator reconciles, or raises the lookback knowingly)
            note = f"restart_gap_exceeds_lookback:{gap_ns / 3.6e12:.1f}h>{self.restart_default_lookback_ns / 3.6e12:.1f}h"
            for name in venues:
                self._restart_account_problems.setdefault(name, []).append(note)
            self.ledger.record("RESTART_GAP_EXCEEDS_LOOKBACK", now_ns, gap_ns=gap_ns, lookback_ns=self.restart_default_lookback_ns, since_ns=since_ns, since_basis=since_basis, venues=sorted(venues))
        for name, venue in ({} if gap_exceeded else venues).items():
            try:
                snap = venue.account_snapshot(since_ns)
            except ValueError as exc:                                    # the adapter refused: the window is beyond what the venue keeps (or a hedge-mode account): not a transient failure, named as such
                snap = None
                self._restart_account_problems.setdefault(name, []).append(f"account_snapshot_refused:{str(exc)[:100]}")
            except Exception as exc:                                     # noqa: BLE001 - any fetch failure keeps the gate closed
                snap = None
                self._restart_account_problems.setdefault(name, []).append(f"account_fetch_failed:{type(exc).__name__}")
            if snap is None:
                self._restart_account_problems.setdefault(name, ["account_fetch_failed:none"])
                continue
            snapshots[name] = snap
        known_orders = {o.order_id for p in self.plans.values() for o in p.children}
        booked_before = {o.order_id: o.filled_quantity for p in open_plans for o in p.children}
        children_before = {o.order_id for p in self.plans.values() for o in p.children}
        venue_orders = {o.order_id: o for snap in snapshots.values() for o in snap.orders}
        executions: dict[str, dict[str, Any]] = {}
        for snap in snapshots.values():
            for ex in snap.executions:
                executions.setdefault(ex.order_id, {})[ex.execution_id] = ex          # a repeated execution id is the same execution
        executed = {oid: sum((e.quantity for e in rows.values()), Decimal("0")) for oid, rows in executions.items()}
        for name, snap in snapshots.items():
            for oid, order in ((o.order_id, o) for o in snap.orders):
                if oid not in known_orders:
                    self._restart_account_problems.setdefault(name, []).append(f"unknown_venue_order:{oid}")
            for oid in {e.order_id for e in snap.executions} - known_orders:
                self._restart_account_problems.setdefault(name, []).append(f"execution_for_unknown_order:{oid}")
        booked_executions = 0
        for plan in open_plans:
            mine = {o.order_id for o in plan.children}                           # before reconcile_plan: a hedge it sends is not part of what the venue could have reported
            plan_orders = {}
            for oid in mine & set(venue_orders):
                snap = venue_orders[oid]
                if snap.filled_quantity > 0 and snap.average_price == 0 and oid in executions:          # a venue that gives no average: the volume-weighted price of its executions
                    rows = executions[oid].values()
                    snap = replace(snap, average_price=sum((e.quantity * e.price for e in rows), Decimal("0")) / sum((e.quantity for e in rows), Decimal("1")))
                plan_orders[oid] = snap
            plan_executed = {oid: executed.get(oid, Decimal("0")) for oid in mine & set(venue_orders)} if executions else None
            self.reconcile_plan(plan.parent_id, plan_orders, now_ns, venue_executions=plan_executed)
            if (plan.parent_id in self._restart_unreconciled and set(plan_orders) >= mine                    # a plan whose every order the venue reported and whose account agrees (an order still resting, say) is reconciled: it need not be finished
                    and not self._reconciliation_disagreements(plan, plan_orders, plan_executed, None)):
                self._restart_unreconciled.discard(plan.parent_id)
                self.ledger.record("RESTART_PLAN_RECONCILED_LIVE", now_ns, parent_id=plan.parent_id, plan_state=plan.state.value)
            booked_executions += sum(1 for oid in plan_orders if plan_orders[oid].filled_quantity > booked_before.get(oid, Decimal("0")))
        # what this call itself sent (a hedge for a missed maker fill) is not in the venue's position as fetched
        sent_here: dict[tuple[str, str], Decimal] = {}
        for plan in self.plans.values():
            for o in plan.children:
                if o.order_id not in children_before and o.filled_quantity:
                    key = (o.venue, o.instrument_id)
                    sent_here[key] = sent_here.get(key, Decimal("0")) + (o.filled_quantity if o.side == Side.BUY else -o.filled_quantity)
        for name, snap in snapshots.items():
            for instrument_id, position in snap.positions.items():
                key = (name, instrument_id)
                expected = self.ledger.positions.get(key, Decimal("0")) - sent_here.get(key, Decimal("0"))
                if expected != position:
                    self._restart_account_problems.setdefault(name, []).append(f"venue_position_differs:{instrument_id}")
        for plan in open_plans:                                           # a plan the venue's account did not settle stays unreconciled
            if plan.parent_id in self._restart_unreconciled:
                for name in {o.venue for o in plan.children}:
                    self._restart_account_problems.setdefault(name, []).append(f"plan_unreconciled:{plan.parent_id}")
        report = {"venues_fetched": sorted(snapshots), "since_ns": since_ns, "since_basis": since_basis, "gap_hours": round(gap_ns / 3.6e12, 3), "recovered_open_plans": len(open_plans), "orders_with_a_booked_fill_here": booked_executions,
                  "problems": {k: sorted(set(v)) for k, v in self._restart_account_problems.items()}, "gate_open": not (self._restart_unreconciled or self._restart_account_problems)}
        self.ledger.record("RESTART_ACCOUNT_RECONCILIATION", now_ns, **{k: v for k, v in report.items() if k != "since_ns"}, since_ns=since_ns)
        return report

    def _reconciliation_disagreements(
        self, plan: ParentPlan, venue_orders: dict[str, VenueOrderSnapshot], venue_executions: dict[str, Decimal] | None,
        venue_positions: dict[tuple[str, str], Decimal] | None,
    ) -> list[str]:
        """What the venue's account says that the OMS does not (owner 12 Nov, late success): empty when client order ids, exchange order ids, execution reports and the venue position all agree. Checked only for what was supplied."""
        if venue_executions is None and venue_positions is None:
            return []
        out: list[str] = []
        ours = {o.order_id: o for o in plan.children}
        for oid, snap in venue_orders.items():
            if oid not in ours and snap.venue_order_id and any(o.venue == snap.venue for o in ours.values()):
                out.append(f"client_order_id_unknown:{oid}")
        known_exchange_ids = {}
        for o in plan.children:
            for row in o.state_updates:
                if row.get("venue_order_id"):
                    known_exchange_ids.setdefault(o.order_id, set()).add(str(row["venue_order_id"]))
        for oid, snap in venue_orders.items():
            ids = known_exchange_ids.get(oid)
            if ids and snap.venue_order_id and snap.venue_order_id not in ids:
                out.append(f"exchange_order_id_differs:{oid}")
        for oid, executed in (venue_executions or {}).items():
            order, snap = ours.get(oid), venue_orders.get(oid)
            if order is None:
                out.append(f"execution_for_unknown_order:{oid}")
            elif snap is not None and snap.filled_quantity != executed:
                out.append(f"executions_differ_from_venue_order_state:{oid}")
            elif order.filled_quantity != executed:
                out.append(f"executions_differ_from_booked:{oid}")
        for key, position in (venue_positions or {}).items():
            if self.ledger.positions.get(key, Decimal("0")) != position:
                out.append(f"venue_position_differs:{key[0]}")
        return out

    def reconcile_plan(
        self,
        parent_id: str,
        venue_orders: dict[str, VenueOrderSnapshot],
        now_ns: int,
        venue_executions: dict[str, Decimal] | None = None,
        venue_positions: dict[tuple[str, str], Decimal] | None = None,
    ) -> ParentPlan:
        """Converge a recovered local plan through canonical REST order updates.
        Owner 12 Nov (late success): when the venue's execution reports (`venue_executions`: client order id -> executed quantity) and its position (`venue_positions`: (venue, instrument) -> position) are given, the
        recovered plan is released from `restart_reconcile_gate` only when FOUR things agree: the client order ids (every order the venue reports is one of the plan's), the exchange order ids (none contradicts the one
        already known), the execution reports (their sum per order equals the venue's order state AND what the OMS booked) and the venue position (equals the ledger's). A disagreement keeps the gate closed."""
        try:
            plan = self.plans[parent_id]
        except KeyError as exc:
            raise ValueError(f"unknown parent plan {parent_id}") from exc
        unresolved = []
        for order in plan.children:
            snapshot = venue_orders.get(order.order_id)
            if snapshot is None:
                unresolved.append(order.order_id)
                continue
            if snapshot.venue != order.venue:
                raise ValueError(f"venue mismatch for recovered order {order.order_id}")
            if snapshot.instrument_id != order.instrument_id:
                raise ValueError(f"instrument mismatch for recovered order {order.order_id}")
            event_type = {
                OrderState.ACKNOWLEDGED: ExecutionEventType.ACKNOWLEDGED,
                OrderState.PARTIAL: ExecutionEventType.PARTIAL,
                OrderState.FILLED: ExecutionEventType.FILLED,
                OrderState.REJECTED: ExecutionEventType.REJECTED,
                OrderState.TIMED_OUT: ExecutionEventType.TIMED_OUT,
                OrderState.CANCELLED: ExecutionEventType.CANCELLED,
                OrderState.CREATED: ExecutionEventType.ACKNOWLEDGED,
                # A venue never reports these (they are OMS-side); an order it holds is working.
                OrderState.SENT: ExecutionEventType.ACKNOWLEDGED,
                OrderState.CANCEL_PENDING: ExecutionEventType.ACKNOWLEDGED,
                OrderState.AMEND_PENDING: ExecutionEventType.ACKNOWLEDGED,
            }[snapshot.state]
            update_ts = snapshot.observed_ts_ns or now_ns
            self.ingest_order_update(CanonicalOrderUpdate(
                update_id=stable_id(
                    "rest-order-update", order.order_id, snapshot.venue_order_id,
                    snapshot.state.value, snapshot.filled_quantity, update_ts,
                ),
                source=OrderUpdateSource.REST_QUERY,
                parent_order_id=parent_id, order_id=order.order_id,
                venue=order.venue, instrument_id=order.instrument_id,
                state=event_type, event_ts_ns=update_ts, observed_ts_ns=now_ns,
                cumulative_filled_quantity=snapshot.filled_quantity,
                average_price=snapshot.average_price, fee=snapshot.fee,
                venue_order_id=snapshot.venue_order_id,
            ))

        for order in plan.children:
            if order.filled_quantity and order.fee == 0:
                self._price_fills(order, now_ns)
                self.ledger.apply_fill(order, now_ns)
        self._recompute_plan_economics(plan, now_ns)
        disagreements = self._reconciliation_disagreements(plan, venue_orders, venue_executions, venue_positions)
        if disagreements:
            self.ledger.record("RECONCILIATION_DISAGREEMENT", now_ns, parent_id=parent_id, disagreements=disagreements, gate_stays_closed=True)
            self._restart_unreconciled.add(parent_id)
        if not disagreements and plan.state == PlanState.CLOSED and all(o.state in TERMINAL_ORDER_STATES for o in plan.children if o.order_id in unresolved):
            self._restart_unreconciled.discard(parent_id)          # the venue's account was applied and the fills it carried have already closed the plan (the hedge they triggered included: it is terminal here, the venue was not asked about it)
        if unresolved:
            self._persist(
                plan, now_ns, "RECONCILIATION_PENDING", unresolved_order_ids=unresolved
            )
            return plan
        if not all(order.state in TERMINAL_ORDER_STATES for order in plan.children):
            self._persist(
                plan, now_ns, "RECONCILIATION_PENDING",
                unresolved_order_ids=[],
                nonterminal_order_ids=[
                    order.order_id for order in plan.children
                    if order.state not in TERMINAL_ORDER_STATES
                ],
            )
            return plan
        self._persist(
            plan, now_ns, "VENUE_RECONCILED",
            reconciled_order_ids=sorted(venue_orders),
        )
        result = self._finalize_plan(plan, now_ns, self._books)
        if not disagreements:
            self._restart_unreconciled.discard(parent_id)
        self.ledger.record(
            "PLAN_RECONCILED_FROM_VENUE",
            now_ns,
            parent_id=parent_id,
            residual_base=plan.residual_base,
            net_pnl=plan.net_pnl,
        )
        return result
