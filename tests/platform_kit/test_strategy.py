from decimal import Decimal

from trading_platform_poc.config import StrategyConfig
from trading_platform_poc.models import BookSnapshot, PriceLevel, Side
from trading_platform_poc.order_book import BinanceOrderBook
from trading_platform_poc.strategy import SpreadCaptureStrategy


def test_fixed_book_produces_known_two_sided_quotes() -> None:
    book = BinanceOrderBook("BTCUSDT")
    book.load_snapshot(
        BookSnapshot(
            "BTCUSDT",
            1,
            (PriceLevel(Decimal("100"), Decimal("1")),),
            (PriceLevel(Decimal("101"), Decimal("1")),),
        ),
        1,
    )
    strategy = SpreadCaptureStrategy(
        StrategyConfig(
            spread_threshold_bps=Decimal("1"),
            order_notional=Decimal("10"),
            quote_cooldown_ms=0,
        )
    )

    intents = strategy.evaluate(book, 1_000, ())

    assert [intent.side for intent in intents] == [Side.BUY, Side.SELL]
    assert [intent.price for intent in intents] == [
        Decimal("100"),
        Decimal("101"),
    ]
    assert all(intent.strategy_id == "spread_capture" for intent in intents)


def test_requests_cancel_when_spread_falls_below_threshold() -> None:
    book = BinanceOrderBook("BTCUSDT")
    book.load_snapshot(
        BookSnapshot(
            "BTCUSDT",
            1,
            (PriceLevel(Decimal("100"), Decimal("1")),),
            (PriceLevel(Decimal("100.001"), Decimal("1")),),
        ),
        1,
    )
    strategy = SpreadCaptureStrategy(
        StrategyConfig(spread_threshold_bps=Decimal("1"))
    )

    assert strategy.should_cancel(book) is True
