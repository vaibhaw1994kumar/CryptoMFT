from __future__ import annotations

from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from trading_platform_poc.models import (
    Order,
    OrderIntent,
    OrderState,
    OrderUpdate,
)


class InvalidOrderTransitionError(Exception):
    pass


OPEN_STATES = {
    OrderState.CREATED,
    OrderState.RISK_APPROVED,
    OrderState.SUBMITTED,
    OrderState.ACKNOWLEDGED,
    OrderState.PARTIALLY_FILLED,
    OrderState.RECONCILIATION_REQUIRED,
}


VALID_TRANSITIONS: dict[OrderState, set[OrderState]] = {
    OrderState.CREATED: {
        OrderState.RISK_APPROVED,
        OrderState.RISK_REJECTED,
    },
    OrderState.RISK_APPROVED: {OrderState.SUBMITTED},
    OrderState.SUBMITTED: {
        OrderState.ACKNOWLEDGED,
        OrderState.RECONCILIATION_REQUIRED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
    },
    OrderState.ACKNOWLEDGED: {
        OrderState.PARTIALLY_FILLED,
        OrderState.RECONCILIATION_REQUIRED,
        OrderState.FILLED,
        OrderState.CANCELED,
        OrderState.EXPIRED,
    },
    OrderState.PARTIALLY_FILLED: {
        OrderState.PARTIALLY_FILLED,
        OrderState.RECONCILIATION_REQUIRED,
        OrderState.FILLED,
        OrderState.CANCELED,
        OrderState.EXPIRED,
    },
    OrderState.RECONCILIATION_REQUIRED: {
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.CANCELED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
    },
    OrderState.RISK_REJECTED: set(),
    OrderState.FILLED: set(),
    OrderState.CANCELED: set(),
    OrderState.REJECTED: set(),
    OrderState.EXPIRED: set(),
}


class OrderManagementSystem:
    def __init__(self) -> None:
        self._orders: dict[UUID, Order] = {}
        self._updates: list[OrderUpdate] = []

    @property
    def orders(self) -> tuple[Order, ...]:
        return tuple(self._orders.values())

    @property
    def updates(self) -> tuple[OrderUpdate, ...]:
        return tuple(self._updates)

    def open_orders(self) -> tuple[Order, ...]:
        return tuple(
            order for order in self._orders.values() if order.state in OPEN_STATES
        )

    def create(self, intent: OrderIntent) -> Order:
        order_id = uuid5(NAMESPACE_URL, f"order:{intent.intent_id}")
        order = Order(
            order_id=order_id,
            intent_id=intent.intent_id,
            client_order_id=f"poc-{order_id.hex[:20]}",
            symbol=intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            quantity=intent.quantity,
            price=intent.price,
            state=OrderState.CREATED,
            created_timestamp_ns=intent.created_timestamp_ns,
        )
        existing = self._orders.get(order_id)
        if existing is not None:
            return existing
        self._orders[order_id] = order
        return order

    def assign_venue(self, order_id: UUID, venue: str) -> Order:
        normalized_venue = venue.strip().lower()
        if not normalized_venue:
            raise ValueError("venue must be non-empty")
        current = self._orders[order_id]
        if current.venue is not None:
            if current.venue != normalized_venue:
                raise ValueError(
                    f"order venue is already assigned to {current.venue}"
                )
            return current
        updated = Order(
            order_id=current.order_id,
            intent_id=current.intent_id,
            client_order_id=current.client_order_id,
            symbol=current.symbol,
            side=current.side,
            order_type=current.order_type,
            quantity=current.quantity,
            price=current.price,
            state=current.state,
            created_timestamp_ns=current.created_timestamp_ns,
            venue=normalized_venue,
        )
        self._orders[order_id] = updated
        return updated

    def update_order_terms(
        self,
        order_id: UUID,
        *,
        quantity: Decimal,
        price: Decimal | None,
    ) -> Order:
        if quantity <= 0:
            raise ValueError("order quantity must be positive")
        current = self._orders[order_id]
        if current.state not in {
            OrderState.CREATED,
            OrderState.RISK_APPROVED,
        }:
            raise InvalidOrderTransitionError(
                "order terms cannot change after submission"
            )
        updated = Order(
            order_id=current.order_id,
            intent_id=current.intent_id,
            client_order_id=current.client_order_id,
            symbol=current.symbol,
            side=current.side,
            order_type=current.order_type,
            quantity=quantity,
            price=price,
            state=current.state,
            created_timestamp_ns=current.created_timestamp_ns,
            venue=current.venue,
        )
        self._orders[order_id] = updated
        return updated

    def transition(
        self,
        order_id: UUID,
        new_state: OrderState,
        timestamp_ns: int,
        cumulative_filled_quantity: Decimal = Decimal(0),
        average_fill_price: Decimal | None = None,
        reason: str | None = None,
    ) -> Order:
        current = self._orders[order_id]
        if new_state not in VALID_TRANSITIONS[current.state]:
            raise InvalidOrderTransitionError(
                f"cannot transition {current.state.value} to {new_state.value}"
            )
        updated = Order(
            order_id=current.order_id,
            intent_id=current.intent_id,
            client_order_id=current.client_order_id,
            symbol=current.symbol,
            side=current.side,
            order_type=current.order_type,
            quantity=current.quantity,
            price=current.price,
            state=new_state,
            created_timestamp_ns=current.created_timestamp_ns,
            venue=current.venue,
        )
        self._orders[order_id] = updated
        self._updates.append(
            OrderUpdate(
                order_id=order_id,
                state=new_state,
                cumulative_filled_quantity=cumulative_filled_quantity,
                average_fill_price=average_fill_price,
                reason=reason,
                update_timestamp_ns=timestamp_ns,
            )
        )
        return updated
