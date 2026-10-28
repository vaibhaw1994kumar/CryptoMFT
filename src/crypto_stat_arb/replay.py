from __future__ import annotations

from collections.abc import Iterable

from .models import EventType, MarketEvent
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


RECORDED_ORDER = "recorded processing order (processing_ordinal written by the recording session)"
RECEIVE_TIME_ORDER = ("receive_ts_ns total order across streams; within a book stream, deltas between "
                      "snapshots in venue sequence order (replay.deterministic_order)")


def complete_processing_order(ordinals: list[int | None] | None, count: int) -> bool:
    """True when every event carries a distinct recorded processing ordinal."""
    return (ordinals is not None and len(ordinals) == count and None not in ordinals
            and len(set(ordinals)) == count)


def replay_order(events: list[MarketEvent], ordinals: list[int | None] | None = None
                 ) -> tuple[list[MarketEvent], str]:
    """The order to replay ``events`` in, and which rule produced it.

    A file recorded by a live/paper session carries ``processing_ordinal``: the order
    the session actually applied its events. That order is not always receive-time
    order (a REST snapshot is stamped when its response arrives but applied after
    frames of another venue that arrived later), so a replay that re-sorted by
    receive time would diverge from the session it replays. With a complete set of
    ordinals the recorded order is reproduced exactly; otherwise (older files,
    synthetic data, merged files) ``deterministic_order`` applies.
    """
    if ordinals is not None and complete_processing_order(ordinals, len(events)):
        recorded = [int(ordinal) for ordinal in ordinals if ordinal is not None]   # all present (checked)
        ranked = sorted(range(len(events)), key=recorded.__getitem__)
        return [events[index] for index in ranked], RECORDED_ORDER
    return deterministic_order(events), RECEIVE_TIME_ORDER


def deterministic_order(events: Iterable[MarketEvent]) -> list[MarketEvent]:
    """``ORDERING_RULE`` across streams; venue sequence order within a book stream.

    ``receive_ts_ns`` is the capture host's wall clock, which can step backwards
    (NTP correction). Book deltas of one (venue, instrument) stream between two
    snapshots are re-placed in venue ``sequence`` order on the same time slots,
    so a clock step cannot invert them into a false sequence gap. Timestamps are
    not modified, missing sequences stay missing, and data whose deltas already
    follow the venue sequence is returned in exactly the ``ORDERING_RULE`` order.
    """
    ordered = sorted(events, key=event_order_key)
    result = list(ordered)
    open_slots: dict[tuple[str, str], list[int]] = {}

    def restore(stream: tuple[str, str]) -> None:
        slots = open_slots.pop(stream, [])
        by_sequence = sorted((ordered[i] for i in slots), key=lambda e: e.sequence)   # stable
        for slot, event in zip(slots, by_sequence):
            result[slot] = event

    for index, event in enumerate(ordered):
        stream = (event.venue, event.instrument_id)
        if event.event_type == EventType.BOOK_SNAPSHOT:
            restore(stream)                 # a snapshot starts a new sequence segment
        elif event.event_type == EventType.BOOK_DELTA:
            open_slots.setdefault(stream, []).append(index)
    for stream in list(open_slots):
        restore(stream)
    return result


assert len(ORDERING_RULE) == 7
