from __future__ import annotations

import io
import json
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from trading_platform_poc.book_display import (
    book_state,
    render_book,
    run_book_display,
)
from trading_platform_poc.config import AppConfig
from trading_platform_poc.models import (
    BookDelta,
    BookSnapshot,
    EventType,
    MarketEvent,
    PriceLevel,
)
from trading_platform_poc.order_book import BinanceOrderBook

SYMBOL = "BTCUSDT"
RUN_ID = uuid4()


def levels(*pairs: tuple[str, str]) -> list[PriceLevel]:
    return [
        PriceLevel(price=Decimal(price), quantity=Decimal(quantity))
        for price, quantity in pairs
    ]


def snapshot(last_update_id: int = 100) -> BookSnapshot:
    return BookSnapshot(
        symbol=SYMBOL,
        last_update_id=last_update_id,
        bids=levels(
            ("62967.82", "4.75148"),
            ("62967.50", "0.50000"),
            ("62967.10", "1.25000"),
        ),
        asks=levels(
            ("62967.83", "2.88185"),
            ("62968.10", "1.25000"),
            ("62968.40", "0.75000"),
        ),
    )


def delta(
    first: int,
    final: int,
    *,
    bids: list[PriceLevel] | None = None,
    asks: list[PriceLevel] | None = None,
) -> BookDelta:
    return BookDelta(
        symbol=SYMBOL,
        first_update_id=first,
        final_update_id=final,
        bids=bids if bids is not None else [],
        asks=asks if asks is not None else [],
    )


def event(payload: Any, timestamp_ns: int) -> MarketEvent:
    is_snapshot = isinstance(payload, BookSnapshot)
    return MarketEvent(
        event_id=uuid4(),
        run_id=RUN_ID,
        venue="binance",
        symbol=SYMBOL,
        event_type=(
            EventType.BOOK_SNAPSHOT if is_snapshot else EventType.BOOK_DELTA
        ),
        exchange_sequence=(
            payload.last_update_id if is_snapshot else payload.final_update_id
        ),
        exchange_timestamp_ns=timestamp_ns,
        receive_timestamp_ns=timestamp_ns,
        process_timestamp_ns=timestamp_ns,
        schema_version=1,
        payload=payload,
    )


def loaded_book() -> BinanceOrderBook:
    book = BinanceOrderBook(SYMBOL)
    book.load_snapshot(snapshot(), 1_000_000_000)
    return book


class FakeFeed:
    """Scripted feed with the same surface `run_book_display` consumes."""

    def __init__(
        self,
        events: list[MarketEvent],
        snapshots: list[BookSnapshot],
    ) -> None:
        self._events = events
        self._snapshots = snapshots
        self.snapshot_calls = 0
        self.events_yielded = 0
        self.events_before_first_snapshot: int | None = None

    @property
    def normalizer(self) -> Any:
        return self

    def normalize_snapshot(
        self,
        raw: Any,
        symbol: str,
        timestamp_ns: int | None = None,
    ) -> MarketEvent:
        return event(raw, timestamp_ns or 1_000_000_000)

    async def fetch_snapshot(self) -> Any:
        if self.events_before_first_snapshot is None:
            self.events_before_first_snapshot = self.events_yielded
        index = min(self.snapshot_calls, len(self._snapshots) - 1)
        self.snapshot_calls += 1
        return self._snapshots[index]

    async def events(self) -> Any:
        for market_event in self._events:
            self.events_yielded += 1
            yield {}, market_event


def render(book: BinanceOrderBook, **overrides: Any) -> str:
    kwargs: dict[str, Any] = {
        "depth": 3,
        "elapsed_seconds": 12.3,
        "duration_seconds": 300.0,
        "events": 1234,
        "healthy": True,
        "now_ns": 1_000_000_000,
        "stale_after_ms": 2000,
    }
    kwargs.update(overrides)
    return render_book(book, **kwargs)


def test_render_reports_waiting_before_the_first_snapshot() -> None:
    frame = render(BinanceOrderBook(SYMBOL))
    assert "waiting for the first snapshot" in frame
    assert "mid" not in frame


def test_render_shows_both_sides_in_price_order() -> None:
    frame = render(loaded_book())
    rows = [line for line in frame.splitlines() if "|" in line]
    body = [row for row in rows if "bid qty" not in row]
    assert len(body) == 3
    assert "4.75148" in body[0]
    assert "62,967.82" in body[0]
    assert "62,967.83" in body[0]
    assert "2.88185" in body[0]
    # Bids descend and asks ascend away from the touch.
    assert "62,967.50" in body[1]
    assert "62,968.10" in body[1]


def test_render_keeps_the_half_tick_in_the_mid_price() -> None:
    # 62967.82/62967.83 has a mid of 62967.825; two decimals would lose it.
    frame = render(loaded_book())
    assert "mid 62,967.825" in frame


def test_render_reports_the_spread_in_basis_points() -> None:
    frame = render(loaded_book())
    assert "spread 0.01 (0.0016 bps)" in frame


def test_render_marks_a_stale_book() -> None:
    fresh = render(loaded_book(), now_ns=1_000_000_000)
    assert "book fresh" in fresh
    stale = render(loaded_book(), now_ns=1_000_000_000 + 3_000_000_000)
    assert "book STALE" in stale


def test_render_marks_an_unhealthy_feed() -> None:
    assert "feed HEALTHY" in render(loaded_book(), healthy=True)
    assert "feed UNHEALTHY" in render(loaded_book(), healthy=False)


def test_render_counts_down_the_remaining_time() -> None:
    frame = render(loaded_book(), elapsed_seconds=12.0, duration_seconds=300.0)
    assert "remaining  288.0s" in frame


def test_render_never_reports_negative_remaining_time() -> None:
    frame = render(loaded_book(), elapsed_seconds=400.0, duration_seconds=300.0)
    assert "remaining    0.0s" in frame


def test_render_limits_the_rows_to_the_requested_depth() -> None:
    frame = render(loaded_book(), depth=1)
    assert "62,967.82" in frame
    assert "62,967.50" not in frame


def test_book_state_exposes_machine_readable_top_of_book() -> None:
    state = book_state(
        loaded_book(),
        depth=2,
        events=5,
        healthy=True,
        now_ns=1_000_000_000,
        stale_after_ms=2000,
    )
    assert state["best_bid"] == "62967.82"
    assert state["best_ask"] == "62967.83"
    assert state["mid_price"] == "62967.825"
    assert len(state["bids"]) == 2
    assert len(state["asks"]) == 2
    assert state["initialized"] is True
    assert json.dumps(state)


def test_book_state_reports_an_uninitialized_book() -> None:
    state = book_state(
        BinanceOrderBook(SYMBOL),
        depth=2,
        events=0,
        healthy=False,
        now_ns=0,
        stale_after_ms=2000,
    )
    assert state["initialized"] is False
    assert "best_bid" not in state


@pytest.mark.asyncio
async def test_display_snapshots_only_after_the_stream_is_running() -> None:
    """Binance loses updates if the snapshot is taken before the stream."""
    feed = FakeFeed(
        events=[event(delta(101, 102), 1_000_000_100)],
        snapshots=[snapshot()],
    )
    stream = io.StringIO()
    await run_book_display(
        AppConfig(),
        symbol=SYMBOL,
        depth=3,
        duration_seconds=60.0,
        refresh_seconds=0.0,
        stream=stream,
        feed=feed,
    )
    # The snapshot is fetched in response to the first delta, not before it.
    assert feed.snapshot_calls == 1
    assert feed.events_before_first_snapshot == 1
    assert "62,967.82" in stream.getvalue()


@pytest.mark.asyncio
async def test_display_applies_deltas_to_the_book() -> None:
    feed = FakeFeed(
        events=[
            event(delta(101, 101), 1_000_000_100),
            event(
                delta(102, 102, bids=levels(("62967.82", "9.00000"))),
                1_000_000_200,
            ),
        ],
        snapshots=[snapshot()],
    )
    stream = io.StringIO()
    await run_book_display(
        AppConfig(),
        symbol=SYMBOL,
        depth=3,
        duration_seconds=60.0,
        refresh_seconds=0.0,
        stream=stream,
        feed=feed,
    )
    assert "9" in stream.getvalue()
    assert "4.75148" not in stream.getvalue().split("mid")[-1]


@pytest.mark.asyncio
async def test_display_resynchronizes_after_a_sequence_gap() -> None:
    """A gap must trigger a fresh snapshot, not a silently wrong book."""
    recovery = BookSnapshot(
        symbol=SYMBOL,
        last_update_id=500,
        bids=levels(("62970.00", "1.00000")),
        asks=levels(("62970.50", "1.00000")),
    )
    feed = FakeFeed(
        events=[
            event(delta(101, 101), 1_000_000_100),
            event(delta(400, 401), 1_000_000_200),
        ],
        snapshots=[snapshot(), recovery],
    )
    stream = io.StringIO()
    await run_book_display(
        AppConfig(),
        symbol=SYMBOL,
        depth=3,
        duration_seconds=60.0,
        refresh_seconds=0.0,
        stream=stream,
        feed=feed,
    )
    assert feed.snapshot_calls == 2
    assert "62,970.00" in stream.getvalue()


@pytest.mark.asyncio
async def test_display_stops_at_the_requested_duration() -> None:
    """The client asked for a five-minute window; it must not overrun."""
    events = [event(delta(100 + index, 100 + index), 1_000_000_000 + index)
              for index in range(1, 200)]
    feed = FakeFeed(events=events, snapshots=[snapshot()])
    stream = io.StringIO()
    processed = await run_book_display(
        AppConfig(),
        symbol=SYMBOL,
        depth=3,
        duration_seconds=0.0000001,
        refresh_seconds=0.0,
        stream=stream,
        feed=feed,
    )
    assert processed < len(events)


@pytest.mark.asyncio
async def test_display_emits_json_when_requested() -> None:
    feed = FakeFeed(
        events=[event(delta(101, 101), 1_000_000_100)],
        snapshots=[snapshot()],
    )
    stream = io.StringIO()
    await run_book_display(
        AppConfig(),
        symbol=SYMBOL,
        depth=2,
        duration_seconds=60.0,
        refresh_seconds=0.0,
        as_json=True,
        stream=stream,
        feed=feed,
    )
    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    payloads = [json.loads(line) for line in lines]
    assert payloads[-1]["symbol"] == SYMBOL
    assert payloads[-1]["best_bid"] == "62967.82"


@pytest.mark.asyncio
async def test_display_defaults_to_the_configured_symbol() -> None:
    config = AppConfig()
    feed = FakeFeed(events=[], snapshots=[snapshot()])
    stream = io.StringIO()
    await run_book_display(
        config,
        depth=2,
        duration_seconds=60.0,
        refresh_seconds=0.0,
        stream=stream,
        feed=feed,
    )
    assert config.symbol.upper() in stream.getvalue()
