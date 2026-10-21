"""Look-ahead-free merge of two venue streams.

This is the single most important correctness property in a cross-exchange arb
backtester, and the usual place these backtests silently lie.

The wrong way (what naive backtests do): merge both venues on `exchange_ts`.
That makes a Binance print visible at the instant Binance matched it. In reality
the packet still has to cross the network. If our host sits in Singapore, a
Binance (Tokyo) event is not actionable for ~36ms. Merging on exchange_ts hands
the strategy 36ms of free foresight on one leg of the trade — which is exactly
the quantity the entire strategy is trying to earn. The result is a backtest
that prints enormous, entirely fictional profit.

The right way: merge on *receive time* — when our process could first have seen
the record.

  * When the collector recorded `local_ts`, that is ground truth for what our
    machine actually saw, and it is used directly. Bybit book_events and trades
    in data/bybit/ carry it, so those replay on measured reality.

  * Otherwise receive_ts = host_clock(exchange_ts) + sampled feed latency. The
    host_clock shift matters: the collector's NTP clock runs tens of ms off the
    exchange's, and in data/bybit/ it is *behind* Bybit for most records. A
    record placed on raw exchange time would sit on a different clock from its
    neighbours that carry local_ts, and replay out of order against them. The
    shift is estimated online from every record that has both stamps
    (latency_model.HostClockAligner) and shared across venues, because it
    measures the host, not the exchange.

Either way per-venue monotonicity is enforced (see latency_model.VenueLatency):
one TCP stream never delivers out of order.

Two venues' exchange clocks are also not synchronised with each other. Merging
on exchange_ts would inherit that skew; receive-time merging sidesteps it — the
host clock is the only clock in the ordering.
"""
from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .event_loader import RawEvent, iter_venue_events
from .latency_model import LatencyBook
from .quality_preflight import run_preflight


@dataclass(slots=True)
class TimedEvent:
    """A raw record plus the time our process could first act on it."""
    receive_ts: float
    exchange_ts: float
    venue: str
    pair: str
    kind: str
    payload: dict

    @property
    def feed_latency_ms(self) -> float:
        return (self.receive_ts - self.exchange_ts) * 1000.0


class CrossVenueFeed:
    """K-way heap merge of per-venue streams, ordered by receive time.

    Ties break on (receive_ts, venue, sequence) so a run is bit-for-bit
    reproducible given the same seed.
    """

    def __init__(
        self,
        data_root: Path | str,
        pair: str,
        venues: tuple[str, ...],
        start_day: str,
        end_day: str,
        latency: LatencyBook,
        trust_local_ts: dict[str, bool] | None = None,
        include_deltas: bool = True,
        venue_roots: dict[str, Path | str] | None = None,
        quality_preflight: bool = False,
        quality_report_only: bool = True,
    ) -> None:
        self.data_root = Path(data_root)
        self.pair = pair
        self.venues = venues
        self.start_day = start_day
        self.end_day = end_day
        self.latency = latency
        self.trust_local_ts = trust_local_ts or {v: True for v in venues}
        self.include_deltas = include_deltas
        self.venue_roots = venue_roots or {}
        self._run_quality_preflight = quality_preflight
        self._quality_report_only = quality_report_only

        # Diagnostics — reported at end of run so feed quality is auditable.
        self.counts: dict[str, int] = {v: 0 for v in venues}
        self.local_ts_used: dict[str, int] = {v: 0 for v in venues}
        self.backlogged: dict[str, int] = {v: 0 for v in venues}       # local_ts present but implausible
        self.no_local_ts: dict[str, int] = {v: 0 for v in venues}
        self.loader_stats: dict[str, dict[str, int]] = {v: {} for v in venues}
        # Latency relative to the HOST clock, i.e. after removing clock offset —
        # the number that is actually comparable across venues.
        self.host_latency_sum: dict[str, float] = {v: 0.0 for v in venues}
        # Quality gate reports, keyed by venue; populated when quality_preflight=True
        self.preflight_reports: dict[str, dict] = {}

    def _timed_stream(self, venue: str) -> Iterator[TimedEvent]:
        root = self.venue_roots.get(venue, self.data_root)
        vl = self.latency[venue]
        trust = self.trust_local_ts.get(venue, True)

        for ev in iter_venue_events(
            root, venue, self.pair, self.start_day, self.end_day,
            include_deltas=self.include_deltas,
            stats=self.loader_stats[venue],
        ):
            if trust and ev.local_ts > 0.0:
                recv, measured = vl.observe_receive_ts(ev.exchange_ts, ev.local_ts)
                if measured:
                    self.local_ts_used[venue] += 1
                else:
                    self.backlogged[venue] += 1
            else:
                recv = vl.project_receive_ts(ev.exchange_ts)
                self.no_local_ts[venue] += 1

            self.counts[venue] += 1
            self.host_latency_sum[venue] += recv - vl.aligner.to_host(ev.exchange_ts)
            yield TimedEvent(recv, ev.exchange_ts, venue, ev.pair, ev.kind, ev.payload)

    def __iter__(self) -> Iterator[TimedEvent]:
        if self._run_quality_preflight:
            for venue in self.venues:
                root = self.venue_roots.get(venue, self.data_root)
                self.preflight_reports[venue] = run_preflight(
                    root, venue, self.pair, self.start_day, self.end_day,
                    report_only=self._quality_report_only,
                    scope=f"{venue}/{self.pair}/{self.start_day}:{self.end_day}",
                )

        counter = itertools.count()
        heap: list[tuple[float, str, int, TimedEvent, Iterator[TimedEvent]]] = []

        for venue in self.venues:
            stream = self._timed_stream(venue)
            first = next(stream, None)
            if first is not None:
                heapq.heappush(heap, (first.receive_ts, venue, next(counter), first, stream))

        while heap:
            _, venue, _, ev, stream = heapq.heappop(heap)
            yield ev
            nxt = next(stream, None)
            if nxt is not None:
                heapq.heappush(heap, (nxt.receive_ts, venue, next(counter), nxt, stream))

    def report(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {"_host_clock": self.latency.aligner.report()}
        for v in self.venues:
            n = max(1, self.counts[v])
            out[v] = {
                "events": float(self.counts[v]),
                "measured_local_ts": float(self.local_ts_used[v]),
                "backlogged_local_ts_rejected": float(self.backlogged[v]),
                "no_local_ts_modelled": float(self.no_local_ts[v]),
                "duplicates_dropped": float(self.loader_stats[v].get("duplicates_dropped", 0)),
                "mean_feed_latency_vs_host_ms": self.host_latency_sum[v] / n * 1000.0,
            }
        if self.preflight_reports:
            out["_quality_preflight"] = self.preflight_reports  # type: ignore[assignment]
        return out


def check_coverage(
    data_root: Path | str,
    pair: str,
    venues: tuple[str, ...],
    start_day: str,
    end_day: str,
    venue_roots: dict[str, Path | str] | None = None,
) -> dict[str, list[str]]:
    """Days present per venue. Cross-venue arb needs BOTH venues on the same day;
    a day with only one venue produces no signals and silently shrinks the test
    window, so the backtester reports the intersection up front."""
    from .event_loader import available_days

    roots = venue_roots or {}
    out: dict[str, list[str]] = {}
    for v in venues:
        root = roots.get(v, data_root)
        days = [d for d in available_days(root, v, pair) if start_day <= d <= end_day]
        out[v] = days
    if len(venues) >= 2:
        common = set(out[venues[0]])
        for v in venues[1:]:
            common &= set(out[v])
        out["_intersection"] = sorted(common)
    return out
