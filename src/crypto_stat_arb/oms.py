from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import json
from pathlib import Path
from collections.abc import Callable

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
    stable_id,
)
from .orderbook import OrderBook
from .risk import (
    PreTradeDecision, ReconciliationReport, RiskEngine, VenueRiskSnapshot,
)
from .alignment import CrossVenueEligibilityPolicy
from .plan_store import ParentPlanStore, VenueOrderSnapshot
from .execution import (
    CanonicalOrderUpdate,
    CanonicalOrderRequest,
    ChildDispatchMeasurement,
    DEFERRED_ACTIONS,
    DispatchPolicy,
    OrderTimeoutPolicy,
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
}
ORDER_STATE_RANK = {
    OrderState.CREATED: 0,
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
    def fill(self, order: ChildOrder, book: OrderBook) -> None:
        filled, average_price = book.walk(order.side, order.quantity, order.limit_price)
        order.filled_quantity = filled
        order.average_price = average_price
        if filled == 0:
            order.state = OrderState.REJECTED
            order.reject_reason = "no executable liquidity"
        elif filled < order.quantity:
            order.state = OrderState.PARTIAL
        else:
            order.state = OrderState.FILLED


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
    ):
        self.instruments = instruments
        self.risk = risk
        self.ledger = ledger
        if getattr(risk, "ledger", None) is None:
            risk.ledger = ledger  # live balances for inventory admission
        self.matcher = matcher or DeterministicMatcher()
        self.router = router or StrategyDirectedRouter(instruments)
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
        self._late_fill_alerted: set[str] = set()
        self._residual_breach_since: dict[str, int] = {}
        self._residual_next_check: dict[str, int] = {}
        self._deferred_close: dict[str, tuple[str, tuple[str, ...]]] = {}
        self.realism_counters: dict[str, int] = {
            "unknown_state_orders": 0,
            "venue_queries": 0,
            "resolved_by_query": 0,
            "unresolved_after_queries": 0,
            "close_deferrals": 0,
            "rejected_cancels_requeried": 0,
            "new_risk_held_residual_breach": 0,
            "residual_breach_kill_escalations": 0,
            "resting_fill_reports": 0,
        }
        self._books: dict[tuple[str, str], OrderBook] = {}
        self._legging_bound_codes: dict[str, tuple[str, ...]] = {}
        self._last_execution_ts_ns = 0
        self.dispatch_measurements: list[ChildDispatchMeasurement] = []
        self.eligibility_policy = eligibility_policy
        self.alignment_rejections = 0
        self.plans: dict[str, ParentPlan] = (
            self.plan_store.load_all() if self.plan_store is not None else {}
        )
        self.recovered_plans = sum(
            plan.state != PlanState.CLOSED for plan in self.plans.values()
        )
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
                if plan.state in {PlanState.CREATED, PlanState.CLOSED}:
                    continue
                try:
                    reservation = self.risk.rehydrate_reservation(plan.opportunity)
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
        for plan in self.plans.values():
            self._recompute_plan_economics(plan, now_ns)
            for order in plan.children:
                if venue is not None and order.venue != venue:
                    continue
                if preserve_risk_reducing and (
                    order.reduce_only or self._order_reduces_residual(plan, order)
                ):
                    continue
                if order.state in (OrderState.CREATED, OrderState.ACKNOWLEDGED, OrderState.PARTIAL):
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
                    callbacks = self.router.route(request)
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
    ) -> ReconciliationReport:
        """Run the RMS release gate against testnet REST/private-stream state."""
        was_active = self.risk.kill_switch
        report = self.risk.reconcile(
            plans=self.plans, ledger=self.ledger, now_ns=now_ns,
            venue_orders=venue_orders, venue_risk=venue_risk,
            require_venue_orders=require_venue_orders,
            require_complete_opportunities=require_complete_opportunities,
            position_tolerance=position_tolerance,
            balance_tolerance=balance_tolerance,
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

    def write_margin_status(
        self,
        path: Path,
        *,
        now_ns: int,
        mark_prices: dict[tuple[str, str], Decimal] | None = None,
        initial_capital_by_venue: dict[str, Decimal] | None = None,
        venue_risk: tuple[VenueRiskSnapshot, ...] = (),
    ) -> dict[str, object]:
        was_active = self.risk.kill_switch
        report = self.risk.evaluate_margin(
            ledger=self.ledger, now_ns=now_ns, mark_prices=mark_prices,
            initial_capital_by_venue=initial_capital_by_venue,
            instruments=self.instruments, venue_risk=venue_risk,
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
            if plan.state != PlanState.CLOSED and plan.residual_base != 0
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
            "carried_residual_base": str(self.carried_residual_base()),
            # Net unhedged base across open and closed plans: what the account
            # actually holds because legs did not pair up.
            "total_residual_base": str(abs(
                self.carried_residual_base()
                + sum((plan.residual_base for plan in open_residuals), Decimal("0"))
            )),
            "open_residual_base": str(sum(
                (abs(plan.residual_base) for plan in open_residuals), Decimal("0")
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
        }

    def _recompute_plan_economics(
        self, plan: ParentPlan, ts_ns: int | None = None
    ) -> None:
        plan.residual_base = sum(
            order.filled_quantity if order.side == Side.BUY else -order.filled_quantity
            for order in plan.children
        )
        plan.gross_pnl = sum(
            -order.filled_quantity * order.average_price
            if order.side == Side.BUY
            else order.filled_quantity * order.average_price
            for order in plan.children
        )
        plan.fees = sum(order.fee for order in plan.children)
        if plan.residual_base:
            plan.residual_mark_price = self._residual_mark_price(plan)
            plan.residual_mark_value = plan.residual_base * plan.residual_mark_price
        else:
            plan.residual_mark_price = Decimal("0")
            plan.residual_mark_value = Decimal("0")
        plan.net_pnl = (
            plan.gross_pnl + plan.residual_mark_value - plan.fees - plan.unwind_cost
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

    def carried_residual_base(self, exclude: ParentPlan | None = None) -> Decimal:
        """Signed residual left open by closed plans (derived, so restart-safe)."""
        return sum(
            (
                plan.residual_base for plan in self.plans.values()
                if plan.state == PlanState.CLOSED and plan is not exclude
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
                and order.state in {OrderState.CREATED, OrderState.ACKNOWLEDGED}
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
        if side == Side.BUY:
            raw_price = book.best_ask * (Decimal("1") + multiplier)
            rounding = ROUND_CEILING
        else:
            raw_price = book.best_bid * (Decimal("1") - multiplier)
            rounding = ROUND_FLOOR
        price = (
            (raw_price / spec.tick_size).to_integral_value(rounding=rounding)
            * spec.tick_size
        )
        return price, OrderType.LIMIT, TimeInForce.IOC, False

    def _unwind_venues(self, plan: ParentPlan) -> tuple[str, ...]:
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
            and order.state in {
                OrderState.CREATED, OrderState.ACKNOWLEDGED, OrderState.PARTIAL
            }
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

    def _execute_residual_unwind(
        self, plan: ParentPlan, ts_ns: int,
        books: dict[tuple[str, str], OrderBook],
    ) -> bool:
        """Dispatch reduce-only children until residual is bounded or work is pending."""
        policy = self.residual_policy
        if self._residual_within_bound(plan):
            return True
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
        for venue in venues:
            if plan.unwind_attempts >= policy.max_unwind_attempts:
                break
            book = books.get((venue, plan.opportunity.instrument_id))
            if book is None or not book.is_tradable:
                self._residual_event(
                    plan, "UNWIND_VENUE_SKIPPED", ts_ns,
                    venue=venue,
                    reason=("book_unavailable" if book is None else "book_not_tradable"),
                )
                continue
            quantity = self.instruments.round_quantity(
                venue, plan.opportunity.instrument_id, abs(plan.residual_base), ts_ns
            )
            spec = self.instruments.get(venue, plan.opportunity.instrument_id, ts_ns)
            reference_price = book.best_ask if plan.residual_base < 0 else book.best_bid
            if quantity < spec.min_quantity or quantity * reference_price < spec.min_notional:
                self._residual_event(
                    plan, "UNWIND_VENUE_SKIPPED", ts_ns, venue=venue,
                    reason="residual_below_venue_minimum", quantity=quantity,
                )
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
                order.fee = self._fee_for(order, callback_ts)
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
            if order.state in {
                OrderState.CREATED, OrderState.ACKNOWLEDGED, OrderState.PARTIAL
            }:
                return False
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
            return plan
        self._transition(plan, PlanState.RECONCILED, ts_ns)
        self._transition(plan, PlanState.CLOSED, ts_ns)
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
        if plan.state == PlanState.CLOSED:
            return plan
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
        self._recompute_plan_economics(plan, ts_ns)
        self._assess_legging_risk(plan, ts_ns, books)
        primary = [order for order in plan.children if order.order_role == "PRIMARY"]
        all_filled = all(
            order.state == OrderState.FILLED for order in primary
        )
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
                self._residual_event(
                    plan, "RESIDUAL_LIMIT_BREACH", ts_ns,
                    max_residual_base=self.residual_policy.max_residual_base,
                    elapsed_ns=elapsed_ns,
                    max_residual_age_ns=self.residual_policy.max_residual_age_ns,
                    unwind_attempts=plan.unwind_attempts,
                )
                self._escalate_residual_breach(plan, ts_ns)
                return plan
            if self.router.has_pending_parent(plan.parent_id):
                return plan
        self._residual_breach_since.pop(plan.parent_id, None)
        working = [
            order for order in plan.children
            if order.state in {
                OrderState.CREATED, OrderState.ACKNOWLEDGED, OrderState.PARTIAL
            }
        ]
        if working:
            self._cancel_working_orders(plan, working, ts_ns)
            still_working = tuple(
                order.order_id for order in plan.children
                if order.state in {
                    OrderState.CREATED, OrderState.ACKNOWLEDGED, OrderState.PARTIAL
                }
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
        return tuple(affected)

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
                    plan, order, request, "SUBMIT", "RELEASED", now_ns,
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
                order.fee = self._fee_for(order, fill_ts)
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

    def _fee_for(self, order: ChildOrder, ts_ns: int) -> Decimal:
        liquidity = Liquidity.MAKER if order.post_only else Liquidity.TAKER
        return self.instruments.fee(
            order.venue, order.instrument_id,
            order.filled_quantity * order.average_price, liquidity, ts_ns,
        )

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

    def _watch_order(self, order: ChildOrder, ts_ns: int, *, immediate: bool = False) -> None:
        if immediate:
            self._requery_orders.add(order.order_id)
        if order.order_id in self._report_deadlines:
            if immediate:
                self._report_deadlines[order.order_id][0] = ts_ns
            return
        deadline = ts_ns if immediate else ts_ns + self.order_timeout_policy.report_timeout_ns
        self._report_deadlines[order.order_id] = [deadline, 0, order.parent_id]
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
            if order.time_in_force == TimeInForce.GTC.value and order.state in {
                OrderState.ACKNOWLEDGED, OrderState.PARTIAL,
            }:
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
            order.fee = self._fee_for(order, ts_ns)
            self.ledger.apply_fill(order, ts_ns)
            self._update_exposure_control(ts_ns)

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
                and order.state in {OrderState.ACKNOWLEDGED, OrderState.PARTIAL}
                and order_id not in self._requery_orders
            ):
                self._report_deadlines.pop(order_id, None)
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
            if status is not None:
                self._apply_venue_status(plan, order, status, query_ts)
                if order.state in TERMINAL_ORDER_STATES or (
                    order.time_in_force == TimeInForce.GTC.value
                    and order.state in {OrderState.ACKNOWLEDGED, OrderState.PARTIAL}
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
        return min(candidates) if candidates else None

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
            steps += 1
            if steps >= max_steps:
                raise RuntimeError("EMS timers did not converge")
        self.enforce_residual_limits(now_ns, only_due=True)
        return steps

    def on_book_update(self, venue: str, instrument_id: str, now_ns: int) -> int:
        book = self._books.get((venue, instrument_id))
        if book is None:
            return 0
        produced = self.router.on_book_update(venue, book, now_ns)
        if produced:
            self.process_throttled_routes(now_ns)
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
        states = {order.state for order in plan.children}
        if all(state == OrderState.FILLED for state in states):
            target = PlanState.FILLED
        elif OrderState.REJECTED in states or OrderState.CANCELLED in states:
            target = PlanState.REJECTED
        elif OrderState.TIMED_OUT in states:
            target = PlanState.TIMED_OUT
        elif any(state in (OrderState.PARTIAL, OrderState.FILLED) for state in states):
            target = PlanState.PARTIAL
        elif OrderState.ACKNOWLEDGED in states:
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
        winner = max(order.state_updates, key=self._order_update_key)
        order.state = OrderState(str(winner["resolved_order_state"]))
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
        # Fees never regress: the OMS may have priced a fill before a venue
        # snapshot without fee data arrives.
        order.fee = max(
            order.fee,
            max(Decimal(str(item["fee"])) for item in order.state_updates),
        )
        order.reject_reason = str(winner.get("reason") or "") or None

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
        self._derive_execution_state(plan, update.observed_ts_ns)
        return plan

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

    def execute(
        self,
        opportunity: Opportunity,
        books: dict[tuple[str, str], OrderBook],
        dispatch_ts_ns: int | None = None,
    ) -> ParentPlan:
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
        plan = ParentPlan(parent_id, opportunity, children)
        self.plans[parent_id] = plan
        now = opportunity.observed_ts_ns
        self._transition(plan, PlanState.CREATED, now, event_type="PLAN_CREATED")
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
        if self.eligibility_policy is not None:
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
        try:
            for order in children:
                self.instruments.validate_order(order, now)
            self.risk.carried_residual_base = self.carried_residual_base()
            reserved = self.risk.reserve(opportunity)
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
        for leg_index, order in enumerate(children):
            dispatch_started_ts = self._next_execution_ts(dispatch_base)
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
            risk_reducing = self._order_reduces_residual(plan, order)
            instruction = RoutingInstruction(
                strategy_id=opportunity.strategy_id,
                eligible_venues=(order.venue,),
                route_priority=(order.venue,),
                order_id=order.order_id,
                parent_opportunity_id=order.opportunity_id,
                parent_order_id=order.parent_id,
                instrument_id=order.instrument_id,
                side=order.side,
                price=order.limit_price,
                quantity=order.quantity,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.IOC,
                maker_taker_intent=MakerTakerIntent.TAKER,
                post_only=False,
                reduce_only=False,
                created_ts_ns=dispatch_started_ts,
                idempotency_key=stable_id("submit", order.order_id),
                risk_reducing=risk_reducing,
            )
            routing = self.route_strategy_order(instruction, books)
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
                order.fee = self._fee_for(order, dispatch_completed_ts)
                fill_ts = callback_timestamps[-1] if callback_timestamps else dispatch_completed_ts
                self.ledger.apply_fill(order, fill_ts)
                self._update_exposure_control(fill_ts)
            self._assess_legging_risk(plan, dispatch_completed_ts, books)

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

        return self._finalize_plan(plan, package_completed_ts, books)

    def reconcile_plan(
        self,
        parent_id: str,
        venue_orders: dict[str, VenueOrderSnapshot],
        now_ns: int,
    ) -> ParentPlan:
        """Converge a recovered local plan through canonical REST order updates."""
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
                order.fee = self._fee_for(order, now_ns)
                self.ledger.apply_fill(order, now_ns)
        self._recompute_plan_economics(plan, now_ns)
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
        self.ledger.record(
            "PLAN_RECONCILED_FROM_VENUE",
            now_ns,
            parent_id=parent_id,
            residual_base=plan.residual_base,
            net_pnl=plan.net_pnl,
        )
        return result
