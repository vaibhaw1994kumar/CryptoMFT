"""Live level-2 order book display for the command line.

Rendering is deliberately separated from streaming. `render_book` is a pure
function over a `BinanceOrderBook`, so the layout can be tested offline
against a book built from a fixed snapshot, with no network and no clock.
The streaming half reuses `TradingEngine.process`, which already owns
snapshot loading, delta application, sequence-gap recovery and data-quality
validation -- reimplementing those here would duplicate roughly two hundred
lines and let the copy drift from the original.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from decimal import Decimal
from typing import Any
from uuid import uuid4

import structlog

from trading_platform_poc.binance_feed import BinancePublicFeed
from trading_platform_poc.config import AppConfig
from trading_platform_poc.engine import TradingEngine
from trading_platform_poc.models import EventType
from trading_platform_poc.order_book import BinanceOrderBook
from trading_platform_poc.recovery import fetch_snapshot_event

logger = structlog.get_logger()

CLEAR_SCREEN = "\x1b[H\x1b[2J"


def _quantity(value: Decimal) -> str:
    return f"{value:,.8f}".rstrip("0").rstrip(".") or "0"


def _price(value: Decimal) -> str:
    return f"{value:,.2f}"


def _optional_price(value: Decimal | None) -> str:
    return "-" if value is None else _price(value)


def _mid(value: Decimal | None) -> str:
    """Mid prices sit on a half-tick, so two decimals would lose them."""
    if value is None:
        return "-"
    integer, _, fraction = f"{value:,.4f}".partition(".")
    return f"{integer}.{fraction.rstrip('0').ljust(2, '0')}"


def render_book(
    book: BinanceOrderBook,
    *,
    depth: int,
    elapsed_seconds: float,
    duration_seconds: float,
    events: int,
    healthy: bool,
    now_ns: int,
    stale_after_ms: int,
) -> str:
    """Render one frame of the book. Pure: no clock, no I/O."""
    width = 62
    remaining = max(0.0, duration_seconds - elapsed_seconds)
    lines = [
        f"{book.symbol}  level-2 order book",
        (
            f"elapsed {elapsed_seconds:6.1f}s"
            f"   remaining {remaining:6.1f}s"
            f"   events {events:,}"
        ),
        "-" * width,
    ]

    if not book.initialized:
        lines.append("waiting for the first snapshot...".center(width))
        lines.append("-" * width)
        return "\n".join(lines)

    bids = book.top_bids(depth)
    asks = book.top_asks(depth)
    lines.append(
        f"{'bid qty':>16}  {'bid':>12} | {'ask':<12}  {'ask qty':<16}"
    )
    for index in range(max(len(bids), len(asks))):
        if index < len(bids):
            left = (
                f"{_quantity(bids[index].quantity):>16}  "
                f"{_price(bids[index].price):>12}"
            )
        else:
            left = " " * 30
        if index < len(asks):
            right = (
                f"{_price(asks[index].price):<12}  "
                f"{_quantity(asks[index].quantity):<16}"
            )
        else:
            right = ""
        lines.append(f"{left} | {right}".rstrip())

    lines.append("-" * width)
    spread = book.spread
    spread_bps = book.spread_bps()
    lines.append(
        f"mid {_mid(book.mid_price)}"
        f"   spread {_optional_price(spread)}"
        + (
            ""
            if spread_bps is None
            else f" ({spread_bps:.4f} bps)"
        )
    )
    stale = book.is_stale(now_ns, stale_after_ms)
    lines.append(
        f"levels {book.bid_depth:,} x {book.ask_depth:,}"
        f"   feed {'HEALTHY' if healthy else 'UNHEALTHY'}"
        f"   book {'STALE' if stale else 'fresh'}"
    )
    return "\n".join(lines)


def book_state(
    book: BinanceOrderBook,
    *,
    depth: int,
    events: int,
    healthy: bool,
    now_ns: int,
    stale_after_ms: int,
) -> dict[str, Any]:
    """Machine-readable form of the same frame."""
    if not book.initialized:
        return {
            "symbol": book.symbol,
            "initialized": False,
            "events": events,
            "healthy": healthy,
        }
    return {
        "symbol": book.symbol,
        "initialized": True,
        "events": events,
        "healthy": healthy,
        "stale": book.is_stale(now_ns, stale_after_ms),
        "best_bid": str(book.best_bid),
        "best_ask": str(book.best_ask),
        "mid_price": str(book.mid_price),
        "spread": str(book.spread),
        "spread_bps": (
            None
            if book.spread_bps() is None
            else str(book.spread_bps())
        ),
        "bid_levels": book.bid_depth,
        "ask_levels": book.ask_depth,
        "checksum": book.checksum(),
        "bids": [
            {"price": str(level.price), "quantity": str(level.quantity)}
            for level in book.top_bids(depth)
        ],
        "asks": [
            {"price": str(level.price), "quantity": str(level.quantity)}
            for level in book.top_asks(depth)
        ],
    }


async def run_book_display(
    config: AppConfig,
    *,
    symbol: str | None = None,
    depth: int = 10,
    duration_seconds: float = 300.0,
    refresh_seconds: float = 0.5,
    as_json: bool = False,
    stream: Any | None = None,
    feed: Any | None = None,
) -> int:
    """Stream the live book and redraw it until the duration elapses.

    Returns the number of market events processed.
    """
    output = sys.stdout if stream is None else stream
    resolved_symbol = (symbol or config.symbol).upper()
    run_id = uuid4()
    book = BinanceOrderBook(resolved_symbol)
    engine = TradingEngine(config, book=book)
    if feed is None:
        feed = BinancePublicFeed(config.binance, resolved_symbol, run_id)
    stale_after_ms = config.binance.stale_after_ms

    # perf_counter: monotonic and high-resolution everywhere (monotonic_ns ticks
    # every ~15.6 ms on Windows, so a short window never appeared to elapse).
    started_ns = time.perf_counter_ns()
    interactive = (
        not as_json
        and hasattr(output, "isatty")
        and output.isatty()
    )
    last_draw = 0.0
    events = 0
    synchronized = False

    def elapsed() -> float:
        return (time.perf_counter_ns() - started_ns) / 1e9

    def draw() -> None:
        if as_json:
            print(
                json.dumps(
                    book_state(
                        book,
                        depth=depth,
                        events=events,
                        healthy=engine.market_data_healthy,
                        now_ns=time.time_ns(),
                        stale_after_ms=stale_after_ms,
                    ),
                    separators=(",", ":"),
                ),
                file=output,
                flush=True,
            )
            return
        frame = render_book(
            book,
            depth=depth,
            elapsed_seconds=elapsed(),
            duration_seconds=duration_seconds,
            events=events,
            healthy=engine.market_data_healthy,
            now_ns=time.time_ns(),
            stale_after_ms=stale_after_ms,
        )
        if interactive:
            print(CLEAR_SCREEN + frame, file=output, flush=True)
        else:
            print(frame + "\n", file=output, flush=True)

    draw()
    logger.info(
        "book_display_started",
        symbol=resolved_symbol,
        duration_seconds=duration_seconds,
        depth=depth,
    )

    async def resynchronize() -> bool:
        """Fetch a fresh snapshot and load it into the book."""
        _, snapshot_event = await fetch_snapshot_event(
            feed,
            resolved_symbol,
        )
        return engine.process(snapshot_event)

    try:
        async for _raw, event in feed.events():
            events += 1
            now = elapsed()
            if now >= duration_seconds:
                break
            # Binance requires the stream to be running before the
            # snapshot is taken, otherwise the updates that occur during
            # the REST call are lost and the book starts with a gap.
            if (
                not synchronized
                and event.event_type is EventType.BOOK_DELTA
            ):
                synchronized = await resynchronize()
            if synchronized:
                processed = engine.process(event)
                if (
                    event.event_type is EventType.BOOK_DELTA
                    and not processed
                    and not engine.market_data_healthy
                ):
                    synchronized = await resynchronize()
            if elapsed() - last_draw >= refresh_seconds:
                last_draw = elapsed()
                draw()
    except asyncio.CancelledError:
        logger.info("book_display_cancelled", symbol=resolved_symbol)
        raise
    finally:
        draw()
        logger.info(
            "book_display_finished",
            symbol=resolved_symbol,
            events=events,
            elapsed_seconds=round(elapsed(), 3),
        )
    return events
