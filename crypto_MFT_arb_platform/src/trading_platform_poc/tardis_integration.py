from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from tardis_dev import Channel, replay

from trading_platform_poc.binance_feed import BinanceNormalizer
from trading_platform_poc.clock import SimulatedClock
from trading_platform_poc.models import MarketEvent


class TardisBinanceSource:
    def __init__(
        self,
        run_id: UUID,
        *,
        exchange: str = "binance",
        from_date: str | datetime,
        to_date: str | datetime,
        symbols: Sequence[str],
        channels: Sequence[str] = ("depth", "trade"),
        api_key: str = "",
        cache_dir: Path = Path(".tardis-cache"),
        replay_fn: Callable[..., AsyncIterator[Any]] = replay,
    ) -> None:
        self._exchange = exchange
        self._from_date = from_date
        self._to_date = to_date
        self._filters = [
            Channel(name=name, symbols=[symbol.lower() for symbol in symbols])
            for name in channels
        ]
        self._api_key = api_key
        self._cache_dir = cache_dir
        self._replay_fn = replay_fn
        self._clock = SimulatedClock()
        self._normalizer = BinanceNormalizer(run_id, self._clock)

    async def events(
        self,
    ) -> AsyncIterator[tuple[dict[str, Any], MarketEvent]]:
        responses = self._replay_fn(
            exchange=self._exchange,
            from_date=self._from_date,
            to_date=self._to_date,
            filters=self._filters,
            api_key=self._api_key,
            cache_dir=str(self._cache_dir),
        )
        async for response in responses:
            if response is None:
                continue
            message = response.message
            if isinstance(message, bytes):
                message = json.loads(message)
            if not isinstance(message, dict):
                continue
            timestamp_ns = _datetime_to_ns(response.local_timestamp)
            self._clock.advance_to(max(timestamp_ns, self._clock.now_ns()))
            event = self._normalizer.normalize(message)
            if event is not None:
                yield message, event


def _datetime_to_ns(value: datetime | bytes) -> int:
    if isinstance(value, bytes):
        value = datetime.fromisoformat(value.decode())
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    seconds = int(value.timestamp())
    return seconds * 1_000_000_000 + value.microsecond * 1_000
