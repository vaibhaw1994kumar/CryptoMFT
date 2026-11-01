"""Deterministic multi-venue replay (MR-05, AT-06).

Streams are merged on a total order so the same build, manifest, config and
seed always produce the same event sequence. Data-quality and control events
are first-class members of the stream, not side channels, so a stale feed or a
kill switch is replayable evidence.
"""

from __future__ import annotations

import hashlib
import heapq
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Sequence

from trading_platform_poc.minreq.canonical import (
    CanonicalEvent,
    CanonicalEventType,
    ControlPayload,
    Correlation,
    make_event_id,
)
from trading_platform_poc.minreq.quality import (
    QualityMonitor,
    QualityReport,
    QualityThresholds,
)


class ReplayError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ControlSchedule:
    """A control event injected at a chosen replay timestamp."""

    receive_timestamp_ns: int
    action: str
    venue: str = "*"
    instrument_id: str = "*"
    reason: str = ""
    details: dict[str, str] = field(default_factory=dict)

    def to_event(self) -> CanonicalEvent:
        return CanonicalEvent(
            event_id=make_event_id(
                self.venue,
                self.instrument_id,
                CanonicalEventType.CONTROL,
                None,
                self.receive_timestamp_ns,
                self.action,
            ),
            event_type=CanonicalEventType.CONTROL,
            venue=self.venue,
            instrument_id=self.instrument_id,
            venue_symbol=self.instrument_id,
            exchange_timestamp_ns=self.receive_timestamp_ns,
            receive_timestamp_ns=self.receive_timestamp_ns,
            sequence=None,
            ingest_timestamp_ns=self.receive_timestamp_ns,
            payload=ControlPayload(
                action=self.action, reason=self.reason, details=dict(self.details)
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "receive_timestamp_ns": self.receive_timestamp_ns,
            "action": self.action,
            "venue": self.venue,
            "instrument_id": self.instrument_id,
            "reason": self.reason,
            "details": dict(self.details),
        }


def merge_streams(
    streams: dict[str, Iterable[CanonicalEvent]],
    extra: Iterable[CanonicalEvent] = (),
) -> Iterator[CanonicalEvent]:
    """Stable k-way merge on the canonical ordering key.

    Venue-local order is preserved because each stream is already sorted and
    the ordering key breaks ties by venue, sequence and then event id.
    """

    iterators: list[Iterator[CanonicalEvent]] = [
        iter(sorted(events, key=lambda item: item.ordering_key))
        for _, events in sorted(streams.items())
    ]
    extra_events = sorted(extra, key=lambda item: item.ordering_key)
    if extra_events:
        iterators.append(iter(extra_events))
    yield from heapq.merge(*iterators, key=lambda item: item.ordering_key)


@dataclass(frozen=True, slots=True)
class ReplayResult:
    events: tuple[CanonicalEvent, ...]
    signature: str
    quality: QualityReport
    control_events: int
    data_quality_events: int
    venues: tuple[str, ...]
    instruments: tuple[str, ...]
    first_receive_ns: int
    last_receive_ns: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_count": len(self.events),
            "signature": self.signature,
            "control_events": self.control_events,
            "data_quality_events": self.data_quality_events,
            "venues": list(self.venues),
            "instruments": list(self.instruments),
            "first_receive_ns": self.first_receive_ns,
            "last_receive_ns": self.last_receive_ns,
            "book_hashes": self.quality.book_hashes,
            "approved": self.quality.approved,
            "quality_failures": list(self.quality.failures),
        }


class MultiVenueReplay:
    """Event-driven replay shared by backtest and internal paper mode."""

    def __init__(
        self,
        streams: dict[str, Iterable[CanonicalEvent]],
        *,
        controls: Sequence[ControlSchedule] = (),
        stale_after_ns: int = 2_000_000_000,
        thresholds: QualityThresholds | None = None,
        run_id: str = "",
        strict_sequencing: bool = True,
    ) -> None:
        self._streams = streams
        self._controls = tuple(controls)
        self._monitor = QualityMonitor(
            stale_after_ns=stale_after_ns,
            thresholds=thresholds,
            strict_sequencing=strict_sequencing,
        )
        self._run_id = run_id

    @property
    def monitor(self) -> QualityMonitor:
        return self._monitor

    def events(self) -> Iterator[CanonicalEvent]:
        """Yield the graded, merged stream. Ordering never depends on wall time."""

        control_events = [schedule.to_event() for schedule in self._controls]
        for event in merge_streams(self._streams, control_events):
            if self._run_id:
                event = event.with_correlation(
                    Correlation(
                        run_id=self._run_id, source_event_id=event.event_id
                    )
                )
            if event.event_type in {
                CanonicalEventType.CONTROL,
                CanonicalEventType.DATA_QUALITY,
            }:
                yield event
                continue
            for graded in self._monitor.process(event):
                yield graded

    def run(self) -> ReplayResult:
        collected: list[CanonicalEvent] = []
        digest = hashlib.sha256()
        controls = 0
        quality = 0
        venues: set[str] = set()
        instruments: set[str] = set()
        first = 0
        last = 0
        for event in self.events():
            collected.append(event)
            digest.update(event.event_id.encode("utf-8"))
            digest.update(str(event.receive_timestamp_ns).encode("utf-8"))
            digest.update(event.event_type.value.encode("utf-8"))
            if event.event_type is CanonicalEventType.CONTROL:
                controls += 1
            elif event.event_type is CanonicalEventType.DATA_QUALITY:
                quality += 1
            else:
                venues.add(event.venue)
                instruments.add(event.instrument_id)
            if not first:
                first = event.receive_timestamp_ns
            last = event.receive_timestamp_ns
        return ReplayResult(
            events=tuple(collected),
            signature=digest.hexdigest(),
            quality=self._monitor.report(),
            control_events=controls,
            data_quality_events=quality,
            venues=tuple(sorted(venues)),
            instruments=tuple(sorted(instruments)),
            first_receive_ns=first,
            last_receive_ns=last,
        )


def replay_signature(events: Iterable[CanonicalEvent]) -> str:
    digest = hashlib.sha256()
    for event in events:
        digest.update(event.event_id.encode("utf-8"))
        digest.update(str(event.receive_timestamp_ns).encode("utf-8"))
        digest.update(event.event_type.value.encode("utf-8"))
    return digest.hexdigest()


def assert_no_forward_leakage(events: Sequence[CanonicalEvent]) -> None:
    """Fail loudly if the merged stream ever moves backwards in receive time."""

    previous = -1
    for event in events:
        if event.receive_timestamp_ns < previous:
            raise ReplayError(
                "replay produced an out-of-order event: "
                f"{event.event_id} at {event.receive_timestamp_ns} < {previous}"
            )
        previous = event.receive_timestamp_ns
