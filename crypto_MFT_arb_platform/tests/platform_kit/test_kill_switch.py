from decimal import Decimal
from uuid import uuid4

from trading_platform_poc.config import AppConfig
from trading_platform_poc.engine import TradingEngine
from trading_platform_poc.models import (
    BookSnapshot,
    OrderIntent,
    OrderState,
    OrderType,
    PriceLevel,
    Side,
)


def test_kill_switch_cancels_resting_orders_and_rejects_new_orders() -> None:
    engine = TradingEngine(AppConfig())
    engine.book.load_snapshot(
        BookSnapshot(
            "BTCUSDT",
            10,
            (PriceLevel(Decimal("100"), Decimal("1")),),
            (PriceLevel(Decimal("101"), Decimal("1")),),
        ),
        1,
    )
    intent = OrderIntent(
        uuid4(),
        "test",
        "BTCUSDT",
        Side.BUY,
        OrderType.POST_ONLY,
        Decimal("0.001"),
        Decimal("100"),
        1,
    )
    order = engine.oms.create(intent)
    order = engine.oms.transition(
        order.order_id, OrderState.RISK_APPROVED, 1
    )
    order = engine.oms.transition(order.order_id, OrderState.SUBMITTED, 1)
    engine._apply_paper_result(engine.exchange.submit(order, engine.book, 1))

    engine.activate_kill_switch(2)
    order_count = len(engine.oms.orders)
    engine._evaluate_strategy(3, False)

    assert engine.oms.open_orders() == ()
    assert engine.oms.orders[0].state is OrderState.CANCELED
    assert engine.risk.kill_switch_active is True
    assert len(engine.oms.orders) == order_count
