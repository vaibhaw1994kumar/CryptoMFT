"""Latency simulation for cross-venue execution.

Derived from AMM/Kraken/altcoin_mm_v1/latency_model.py, rewritten for the
cross-venue case with three changes that matter:

  1. Per-venue profiles. Bybit and Binance sit in different AWS regions, so a
     single scalar latency is wrong in a way that flatters arb PnL. Feed, entry
     and cancel latencies are modelled separately per venue.

  2. Heap-backed pending queue. The original scanned a list on every event;
     at ~10^7-10^8 replay events that dominates runtime.

  3. Monotonic receive-time projection. Sampling i.i.d. feed latency per record
     can reorder two records from the same venue, which never happens on a
     single TCP stream. project_receive_ts() enforces per-venue monotonicity.

Latency is sampled lognormal: exchange RTTs are right-skewed (tight mode, fat
tail from congestion/GC), and lognormal is strictly positive where a gaussian
would produce negative samples that silently become look-ahead.
"""
from __future__ import annotations

import heapq
import itertools
import math
import random
from collections import deque
from dataclasses import dataclass, field
from typing import Iterator

from .config import VenueLatencySpec


# ── Sampler ───────────────────────────────────────────────────────────────────

class LatencySampler:
    """Lognormal (default), gaussian, or fixed latency in SECONDS.

    Parameterised by the mean/std of the *raw* variable, not of its log, so
    config values read as real milliseconds.
    """

    def __init__(
        self,
        mean_ms: float,
        std_ms: float,
        min_ms: float = 0.3,
        max_ms: float = 2000.0,
        model: str = "lognormal",
        seed: int | None = None,
    ) -> None:
        self.mean_ms = float(mean_ms)
        self.std_ms = float(std_ms)
        self.min_ms = float(min_ms)
        self.max_ms = float(max_ms)
        self.model = model
        self._rng = random.Random(seed)

        if model == "lognormal" and self.std_ms > 0 and self.mean_ms > 0:
            sigma2 = math.log(1.0 + (self.std_ms / self.mean_ms) ** 2)
            self._mu = math.log(self.mean_ms) - sigma2 / 2.0
            self._sigma = math.sqrt(sigma2)
        else:
            self._mu = math.log(max(self.mean_ms, 1e-9))
            self._sigma = 0.0

    def sample_ms(self) -> float:
        if self.model == "fixed" or self._sigma <= 0:
            ms = self.mean_ms
        elif self.model == "gaussian":
            ms = self._rng.gauss(self.mean_ms, self.std_ms)
        else:
            ms = math.exp(self._rng.gauss(self._mu, self._sigma))
        return max(self.min_ms, min(self.max_ms, ms))

    def sample(self) -> float:
        """Seconds."""
        return self.sample_ms() / 1000.0

    def percentiles(self, pcts: tuple[float, ...] = (10, 50, 90, 99), n: int = 20_000) -> dict[float, float]:
        samples = sorted(self.sample_ms() for _ in range(n))
        return {p: samples[min(int(p / 100.0 * n), n - 1)] for p in pcts}


# ── Per-venue profile ─────────────────────────────────────────────────────────

class HostClockAligner:
    """Robust running estimate of (host clock - exchange clock), in seconds.

    The collector's NTP clock and the exchange's are not the same clock; in
    data/bybit/ the host sits ~78ms *behind* Bybit at the median. That offset
    is irrelevant to ordering as long as every record is placed on ONE
    timebase. Records carrying local_ts already are. Records without it (Bybit
    snapshots; anything modelled from exchange_ts) must be shifted by this
    offset before adding feed latency, or they land on a different clock than
    their neighbours and replay out of order.

    Why a windowed median and a plausibility band, not an EWMA: measured on
    data/bybit/, local_ts - exchange_ts has a tight core (p1 -195ms, p50 -78ms)
    and a tail at p90 = 119 s, p99 = 615 s. The tail is not clock skew — it is
    the collector stamping records at *write* time while minutes behind under
    backpressure. Those local_ts values are not receive times; used as such
    they would shift one venue forward by minutes against the other and
    fabricate cross-venue dislocations. The median ignores the tail, and
    is_plausible() lets the feed route backlogged records through the modelled
    path instead.

    Shared across venues: it measures the host, not the exchange.
    """

    def __init__(
        self, window: int = 2000, tolerance_s: float = 0.30,
        warmup: int = 2000, hard_cap_s: float = 30.0, reseed_after: int = 3000,
    ) -> None:
        self.window = window
        self.tolerance_s = tolerance_s
        self.warmup = warmup
        self.hard_cap_s = hard_cap_s
        self.reseed_after = reseed_after
        self._warm: list[float] = []
        self._samples: deque[float] = deque()
        self._sorted: list[float] = []
        self._consecutive_rejects = 0
        self.offset_s: float = 0.0
        self.samples = 0
        self.rejected = 0
        self.warmup_records = 0
        self.negative_samples = 0
        self.reseeds = 0

    @property
    def ready(self) -> bool:
        return self.samples > 0

    def is_plausible(self, exchange_ts: float, local_ts: float) -> bool:
        d = local_ts - exchange_ts
        if abs(d) > self.hard_cap_s:
            return False
        if not self.ready:
            return True
        return abs(d - self.offset_s) <= self.tolerance_s

    def _seed(self) -> None:
        # The first records after a collector start are its subscription burst
        # and stamp minutes late; a median over the warm-up buffer lands on the
        # core only if the core dominates it. When it does not, the re-seed rule
        # below catches the mistake within reseed_after records.
        self._sorted = sorted(self._warm)
        self._samples = deque(self._warm)
        self.offset_s = self._sorted[len(self._sorted) // 2]
        self.samples = len(self._warm)
        self._warm = []

    def _reseed(self) -> None:
        self._samples.clear()
        self._sorted = []
        self._warm = []
        self.samples = 0
        self._consecutive_rejects = 0
        self.reseeds += 1

    def observe(self, exchange_ts: float, local_ts: float) -> bool:
        """Fold in one (exchange, host) stamp pair. Returns whether it was
        accepted as a genuine receive time. During warm-up nothing is accepted
        (there is no estimate yet to judge against) and the caller should
        project the record from exchange time; those are counted separately."""
        d = local_ts - exchange_ts
        if abs(d) > self.hard_cap_s:
            self.rejected += 1
            return False

        if not self.ready:
            self._warm.append(d)
            self.warmup_records += 1
            if len(self._warm) >= self.warmup:
                self._seed()
            return False

        if abs(d - self.offset_s) > self.tolerance_s:
            self.rejected += 1
            self._consecutive_rejects += 1
            if self._consecutive_rejects >= self.reseed_after:
                self._reseed()
            return False

        self._consecutive_rejects = 0
        if d < 0:
            self.negative_samples += 1
        import bisect
        self._samples.append(d)
        bisect.insort(self._sorted, d)
        if len(self._samples) > self.window:
            old = self._samples.popleft()
            idx = bisect.bisect_left(self._sorted, old)
            if idx < len(self._sorted) and self._sorted[idx] == old:
                self._sorted.pop(idx)
        self.offset_s = self._sorted[len(self._sorted) // 2]
        self.samples += 1
        return True

    def to_host(self, exchange_ts: float) -> float:
        return exchange_ts + self.offset_s

    def report(self) -> dict[str, float]:
        return {
            "host_minus_exchange_ms": self.offset_s * 1000.0,
            "accepted_samples": float(self.samples),
            "rejected_backlogged": float(self.rejected),
            "warmup_records": float(self.warmup_records),
            "reseeds": float(self.reseeds),
            "negative_raw_drift_share": self.negative_samples / max(1, self.samples),
        }


class VenueLatency:
    """Feed / entry / cancel samplers for one venue, plus receive-time projection."""

    def __init__(
        self, venue: str, spec: VenueLatencySpec, seed: int | None = None,
        aligner: HostClockAligner | None = None,
    ) -> None:
        self.venue = venue
        self.spec = spec
        self.aligner = aligner or HostClockAligner()
        base = 0 if seed is None else seed
        self.feed = LatencySampler(spec.feed_ms, spec.jitter_ms, seed=base + 101)
        self.entry = LatencySampler(spec.entry_ms, spec.jitter_ms, seed=base + 202)
        self.cancel = LatencySampler(spec.cancel_ms, spec.jitter_ms, seed=base + 303)
        self._last_receive_ts = float("-inf")

    def project_receive_ts(self, exchange_ts: float) -> float:
        """Map an exchange timestamp to the host time our process could first
        act on it: shift onto the host clock, add a feed-latency sample, and
        enforce monotonicity (one TCP stream never delivers out of order)."""
        ts = self.aligner.to_host(exchange_ts) + self.feed.sample()
        if ts <= self._last_receive_ts:
            ts = math.nextafter(self._last_receive_ts, math.inf)
        self._last_receive_ts = ts
        return ts

    def observe_receive_ts(self, exchange_ts: float, local_ts: float) -> tuple[float, bool]:
        """Use a recorded local_ts as receive time if it is a plausible one.

        Returns (receive_ts, measured). When the collector was backlogged the
        stamp is rejected and the record is projected from exchange time like
        a record with no local_ts at all; measured=False says which happened.
        """
        if not self.aligner.observe(exchange_ts, local_ts):
            return self.project_receive_ts(exchange_ts), False
        ts = local_ts
        if ts <= self._last_receive_ts:
            ts = math.nextafter(self._last_receive_ts, math.inf)
        self._last_receive_ts = ts
        return ts, True

    def reset(self) -> None:
        self._last_receive_ts = float("-inf")


class LatencyBook:
    """All venues' latency profiles in one object."""

    def __init__(self, specs: dict[str, VenueLatencySpec], seed: int | None = 7) -> None:
        # One aligner for all venues: it estimates the host clock, and there is
        # one host. Venues with measured local_ts feed it; venues without read it.
        self.aligner = HostClockAligner()
        self.venues: dict[str, VenueLatency] = {}
        for i, (name, spec) in enumerate(sorted(specs.items())):
            self.venues[name] = VenueLatency(
                name, spec, seed=None if seed is None else seed + i * 1000,
                aligner=self.aligner,
            )

    def __getitem__(self, venue: str) -> VenueLatency:
        return self.venues[venue]

    def entry_delay(self, venue: str) -> float:
        return self.venues[venue].entry.sample()

    def cancel_delay(self, venue: str) -> float:
        return self.venues[venue].cancel.sample()

    def reset(self) -> None:
        self.aligner = HostClockAligner()
        for v in self.venues.values():
            v.reset()
            v.aligner = self.aligner

    def report(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {"_host_clock": self.aligner.report()}
        for name, v in self.venues.items():
            out[name] = {
                "feed_mean_ms": v.spec.feed_ms,
                "entry_mean_ms": v.spec.entry_ms,
                "cancel_mean_ms": v.spec.cancel_ms,
                "entry_p50_ms": v.entry.percentiles((50,))[50],
                "entry_p99_ms": v.entry.percentiles((99,))[99],
            }
        return out


# ── In-flight order queue ─────────────────────────────────────────────────────

@dataclass
class InFlightOrder:
    """An order between send and arrival at the matching engine.

    `book_at_send` captures what the strategy believed when it fired, so the
    backtester can measure how much of the expected edge evaporated in flight.
    """
    release_ts: float
    order_id: int
    venue: str
    side: str              # "buy" | "sell"
    qty: float
    limit_price: float
    is_taker: bool
    sent_ts: float
    signal_id: int
    expected_price: float = 0.0
    expected_edge_bps: float = 0.0
    tag: str = ""


class PendingOrderQueue:
    """Min-heap of in-flight orders keyed by release_ts.

    Cancellation is lazy: cancelled ids go into a tombstone set and are skipped
    on pop. That keeps enqueue/release at O(log n) instead of the O(n) list scan
    the original used.
    """

    def __init__(self) -> None:
        self._heap: list[tuple[float, int, InFlightOrder]] = []
        self._cancelled: set[int] = set()
        self._counter = itertools.count()
        self._live = 0

    def enqueue(self, order: InFlightOrder) -> None:
        heapq.heappush(self._heap, (order.release_ts, next(self._counter), order))
        self._live += 1

    def release(self, current_ts: float) -> Iterator[InFlightOrder]:
        """Yield every order whose flight time has elapsed, in arrival order."""
        while self._heap and self._heap[0][0] <= current_ts:
            _, _, order = heapq.heappop(self._heap)
            self._live -= 1
            if order.order_id in self._cancelled:
                self._cancelled.discard(order.order_id)
                continue
            yield order

    def maker_legs_in_flight(self) -> int:
        return sum(1 for _, _, o in self._heap if not o.is_taker and o.order_id not in self._cancelled)

    def cancel(self, order_id: int) -> None:
        self._cancelled.add(order_id)

    def cancel_signal(self, signal_id: int) -> int:
        """Cancel every in-flight leg of one signal. Returns count cancelled."""
        n = 0
        for _, _, order in self._heap:
            if order.signal_id == signal_id and order.order_id not in self._cancelled:
                self._cancelled.add(order.order_id)
                n += 1
        return n

    def drain(self) -> Iterator[InFlightOrder]:
        """Release everything regardless of time — used at end of replay."""
        while self._heap:
            _, _, order = heapq.heappop(self._heap)
            self._live -= 1
            if order.order_id in self._cancelled:
                self._cancelled.discard(order.order_id)
                continue
            yield order

    @property
    def pending_count(self) -> int:
        return max(0, self._live - len(self._cancelled))

    def clear(self) -> None:
        self._heap.clear()
        self._cancelled.clear()
        self._live = 0


# ── Markouts ──────────────────────────────────────────────────────────────────

@dataclass
class _PendingMarkout:
    target_ts: float
    horizon_s: float
    venue: str
    side: str
    price: float
    qty: float


class MarkoutTracker:
    """Forward price markouts per fill, evaluated against the venue mid.

    Streaming rather than batch: pending markouts sit in a heap and resolve as
    the replay passes their horizon, so memory stays flat over a 10-day run.
    Sign convention: positive = price moved in our favour after the fill.
    """

    def __init__(self, horizons_s: tuple[float, ...] = (1.0, 5.0, 30.0)) -> None:
        self.horizons_s = horizons_s
        self._pending: list[tuple[float, int, _PendingMarkout]] = []
        self._counter = itertools.count()
        self.stats: dict[float, dict[str, float]] = {
            h: {"count": 0.0, "qty": 0.0, "bps_qty_sum": 0.0, "adverse": 0.0}
            for h in horizons_s
        }

    def record_fill(self, ts: float, venue: str, side: str, price: float, qty: float) -> None:
        if price <= 0 or qty <= 0:
            return
        for h in self.horizons_s:
            pm = _PendingMarkout(ts + h, h, venue, side, price, qty)
            heapq.heappush(self._pending, (pm.target_ts, next(self._counter), pm))

    def resolve(self, now: float, mids: dict[str, float], finalize: bool = False) -> None:
        while self._pending and (finalize or self._pending[0][0] <= now):
            _, _, pm = heapq.heappop(self._pending)
            mid = mids.get(pm.venue, 0.0)
            if mid <= 0:
                continue
            # buy: we profit if mid rose; sell: we profit if mid fell
            sign = 1.0 if pm.side == "buy" else -1.0
            bps = sign * (mid - pm.price) / pm.price * 10_000.0
            st = self.stats[pm.horizon_s]
            st["count"] += 1.0
            st["qty"] += pm.qty
            st["bps_qty_sum"] += bps * pm.qty
            st["adverse"] += float(bps < 0.0)

    def summary(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for h, st in self.stats.items():
            c, q = st["count"], st["qty"]
            out[f"{h:g}s"] = {
                "count": c,
                "qty_weighted_markout_bps": st["bps_qty_sum"] / q if q > 0 else 0.0,
                "adverse_rate": st["adverse"] / c if c > 0 else 0.0,
            }
        return out
