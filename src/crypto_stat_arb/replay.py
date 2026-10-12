from __future__ import annotations

from collections.abc import Iterable

from .models import MarketEvent
from .time_integrity import ORDERING_RULE


def event_order_key(event: MarketEvent) -> tuple[int | str, ...]:
    """Recorded-time total order; never uses wall-clock processing time."""
    return (
        event.receive_ts_ns,
        event.exchange_ts_ns,
        event.source_ts_ns,
        event.venue,
        event.sequence_start,
        event.sequence,
        event.event_id,
    )


def deterministic_order(events: Iterable[MarketEvent]) -> list[MarketEvent]:
    return sorted(
        events,
        key=event_order_key,
    )


assert len(ORDERING_RULE) == 7
