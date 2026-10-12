from decimal import Decimal

import pytest

from trading_platform_poc.models import BookDelta, BookSnapshot, PriceLevel
from trading_platform_poc.order_book import (
    BinanceOrderBook,
    InvalidBookError,
    SequenceGapError,
)


def snapshot(last_update_id: int = 10) -> BookSnapshot:
    return BookSnapshot(
        symbol="BTCUSDT",
        last_update_id=last_update_id,
        bids=(
            PriceLevel(Decimal("100"), Decimal("2")),
            PriceLevel(Decimal("99"), Decimal("3")),
        ),
        asks=(
            PriceLevel(Decimal("101"), Decimal("2")),
            PriceLevel(Decimal("102"), Decimal("3")),
        ),
    )


def test_loads_snapshot_and_exposes_market() -> None:
    book = BinanceOrderBook("BTCUSDT")

    book.load_snapshot(snapshot(), 1_000)

    assert book.best_bid == Decimal("100")
    assert book.best_ask == Decimal("101")
    assert book.mid_price == Decimal("100.5")
    assert book.spread_bps() == Decimal("1") / Decimal("100.5") * Decimal(10_000)


def test_applies_bridging_delta_and_removes_zero_quantity() -> None:
    book = BinanceOrderBook("BTCUSDT")
    book.load_snapshot(snapshot(), 1_000)
    delta = BookDelta(
        symbol="BTCUSDT",
        first_update_id=10,
        final_update_id=11,
        bids=(PriceLevel(Decimal("100"), Decimal("0")),),
        asks=(PriceLevel(Decimal("100.5"), Decimal("1")),),
    )

    applied = book.apply_delta(delta, 2_000)

    assert applied is True
    assert book.best_bid == Decimal("99")
    assert book.best_ask == Decimal("100.5")
    assert book.last_update_id == 11


def test_ignores_stale_delta() -> None:
    book = BinanceOrderBook("BTCUSDT")
    book.load_snapshot(snapshot(20), 1_000)
    delta = BookDelta("BTCUSDT", 10, 15, (), ())

    assert book.apply_delta(delta, 2_000) is False
    assert book.last_update_id == 20


def test_duplicate_and_out_of_order_deltas_do_not_change_book() -> None:
    book = BinanceOrderBook("BTCUSDT")
    book.load_snapshot(snapshot(20), 1_000)
    checksum = book.checksum()

    duplicate = BookDelta(
        "BTCUSDT",
        19,
        20,
        (PriceLevel(Decimal("98"), Decimal("9")),),
        (),
    )
    out_of_order = BookDelta(
        "BTCUSDT",
        10,
        18,
        (PriceLevel(Decimal("97"), Decimal("9")),),
        (),
    )

    assert book.apply_delta(duplicate, 2_000) is False
    assert book.apply_delta(out_of_order, 3_000) is False
    assert book.checksum() == checksum


def test_detects_sequence_gap() -> None:
    book = BinanceOrderBook("BTCUSDT")
    book.load_snapshot(snapshot(), 1_000)
    delta = BookDelta("BTCUSDT", 12, 12, (), ())

    with pytest.raises(SequenceGapError, match="expected update 11"):
        book.apply_delta(delta, 2_000)


def test_detects_crossed_book() -> None:
    book = BinanceOrderBook("BTCUSDT")
    crossed = BookSnapshot(
        "BTCUSDT",
        10,
        (PriceLevel(Decimal("101"), Decimal("1")),),
        (PriceLevel(Decimal("100"), Decimal("1")),),
    )

    with pytest.raises(InvalidBookError, match="crossed book"):
        book.load_snapshot(crossed, 1_000)


def test_invalid_snapshot_does_not_replace_previous_valid_state() -> None:
    book = BinanceOrderBook("BTCUSDT")
    book.load_snapshot(snapshot(), 1_000)
    checksum = book.checksum()
    crossed = BookSnapshot(
        "BTCUSDT",
        11,
        (PriceLevel(Decimal("101"), Decimal("1")),),
        (PriceLevel(Decimal("100"), Decimal("1")),),
    )

    with pytest.raises(InvalidBookError):
        book.load_snapshot(crossed, 2_000)

    assert book.last_update_id == 10
    assert book.checksum() == checksum


def test_rejects_duplicate_and_non_finite_levels_atomically() -> None:
    book = BinanceOrderBook("BTCUSDT")
    book.load_snapshot(snapshot(), 1_000)
    checksum = book.checksum()
    duplicate = BookSnapshot(
        "BTCUSDT",
        11,
        (
            PriceLevel(Decimal("100"), Decimal("1")),
            PriceLevel(Decimal("100"), Decimal("2")),
        ),
        (PriceLevel(Decimal("101"), Decimal("1")),),
    )

    with pytest.raises(InvalidBookError, match="duplicate"):
        book.load_snapshot(duplicate, 2_000)
    with pytest.raises(InvalidBookError, match="finite"):
        book.apply_delta(
            BookDelta(
                "BTCUSDT",
                11,
                11,
                (PriceLevel(Decimal("NaN"), Decimal("1")),),
                (),
            ),
            2_000,
        )

    assert book.last_update_id == 10
    assert book.checksum() == checksum


def test_checksum_is_deterministic() -> None:
    first = BinanceOrderBook("BTCUSDT")
    second = BinanceOrderBook("BTCUSDT")
    first.load_snapshot(snapshot(), 1_000)
    second.load_snapshot(snapshot(), 2_000)

    assert first.checksum() == second.checksum()
