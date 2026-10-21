from decimal import Decimal
from uuid import UUID

import msgspec

from trading_platform_poc.models import (
    EventType,
    MarketEvent,
    TradeTick,
)


def test_market_event_serialization_is_stable() -> None:
    event = MarketEvent(
        event_id=UUID("00000000-0000-0000-0000-000000000001"),
        run_id=UUID("00000000-0000-0000-0000-000000000002"),
        venue="binance",
        symbol="BTCUSDT",
        event_type=EventType.TRADE,
        exchange_sequence=42,
        exchange_timestamp_ns=1_000,
        receive_timestamp_ns=1_100,
        process_timestamp_ns=1_200,
        schema_version=1,
        payload=TradeTick(
            symbol="BTCUSDT",
            trade_id=42,
            price=Decimal("100.50"),
            quantity=Decimal("0.25"),
            buyer_is_maker=True,
            trade_timestamp_ns=1_000,
        ),
    )

    assert msgspec.json.encode(event) == msgspec.json.encode(event)
