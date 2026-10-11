from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import asdict
import json
from pathlib import Path
from typing import Protocol

from .models import MarketEvent, json_default


CANONICAL_SCHEMA_VERSION = "1.2.0"


class CanonicalEventSink(Protocol):
    def on_event(self, event: MarketEvent) -> None: ...


def validate_canonical_event(event: MarketEvent, *, require_raw_ref: bool = True) -> None:
    """Validate the fields common to every downstream market-data consumer."""
    missing = []
    for name in ("event_id", "venue", "instrument_id", "schema_version", "adapter_version"):
        if not getattr(event, name):
            missing.append(name)
    if require_raw_ref and not event.raw_ref:
        missing.append("raw_ref")
    if missing:
        raise ValueError(f"canonical envelope missing required fields: {', '.join(missing)}")
    if min(event.source_ts_ns, event.exchange_ts_ns, event.receive_ts_ns) < 0:
        raise ValueError("canonical timestamps must be non-negative")
    if event.sequence < 0:
        raise ValueError("canonical sequence must be non-negative")
    if event.sequence_start < 0 or event.sequence_start > event.sequence:
        raise ValueError("canonical sequence_start must be between zero and sequence")


class CanonicalEventDispatcher:
    """Validate once, then pass the same immutable envelope instance to every sink."""

    def __init__(self, sinks: Iterable[CanonicalEventSink], *, require_raw_ref: bool = True):
        self._sinks = tuple(sinks)
        self.require_raw_ref = require_raw_ref
        self.delivered = 0

    def dispatch(self, event: MarketEvent) -> None:
        validate_canonical_event(event, require_raw_ref=self.require_raw_ref)
        for sink in self._sinks:
            sink.on_event(event)
        self.delivered += 1


class CallbackEventSink:
    """Adapter for a paper strategy or other in-process event callback."""

    def __init__(self, callback: Callable[[MarketEvent], None]):
        self._callback = callback

    def on_event(self, event: MarketEvent) -> None:
        self._callback(event)


class CanonicalEventRecorder:
    """JSONL recorder for the exact envelope delivered to the strategy path."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._handle = path.open("w", encoding="utf-8")

    def on_event(self, event: MarketEvent) -> None:
        self._handle.write(json.dumps(asdict(event), default=json_default, sort_keys=True) + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> "CanonicalEventRecorder":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
