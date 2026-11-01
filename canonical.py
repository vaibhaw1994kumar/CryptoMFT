from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import asdict
import json
from pathlib import Path
from typing import Protocol, Union

from .models import MarketEvent, Side, TradeEvent, json_default


CANONICAL_SCHEMA_VERSION = "1.2.0"

CanonicalEvent = Union[MarketEvent, TradeEvent]
TRADE_AGGRESSOR_SIDES = frozenset(side.value for side in Side)


class CanonicalEventSink(Protocol):
    def on_event(self, event: MarketEvent) -> None: ...


def validate_canonical_event(event: CanonicalEvent, *, require_raw_ref: bool = True) -> None:
    """Validate the fields common to every downstream market-data consumer."""
    missing = []
    for name in ("event_id", "venue", "instrument_id", "schema_version", "adapter_version"):
        if not getattr(event, name):
            missing.append(name)
    if isinstance(event, TradeEvent) and not event.trade_id:
        missing.append("trade_id")
    if require_raw_ref and not event.raw_ref:
        missing.append("raw_ref")
    if missing:
        raise ValueError(f"canonical envelope missing required fields: {', '.join(missing)}")
    if min(event.source_ts_ns, event.exchange_ts_ns, event.receive_ts_ns) < 0:
        raise ValueError("canonical timestamps must be non-negative")
    if event.sequence < 0:
        raise ValueError("canonical sequence must be non-negative")
    if isinstance(event, TradeEvent):
        if event.arrival_ordinal < 0:
            raise ValueError("canonical arrival_ordinal must be non-negative")
        if not (event.price.is_finite() and event.price > 0):
            raise ValueError("canonical trade price must be positive")
        if not (event.quantity.is_finite() and event.quantity > 0):
            raise ValueError("canonical trade quantity must be positive")
        if event.aggressor_side not in TRADE_AGGRESSOR_SIDES:
            raise ValueError(f"canonical trade aggressor_side {event.aggressor_side!r} is not BUY/SELL")
        return
    if event.sequence_start < 0 or event.sequence_start > event.sequence:
        raise ValueError("canonical sequence_start must be between zero and sequence")


class CanonicalEventDispatcher:
    """Validate once, then pass the same immutable envelope instance to every sink.

    Book (``MarketEvent``) and trade (``TradeEvent``) envelopes share this
    path. A sink that only understands one kind declares it with
    ``accepts = (MarketEvent,)`` (the default for sinks without the attribute,
    so existing book consumers never see a trade); ``accepts = None`` means
    every canonical event.
    """

    def __init__(self, sinks: Iterable[CanonicalEventSink], *, require_raw_ref: bool = True):
        self._sinks = tuple(sinks)
        self.require_raw_ref = require_raw_ref
        self.delivered = 0
        self.delivered_by_type: dict[str, int] = {}

    def dispatch(self, event: CanonicalEvent) -> None:
        validate_canonical_event(event, require_raw_ref=self.require_raw_ref)
        for sink in self._sinks:
            accepts = getattr(sink, "accepts", (MarketEvent,))
            if accepts is None or isinstance(event, accepts):
                sink.on_event(event)
        self.delivered += 1
        kind = type(event).__name__
        self.delivered_by_type[kind] = self.delivered_by_type.get(kind, 0) + 1


class CallbackEventSink:
    """Adapter for a paper strategy or other in-process event callback."""

    def __init__(
        self,
        callback: Callable[[CanonicalEvent], None],
        *,
        accepts: tuple[type, ...] | None = (MarketEvent,),
    ):
        self._callback = callback
        self.accepts = accepts

    def on_event(self, event: CanonicalEvent) -> None:
        self._callback(event)


class CanonicalEventRecorder:
    """JSONL recorder for the exact envelope delivered to the strategy path.

    Books only by default, so ``canonical_events.jsonl`` stays a pure book
    stream that ``runner.load_events`` can replay. Pass
    ``accepts=(TradeEvent,)`` (or ``None`` for both) to record trades.
    """

    def __init__(self, path: Path, *, accepts: tuple[type, ...] | None = (MarketEvent,)):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.accepts = accepts
        self._handle = path.open("w", encoding="utf-8")
        self._ordinal = 0

    def on_event(self, event: CanonicalEvent) -> None:
        row = event.to_dict() if isinstance(event, TradeEvent) else asdict(event)
        self._ordinal += 1
        row["processing_ordinal"] = self._ordinal   # replay.replay_order reproduces this order
        self._handle.write(json.dumps(row, default=json_default, sort_keys=True) + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> "CanonicalEventRecorder":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
