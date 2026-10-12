"""Live public market-data capture and recording (MR-01, MR-04).

This is the live ingestion entry point for the minimum-requirements layer.
:class:`LiveCapture` merges public WebSocket feeds from several venues
(``venues.PublicFeed``) into one stream of raw records stamped by a single
monotonic clock, and :func:`record_live` persists that stream through the same
idempotent :class:`~trading_platform_poc.minreq.pipeline.Pipeline` a synthetic
dataset uses, so a live capture gets the same raw partitions, canonical
events, quality report, quarantine and manifest, and can be replayed with
``mr-backtest --dataset``.

Venue synchronisation:

* **binance** diff-depth updates start flowing before the REST snapshot that
  anchors them. The first update is forwarded (the book ignores updates that
  precede its first snapshot without flagging a defect), then the snapshot,
  then the live stream; overlapping updates are absorbed by the book's
  sequence rules.
* **okx** and **bybit** send a full snapshot as the first book message after
  subscription, so no REST call is needed.

A dropped connection is retried with bounded exponential backoff. The gap it
leaves is not hidden: the book flags the sequence gap and resynchronises on
the next snapshot, exactly as for a gap inside a session.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Mapping, Sequence

from trading_platform_poc.minreq.pipeline import Pipeline
from trading_platform_poc.minreq.securitymaster import (
    SecurityMaster,
    default_security_master,
)
from trading_platform_poc.minreq.venues import (
    PublicFeed,
    RawRecord,
    venue_symbol_for,
)

Clock = Callable[[], int]


class MonotonicWallClock:
    """Wall-clock nanoseconds that never move backwards.

    Receive timestamps from several venue connections share one clock, and a
    backwards NTP step would otherwise reorder them and trip the feature
    engine's lookahead guard. Regressions are counted, not hidden.
    """

    def __init__(self, source: Clock = time.time_ns) -> None:
        self._source = source
        self._last = 0
        self.regressions = 0

    def __call__(self) -> int:
        now = self._source()
        if now <= self._last:
            if now < self._last:
                self.regressions += 1
            now = self._last + 1
        self._last = now
        return now


FeedFactory = Callable[[str, str, Clock], Any]


def public_feed_factory(venue: str, symbol: str, clock: Clock) -> PublicFeed:
    return PublicFeed(venue, venue_symbol_for(venue, symbol), clock_ns=clock)


@dataclass
class CaptureStats:
    records: dict[str, int] = field(default_factory=dict)
    snapshots: dict[str, int] = field(default_factory=dict)
    connections: dict[str, int] = field(default_factory=dict)
    reconnects: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def bump(self, table: dict[str, int], venue: str) -> None:
        table[venue] = table.get(venue, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": dict(sorted(self.records.items())),
            "snapshots": dict(sorted(self.snapshots.items())),
            "connections": dict(sorted(self.connections.items())),
            "reconnects": dict(sorted(self.reconnects.items())),
            "errors": list(self.errors[-50:]),
        }


class LiveCapture:
    """Merge several venues' public feeds into one receive-ordered stream."""

    def __init__(
        self,
        venues: Sequence[str],
        symbol: str,
        *,
        feed_factory: FeedFactory = public_feed_factory,
        clock: Clock | None = None,
        heartbeat_seconds: float = 0.05,
        reconnect_min_seconds: float = 1.0,
        reconnect_max_seconds: float = 30.0,
        queue_size: int = 100_000,
    ) -> None:
        if not venues:
            raise ValueError("live capture needs at least one venue")
        self.venues = tuple(venue.lower() for venue in venues)
        self.symbol = symbol.upper()
        self.feed_factory = feed_factory
        self.clock = clock or MonotonicWallClock()
        self.heartbeat_seconds = heartbeat_seconds
        self.reconnect_min_seconds = reconnect_min_seconds
        self.reconnect_max_seconds = reconnect_max_seconds
        self.queue_size = queue_size
        self.stats = CaptureStats()

    async def stream(
        self,
        *,
        duration_seconds: float | None = None,
        max_records: int | None = None,
    ) -> AsyncIterator[RawRecord | None]:
        """Yield raw records, or ``None`` as a heartbeat when the feeds are quiet.

        Heartbeats let a consumer advance time (order timeouts, residual
        limits, stale-feed detection) even when no market data arrives.
        """

        queue: asyncio.Queue[RawRecord | BaseException] = asyncio.Queue(
            maxsize=self.queue_size
        )
        stop = asyncio.Event()
        tasks = [
            asyncio.create_task(self._pump(venue, queue, stop), name=f"capture-{venue}")
            for venue in self.venues
        ]
        loop = asyncio.get_running_loop()
        deadline = None if duration_seconds is None else loop.time() + duration_seconds
        produced = 0
        try:
            while True:
                timeout = self.heartbeat_seconds
                if deadline is not None:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        return
                    timeout = min(timeout, remaining)
                if all(task.done() for task in tasks) and queue.empty():
                    return
                try:
                    item = await asyncio.wait_for(queue.get(), timeout)
                except asyncio.TimeoutError:
                    yield None
                    continue
                if isinstance(item, BaseException):
                    raise item
                self.stats.bump(self.stats.records, item.venue)
                yield item
                produced += 1
                if max_records is not None and produced >= max_records:
                    return
        finally:
            stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _pump(
        self,
        venue: str,
        queue: asyncio.Queue[RawRecord | BaseException],
        stop: asyncio.Event,
    ) -> None:
        delay = self.reconnect_min_seconds
        while not stop.is_set():
            feed = self.feed_factory(venue, self.symbol, self.clock)
            self.stats.bump(self.stats.connections, venue)
            try:
                if venue == "binance":
                    await self._pump_binance(venue, feed, queue)
                else:
                    snapshot = await feed.snapshot()
                    if snapshot is not None:
                        self.stats.bump(self.stats.snapshots, venue)
                        await queue.put(snapshot)
                    async for record in feed.stream():
                        await queue.put(record)
                        delay = self.reconnect_min_seconds
                if getattr(feed, "finite", False):
                    return
            except asyncio.CancelledError:
                raise
            except Exception as error:  # a venue failure must not stop the others
                self.stats.errors.append(f"{venue}: {type(error).__name__}: {error}")
            if stop.is_set():
                return
            self.stats.bump(self.stats.reconnects, venue)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.reconnect_max_seconds)

    async def _pump_binance(
        self,
        venue: str,
        feed: Any,
        queue: asyncio.Queue[RawRecord | BaseException],
    ) -> None:
        iterator = feed.stream().__aiter__()
        try:
            first = await iterator.__anext__()
        except StopAsyncIteration:
            return
        await queue.put(first)
        snapshot = await feed.snapshot()
        if snapshot is not None:
            self.stats.bump(self.stats.snapshots, venue)
            await queue.put(snapshot)
        async for record in iterator:
            await queue.put(record)


async def record_live(
    dataset_root: str | Path,
    *,
    venues: Sequence[str],
    symbol: str,
    duration_seconds: float | None = None,
    max_records: int | None = None,
    security_master: SecurityMaster | None = None,
    feed_factory: FeedFactory = public_feed_factory,
    clock: Clock | None = None,
    flush_every: int = 500,
) -> dict[str, Any]:
    """Capture live public data and publish it as an approved-or-not dataset."""

    master = security_master or default_security_master()
    pipeline = Pipeline(Path(dataset_root), master)
    capture = LiveCapture(venues, symbol, feed_factory=feed_factory, clock=clock)
    batch: list[RawRecord] = []
    async for record in capture.stream(
        duration_seconds=duration_seconds, max_records=max_records
    ):
        if record is None:
            continue
        batch.append(record)
        if len(batch) >= flush_every:
            pipeline.ingest(batch)
            batch = []
    if batch:
        pipeline.ingest(batch)
    manifest: Mapping[str, Any] = (
        pipeline.run() if pipeline.ingestor.checkpoint.records_written else {}
    )
    return {"manifest": dict(manifest), "capture": capture.stats.to_dict()}
