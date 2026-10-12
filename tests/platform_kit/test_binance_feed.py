import asyncio
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import aiohttp
import pytest

import trading_platform_poc.binance_feed as feed_module
from trading_platform_poc.binance_feed import BinanceNormalizer
from trading_platform_poc.binance_feed import BinancePublicFeed
from trading_platform_poc.clock import SimulatedClock
from trading_platform_poc.config import BinanceConfig
from trading_platform_poc.models import BookDelta, EventType, TradeTick
from trading_platform_poc.models import FeedState


RUN_ID = UUID("00000000-0000-0000-0000-000000000001")


def test_normalizes_depth_update() -> None:
    normalizer = BinanceNormalizer(RUN_ID, SimulatedClock(2_000_000_000))
    raw = {
        "stream": "btcusdt@depth@100ms",
        "data": {
            "e": "depthUpdate",
            "E": 1_000,
            "s": "BTCUSDT",
            "U": 10,
            "u": 12,
            "b": [["100.10", "0.25"]],
            "a": [["100.20", "0.40"]],
        },
    }

    event = normalizer.normalize(raw)

    assert event is not None
    assert event.event_type is EventType.BOOK_DELTA
    assert event.exchange_timestamp_ns == 1_000_000_000
    assert isinstance(event.payload, BookDelta)
    assert event.payload.final_update_id == 12
    assert event.payload.bids[0].price == Decimal("100.10")


def test_normalizes_trade() -> None:
    normalizer = BinanceNormalizer(RUN_ID, SimulatedClock(2_000_000_000))
    raw = {
        "data": {
            "e": "trade",
            "E": 1_000,
            "s": "BTCUSDT",
            "t": 42,
            "p": "100.50",
            "q": "0.25",
            "T": 999,
            "m": True,
        }
    }

    event = normalizer.normalize(raw)

    assert event is not None
    assert event.event_type is EventType.TRADE
    assert isinstance(event.payload, TradeTick)
    assert event.payload.trade_id == 42
    assert event.payload.trade_timestamp_ns == 999_000_000


def test_ignores_unknown_event_type() -> None:
    normalizer = BinanceNormalizer(RUN_ID, SimulatedClock())

    assert normalizer.normalize({"data": {"e": "unknown"}}) is None


@pytest.mark.asyncio
async def test_reconnects_after_websocket_disconnect(monkeypatch) -> None:
    attempts = 0
    original_sleep = asyncio.sleep

    class FakeWebSocketContext:
        async def __aenter__(self):
            nonlocal attempts
            attempts += 1
            return self

        async def __aexit__(self, *args):
            return None

        def __aiter__(self):
            if attempts == 1:
                async def empty_messages():
                    if False:
                        yield None

                return empty_messages()
            message = SimpleNamespace(
                type=aiohttp.WSMsgType.TEXT,
                json=lambda: {
                    "data": {
                        "e": "trade",
                        "E": 1,
                        "s": "BTCUSDT",
                        "t": 1,
                        "p": "100",
                        "q": "0.1",
                        "T": 1,
                        "m": False,
                    }
                },
            )

            async def messages():
                yield message

            return messages()

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def ws_connect(self, *args, **kwargs):
            return FakeWebSocketContext()

    async def no_wait(_):
        return None

    monkeypatch.setattr(
        feed_module.aiohttp,
        "ClientSession",
        lambda **_: FakeSession(),
    )
    monkeypatch.setattr(feed_module.asyncio, "sleep", no_wait)
    statuses = []
    feed = BinancePublicFeed(
        BinanceConfig(),
        "BTCUSDT",
        RUN_ID,
        status_handler=lambda state, reason, details: statuses.append(
            state
        ),
    )

    _, event = await anext(feed.events())
    await original_sleep(0)

    assert attempts == 2
    assert event.event_type is EventType.TRADE
    assert FeedState.DISCONNECTED in statuses
    assert statuses[-1] is FeedState.HEALTHY


@pytest.mark.asyncio
async def test_status_handler_failure_is_isolated() -> None:
    loop = asyncio.get_running_loop()
    observed_errors = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(
        lambda _loop, context: observed_errors.append(context)
    )
    feed = BinancePublicFeed(
        BinanceConfig(),
        "BTCUSDT",
        RUN_ID,
        status_handler=lambda *_: (_ for _ in ()).throw(
            RuntimeError("observer failed")
        ),
    )

    feed._notify_status(
        FeedState.DISCONNECTED,
        "test",
        {},
    )
    await asyncio.sleep(0)
    loop.set_exception_handler(previous_handler)

    assert observed_errors


@pytest.mark.asyncio
async def test_invalid_message_isolated_before_next_valid_event(
    monkeypatch,
) -> None:
    class FakeWebSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def __aiter__(self):
            messages = (
                SimpleNamespace(
                    type=aiohttp.WSMsgType.TEXT,
                    json=lambda: {"data": {"e": "trade"}},
                ),
                SimpleNamespace(
                    type=aiohttp.WSMsgType.TEXT,
                    json=lambda: {
                        "data": {
                            "e": "trade",
                            "E": 1,
                            "s": "BTCUSDT",
                            "t": 1,
                            "p": "100",
                            "q": "0.1",
                            "T": 1,
                            "m": False,
                        }
                    },
                ),
            )

            async def iterate():
                for message in messages:
                    yield message

            return iterate()

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def ws_connect(self, *args, **kwargs):
            return FakeWebSocket()

    monkeypatch.setattr(
        feed_module.aiohttp,
        "ClientSession",
        lambda **_: FakeSession(),
    )
    statuses = []
    feed = BinancePublicFeed(
        BinanceConfig(),
        "BTCUSDT",
        RUN_ID,
        status_handler=lambda state, *_: statuses.append(state),
    )

    _, event = await anext(feed.events())
    await asyncio.sleep(0)

    assert event.payload.trade_id == 1
    assert FeedState.INVALID in statuses
