"""One trading stack shared by backtest replay, live paper and the benchmark.

The same books, integrity gate, time monitor, strategy, OMS/EMS, latency
model and risk engine are used in every mode; only the event source and the
venue adapters change. The execution mode is explicit and checked at start-up
so a paper fill can never be labelled as a venue fill (and vice versa).
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict
from enum import StrEnum
from decimal import Decimal
import time
from typing import Any, Callable

from .alignment import CrossVenueEligibilityPolicy
from .book_gate import BookIntegrityGate
from .config import AppConfig
from .execution import StrategyDirectedRouter, VenueExecutionAdapter
from .latency import LatencyModel, latency_model_from_config
from .economics import opening_balance_events
from .borrow import BorrowEngine, BorrowLine, BorrowRecall
from .financing_journal import FinancingJournal
from .funding import FundingEngine, FundingRateBook, FundingSpec, is_perpetual
from .margin import LiquidationEngine, MarginConfig
from .risk import InventoryModel
from .ledger import Ledger
from .models import EventType, MarketEvent, Opportunity, PlanState
from .oms import OMS, instrument_assets
from .orderbook import OrderBook
from .plan_store import ParentPlanStore
from .risk import RiskEngine
from .strategy import CrossVenueSpreadStrategy
from .mean_reversion import CrossVenueMeanReversionStrategy, MeanReversionConfig
from .time_integrity import TimeIntegrityMonitor


class ExecutionMode(StrEnum):
    INTERNAL_SIMULATION = "INTERNAL_SIMULATION"  # recorded data, simulated venue
    INTERNAL_PAPER = "INTERNAL_PAPER"            # live data, simulated venue
    VENUE_TESTNET = "VENUE_TESTNET"              # live data, real testnet orders


def assert_execution_mode(
    mode: ExecutionMode, *, live_data: bool,
    adapters: dict[str, VenueExecutionAdapter] | None, venues: tuple[str, ...],
) -> None:
    """Fail closed on combinations that would mislabel fills."""
    if mode == ExecutionMode.VENUE_TESTNET:
        if not live_data:
            raise ValueError("VENUE_TESTNET requires a live market-data feed")
        if set(adapters or {}) != set(venues):
            raise ValueError("VENUE_TESTNET requires one testnet adapter per venue")
    elif adapters and any(
        type(adapter).__name__.lower().startswith("testnet")
        for adapter in adapters.values()
    ):
        raise ValueError(f"{mode.value} must not use testnet venue adapters")
    if mode == ExecutionMode.INTERNAL_SIMULATION and live_data:
        raise ValueError("live data must run as INTERNAL_PAPER or VENUE_TESTNET")
    if mode == ExecutionMode.INTERNAL_PAPER and not live_data:
        raise ValueError("INTERNAL_PAPER requires live data; replay uses INTERNAL_SIMULATION")


class StageTimer:
    """Bounded wall-clock samples per processing stage (MR-14)."""

    def __init__(self, max_samples: int = 200_000) -> None:
        self._samples: dict[str, deque[int]] = {}
        self._counts: dict[str, int] = {}
        self.max_samples = max_samples

    def add(self, stage: str, value_ns: int) -> None:
        store = self._samples.get(stage)
        if store is None:
            store = self._samples[stage] = deque(maxlen=self.max_samples)
        store.append(max(0, int(value_ns)))
        self._counts[stage] = self._counts.get(stage, 0) + 1

    def report(self) -> dict[str, Any]:
        result = {}
        for stage, values in sorted(self._samples.items()):
            ordered = sorted(values)
            count = len(ordered)
            result[stage] = {
                "count": self._counts[stage],
                "p50_ns": ordered[count // 2],
                "p95_ns": ordered[min(count - 1, int(count * 0.95))],
                "p99_ns": ordered[min(count - 1, int(count * 0.99))],
                "max_ns": ordered[-1],
                "mean_ns": sum(ordered) // count,
            }
        return result


def funding_engine(cfg: AppConfig, ledger: Ledger) -> FundingEngine:
    """FUNDING generator for the configured perpetual venues (none for spot)."""
    section = cfg.funding or {}
    specs = []
    for venue in cfg.venues:
        spec = cfg.instruments.get(venue, cfg.instrument_id)
        if is_perpetual(spec.product_type):
            specs.append(FundingSpec(
                venue=venue, instrument_id=spec.instrument_id,
                contract_asset=spec.instrument_id,
                settlement_asset=spec.settlement_asset or spec.quote,
                multiplier=spec.contract_multiplier, inverse=spec.inverse,
                interval_ns=spec.funding_interval_ns, anchor_ns=spec.funding_anchor_ns,
                rate_cap=spec.funding_rate_cap, rate_floor=spec.funding_rate_floor,
            ))
    rates = FundingRateBook()
    if section.get("rates_path"):
        from pathlib import Path
        rates = FundingRateBook.from_jsonl(Path(section["rates_path"]))
    for number, row in enumerate(section.get("rates", []), 1):
        rates.add_row(row, f"config funding.rates[{number}]")
    max_age = section.get("max_rate_age_ms")
    return FundingEngine(
        ledger, specs, rates,
        settle_lag_ns=int(section.get("settle_lag_ms", 5_000)) * 1_000_000,
        max_rate_age_ns=None if max_age is None else int(max_age) * 1_000_000,
        mark_fallback=str(section.get("mark_fallback", "none")),
        max_abs_rate=Decimal(str(section.get("max_abs_rate", "0.05"))),
    )


def borrow_engine(cfg: AppConfig, ledger: Ledger, on_forced_close=None) -> BorrowEngine:
    """Spot borrow lines (ECO03); with no lines, every spot short is refused."""
    section = cfg.borrow or {}
    spot: dict[tuple[str, str], tuple[str, str]] = {}
    eligible: dict[tuple[str, str], bool] = {}
    for venue in cfg.venues:
        spec = cfg.instruments.get(venue, cfg.instrument_id)
        if not is_perpetual(spec.product_type):
            spot[(venue, spec.instrument_id)] = (spec.base, spec.quote)
            eligible[(venue, spec.base)] = spec.borrow_applicable
    return BorrowEngine(
        ledger, [BorrowLine.from_config(row) for row in section.get("lines", [])],
        spot_instruments=spot, eligible=eligible,
        recalls=[BorrowRecall.from_config(row) for row in section.get("recalls", [])],
        repay_policy=str(section.get("repay_policy", "auto_on_buy")),
        forced_close_fee_bps=Decimal(str(section.get("forced_close_fee_bps", "0"))),
        settle_lag_ns=int(section.get("settle_lag_ms", 5_000)) * 1_000_000,
        on_forced_close=on_forced_close,
        # The legacy unconstrained model sells base it does not hold by design;
        # report those shorts as unfinanced instead of failing the run.
        unfinanced_short_policy=(
            "NOTE" if cfg.inventory_policy.model == InventoryModel.UNCONSTRAINED else "FAIL"
        ),
    )


class TradingSession:
    def __init__(
        self,
        cfg: AppConfig,
        *,
        execution_mode: ExecutionMode = ExecutionMode.INTERNAL_SIMULATION,
        live_data: bool = False,
        adapters: dict[str, VenueExecutionAdapter] | None = None,
        latency_model: LatencyModel | None = None,
        books: dict[tuple[str, str], OrderBook] | None = None,
        plan_store: ParentPlanStore | None = None,
        clock_ns: Callable[[], int] | None = None,
        wall_ns: Callable[[], int] = time.perf_counter_ns,
        strategies: list[Any] | None = None,
    ) -> None:
        execution_mode = ExecutionMode(execution_mode)
        assert_execution_mode(
            execution_mode, live_data=live_data, adapters=adapters, venues=cfg.venues
        )
        self.cfg = cfg
        self.execution_mode = execution_mode
        self.live_data = live_data
        self.clock_ns = clock_ns
        self.wall_ns = wall_ns
        self.ledger = Ledger()
        # Opening balances are economic events (not dict lookups), booked
        # before the OMS can rebuild recovered fills, so as-of balances and
        # the equity reconciliation are reproducible from events alone.
        quote_opening = cfg.inventory_policy.initial_quote_by_venue or cfg.initial_capital_by_venue
        opening_rows = []
        for venue in cfg.venues:
            spec = cfg.instruments.get(venue, cfg.instrument_id)
            opening_rows.append((venue, spec.quote, quote_opening.get(venue, Decimal("0"))))
            if is_perpetual(spec.product_type):
                continue  # no pre-funded contract positions
            opening_rows.append((
                venue, spec.base, cfg.inventory_policy.initial_base_by_venue.get(venue, Decimal("0")),
            ))
        for opening_event in opening_balance_events(opening_rows):
            self.ledger.economics.apply(opening_event)
        # First valid mid per book: the opening marks for equity reconciliation.
        self.opening_marks: dict[tuple[str, str], Decimal] = {}
        self.risk = RiskEngine(
            cfg.risk_limits, cfg.account_risk_snapshots, inventory=cfg.inventory_policy
        )
        self.alignment_policy = CrossVenueEligibilityPolicy(
            cfg.venues, cfg.instrument_id, cfg.max_book_age_ns
        )
        self.latency_model = (
            latency_model if latency_model is not None
            else latency_model_from_config(cfg.latency, cfg.venues)
        )
        # Testnet orders already experience real network latency; the model
        # must not delay them a second time.
        router_latency = (
            None if execution_mode == ExecutionMode.VENUE_TESTNET
            else self.latency_model
        )
        self.measured_latency = LatencyModel(source="measured")
        self.router = StrategyDirectedRouter(
            cfg.instruments, adapters=adapters, latency_model=router_latency
        )
        # Financing engines and their journal come before the OMS: on a restart
        # the OMS re-reserves risk for open plans, and a sell leg financed by a
        # borrow needs the loan (journal) and the borrow line (engine) to exist.
        self.funding = funding_engine(cfg, self.ledger)
        self.borrow = borrow_engine(cfg, self.ledger, self._borrow_forced_close)
        self.risk.borrow = self.borrow
        self.financing_journal: FinancingJournal | None = None
        if plan_store is not None:
            # Journal fills (buy-ins, liquidations) need the same asset mapping
            # the OMS installs, before the OMS exists.
            self.ledger.asset_resolver = lambda venue, instrument_id: instrument_assets(
                cfg.instruments, venue, instrument_id
            )
            self.financing_journal = FinancingJournal(plan_store)
            self.financing_journal.replay(self.ledger)
            for engine in (self.funding, self.borrow):
                engine.journal = self.financing_journal
                engine.restore(self.ledger.economics.events)
        # Fills rebuilt from the plan store are history, not new fills: the
        # engines must not open loans or rebook settlements for them again.
        listeners, self.ledger.trade_fill_listeners = self.ledger.trade_fill_listeners, []
        try:
            self.oms = OMS(
                cfg.instruments, self.risk, self.ledger,
                eligibility_policy=self.alignment_policy, router=self.router,
                residual_policy=cfg.residual_policy, plan_store=plan_store,
                order_timeout_policy=cfg.order_timeout_policy,
            )
        finally:
            self.ledger.trade_fill_listeners = listeners
        self.ledger.journal = self.financing_journal
        # Margin is evaluated during the session, not only at the end (ECO04):
        # on market-data time every ``evaluate_interval_ms`` and after any fill
        # on a margin venue; only where margin applies, so spot runs are unchanged.
        self.margin_config: MarginConfig = cfg.margin or MarginConfig()
        self.risk.margin_config = self.margin_config if self.margin_config.configured else None
        self.margin_venues = {
            venue for venue in cfg.venues
            if venue in self.margin_config.venues
            or is_perpetual(cfg.instruments.get(venue, cfg.instrument_id).product_type)
            or self.risk._account(venue).margin_enabled
        }
        self._margin_due_ns: int | None = None
        self._margin_dirty = False

        self.liquidation = LiquidationEngine(
            self.ledger, self.margin_config,
            adapter_for=self._venue_adapter, on_liquidation=self._liquidation_stop,
        )
        if self.margin_venues:
            self.ledger.trade_fill_listeners.append(self._margin_fill)
            self.liquidation.restore(self.ledger)
        settings = cfg.strategies or {}
        if strategies is None:
            strategies = []
            if settings.get("cross_venue_arb", {}).get("enabled", True):
                strategies.append(CrossVenueSpreadStrategy(
                    cfg.strategy_id, cfg.instrument_id, cfg.venues, cfg.quantity,
                    cfg.entry_bps, cfg.max_book_age_ns, cfg.min_net_edge_bps,
                    cfg.instruments, self.alignment_policy,
                ))
            mean_reversion = settings.get("mean_reversion", {})
            if mean_reversion.get("enabled", False):
                strategies.append(CrossVenueMeanReversionStrategy(
                    MeanReversionConfig.from_config(mean_reversion),
                    cfg.instrument_id, cfg.venues, cfg.instruments,
                    self.alignment_policy,
                ))
        self.strategies = list(strategies)
        self.strategy = self.strategies[0] if self.strategies else None
        self.books = books if books is not None else {
            (venue, cfg.instrument_id): cfg.book_sanity.new_book(venue, cfg.instrument_id)
            for venue in cfg.venues
        }
        self.book_gate = BookIntegrityGate(
            cfg.max_book_age_ns, self.oms.cancel_open_orders,
            venue_control_callback=self.oms.apply_feed_control,
        )
        self.time_monitor = TimeIntegrityMonitor(cfg.time_policy, cfg.venues)
        self.stage_timer = StageTimer()
        self.timestamp_sink: Callable[[dict[str, Any]], None] | None = None
        self.events_processed = 0
        self.opportunities = 0
        self.last_event_ts_ns = 0
        self.last_seen_ts_by_venue: dict[str, int] = {}
        self.opportunities_by_strategy: dict[str, int] = {}
        self._tracked_plans: dict[str, Any] = {}
        self.flattening = False

    def _now(self, event: MarketEvent, now_ns: int | None) -> int:
        if now_ns is not None:
            return now_ns
        if self.clock_ns is not None:
            return int(self.clock_ns())
        return event.receive_ts_ns

    def on_event(
        self, event: MarketEvent, *, now_ns: int | None = None,
        apply_book: bool = True, gate_allowed: bool | None = None,
    ) -> Opportunity | None:
        wall_start = self.wall_ns()
        now = self._now(event, now_ns)
        self.events_processed += 1
        self.last_event_ts_ns = max(self.last_event_ts_ns, now)
        self.last_seen_ts_by_venue[event.venue] = event.receive_ts_ns
        assessment = self.time_monitor.observe(event, now_ns=now)
        if assessment.blocking:
            self.ledger.record(
                "TIME_INTEGRITY_BLOCK", event.receive_ts_ns,
                event_id=event.event_id, flags=assessment.flags,
            )
            # A timestamp anomaly blocks *decisions* on this event, not the
            # sequenced book data: live, the feed service applies every delta
            # before the session sees it. Skipping the delta here would open a
            # sequence gap that keeps the replay book invalid until the venue's
            # next snapshot (minutes on Bybit), so replay must apply it too.
            if apply_book and event.event_type in (EventType.BOOK_SNAPSHOT, EventType.BOOK_DELTA):
                self.books[(event.venue, event.instrument_id)].apply(event)
            return None
        if event.event_type not in (EventType.BOOK_SNAPSHOT, EventType.BOOK_DELTA):
            return None
        feed_latency = assessment.latency_ns - (
            self.time_monitor.clock_offset_estimate_ns or 0
        )
        if feed_latency >= 0:
            self.measured_latency.observe(event.venue, "market_data", feed_latency)
        self.oms.advance_time(now, self.books)
        if apply_book:
            self.books[(event.venue, event.instrument_id)].apply(event)
        self.oms.on_book_update(event.venue, event.instrument_id, now)
        book_key = (event.venue, event.instrument_id)
        if book_key not in self.opening_marks:
            book = self.books.get(book_key)
            if book is not None and book.state.value == "VALID" and book.bids and book.asks:
                self.opening_marks[book_key] = (book.best_bid + book.best_ask) / Decimal("2")
        if self.funding.configured:
            book = self.books.get(book_key)
            mid = (
                (book.best_bid + book.best_ask) / Decimal("2")
                if book is not None and book.state.value == "VALID" and book.bids and book.asks
                else None
            )
            self.funding.observe(event.venue, event.instrument_id, now, mid)
            self.funding.settle_until(now)
        book = self.books.get(book_key)
        if book is not None and book.state.value == "VALID" and book.bids and book.asks:
            self.borrow.observe(
                event.venue, event.instrument_id, now,
                (book.best_bid + book.best_ask) / Decimal("2"), book.best_ask,
            )
        else:
            self.borrow.observe(event.venue, event.instrument_id, now, None)
        if self.borrow.configured:
            self.borrow.process(now)
            self.borrow.settle_until(now)
        if self.margin_venues:
            self.oms.retry_flatten(event.venue, now)
            if (event.venue, event.instrument_id) in self.liquidation.active:
                # A new book update: keep liquidating the residual until flat.
                result = self.liquidation.liquidate(
                    event.venue, event.instrument_id, self.books.get(book_key), now,
                    reason="continuing liquidation of the residual",
                )
                if result is not None:
                    self._margin_dirty = True
                    self._flatten_surviving_leg(result, now)
            self._evaluate_margin(now)
        self._notify_closed_plans()
        if gate_allowed is None:
            required = tuple(
                self.books[(venue, self.cfg.instrument_id)] for venue in self.cfg.venues
            )
            gate_allowed = self.book_gate.allow(required, now)
        if not gate_allowed:
            return None
        wall_strategy = self.wall_ns()
        self.stage_timer.add("feed_to_strategy_ns", wall_strategy - wall_start)
        last: Opportunity | None = None
        for strategy in self.strategies:
            if self.flattening and not hasattr(strategy, "force_exit"):
                continue
            started = self.wall_ns()
            opportunity = strategy.evaluate(self.books, now)
            decided = self.wall_ns()
            self.stage_timer.add("strategy_eval_ns", decided - started)
            if opportunity is None:
                continue
            self._dispatch(strategy, opportunity, now, decided, event)
            last = opportunity
        return last

    def _dispatch(
        self, strategy: Any, opportunity: Opportunity, now: int,
        wall_decision: int, event: MarketEvent | None = None,
    ):
        self.opportunities += 1
        strategy_id = opportunity.strategy_id
        self.opportunities_by_strategy[strategy_id] = (
            self.opportunities_by_strategy.get(strategy_id, 0) + 1
        )
        decision_ts = self.clock_ns() if self.clock_ns is not None else now
        self.ledger.record("OPPORTUNITY", now, opportunity=asdict(opportunity))
        dispatch_ts = None
        if (
            self.execution_mode == ExecutionMode.INTERNAL_SIMULATION
            and self.latency_model is not None
        ):
            dispatch_ts = (
                now
                + self.latency_model.draw(opportunity.buy_venue, "strategy")
                + self.latency_model.draw(opportunity.buy_venue, "risk")
            )
        plan = self.oms.execute(opportunity, self.books, dispatch_ts_ns=dispatch_ts)
        if hasattr(strategy, "on_plan_closed"):
            self._tracked_plans[plan.parent_id] = strategy
        wall_routed = self.wall_ns()
        self.stage_timer.add("route_decision_ns", wall_routed - wall_decision)
        route_ts = self.clock_ns() if self.clock_ns is not None else (dispatch_ts or now)
        receive_ts = event.receive_ts_ns if event is not None else now
        if self.live_data:
            self.measured_latency.observe(
                opportunity.buy_venue, "strategy", max(0, decision_ts - receive_ts)
            )
            self.measured_latency.observe(
                opportunity.buy_venue, "risk", max(0, route_ts - decision_ts)
            )
        if self.timestamp_sink is not None:
            self.timestamp_sink({
                "record_type": "DECISION",
                "execution_mode": self.execution_mode.value,
                "strategy_id": strategy_id,
                "event_id": event.event_id if event is not None else None,
                "venue": event.venue if event is not None else opportunity.buy_venue,
                "exchange_ts_ns": event.exchange_ts_ns if event is not None else None,
                "receive_ts_ns": receive_ts,
                "decision_ts_ns": decision_ts,
                "route_decision_ts_ns": route_ts,
                "opportunity_id": opportunity.opportunity_id,
                "parent_id": plan.parent_id,
                "plan_state": plan.state.value,
                "buy_venue": opportunity.buy_venue,
                "sell_venue": opportunity.sell_venue,
                "quantity": str(opportunity.quantity),
                "expected_net_edge_bps": str(opportunity.estimated_net_edge_bps),
            })
        self._notify_closed_plans()
        return plan

    def _notify_closed_plans(self) -> None:
        if not self._tracked_plans:
            return
        for parent_id, strategy in list(self._tracked_plans.items()):
            plan = self.oms.plans.get(parent_id)
            if plan is None or plan.state != PlanState.CLOSED:
                continue
            del self._tracked_plans[parent_id]
            strategy.on_plan_closed(plan)

    def tick(self, now_ns: int) -> int:
        """Run due timers without a market event (live idle periods)."""
        steps = self.oms.advance_time(now_ns, self.books)
        self._notify_closed_plans()
        if self.flattening:
            self.flatten_strategies(now_ns)
        return steps

    def enter_flatten(self, now_ns: int) -> int:
        """Stop new entries and close open strategy stances (before shutdown)."""
        self.flattening = True
        for strategy in self.strategies:
            if hasattr(strategy, "allow_entries"):
                strategy.allow_entries = False
        return self.flatten_strategies(now_ns)

    def flatten_strategies(self, now_ns: int) -> int:
        sent = 0
        for strategy in self.strategies:
            force_exit = getattr(strategy, "force_exit", None)
            if force_exit is None:
                continue
            opportunity = force_exit(self.books, now_ns)
            if opportunity is not None:
                self._dispatch(strategy, opportunity, now_ns, self.wall_ns())
                sent += 1
        return sent

    def strategy_report(self) -> dict[str, Any]:
        report: dict[str, Any] = {}
        for strategy in self.strategies:
            strategy_id = strategy.strategy_id
            plans = [
                plan for plan in self.oms.plans.values()
                if plan.opportunity.strategy_id == strategy_id
            ]
            outcomes: dict[str, int] = {}
            for plan in plans:
                key = plan.terminal_outcome or plan.state.value
                outcomes[key] = outcomes.get(key, 0) + 1
            summary = (
                strategy.summary() if hasattr(strategy, "summary")
                else {"type": type(strategy).__name__}
            )
            report[strategy_id] = {
                "type": summary.get("type", type(strategy).__name__),
                "opportunities": self.opportunities_by_strategy.get(strategy_id, 0),
                "plans": len(plans),
                "closed_plans": sum(plan.state == PlanState.CLOSED for plan in plans),
                "outcomes": dict(sorted(outcomes.items())),
                "gross_pnl": str(sum((plan.gross_pnl for plan in plans), start=0)),
                "fees": str(sum((plan.fees for plan in plans), start=0)),
                "net_pnl": str(sum((plan.net_pnl for plan in plans), start=0)),
                "summary": summary,
            }
        return {"flattening": self.flattening, "strategies": report}

    def drain(self, horizon_ns: int | None = None) -> dict[str, Any]:
        horizon = self.cfg.drain_horizon_ns if horizon_ns is None else horizon_ns
        result = self.oms.drain(self.last_event_ts_ns + horizon)
        self.finalize_funding()
        return result

    def finalize_funding(self) -> None:
        """Settle everything up to the last market event, never beyond the data."""
        if self.last_event_ts_ns:
            self.funding.settle_until(self.last_event_ts_ns, final=True)
            if self.borrow.configured:
                self.borrow.process(self.last_event_ts_ns)
                self.borrow.settle_until(self.last_event_ts_ns, final=True)

    # -- margin (ECO04) -------------------------------------------------------------
    def _venue_adapter(self, venue: str):
        from .execution import DeterministicVenueAdapter

        return self.router.adapters.setdefault(venue, DeterministicVenueAdapter(venue))

    def _margin_fill(self, event) -> None:
        if event.venue in self.margin_venues:
            self._margin_dirty = True

    def _liquidation_stop(self, reason: str, now_ns: int) -> None:
        if not self.risk.kill_switch:
            self.oms.activate_kill_switch(
                reason=reason, now_ns=now_ns, operator_id="liquidation-engine", source="LIQUIDATION",
            )

    def _evaluate_margin(self, now: int) -> None:
        due = self._margin_due_ns is None or now >= self._margin_due_ns
        if not (due or self._margin_dirty):
            return
        self._margin_dirty = False
        self._margin_due_ns = now + self.margin_config.evaluate_interval_ns
        marks = {
            key: (book.best_bid + book.best_ask) / Decimal("2")
            for key, book in self.books.items()
            if book.state.value == "VALID" and book.bids and book.asks
        }
        report = self.oms.evaluate_margin(
            now_ns=now, mark_prices=marks, initial_capital_by_venue=self.cfg.initial_capital_by_venue,
        )
        for row in report.venues:
            if row.state.value != "BREACH" or row.venue not in self.margin_config.venues:
                continue
            for instrument_id in row.details.get("breached_positions", []):
                result = self.liquidation.liquidate(
                    row.venue, instrument_id, self.books.get((row.venue, instrument_id)), now,
                    reason=f"margin {row.state.value}: {','.join(row.alert_codes)}",
                )
                if result is not None:
                    self._margin_dirty = True
                    self._flatten_surviving_leg(result, now)
            last = next((e for e in reversed(self.liquidation.events) if e["venue"] == row.venue), None)
            if last is not None and last["code"] != "BANKRUPTCY_DEFICIT":
                # Checked once per liquidation episode: after the latest
                # liquidation on this venue, not on every later evaluation.
                after = self.risk.evaluate_margin(
                    ledger=self.ledger, now_ns=now, mark_prices=marks,
                    open_orders=self.oms.open_risk_orders(), dry_run=True,
                )
                venue_after = next((r for r in after.venues if r.venue == row.venue), None)
                if venue_after is not None and venue_after.effective_margin_balance < 0:
                    self.liquidation.events.append({
                        "venue": row.venue, "ts_ns": now, "code": "BANKRUPTCY_DEFICIT",
                        "deficit": str(-venue_after.effective_margin_balance),
                        "detail": "equity below zero after liquidation; insurance fund and ADL are UNSUPPORTED",
                    })

    def _flatten_surviving_leg(self, liquidation: dict, now: int) -> None:
        """A liquidation closed one leg of a cross-venue hedge: close the other.

        Each liquidation fill flattens the same quantity of the opposite
        position on the other venues (never more than is still open there),
        through a risk-flatten plan that may trade under the kill switch.
        Perpetuals only: after a spot borrow buy-in nothing is flattened.
        """
        filled = Decimal(liquidation.get("filled", "0"))
        if not self.margin_config.flatten_after_liquidation or filled <= 0:
            return
        venue, instrument_id = liquidation["venue"], liquidation["instrument_id"]
        closed_long = liquidation["side"] == "SELL"
        remaining = filled
        for other in self.cfg.venues:
            if other == venue or remaining <= 0:
                continue
            position = self.ledger.positions.get((other, instrument_id), Decimal("0"))
            if position == 0 or (position > 0) == closed_long:
                continue                      # no opposite exposure here
            # Still to be closed by flatten plans already open on that venue
            # (what they have filled is already out of ``position``).
            outstanding = sum((
                abs(plan.residual_base) for plan in self.oms.plans.values()
                if plan.inherited_base and plan.state.value != "CLOSED"
                and (plan.opportunity.buy_venue, plan.opportunity.instrument_id) == (other, instrument_id)
            ), Decimal("0"))
            size = min(remaining, abs(position) - outstanding)
            if size <= 0:
                continue
            exposure = size if position > 0 else -size
            remaining -= size
            self.oms.open_flatten_plan(
                other, instrument_id, exposure, now,
                reason=f"liquidation of {venue} {instrument_id} ({liquidation['code']}) left this leg unhedged",
            )

    def _borrow_forced_close(self, reason: str, now_ns: int) -> None:
        """A venue buy-in leaves the other leg unhedged: stop trading."""
        self.oms.activate_kill_switch(
            reason=reason, now_ns=now_ns, operator_id="borrow-engine", source="BORROW",
        )

    def silent_feeds(self) -> list[str]:
        """Configured venues with no data, or silent while others kept publishing."""
        latest = self.last_event_ts_ns
        max_age = self.cfg.time_policy.max_feed_age_ns
        return sorted(
            venue for venue in self.cfg.venues
            if venue not in self.last_seen_ts_by_venue
            or latest - self.last_seen_ts_by_venue[venue] > max_age
        )

    def order_timestamps(self) -> list[dict[str, Any]]:
        """Send / arrival / report timestamps per order from the EMS trace."""
        rows: dict[str, dict[str, Any]] = {}
        for record in self.router.trace:
            kind = record["record_type"]
            if kind == "ORDER_IN_FLIGHT" and record["action"] == "SUBMIT":
                rows.setdefault(record["order_id"], {
                    "order_id": record["order_id"], "venue": record["venue"],
                })["order_send_ts_ns"] = record["ts_ns"]
            elif kind == "ORDER_ARRIVED" and record["action"] == "SUBMIT":
                rows.setdefault(record["order_id"], {
                    "order_id": record["order_id"], "venue": record["venue"],
                })["venue_match_ts_ns"] = record["ts_ns"]
            elif kind == "EXECUTION_CALLBACK_DELIVERED":
                row = rows.setdefault(record["order_id"], {
                    "order_id": record["order_id"], "venue": record["venue"],
                })
                key = (
                    "fill_observed_ts_ns"
                    if record["callback_type"] in {"PARTIAL", "FILLED"}
                    else "ack_observed_ts_ns"
                )
                row.setdefault(key, record["ts_ns"])
        return [rows[key] for key in sorted(rows)]

    def latency_report(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "execution_mode": self.execution_mode.value,
            "model": self.latency_model.report() if self.latency_model else None,
            "measured": self.measured_latency.report(),
            "wall_clock_stages": self.stage_timer.report(),
            "limitations": [
                "Market-data latency is measured from recorded receive and "
                "exchange timestamps after host-offset correction; it is not "
                "re-drawn during replay.",
                "Resting orders fill only when the book trades through them; "
                "queue-position fills are modelled in the advanced SDK backtester.",
            ],
        }
