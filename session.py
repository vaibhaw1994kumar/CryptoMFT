"""One trading stack shared by backtest replay, live paper and the benchmark.

The same books, integrity gate, time monitor, strategy, OMS/EMS, latency
model and risk engine are used in every mode; only the event source and the
venue adapters change. The execution mode is explicit and checked at start-up
so a paper fill can never be labelled as a venue fill (and vice versa).
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, replace as dataclass_replace
from enum import StrEnum
from decimal import Decimal
import time
from typing import Any, Callable

from .alignment import CrossVenueEligibilityPolicy
from .book_gate import BookIntegrityGate
from .config import AppConfig
from .execution import DispatchPolicy, StrategyDirectedRouter, VenueExecutionAdapter
from .latency import LatencyModel, latency_model_from_config
from .economics import opening_balance_events
from .borrow import BorrowEngine, BorrowLine, BorrowRecall
from .financing_journal import FinancingJournal
from .funding import FundingEngine, FundingRateBook, FundingSpec, is_perpetual
from .margin import LiquidationEngine, MarginConfig
from .transfers import RebalanceRule, TransferEngine, TransferRoute
from .risk import InventoryModel
from .ledger import Ledger
from .models import BookState, EventType, MarketEvent, Opportunity, PlanState, TradeEvent
from .oms import LIVE_ORDER_STATES, OMS, instrument_assets
from .circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from .hedge_lifecycle import HedgeState
from .ems.risk import RestingChild, plan_flatten_on_trip
from .ems.risk_gate import ExposureView, GateChild, GateContext, GateLimits, evaluate as gate_evaluate
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
        max_mark_age_ns=int(section.get("max_mark_age_ms", 60_000)) * 1_000_000,
        schedule_tolerance_ns=int(section.get("schedule_tolerance_ms", 1_000)) * 1_000_000,
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
        dispatch_policy: DispatchPolicy = DispatchPolicy.DETERMINISTIC_SEQUENTIAL,
        maker_venues: tuple[str, ...] = (),
        hedge_policy=None,
        resync_silence_ns: int | None = None,
        resync_delay_ns: int | None = None,
        resync_source: Callable[[str, str, int], MarketEvent | None] | None = None,
        gate_on_every_event: bool = False,
        markout_horizons_s: tuple[float, ...] | None = None,
        intent_journal: bool = False,
        breaker_config: CircuitBreakerConfig | None = None,
        risk_gate_mode: str = "off",
        gate_limits: GateLimits | None = None,
        layer_risk_mode: str = "count",
        layer_route_mode: str = "count",
        execution_config: Any = None,
    ) -> None:
        execution_mode = ExecutionMode(execution_mode)
        assert_execution_mode(
            execution_mode, live_data=live_data, adapters=adapters, venues=cfg.venues
        )
        if execution_mode == ExecutionMode.VENUE_TESTNET and not str(cfg.origin).startswith("profile:"):
            # Owner 11 Nov: real orders start only from a profile, the route that applies the safe-config checks (config.raw_config_from_profile). A plain config/*.json is for replay and paper.
            raise ValueError(f"{execution_mode.value} sends real orders and needs a config built from a profile (config.raw_config_from_profile); this config's origin is {cfg.origin!r}")
        self.cfg = cfg
        self.execution_mode = execution_mode
        self.maker_venues = tuple(maker_venues)      # Phase D: under FILL_TRIGGERED the maker leg rests on the first of these venues that the opportunity trades on (the profile's arb.maker_venues)
        # Replay book policy (Phase D, owner 8 Nov): see `_schedule_resync`. None = off (live and every existing caller: the book stays invalid until the venue's own next snapshot).
        if (resync_delay_ns is None) != (resync_source is None):
            raise ValueError("a resync delay and a resync source go together")
        self.resync_delay_ns = resync_delay_ns
        # Owner 9 Nov (D7d): the live service evaluates the book gate on EVERY book event, before the session sees it. With this on, a replay does the same: `on_event` applies the book and evaluates the gate first, so the gate is not skipped when the event is
        # time-integrity-blocked and its feed-control, kill-switch and margin events are stamped as live stamps them. Off = every existing caller, byte for byte.
        self.gate_on_every_event = gate_on_every_event
        # Forward price markouts per fill (owner 9 Nov, ported from the backtester's `markout_by_venue_role`): the core's own `MarkoutTracker`, fed by the ledger's trade fills and resolved on market-data time. Off (None) for every caller
        # that does not pass horizons: the session replay passes the profile's `markout_horizons_s`. It reads the books and the ledger; it changes no decision.
        self.markouts = None
        self._markout_horizons_s = markout_horizons_s
        self._intent_journal = intent_journal           # E0 step (a) (owner 10 Nov): journal the OMS's orders as execution-layer intents; observation only, read by nothing
        self.intent_observer = None
        self._markout_touch: dict[str, tuple] = {}
        self.resync_silence_ns = resync_silence_ns           # a venue silent this long (any message) is treated as a dropped feed: resubscribe after the same delay (Q-G2: the 5 s is a resubscribe trigger, not a blocking gate)
        self.resync_unavailable = 0
        self.resync_source = resync_source
        self._resync_due: dict[tuple[str, str], tuple[int, int, str]] = {}      # (venue, instrument) -> (due ns, invalid since ns, reason)
        self.resync_log: list[dict[str, Any]] = []
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
        # Each route carries only what its venue and adapter support (REF05).
        from .capabilities import configure_route
        from .execution import VenueRouteStatus

        for venue in cfg.venues:
            configure_route(
                self.router.route_status.setdefault(venue, VenueRouteStatus()), venue,
                cfg.instruments.get(venue, cfg.instrument_id).product_type,
                testnet=execution_mode == ExecutionMode.VENUE_TESTNET,
            )
        # Scoped venue rate limits with capacity kept for cancels (EMS02).
        rate_settings = {"enabled": True, **(cfg.rate_limits or {})}
        if rate_settings["enabled"]:
            from .capabilities import scoped_rate_limiter

            self.router.scoped_limits = scoped_rate_limiter(
                {venue: cfg.instruments.get(venue, cfg.instrument_id).product_type for venue in cfg.venues},
                quote_share=float(rate_settings.get("quote_share", 0.8)),
                quote_min_free=int(rate_settings.get("quote_min_free", 2)),
            )
        # Financing engines and their journal come before the OMS: on a restart
        # the OMS re-reserves risk for open plans, and a sell leg financed by a
        # borrow needs the loan (journal) and the borrow line (engine) to exist.
        self.funding = funding_engine(cfg, self.ledger)
        self.borrow = borrow_engine(cfg, self.ledger, self._borrow_forced_close)
        self.risk.borrow = self.borrow
        self.transfers = self._transfer_engine(cfg)
        if self.transfers.configured:
            self.risk.unavailable_provider = self.transfers.in_transit_out
            self.risk.rebalancing = self.transfers.rebalance_enabled
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
            self.transfers.restore(self.ledger.economics.events)
        # Fills rebuilt from the plan store are history, not new fills: the
        # engines must not open loans or rebook settlements for them again.
        listeners, self.ledger.trade_fill_listeners = self.ledger.trade_fill_listeners, []
        try:
            self.oms = OMS(
                cfg.instruments, self.risk, self.ledger,
                eligibility_policy=self.alignment_policy, router=self.router,
                residual_policy=cfg.residual_policy, plan_store=plan_store,
                order_timeout_policy=cfg.order_timeout_policy, dispatch_policy=dispatch_policy, hedge_policy=hedge_policy,
            )
            self.risk.alert_context = self.alert_context            # owner 12 Nov: a kill-switch activation carries the alert fields
        finally:
            # Keep what the OMS registered (reservation consumption) after the
            # engines' listeners, which were held back during the rebuild.
            registered = [l for l in self.ledger.trade_fill_listeners if l not in listeners]
            self.ledger.trade_fill_listeners = listeners + registered
        if layer_risk_mode not in ("count", "enforce"):
            raise ValueError("layer_risk_mode must be count or enforce")
        if layer_risk_mode == "enforce" and not self._intent_journal:
            raise ValueError("layer_risk_mode='enforce' needs the execution layer (intent_journal=True): refusing to run without the gate")
        self.layer_risk_mode = layer_risk_mode
        self.quotes_skipped_rate_limit = 0
        self.quotes_withdrawn = 0
        if layer_route_mode not in ("count", "enforce"):
            raise ValueError("layer_route_mode must be count or enforce")
        if layer_route_mode == "enforce" and not self._intent_journal:
            raise ValueError("layer_route_mode='enforce' needs the execution layer (intent_journal=True): refusing to run without it")
        self.layer_route_mode = layer_route_mode
        if self._intent_journal:
            from .ems.config import ExecutionConfig
            from .ems.layer import ExecutionLayer, IntentObserver

            # the layer's config is the profile's own `execution:` block when the caller has it (the manifest then reports what the run used); the gate mode stays the session's layer_risk_mode.
            # The layer only observes: nothing the OMS decides reads it back, so this changes the report and the layer's own counters, never an order.
            layer_cfg = dataclass_replace(execution_config, risk_reservation=layer_risk_mode) if execution_config is not None else ExecutionConfig(risk_reservation=layer_risk_mode)
            self.intent_observer = IntentObserver(ExecutionLayer(
                layer_cfg, instrument=cfg.instrument_id,
                dispatch_mode="fill_triggered" if self.oms.dispatch_policy == DispatchPolicy.FILL_TRIGGERED else "simultaneous"))
            self.oms.observer = self.intent_observer
            self.oms.route_mode = layer_route_mode
            if len(self.maker_venues) >= 1 and len(cfg.venues) == 2 and self.maker_venues[0] in cfg.venues:          # E0 step (d): fixed assignment, the first maker venue rests and the other venue hedges
                self.intent_observer.layer.set_routing(self.maker_venues[0], next(v for v in cfg.venues if v != self.maker_venues[0]))
            self.intent_observer.layer.set_risk(self._layer_limits, self._base_by_venue)       # E0 step (c): the layer's reservation gate reads the session's own limits and positions (one rule, not a second mapping)
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
        if self._markout_horizons_s:
            from .simulation.latency_model import MarkoutTracker

            self.markouts = MarkoutTracker(tuple(self._markout_horizons_s))
            self.ledger.trade_fill_listeners.append(self._markout_fill)
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
        # Track A item 1 (owner 11 Nov): the circuit breaker (pause versus trip, flatten on trip, manual reset) and the pre-send risk gate on the session. Both OFF by default: with breaker_config None and
        # risk_gate_mode "off" nothing below runs and every result is as before. The rules are the pure modules circuit_breaker.py, ems/risk.py (plan_flatten_on_trip) and ems/risk_gate.py.
        if risk_gate_mode not in ("off", "count", "enforce"):
            raise ValueError("risk_gate_mode must be off, count or enforce")
        if breaker_config is not None and len(cfg.venues) != 2:
            raise ValueError("the circuit breaker watches one venue pair")
        self.breaker: CircuitBreaker | None = CircuitBreaker(breaker_config, cfg.venues[0], cfg.venues[1]) if breaker_config is not None else None
        self.risk_gate_mode = risk_gate_mode
        self.hedge_gate_would_refuse: dict[str, int] = {}
        if risk_gate_mode != "off":
            self.oms.hedge_gate = self._hedge_gate_observe
        self.gate_limits = gate_limits
        self.gate_counts: dict[str, int] = {}
        self.breaker_alarms: list[dict[str, Any]] = []
        self._breaker_pos = 0
        self._breaker_seeded = False
        self._breaker_state_logged = "OK"
        self._breaker_chains: set[str] = set()
        self._breaker_unknown: dict[str, int] = {}
        self._breaker_trip_handled = False
        self._breaker_attempt = 0
        self._breaker_last_attempt_ns = 0
        self._breaker_alarmed: set[str] = set()
        self._breaker_unavailable_streak = 0
        self._breaker_backoff_ns = 1_000_000_000          # owner 13 Nov: a flatten retry because a venue is unavailable waits 1 s, doubling to 30 s (reset by any other outcome)
        self._breaker_dust: dict[str, Any] | None = None  # the flatten remainder below the venue's minimum (owner 13 Nov): recorded once, never retried, the trip stays latched until a manual reset
        self.breaker_dust_events: list[dict[str, Any]] = []
        self.breaker_flatten_counters: dict[str, int] = {"dust_recorded": 0, "unavailable_retries": 0}
        self.signals_blocked_by_breaker = 0
        self._last_good_marks: dict[tuple[str, str], tuple[int, Decimal]] = {}
        self.margin_price_paused = False                  # a missing margin price pauses new risk (margin.missing_price_action 'pause'); resumes when the price is back
        self.signals_blocked_by_margin_price = 0
        self.exchange_marks: dict[tuple[str, str], tuple[int, Decimal]] = {}       # (venue, instrument) -> (receive ts, mark price) from the derivatives stream, or the replay's labelled MARK_FROM_MID

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
        if self.gate_on_every_event and gate_allowed is None and event.event_type in (EventType.BOOK_SNAPSHOT, EventType.BOOK_DELTA):
            # As the live service does: apply the book, evaluate the gate, then hand the event to the session (the gate is not skipped by the early returns below).
            if apply_book:
                self.books[(event.venue, event.instrument_id)].apply(event)
                apply_book = False
            gate_allowed = self.book_gate.allow(tuple(self.books[(venue, self.cfg.instrument_id)] for venue in self.cfg.venues), now)
        if self.resync_delay_ns is not None:
            self._resync_due_books(now)
        self.events_processed += 1
        self.last_event_ts_ns = max(self.last_event_ts_ns, now)
        self.last_seen_ts_by_venue[event.venue] = event.receive_ts_ns
        if self.breaker is not None and event.venue in self.breaker.connectivity:
            self.breaker.connectivity[event.venue].record_heartbeat(event.receive_ts_ns / 1e9)       # any message from a venue proves its gateway reachable
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
            self._schedule_resync(now)
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
        self._schedule_resync(now)                      # also when the caller applied the book itself (apply_book=False)
        self.oms.on_book_update(event.venue, event.instrument_id, now)
        if self.markouts is not None:                    # the backtester's trigger: resolve when THIS venue's touch changed (not on every update), so both read the same mid at the same moments
            mb = self.books[(event.venue, event.instrument_id)]
            touch = (mb.best_bid, mb.best_ask) if mb.bids and mb.asks else None
            if touch is not None and touch != self._markout_touch.get(event.venue):
                self._markout_touch[event.venue] = touch
                self.resolve_markouts(now)
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
        if self.transfers.configured:
            self.transfers.process(now)
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
        self._breaker_step(now)
        if gate_allowed is None:
            required = tuple(
                self.books[(venue, self.cfg.instrument_id)] for venue in self.cfg.venues
            )
            gate_allowed = self.book_gate.allow(required, now)
        if not gate_allowed:
            return None
        wall_strategy = self.wall_ns()
        self.stage_timer.add("feed_to_strategy_ns", wall_strategy - wall_start)
        self._withdraw_invalid_quotes(now)          # quote validity (default off: a strategy without `quotes_to_withdraw`, or with it switched off, returns nothing)
        last: Opportunity | None = None
        for strategy in self.strategies:
            if self.flattening and not hasattr(strategy, "force_exit"):
                continue
            if self.breaker is not None and not self.breaker.allow_signal(now / 1e9, self._net_abs()) and not hasattr(strategy, "force_exit"):
                self.signals_blocked_by_breaker += 1           # PAUSED or TRIPPED: no new risk (a force_exit strategy still closes)
                continue
            if self.margin_price_paused and not hasattr(strategy, "force_exit"):
                self.signals_blocked_by_margin_price += 1       # no price for the margin check: no new risk until it is back
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

    # Book states that only a new snapshot can leave. PRICE_ANOMALY and STALE end by themselves (the next sane update); SYNCING before the first snapshot is the start-up wait.
    _NEEDS_SNAPSHOT = (BookState.GAP_DETECTED, BookState.CHECKSUM_FAILED, BookState.INVALID_DATA)

    def _schedule_resync(self, now: int) -> None:
        """The one replay rule for a book that went invalid with no snapshot in the data to repair it: trading stays suspended (the book gate blocks on the invalid book) and, after
        `resync_delay_ns` (ASSUMED: the live feed's reconnect backoff plus a snapshot round trip), the venue is resubscribed and a fresh snapshot of the exchange state is applied. The
        snapshot comes from `resync_source(venue, instrument_id, now_ns)`, which the replay feed answers from the data's own state at `now` (no later information); None = nothing to
        resync from, the wait is repeated. A snapshot of the data itself that arrives first ends the wait. Nothing waits for a snapshot the data does not have."""
        if self.resync_delay_ns is None:
            return
        for venue in self.cfg.venues:
            key = (venue, self.cfg.instrument_id)
            book = self.books[key]
            silent = (self.resync_silence_ns is not None and book.last_receive_ts_ns is not None and now - book.last_receive_ts_ns >= self.resync_silence_ns)
            if book.state in self._NEEDS_SNAPSHOT or silent:
                if key not in self._resync_due:
                    self._resync_due[key] = (now + self.resync_delay_ns, now, f"{book.state.value}:{book.invalid_reason}" if book.state in self._NEEDS_SNAPSHOT else f"FEED_SILENT:{(now - book.last_receive_ts_ns) / 1e9:.1f}s")
            elif key in self._resync_due and book.state == BookState.VALID:
                due, since, reason = self._resync_due.pop(key)
                self.resync_log.append({"venue": venue, "invalid_since_ns": since, "resync_at_ns": now, "wait_ns": now - since, "reason": reason, "by": "DATA_SNAPSHOT", "state_after": book.state.value})

    def note_venue_message(self, venue: str, ts_ns: int) -> None:
        """A message of any kind from a venue (a trade print, a heartbeat) proves its feed alive: its books' age is measured from it (Q-G1)."""
        for (v, _), book in self.books.items():
            if v == venue:
                book.note_venue_message(ts_ns)
        if self.breaker is not None and venue in self.breaker.connectivity:
            self.breaker.connectivity[venue].record_heartbeat(ts_ns / 1e9)

    def resync_if_due(self, now_ns: int) -> None:
        """For a caller that applies books itself: resync any book whose wait is over BEFORE applying the next event (`on_event` does the same at its start)."""
        if self.resync_delay_ns is not None:
            self._resync_due_books(now_ns)

    def _resync_due_books(self, now: int) -> None:
        for key, (due, since, reason) in list(self._resync_due.items()):
            if now < due:
                continue
            venue, instrument_id = key
            snapshot = self.resync_source(venue, instrument_id, now)
            if snapshot is None:
                self._resync_due[key] = (now + self.resync_delay_ns, since, reason)
                self.resync_unavailable += 1
                if self.resync_unavailable <= 50:                      # a long outage repeats every delay: the first 50 are logged, all are counted
                    self.resync_log.append({"venue": venue, "invalid_since_ns": since, "resync_at_ns": now, "wait_ns": now - since, "reason": reason, "by": "UNAVAILABLE", "state_after": self.books[key].state.value})
                continue
            book = self.books[key]
            del self._resync_due[key]
            book.start_resync("replay_resubscribe")
            book.apply(snapshot)
            self.resync_log.append({"venue": venue, "invalid_since_ns": since, "resync_at_ns": now, "wait_ns": now - since, "reason": reason, "by": "RESYNC_SOURCE_ASSUMED_DELAY", "state_after": book.state.value})

    def book_policy_report(self, now_ns: int) -> dict[str, Any]:
        """For the run manifest: the book policy, every resync, and the suspended seconds per UTC day and cause (any reason the book gate blocked, start-up included)."""
        by_day = self.book_gate.suspended_seconds_by_day(now_ns)
        return {
            "invalid_book_policy": "RESUBSCRIBE_AFTER_ASSUMED_DELAY" if self.resync_delay_ns is not None else "WAIT_FOR_DATA_SNAPSHOT",
            "resync_delay_s": None if self.resync_delay_ns is None else self.resync_delay_ns / 1e9,
            "resync_delay_label": "ASSUMED" if self.resync_delay_ns is not None else None,
            "resyncs": list(self.resync_log),
            "resync_unavailable_count": self.resync_unavailable,
            "resyncs_by_source": {by: sum(1 for r in self.resync_log if r["by"] == by) for by in sorted({r["by"] for r in self.resync_log})},
            "suspension_intervals_ns": [list(w) for w in self.book_gate.open_intervals(now_ns)],
            "suspended_seconds_by_day": by_day,
            "suspended_seconds_total_by_day": {day: round(sum(causes.values()), 9) for day, causes in by_day.items()},     # a cause that changes mid-suspension splits it across keys: the total is the number to quote
        }

    def on_trade(self, trade: TradeEvent, *, now_ns: int | None = None) -> int:
        """Phase D (D2): a public trade print reaches the venue simulator (queue fills come from trades, never from a book update). It does not feed the book gate, the time monitor or the strategy; a venue adapter without an ``on_trade`` hook ignores it. Applies even when the last book event was blocked on its timestamps: the venue matches whatever our clock says."""
        self.note_venue_message(trade.venue, trade.receive_ts_ns)          # a print proves the feed alive (age is measured from the last message of any kind)
        now = trade.receive_ts_ns if now_ns is None else now_ns
        if self.clock_ns is not None and now_ns is None:
            now = int(self.clock_ns())
        self.oms.advance_time(now, self.books)
        produced = self.oms.on_trade(trade.venue, trade, now)
        if produced:
            self.last_event_ts_ns = max(self.last_event_ts_ns, now)
            self._notify_closed_plans()
        return produced

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
        maker_venue = None
        if self.oms.dispatch_policy == DispatchPolicy.FILL_TRIGGERED:
            maker_venue = next((v for v in self.maker_venues if v in (opportunity.buy_venue, opportunity.sell_venue)), None)
            if maker_venue is None:
                raise ValueError(f"FILL_TRIGGERED needs a maker venue: none of maker_venues {self.maker_venues} is a venue of the opportunity ({opportunity.buy_venue}, {opportunity.sell_venue})")
        if self.risk_gate_mode != "off" and not self._risk_gate_allows(opportunity, maker_venue, now):
            return None
        if maker_venue is not None and not self.oms.router.quote_has_room(maker_venue, opportunity.instrument_id, now):
            self.quotes_skipped_rate_limit += 1          # owner 12 Nov (the 2f finding): the quote share of the rate-limit window is used up: this quote is SKIPPED (no plan, nothing queued, nothing retried); hedges, unwinds, flattens and cancels keep the whole window
            if self.quotes_skipped_rate_limit <= 200:
                self.ledger.record("QUOTE_SKIPPED_RATE_LIMIT", now, opportunity_id=opportunity.opportunity_id, venue=maker_venue)
            return None
        plan = self.oms.execute(opportunity, self.books, dispatch_ts_ns=dispatch_ts, maker_venue=maker_venue)
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

    # -- circuit breaker and risk gate (Track A item 1, owner 11 Nov) -------------------------------------------------------------------------------------------------------------------------
    def _net_abs(self) -> float:
        return abs(float(sum(self.ledger.positions.values(), Decimal(0))))

    def _base_by_venue(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for (venue, _), qty in self.ledger.positions.items():
            out[venue] = out.get(venue, 0.0) + float(qty)
        return out

    def _breaker_inputs(self) -> tuple[float, int, int, bool]:
        """(|net base|, hedge chains not yet settled, our orders resting or in flight, True when no hedge chain is UNKNOWN: the open orders are reconciled)."""
        pending = unknown = 0
        for chains in self.oms.hedge_chains.values():
            for chain in chains.values():
                if chain.state not in (HedgeState.HEDGED, HedgeState.RECONCILED, HedgeState.FAILED):
                    pending += 1
                if chain.state == HedgeState.UNKNOWN:
                    unknown += 1
        open_orders = sum(1 for plan in self.oms.plans.values() for order in plan.children if order.state in LIVE_ORDER_STATES)
        return self._net_abs(), pending, open_orders, unknown == 0

    def _breaker_ingest(self, net: float, pending: int) -> None:
        """Feed the breaker from the ledger events written since the last step: a hedge chain's first outcome (success or failure), a hedge going UNKNOWN and being resolved, a failed unwind."""
        b = self.breaker
        events = self.ledger.events
        while self._breaker_pos < len(events):
            e = events[self._breaker_pos]
            self._breaker_pos += 1
            kind, ts = e["event_type"], e["ts_ns"] / 1e9
            if kind == "HEDGE_STATE":
                chain, to, frm = e["chain_id"], e["to_state"], e["from_state"]
                if chain not in self._breaker_chains and to in ("HEDGED", "RECONCILED", "REJECTED", "UNKNOWN", "ESCALATED", "FAILED"):
                    self._breaker_chains.add(chain)           # the first outcome of a chain: a retry that later succeeds is still the failure it was
                    b.on_hedge_result(ts, to not in ("HEDGED", "RECONCILED"), net, pending)
                if to == "UNKNOWN":
                    key = self._breaker_unknown.setdefault(chain, len(self._breaker_unknown) + 1)
                    b.on_unknown_order(key, ts, "")
                elif frm == "UNKNOWN" and chain in self._breaker_unknown:
                    b.on_order_resolved(self._breaker_unknown[chain])
            elif kind == "UNWIND_EXHAUSTED_ALARM":
                b.on_failed_unwind(ts, net, pending)

    def _breaker_step(self, now_ns: int) -> None:
        """One breaker cycle on the session's clock: feed it, let it decide, and on a trip cancel the resting quotes and flatten the net (see `_breaker_on_trip`)."""
        b = self.breaker
        if b is None:
            return
        now = now_ns / 1e9
        if not self._breaker_seeded:
            self._breaker_seeded = True
            for mon in b.connectivity.values():
                mon.record_heartbeat(now)                      # start-up grace: a venue is not 'down' before its first message
        net, pending, open_orders, reconciled = self._breaker_inputs()
        self._breaker_ingest(net, pending)
        b.on_net_update(now, net, pending, dust_below=self._hedge_venue_lot(now_ns))
        b.check_unknown_orders(now, net, pending)
        b.on_gateway_status(now, net, pending, open_orders, reconciled)
        b.maybe_simulated_operator_reset(now, net, pending, open_orders)
        state = b.state()
        if state != self._breaker_state_logged:
            self.ledger.record("BREAKER_STATE_TRANSITION", now_ns, previous_state=self._breaker_state_logged, state=state,
                               cause=(b.trips[-1].cause if state == "TRIPPED" and b.trips else (b.pauses[-1].cause if state == "PAUSED" and b.pauses else "")))
            self._breaker_state_logged = state
        if state == "TRIPPED":
            self._breaker_on_trip(now_ns, net)
        elif self._breaker_trip_handled:
            self._breaker_trip_handled, self._breaker_attempt = False, 0

    def _hedge_venue_lot(self, now_ns: int) -> float:
        """One lot of the hedge venue (the venue that is not the maker venue): a net exposure below it is untradeable dust (owner 13 Nov). 0.0 when the venue pair or the instrument is not known."""
        venues = [v for v in self.cfg.venues if v not in self.maker_venues] or list(self.cfg.venues[-1:])
        try:
            return float(self.oms.instruments.get(venues[0], self.cfg.instrument_id, now_ns).quantity_step)
        except (KeyError, ValueError, IndexError):
            return 0.0

    def _breaker_on_trip(self, now_ns: int, net: float) -> None:
        """A TRIP: new risk is already blocked. Cancel every resting PASSIVE order (the hedge and unwind children stay), then flatten the NET base with a risk-flatten plan that the OMS prices at the touch plus the
        unwind band and retries on every new book; when the retry budget is gone with exposure open the plan becomes a persistent ALARM (no more sends). A manual `reset_breaker` is the only way out."""
        b = self.breaker
        first = not self._breaker_trip_handled
        if first:
            self._breaker_trip_handled = True
            trip = b.trips[-1] if b.trips else None
            escalated = self.oms.escalate_open_chains(now_ns, "breaker trip")          # a chain with no order in flight unwinds NOW through its own path; the flatten below covers only what no open plan manages
            self.ledger.record("BREAKER_TRIP", now_ns, cause=trip.cause if trip else "", net_base=net, flatten_mode="net", chains_escalated=len(escalated), **self.alert_context(now_ns),
                               **({"dust_residual_base": b.dust_base} if b.dust_base > 0 else {}))
        managed = self.oms.open_plan_position_by_venue()
        net_d = sum(self.ledger.positions.values(), Decimal(0)) - sum(managed.values(), Decimal(0))      # the net no open plan manages: the only exposure the breaker flattens
        net = abs(float(net_d))
        if not first:
            flatten_open = any(p.inherited_base and p.state != PlanState.CLOSED for p in self.oms.plans.values())
            if net <= 1e-10 or flatten_open or now_ns - self._breaker_last_attempt_ns < self._breaker_backoff_ns:
                return
            if self._breaker_dust is not None and abs(net - self._breaker_dust["net_base"]) < 1e-12:
                return                                                     # the remainder is dust and has not changed: recorded once, not retried
            self._breaker_attempt += 1
        self._breaker_last_attempt_ns = now_ns
        resting = [RestingChild(o.order_id, o.venue, "PASSIVE") for plan in self.oms.plans.values() for o in plan.children
                   if o.order_role == "PRIMARY" and not o.reduce_only and o.state in LIVE_ORDER_STATES]
        mid, touch, available = {}, {}, {}
        for venue in self.cfg.venues:
            book = self.books.get((venue, self.cfg.instrument_id))
            ok = book is not None and book.state == BookState.VALID and bool(book.bids) and bool(book.asks)
            available[venue] = ok
            if ok:
                touch[venue] = (float(book.best_bid), float(book.best_ask))
                mid[venue] = (touch[venue][0] + touch[venue][1]) / 2
        base_unmanaged = {v: q - float(managed.get(v, Decimal(0))) for v, q in self._base_by_venue().items()}
        steps = plan_flatten_on_trip(resting, base_unmanaged, mid, band_bps=float(self.oms.residual_policy.unwind_price_slippage_bps), attempt=self._breaker_attempt,
                                     retry_budget=b.config.tripped_unwind_retries, available=available, touch=touch)
        if any(step.kind == "CANCEL" for step in steps):
            self.oms._cancel_orders("breaker trip", now_ns, venue=None, preserve_risk_reducing=True, ledger_event="BREAKER_TRIP_CANCEL")
        if any(step.kind == "ALARM" and step.reason in ("VENUE_DOWN_CANNOT_FLATTEN", "NO_AVAILABLE_VENUE_TO_FLATTEN_ON") for step in steps):
            self._breaker_backoff_ns = min(30_000_000_000, 1_000_000_000 * 2 ** self._breaker_unavailable_streak)      # the venue is unavailable: the next retry waits 1 s, then 2, 4, ... capped at 30 s (not every event)
            self._breaker_unavailable_streak += 1
            self.breaker_flatten_counters["unavailable_retries"] += 1
        else:
            self._breaker_backoff_ns, self._breaker_unavailable_streak = 1_000_000_000, 0
        for step in steps:
            if step.kind == "FLATTEN":
                qty = net_d if abs(float(net_d) - step.qty) < 1e-9 else Decimal(str(step.qty))
                if self._flatten_is_dust(step, qty, touch, now_ns, net):
                    continue
                exposure = abs(qty) if step.side == "sell" else -abs(qty)
                self.ledger.record("BREAKER_FLATTEN", now_ns, venue=step.venue, side=step.side, quantity=abs(qty), tactic=step.tactic, worst_price=step.worst_price, attempt=self._breaker_attempt)
                self.oms.open_flatten_plan(step.venue, self.cfg.instrument_id, exposure, now_ns, reason="circuit breaker trip: flatten the net", shadow=True)
            elif step.kind == "ALARM" and step.reason not in self._breaker_alarmed:
                self._breaker_alarmed.add(step.reason)
                alarm = {"ts_ns": now_ns, "reason": step.reason, "venue": step.venue, "quantity": step.qty,
                         **({"dust": {k: str(v) for k, v in self._breaker_dust.items()}} if self._breaker_dust is not None else {})}
                self.breaker_alarms.append(alarm)
                self.ledger.record("BREAKER_ALARM", now_ns, **{k: v for k, v in alarm.items() if k != "ts_ns"}, **self.alert_context(now_ns))

    def _flatten_is_dust(self, step, qty: Decimal, touch: dict[str, tuple[float, float]], now_ns: int, net: float) -> bool:
        """Owner 13 Nov: a flatten remainder below the venue's minimum quantity or notional cannot be sent. It is marked DUST and recorded ONCE (BREAKER_FLATTEN_DUST, with the minimums and the lot); it is not
        retried, and the trip stays latched until the manual reset. Counted in the exposure and shown in the alarm payload and the end-of-day report."""
        instrument_id = self.cfg.instrument_id
        rounded = self.oms.instruments.round_quantity(step.venue, instrument_id, abs(qty), now_ns)
        spec = self.oms.instruments.get(step.venue, instrument_id, now_ns)
        px = touch.get(step.venue, (0.0, 0.0))[1 if step.side == "buy" else 0]
        if not (rounded < spec.min_quantity or rounded * Decimal(str(px)) < spec.min_notional):
            self._breaker_dust = None
            return False
        if self._breaker_dust is None or abs(net - self._breaker_dust["net_base"]) >= 1e-12:
            self._breaker_dust = {"venue": step.venue, "side": step.side, "residual_base": abs(qty), "tradeable_quantity": rounded, "min_quantity": spec.min_quantity, "min_notional": spec.min_notional,
                                  "lot": spec.quantity_step, "net_base": net, "since_ts_ns": now_ns}
            self.breaker_flatten_counters["dust_recorded"] += 1
            record = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in self._breaker_dust.items() if k != "since_ts_ns"}
            if len(self.breaker_dust_events) < 100:
                self.breaker_dust_events.append({"ts_ns": now_ns, **record})
            self.ledger.record("BREAKER_FLATTEN_DUST", now_ns, **record, note="below the venue minimum: DUST, counted in the exposure, not retried; the trip stays latched until a manual reset")
        return True

    def reset_breaker(self, now_ns: int, operator_id: str = "") -> None:
        """The manual reset, an operator action: the only way a TRIPPED breaker (or a persistent flatten alarm) is cleared."""
        if self.breaker is None:
            raise ValueError("no circuit breaker is configured")
        self.breaker.reset()
        self.ledger.record("BREAKER_RESET", now_ns, operator_id=operator_id)
        self._breaker_state_logged = "OK"
        self._breaker_trip_handled, self._breaker_attempt = False, 0
        self._breaker_alarmed.clear()
        self._breaker_dust, self._breaker_backoff_ns, self._breaker_unavailable_streak = None, 1_000_000_000, 0

    def alert_context(self, now_ns: int) -> dict[str, object]:
        """The OMS's alert context (confirmed residual, pending hedge quantity, oldest exposure age, why execution is blocked) with the session's own blockers put first: a tripped or paused breaker, the margin price pause."""
        ctx = self.oms.alert_context(now_ns)
        if getattr(self, "breaker", None) is not None and self.breaker.state() != "OK":
            ctx["execution_blocked_reason"] = f"breaker:{self.breaker.state()}"
        elif getattr(self, "margin_price_paused", False):
            ctx["execution_blocked_reason"] = "margin_price_missing"
        return ctx

    def _withdraw_invalid_quotes(self, now_ns: int) -> None:
        for strategy in self.strategies:
            quotes = getattr(strategy, "quotes_to_withdraw", None)
            if quotes is None:
                continue
            for q in quotes(self.books, now_ns):
                if self.oms.withdraw_quote(q["order_id"], now_ns, q["reason"], q["net_bps"]):
                    self.quotes_withdrawn += 1

    def _layer_limits(self):
        """The limits of the layer's reservation gate: the session's own gate limits, so the base ceiling is one rule (E0 step (c))."""
        from .ems.risk import RiskLimits as LayerLimits
        gl = self._gate_limits()
        return LayerLimits(venue_cap=gl.venue_cap, net_ceiling=gl.net_ceiling + gl.fill_headroom)

    def _hedge_gate_observe(self, child: ChildOrder, ts_ns: int) -> None:
        """E0 step (c): a hedge child is evaluated against the risk gate and NEVER blocked (a refused hedge would leave naked exposure): the reasons it would have been refused are counted and logged."""
        decision = self.gate_child(GateChild("HEDGE", "IOC_CAPPED", child.venue, child.side.value.lower(), float(child.quantity), child.order_id, limit_price=float(child.limit_price)))
        if decision.allowed:
            return
        self.hedge_gate_would_refuse[decision.reason] = self.hedge_gate_would_refuse.get(decision.reason, 0) + 1
        self.ledger.record("RISK_GATE_WOULD_REFUSE", ts_ns, role="HEDGE", reason=decision.reason, detail=decision.detail, venue=child.venue, side=child.side.value.lower(),
                           quantity=str(child.quantity), never_blocked=True)

    def _gate_limits(self) -> GateLimits:
        if self.gate_limits is not None:
            return self.gate_limits
        cap = float(self.oms.risk.limits.max_position_base)      # the profile config puts the net ceiling plus the fill headroom here
        return GateLimits(net_ceiling=cap, fill_headroom=0.0, venue_cap=cap, band_bps=float(self.oms.residual_policy.unwind_price_slippage_bps))

    def _risk_gate_allows(self, opportunity: Opportunity, maker_venue: str | None, now_ns: int) -> bool:
        """The pre-send gate for the new-risk (maker) leg of an opportunity. `count`: evaluate and log what it would refuse, send anyway; `enforce`: refuse. (The hedge children are the OMS's and the
        execution layer's: they reach the same rule through `gate_child`, wired in E0 step (c).)"""
        venue = maker_venue or opportunity.buy_venue
        buy = venue == opportunity.buy_venue
        price = opportunity.buy_price if buy else opportunity.sell_price
        child = GateChild("PRIMARY", "PASSIVE", venue, "buy" if buy else "sell", float(opportunity.quantity), opportunity.opportunity_id, limit_price=float(price))
        decision = self.gate_child(child)
        if decision.allowed:
            return True
        self.gate_counts[decision.reason] = self.gate_counts.get(decision.reason, 0) + 1
        enforced = self.risk_gate_mode == "enforce"
        self.ledger.record("RISK_GATE_REFUSED" if enforced else "RISK_GATE_WOULD_REFUSE", now_ns, opportunity_id=opportunity.opportunity_id, reason=decision.reason, detail=decision.detail,
                           venue=venue, side=child.side, quantity=child.qty)
        return not enforced

    def gate_child(self, child: GateChild):
        """Evaluate one child order against the session's state: the breaker, the reachable exposure (filled + resting primary orders + hedge children in UNKNOWN), the limits, the bands from the touch."""
        resting: dict[tuple[str, str], float] = {}
        unknown: dict[tuple[str, str], float] = {}
        for plan in self.oms.plans.values():
            for order in plan.children:
                if order.state not in LIVE_ORDER_STATES and order.state.value != "TIMED_OUT":      # TIMED_OUT = UNKNOWN: it may still be live at the venue, so it counts
                    continue
                kind = unknown if order.state.value == "TIMED_OUT" else resting if order.order_role == "PRIMARY" else None
                if kind is not None:
                    key = (order.venue, order.side.value.lower())
                    kind[key] = kind.get(key, 0.0) + float(order.quantity - order.filled_quantity)
        touch, mid = {}, {}
        for venue in self.cfg.venues:
            book = self.books.get((venue, self.cfg.instrument_id))
            if book is not None and book.state == BookState.VALID and book.bids and book.asks:
                touch[venue] = (float(book.best_bid), float(book.best_ask))
                mid[venue] = (touch[venue][0] + touch[venue][1]) / 2
        ctx = GateContext(breaker_state=self.breaker.state() if self.breaker is not None else "OK",
                          exposure=ExposureView(filled=self._base_by_venue(), resting=resting, unknown=unknown), limits=self._gate_limits(), mid=mid, touch=touch)
        return gate_evaluate(child, ctx)

    def breaker_report(self) -> dict[str, Any]:
        return {"breaker": self.breaker.summary() if self.breaker is not None else None, "breaker_alarms": list(self.breaker_alarms), "signals_blocked_by_breaker": self.signals_blocked_by_breaker,
                **({"breaker_flatten": {"counters": dict(self.breaker_flatten_counters), "dust_events": list(self.breaker_dust_events), "dust_now": None if self._breaker_dust is None else {k: str(v) for k, v in self._breaker_dust.items()}}}
                   if (self.breaker_flatten_counters["dust_recorded"] or self.breaker_flatten_counters["unavailable_retries"]) else {}),
                "margin_price_paused": self.margin_price_paused, "signals_blocked_by_margin_price": self.signals_blocked_by_margin_price,
                "risk_gate_mode": self.risk_gate_mode, "risk_gate_refusals_by_reason": dict(sorted(self.gate_counts.items())),
                "layer_risk_mode": self.layer_risk_mode, "layer_route_mode": self.layer_route_mode, "hedge_gate_would_refuse_by_reason": dict(sorted(self.hedge_gate_would_refuse.items()))}

    def tick(self, now_ns: int) -> int:
        """Run due timers without a market event (live idle periods)."""
        steps = self.oms.advance_time(now_ns, self.books)
        self._notify_closed_plans()
        self._breaker_step(now_ns)
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
        out: dict[str, Any] = {"flattening": self.flattening, "strategies": report}
        if self.breaker is not None or self.risk_gate_mode != "off":
            out["breaker_and_gate"] = self.breaker_report()
        return out

    def drain(self, horizon_ns: int | None = None) -> dict[str, Any]:
        horizon = self.cfg.drain_horizon_ns if horizon_ns is None else horizon_ns
        result = self.oms.drain(self.last_event_ts_ns + horizon)
        self.finalize_funding()
        return result

    def intent_report(self) -> dict[str, Any] | None:
        """The execution-layer journal of this session (E0 step a), or None when it is off."""
        if self.intent_observer is None:
            return None
        self.intent_observer.finalize(self.last_event_ts_ns / 1e9)
        return self.intent_observer.report(self.oms.plans.values())

    def finalize_funding(self) -> None:
        """Settle everything up to the last market event, never beyond the data."""
        if self.last_event_ts_ns:
            self.funding.settle_until(self.last_event_ts_ns, final=True)
            if self.borrow.configured:
                self.borrow.process(self.last_event_ts_ns)
                self.borrow.settle_until(self.last_event_ts_ns, final=True)
            if self.transfers.configured:
                self.transfers.process(self.last_event_ts_ns)   # never confirmed past the data

    # -- transfers (ECO05) ----------------------------------------------------------
    def _transfer_engine(self, cfg: AppConfig) -> TransferEngine:
        section = cfg.transfers or {}
        rebalance = section.get("rebalance") or {}
        return TransferEngine(
            self.ledger, [TransferRoute.from_config(row) for row in section.get("routes", [])],
            free_balance=lambda venue, asset: self.risk.balance_view(venue, asset)["free"],
            rebalance=[
                RebalanceRule(str(asset), Decimal(str(rule["low_watermark"])), Decimal(str(rule["target"])))
                for asset, rule in (rebalance.get("assets") or {}).items()
            ],
            rebalance_enabled=bool(rebalance.get("enabled", False)),
            cooldown_ns=int(rebalance.get("cooldown_ms", 60_000)) * 1_000_000,
            price=self._asset_mid,
        )

    def _asset_mid(self, venue: str, asset: str) -> Decimal | None:
        for (book_venue, instrument_id), book in self.books.items():
            if book_venue != venue or not (book.bids and book.asks):
                continue
            spec = self.cfg.instruments.get(venue, instrument_id)
            if spec.base == asset and spec.quote == "USDT":
                return (book.best_bid + book.best_ask) / Decimal("2")
        return None

    # -- margin (ECO04) -------------------------------------------------------------
    def _venue_adapter(self, venue: str):
        from .execution import DeterministicVenueAdapter

        return self.router.adapters.setdefault(venue, DeterministicVenueAdapter(venue))

    def _markout_fill(self, event) -> None:
        """One trade fill into the markout tracker: venue, side and role (maker or taker, as the ledger booked it), price and quantity of THIS fill (not the cumulative), time in seconds."""
        details = dict(event.details)
        quote = self.cfg.instruments.get(event.venue, event.instrument_id).quote
        qty = abs(next((p.quantity for p in event.postings if p.account == "main" and p.asset != quote), Decimal("0")))          # the position leg: the base asset on spot, the contract on a perpetual
        self.markouts.record_fill(event.ts_ns / 1e9, event.venue.lower(), str(details.get("side", "")).lower(), float(event.valuation_price), float(qty), str(details.get("liquidity", "")).lower() or "maker")

    def _markout_mids(self) -> dict[str, float]:
        return {venue.lower(): float((book.best_bid + book.best_ask) / 2) for (venue, _), book in self.books.items() if book.bids and book.asks}

    def resolve_markouts(self, now_ns: int, finalize: bool = False) -> None:
        """Resolve the markouts whose horizon has passed against the books' mids now (all of them against the last mids when `finalize`)."""
        if self.markouts is not None and (finalize or self.markouts.due(now_ns / 1e9)):
            self.markouts.resolve(now_ns / 1e9, self._markout_mids(), finalize=finalize)

    def _margin_fill(self, event) -> None:
        if event.venue in self.margin_venues:
            self._margin_dirty = True

    def _liquidation_stop(self, reason: str, now_ns: int) -> None:
        if not self.risk.kill_switch:
            self.oms.activate_kill_switch(
                reason=reason, now_ns=now_ns, operator_id="liquidation-engine", source="LIQUIDATION",
            )

    def _margin_marks(self, now: int) -> dict[tuple[str, str], Decimal]:
        """Marks for the margin check (owner 11 Nov): a venue's own mark price when it arrived within `mark_max_age`, else the book mid of a VALID book. Every mark seen this way is remembered as the last good mark
        and stays usable for up to `margin_config.mark_max_age_ns` (ASSUMED 5 s, stamped when the evaluation saw it, so up to one evaluation interval generous); only after that is the price missing.
        Without this a book that goes quiet for 500 ms (STALE) lost its mark, the position was priced as missing, the venue went into margin BREACH and the kill switch tripped (2026-10-29 capture: every breach sat inside a BINANCE:STALE interval)."""
        max_age = self.margin_config.mark_max_age_ns
        fresh = {
            key: (book.best_bid + book.best_ask) / Decimal("2")
            for key, book in self.books.items()
            if book.state.value == "VALID" and book.bids and book.asks
        }
        for key, (ts, price) in self.exchange_marks.items():
            if now - ts <= max_age:
                fresh[key] = price
        for key, price in fresh.items():
            self._last_good_marks[key] = (now, price)
        marks = dict(fresh)
        for key, (ts, price) in self._last_good_marks.items():
            if key not in marks and now - ts <= max_age:
                marks[key] = price
        return marks

    def _margin_price_step(self, report, now: int) -> None:
        """A missing price pauses new risk and resumes by itself when a fresh price is back; it never trips the kill switch (a real shortfall with a fresh price still does, through the margin BREACH)."""
        missing = sorted(row.venue for row in report.venues if row.details.get("price_missing"))
        if bool(missing) == self.margin_price_paused:
            return
        self.margin_price_paused = bool(missing)
        self.ledger.record("MARGIN_PRICE_PAUSE" if missing else "MARGIN_PRICE_RESUME", now, venues=missing, mark_max_age_s=self.margin_config.mark_max_age_ns / 1e9,
                           **(self.alert_context(now) if missing else {}))      # owner 12 Nov: the pause is an alert, the resume is not

    def _evaluate_margin(self, now: int) -> None:
        due = self._margin_due_ns is None or now >= self._margin_due_ns
        if not (due or self._margin_dirty):
            return
        self._margin_dirty = False
        self._margin_due_ns = now + self.margin_config.evaluate_interval_ns
        marks = self._margin_marks(now)
        report = self.oms.evaluate_margin(
            now_ns=now, mark_prices=marks, initial_capital_by_venue=self.cfg.initial_capital_by_venue,
        )
        if self.margin_config.missing_price_action == "pause":
            self._margin_price_step(report, now)
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

    def on_derivative(self, ticker) -> None:
        """Venue mark/index/funding (derivatives stream): the exchange mark for
        settlements and the predicted rate for funding forecasts, never realised funding."""
        if ticker.mark_price is not None:
            self.exchange_marks[(ticker.venue, ticker.instrument_id)] = (ticker.receive_ts_ns, Decimal(ticker.mark_price))      # the margin check prefers a fresh exchange mark to a book mid (see _margin_marks)
        if not self.funding.configured:
            return
        if ticker.mark_price is not None:
            self.funding.observe_exchange_mark(ticker.venue, ticker.instrument_id, ticker.receive_ts_ns,
                                               ticker.mark_price)
        if ticker.funding_rate is not None and ticker.next_funding_ts_ns:
            self.funding.observe_prediction(ticker.venue, ticker.instrument_id, ticker.funding_rate,
                                            ticker.next_funding_ts_ns, ticker.receive_ts_ns, ticker.mark_price)
        self.derivative_ticks = getattr(self, "derivative_ticks", 0) + 1

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
