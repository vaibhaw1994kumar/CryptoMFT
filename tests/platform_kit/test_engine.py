from decimal import Decimal
from uuid import UUID

from trading_platform_poc.config import (
    AppConfig,
    PaperConfig,
    RiskConfig,
    StrategyConfig,
)
from trading_platform_poc.data_quality import AnomalyCode
from trading_platform_poc.engine import TradingEngine
from trading_platform_poc.models import (
    BookDelta,
    BookSnapshot,
    EventType,
    MarketEvent,
    OrderState,
    PriceLevel,
    TradeTick,
)


def event(event_type, payload, exchange_ns, receive_ns):
    return MarketEvent(
        event_id=UUID("00000000-0000-0000-0000-000000000001"),
        run_id=UUID("00000000-0000-0000-0000-000000000002"),
        venue="binance",
        symbol="BTCUSDT",
        event_type=event_type,
        exchange_sequence=10,
        exchange_timestamp_ns=exchange_ns,
        receive_timestamp_ns=receive_ns,
        process_timestamp_ns=receive_ns,
        schema_version=1,
        payload=payload,
    )


def test_rejects_strategy_orders_from_delayed_feed() -> None:
    config = AppConfig(
        binance={"stale_after_ms": 100},
        risk=RiskConfig(max_net_position=Decimal("1")),
        strategy=StrategyConfig(
            spread_threshold_bps=Decimal("1"),
            order_notional=Decimal("10"),
            quote_cooldown_ms=0,
        ),
    )
    engine = TradingEngine(config)
    engine.process(
        event(
            EventType.BOOK_SNAPSHOT,
            BookSnapshot(
                "BTCUSDT",
                10,
                (PriceLevel(Decimal("100"), Decimal("1")),),
                (PriceLevel(Decimal("101"), Decimal("1")),),
            ),
            1_000_000_000,
            1_000_000_000,
        )
    )

    engine.process(
        event(
            EventType.BOOK_DELTA,
            BookDelta("BTCUSDT", 11, 11, (), ()),
            1_000_000_000,
            1_500_000_000,
        )
    )

    assert len(engine.opportunities) == 2
    assert {item["reason"] for item in engine.opportunities} == {
        "stale_market_data"
    }


def test_gap_cancels_live_orders_and_blocks_fills_until_snapshot() -> None:
    config = AppConfig(
        paper=PaperConfig(
            acknowledgement_latency_ms=0,
            cancellation_latency_ms=0,
        ),
        risk=RiskConfig(max_net_position=Decimal("1")),
        strategy=StrategyConfig(
            spread_threshold_bps=Decimal("1"),
            order_notional=Decimal("10"),
            quote_cooldown_ms=0,
        ),
    )
    engine = TradingEngine(config)

    assert engine.process(
        event(
            EventType.BOOK_SNAPSHOT,
            BookSnapshot(
                "BTCUSDT",
                10,
                (PriceLevel(Decimal("100"), Decimal("1")),),
                (PriceLevel(Decimal("101"), Decimal("1")),),
            ),
            1_000_000_000,
            1_000_000_000,
        )
    )
    assert engine.process(
        event(
            EventType.BOOK_DELTA,
            BookDelta("BTCUSDT", 11, 11, (), ()),
            2_000_000_000,
            2_000_000_000,
        )
    )
    assert len(engine.oms.open_orders()) == 2
    pnl_before_gap = engine.portfolio.net_realized_pnl
    fills_before_gap = len(engine.portfolio.fills)

    assert not engine.process(
        event(
            EventType.BOOK_DELTA,
            BookDelta("BTCUSDT", 13, 13, (), ()),
            3_000_000_000,
            3_000_000_000,
        )
    )
    assert not engine.market_data_healthy
    assert not engine.book.initialized
    assert not engine.oms.open_orders()
    assert {
        order.state for order in engine.oms.orders
    } == {OrderState.CANCELED}

    assert not engine.process(
        event(
            EventType.TRADE,
            TradeTick(
                "BTCUSDT",
                1,
                Decimal("100"),
                Decimal("1"),
                False,
                4_000_000_000,
            ),
            4_000_000_000,
            4_000_000_000,
        )
    )
    assert len(engine.portfolio.fills) == fills_before_gap
    assert engine.portfolio.net_realized_pnl == pnl_before_gap
    assert engine.portfolio.position == 0

    assert not engine.process(
        event(
            EventType.BOOK_SNAPSHOT,
            BookSnapshot(
                "BTCUSDT",
                12,
                (PriceLevel(Decimal("100"), Decimal("1")),),
                (PriceLevel(Decimal("101"), Decimal("1")),),
            ),
            5_000_000_000,
            5_000_000_000,
        )
    )
    assert not engine.market_data_healthy
    assert not engine.book.initialized

    assert engine.process(
        event(
            EventType.BOOK_SNAPSHOT,
            BookSnapshot(
                "BTCUSDT",
                13,
                (PriceLevel(Decimal("100"), Decimal("1")),),
                (PriceLevel(Decimal("101"), Decimal("1")),),
            ),
            6_000_000_000,
            6_000_000_000,
        )
    )
    assert engine.market_data_healthy
    assert engine.book.initialized
    assert engine.process(
        event(
            EventType.BOOK_DELTA,
            BookDelta("BTCUSDT", 14, 14, (), ()),
            7_000_000_000,
            7_000_000_000,
        )
    )
    assert len(engine.oms.open_orders()) == 2

    assert not engine.process(
        event(
            EventType.BOOK_DELTA,
            BookDelta("BTCUSDT", 16, 16, (), ()),
            8_000_000_000,
            8_000_000_000,
        )
    )
    assert not engine.process(
        event(
            EventType.TRADE,
            TradeTick(
                "BTCUSDT",
                2,
                Decimal("100"),
                Decimal("1"),
                False,
                9_000_000_000,
            ),
            9_000_000_000,
            9_000_000_000,
        )
    )
    assert len(engine.portfolio.fills) == fills_before_gap
    assert engine.portfolio.net_realized_pnl == pnl_before_gap
    assert engine.portfolio.position == 0
    assert (
        engine.data_quality.summary()["by_code"][
            AnomalyCode.SEQUENCE_GAP.value
        ]
        == 2
    )

    assert engine.process(
        event(
            EventType.BOOK_SNAPSHOT,
            BookSnapshot(
                "BTCUSDT",
                16,
                (PriceLevel(Decimal("100"), Decimal("1")),),
                (PriceLevel(Decimal("101"), Decimal("1")),),
            ),
            10_000_000_000,
            10_000_000_000,
        )
    )
    assert engine.market_data_healthy
