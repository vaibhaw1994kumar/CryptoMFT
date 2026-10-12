from __future__ import annotations

from typing import Any

from trading_platform_poc.models import MarketEvent


async def fetch_snapshot_event(
    feed,
    symbol: str,
    timestamp_ns: int | None = None,
) -> tuple[dict[str, Any], MarketEvent]:
    raw_snapshot = await feed.fetch_snapshot()
    return (
        raw_snapshot,
        feed.normalizer.normalize_snapshot(
            raw_snapshot,
            symbol,
            timestamp_ns,
        ),
    )
