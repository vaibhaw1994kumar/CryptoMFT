"""Deterministic in-process exchange used by demos and offline tests.

The simulator implements the :class:`ExchangeAdapter` protocol without any
network, credentials, or native dependencies, so an opportunity can be
driven end to end on any machine. Behaviour is scripted per venue, which
makes partial fills, rejects, and timeouts reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .exchange_adapter import (
    AccountSnapshot,
    ExchangeAPIError,
    ExchangeOrder,
    ExchangeTrade,
    InstrumentRules,
    NormalizedOrder,
    ReconciliationSnapshot,
    RequestTelemetry,
    deterministic_client_id,
)
from .models import Order, Side
from .oms import OrderManagementSystem


@dataclass(slots=True)
class FillStep:
    """One scripted fill applied on the next poll of an order."""

    quantity: Decimal
    price: Decimal


@dataclass(slots=True)
class VenueScript:
    """Scripted behaviour for a single simulated venue.

    ``reject_message`` short-circuits submission. ``fills`` is consumed one
    step per :meth:`SimulatedVenue.query_order` call, which lets a demo show
    a leg progressing from acknowledged to partially filled to filled.
    """

    fills: tuple[FillStep, ...] = ()
    reject_message: str | None = None
    fee_bps: Decimal = Decimal("1")
    tick_size: Decimal = Decimal("0.01")
    quantity_step: Decimal = Decimal("0.00000001")
    minimum_quantity: Decimal = Decimal("0.00000001")
    allow_client_id_reuse: bool = False
    """Emulate venues that free a client order id once it stops being live.

    Binance permits reuse after an order leaves the book, so a blind
    resubmission during recovery creates a *second* real order rather than
    being rejected as a duplicate. Enable this to prove the coordinator
    resyncs before resubmitting instead of relying on the venue to protect
    it.
    """


_SIM_TERMINAL_STATUSES = frozenset(
    {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}
)


@dataclass(slots=True)
class _OrderState:
    order: ExchangeOrder
    remaining: list[FillStep]
    trades: list[ExchangeTrade] = field(default_factory=list)


class SimulatedVenue:
    """An offline exchange adapter with deterministic, scripted behaviour."""

    def __init__(
        self,
        venue: str,
        *,
        script: VenueScript | None = None,
        client_id_length: int = 32,
        alphanumeric_only: bool = False,
    ) -> None:
        self.venue = venue.lower()
        self.script = script or VenueScript()
        self.last_request: RequestTelemetry | None = None
        self.submissions: list[str] = []
        """Every client order id this venue actually opened an order for.

        Duplicate entries mean a caller submitted the same logical order
        twice, which is exactly what the idempotency rules forbid.
        """
        self._client_id_length = client_id_length
        self._alphanumeric_only = alphanumeric_only
        self._orders: dict[str, _OrderState] = {}
        self._sequence = 0

    async def close(self) -> None:
        return None

    def client_order_id(self, order: Order) -> str:
        return deterministic_client_id(
            self.venue,
            order.client_order_id,
            maximum_length=self._client_id_length,
            alphanumeric_only=self._alphanumeric_only,
        )

    async def instrument_rules(self, symbol: str) -> InstrumentRules:
        return InstrumentRules(
            venue=self.venue,
            symbol=symbol.upper(),
            tick_size=self.script.tick_size,
            quantity_step=self.script.quantity_step,
            minimum_quantity=self.script.minimum_quantity,
        )

    async def normalize_order(
        self,
        order: Order,
        *,
        reference_price: Decimal | None = None,
    ) -> NormalizedOrder:
        return NormalizedOrder(
            quantity=order.quantity,
            price=order.price if order.price is not None else reference_price,
            client_order_id=self.client_order_id(order),
        )

    async def submit_order(self, order: Order) -> ExchangeOrder:
        client_order_id = self.client_order_id(order)
        existing = self._orders.get(client_order_id)
        if existing is not None and not (
            self.script.allow_client_id_reuse
            and existing.order.status.upper() in _SIM_TERMINAL_STATUSES
        ):
            # Idempotent resubmission must never create a second order.
            return existing.order
        if self.script.reject_message is not None:
            raise ExchangeAPIError(
                venue=self.venue,
                status=400,
                code=-1013,
                message=self.script.reject_message,
                payload={"msg": self.script.reject_message},
            )
        self._sequence += 1
        exchange_order = ExchangeOrder(
            venue=self.venue,
            symbol=order.symbol.upper(),
            exchange_order_id=f"{self.venue.upper()}-{self._sequence:06d}",
            client_order_id=client_order_id,
            status="NEW",
            side=order.side.value,
            order_type=order.order_type.value,
            quantity=order.quantity,
            filled_quantity=Decimal(0),
            price=order.price,
            average_fill_price=None,
            timestamp_ms=self._sequence,
            raw={"simulated": True},
        )
        self._orders[client_order_id] = _OrderState(
            order=exchange_order,
            remaining=list(self.script.fills),
        )
        self.submissions.append(client_order_id)
        return exchange_order

    async def cancel_order(
        self,
        symbol: str,
        *,
        exchange_order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> ExchangeOrder:
        state = self._find(exchange_order_id, client_order_id)
        state.remaining.clear()
        cancelled = _replace_order(state.order, status="CANCELED")
        state.order = cancelled
        return cancelled

    async def query_order(
        self,
        symbol: str,
        *,
        exchange_order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> ExchangeOrder:
        state = self._find(exchange_order_id, client_order_id)
        if state.remaining and state.order.status not in {
            "CANCELED",
            "FILLED",
        }:
            self._apply_fill(state, state.remaining.pop(0))
        return state.order

    async def open_orders(
        self,
        symbol: str | None = None,
    ) -> tuple[ExchangeOrder, ...]:
        return tuple(
            state.order
            for state in self._orders.values()
            if state.order.status in {"NEW", "PARTIALLY_FILLED"}
            and (symbol is None or state.order.symbol == symbol.upper())
        )

    async def trades(
        self,
        symbol: str,
        *,
        limit: int = 100,
    ) -> tuple[ExchangeTrade, ...]:
        collected: list[ExchangeTrade] = []
        for state in self._orders.values():
            if state.order.symbol != symbol.upper():
                continue
            collected.extend(state.trades)
        return tuple(collected[-limit:])

    async def account(self, currency: str | None = None) -> AccountSnapshot:
        return AccountSnapshot(
            venue=self.venue,
            currency=currency,
            timestamp_ms=self._sequence,
            raw={"simulated": True},
        )

    async def reconcile(
        self,
        oms: OrderManagementSystem,
        symbol: str,
    ) -> ReconciliationSnapshot:
        open_orders = await self.open_orders(symbol)
        return ReconciliationSnapshot(
            venue=self.venue,
            symbol=symbol.upper(),
            captured_at_ms=self._sequence,
            exchange_open_orders=open_orders,
            recent_trades=await self.trades(symbol),
            account=await self.account(),
            missing_on_exchange=(),
            unknown_on_exchange=(),
        )

    def positions(self) -> dict[str, Decimal]:
        """Net filled quantity per symbol, signed by side."""

        positions: dict[str, Decimal] = {}
        for state in self._orders.values():
            signed = (
                state.order.filled_quantity
                if state.order.side == Side.BUY.value
                else -state.order.filled_quantity
            )
            symbol = state.order.symbol
            positions[symbol] = positions.get(symbol, Decimal(0)) + signed
        return positions

    def _find(
        self,
        exchange_order_id: str | None,
        client_order_id: str | None,
    ) -> _OrderState:
        if client_order_id is not None:
            state = self._orders.get(client_order_id)
            if state is not None:
                return state
        if exchange_order_id is not None:
            for state in self._orders.values():
                if state.order.exchange_order_id == exchange_order_id:
                    return state
        raise ExchangeAPIError(
            venue=self.venue,
            status=404,
            code=-2013,
            message="Order does not exist.",
            payload={},
        )

    def _apply_fill(self, state: _OrderState, step: FillStep) -> None:
        filled = min(
            state.order.quantity,
            state.order.filled_quantity + step.quantity,
        )
        if filled <= state.order.filled_quantity:
            return
        increment = filled - state.order.filled_quantity
        previous_notional = state.order.filled_quantity * (
            state.order.average_fill_price or step.price
        )
        average = (previous_notional + increment * step.price) / filled
        status = "FILLED" if filled >= state.order.quantity else (
            "PARTIALLY_FILLED"
        )
        state.order = _replace_order(
            state.order,
            status=status,
            filled_quantity=filled,
            average_fill_price=average,
        )
        fee = increment * step.price * self.script.fee_bps / Decimal(10000)
        state.trades.append(
            ExchangeTrade(
                venue=self.venue,
                symbol=state.order.symbol,
                trade_id=f"{state.order.exchange_order_id}-"
                f"{len(state.trades) + 1}",
                exchange_order_id=state.order.exchange_order_id,
                side=state.order.side,
                price=step.price,
                quantity=increment,
                fee=fee,
                fee_currency="USDT",
                timestamp_ms=self._sequence,
                raw={"simulated": True},
            )
        )


def _replace_order(order: ExchangeOrder, **changes: Any) -> ExchangeOrder:
    values: dict[str, Any] = {
        "venue": order.venue,
        "symbol": order.symbol,
        "exchange_order_id": order.exchange_order_id,
        "client_order_id": order.client_order_id,
        "status": order.status,
        "side": order.side,
        "order_type": order.order_type,
        "quantity": order.quantity,
        "filled_quantity": order.filled_quantity,
        "price": order.price,
        "average_fill_price": order.average_fill_price,
        "timestamp_ms": order.timestamp_ms,
        "raw": order.raw,
    }
    values.update(changes)
    return ExchangeOrder(**values)


__all__ = [
    "FillStep",
    "SimulatedVenue",
    "VenueScript",
]
