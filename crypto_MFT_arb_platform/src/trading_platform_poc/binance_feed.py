from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from typing import Any
from uuid import UUID, NAMESPACE_URL, uuid5

import aiohttp
import structlog

from trading_platform_poc.clock import Clock, LiveClock
from trading_platform_poc.config import BinanceConfig
from trading_platform_poc.models import (
    BookSnapshot,
    BookDelta,
    EventType,
    FeedState,
    MarketEvent,
    PriceLevel,
    TradeTick,
)


logger = structlog.get_logger()


class BinanceNormalizer:
    def __init__(self, run_id: UUID, clock: Clock | None = None) -> None:
        self._run_id = run_id
        self._clock = clock or LiveClock()

    def normalize(self, raw_message: dict[str, Any]) -> MarketEvent | None:
        data = raw_message.get("data", raw_message)
        event_name = data.get("e")
        receive_timestamp_ns = self._clock.now_ns()
        if event_name == "depthUpdate":
            payload = BookDelta(
                symbol=str(data["s"]),
                first_update_id=int(data["U"]),
                final_update_id=int(data["u"]),
                bids=self._price_levels(data["b"]),
                asks=self._price_levels(data["a"]),
            )
            event_type = EventType.BOOK_DELTA
            sequence = payload.final_update_id
            exchange_timestamp_ns = int(data["E"]) * 1_000_000
        elif event_name == "trade":
            payload = TradeTick(
                symbol=str(data["s"]),
                trade_id=int(data["t"]),
                price=Decimal(data["p"]),
                quantity=Decimal(data["q"]),
                buyer_is_maker=bool(data["m"]),
                trade_timestamp_ns=int(data["T"]) * 1_000_000,
            )
            event_type = EventType.TRADE
            sequence = payload.trade_id
            exchange_timestamp_ns = int(data["E"]) * 1_000_000
        else:
            return None

        event_key = (
            f"binance:{payload.symbol}:{event_type.value}:"
            f"{sequence}:{exchange_timestamp_ns}"
        )
        return MarketEvent(
            event_id=uuid5(NAMESPACE_URL, event_key),
            run_id=self._run_id,
            venue="binance",
            symbol=payload.symbol,
            event_type=event_type,
            exchange_sequence=sequence,
            exchange_timestamp_ns=exchange_timestamp_ns,
            receive_timestamp_ns=receive_timestamp_ns,
            process_timestamp_ns=self._clock.now_ns(),
            schema_version=1,
            payload=payload,
        )

    def normalize_snapshot(
        self,
        raw_snapshot: dict[str, Any],
        symbol: str,
        timestamp_ns: int | None = None,
    ) -> MarketEvent:
        effective_timestamp_ns = timestamp_ns or self._clock.now_ns()
        payload = BookSnapshot(
            symbol=symbol.upper(),
            last_update_id=int(raw_snapshot["lastUpdateId"]),
            bids=self._price_levels(raw_snapshot["bids"]),
            asks=self._price_levels(raw_snapshot["asks"]),
        )
        event_key = (
            f"binance:{payload.symbol}:{EventType.BOOK_SNAPSHOT.value}:"
            f"{payload.last_update_id}"
        )
        return MarketEvent(
            event_id=uuid5(NAMESPACE_URL, event_key),
            run_id=self._run_id,
            venue="binance",
            symbol=payload.symbol,
            event_type=EventType.BOOK_SNAPSHOT,
            exchange_sequence=payload.last_update_id,
            exchange_timestamp_ns=effective_timestamp_ns,
            receive_timestamp_ns=effective_timestamp_ns,
            process_timestamp_ns=effective_timestamp_ns,
            schema_version=1,
            payload=payload,
        )

    @staticmethod
    def _price_levels(levels: list[list[str]]) -> tuple[PriceLevel, ...]:
        return tuple(
            PriceLevel(price=Decimal(price), quantity=Decimal(quantity))
            for price, quantity in levels
        )


class BinancePublicFeed:
    def __init__(
        self,
        config: BinanceConfig,
        symbol: str,
        run_id: UUID,
        status_handler: (
            Callable[[FeedState, str, dict[str, str]], None] | None
        ) = None,
    ) -> None:
        self._config = config
        self._symbol = symbol.lower()
        self._normalizer = BinanceNormalizer(run_id)
        self._status_handler = status_handler

    @property
    def normalizer(self) -> BinanceNormalizer:
        return self._normalizer

    @property
    def stream_url(self) -> str:
        streams = f"{self._symbol}@depth@100ms/{self._symbol}@trade"
        return f"{self._config.websocket_base_url}/stream?streams={streams}"

    async def fetch_snapshot(self) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=10)
        url = f"{self._config.rest_base_url}/api/v3/depth"
        params = {
            "symbol": self._symbol.upper(),
            "limit": self._config.depth_levels,
        }
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                payload = await response.json()
        if not isinstance(payload, dict):
            raise ValueError("Binance depth snapshot must be a JSON object")
        return payload

    async def events(
        self,
    ) -> AsyncIterator[tuple[dict[str, Any], MarketEvent]]:
        reconnect_delay = 1.0
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while True:
                try:
                    self._notify_status(
                        FeedState.CONNECTING,
                        "connecting to Binance public feed",
                        {"symbol": self._symbol.upper()},
                    )
                    logger.info(
                        "binance_feed_connecting",
                        symbol=self._symbol.upper(),
                        url=self.stream_url,
                    )
                    async with session.ws_connect(
                        self.stream_url,
                        heartbeat=15,
                        autoping=True,
                    ) as websocket:
                        reconnect_delay = 1.0
                        logger.info(
                            "binance_feed_connected",
                            symbol=self._symbol.upper(),
                        )
                        self._notify_status(
                            FeedState.HEALTHY,
                            "Binance public feed connected",
                            {"symbol": self._symbol.upper()},
                        )
                        async for message in websocket:
                            if message.type is aiohttp.WSMsgType.TEXT:
                                try:
                                    raw_message = message.json()
                                    event = self._normalizer.normalize(
                                        raw_message
                                    )
                                except (
                                    KeyError,
                                    TypeError,
                                    ValueError,
                                ) as error:
                                    logger.warning(
                                        "binance_feed_invalid_message",
                                        symbol=self._symbol.upper(),
                                        error=str(error),
                                    )
                                    self._notify_status(
                                        FeedState.INVALID,
                                        "Binance feed sent an invalid message",
                                        {"error": str(error)},
                                    )
                                    continue
                                if event is not None:
                                    yield raw_message, event
                            elif message.type in {
                                aiohttp.WSMsgType.ERROR,
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.CLOSE,
                            }:
                                break
                        self._notify_status(
                            FeedState.DISCONNECTED,
                            "Binance WebSocket stream ended",
                            {"symbol": self._symbol.upper()},
                        )
                except asyncio.CancelledError:
                    raise
                except (aiohttp.ClientError, TimeoutError) as error:
                    logger.warning(
                        "binance_feed_disconnected",
                        symbol=self._symbol.upper(),
                        error=str(error),
                        reconnect_delay=reconnect_delay,
                    )
                    self._notify_status(
                        FeedState.DISCONNECTED,
                        "Binance public feed disconnected",
                        {
                            "symbol": self._symbol.upper(),
                            "error": str(error),
                            "reconnect_delay": str(reconnect_delay),
                        },
                    )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 15)

    def _notify_status(
        self,
        state: FeedState,
        reason: str,
        details: dict[str, str],
    ) -> None:
        if self._status_handler is not None:
            asyncio.get_running_loop().call_soon(
                self._status_handler,
                state,
                reason,
                details,
            )
