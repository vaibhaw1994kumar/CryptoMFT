from __future__ import annotations

import asyncio
import importlib
import json
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Sequence
from uuid import NAMESPACE_URL, uuid5

from trading_platform_poc.binance_execution import (
    BinanceAmbiguousOrderError,
    BinanceAPIError,
    BinanceCredentials,
    BinanceGatewayError,
    BinanceOrderResult,
    BinanceSpotTestnetGateway,
)
from trading_platform_poc.config import AppConfig
from trading_platform_poc.deribit_test import (
    DeribitCredentials,
    DeribitTestAdapter,
    DeribitTestConfig,
)
from trading_platform_poc.exchange_adapter import (
    AccountSnapshot,
    AmbiguousOrderError,
    ExchangeAdapter,
    ExchangeAdapterError,
    ExchangeAPIError,
    ExchangeOrder,
    ExchangeTrade,
    InstrumentRules,
    NormalizedOrder,
    QuantityUnit,
    ReconciliationSnapshot,
)
from trading_platform_poc.models import Order, OrderState, OrderType, Side
from trading_platform_poc.okx_demo import (
    OKXCredentials,
    OKXDemoAdapter,
    OKXDemoConfig,
)


class _NativeProxy:
    module: Any | None = None

    def __getattr__(self, name: str) -> Any:
        if self.module is None:
            self.module = importlib.import_module("hft_platform_kit._ems")
        return getattr(self.module, name)


_native = _NativeProxy()
_SUPPORTED_VENUES = frozenset({"binance", "okx", "deribit"})
_FILLED_STATUSES = frozenset({"filled"})
_OPEN_STATUSES = frozenset(
    {
        "accepted",
        "cancel_requested",
        "live",
        "new",
        "open",
        "partially_filled",
        "untriggered",
    }
)
_CANCELED_STATUSES = frozenset(
    {"canceled", "cancelled", "expired", "mmp_canceled"}
)
_REJECTED_STATUSES = frozenset({"rejected"})


class DispatchPolicy(str, Enum):
    SEQUENTIAL = "sequential"
    SIMULTANEOUS = "simultaneous"


class ExecutionOutcome(str, Enum):
    OPEN = "open"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELED = "canceled"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"
    HEDGE_REQUIRED = "hedge_required"
    UNWIND_REQUIRED = "unwind_required"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    RECOVERED = "recovered"
    OPERATOR_RESOLVED = "operator_resolved"


class RouteOrderStatus(str, Enum):
    PENDING = "pending"
    SUBMITTING = "submitting"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"
    SKIPPED = "skipped"


class ScheduleState(str, Enum):
    SCHEDULED = "scheduled"
    RUNNING = "running"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    CANCEL_REQUESTED = "cancel_requested"
    RECONCILING = "reconciling"
    RECOVERY_REQUIRED = "recovery_required"
    UNWIND_REQUIRED = "unwind_required"
    RECOVERED = "recovered"
    FILLED = "filled"
    CANCELED = "canceled"
    FAILED = "failed"
    OPERATOR_RESOLVED = "operator_resolved"


class ScheduleRiskLimitError(RuntimeError):
    pass


class UnexecutableResidualError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VenueQuote:
    venue: str
    symbol: str
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    fee_bps: Decimal = Decimal(0)
    latency_us: Decimal = Decimal(0)
    healthy: bool = True


@dataclass(frozen=True, slots=True)
class InstrumentMapping:
    canonical_symbol: str
    venue: str
    venue_symbol: str
    compatibility_group: str
    canonical_quantity_unit: QuantityUnit = QuantityUnit.BASE
    venue_quantity_unit: QuantityUnit = QuantityUnit.BASE
    contract_size_quote: Decimal | None = None

    def __post_init__(self) -> None:
        if not all(
            (
                self.canonical_symbol,
                self.venue,
                self.venue_symbol,
                self.compatibility_group,
            )
        ):
            raise ValueError("instrument mapping identifiers must not be empty")
        uses_contracts = QuantityUnit.CONTRACTS in {
            self.canonical_quantity_unit,
            self.venue_quantity_unit,
        }
        if uses_contracts and self.contract_size_quote is None:
            raise ValueError(
                "contract_size_quote is required for contract conversion"
            )
        if not uses_contracts and self.contract_size_quote is not None:
            raise ValueError(
                "contract_size_quote is only valid for contract conversion"
            )


@dataclass(frozen=True, slots=True)
class ExecutionLeg:
    symbol: str
    side: Side
    quantity: Decimal
    reference_price: Decimal
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None
    venue_preferences: tuple[str, ...] = ()
    hedge: bool = False

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("leg symbol must not be empty")
        if self.quantity <= 0 or self.reference_price <= 0:
            raise ValueError("leg quantity and reference price must be positive")
        if self.order_type is not OrderType.MARKET and self.limit_price is None:
            raise ValueError("limit and post-only legs require limit_price")
        if len(set(self.venue_preferences)) != len(self.venue_preferences):
            raise ValueError("venue preferences must be unique")


@dataclass(frozen=True, slots=True)
class ExecutionPlanRequest:
    plan_id: str
    legs: tuple[ExecutionLeg, ...]
    dispatch_policy: DispatchPolicy | None = None

    def __post_init__(self) -> None:
        if not self.plan_id:
            raise ValueError("plan_id must not be empty")
        if len(self.legs) < 2:
            raise ValueError("multi-leg execution requires at least two legs")


@dataclass(slots=True)
class RouteOrderResult:
    leg_index: int
    slice_index: int
    venue: str
    order: Order
    expected_fee: Decimal
    score: Decimal
    canonical_symbol: str
    canonical_quantity: Decimal
    quantity_mapping: InstrumentMapping | None = None
    reference_price: Decimal | None = None
    status: RouteOrderStatus = RouteOrderStatus.PENDING
    exchange_order: ExchangeOrder | None = None
    error: str | None = None
    cancel_error: str | None = None
    reconciliation_error: str | None = None
    cumulative_filled_quantity: Decimal = Decimal(0)
    reconciled: bool = False

    @property
    def filled_quantity(self) -> Decimal:
        if self.exchange_order is None:
            return self.cumulative_filled_quantity
        observed = _canonical_exchange_fill(self, self.exchange_order)
        if observed is None:
            return self.cumulative_filled_quantity
        return max(self.cumulative_filled_quantity, observed)


@dataclass(slots=True)
class ExecutionLegResult:
    leg_index: int
    leg: ExecutionLeg
    orders: list[RouteOrderResult] = field(default_factory=list)

    @property
    def filled_quantity(self) -> Decimal:
        return sum(
            (order.filled_quantity for order in self.orders),
            start=Decimal(0),
        )


@dataclass(frozen=True, slots=True)
class PlanRecoveryOrderEvidence:
    recovery_id: str
    canonical_symbol: str
    side: Side
    filled_quantity: Decimal
    venue: str
    exchange_order_id: str | None
    reconciliation_evidence: str
    status: RouteOrderStatus = RouteOrderStatus.FILLED
    reconciled: bool = True
    client_order_id: str | None = None

    def __post_init__(self) -> None:
        if not all(
            (
                self.recovery_id.strip(),
                self.canonical_symbol.strip(),
                self.venue.strip(),
                self.reconciliation_evidence.strip(),
            )
        ):
            raise ValueError("plan recovery order evidence must be complete")
        if not (
            (self.exchange_order_id or "").strip()
            or (self.client_order_id or "").strip()
        ):
            raise ValueError(
                "plan recovery order evidence requires an exchange or client "
                "order id"
            )
        if self.filled_quantity <= 0:
            raise ValueError("plan recovery filled quantity must be positive")


@dataclass(frozen=True, slots=True)
class ScheduleRecoveryOrderEvidence:
    venue: str
    status: RouteOrderStatus
    reconciliation_evidence: str
    exchange_order_id: str | None = None
    client_order_id: str | None = None
    filled_quantity: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        if not self.venue.strip() or not self.reconciliation_evidence.strip():
            raise ValueError("schedule recovery order evidence must be complete")
        if not (
            (self.exchange_order_id or "").strip()
            or (self.client_order_id or "").strip()
        ):
            raise ValueError(
                "schedule recovery order evidence requires an exchange or "
                "client order id"
            )
        if self.status not in {
            RouteOrderStatus.FILLED,
            RouteOrderStatus.CANCELED,
            RouteOrderStatus.REJECTED,
            RouteOrderStatus.FAILED,
        }:
            raise ValueError(
                "schedule recovery order evidence must establish a terminal "
                "status"
            )
        if self.filled_quantity < 0:
            raise ValueError(
                "schedule recovery filled quantity must not be negative"
            )


@dataclass(slots=True)
class ExecutionPlanResult:
    plan_id: str
    dispatch_policy: DispatchPolicy
    legs: list[ExecutionLegResult]
    outcome: ExecutionOutcome = ExecutionOutcome.OPEN
    native_state: str = "CREATED"
    state_history: list[str] = field(default_factory=lambda: ["CREATED"])
    recovery_actions: list[str] = field(default_factory=list)
    recovery_orders: list[PlanRecoveryOrderEvidence] = field(
        default_factory=list
    )
    recovery_completed: bool = False
    operator_resolved: bool = False
    operator_resolution_reason: str | None = None
    operator_resolution_evidence: str | None = None
    risk_released: bool = False

    @property
    def orders(self) -> tuple[RouteOrderResult, ...]:
        return tuple(order for leg in self.legs for order in leg.orders)

    @property
    def residual_canonical_exposure_by_symbol(
        self,
    ) -> dict[str, Decimal]:
        if self.recovery_completed or self.operator_resolved:
            return {}
        exposure: dict[str, Decimal] = {}
        for routed in self.orders:
            signed_quantity = (
                routed.filled_quantity
                if routed.order.side is Side.BUY
                else -routed.filled_quantity
            )
            exposure[routed.canonical_symbol] = (
                exposure.get(routed.canonical_symbol, Decimal(0))
                + signed_quantity
            )
        for recovery in self.recovery_orders:
            symbol = recovery.canonical_symbol.upper()
            signed_quantity = (
                recovery.filled_quantity
                if recovery.side is Side.BUY
                else -recovery.filled_quantity
            )
            exposure[symbol] = (
                exposure.get(symbol, Decimal(0)) + signed_quantity
            )
        return {
            symbol: quantity
            for symbol, quantity in exposure.items()
            if quantity != 0
        }

    @property
    def residual_canonical_exposure(self) -> Decimal:
        return sum(
            (
                abs(quantity)
                for quantity in self.residual_canonical_exposure_by_symbol.values()
            ),
            start=Decimal(0),
        )


@dataclass(frozen=True, slots=True)
class ScheduledExecution:
    schedule_id: str
    leg: ExecutionLeg
    slices: tuple[tuple[int, Decimal], ...]
    requested_quantity: Decimal
    normalized_quantity: Decimal
    residual_quantity: Decimal


@dataclass(slots=True)
class ScheduledSliceResult:
    schedule_id: str
    slice_index: int
    timestamp_ns: int
    quantity: Decimal
    outcome: ExecutionOutcome
    orders: tuple[RouteOrderResult, ...]


@dataclass(slots=True)
class ScheduleResult:
    schedule_id: str
    requested_quantity: Decimal
    state: ScheduleState = ScheduleState.SCHEDULED
    routes: list[RouteOrderResult] = field(default_factory=list)
    slices: list[ScheduledSliceResult] = field(default_factory=list)
    queued_slice_count: int = 0
    cancel_requested: bool = False
    risk_released: bool = False
    recovery_completed: bool = False
    operator_resolved: bool = False
    operator_resolution_reason: str | None = None
    operator_resolution_evidence: str | None = None
    operator_order_evidence: list[ScheduleRecoveryOrderEvidence] = field(
        default_factory=list
    )
    recovery_orders: list[PlanRecoveryOrderEvidence] = field(
        default_factory=list
    )
    state_history: list[ScheduleState] = field(
        default_factory=lambda: [ScheduleState.SCHEDULED]
    )

    @property
    def orders(self) -> tuple[RouteOrderResult, ...]:
        return tuple(self.routes)

    @property
    def cumulative_filled_quantity(self) -> Decimal:
        return sum(
            (order.filled_quantity for order in self.routes),
            start=Decimal(0),
        )

    @property
    def remaining_quantity(self) -> Decimal:
        return max(
            Decimal(0),
            self.requested_quantity - self.cumulative_filled_quantity,
        )

    @property
    def residual_canonical_exposure(self) -> Decimal:
        if self.recovery_completed or self.operator_resolved:
            return Decimal(0)
        if (
            not self.queued_slice_count
            and self.routes
            and all(
                order.status is RouteOrderStatus.FILLED
                and order.reconciled
                and order.reconciliation_error is None
                and order.cancel_error is None
                for order in self.routes
            )
            and self.cumulative_filled_quantity == self.requested_quantity
        ):
            return Decimal(0)
        signed_exposure = sum(
            (
                order.filled_quantity
                if order.order.side is Side.BUY
                else -order.filled_quantity
                for order in self.routes
            ),
            start=Decimal(0),
        )
        signed_exposure += sum(
            (
                order.filled_quantity
                if order.side is Side.BUY
                else -order.filled_quantity
                for order in self.recovery_orders
            ),
            start=Decimal(0),
        )
        return abs(signed_exposure)

    @property
    def reconciliation_errors(self) -> tuple[str, ...]:
        return tuple(
            message
            for order in self.routes
            for message in (order.reconciliation_error, order.cancel_error)
            if message
        )


@dataclass(frozen=True, slots=True)
class _PlannedRoute:
    venue: str
    quantity: Decimal
    venue_quantity: Decimal
    canonical_symbol: str
    venue_symbol: str
    reference_price: Decimal
    expected_fee: Decimal
    score: Decimal
    order_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class _ScheduledEvent:
    schedule_id: str
    slice_index: int
    timestamp_ns: int
    leg: ExecutionLeg
    routes: tuple[_PlannedRoute, ...]
    orders: tuple[RouteOrderResult, ...]
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class _RouteRules:
    venue: str
    step: Decimal
    minimum: Decimal
    capacity: Decimal
    maximum_per_order: Decimal | None
    mapping: InstrumentMapping


class BinanceTestnetAdapter:
    """Common-adapter facade over the guarded Binance Spot Testnet gateway."""

    venue = "binance"
    last_request = None

    def __init__(self, gateway: BinanceSpotTestnetGateway) -> None:
        self.gateway = gateway

    def client_order_id(self, order: Order) -> str:
        return order.client_order_id

    async def close(self) -> None:
        await self.gateway.close()

    async def instrument_rules(self, symbol: str) -> InstrumentRules:
        rules = await self.gateway.symbol_rules(symbol)
        return InstrumentRules(
            venue=self.venue,
            symbol=rules.symbol,
            tick_size=rules.tick_size,
            quantity_step=rules.step_size,
            minimum_quantity=rules.minimum_quantity,
            maximum_limit_quantity=rules.maximum_quantity,
            maximum_market_quantity=rules.market_maximum_quantity,
            base_currency=rules.base_asset,
            quote_currency=rules.quote_asset,
        )

    async def normalize_order(
        self,
        order: Order,
        *,
        reference_price: Decimal | None = None,
    ) -> NormalizedOrder:
        normalized = await self.gateway.normalize_order(
            order,
            reference_price,
        )
        return NormalizedOrder(
            quantity=normalized.quantity,
            price=normalized.price,
            client_order_id=order.client_order_id,
        )

    async def submit_order(self, order: Order) -> ExchangeOrder:
        try:
            result = await self.gateway.submit_order(order)
        except BinanceAmbiguousOrderError as error:
            raise AmbiguousOrderError(
                self.venue,
                error.client_order_id,
            ) from error
        return _binance_order(result)

    async def cancel_order(
        self,
        symbol: str,
        *,
        exchange_order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> ExchangeOrder:
        try:
            result = await self.gateway.cancel_order(
                symbol,
                order_id=(
                    int(exchange_order_id)
                    if exchange_order_id is not None
                    else None
                ),
                client_order_id=client_order_id,
            )
        except BinanceAmbiguousOrderError as error:
            raise AmbiguousOrderError(
                self.venue,
                error.client_order_id,
            ) from error
        return _binance_order(result)

    async def query_order(
        self,
        symbol: str,
        *,
        exchange_order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> ExchangeOrder:
        result = await self.gateway.query_order(
            symbol,
            order_id=(
                int(exchange_order_id)
                if exchange_order_id is not None
                else None
            ),
            client_order_id=client_order_id,
        )
        return _binance_order(result)

    async def open_orders(
        self,
        symbol: str | None = None,
    ) -> tuple[ExchangeOrder, ...]:
        return tuple(
            _binance_order(order)
            for order in await self.gateway.open_orders(symbol)
        )

    async def trades(
        self,
        symbol: str,
        *,
        limit: int = 100,
    ) -> tuple[ExchangeTrade, ...]:
        rows = await self.gateway.my_trades(symbol, limit=limit)
        return tuple(_binance_trade(symbol, row) for row in rows)

    async def account(self, currency: str | None = None) -> AccountSnapshot:
        raw = await self.gateway.account()
        return AccountSnapshot(
            venue=self.venue,
            currency=currency,
            timestamp_ms=int(raw["updateTime"]) if raw.get("updateTime") else None,
            raw=raw,
        )

    async def reconcile(
        self,
        oms: Any,
        symbol: str,
    ) -> ReconciliationSnapshot:
        report = await self.gateway.reconcile(oms, symbol)
        trades = tuple(
            _binance_trade(symbol, row) for row in report.recent_trades
        )
        return ReconciliationSnapshot(
            venue=self.venue,
            symbol=symbol.upper(),
            captured_at_ms=int(time.time() * 1000),
            exchange_open_orders=tuple(
                _binance_order(order) for order in report.exchange_open_orders
            ),
            recent_trades=trades,
            account=AccountSnapshot(
                venue=self.venue,
                currency=None,
                timestamp_ms=None,
                raw=report.account,
            ),
            missing_on_exchange=report.missing_on_exchange,
            unknown_on_exchange=report.unknown_on_exchange,
        )


class ExecutionCoordinator:
    """Single-loop Python coordinator around the native execution core."""

    def __init__(
        self,
        adapters: Mapping[str, ExchangeAdapter],
        *,
        max_reserved_notional: Decimal = Decimal("1000000"),
        latency_penalty_bps_per_ms: Decimal = Decimal(0),
        dispatch_policy: DispatchPolicy = DispatchPolicy.SEQUENTIAL,
        instrument_mappings: Sequence[InstrumentMapping] | None = None,
    ) -> None:
        normalized = {venue.lower(): adapter for venue, adapter in adapters.items()}
        unsupported = set(normalized) - _SUPPORTED_VENUES
        if unsupported:
            raise ValueError(
                f"unsupported execution venues: {', '.join(sorted(unsupported))}"
            )
        for venue, adapter in normalized.items():
            if adapter.venue.lower() != venue:
                raise ValueError(f"adapter venue mismatch for {venue}")
        if max_reserved_notional <= 0 or latency_penalty_bps_per_ms < 0:
            raise ValueError("EMS risk limit must be positive and latency non-negative")
        self.adapters = normalized
        self.dispatch_policy = dispatch_policy
        mappings = tuple(instrument_mappings or _default_instrument_mappings())
        self._mappings: dict[tuple[str, str], InstrumentMapping] = {}
        groups: dict[str, str] = {}
        for mapping in mappings:
            key = (
                mapping.canonical_symbol.upper(),
                mapping.venue.lower(),
            )
            if key in self._mappings:
                raise ValueError(f"duplicate instrument mapping: {key}")
            existing_group = groups.setdefault(
                key[0],
                mapping.compatibility_group,
            )
            if existing_group != mapping.compatibility_group:
                raise ValueError(
                    f"incompatible product groups for {key[0]}"
                )
            self._mappings[key] = mapping
        self._product_groups = groups
        self._ems = _native.ExecutionManagementSystem(
            float(latency_penalty_bps_per_ms)
        )
        self._risk = _native.RiskReservationBook(float(max_reserved_notional))
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread_id: int | None = None
        self._plans: dict[str, ExecutionPlanResult] = {}
        self._plan_locks: dict[str, asyncio.Lock] = {}
        self._quotes: dict[tuple[str, str], VenueQuote] = {}
        self._scheduled: dict[int, _ScheduledEvent] = {}
        self._schedule_sequences: dict[str, set[int]] = {}
        self._completed_schedules: dict[str, list[ScheduledSliceResult]] = {}
        self._schedule_results: dict[str, ScheduleResult] = {}
        self._schedule_reservations: dict[str, str] = {}
        self._schedule_locks: dict[str, asyncio.Lock] = {}
        self._halted_schedules: set[str] = set()
        self._ignored_schedule_sequences: set[int] = set()
        self._order_id_owners: dict[str, str] = {}
        self._exchange_order_id_owners: dict[tuple[str, str], str] = {}
        self._client_order_id_owners: dict[tuple[str, str], str] = {}
        self._route_identity_owners: dict[int, str] = {}

    async def close(self) -> None:
        for adapter in self.adapters.values():
            await adapter.close()

    async def update_quote(self, quote: VenueQuote) -> None:
        self._bind_loop()
        venue = quote.venue.lower()
        canonical_symbol = quote.symbol.upper()
        if venue not in self.adapters:
            raise ValueError(f"no configured adapter for venue {quote.venue}")
        mapping = self._mappings.get((canonical_symbol, venue))
        if mapping is None:
            raise ValueError(
                f"{venue} is not compatible with canonical instrument "
                f"{canonical_symbol}"
            )
        canonical_bid_size = _venue_to_canonical_quantity(
            quote.bid_size,
            quote.bid,
            mapping,
        )
        canonical_ask_size = _venue_to_canonical_quantity(
            quote.ask_size,
            quote.ask,
            mapping,
        )
        native_quote = _native.VenueQuote()
        native_quote.venue = venue
        native_quote.symbol = canonical_symbol
        native_quote.bid = float(quote.bid)
        native_quote.ask = float(quote.ask)
        native_quote.bid_size = float(canonical_bid_size)
        native_quote.ask_size = float(canonical_ask_size)
        native_quote.fee_bps = float(quote.fee_bps)
        native_quote.latency_us = float(quote.latency_us)
        native_quote.healthy = quote.healthy
        self._ems.update_quote(native_quote)
        self._quotes[(canonical_symbol, venue)] = VenueQuote(
            venue=venue,
            symbol=canonical_symbol,
            bid=quote.bid,
            ask=quote.ask,
            bid_size=canonical_bid_size,
            ask_size=canonical_ask_size,
            fee_bps=quote.fee_bps,
            latency_us=quote.latency_us,
            healthy=quote.healthy,
        )

    async def preview_route(
        self,
        symbol: str,
        side: Side,
        quantity: Decimal,
    ) -> tuple[dict[str, Decimal | str], ...]:
        self._bind_loop()
        self._require_canonical_instrument(symbol)
        routes = self._ems.route(
            symbol.upper(),
            _native_side(side),
            float(quantity),
        )
        return tuple(_route_dict(route) for route in routes)

    async def execute_plan(
        self,
        request: ExecutionPlanRequest,
    ) -> ExecutionPlanResult:
        self._bind_loop()
        for leg in request.legs:
            self._validate_leg_mappings(leg)
        if request.plan_id in self._plans:
            raise ValueError(f"execution plan already exists: {request.plan_id}")
        policy = request.dispatch_policy or self.dispatch_policy
        result = ExecutionPlanResult(
            plan_id=request.plan_id,
            dispatch_policy=policy,
            legs=[
                ExecutionLegResult(index, leg)
                for index, leg in enumerate(request.legs)
            ],
        )
        self._plans[request.plan_id] = result
        lock = asyncio.Lock()
        self._plan_locks[request.plan_id] = lock
        async with lock:
            native_legs = [_native_leg(leg) for leg in request.legs]
            self._ems.create_plan(request.plan_id, native_legs)
            if not self._ems.reserve_plan(request.plan_id, self._risk):
                self._record_state(result, "FAILED_SAFE")
                result.outcome = ExecutionOutcome.FAILED
                result.risk_released = True
                return result
            self._record_state(result, "RISK_RESERVED")
            try:
                routes = self._ems.route_plan(request.plan_id)
                self._record_state(result, "LEGS_DISPATCHED")
                await self._prepare_orders(result, routes)
            except (RuntimeError, ValueError, ArithmeticError) as error:
                result.recovery_actions.append(
                    f"preflight_failed:{type(error).__name__}"
                )
                await self._recover_without_exposure(result)
                return result

            self._ems.transition_plan(
                request.plan_id,
                _native.PlanState.EXECUTING,
            )
            self._record_state(result, "EXECUTING")
            await self._dispatch(result)
            await self._evaluate_after_dispatch(result)
            return result

    async def reconcile_plan(self, plan_id: str) -> ExecutionPlanResult:
        self._bind_loop()
        async with self._plan_locks[plan_id]:
            return await self._reconcile_plan_locked(self._plans[plan_id])

    async def _reconcile_plan_locked(
        self,
        result: ExecutionPlanResult,
    ) -> ExecutionPlanResult:
        plan_id = result.plan_id
        native_state = self._ems.plan(plan_id).state
        if result.risk_released or native_state in {
            _native.PlanState.CLOSED,
            _native.PlanState.FAILED_SAFE,
        }:
            return result

        if native_state == _native.PlanState.RECOVERY_REQUIRED:
            self._ems.recover_plan(plan_id, _native.PlanState.RECONCILING)
            self._record_state(result, "RECONCILING")
        for routed in result.orders:
            if self._order_confirmed_terminal(routed):
                continue
            adapter = self.adapters[routed.venue]
            previous_status = routed.status
            try:
                exchange_order = await adapter.query_order(
                    routed.order.symbol,
                    exchange_order_id=(
                        routed.exchange_order.exchange_order_id
                        if routed.exchange_order is not None
                        else None
                    ),
                    client_order_id=(
                        None
                        if routed.exchange_order is not None
                        else adapter.client_order_id(routed.order)
                    ),
                )
            except (
                ExchangeAdapterError,
                BinanceGatewayError,
                asyncio.TimeoutError,
                OSError,
            ) as error:
                if previous_status not in {
                    RouteOrderStatus.FILLED,
                    RouteOrderStatus.CANCELED,
                    RouteOrderStatus.REJECTED,
                    RouteOrderStatus.FAILED,
                }:
                    routed.status = RouteOrderStatus.AMBIGUOUS
                routed.reconciled = False
                routed.error = f"reconcile:{type(error).__name__}: {error}"
                routed.reconciliation_error = (
                    f"{type(error).__name__}: {error}"
                )
            else:
                observed_status = self._apply_exchange_order(
                    routed,
                    exchange_order,
                )
                self._mark_terminal_observation_conflict(
                    routed,
                    previous_status,
                    observed_status,
                )

        has_exposure = any(
            order.filled_quantity > 0 for order in result.orders
        )
        recovering = self._ems.plan(plan_id).state in {
            _native.PlanState.RECOVERY_REQUIRED,
            _native.PlanState.RECONCILING,
        }
        if (has_exposure or recovering) and any(
            self._order_needs_cancellation(order) for order in result.orders
        ):
            await self._cancel_remaining(result)

        unresolved = any(
            not self._order_confirmed_terminal(order)
            for order in result.orders
        )
        if unresolved:
            await self._mark_reconciliation_required(result)
        elif all(
            order.status is RouteOrderStatus.FILLED for order in result.orders
        ):
            if self._plan_has_canonical_fill_mismatch(result):
                await self._mark_hedge_and_unwind(result)
            else:
                await self._close_filled(result)
        elif any(order.filled_quantity > 0 for order in result.orders):
            await self._mark_hedge_and_unwind(result)
        elif all(
            order.status
            in {
                RouteOrderStatus.CANCELED,
                RouteOrderStatus.REJECTED,
                RouteOrderStatus.FAILED,
            }
            for order in result.orders
        ):
            await self._close_canceled(result)
        else:
            result.outcome = (
                ExecutionOutcome.RECONCILIATION_REQUIRED
                if self._ems.plan(plan_id).state
                == _native.PlanState.RECONCILING
                else ExecutionOutcome.OPEN
            )
        return result

    async def complete_plan_recovery(
        self,
        plan_id: str,
        *,
        recovery_orders: Sequence[PlanRecoveryOrderEvidence],
    ) -> ExecutionPlanResult:
        self._bind_loop()
        async with self._plan_locks[plan_id]:
            result = self._plans[plan_id]
            if result.risk_released:
                return result
            await self._reconcile_plan_for_recovery(result)
            self._require_plan_routes_terminal(result)
            self._require_plan_recovery_state(result)
            await self._record_plan_recovery_orders(result, recovery_orders)
            if result.residual_canonical_exposure != 0:
                raise RuntimeError(
                    "plan recovery cannot complete with nonzero residual "
                    "canonical exposure"
                )
            self._complete_native_plan_recovery(result)
            result.recovery_completed = True
            result.recovery_actions.append("recovery_completed")
            self._record_state(result, "RECOVERED")
            self._close_recovered_plan(result)
            result.outcome = ExecutionOutcome.RECOVERED
            return result

    async def resolve_plan_recovery(
        self,
        plan_id: str,
        *,
        reason: str,
        external_evidence: str,
    ) -> ExecutionPlanResult:
        self._bind_loop()
        if not reason.strip() or not external_evidence.strip():
            raise ValueError(
                "operator resolution requires a reason and external evidence"
            )
        async with self._plan_locks[plan_id]:
            result = self._plans[plan_id]
            if result.risk_released:
                return result
            await self._reconcile_plan_for_recovery(result)
            self._require_plan_routes_terminal(result)
            self._require_plan_recovery_state(result)
            self._complete_native_plan_recovery(result)
            result.operator_resolved = True
            result.operator_resolution_reason = reason
            result.operator_resolution_evidence = external_evidence
            result.recovery_actions.append(f"operator_resolved:{reason}")
            self._record_state(result, "OPERATOR_RESOLVED")
            self._close_recovered_plan(result)
            result.outcome = ExecutionOutcome.OPERATOR_RESOLVED
            return result

    async def schedule_twap(
        self,
        schedule_id: str,
        leg: ExecutionLeg,
        *,
        start_ns: int,
        end_ns: int,
        slice_count: int,
    ) -> ScheduledExecution:
        self._bind_loop()
        self._validate_leg_mappings(leg)
        native_slices = _native.Scheduler.twap(
            start_ns,
            end_ns,
            float(leg.quantity),
            slice_count,
        )
        return await self._prepare_schedule(schedule_id, leg, native_slices)

    async def schedule_vwap(
        self,
        schedule_id: str,
        leg: ExecutionLeg,
        *,
        start_ns: int,
        end_ns: int,
        volume_weights: Sequence[Decimal],
    ) -> ScheduledExecution:
        self._bind_loop()
        self._validate_leg_mappings(leg)
        native_slices = _native.Scheduler.vwap(
            start_ns,
            end_ns,
            float(leg.quantity),
            [float(weight) for weight in volume_weights],
        )
        return await self._prepare_schedule(schedule_id, leg, native_slices)

    async def run_due(self, timestamp_ns: int) -> tuple[ScheduledSliceResult, ...]:
        self._bind_loop()
        results: list[ScheduledSliceResult] = []
        for event in self._ems.loop().drain_until(timestamp_ns):
            if event.sequence in self._ignored_schedule_sequences:
                self._ignored_schedule_sequences.discard(event.sequence)
                self._scheduled.pop(event.sequence, None)
                continue
            scheduled = self._scheduled.get(event.sequence)
            if scheduled is None:
                raise RuntimeError(f"unknown native scheduled event {event.sequence}")
            async with self._schedule_locks[scheduled.schedule_id]:
                scheduled = self._scheduled.pop(event.sequence, None)
                if scheduled is None:
                    raise RuntimeError(
                        f"unknown native scheduled event {event.sequence}"
                    )
                if event.sequence in self._ignored_schedule_sequences:
                    self._ignored_schedule_sequences.discard(event.sequence)
                    continue
                self._schedule_sequences[scheduled.schedule_id].discard(
                    event.sequence
                )
                result = await self._execute_scheduled_slice(scheduled)
                self._completed_schedules[scheduled.schedule_id].append(result)
                self._schedule_results[
                    scheduled.schedule_id
                ].queued_slice_count = len(
                    self._schedule_sequences[scheduled.schedule_id]
                )
                await self._recover_schedule_after_slice(
                    scheduled.schedule_id,
                    result,
                )
                self._refresh_schedule_state(scheduled.schedule_id)
                results.append(result)
        return tuple(results)

    async def run_schedule(
        self,
        schedule_id: str,
        *,
        clock_ns: Callable[[], int] = time.time_ns,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> tuple[ScheduledSliceResult, ...]:
        self._bind_loop()
        lock = self._schedule_locks[schedule_id]
        while True:
            async with lock:
                sequences = self._schedule_sequences.get(schedule_id, set())
                if not sequences:
                    break
                timestamps = [
                    item.timestamp_ns
                    for sequence, item in self._scheduled.items()
                    if sequence in sequences
                ]
                if not timestamps:
                    raise RuntimeError(
                        "native schedule has queued sequences without events"
                    )
                next_timestamp = min(timestamps)
            delay = max(0, next_timestamp - clock_ns()) / 1_000_000_000
            if delay:
                await sleep(delay)
            due = await self.run_due(max(next_timestamp, clock_ns()))
            if not due:
                async with lock:
                    if self._schedule_sequences.get(schedule_id):
                        raise RuntimeError(
                            "native schedule made no forward progress"
                        )
        return tuple(self._completed_schedules.get(schedule_id, ()))

    def scheduled_results(
        self,
        schedule_id: str,
    ) -> tuple[ScheduledSliceResult, ...]:
        return tuple(self._completed_schedules.get(schedule_id, ()))

    def schedule_result(self, schedule_id: str) -> ScheduleResult:
        return self._schedule_results[schedule_id]

    async def cancel_schedule(self, schedule_id: str) -> ScheduleResult:
        return await self.reconcile_schedule(
            schedule_id,
            cancel_open_orders=True,
        )

    async def reconcile_schedule(
        self,
        schedule_id: str,
        *,
        cancel_open_orders: bool = False,
    ) -> ScheduleResult:
        self._bind_loop()
        async with self._schedule_locks[schedule_id]:
            return await self._reconcile_schedule_locked(
                schedule_id,
                cancel_open_orders=cancel_open_orders,
            )

    async def _reconcile_schedule_locked(
        self,
        schedule_id: str,
        *,
        cancel_open_orders: bool,
    ) -> ScheduleResult:
        result = self._schedule_results[schedule_id]
        if result.risk_released:
            return result
        if cancel_open_orders:
            result.cancel_requested = True
            self._halted_schedules.add(schedule_id)
            self._cancel_queued_schedule_slices(schedule_id)
            if result.state is not ScheduleState.UNWIND_REQUIRED:
                self._set_schedule_state(
                    result,
                    ScheduleState.CANCEL_REQUESTED,
                )
        if result.state not in {
            ScheduleState.FILLED,
            ScheduleState.CANCELED,
            ScheduleState.FAILED,
            ScheduleState.UNWIND_REQUIRED,
            ScheduleState.RECOVERED,
            ScheduleState.OPERATOR_RESOLVED,
        }:
            self._set_schedule_state(result, ScheduleState.RECONCILING)

        for routed in result.routes:
            if not self._schedule_order_reached_exchange(routed):
                continue
            await self._query_schedule_order(routed)

        terminal_partial_exposure = (
            self._schedule_has_terminal_partial_exposure(result)
        )
        if terminal_partial_exposure:
            result.cancel_requested = True
            self._halted_schedules.add(schedule_id)
            self._cancel_queued_schedule_slices(schedule_id)

        if cancel_open_orders or terminal_partial_exposure:
            for routed in result.routes:
                if not self._order_needs_cancellation(routed):
                    continue
                await self._cancel_schedule_order(routed)

        self._refresh_schedule_slice_outcomes(schedule_id)
        self._refresh_schedule_state(schedule_id)
        return result

    async def complete_schedule_recovery(
        self,
        schedule_id: str,
        *,
        cancel_open_orders: bool = True,
        unwind_completed: bool = False,
        recovery_orders: Sequence[PlanRecoveryOrderEvidence] = (),
        operator_resolved: bool = False,
    ) -> ScheduleResult:
        self._bind_loop()
        if operator_resolved:
            raise ValueError(
                "bare operator resolution is not allowed; use "
                "resolve_schedule_recovery with external evidence"
            )
        async with self._schedule_locks[schedule_id]:
            result = await self._reconcile_schedule_locked(
                schedule_id,
                cancel_open_orders=cancel_open_orders,
            )
            if result.risk_released:
                return result
            if unwind_completed:
                if result.state is not ScheduleState.UNWIND_REQUIRED:
                    raise RuntimeError(
                        "schedule unwind cannot complete before terminal "
                        "exposure reconciliation"
                    )
                await self._record_schedule_recovery_orders(
                    result,
                    recovery_orders,
                )
                if result.residual_canonical_exposure != 0:
                    raise RuntimeError(
                        "schedule recovery cannot complete with nonzero "
                        "residual canonical exposure"
                    )
                result.recovery_completed = True
                self._set_schedule_state(result, ScheduleState.RECOVERED)
                self._release_schedule_risk(schedule_id)
            return result

    async def resolve_schedule_recovery(
        self,
        schedule_id: str,
        *,
        reason: str,
        external_evidence: str,
        order_evidence: Sequence[ScheduleRecoveryOrderEvidence] = (),
        recovery_orders: Sequence[PlanRecoveryOrderEvidence] = (),
        cancel_open_orders: bool = True,
    ) -> ScheduleResult:
        self._bind_loop()
        if not reason.strip() or not external_evidence.strip():
            raise ValueError(
                "operator resolution requires a reason and external evidence"
            )
        async with self._schedule_locks[schedule_id]:
            result = self._schedule_results[schedule_id]
            if result.risk_released:
                return result
            result = await self._reconcile_schedule_locked(
                schedule_id,
                cancel_open_orders=cancel_open_orders,
            )
            if result.risk_released:
                return result
            self._record_schedule_order_evidence(result, order_evidence)
            self._require_schedule_routes_terminal(result)
            if result.residual_canonical_exposure != 0:
                await self._record_schedule_recovery_orders(
                    result,
                    recovery_orders,
                )
                if result.residual_canonical_exposure != 0:
                    raise RuntimeError(
                        "schedule recovery cannot release risk with nonzero "
                        "residual canonical exposure"
                    )
            self._halted_schedules.add(schedule_id)
            self._cancel_queued_schedule_slices(schedule_id)
            result.operator_resolved = True
            result.operator_resolution_reason = reason
            result.operator_resolution_evidence = external_evidence
            self._set_schedule_state(
                result,
                ScheduleState.OPERATOR_RESOLVED,
            )
            self._release_schedule_risk(schedule_id)
            return result

    def schedule_is_reserved(self, schedule_id: str) -> bool:
        return schedule_id in self._schedule_reservations

    def plan(self, plan_id: str) -> ExecutionPlanResult:
        return self._plans[plan_id]

    def plan_is_reserved(self, plan_id: str) -> bool:
        return not self._plans[plan_id].risk_released

    @property
    def reserved_notional(self) -> Decimal:
        return Decimal(str(self._risk.reserved_notional))

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        thread_id = threading.get_ident()
        if self._loop is None:
            self._loop = loop
            self._thread_id = thread_id
        elif self._loop is not loop or self._thread_id != thread_id:
            raise RuntimeError(
                "ExecutionCoordinator must remain on its owning asyncio loop"
            )

    def _require_canonical_instrument(self, symbol: str) -> None:
        if symbol.upper() not in self._product_groups:
            raise ValueError(f"unknown canonical instrument: {symbol}")

    def _validate_leg_mappings(self, leg: ExecutionLeg) -> None:
        canonical_symbol = leg.symbol.upper()
        self._require_canonical_instrument(canonical_symbol)
        compatible = {
            venue
            for symbol, venue in self._mappings
            if symbol == canonical_symbol and venue in self.adapters
        }
        if not compatible:
            raise ValueError(
                f"no configured compatible venue for {canonical_symbol}"
            )
        incompatible_preferences = set(leg.venue_preferences) - compatible
        if incompatible_preferences:
            raise ValueError(
                f"incompatible venue preferences for {canonical_symbol}: "
                f"{', '.join(sorted(incompatible_preferences))}"
            )

    async def _prepare_schedule(
        self,
        schedule_id: str,
        leg: ExecutionLeg,
        native_slices: Sequence[Any],
    ) -> ScheduledExecution:
        if not schedule_id:
            raise ValueError("schedule_id must not be empty")
        if schedule_id in self._schedule_locks:
            raise ValueError(f"schedule already exists: {schedule_id}")
        risk_key = f"schedule:{schedule_id}"
        if not self._risk.reserve(risk_key, [_native_leg(leg)]):
            raise ScheduleRiskLimitError(
                f"schedule {schedule_id} exceeds max_reserved_notional"
            )
        lock = asyncio.Lock()
        schedule_result = ScheduleResult(
            schedule_id=schedule_id,
            requested_quantity=leg.quantity,
        )
        self._schedule_locks[schedule_id] = lock
        self._schedule_sequences[schedule_id] = set()
        self._schedule_results[schedule_id] = schedule_result
        self._completed_schedules[schedule_id] = schedule_result.slices
        self._schedule_reservations[schedule_id] = risk_key
        prepared = False
        async with lock:
            try:
                aggregate_routes = self._ems.route(
                    leg.symbol.upper(),
                    _native_side(leg.side),
                    float(leg.quantity),
                )
                planned_routes, route_rules = (
                    await self._normalize_route_allocations(
                        leg,
                        aggregate_routes,
                        source_id=risk_key,
                        scheduled=True,
                    )
                )
                raw_quantities = tuple(
                    Decimal(str(native_slice.quantity))
                    for native_slice in native_slices
                )
                route_slices: list[tuple[Decimal, ...]] = []
                for route_index, route in enumerate(planned_routes):
                    rules = route_rules[route.venue]
                    allocated = _allocate_schedule_quantity(
                        route.quantity,
                        rules.step,
                        rules.minimum,
                        rules.maximum_per_order,
                        raw_quantities,
                    )
                    validated, order_price = (
                        await self._validate_schedule_allocations(
                            risk_key,
                            leg,
                            route,
                            rules,
                            allocated,
                        )
                    )
                    route_slices.append(validated)
                    if order_price is not None:
                        planned_routes[route_index] = _PlannedRoute(
                            venue=route.venue,
                            quantity=route.quantity,
                            venue_quantity=route.venue_quantity,
                            canonical_symbol=route.canonical_symbol,
                            venue_symbol=route.venue_symbol,
                            reference_price=route.reference_price,
                            expected_fee=route.expected_fee,
                            score=route.score,
                            order_price=order_price,
                        )

                normalized_quantity = sum(
                    (
                        sum(
                            (
                                route_slices[route_index][slice_index]
                                for route_index in range(len(planned_routes))
                            ),
                            start=Decimal(0),
                        )
                        for slice_index in range(len(native_slices))
                    ),
                    start=Decimal(0),
                )
                if normalized_quantity != leg.quantity:
                    raise UnexecutableResidualError(
                        "normalized schedule slices do not conserve aggregate quantity"
                    )
                slices: list[tuple[int, Decimal]] = []
                sequences: set[int] = set()
                self._schedule_sequences[schedule_id] = sequences
                scheduled_orders: list[RouteOrderResult] = []
                for index, native_slice in enumerate(native_slices):
                    planned_slice_routes = tuple(
                        _PlannedRoute(
                            venue=route.venue,
                            quantity=route_slices[route_index][index],
                            venue_quantity=_canonical_to_venue_quantity(
                                route_slices[route_index][index],
                                route.reference_price,
                                route_rules[route.venue].mapping,
                            ),
                            canonical_symbol=route.canonical_symbol,
                            venue_symbol=route.venue_symbol,
                            reference_price=route.reference_price,
                            expected_fee=(
                                Decimal(0)
                                if route.quantity == 0
                                else route.expected_fee
                                * route_slices[route_index][index]
                                / route.quantity
                            ),
                            score=route.score,
                            order_price=route.order_price,
                        )
                        for route_index, route in enumerate(planned_routes)
                        if route_slices[route_index][index] > 0
                    )
                    quantity = sum(
                        (route.quantity for route in planned_slice_routes),
                        start=Decimal(0),
                    )
                    synthetic_leg = (
                        ExecutionLeg(
                            symbol=leg.symbol,
                            side=leg.side,
                            quantity=quantity,
                            reference_price=leg.reference_price,
                            order_type=leg.order_type,
                            limit_price=leg.limit_price,
                            venue_preferences=leg.venue_preferences,
                            hedge=leg.hedge,
                        )
                        if quantity > 0
                        else None
                    )
                    slice_orders = tuple(
                        RouteOrderResult(
                            leg_index=0,
                            slice_index=route_index,
                            venue=route.venue,
                            order=_route_order(
                                f"schedule:{schedule_id}:{index}",
                                0,
                                route_index,
                                synthetic_leg or leg,
                                route,
                            ),
                            expected_fee=route.expected_fee,
                            score=route.score,
                            canonical_symbol=route.canonical_symbol,
                            canonical_quantity=route.quantity,
                            quantity_mapping=self._mappings[
                                (route.canonical_symbol, route.venue)
                            ],
                            reference_price=route.reference_price,
                        )
                        for route_index, route in enumerate(planned_slice_routes)
                    )
                    payload = json.dumps(
                        {
                            "schedule_id": schedule_id,
                            "slice_index": index,
                            "quantity": str(quantity),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    sequence = self._ems.loop().enqueue(
                        native_slice.timestamp_ns,
                        "scheduled_slice",
                        payload,
                    )
                    self._scheduled[sequence] = _ScheduledEvent(
                        schedule_id=schedule_id,
                        slice_index=index,
                        timestamp_ns=native_slice.timestamp_ns,
                        leg=leg,
                        routes=planned_slice_routes,
                        orders=slice_orders,
                        quantity=quantity,
                    )
                    scheduled_orders.extend(slice_orders)
                    sequences.add(sequence)
                    slices.append((native_slice.timestamp_ns, quantity))
                schedule_result.routes.extend(scheduled_orders)
                self._register_route_orders(
                    (
                        (
                            routed,
                            f"schedule:{schedule_id}:"
                            f"{event.slice_index}:{routed.slice_index}:"
                            f"{routed.venue}",
                        )
                        for event in self._scheduled.values()
                        if event.schedule_id == schedule_id
                        for routed in event.orders
                    )
                )
                schedule_result.queued_slice_count = len(sequences)
                prepared = True
                return ScheduledExecution(
                    schedule_id=schedule_id,
                    leg=leg,
                    slices=tuple(slices),
                    requested_quantity=leg.quantity,
                    normalized_quantity=normalized_quantity,
                    residual_quantity=leg.quantity - normalized_quantity,
                )
            finally:
                if not prepared:
                    sequences = self._schedule_sequences.pop(
                        schedule_id,
                        set(),
                    )
                    self._ignored_schedule_sequences.update(sequences)
                    self._schedule_results.pop(schedule_id, None)
                    self._completed_schedules.pop(schedule_id, None)
                    self._schedule_reservations.pop(schedule_id, None)
                    self._schedule_locks.pop(schedule_id, None)
                    self._risk.release(risk_key)

    async def _prepare_orders(
        self,
        result: ExecutionPlanResult,
        routes: Sequence[Sequence[Any]],
    ) -> None:
        for leg_result, leg_routes in zip(result.legs, routes, strict=True):
            planned_routes, _ = await self._normalize_route_allocations(
                leg_result.leg,
                leg_routes,
                source_id=f"plan:{result.plan_id}:{leg_result.leg_index}",
                scheduled=False,
            )
            for slice_index, route in enumerate(planned_routes):
                venue = route.venue.lower()
                order = _route_order(
                    f"plan:{result.plan_id}",
                    leg_result.leg_index,
                    slice_index,
                    leg_result.leg,
                    route,
                )
                routed = RouteOrderResult(
                    leg_index=leg_result.leg_index,
                    slice_index=slice_index,
                    venue=venue,
                    order=order,
                    expected_fee=route.expected_fee,
                    score=route.score,
                    canonical_symbol=route.canonical_symbol,
                    canonical_quantity=route.quantity,
                    quantity_mapping=self._mappings[
                        (route.canonical_symbol, venue)
                    ],
                    reference_price=route.reference_price,
                )
                leg_result.orders.append(routed)
        self._register_route_orders(
            (
                (
                    routed,
                    f"plan:{result.plan_id}:{routed.leg_index}:"
                    f"{routed.slice_index}:{routed.venue}",
                )
                for routed in result.orders
            )
        )

    def _register_route_orders(
        self,
        registrations: Iterable[tuple[RouteOrderResult, str]],
    ) -> None:
        pending_order_ids: dict[str, str] = {}
        pending_client_ids: dict[tuple[str, str], str] = {}
        pending_routes: list[tuple[RouteOrderResult, str]] = []
        for routed, owner in registrations:
            order_id = str(routed.order.order_id)
            client_order_id = self.adapters[routed.venue].client_order_id(
                routed.order
            )
            client_identity = (routed.venue.lower(), client_order_id)
            for identifier, existing, pending, kind in (
                (
                    order_id,
                    self._order_id_owners,
                    pending_order_ids,
                    "order ID",
                ),
                (
                    client_identity,
                    self._client_order_id_owners,
                    pending_client_ids,
                    "client order ID",
                ),
            ):
                previous_owner = pending.get(identifier) or existing.get(identifier)
                if previous_owner is not None and previous_owner != owner:
                    raise ValueError(
                        f"coordinator-wide {kind} collision between "
                        f"{previous_owner} and {owner}: {identifier}"
                    )
                pending[identifier] = owner
            pending_routes.append((routed, owner))
        self._order_id_owners.update(pending_order_ids)
        self._client_order_id_owners.update(pending_client_ids)
        self._route_identity_owners.update(
            (id(routed), owner) for routed, owner in pending_routes
        )

    async def _normalize_route_allocations(
        self,
        leg: ExecutionLeg,
        routes: Sequence[Any],
        *,
        source_id: str,
        scheduled: bool,
    ) -> tuple[list[_PlannedRoute], dict[str, _RouteRules]]:
        planned: list[_PlannedRoute] = []
        fee_per_unit: list[Decimal] = []
        route_rules: dict[str, _RouteRules] = {}
        for route in routes:
            venue = route.venue.lower()
            adapter = self.adapters.get(venue)
            if adapter is None:
                raise ValueError(f"native route selected unconfigured {venue}")
            mapping = self._mappings.get((leg.symbol.upper(), venue))
            if mapping is None:
                raise ValueError(
                    f"{venue} route is incompatible with {leg.symbol}"
                )
            reference_price = Decimal(str(route.reference_price))
            rules = await adapter.instrument_rules(mapping.venue_symbol)
            if rules.quantity_unit is not mapping.venue_quantity_unit:
                raise ExchangeAdapterError(
                    f"{venue} instrument rules use {rules.quantity_unit.value} "
                    f"amounts but mapping declares "
                    f"{mapping.venue_quantity_unit.value}"
                )
            quote = self._quotes.get((leg.symbol.upper(), venue))
            if quote is None:
                raise RuntimeError(f"venue quote disappeared before routing: {venue}")
            capacity = quote.ask_size if leg.side is Side.BUY else quote.bid_size
            venue_maximum = (
                rules.maximum_market_quantity
                if leg.order_type is OrderType.MARKET
                else rules.maximum_limit_quantity
            )
            step = _venue_to_canonical_quantity(
                rules.quantity_step,
                reference_price,
                mapping,
            )
            minimum = _venue_to_canonical_quantity(
                rules.minimum_quantity,
                reference_price,
                mapping,
            )
            maximum_per_order = (
                _venue_to_canonical_quantity(
                    venue_maximum,
                    reference_price,
                    mapping,
                )
                if venue_maximum is not None
                else None
            )
            aggregate_capacity = capacity
            if not scheduled and maximum_per_order is not None:
                aggregate_capacity = min(aggregate_capacity, maximum_per_order)
            route_rules[venue] = _RouteRules(
                venue=venue,
                step=step,
                minimum=minimum,
                capacity=aggregate_capacity,
                maximum_per_order=maximum_per_order,
                mapping=mapping,
            )
            raw_quantity = min(
                Decimal(str(route.quantity)),
                aggregate_capacity,
            )
            quantity = _floor_to_step(raw_quantity, step)
            if quantity < minimum:
                quantity = Decimal(0)
            unit_fee = (
                Decimal(0)
                if Decimal(str(route.quantity)) == 0
                else Decimal(str(route.expected_fee))
                / Decimal(str(route.quantity))
            )
            fee_per_unit.append(unit_fee)
            planned.append(
                _PlannedRoute(
                    venue=venue,
                    quantity=quantity,
                    venue_quantity=_canonical_to_venue_quantity(
                        quantity,
                        reference_price,
                        mapping,
                    ),
                    canonical_symbol=leg.symbol.upper(),
                    venue_symbol=mapping.venue_symbol,
                    reference_price=reference_price,
                    expected_fee=unit_fee * quantity,
                    score=Decimal(str(route.score)),
                )
            )

        allocated = sum(
            (route.quantity for route in planned),
            start=Decimal(0),
        )
        residual = leg.quantity - allocated
        for index, route in enumerate(planned):
            if residual == 0:
                break
            rules = route_rules[route.venue]
            maximum = _floor_to_step(rules.capacity, rules.step)
            room = maximum - route.quantity
            addition = min(_floor_to_step(residual, rules.step), room)
            if addition <= 0:
                continue
            candidate = route.quantity + addition
            if candidate < rules.minimum:
                continue
            planned[index] = _PlannedRoute(
                venue=route.venue,
                quantity=candidate,
                venue_quantity=_canonical_to_venue_quantity(
                    candidate,
                    route.reference_price,
                    rules.mapping,
                ),
                canonical_symbol=route.canonical_symbol,
                venue_symbol=route.venue_symbol,
                reference_price=route.reference_price,
                expected_fee=fee_per_unit[index] * candidate,
                score=route.score,
            )
            residual -= addition

        if residual != 0:
            raise UnexecutableResidualError(
                f"unexecutable residual {residual} for {leg.symbol}; "
                "below available venue lot/minimum constraints"
            )

        executable = [route for route in planned if route.quantity > 0]
        normalized_routes: list[_PlannedRoute] = []
        for index, route in enumerate(executable):
            rules = route_rules[route.venue]
            if (
                scheduled
                and rules.maximum_per_order is not None
                and route.quantity > rules.maximum_per_order
            ):
                normalized_routes.append(route)
                continue
            order = _route_order(
                source_id,
                0,
                index,
                leg,
                route,
            )
            adapter = self.adapters[route.venue]
            try:
                normalized = await adapter.normalize_order(
                    order,
                    reference_price=route.reference_price,
                )
            except (
                ExchangeAdapterError,
                BinanceGatewayError,
                ValueError,
            ) as error:
                raise UnexecutableResidualError(
                    f"{route.venue} cannot execute normalized aggregate "
                    f"{route.quantity} for {leg.symbol}"
                ) from error
            normalized_canonical = _venue_to_canonical_quantity(
                normalized.quantity,
                route.reference_price,
                rules.mapping,
            )
            if normalized_canonical != route.quantity:
                raise UnexecutableResidualError(
                    f"{route.venue} normalization left unexecutable residual "
                    f"{route.quantity - normalized_canonical}"
                )
            normalized_routes.append(
                _PlannedRoute(
                    venue=route.venue,
                    quantity=route.quantity,
                    venue_quantity=normalized.quantity,
                    canonical_symbol=route.canonical_symbol,
                    venue_symbol=route.venue_symbol,
                    reference_price=route.reference_price,
                    expected_fee=route.expected_fee,
                    score=route.score,
                    order_price=normalized.price,
                )
            )

        normalized_total = sum(
            (route.quantity for route in normalized_routes),
            start=Decimal(0),
        )
        if normalized_total != leg.quantity:
            raise UnexecutableResidualError(
                "normalized venue routes do not conserve requested quantity"
            )
        return normalized_routes, route_rules

    async def _validate_schedule_allocations(
        self,
        source_id: str,
        leg: ExecutionLeg,
        route: _PlannedRoute,
        rules: _RouteRules,
        allocations: tuple[Decimal, ...],
    ) -> tuple[tuple[Decimal, ...], Decimal | None]:
        quantities = list(allocations)
        normalized_price = route.order_price
        index = 0
        adjustments = 0
        while index < len(quantities):
            quantity = quantities[index]
            if quantity == 0:
                index += 1
                continue
            slice_route = _PlannedRoute(
                venue=route.venue,
                quantity=quantity,
                venue_quantity=_canonical_to_venue_quantity(
                    quantity,
                    route.reference_price,
                    rules.mapping,
                ),
                canonical_symbol=route.canonical_symbol,
                venue_symbol=route.venue_symbol,
                reference_price=route.reference_price,
                expected_fee=Decimal(0),
                score=route.score,
                order_price=route.order_price,
            )
            order = _route_order(source_id, 0, index, leg, slice_route)
            try:
                normalized = await self.adapters[route.venue].normalize_order(
                    order,
                    reference_price=route.reference_price,
                )
            except (
                ExchangeAdapterError,
                BinanceGatewayError,
                ValueError,
            ) as error:
                target = _coalesce_target(quantities, index, rules)
                if target is None:
                    raise UnexecutableResidualError(
                        f"{route.venue} slice quantity {quantity} is below "
                        "lot/min-notional and cannot be coalesced"
                    ) from error
                quantities[target] += quantity
                quantities[index] = Decimal(0)
                adjustments += 1
                if adjustments > len(quantities) * 2:
                    raise UnexecutableResidualError(
                        "schedule normalization could not converge"
                    ) from error
                index = min(index, target)
                continue
            normalized_canonical = _venue_to_canonical_quantity(
                normalized.quantity,
                route.reference_price,
                rules.mapping,
            )
            if normalized_canonical != quantity:
                raise UnexecutableResidualError(
                    f"{route.venue} schedule normalization left residual "
                    f"{quantity - normalized_canonical}"
                )
            normalized_price = normalized.price
            index += 1
        if sum(quantities, start=Decimal(0)) != route.quantity:
            raise UnexecutableResidualError(
                "schedule route allocations do not conserve normalized quantity"
            )
        return tuple(quantities), normalized_price

    async def _dispatch(self, result: ExecutionPlanResult) -> None:
        if result.dispatch_policy is DispatchPolicy.SEQUENTIAL:
            for index, routed in enumerate(result.orders):
                failed = await self._submit_one(result, routed)
                if failed:
                    for pending in result.orders[index + 1 :]:
                        pending.status = RouteOrderStatus.SKIPPED
                        pending.reconciled = True
                    break
            return
        outcomes = await asyncio.gather(
            *(self._submit_one(result, routed) for routed in result.orders)
        )
        if any(outcomes):
            result.recovery_actions.append("simultaneous_dispatch_failure")

    async def _submit_one(
        self,
        result: ExecutionPlanResult,
        routed: RouteOrderResult,
    ) -> bool:
        routed.status = RouteOrderStatus.SUBMITTING
        adapter = self.adapters[routed.venue]
        try:
            exchange_order = await adapter.submit_order(routed.order)
        except (ExchangeAPIError, BinanceAPIError) as error:
            if error.status < 500:
                routed.status = RouteOrderStatus.FAILED
                routed.error = f"{type(error).__name__}: {error}"
                routed.reconciled = True
                return True
            return await self._reconcile_ambiguous_submission(
                result,
                routed,
                adapter,
                error,
            )
        except (
            AmbiguousOrderError,
            ExchangeAdapterError,
            BinanceGatewayError,
            asyncio.TimeoutError,
            OSError,
        ) as error:
            return await self._reconcile_ambiguous_submission(
                result,
                routed,
                adapter,
                error,
            )
        self._apply_exchange_order(routed, exchange_order, reconciled=True)
        return routed.status in {
            RouteOrderStatus.AMBIGUOUS,
            RouteOrderStatus.CANCELED,
            RouteOrderStatus.REJECTED,
        }

    async def _reconcile_ambiguous_submission(
        self,
        result: ExecutionPlanResult,
        routed: RouteOrderResult,
        adapter: ExchangeAdapter,
        error: BaseException,
    ) -> bool:
        client_order_id = adapter.client_order_id(routed.order)
        result.recovery_actions.append(
            f"ambiguous_submission:{routed.venue}:{client_order_id}"
        )
        routed.status = RouteOrderStatus.AMBIGUOUS
        routed.reconciled = False
        routed.error = f"{type(error).__name__}: {error}"
        try:
            exchange_order = await adapter.query_order(
                routed.order.symbol,
                client_order_id=client_order_id,
            )
        except (
            ExchangeAdapterError,
            BinanceGatewayError,
            asyncio.TimeoutError,
            OSError,
        ) as query_error:
            routed.error += (
                f"; reconcile:{type(query_error).__name__}: {query_error}"
            )
            routed.reconciliation_error = (
                f"{type(query_error).__name__}: {query_error}"
            )
            return True
        self._apply_exchange_order(routed, exchange_order, reconciled=True)
        return routed.status in {
            RouteOrderStatus.AMBIGUOUS,
            RouteOrderStatus.CANCELED,
            RouteOrderStatus.REJECTED,
        }

    async def _evaluate_after_dispatch(self, result: ExecutionPlanResult) -> None:
        orders = result.orders
        failure = any(
            order.status
            in {
                RouteOrderStatus.FAILED,
                RouteOrderStatus.REJECTED,
                RouteOrderStatus.CANCELED,
                RouteOrderStatus.AMBIGUOUS,
            }
            for order in orders
        ) or any(order.status is RouteOrderStatus.PENDING for order in orders)
        if failure:
            hard_failure = any(
                order.status
                in {
                    RouteOrderStatus.FAILED,
                    RouteOrderStatus.REJECTED,
                }
                for order in orders
            )
            await self._cancel_remaining(result)
            unresolved = any(
                not self._order_confirmed_terminal(order)
                for order in orders
            )
            if unresolved:
                await self._mark_reconciliation_required(result)
            elif any(order.filled_quantity > 0 for order in orders):
                await self._mark_hedge_and_unwind(result)
            else:
                await self._close_canceled(result)
                if hard_failure:
                    result.outcome = ExecutionOutcome.FAILED
            return
        if all(order.status is RouteOrderStatus.FILLED for order in orders):
            if self._plan_has_canonical_fill_mismatch(result):
                await self._mark_hedge_and_unwind(result)
            else:
                await self._close_filled(result)
            return
        if any(order.filled_quantity > 0 for order in orders):
            await self._cancel_remaining(result)
            if any(
                not self._order_confirmed_terminal(order)
                for order in orders
            ):
                await self._mark_reconciliation_required(result)
            else:
                await self._mark_hedge_and_unwind(result)
            return
        result.outcome = ExecutionOutcome.OPEN

    def _plan_has_canonical_fill_mismatch(
        self,
        result: ExecutionPlanResult,
    ) -> bool:
        return any(
            leg_result.filled_quantity != leg_result.leg.quantity
            for leg_result in result.legs
        )

    async def _cancel_remaining(self, result: ExecutionPlanResult) -> None:
        result.recovery_actions.append("cancel_remaining")
        for routed in result.orders:
            if not self._order_needs_cancellation(routed):
                continue
            adapter = self.adapters[routed.venue]
            if routed.status is RouteOrderStatus.SUBMITTING:
                if not await self._query_cancellation_outcome(adapter, routed):
                    continue
                if routed.status in {
                    RouteOrderStatus.FILLED,
                    RouteOrderStatus.CANCELED,
                    RouteOrderStatus.REJECTED,
                }:
                    continue
            try:
                canceled = await adapter.cancel_order(
                    routed.order.symbol,
                    exchange_order_id=(
                        routed.exchange_order.exchange_order_id
                        if routed.exchange_order is not None
                        else None
                    ),
                    client_order_id=(
                        None
                        if routed.exchange_order is not None
                        else adapter.client_order_id(routed.order)
                    ),
                )
            except (
                AmbiguousOrderError,
                ExchangeAdapterError,
                BinanceGatewayError,
                asyncio.TimeoutError,
                OSError,
            ) as error:
                routed.cancel_error = f"{type(error).__name__}: {error}"
                await self._query_cancellation_outcome(adapter, routed)
            else:
                self._apply_exchange_order(routed, canceled, reconciled=False)
                await self._query_cancellation_outcome(adapter, routed)

    async def _query_cancellation_outcome(
        self,
        adapter: ExchangeAdapter,
        routed: RouteOrderResult,
    ) -> bool:
        previous_status = routed.status
        try:
            queried = await adapter.query_order(
                routed.order.symbol,
                exchange_order_id=(
                    routed.exchange_order.exchange_order_id
                    if routed.exchange_order is not None
                    else None
                ),
                client_order_id=(
                    None
                    if routed.exchange_order is not None
                    else adapter.client_order_id(routed.order)
                ),
            )
        except (
            ExchangeAdapterError,
            BinanceGatewayError,
            asyncio.TimeoutError,
            OSError,
        ) as error:
            if previous_status not in {
                RouteOrderStatus.FILLED,
                RouteOrderStatus.CANCELED,
                RouteOrderStatus.REJECTED,
                RouteOrderStatus.FAILED,
            }:
                routed.status = RouteOrderStatus.AMBIGUOUS
            routed.reconciled = False
            routed.reconciliation_error = (
                f"{type(error).__name__}: {error}"
            )
            routed.cancel_error = (
                f"{routed.cancel_error or ''}; "
                f"reconcile:{type(error).__name__}: {error}"
            ).lstrip("; ")
            return False
        observed_status = self._apply_exchange_order(
            routed,
            queried,
            reconciled=True,
        )
        if observed_status in {
            RouteOrderStatus.ACCEPTED,
            RouteOrderStatus.PARTIALLY_FILLED,
            RouteOrderStatus.SUBMITTING,
            RouteOrderStatus.AMBIGUOUS,
        }:
            if previous_status not in {
                RouteOrderStatus.FILLED,
                RouteOrderStatus.CANCELED,
                RouteOrderStatus.REJECTED,
                RouteOrderStatus.FAILED,
            }:
                routed.status = RouteOrderStatus.AMBIGUOUS
            routed.reconciled = False
            routed.cancel_error = (
                "cancellation reconciliation did not establish "
                "a terminal outcome"
            )
            return False
        routed.cancel_error = None
        return True

    async def _recover_without_exposure(
        self,
        result: ExecutionPlanResult,
    ) -> None:
        native_state = self._ems.plan(result.plan_id).state
        if native_state == _native.PlanState.LEGS_DISPATCHED:
            self._ems.transition_plan(
                result.plan_id,
                _native.PlanState.RECOVERY_REQUIRED,
            )
        self._record_state(result, "RECOVERY_REQUIRED")
        self._ems.recover_plan(result.plan_id, _native.PlanState.CANCEL_REMAINING)
        self._record_state(result, "CANCEL_REMAINING")
        self._ems.transition_plan(result.plan_id, _native.PlanState.CLOSED)
        self._record_state(result, "CLOSED")
        self._release_risk(result)
        result.outcome = ExecutionOutcome.FAILED

    async def _close_filled(self, result: ExecutionPlanResult) -> None:
        state = self._ems.plan(result.plan_id).state
        if state in {
            _native.PlanState.EXECUTING,
            _native.PlanState.PARTIALLY_FILLED,
        }:
            self._ems.transition_plan(result.plan_id, _native.PlanState.FILLED)
            self._record_state(result, "FILLED")
            state = _native.PlanState.FILLED
        if state == _native.PlanState.FILLED:
            self._ems.transition_plan(result.plan_id, _native.PlanState.RECONCILING)
            self._record_state(result, "RECONCILING")
        self._ems.transition_plan(result.plan_id, _native.PlanState.CLOSED)
        self._record_state(result, "CLOSED")
        self._release_risk(result)
        result.outcome = ExecutionOutcome.FILLED

    async def _close_canceled(self, result: ExecutionPlanResult) -> None:
        state = self._ems.plan(result.plan_id).state
        if state in {
            _native.PlanState.EXECUTING,
            _native.PlanState.LEGS_DISPATCHED,
        }:
            self._ems.transition_plan(
                result.plan_id,
                _native.PlanState.RECOVERY_REQUIRED,
            )
            self._record_state(result, "RECOVERY_REQUIRED")
            state = _native.PlanState.RECOVERY_REQUIRED
        if state == _native.PlanState.RECOVERY_REQUIRED:
            self._ems.recover_plan(
                result.plan_id,
                _native.PlanState.CANCEL_REMAINING,
            )
            self._record_state(result, "CANCEL_REMAINING")
        self._ems.transition_plan(result.plan_id, _native.PlanState.CLOSED)
        self._record_state(result, "CLOSED")
        self._release_risk(result)
        result.outcome = ExecutionOutcome.CANCELED

    async def _mark_reconciliation_required(
        self,
        result: ExecutionPlanResult,
    ) -> None:
        state = self._ems.plan(result.plan_id).state
        if state in {
            _native.PlanState.EXECUTING,
            _native.PlanState.PARTIALLY_FILLED,
            _native.PlanState.RECONCILING,
        }:
            self._ems.transition_plan(
                result.plan_id,
                _native.PlanState.RECOVERY_REQUIRED,
            )
            self._record_state(result, "RECOVERY_REQUIRED")
        result.native_state = "RECOVERY_REQUIRED"
        if result.state_history[-1] != "RECONCILIATION_REQUIRED":
            result.state_history.append("RECONCILIATION_REQUIRED")
        result.outcome = ExecutionOutcome.RECONCILIATION_REQUIRED

    async def _mark_hedge_and_unwind(
        self,
        result: ExecutionPlanResult,
    ) -> None:
        state = self._ems.plan(result.plan_id).state
        if state == _native.PlanState.EXECUTING:
            self._ems.transition_plan(
                result.plan_id,
                _native.PlanState.PARTIALLY_FILLED,
            )
            self._record_state(result, "PARTIALLY_FILLED")
            state = _native.PlanState.PARTIALLY_FILLED
        if state == _native.PlanState.RECONCILING:
            self._ems.transition_plan(
                result.plan_id,
                _native.PlanState.HEDGE_REQUIRED,
            )
            self._record_state(result, "HEDGE_REQUIRED")
            state = _native.PlanState.HEDGE_REQUIRED
        elif state == _native.PlanState.PARTIALLY_FILLED:
            self._ems.transition_plan(
                result.plan_id,
                _native.PlanState.HEDGE_REQUIRED,
            )
            self._record_state(result, "HEDGE_REQUIRED")
            state = _native.PlanState.HEDGE_REQUIRED
        if state == _native.PlanState.HEDGE_REQUIRED:
            self._ems.transition_plan(
                result.plan_id,
                _native.PlanState.UNWIND_REQUIRED,
            )
            self._record_state(result, "UNWIND_REQUIRED")
        result.recovery_actions.extend(["hedge_required", "unwind_required"])
        result.outcome = ExecutionOutcome.UNWIND_REQUIRED

    async def _reconcile_plan_for_recovery(
        self,
        result: ExecutionPlanResult,
    ) -> None:
        if any(
            not self._order_confirmed_terminal(order)
            for order in result.orders
        ):
            await self._reconcile_plan_locked(result)

    def _require_plan_routes_terminal(
        self,
        result: ExecutionPlanResult,
    ) -> None:
        unresolved = [
            order.order.client_order_id
            for order in result.orders
            if not self._order_confirmed_terminal(order)
        ]
        if unresolved:
            raise RuntimeError(
                "plan recovery cannot release risk while a route is live, "
                "ambiguous, or unreconciled: "
                + ", ".join(unresolved)
            )

    def _require_plan_recovery_state(
        self,
        result: ExecutionPlanResult,
    ) -> None:
        state = self._ems.plan(result.plan_id).state
        if state not in {
            _native.PlanState.HEDGE_REQUIRED,
            _native.PlanState.UNWIND_REQUIRED,
            _native.PlanState.RECOVERY_REQUIRED,
            _native.PlanState.RECONCILING,
        }:
            raise RuntimeError(
                "plan recovery can only complete from a recovery-required "
                "state"
            )

    async def _record_plan_recovery_orders(
        self,
        result: ExecutionPlanResult,
        recovery_orders: Sequence[PlanRecoveryOrderEvidence],
    ) -> None:
        known_recovery_ids = {
            order.recovery_id: order for order in result.recovery_orders
        }
        known_order_ids = {
            self._plan_recovery_order_identity(order): order
            for order in result.recovery_orders
        }
        additions: list[PlanRecoveryOrderEvidence] = []
        pending_exchange_ids: dict[tuple[str, str], str] = {}
        pending_client_ids: dict[tuple[str, str], str] = {}
        for recovery in recovery_orders:
            if recovery.status is not RouteOrderStatus.FILLED:
                raise RuntimeError(
                    f"plan recovery order {recovery.recovery_id} is not filled"
                )
            if not recovery.reconciled:
                raise RuntimeError(
                    f"plan recovery order {recovery.recovery_id} is not "
                    "reconciled"
                )
            existing = known_recovery_ids.get(recovery.recovery_id)
            if existing is not None:
                if not self._recovery_evidence_matches(existing, recovery):
                    raise RuntimeError(
                        "plan recovery order id was reused with different "
                        f"evidence: {recovery.recovery_id}"
                    )
                continue
            owner = f"recovery:plan:{result.plan_id}:{recovery.recovery_id}"
            verified = await self._verify_recovery_order(
                recovery,
                owner=owner,
                pending_exchange_ids=pending_exchange_ids,
                pending_client_ids=pending_client_ids,
            )
            identity = self._plan_recovery_order_identity(verified)
            existing = known_order_ids.get(identity)
            if existing is not None:
                same_fill = (
                    existing.canonical_symbol.upper()
                    == verified.canonical_symbol.upper()
                    and existing.side is verified.side
                    and existing.filled_quantity
                    == verified.filled_quantity
                    and existing.status is verified.status
                    and existing.reconciled == verified.reconciled
                )
                conflict = "conflicting" if not same_fill else "duplicate"
                raise RuntimeError(
                    f"{conflict} plan recovery fill evidence for "
                    f"{identity[0]} order {identity[2]}"
                )
            known_recovery_ids[verified.recovery_id] = verified
            known_order_ids[identity] = verified
            additions.append(verified)
        self._exchange_order_id_owners.update(pending_exchange_ids)
        self._client_order_id_owners.update(pending_client_ids)
        result.recovery_orders.extend(additions)
        result.recovery_actions.extend(
            f"recovery_order_confirmed:{order.recovery_id}"
            for order in additions
        )

    async def _record_schedule_recovery_orders(
        self,
        result: ScheduleResult,
        recovery_orders: Sequence[PlanRecoveryOrderEvidence],
    ) -> None:
        known_recovery_ids = {
            order.recovery_id: order for order in result.recovery_orders
        }
        known_order_ids = {
            self._plan_recovery_order_identity(order): order
            for order in result.recovery_orders
        }
        additions: list[PlanRecoveryOrderEvidence] = []
        pending_exchange_ids: dict[tuple[str, str], str] = {}
        pending_client_ids: dict[tuple[str, str], str] = {}
        for recovery in recovery_orders:
            existing = known_recovery_ids.get(recovery.recovery_id)
            if existing is not None:
                if not self._recovery_evidence_matches(existing, recovery):
                    raise RuntimeError(
                        "schedule recovery order id was reused with different "
                        f"evidence: {recovery.recovery_id}"
                    )
                continue
            owner = (
                f"recovery:schedule:{result.schedule_id}:"
                f"{recovery.recovery_id}"
            )
            verified = await self._verify_recovery_order(
                recovery,
                owner=owner,
                pending_exchange_ids=pending_exchange_ids,
                pending_client_ids=pending_client_ids,
            )
            identity = self._plan_recovery_order_identity(verified)
            existing = known_order_ids.get(identity)
            if existing is not None:
                raise RuntimeError(
                    "duplicate schedule recovery fill evidence for "
                    f"{identity[0]} order {identity[2]}"
                )
            known_recovery_ids[verified.recovery_id] = verified
            known_order_ids[identity] = verified
            additions.append(verified)
        self._exchange_order_id_owners.update(pending_exchange_ids)
        self._client_order_id_owners.update(pending_client_ids)
        result.recovery_orders.extend(additions)

    def _recovery_evidence_matches(
        self,
        existing: PlanRecoveryOrderEvidence,
        incoming: PlanRecoveryOrderEvidence,
    ) -> bool:
        if (
            existing.recovery_id != incoming.recovery_id
            or existing.canonical_symbol.upper()
            != incoming.canonical_symbol.upper()
            or existing.side is not incoming.side
            or existing.filled_quantity != incoming.filled_quantity
            or existing.venue.lower() != incoming.venue.lower()
            or existing.status is not incoming.status
            or existing.reconciled != incoming.reconciled
            or existing.reconciliation_evidence
            != incoming.reconciliation_evidence
        ):
            return False
        incoming_exchange_id = (incoming.exchange_order_id or "").strip()
        incoming_client_id = (incoming.client_order_id or "").strip()
        return (
            not incoming_exchange_id
            or incoming_exchange_id
            == (existing.exchange_order_id or "").strip()
        ) and (
            not incoming_client_id
            or incoming_client_id == (existing.client_order_id or "").strip()
        )

    async def _verify_recovery_order(
        self,
        recovery: PlanRecoveryOrderEvidence,
        *,
        owner: str,
        pending_exchange_ids: dict[tuple[str, str], str],
        pending_client_ids: dict[tuple[str, str], str],
    ) -> PlanRecoveryOrderEvidence:
        venue = recovery.venue.strip().lower()
        adapter = self.adapters.get(venue)
        if adapter is None:
            raise RuntimeError(
                f"recovery order venue is not configured for verification: {venue}"
            )
        canonical_symbol = recovery.canonical_symbol.strip().upper()
        mapping = self._mappings.get((canonical_symbol, venue))
        if mapping is None:
            raise RuntimeError(
                "recovery order does not match a configured canonical "
                f"instrument: {canonical_symbol} on {venue}"
            )
        supplied_exchange_id = (recovery.exchange_order_id or "").strip()
        supplied_client_id = (recovery.client_order_id or "").strip()
        self._validate_recovery_identity_claim(
            "exchange",
            venue,
            supplied_exchange_id,
            owner,
            pending_exchange_ids,
        )
        self._validate_recovery_identity_claim(
            "client",
            venue,
            supplied_client_id,
            owner,
            pending_client_ids,
        )
        try:
            observed = await adapter.query_order(
                mapping.venue_symbol,
                exchange_order_id=supplied_exchange_id or None,
                client_order_id=(
                    supplied_client_id if not supplied_exchange_id else None
                ),
            )
        except (
            ExchangeAdapterError,
            BinanceGatewayError,
            asyncio.TimeoutError,
            OSError,
        ) as error:
            raise RuntimeError(
                f"recovery order verification failed for {owner}"
            ) from error
        observed_exchange_id = observed.exchange_order_id.strip()
        observed_client_id = observed.client_order_id.strip()
        if observed.venue.strip().lower() != venue:
            raise RuntimeError("recovery order venue verification mismatch")
        if observed.symbol.strip().upper() != mapping.venue_symbol.upper():
            raise RuntimeError("recovery order symbol verification mismatch")
        if (
            supplied_exchange_id
            and observed_exchange_id != supplied_exchange_id
        ):
            raise RuntimeError(
                "recovery order exchange ID verification mismatch"
            )
        if supplied_client_id and observed_client_id != supplied_client_id:
            raise RuntimeError(
                "recovery order client ID verification mismatch"
            )
        if not observed_exchange_id and not observed_client_id:
            raise RuntimeError(
                "verified recovery order must expose an exchange or client ID"
            )
        try:
            observed_side = Side(observed.side.strip().lower())
        except ValueError as error:
            raise RuntimeError(
                f"recovery order returned unsupported side: {observed.side}"
            ) from error
        if observed_side is not recovery.side:
            raise RuntimeError("recovery order side verification mismatch")
        status = observed.status.strip().lower()
        if (
            status not in _FILLED_STATUSES
            or observed.quantity <= 0
            or observed.filled_quantity < observed.quantity
        ):
            raise RuntimeError(
                "recovery order verification requires a terminal fill record"
            )
        conversion_price = observed.average_fill_price or Decimal(1)
        if (
            mapping.canonical_quantity_unit
            is not mapping.venue_quantity_unit
            and QuantityUnit.BASE
            in {
                mapping.canonical_quantity_unit,
                mapping.venue_quantity_unit,
            }
            and (
                observed.average_fill_price is None
                or not observed.average_fill_price.is_finite()
                or observed.average_fill_price <= 0
            )
        ):
            raise RuntimeError(
                "recovery fill conversion requires a trustworthy average "
                "fill price"
            )
        observed_quantity = _venue_to_canonical_quantity(
            observed.filled_quantity,
            conversion_price,
            mapping,
        )
        if observed_quantity != recovery.filled_quantity:
            raise RuntimeError(
                "recovery order filled quantity verification mismatch"
            )
        self._validate_recovery_identity_claim(
            "exchange",
            venue,
            observed_exchange_id,
            owner,
            pending_exchange_ids,
        )
        self._validate_recovery_identity_claim(
            "client",
            venue,
            observed_client_id,
            owner,
            pending_client_ids,
        )
        return PlanRecoveryOrderEvidence(
            recovery_id=recovery.recovery_id,
            canonical_symbol=canonical_symbol,
            side=observed_side,
            filled_quantity=observed_quantity,
            venue=venue,
            exchange_order_id=observed_exchange_id,
            client_order_id=observed_client_id,
            reconciliation_evidence=recovery.reconciliation_evidence,
        )

    def _validate_recovery_identity_claim(
        self,
        kind: str,
        venue: str,
        identifier: str,
        owner: str,
        pending: dict[tuple[str, str], str],
    ) -> None:
        if not identifier:
            return
        identity = (venue, identifier)
        registry = (
            self._exchange_order_id_owners
            if kind == "exchange"
            else self._client_order_id_owners
        )
        previous_owner = pending.get(identity) or registry.get(identity)
        if previous_owner is not None and previous_owner != owner:
            raise RuntimeError(
                "duplicate or conflicting recovery fill evidence: "
                f"{kind} order identity is already owned by "
                f"{previous_owner}: {venue}:{identifier}"
            )
        pending[identity] = owner

    def _plan_recovery_order_identity(
        self,
        recovery: PlanRecoveryOrderEvidence,
    ) -> tuple[str, str, str]:
        venue = recovery.venue.strip().lower()
        exchange_order_id = (recovery.exchange_order_id or "").strip()
        if exchange_order_id:
            return ("exchange", venue, exchange_order_id)
        return ("client", venue, (recovery.client_order_id or "").strip())

    def _record_schedule_order_evidence(
        self,
        result: ScheduleResult,
        evidence: Sequence[ScheduleRecoveryOrderEvidence],
    ) -> None:
        unresolved: dict[tuple[str, str, str], RouteOrderResult] = {}
        for order in result.routes:
            if (
                not self._schedule_order_reached_exchange(order)
                or self._order_confirmed_terminal(order)
            ):
                continue
            identity = self._schedule_route_identity(order)
            if identity in unresolved:
                raise RuntimeError(
                    "multiple live schedule orders share recovery identity "
                    f"{identity[2]}"
                )
            unresolved[identity] = order
        seen: set[tuple[str, str, str]] = set()
        additions: list[
            tuple[ScheduleRecoveryOrderEvidence, RouteOrderResult]
        ] = []
        for item in evidence:
            identity = self._schedule_evidence_identity(item)
            if identity in seen:
                raise RuntimeError(
                    "duplicate schedule recovery evidence for "
                    f"{identity[0]} order {identity[2]}"
                )
            seen.add(identity)
            routed = unresolved.pop(identity, None)
            if routed is None:
                raise RuntimeError(
                    "schedule recovery evidence does not identify an "
                    f"unresolved submitted order: {identity[2]}"
                )
            if item.filled_quantity > routed.canonical_quantity:
                raise RuntimeError(
                    "schedule recovery fill exceeds the submitted canonical "
                    f"quantity for order {identity[2]}"
                )
            if (
                item.status is RouteOrderStatus.FILLED
                and item.filled_quantity != routed.canonical_quantity
            ):
                raise RuntimeError(
                    "filled schedule recovery evidence must confirm the full "
                    f"canonical quantity for order {identity[2]}"
                )
            additions.append((item, routed))
        if unresolved:
            missing = ", ".join(
                identity[2] for identity in sorted(unresolved)
            )
            raise RuntimeError(
                "operator resolution requires terminal evidence for every "
                f"live exchange order: {missing}"
            )
        for item, routed in additions:
            routed.status = item.status
            routed.cumulative_filled_quantity = max(
                routed.cumulative_filled_quantity,
                item.filled_quantity,
            )
            routed.reconciled = True
            routed.error = None
            routed.cancel_error = None
            routed.reconciliation_error = None
            result.operator_order_evidence.append(item)

    def _schedule_route_identity(
        self,
        routed: RouteOrderResult,
    ) -> tuple[str, str, str]:
        if (
            routed.exchange_order is not None
            and routed.exchange_order.exchange_order_id.strip()
        ):
            return (
                "exchange",
                routed.venue.lower(),
                routed.exchange_order.exchange_order_id.strip(),
            )
        adapter = self.adapters[routed.venue]
        return (
            "client",
            routed.venue.lower(),
            adapter.client_order_id(routed.order).strip(),
        )

    def _schedule_evidence_identity(
        self,
        evidence: ScheduleRecoveryOrderEvidence,
    ) -> tuple[str, str, str]:
        exchange_order_id = (evidence.exchange_order_id or "").strip()
        if exchange_order_id:
            return ("exchange", evidence.venue.strip().lower(), exchange_order_id)
        return (
            "client",
            evidence.venue.strip().lower(),
            (evidence.client_order_id or "").strip(),
        )

    def _require_schedule_routes_terminal(
        self,
        result: ScheduleResult,
    ) -> None:
        unresolved = [
            self._schedule_route_identity(order)[2]
            for order in result.routes
            if self._schedule_order_reached_exchange(order)
            and not self._order_confirmed_terminal(order)
        ]
        if unresolved:
            raise RuntimeError(
                "schedule recovery cannot release risk while a submitted "
                "order is live, ambiguous, or unreconciled: "
                + ", ".join(unresolved)
            )

    def _complete_native_plan_recovery(
        self,
        result: ExecutionPlanResult,
    ) -> None:
        state = self._ems.plan(result.plan_id).state
        if state == _native.PlanState.RECOVERY_REQUIRED:
            self._ems.recover_plan(
                result.plan_id,
                _native.PlanState.UNWIND_REQUIRED,
            )
            self._record_state(result, "UNWIND_REQUIRED")
            state = _native.PlanState.UNWIND_REQUIRED
        elif state == _native.PlanState.RECONCILING:
            self._ems.transition_plan(
                result.plan_id,
                _native.PlanState.HEDGE_REQUIRED,
            )
            self._record_state(result, "HEDGE_REQUIRED")
            state = _native.PlanState.HEDGE_REQUIRED
        if state == _native.PlanState.HEDGE_REQUIRED:
            self._ems.transition_plan(
                result.plan_id,
                _native.PlanState.UNWIND_REQUIRED,
            )
            self._record_state(result, "UNWIND_REQUIRED")
            state = _native.PlanState.UNWIND_REQUIRED
        self._ems.transition_plan(
            result.plan_id,
            _native.PlanState.UNWINDING,
        )
        self._record_state(result, "UNWINDING")
        self._ems.transition_plan(
            result.plan_id,
            _native.PlanState.RECONCILING,
        )
        self._record_state(result, "RECONCILING")

    def _close_recovered_plan(self, result: ExecutionPlanResult) -> None:
        self._ems.transition_plan(
            result.plan_id,
            _native.PlanState.CLOSED,
        )
        self._record_state(result, "CLOSED")
        self._release_risk(result)

    def _release_risk(self, result: ExecutionPlanResult) -> None:
        if result.risk_released:
            return
        self._ems.release_plan(result.plan_id, self._risk)
        result.risk_released = True

    def _record_state(self, result: ExecutionPlanResult, state: str) -> None:
        result.native_state = state
        if not result.state_history or result.state_history[-1] != state:
            result.state_history.append(state)

    def _exchange_order_identity_error(
        self,
        routed: RouteOrderResult,
        exchange_order: ExchangeOrder,
    ) -> str | None:
        owner = self._route_identity_owners.get(id(routed))
        expected_client_order_id = self.adapters[
            routed.venue
        ].client_order_id(routed.order)
        venue = routed.venue.lower()
        mismatches: list[str] = []
        if owner is None:
            mismatches.append("route owner is not registered")
        if exchange_order.venue.strip().lower() != venue:
            mismatches.append(
                f"venue={exchange_order.venue!r}, expected={routed.venue!r}"
            )
        if exchange_order.symbol.strip().upper() != routed.order.symbol.upper():
            mismatches.append(
                f"symbol={exchange_order.symbol!r}, "
                f"expected={routed.order.symbol!r}"
            )
        if exchange_order.side.strip().lower() != routed.order.side.value:
            mismatches.append(
                f"side={exchange_order.side!r}, "
                f"expected={routed.order.side.value!r}"
            )
        client_order_id = exchange_order.client_order_id.strip()
        if client_order_id != expected_client_order_id:
            mismatches.append(
                f"client_order_id={exchange_order.client_order_id!r}, "
                f"expected={expected_client_order_id!r}"
            )
        exchange_order_id = exchange_order.exchange_order_id.strip()
        for kind, identifier, registry in (
            (
                "exchange_order_id",
                exchange_order_id,
                self._exchange_order_id_owners,
            ),
            (
                "client_order_id",
                client_order_id,
                self._client_order_id_owners,
            ),
        ):
            if not identifier:
                continue
            previous_owner = registry.get((venue, identifier))
            if (
                previous_owner is not None
                and owner is not None
                and previous_owner != owner
            ):
                mismatches.append(
                    f"{kind}={identifier!r} is owned by {previous_owner!r}"
                )
        if not mismatches:
            return None
        return "adapter response identity mismatch: " + "; ".join(mismatches)

    def _apply_exchange_order(
        self,
        routed: RouteOrderResult,
        exchange_order: ExchangeOrder,
        *,
        reconciled: bool = True,
    ) -> RouteOrderStatus:
        previous_status = routed.status
        identity_error = self._exchange_order_identity_error(
            routed,
            exchange_order,
        )
        if identity_error is not None:
            routed.reconciled = False
            routed.reconciliation_error = identity_error
            routed.error = identity_error
            if previous_status not in {
                RouteOrderStatus.FILLED,
                RouteOrderStatus.CANCELED,
                RouteOrderStatus.REJECTED,
                RouteOrderStatus.FAILED,
            }:
                routed.status = RouteOrderStatus.AMBIGUOUS
            return routed.status
        self._register_original_exchange_order(routed, exchange_order)
        observed_fill = _canonical_exchange_fill(routed, exchange_order)
        routed.exchange_order = exchange_order
        if observed_fill is None:
            routed.reconciled = False
            routed.reconciliation_error = (
                "nonzero price-dependent venue fill lacks a trustworthy "
                "average_fill_price"
            )
            routed.error = routed.reconciliation_error
            routed.status = RouteOrderStatus.AMBIGUOUS
            return RouteOrderStatus.AMBIGUOUS
        routed.cumulative_filled_quantity = max(
            routed.cumulative_filled_quantity,
            observed_fill,
        )
        status = exchange_order.status.lower()
        if (
            exchange_order.quantity > 0
            and exchange_order.filled_quantity >= exchange_order.quantity
        ) or status in _FILLED_STATUSES:
            observed_status = RouteOrderStatus.FILLED
        elif status in _CANCELED_STATUSES:
            observed_status = RouteOrderStatus.CANCELED
        elif status in _REJECTED_STATUSES:
            observed_status = RouteOrderStatus.REJECTED
        elif exchange_order.filled_quantity > 0:
            observed_status = RouteOrderStatus.PARTIALLY_FILLED
        elif status in _OPEN_STATUSES:
            observed_status = RouteOrderStatus.ACCEPTED
        else:
            observed_status = RouteOrderStatus.AMBIGUOUS
            routed.error = f"unsupported exchange order status: {status}"
        routed.reconciled = reconciled and observed_status in {
            RouteOrderStatus.FILLED,
            RouteOrderStatus.CANCELED,
            RouteOrderStatus.REJECTED,
        }
        if routed.reconciled:
            routed.reconciliation_error = None
            routed.cancel_error = None
        terminal_statuses = {
            RouteOrderStatus.FILLED,
            RouteOrderStatus.CANCELED,
            RouteOrderStatus.REJECTED,
            RouteOrderStatus.FAILED,
        }
        if previous_status is RouteOrderStatus.FILLED:
            routed.status = RouteOrderStatus.FILLED
        elif (
            previous_status in terminal_statuses
            and observed_status
            in {
                RouteOrderStatus.ACCEPTED,
                RouteOrderStatus.PARTIALLY_FILLED,
                RouteOrderStatus.AMBIGUOUS,
            }
        ):
            routed.status = previous_status
        elif (
            observed_status is RouteOrderStatus.ACCEPTED
            and routed.cumulative_filled_quantity > 0
        ):
            routed.status = RouteOrderStatus.PARTIALLY_FILLED
        else:
            routed.status = observed_status
        return observed_status

    def _register_original_exchange_order(
        self,
        routed: RouteOrderResult,
        exchange_order: ExchangeOrder,
    ) -> None:
        owner = self._route_identity_owners.get(id(routed))
        if owner is None:
            raise RuntimeError("route order identity owner is not registered")
        venue = routed.venue.lower()
        expected_client_id = self.adapters[venue].client_order_id(routed.order)
        exchange_order_id = exchange_order.exchange_order_id.strip()
        client_order_id = exchange_order.client_order_id.strip()
        if exchange_order.venue.strip().lower() != venue:
            raise RuntimeError("exchange order venue does not match route owner")
        if exchange_order.symbol.strip().upper() != routed.order.symbol.upper():
            raise RuntimeError("exchange order symbol does not match route owner")
        if exchange_order.side.strip().lower() != routed.order.side.value:
            raise RuntimeError("exchange order side does not match route owner")
        if client_order_id != expected_client_id:
            raise RuntimeError(
                "exchange order client ID does not match route owner"
            )
        client_identity = (venue, client_order_id)
        identities: list[
            tuple[str, tuple[str, str], dict[tuple[str, str], str]]
        ] = [
            (
                "client order ID",
                client_identity,
                self._client_order_id_owners,
            )
        ]
        if exchange_order_id:
            identities.append(
                (
                    "exchange order ID",
                    (venue, exchange_order_id),
                    self._exchange_order_id_owners,
                )
            )
        for kind, identity, registry in identities:
            previous_owner = registry.get(identity)
            if previous_owner is not None and previous_owner != owner:
                raise RuntimeError(
                    f"coordinator-wide {kind} collision between "
                    f"{previous_owner} and {owner}: {identity}"
                )
        if exchange_order_id:
            self._exchange_order_id_owners[(venue, exchange_order_id)] = owner
        self._client_order_id_owners[client_identity] = owner

    def _order_may_be_live(self, routed: RouteOrderResult) -> bool:
        return routed.status in {
            RouteOrderStatus.SUBMITTING,
            RouteOrderStatus.ACCEPTED,
            RouteOrderStatus.PARTIALLY_FILLED,
            RouteOrderStatus.AMBIGUOUS,
        }

    def _order_needs_cancellation(self, routed: RouteOrderResult) -> bool:
        if self._order_may_be_live(routed):
            return True
        return (
            routed.status
            in {
                RouteOrderStatus.CANCELED,
                RouteOrderStatus.REJECTED,
                RouteOrderStatus.FAILED,
            }
            and not self._order_confirmed_terminal(routed)
        )

    def _order_confirmed_terminal(self, routed: RouteOrderResult) -> bool:
        return (
            routed.status
            in {
                RouteOrderStatus.FILLED,
                RouteOrderStatus.CANCELED,
                RouteOrderStatus.REJECTED,
                RouteOrderStatus.FAILED,
                RouteOrderStatus.SKIPPED,
            }
            and routed.reconciled
            and routed.reconciliation_error is None
            and routed.cancel_error is None
        )

    def _mark_terminal_observation_conflict(
        self,
        routed: RouteOrderResult,
        previous_status: RouteOrderStatus,
        observed_status: RouteOrderStatus,
    ) -> None:
        if (
            previous_status
            in {
                RouteOrderStatus.FILLED,
                RouteOrderStatus.CANCELED,
                RouteOrderStatus.REJECTED,
                RouteOrderStatus.FAILED,
            }
            and observed_status
            in {
                RouteOrderStatus.ACCEPTED,
                RouteOrderStatus.PARTIALLY_FILLED,
                RouteOrderStatus.AMBIGUOUS,
            }
        ):
            routed.reconciled = False
            routed.reconciliation_error = (
                "terminal order reconciliation returned non-terminal state "
                f"{observed_status.value}"
            )

    def _schedule_has_terminal_partial_exposure(
        self,
        result: ScheduleResult,
    ) -> bool:
        return (
            Decimal(0)
            < result.cumulative_filled_quantity
            < result.requested_quantity
            and any(
                order.status
                in {
                    RouteOrderStatus.CANCELED,
                    RouteOrderStatus.REJECTED,
                    RouteOrderStatus.FAILED,
                }
                for order in result.routes
            )
        )

    def _release_terminal_schedule(self, schedule_id: str) -> None:
        result = self._schedule_results[schedule_id]
        terminal_statuses = {
            RouteOrderStatus.FILLED,
            RouteOrderStatus.CANCELED,
            RouteOrderStatus.REJECTED,
            RouteOrderStatus.FAILED,
            RouteOrderStatus.SKIPPED,
        }
        if result.queued_slice_count:
            return
        if any(
            order.status not in terminal_statuses or not order.reconciled
            for order in result.routes
        ):
            return
        if (
            result.residual_canonical_exposure > 0
            and not result.recovery_completed
            and not result.operator_resolved
        ):
            return
        self._release_schedule_risk(schedule_id)

    def _release_schedule_risk(self, schedule_id: str) -> None:
        result = self._schedule_results[schedule_id]
        if result.risk_released:
            return
        risk_key = self._schedule_reservations.pop(schedule_id, None)
        if risk_key is None:
            if not result.operator_resolved:
                raise RuntimeError("schedule risk reservation is missing")
            result.risk_released = True
            return
        if not self._risk.release(risk_key):
            raise RuntimeError("schedule risk reservation is missing")
        result.risk_released = True

    def _set_schedule_state(
        self,
        result: ScheduleResult,
        state: ScheduleState,
    ) -> None:
        result.state = state
        if not result.state_history or result.state_history[-1] is not state:
            result.state_history.append(state)

    def _refresh_schedule_state(self, schedule_id: str) -> None:
        result = self._schedule_results[schedule_id]
        result.queued_slice_count = len(
            self._schedule_sequences.get(schedule_id, ())
        )
        if result.operator_resolved:
            self._set_schedule_state(result, ScheduleState.OPERATOR_RESOLVED)
            return
        if result.recovery_completed:
            self._set_schedule_state(result, ScheduleState.RECOVERED)
            return
        live_statuses = {
            RouteOrderStatus.ACCEPTED,
            RouteOrderStatus.PARTIALLY_FILLED,
            RouteOrderStatus.SUBMITTING,
        }
        if any(
            order.status is RouteOrderStatus.AMBIGUOUS
            or order.reconciliation_error is not None
            or order.cancel_error is not None
            for order in result.routes
        ):
            self._set_schedule_state(result, ScheduleState.RECOVERY_REQUIRED)
            return
        if any(order.status in live_statuses for order in result.routes):
            state = (
                ScheduleState.PARTIALLY_FILLED
                if result.cumulative_filled_quantity > 0
                else ScheduleState.OPEN
            )
            self._set_schedule_state(result, state)
            return
        if result.queued_slice_count:
            state = (
                ScheduleState.RUNNING
                if result.slices
                else ScheduleState.SCHEDULED
            )
            self._set_schedule_state(result, state)
            return

        terminal_statuses = {
            RouteOrderStatus.FILLED,
            RouteOrderStatus.CANCELED,
            RouteOrderStatus.REJECTED,
            RouteOrderStatus.FAILED,
            RouteOrderStatus.SKIPPED,
        }
        if any(
            order.status not in terminal_statuses or not order.reconciled
            for order in result.routes
        ):
            self._set_schedule_state(result, ScheduleState.RECOVERY_REQUIRED)
            return
        if result.routes and all(
            order.status is RouteOrderStatus.FILLED
            for order in result.routes
        ):
            if (
                result.cumulative_filled_quantity
                == result.requested_quantity
            ):
                self._set_schedule_state(result, ScheduleState.FILLED)
            else:
                self._set_schedule_state(
                    result,
                    ScheduleState.UNWIND_REQUIRED,
                )
                return
        elif result.cumulative_filled_quantity > 0:
            self._set_schedule_state(result, ScheduleState.UNWIND_REQUIRED)
            return
        elif result.cancel_requested or any(
            order.status in {
                RouteOrderStatus.CANCELED,
                RouteOrderStatus.SKIPPED,
            }
            for order in result.routes
        ):
            self._set_schedule_state(result, ScheduleState.CANCELED)
        elif any(
            order.status in {
                RouteOrderStatus.FAILED,
                RouteOrderStatus.REJECTED,
            }
            for order in result.routes
        ):
            self._set_schedule_state(result, ScheduleState.FAILED)
        else:
            self._set_schedule_state(result, ScheduleState.CANCELED)
        self._release_terminal_schedule(schedule_id)

    def _cancel_queued_schedule_slices(self, schedule_id: str) -> None:
        sequences = self._schedule_sequences.get(schedule_id)
        if not sequences:
            return
        result = self._schedule_results[schedule_id]
        queued = sorted(
            (
                self._scheduled[sequence]
                for sequence in sequences
                if sequence in self._scheduled
            ),
            key=lambda event: (event.timestamp_ns, event.slice_index),
        )
        for event in queued:
            for routed in event.orders:
                routed.status = RouteOrderStatus.SKIPPED
                routed.reconciled = True
            result.slices.append(
                ScheduledSliceResult(
                    event.schedule_id,
                    event.slice_index,
                    event.timestamp_ns,
                    event.quantity,
                    ExecutionOutcome.CANCELED,
                    event.orders,
                )
            )
        self._ignored_schedule_sequences.update(sequences)
        sequences.clear()
        result.queued_slice_count = 0

    def _schedule_order_reached_exchange(
        self,
        routed: RouteOrderResult,
    ) -> bool:
        return (
            routed.exchange_order is not None
            or routed.status
            in {
                RouteOrderStatus.SUBMITTING,
                RouteOrderStatus.ACCEPTED,
                RouteOrderStatus.PARTIALLY_FILLED,
                RouteOrderStatus.FILLED,
                RouteOrderStatus.CANCELED,
                RouteOrderStatus.AMBIGUOUS,
            }
        )

    async def _query_schedule_order(
        self,
        routed: RouteOrderResult,
    ) -> RouteOrderStatus | None:
        adapter = self.adapters[routed.venue]
        previous_status = routed.status
        try:
            exchange_order = await adapter.query_order(
                routed.order.symbol,
                exchange_order_id=(
                    routed.exchange_order.exchange_order_id
                    if routed.exchange_order is not None
                    else None
                ),
                client_order_id=(
                    None
                    if routed.exchange_order is not None
                    else adapter.client_order_id(routed.order)
                ),
            )
        except (
            ExchangeAdapterError,
            BinanceGatewayError,
            asyncio.TimeoutError,
            OSError,
        ) as error:
            if previous_status not in {
                RouteOrderStatus.FILLED,
                RouteOrderStatus.CANCELED,
                RouteOrderStatus.REJECTED,
                RouteOrderStatus.FAILED,
            }:
                routed.status = RouteOrderStatus.AMBIGUOUS
            routed.reconciled = False
            routed.reconciliation_error = (
                f"{type(error).__name__}: {error}"
            )
            return None
        observed_status = self._apply_exchange_order(
            routed,
            exchange_order,
            reconciled=True,
        )
        if (
            previous_status
            in {
                RouteOrderStatus.FILLED,
                RouteOrderStatus.CANCELED,
                RouteOrderStatus.REJECTED,
                RouteOrderStatus.FAILED,
            }
            and observed_status
            in {
                RouteOrderStatus.ACCEPTED,
                RouteOrderStatus.PARTIALLY_FILLED,
                RouteOrderStatus.AMBIGUOUS,
            }
        ):
            routed.reconciled = False
            routed.reconciliation_error = (
                "terminal order reconciliation returned non-terminal state "
                f"{observed_status.value}"
            )
        elif observed_status in {
            RouteOrderStatus.FILLED,
            RouteOrderStatus.CANCELED,
            RouteOrderStatus.REJECTED,
        }:
            routed.cancel_error = None
        return observed_status

    async def _cancel_schedule_order(
        self,
        routed: RouteOrderResult,
    ) -> None:
        adapter = self.adapters[routed.venue]
        try:
            canceled = await adapter.cancel_order(
                routed.order.symbol,
                exchange_order_id=(
                    routed.exchange_order.exchange_order_id
                    if routed.exchange_order is not None
                    else None
                ),
                client_order_id=(
                    None
                    if routed.exchange_order is not None
                    else adapter.client_order_id(routed.order)
                ),
            )
        except (
            AmbiguousOrderError,
            ExchangeAdapterError,
            BinanceGatewayError,
            asyncio.TimeoutError,
            OSError,
        ) as error:
            routed.cancel_error = f"{type(error).__name__}: {error}"
        else:
            routed.cancel_error = None
            self._apply_exchange_order(routed, canceled, reconciled=False)

        observed_status = await self._query_schedule_order(routed)
        if observed_status is None:
            return
        if observed_status in {
            RouteOrderStatus.ACCEPTED,
            RouteOrderStatus.PARTIALLY_FILLED,
            RouteOrderStatus.SUBMITTING,
            RouteOrderStatus.AMBIGUOUS,
        }:
            routed.reconciled = False
            routed.cancel_error = (
                "cancel reconciliation remains non-terminal with state "
                f"{observed_status.value}"
            )

    async def _recover_schedule_after_slice(
        self,
        schedule_id: str,
        latest: ScheduledSliceResult,
    ) -> None:
        if latest.outcome not in {
            ExecutionOutcome.FAILED,
            ExecutionOutcome.RECONCILIATION_REQUIRED,
            ExecutionOutcome.UNWIND_REQUIRED,
        }:
            return
        self._halted_schedules.add(schedule_id)
        await self._reconcile_schedule_locked(
            schedule_id,
            cancel_open_orders=True,
        )

    def _refresh_schedule_slice_outcomes(self, schedule_id: str) -> None:
        for result in self._completed_schedules[schedule_id]:
            if not result.orders:
                result.outcome = ExecutionOutcome.CANCELED
            elif any(
                order.status is RouteOrderStatus.AMBIGUOUS
                or order.reconciliation_error is not None
                or order.cancel_error is not None
                for order in result.orders
            ):
                result.outcome = ExecutionOutcome.RECONCILIATION_REQUIRED
            elif any(
                order.status
                in {
                    RouteOrderStatus.ACCEPTED,
                    RouteOrderStatus.PARTIALLY_FILLED,
                    RouteOrderStatus.SUBMITTING,
                }
                for order in result.orders
            ):
                result.outcome = (
                    ExecutionOutcome.PARTIAL
                    if any(order.filled_quantity > 0 for order in result.orders)
                    else ExecutionOutcome.OPEN
                )
            elif all(
                order.status is RouteOrderStatus.FILLED
                for order in result.orders
            ):
                filled_quantity = sum(
                    (order.filled_quantity for order in result.orders),
                    start=Decimal(0),
                )
                result.outcome = (
                    ExecutionOutcome.FILLED
                    if filled_quantity == result.quantity
                    else ExecutionOutcome.UNWIND_REQUIRED
                )
            elif any(order.filled_quantity > 0 for order in result.orders):
                result.outcome = ExecutionOutcome.PARTIAL
            elif any(
                order.status
                in {
                    RouteOrderStatus.FAILED,
                    RouteOrderStatus.REJECTED,
                }
                for order in result.orders
            ):
                result.outcome = ExecutionOutcome.FAILED
            else:
                result.outcome = ExecutionOutcome.CANCELED

    async def _execute_scheduled_slice(
        self,
        scheduled: _ScheduledEvent,
    ) -> ScheduledSliceResult:
        if (
            scheduled.quantity == 0
            or scheduled.schedule_id in self._halted_schedules
        ):
            for routed in scheduled.orders:
                routed.status = RouteOrderStatus.SKIPPED
                routed.reconciled = True
            return ScheduledSliceResult(
                scheduled.schedule_id,
                scheduled.slice_index,
                scheduled.timestamp_ns,
                scheduled.quantity,
                ExecutionOutcome.CANCELED,
                scheduled.orders,
            )
        synthetic_leg = ExecutionLeg(
            symbol=scheduled.leg.symbol,
            side=scheduled.leg.side,
            quantity=scheduled.quantity,
            reference_price=scheduled.leg.reference_price,
            order_type=scheduled.leg.order_type,
            limit_price=scheduled.leg.limit_price,
            venue_preferences=scheduled.leg.venue_preferences,
            hedge=scheduled.leg.hedge,
        )
        result = ExecutionPlanResult(
            plan_id=f"{scheduled.schedule_id}:{scheduled.slice_index}",
            dispatch_policy=self.dispatch_policy,
            legs=[
                ExecutionLegResult(
                    0,
                    synthetic_leg,
                    orders=list(scheduled.orders),
                )
            ],
        )
        await self._dispatch(result)
        failure = any(
            order.status
            in {
                RouteOrderStatus.FAILED,
                RouteOrderStatus.REJECTED,
                RouteOrderStatus.CANCELED,
                RouteOrderStatus.AMBIGUOUS,
                RouteOrderStatus.PENDING,
            }
            for order in result.orders
        )
        if any(order.status is RouteOrderStatus.AMBIGUOUS for order in result.orders):
            outcome = ExecutionOutcome.RECONCILIATION_REQUIRED
        elif failure and any(
            order.filled_quantity > 0 for order in result.orders
        ):
            outcome = ExecutionOutcome.UNWIND_REQUIRED
        elif all(
            order.status is RouteOrderStatus.FILLED for order in result.orders
        ):
            filled_quantity = sum(
                (order.filled_quantity for order in result.orders),
                start=Decimal(0),
            )
            outcome = (
                ExecutionOutcome.FILLED
                if filled_quantity == scheduled.quantity
                else ExecutionOutcome.UNWIND_REQUIRED
            )
        elif failure:
            outcome = ExecutionOutcome.FAILED
        elif any(order.filled_quantity > 0 for order in result.orders):
            outcome = ExecutionOutcome.PARTIAL
        else:
            outcome = ExecutionOutcome.OPEN
        return ScheduledSliceResult(
            scheduled.schedule_id,
            scheduled.slice_index,
            scheduled.timestamp_ns,
            scheduled.quantity,
            outcome,
            result.orders,
        )


def build_guarded_adapters(
    config: AppConfig,
    *,
    load_credentials: bool = True,
) -> dict[str, ExchangeAdapter]:
    """Build credentialed adapters for guarded non-production environments."""

    if config.binance_execution.environment != "testnet":
        raise ValueError("EMS only supports Binance Spot Testnet")
    adapters: dict[str, ExchangeAdapter] = {}
    if "binance" in config.ems.enabled_venues:
        binance_credentials = (
            BinanceCredentials.from_environment(config.binance_execution)
            if load_credentials
            else None
        )
        adapters["binance"] = BinanceTestnetAdapter(
            BinanceSpotTestnetGateway(
                config.binance_execution,
                binance_credentials,
            )
        )
    okx_config = OKXDemoConfig(
        rest_base_url=config.okx_demo_execution.rest_base_url,
        api_key_environment_variable=(
            config.okx_demo_execution.api_key_environment_variable
        ),
        api_secret_environment_variable=(
            config.okx_demo_execution.api_secret_environment_variable
        ),
        passphrase_environment_variable=(
            config.okx_demo_execution.passphrase_environment_variable
        ),
        request_timeout_seconds=(
            config.okx_demo_execution.request_timeout_seconds
        ),
        transaction_expiry_ms=config.okx_demo_execution.transaction_expiry_ms,
        instrument_type=config.okx_demo_execution.instrument_type,
        trade_mode=config.okx_demo_execution.trade_mode,
    )
    deribit_config = DeribitTestConfig(
        rest_base_url=config.deribit_test_execution.rest_base_url,
        client_id_environment_variable=(
            config.deribit_test_execution.client_id_environment_variable
        ),
        client_secret_environment_variable=(
            config.deribit_test_execution.client_secret_environment_variable
        ),
        request_timeout_seconds=(
            config.deribit_test_execution.request_timeout_seconds
        ),
        transaction_expiry_ms=(
            config.deribit_test_execution.transaction_expiry_ms
        ),
        instrument_kind=config.deribit_test_execution.instrument_kind,
    )
    if "okx" in config.ems.enabled_venues:
        adapters["okx"] = OKXDemoAdapter(
            okx_config,
            (
                OKXCredentials.from_environment(okx_config)
                if load_credentials
                else None
            ),
        )
    if "deribit" in config.ems.enabled_venues:
        adapters["deribit"] = DeribitTestAdapter(
            deribit_config,
            (
                DeribitCredentials.from_environment(deribit_config)
                if load_credentials
                else None
            ),
        )
    return adapters


def _native_side(side: Side) -> Any:
    return _native.Side.BUY if side is Side.BUY else _native.Side.SELL


def _native_leg(leg: ExecutionLeg) -> Any:
    native_leg = _native.Leg()
    native_leg.symbol = leg.symbol.upper()
    native_leg.side = _native_side(leg.side)
    native_leg.quantity = float(leg.quantity)
    native_leg.reference_price = float(leg.reference_price)
    native_leg.venue_preferences = [
        venue.lower() for venue in leg.venue_preferences
    ]
    native_leg.hedge = leg.hedge
    return native_leg


def _route_order(
    plan_id: str,
    leg_index: int,
    slice_index: int,
    leg: ExecutionLeg,
    route: Any,
) -> Order:
    source = f"{plan_id}:{leg_index}:{slice_index}:{route.venue}"
    order_id = uuid5(NAMESPACE_URL, f"ems-order:{source}")
    intent_id = uuid5(NAMESPACE_URL, f"ems-intent:{plan_id}:{leg_index}")
    client_order_id = f"ems-{uuid5(NAMESPACE_URL, source).hex[:28]}"
    return Order(
        order_id=order_id,
        intent_id=intent_id,
        client_order_id=client_order_id,
        symbol=getattr(route, "venue_symbol", leg.symbol.upper()),
        side=leg.side,
        order_type=leg.order_type,
        quantity=Decimal(
            str(getattr(route, "venue_quantity", route.quantity))
        ),
        price=(
            None
            if leg.order_type is OrderType.MARKET
            else getattr(route, "order_price", None)
            or leg.limit_price
            or Decimal(str(route.reference_price))
        ),
        state=OrderState.RISK_APPROVED,
        created_timestamp_ns=time.time_ns(),
    )


def _replace_order(
    order: Order,
    *,
    quantity: Decimal,
    price: Decimal | None,
) -> Order:
    return Order(
        order_id=order.order_id,
        intent_id=order.intent_id,
        client_order_id=order.client_order_id,
        symbol=order.symbol,
        side=order.side,
        order_type=order.order_type,
        quantity=quantity,
        price=price,
        state=order.state,
        created_timestamp_ns=order.created_timestamp_ns,
    )


def _route_dict(route: Any) -> dict[str, Decimal | str]:
    return {
        "venue": route.venue,
        "quantity": Decimal(str(route.quantity)),
        "reference_price": Decimal(str(route.reference_price)),
        "expected_fee": Decimal(str(route.expected_fee)),
        "score": Decimal(str(route.score)),
    }


def _canonical_exchange_fill(
    routed: RouteOrderResult,
    exchange_order: ExchangeOrder,
) -> Decimal | None:
    if routed.quantity_mapping is not None and routed.reference_price is not None:
        mapping = routed.quantity_mapping
        conversion_price = routed.reference_price
        if (
            exchange_order.filled_quantity != 0
            and mapping.canonical_quantity_unit
            is not mapping.venue_quantity_unit
            and QuantityUnit.BASE
            in {
                mapping.canonical_quantity_unit,
                mapping.venue_quantity_unit,
            }
        ):
            average_fill_price = exchange_order.average_fill_price
            if (
                average_fill_price is None
                or not average_fill_price.is_finite()
                or average_fill_price <= 0
            ):
                return None
            conversion_price = average_fill_price
        return _venue_to_canonical_quantity(
            exchange_order.filled_quantity,
            conversion_price,
            mapping,
        )
    if routed.order.quantity == 0:
        return Decimal(0)
    return (
        exchange_order.filled_quantity
        * routed.canonical_quantity
        / routed.order.quantity
    )


def _binance_order(order: BinanceOrderResult) -> ExchangeOrder:
    return ExchangeOrder(
        venue="binance",
        symbol=order.symbol,
        exchange_order_id=str(order.order_id),
        client_order_id=order.client_order_id,
        status=order.status.lower(),
        side=order.side.lower(),
        order_type=order.order_type.lower(),
        quantity=order.original_quantity,
        filled_quantity=order.executed_quantity,
        price=order.price if order.price > 0 else None,
        average_fill_price=order.average_fill_price,
        timestamp_ms=order.transact_time_ms,
        raw=order.raw,
    )


def _binance_trade(symbol: str, row: dict[str, Any]) -> ExchangeTrade:
    return ExchangeTrade(
        venue="binance",
        symbol=symbol.upper(),
        trade_id=str(row["id"]),
        exchange_order_id=str(row["orderId"]),
        side="buy" if bool(row.get("isBuyer")) else "sell",
        price=Decimal(str(row["price"])),
        quantity=Decimal(str(row["qty"])),
        fee=(
            Decimal(str(row["commission"]))
            if row.get("commission") is not None
            else None
        ),
        fee_currency=(
            str(row["commissionAsset"])
            if row.get("commissionAsset") is not None
            else None
        ),
        timestamp_ms=int(row["time"]),
        raw=row,
    )


def _floor_to_step(quantity: Decimal, step: Decimal) -> Decimal:
    if quantity <= 0:
        return Decimal(0)
    return (
        (quantity / step).to_integral_value(rounding=ROUND_DOWN)
        * step
    )


def _default_instrument_mappings() -> tuple[InstrumentMapping, ...]:
    return (
        InstrumentMapping(
            canonical_symbol="BTCUSDT",
            venue="binance",
            venue_symbol="BTCUSDT",
            compatibility_group="btc-usdt-spot",
        ),
        InstrumentMapping(
            canonical_symbol="BTCUSDT",
            venue="okx",
            venue_symbol="BTC-USDT",
            compatibility_group="btc-usdt-spot",
        ),
        InstrumentMapping(
            canonical_symbol="ETHUSDT",
            venue="binance",
            venue_symbol="ETHUSDT",
            compatibility_group="eth-usdt-spot",
        ),
        InstrumentMapping(
            canonical_symbol="ETHUSDT",
            venue="okx",
            venue_symbol="ETH-USDT",
            compatibility_group="eth-usdt-spot",
        ),
        InstrumentMapping(
            canonical_symbol="BTC-PERPETUAL",
            venue="deribit",
            venue_symbol="BTC-PERPETUAL",
            compatibility_group="btc-usd-inverse-perpetual",
            venue_quantity_unit=QuantityUnit.QUOTE,
        ),
    )


def instrument_mappings_from_config(
    config: AppConfig,
) -> tuple[InstrumentMapping, ...]:
    mappings: list[InstrumentMapping] = []
    for canonical_symbol, instrument in config.ems.instruments.items():
        for venue, venue_mapping in instrument.venues.items():
            if venue not in config.ems.enabled_venues:
                continue
            mappings.append(
                InstrumentMapping(
                    canonical_symbol=canonical_symbol,
                    venue=venue,
                    venue_symbol=venue_mapping.symbol,
                    compatibility_group=instrument.compatibility_group,
                    canonical_quantity_unit=QuantityUnit(
                        instrument.canonical_quantity_unit
                    ),
                    venue_quantity_unit=QuantityUnit(
                        venue_mapping.quantity_unit
                    ),
                    contract_size_quote=venue_mapping.contract_size_quote,
                )
            )
    return tuple(mappings)


def _canonical_to_venue_quantity(
    quantity: Decimal,
    reference_price: Decimal,
    mapping: InstrumentMapping,
) -> Decimal:
    return _convert_quantity(
        quantity,
        reference_price,
        mapping.canonical_quantity_unit,
        mapping.venue_quantity_unit,
        mapping.contract_size_quote,
    )


def _venue_to_canonical_quantity(
    quantity: Decimal,
    reference_price: Decimal,
    mapping: InstrumentMapping,
) -> Decimal:
    return _convert_quantity(
        quantity,
        reference_price,
        mapping.venue_quantity_unit,
        mapping.canonical_quantity_unit,
        mapping.contract_size_quote,
    )


def _convert_quantity(
    quantity: Decimal,
    reference_price: Decimal,
    source_unit: QuantityUnit,
    target_unit: QuantityUnit,
    contract_size_quote: Decimal | None,
) -> Decimal:
    if quantity < 0 or reference_price <= 0:
        raise ValueError("quantity must be non-negative and price positive")
    if source_unit is target_unit:
        return quantity
    if source_unit is QuantityUnit.BASE:
        quote_quantity = quantity * reference_price
    elif source_unit is QuantityUnit.QUOTE:
        quote_quantity = quantity
    else:
        if contract_size_quote is None:
            raise ValueError("contract conversion requires contract size")
        quote_quantity = quantity * contract_size_quote
    if target_unit is QuantityUnit.QUOTE:
        return quote_quantity
    if target_unit is QuantityUnit.BASE:
        return quote_quantity / reference_price
    if contract_size_quote is None:
        raise ValueError("contract conversion requires contract size")
    return quote_quantity / contract_size_quote


def _allocate_schedule_quantity(
    total_quantity: Decimal,
    step: Decimal,
    minimum: Decimal,
    maximum: Decimal | None,
    raw_quantities: Sequence[Decimal],
) -> tuple[Decimal, ...]:
    raw_total = sum(raw_quantities, start=Decimal(0))
    if raw_total <= 0:
        raise ValueError("schedule allocation requires positive raw quantity")
    total_units_decimal = total_quantity / step
    total_units = int(total_units_decimal)
    if Decimal(total_units) != total_units_decimal:
        raise UnexecutableResidualError(
            f"normalized quantity {total_quantity} is not aligned to step {step}"
        )
    positive = [
        index for index, quantity in enumerate(raw_quantities) if quantity > 0
    ]
    if not positive:
        raise ValueError("schedule allocation requires a positive slice")
    last_positive = positive[-1]
    quantities = [Decimal(0) for _ in raw_quantities]
    cumulative = Decimal(0)
    allocated_units = 0
    for index, raw_quantity in enumerate(raw_quantities):
        if raw_quantity <= 0:
            continue
        if index == last_positive:
            units = total_units - allocated_units
        else:
            cumulative += raw_quantity
            target_units = int(
                (
                    Decimal(total_units) * cumulative / raw_total
                ).to_integral_value(rounding=ROUND_DOWN)
            )
            units = target_units - allocated_units
        quantities[index] = Decimal(units) * step
        allocated_units += units

    for index in range(len(quantities)):
        quantity = quantities[index]
        if quantity == 0 or quantity >= minimum:
            continue
        target = _coalesce_target_values(
            quantities,
            index,
            minimum,
            maximum,
        )
        if target is None:
            raise UnexecutableResidualError(
                f"schedule residual {quantity} is below minimum {minimum}"
            )
        quantities[target] += quantity
        quantities[index] = Decimal(0)

    if maximum is not None:
        aligned_maximum = _floor_to_step(maximum, step)
        for index, quantity in enumerate(tuple(quantities)):
            if quantity <= aligned_maximum:
                continue
            excess = quantity - aligned_maximum
            quantities[index] = aligned_maximum
            for target in range(index + 1, len(quantities)):
                room = aligned_maximum - quantities[target]
                moved = min(excess, room)
                quantities[target] += moved
                excess -= moved
                if excess == 0:
                    break
            if excess != 0:
                raise UnexecutableResidualError(
                    "schedule quantity exceeds per-order venue maximum"
                )

    if sum(quantities, start=Decimal(0)) != total_quantity:
        raise UnexecutableResidualError(
            "schedule lot allocation does not conserve normalized quantity"
        )
    return tuple(quantities)


def _coalesce_target(
    quantities: Sequence[Decimal],
    index: int,
    rules: _RouteRules,
) -> int | None:
    return _coalesce_target_values(
        quantities,
        index,
        rules.minimum,
        rules.maximum_per_order,
    )


def _coalesce_target_values(
    quantities: Sequence[Decimal],
    index: int,
    minimum: Decimal,
    maximum: Decimal | None,
) -> int | None:
    candidates = list(range(index + 1, len(quantities))) + list(
        range(index - 1, -1, -1)
    )
    for candidate in candidates:
        combined = quantities[candidate] + quantities[index]
        if maximum is not None and combined > maximum:
            continue
        if combined >= minimum:
            return candidate
    return None


__all__ = [
    "BinanceTestnetAdapter",
    "DispatchPolicy",
    "ExecutionCoordinator",
    "ExecutionLeg",
    "ExecutionLegResult",
    "ExecutionOutcome",
    "ExecutionPlanRequest",
    "ExecutionPlanResult",
    "InstrumentMapping",
    "QuantityUnit",
    "RouteOrderResult",
    "RouteOrderStatus",
    "ScheduleResult",
    "ScheduleRiskLimitError",
    "ScheduleState",
    "ScheduledExecution",
    "ScheduledSliceResult",
    "VenueQuote",
    "UnexecutableResidualError",
    "build_guarded_adapters",
    "instrument_mappings_from_config",
]
