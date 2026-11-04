from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import asdict, dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
import time
from typing import Any, Callable, Iterable, Protocol

from .alignment import CrossVenueEligibilityPolicy
from .book_gate import BookIntegrityGate
from .config import AppConfig
from .execution import (
    ORDER_API_SCHEMA_VERSION,
    CanonicalOrderRequest,
    ExecutionCallback,
    ExecutionEventType,
    StrategyDirectedRouter,
    VenueExecutionAdapter,
    VenueOrderStatus,
)
from .ledger import Ledger
from .models import EventType, MarketEvent, Opportunity, ParentPlan, stable_id
from .oms import OMS
from .orderbook import OrderBook
from .replay import deterministic_order
from .risk import RiskEngine
from .strategy import CrossVenueSpreadStrategy
from .credentials import validate_offline_backtest_credentials
from .latency import LatencyModel


STRATEGY_SDK_VERSION = "1.2.0"
MARKET_FEATURE_VERSION = "1.0.0"


class RuntimeMode(StrEnum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    TESTNET = "TESTNET"


@dataclass(frozen=True, slots=True)
class AdvancedBacktestSpec:
    """Configuration exposed by the SDK for latency-aware venue replay."""

    pair: str
    data_root: Path
    start_date: str
    end_date: str
    venues: tuple[str, ...] = ("bybit", "binance")
    execution_mode: str = "taker_taker"
    latency_preset: str = "singapore"
    bybit_fee_preset: str = "bybit_spot_mm"
    binance_fee_preset: str = "binance_perp_vip0"
    min_net_edge_bps: float = 1.0
    safety_buffer_bps: float = 0.5
    max_book_age_ms: float = 500.0
    max_touch_age_ms: float = 300.0
    max_walk_bps: float = 2.0
    max_clip_notional: float = 25_000.0
    max_position_base: float = 0.0
    block_on_toxic: bool = True
    consume_liquidity: bool = True
    include_deltas: bool = True
    seed: int = 7
    max_events: int | None = None
    output_dir: Path | None = None
    warm_start_fill_events: Path | None = None
    progress_every: int = 0


class BacktestAPI:
    """SDK facade for the queue-, depth-, and latency-aware replay engine.

    It is deliberately available only in BACKTEST mode. The engine receives
    historical public data and has no credential or live-order path.
    """

    def __init__(self, mode: RuntimeMode = RuntimeMode.BACKTEST):
        self.mode = mode

    def _config(self, spec: AdvancedBacktestSpec):
        if self.mode != RuntimeMode.BACKTEST:
            raise RuntimeError("advanced backtesting is available only in BACKTEST mode")
        from new_backtest.arb_strategy import ArbConfig
        from new_backtest.backtester import ArbBacktestConfig

        return ArbBacktestConfig(
            pair=spec.pair.upper(),
            data_root=str(spec.data_root),
            venues=tuple(venue.lower() for venue in spec.venues),
            start_date=spec.start_date,
            end_date=spec.end_date,
            max_events=spec.max_events,
            latency_preset=spec.latency_preset,
            bybit_fee_preset=spec.bybit_fee_preset,
            binance_fee_preset=spec.binance_fee_preset,
            arb=ArbConfig(
                mode=spec.execution_mode,
                min_net_edge_bps=spec.min_net_edge_bps,
                safety_buffer_bps=spec.safety_buffer_bps,
                max_book_age_s=spec.max_book_age_ms / 1000.0,
                max_touch_age_s=spec.max_touch_age_ms / 1000.0,
                max_walk_bps=spec.max_walk_bps,
                max_clip_notional=spec.max_clip_notional,
                max_position_base=spec.max_position_base,
                block_on_toxic=spec.block_on_toxic,
            ),
            consume_liquidity=spec.consume_liquidity,
            include_deltas=spec.include_deltas,
            seed=spec.seed,
            output_dir=str(spec.output_dir) if spec.output_dir else None,
            warm_start_fill_events=(
                str(spec.warm_start_fill_events)
                if spec.warm_start_fill_events else None
            ),
            progress_every=spec.progress_every,
        )

    def run(self, spec: AdvancedBacktestSpec) -> dict[str, Any]:
        from new_backtest.backtester import CrossVenueArbBacktester
        from new_backtest import virtual_clock

        try:
            return asdict(CrossVenueArbBacktester(self._config(spec)).run())
        finally:
            # The imported engine virtualizes process-wide time for replay.
            # SDK callers may host multiple runtimes, so never leak that clock.
            virtual_clock.uninstall()

    def run_batch(
        self, spec: AdvancedBacktestSpec, pairs: Iterable[str], *,
        two_pass: bool | None = None,
    ) -> dict[str, Any]:
        from new_backtest.arb_strategy import MAKER_TAKER
        from new_backtest.batch_backtest import run_batch
        from new_backtest import virtual_clock

        config = self._config(spec)
        if spec.output_dir is None:
            raise ValueError("batch backtesting requires output_dir")
        use_two_pass = (
            spec.execution_mode == MAKER_TAKER if two_pass is None else two_pass
        )
        try:
            return asdict(run_batch(
                tuple(pair.upper() for pair in pairs), config,
                str(spec.output_dir), two_pass=use_two_pass, verbose=False,
            ))
        finally:
            virtual_clock.uninstall()


class StrategyProtocol(Protocol):
    strategy_id: str

    def on_event(
        self, event: MarketEvent, context: "StrategyContext"
    ) -> Opportunity | None: ...


class TestnetTransport(Protocol):
    def __call__(
        self, request: dict[str, Any]
    ) -> Iterable[dict[str, Any]]: ...


def _native_liquidity(native: dict[str, Any]) -> str:
    """MAKER/TAKER from a venue fill report, or "" when the venue did not say.

    Accepts a normalized ``liquidity`` field or the venues' own maker flags:
    Binance ``m`` / ``isMaker`` (execution reports and user trades) and Bybit
    ``isMaker``. Without one the OMS infers from post_only and flags the fee
    as LIQUIDITY_INFERRED.
    """
    label = str(native.get("liquidity", "")).upper()
    if label in ("MAKER", "TAKER"):
        return label
    for key in ("isMaker", "is_maker", "m"):
        value = native.get(key)
        if isinstance(value, bool):
            return "MAKER" if value else "TAKER"
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return "MAKER" if value.lower() == "true" else "TAKER"
    return ""


class TestnetVenueAdapter:
    """Synchronous testnet boundary translating native responses to callbacks.

    Network authentication and HTTP/WebSocket I/O remain in the injected
    transport. The SDK and OMS only see the canonical order contract.
    """

    def __init__(
        self, venue: str, transport: TestnetTransport, *,
        clock_ns: Callable[[], int] = time.time_ns,
    ):
        self.venue = venue
        self.transport = transport
        self.clock_ns = clock_ns
        self.requests: list[dict[str, Any]] = []
        self.timings: list[dict[str, Any]] = []
        self.transport_errors: list[dict[str, Any]] = []
        self._submits: dict[str, dict[str, Any]] = {}

    def handle(
        self, request: CanonicalOrderRequest, book: OrderBook | None
    ) -> tuple[ExecutionCallback, ...]:
        if request.venue != self.venue:
            raise ValueError("testnet adapter venue does not match request")
        payload = request.to_dict()
        self.requests.append(payload)
        if request.action.value != "CANCEL":
            self._submits[request.order_id] = payload
        sent_ts = self.clock_ns()
        try:
            natives = tuple(self.transport(payload))
        except Exception as exc:
            # No answer: the order state is unknown. Returning no callbacks
            # makes the OMS query the venue at the report deadline; the
            # router's idempotency cache guarantees it is never resent.
            self.transport_errors.append({
                "order_id": request.order_id, "action": request.action.value,
                "ts_ns": sent_ts, "error": type(exc).__name__,
                "message": str(exc)[:200],
            })
            return ()
        self.timings.append({
            "order_id": request.order_id, "venue": self.venue,
            "action": request.action.value, "order_send_ts_ns": sent_ts,
            "response_ts_ns": self.clock_ns(),
            "events": [native.get("event_type") for native in natives],
        })
        callbacks = []
        for sequence, native in enumerate(natives):
            if "event_type" not in native:
                raise ValueError("testnet response requires event_type")
            event_type = ExecutionEventType(str(native["event_type"]))
            cumulative = Decimal(str(native.get("cumulative_filled_quantity", "0")))
            last_fill = Decimal(str(native.get("last_fill_quantity", "0")))
            average = Decimal(str(native.get("average_price", "0")))
            event_ts_ns = int(native.get("event_ts_ns", request.created_ts_ns))
            callbacks.append(ExecutionCallback(
                schema_version=ORDER_API_SCHEMA_VERSION,
                callback_id=str(native.get("callback_id") or stable_id(
                    "testnet-callback", request.request_id, sequence, event_type,
                    cumulative, native.get("reason", ""),
                )),
                request_id=request.request_id,
                order_id=request.order_id,
                parent_opportunity_id=request.parent_opportunity_id,
                parent_order_id=request.parent_order_id,
                venue=request.venue,
                instrument_id=request.instrument_id,
                event_type=event_type,
                event_ts_ns=event_ts_ns,
                cumulative_filled_quantity=cumulative,
                last_fill_quantity=last_fill,
                average_price=average,
                reason=str(native.get("reason", "")),
                liquidity=_native_liquidity(native),
                native={"adapter": "strategy-sdk-testnet-v1", "response": dict(native)},
            ))
        return tuple(callbacks)

    def query_order(self, order_id: str) -> VenueOrderStatus | None:
        original = self._submits.get(order_id)
        if original is None:
            return VenueOrderStatus(
                order_id=order_id, venue=self.venue, instrument_id="", state=None,
                reason="order was never sent by this adapter",
            )
        natives = tuple(self.transport({**original, "action": "QUERY"}))
        if not natives:
            return None
        native = natives[-1]
        event_type = native.get("event_type")
        return VenueOrderStatus(
            order_id=order_id, venue=self.venue,
            instrument_id=str(original["instrument_id"]),
            state=ExecutionEventType(str(event_type)) if event_type else None,
            cumulative_filled_quantity=Decimal(
                str(native.get("cumulative_filled_quantity", "0"))
            ),
            average_price=Decimal(str(native.get("average_price", "0"))),
            reason=str(native.get("reason", "")),
            venue_ts_ns=int(native.get("event_ts_ns", 0)),
            venue_order_id=str(native.get("venue_order_id", "")),
        )


class LookaheadError(RuntimeError):
    """A feature used information not available at its timestamp."""


@dataclass(frozen=True, slots=True)
class FeatureDefinition:
    name: str
    version: str
    description: str = ""
    inputs: tuple[str, ...] = ()

    @property
    def definition_id(self) -> str:
        return stable_id("feature-definition", self.name, self.version, *self.inputs)


@dataclass(frozen=True, slots=True)
class FeatureValue:
    name: str
    value: Any
    ts_ns: int
    source_event_ids: tuple[str, ...]
    version: str = "unversioned"
    definition_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FeatureAPI:
    """Versioned, point-in-time feature store with a no-lookahead guard.

    * Every feature has a registered version; publishing under a different
      version fails, so a changed definition needs a new release.
    * With a runtime clock, publishing a value stamped after "now", or built
      from an event received after the value's timestamp, raises
      ``LookaheadError``.
    * ``latest(name, as_of_ns=...)`` returns the newest value at or before the
      requested time, never a later one.
    """

    def __init__(
        self, clock_ns: Callable[[], int] | None = None, *,
        history_per_feature: int = 1_024, max_tracked_events: int = 100_000,
    ) -> None:
        self._latest: dict[str, FeatureValue] = {}
        self._history: dict[str, deque[FeatureValue]] = {}
        self._definitions: dict[str, FeatureDefinition] = {}
        self._event_receive_ts: OrderedDict[str, int] = OrderedDict()
        self.clock_ns = clock_ns
        self.history_per_feature = history_per_feature
        self.max_tracked_events = max_tracked_events
        self.unverified_sources = 0

    def register(
        self, name: str, version: str, *, description: str = "",
        inputs: tuple[str, ...] = (),
    ) -> FeatureDefinition:
        if not name or not version:
            raise ValueError("feature name and version are required")
        existing = self._definitions.get(name)
        definition = FeatureDefinition(name, version, description, tuple(inputs))
        if existing is not None and existing != definition:
            raise ValueError(
                f"feature {name} is already registered as version {existing.version}"
            )
        self._definitions[name] = definition
        return definition

    def observe_event(self, event: MarketEvent) -> None:
        self._event_receive_ts[event.event_id] = event.receive_ts_ns
        while len(self._event_receive_ts) > self.max_tracked_events:
            self._event_receive_ts.popitem(last=False)

    def publish(
        self, name: str, value: Any, *, ts_ns: int,
        source_event_ids: tuple[str, ...] = (), version: str | None = None,
    ) -> FeatureValue:
        if not name:
            raise ValueError("feature name is required")
        definition = self._definitions.get(name)
        if definition is None and version is not None:
            definition = self.register(name, version)
        if definition is not None and version is not None and version != definition.version:
            raise ValueError(
                f"feature {name} version {version} does not match registered "
                f"{definition.version}"
            )
        if self.clock_ns is not None:
            now = int(self.clock_ns())
            if ts_ns > now:
                raise LookaheadError(
                    f"feature {name} stamped {ts_ns} after runtime clock {now}"
                )
        for source in source_event_ids:
            received = self._event_receive_ts.get(source)
            if received is None:
                self.unverified_sources += 1
            elif received > ts_ns:
                raise LookaheadError(
                    f"feature {name} at {ts_ns} uses event {source} received at {received}"
                )
        history = self._history.setdefault(
            name, deque(maxlen=self.history_per_feature)
        )
        if history and history[-1].ts_ns > ts_ns:
            raise LookaheadError(f"feature {name} published out of time order")
        row = FeatureValue(
            name, value, ts_ns, tuple(source_event_ids),
            definition.version if definition is not None else "unversioned",
            definition.definition_id if definition is not None else "",
        )
        history.append(row)
        self._latest[name] = row
        return row

    def latest(
        self, name: str, default: Any = None, *, as_of_ns: int | None = None
    ) -> Any:
        row = self.value_at(name, as_of_ns)
        return default if row is None else row.value

    def value_at(self, name: str, as_of_ns: int | None = None) -> FeatureValue | None:
        if as_of_ns is None and self.clock_ns is not None:
            as_of_ns = int(self.clock_ns())
        if as_of_ns is None:
            return self._latest.get(name)
        for row in reversed(self._history.get(name, ())):
            if row.ts_ns <= as_of_ns:
                return row
        return None

    def definitions(self) -> dict[str, dict[str, Any]]:
        return {
            name: {**asdict(item), "definition_id": item.definition_id}
            for name, item in sorted(self._definitions.items())
        }

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            name: row.to_dict() for name, row in sorted(self._latest.items())
        }


class CanonicalDataAPI:
    def __init__(self, books: dict[tuple[str, str], OrderBook]):
        self.books = books
        self.latest_event: MarketEvent | None = None
        self.event_count = 0

    def book(self, venue: str, instrument_id: str) -> OrderBook:
        try:
            return self.books[(venue, instrument_id)]
        except KeyError as exc:
            raise ValueError(f"unknown SDK book: {venue}:{instrument_id}") from exc

    def top(self, venue: str, instrument_id: str) -> dict[str, Any]:
        book = self.book(venue, instrument_id)
        return {
            "state": book.state.value,
            "bid": book.best_bid if book.bids else None,
            "ask": book.best_ask if book.asks else None,
            "sequence": book.sequence,
            "event_id": book.last_event_id,
        }


class PortfolioAPI:
    def __init__(self, oms: OMS, data: CanonicalDataAPI, config: AppConfig):
        self._oms = oms
        self._data = data
        self._config = config

    def snapshot(self) -> dict[str, object]:
        marks = {
            key: (book.best_bid + book.best_ask) / Decimal("2")
            for key, book in self._data.books.items() if book.bids and book.asks
        }
        return self._oms.recalculate_account_state(
            mark_prices=marks,
            initial_capital_by_venue=self._config.initial_capital_by_venue,
        )


class RiskAPI:
    def __init__(self, risk: RiskEngine):
        self._risk = risk

    @property
    def new_risk_allowed(self) -> bool:
        return not self._risk.kill_switch

    def capacity(self) -> dict[str, object]:
        return self._risk.capacity_snapshot()

    def kill_switch(self) -> dict[str, Any]:
        return self._risk.kill_switch_snapshot()


class OMSAPI:
    def __init__(self, oms: OMS, ledger: Ledger, data: CanonicalDataAPI):
        self._oms = oms
        self._ledger = ledger
        self._data = data

    def submit(self, opportunity: Opportunity) -> ParentPlan:
        self._ledger.record(
            "OPPORTUNITY", opportunity.observed_ts_ns,
            opportunity=asdict(opportunity), sdk_version=STRATEGY_SDK_VERSION,
        )
        return self._oms.execute(opportunity, self._data.books)

    def plan(self, parent_id: str) -> ParentPlan:
        return self._oms.plans[parent_id]

    def plans(self) -> tuple[ParentPlan, ...]:
        return tuple(self._oms.plans[key] for key in sorted(self._oms.plans))


@dataclass(slots=True)
class StrategyContext:
    sdk_version: str
    mode: RuntimeMode
    data: CanonicalDataAPI
    features: FeatureAPI
    portfolio: PortfolioAPI
    risk: RiskAPI
    oms: OMSAPI
    backtest: BacktestAPI
    now_ns: int = 0


@dataclass(frozen=True, slots=True)
class StrategyRunResult:
    sdk_version: str
    mode: RuntimeMode
    strategy_id: str
    event_count: int
    opportunity_count: int
    plan_ids: tuple[str, ...]
    feature_names: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["mode"] = self.mode.value
        return row


class StrategyRuntime:
    """Stable mode-neutral strategy host over canonical platform interfaces."""

    def __init__(
        self, config: AppConfig, strategy: StrategyProtocol, *,
        mode: RuntimeMode = RuntimeMode.PAPER,
        adapters: dict[str, VenueExecutionAdapter] | None = None,
        credential_ids: tuple[str, ...] = (),
        latency_model: LatencyModel | None = None,
    ):
        if mode == RuntimeMode.BACKTEST:
            validate_offline_backtest_credentials(credential_ids)
        if mode == RuntimeMode.TESTNET and set(adapters or {}) != set(config.venues):
            raise ValueError("testnet mode requires one explicit adapter per venue")
        self.config = config
        self.strategy = strategy
        self.mode = mode
        self.ledger = Ledger()
        self.risk_engine = RiskEngine(
            config.risk_limits, config.account_risk_snapshots,
            inventory=config.inventory_policy,
        )
        self.eligibility = CrossVenueEligibilityPolicy(
            config.venues, config.instrument_id, config.max_book_age_ns
        )
        self.router = StrategyDirectedRouter(
            config.instruments, adapters=adapters,
            # Opt-in: the SDK acceptance contract compares order requests
            # across modes. TradingSession/live-paper apply config latency.
            latency_model=None if mode == RuntimeMode.TESTNET else latency_model,
        )
        self.oms_engine = OMS(
            config.instruments, self.risk_engine, self.ledger,
            eligibility_policy=self.eligibility, router=self.router,
            residual_policy=config.residual_policy,
            order_timeout_policy=config.order_timeout_policy,
        )
        books = {
            (venue, config.instrument_id): config.book_sanity.new_book(
                venue, config.instrument_id
            )
            for venue in config.venues
        }
        data = CanonicalDataAPI(books)
        self.book_gate = BookIntegrityGate(
            config.max_book_age_ns, self.oms_engine.cancel_open_orders,
            venue_control_callback=self.oms_engine.apply_feed_control,
        )
        self.context = StrategyContext(
            sdk_version=STRATEGY_SDK_VERSION,
            mode=mode,
            data=data,
            features=FeatureAPI(clock_ns=lambda: self.context.now_ns),
            portfolio=PortfolioAPI(self.oms_engine, data, config),
            risk=RiskAPI(self.risk_engine),
            oms=OMSAPI(self.oms_engine, self.ledger, data),
            backtest=BacktestAPI(mode),
        )

    def _publish_market_features(self, event: MarketEvent, book: OrderBook) -> None:
        if not book.is_tradable:
            return
        mid = (book.best_bid + book.best_ask) / Decimal("2")
        spread_bps = (book.best_ask - book.best_bid) / mid * Decimal("10000")
        prefix = f"{event.venue}:{event.instrument_id}"
        sources = (event.event_id,)
        self.context.features.publish(
            f"{prefix}:mid", mid, ts_ns=event.receive_ts_ns,
            source_event_ids=sources, version=MARKET_FEATURE_VERSION,
        )
        self.context.features.publish(
            f"{prefix}:spread_bps", spread_bps, ts_ns=event.receive_ts_ns,
            source_event_ids=sources, version=MARKET_FEATURE_VERSION,
        )

    def run(self, events: Iterable[MarketEvent]) -> StrategyRunResult:
        ordered = deterministic_order(list(events))
        starter = getattr(self.strategy, "on_start", None)
        if starter is not None:
            starter(self.context)
        for event in ordered:
            key = (event.venue, event.instrument_id)
            if key not in self.context.data.books:
                raise ValueError(f"event outside configured SDK universe: {key}")
            self.context.now_ns = event.receive_ts_ns
            self.context.data.latest_event = event
            self.context.data.event_count += 1
            self.context.features.observe_event(event)
            if event.event_type not in {EventType.BOOK_SNAPSHOT, EventType.BOOK_DELTA}:
                continue
            self.oms_engine.advance_time(event.receive_ts_ns, self.context.data.books)
            book = self.context.data.books[key]
            book.apply(event)
            self.oms_engine.on_book_update(
                event.venue, event.instrument_id, event.receive_ts_ns
            )
            self._publish_market_features(event, book)
            required = tuple(
                self.context.data.book(venue, self.config.instrument_id)
                for venue in self.config.venues
            )
            if not self.book_gate.allow(required, event.receive_ts_ns):
                continue
            opportunity = self.strategy.on_event(event, self.context)
            if opportunity is not None:
                self.context.oms.submit(opportunity)
        if ordered:
            self.oms_engine.drain(
                ordered[-1].receive_ts_ns + self.config.drain_horizon_ns
            )
        stopper = getattr(self.strategy, "on_stop", None)
        if stopper is not None:
            stopper(self.context)
        plans = self.context.oms.plans()
        return StrategyRunResult(
            sdk_version=STRATEGY_SDK_VERSION,
            mode=self.mode,
            strategy_id=self.strategy.strategy_id,
            event_count=len(ordered),
            opportunity_count=len(plans),
            plan_ids=tuple(plan.parent_id for plan in plans),
            feature_names=tuple(self.context.features.snapshot()),
        )


class CrossVenueSDKStrategy:
    """Minimal port of the existing strategy to the stable SDK protocol."""

    def __init__(self, config: AppConfig):
        self.strategy_id = config.strategy_id
        self._signal = CrossVenueSpreadStrategy(
            config.strategy_id, config.instrument_id, config.venues,
            config.quantity, config.entry_bps, config.max_book_age_ns,
            config.min_net_edge_bps, config.instruments,
            CrossVenueEligibilityPolicy(
                config.venues, config.instrument_id, config.max_book_age_ns
            ),
        )

    def on_event(
        self, event: MarketEvent, context: StrategyContext
    ) -> Opportunity | None:
        opportunity = self._signal.evaluate(context.data.books, context.now_ns)
        if opportunity is not None:
            context.features.publish(
                "cross_venue:expected_net_edge_bps",
                opportunity.estimated_net_edge_bps,
                ts_ns=context.now_ns,
                source_event_ids=opportunity.source_event_ids,
                version=MARKET_FEATURE_VERSION,
            )
        return opportunity


def immediate_fill_testnet_transport(
    request: dict[str, Any]
) -> tuple[dict[str, Any], ...]:
    """Deterministic test transport for SDK examples and acceptance tests."""
    return (
        {"event_type": "ACKNOWLEDGED", "event_ts_ns": request["created_ts_ns"]},
        {
            "event_type": "FILLED",
            "event_ts_ns": request["created_ts_ns"],
            "cumulative_filled_quantity": request["quantity"],
            "last_fill_quantity": request["quantity"],
            "average_price": request["price"],
        },
    )
