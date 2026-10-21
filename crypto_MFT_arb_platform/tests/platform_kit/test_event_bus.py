from decimal import Decimal
from uuid import uuid4

import pytest

from trading_platform_poc.event_bus import EventBus
from trading_platform_poc.models import EventType, MarketEvent, PriceLevel, TradeTick


@pytest.mark.asyncio
async def test_dispatches_handlers_in_subscription_order() -> None:
    bus = EventBus()
    calls: list[str] = []

    async def first(_: MarketEvent) -> None:
        calls.append("first")

    async def second(_: MarketEvent) -> None:
        calls.append("second")

    bus.subscribe(EventType.TRADE, first)
    bus.subscribe(EventType.TRADE, second)
    event = MarketEvent(
        event_id=uuid4(),
        run_id=uuid4(),
        venue="binance",
        symbol="BTCUSDT",
        event_type=EventType.TRADE,
        exchange_sequence=1,
        exchange_timestamp_ns=100,
        receive_timestamp_ns=110,
        process_timestamp_ns=120,
        schema_version=1,
        payload=TradeTick(
            symbol="BTCUSDT",
            trade_id=1,
            price=Decimal("100"),
            quantity=Decimal("0.1"),
            buyer_is_maker=False,
            trade_timestamp_ns=100,
        ),
    )

    await bus.publish(event)

    assert calls == ["first", "second"]


def test_rejects_duplicate_subscription() -> None:
    bus = EventBus()

    async def handler(_: MarketEvent) -> None:
        return None

    bus.subscribe(EventType.TRADE, handler)

    with pytest.raises(ValueError, match="already subscribed"):
        bus.subscribe(EventType.TRADE, handler)


def test_price_level_uses_decimal_values() -> None:
    level = PriceLevel(price=Decimal("100.10"), quantity=Decimal("0.25"))

    assert level.price * level.quantity == Decimal("25.0250")
