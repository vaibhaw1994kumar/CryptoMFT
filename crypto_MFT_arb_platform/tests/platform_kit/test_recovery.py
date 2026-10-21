from decimal import Decimal
from uuid import UUID

from trading_platform_poc.binance_feed import BinanceNormalizer
from trading_platform_poc.clock import SimulatedClock
from trading_platform_poc.models import BookDelta, PriceLevel
from trading_platform_poc.order_book import BinanceOrderBook, SequenceGapError
from trading_platform_poc.recovery import fetch_snapshot_event


class FakeFeed:
    def __init__(self) -> None:
        self.normalizer = BinanceNormalizer(
            UUID("00000000-0000-0000-0000-000000000001"),
            SimulatedClock(2_000),
        )

    async def fetch_snapshot(self):
        return {
            "lastUpdateId": 20,
            "bids": [["100", "1"]],
            "asks": [["101", "1"]],
        }


async def test_sequence_gap_recovers_from_new_snapshot_and_continues() -> None:
    book = BinanceOrderBook("BTCUSDT")
    _, first_snapshot = await fetch_snapshot_event(
        FakeFeed(),
        "BTCUSDT",
        1_000,
    )
    book.load_snapshot(first_snapshot.payload, 1_000)

    try:
        book.apply_delta(
            BookDelta("BTCUSDT", 22, 22, (), ()),
            2_000,
        )
    except SequenceGapError:
        _, recovered_snapshot = await fetch_snapshot_event(
            FakeFeed(),
            "BTCUSDT",
            2_000,
        )
        book.load_snapshot(recovered_snapshot.payload, 2_000)

    applied = book.apply_delta(
        BookDelta(
            "BTCUSDT",
            21,
            21,
            (PriceLevel(Decimal("100"), Decimal("2")),),
            (),
        ),
        3_000,
    )

    assert applied is True
    assert book.last_update_id == 21
    assert book.top_bids(1)[0].quantity == Decimal("2")
