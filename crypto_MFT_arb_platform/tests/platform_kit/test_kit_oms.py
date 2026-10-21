from decimal import Decimal
from uuid import uuid4

import pytest

from trading_platform_poc.models import OrderIntent, OrderState, OrderType, Side
from trading_platform_poc.oms import (
    InvalidOrderTransitionError,
    OrderManagementSystem,
)


def make_intent() -> OrderIntent:
    return OrderIntent(
        intent_id=uuid4(),
        strategy_id="test",
        symbol="BTCUSDT",
        side=Side.BUY,
        order_type=OrderType.POST_ONLY,
        quantity=Decimal("0.01"),
        price=Decimal("100"),
        created_timestamp_ns=1,
    )


def test_order_lifecycle() -> None:
    oms = OrderManagementSystem()
    order = oms.create(make_intent())

    oms.transition(order.order_id, OrderState.RISK_APPROVED, 2)
    oms.transition(order.order_id, OrderState.SUBMITTED, 3)
    oms.transition(order.order_id, OrderState.ACKNOWLEDGED, 4)
    final = oms.transition(
        order.order_id,
        OrderState.FILLED,
        5,
        cumulative_filled_quantity=Decimal("0.01"),
        average_fill_price=Decimal("100"),
    )

    assert final.state is OrderState.FILLED
    assert oms.open_orders() == ()
    assert len(oms.updates) == 4


def test_rejects_illegal_transition() -> None:
    oms = OrderManagementSystem()
    order = oms.create(make_intent())

    with pytest.raises(InvalidOrderTransitionError):
        oms.transition(order.order_id, OrderState.FILLED, 2)


def test_assigns_venue_immutably_and_preserves_it() -> None:
    oms = OrderManagementSystem()
    original = oms.create(make_intent())

    assigned = oms.assign_venue(original.order_id, " Binance ")
    transitioned = oms.transition(
        original.order_id,
        OrderState.RISK_APPROVED,
        2,
    )

    assert original.venue is None
    assert assigned is not original
    assert assigned.venue == "binance"
    assert transitioned.venue == "binance"
    assert oms.assign_venue(original.order_id, "binance") is transitioned
    with pytest.raises(ValueError, match="already assigned"):
        oms.assign_venue(original.order_id, "okx")
