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

# Phase A: the venue latency specs and presets moved here from new_backtest/config.py (which re-exports them until Phase F).
BYBIT = "bybit"
BINANCE = "binance"


# ── Latency ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class VenueLatencySpec:
    """One-way latencies in milliseconds.

    feed_ms      market data: exchange match -> our process sees it
    entry_ms     our order send -> exchange matching engine accepts it
    cancel_ms    our cancel send -> exchange applies it
    jitter_ms    std-dev of the lognormal around the mean
    """
    feed_ms: float
    entry_ms: float
    cancel_ms: float
    jitter_ms: float


# You cannot be co-located with both venues at once. Bybit matching runs out of
# AWS ap-southeast-1 (Singapore); Binance out of ap-northeast-1 (Tokyo).
# Tokyo <-> Singapore one-way is ~35ms. That asymmetry is the single biggest
# driver of cross-exchange arb viability, so it is modelled explicitly.
LATENCY_PRESETS: dict[str, dict[str, VenueLatencySpec]] = {
    # Co-located Singapore: fast to Bybit, slow to Binance.
    "singapore": {
        BYBIT:   VenueLatencySpec(feed_ms=2.0,  entry_ms=6.0,  cancel_ms=6.0,  jitter_ms=2.5),
        BINANCE: VenueLatencySpec(feed_ms=36.0, entry_ms=40.0, cancel_ms=40.0, jitter_ms=8.0),
    },
    # Co-located Tokyo: fast to Binance, slow to Bybit.
    "tokyo": {
        BYBIT:   VenueLatencySpec(feed_ms=36.0, entry_ms=40.0, cancel_ms=40.0, jitter_ms=8.0),
        BINANCE: VenueLatencySpec(feed_ms=2.0,  entry_ms=6.0,  cancel_ms=6.0,  jitter_ms=2.5),
    },
    # Midpoint host (e.g. Hong Kong) — symmetric but slower to both.
    "neutral": {
        BYBIT:   VenueLatencySpec(feed_ms=18.0, entry_ms=22.0, cancel_ms=22.0, jitter_ms=6.0),
        BINANCE: VenueLatencySpec(feed_ms=20.0, entry_ms=24.0, cancel_ms=24.0, jitter_ms=6.0),
    },
    # Physically impossible; use only as an upper bound in sensitivity sweeps.
    "colocated_both": {
        BYBIT:   VenueLatencySpec(feed_ms=1.0, entry_ms=3.0, cancel_ms=3.0, jitter_ms=1.0),
        BINANCE: VenueLatencySpec(feed_ms=1.0, entry_ms=3.0, cancel_ms=3.0, jitter_ms=1.0),
    },
}

DEFAULT_LATENCY_PRESET = "singapore"



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
    # Hedge-failure injection (hedge_faults.py); all defaults mean an ordinary order.
    kind: str = "order"        # "order" | "recover" (a strategy-side timer: failure detected / retry due; never reaches an exchange)
    is_hedge: bool = False
    role: str = ""             # "" | "unwind"
    attempt: int = 0
    fault: str = ""            # "" | "reject" | "partial"
    partial_fraction: float = 1.0
    ghost: bool = False        # delivered after the strategy gave up on it
    origin_id: int = 0         # order_id of the first hedge of a retry/unwind chain (0 = this order is its own origin)
    intent_id: str = ""        # canonical execution layer: the intent this child order belongs to


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

    def peek_release_ts(self) -> float | None:
        """Earliest release time still in flight (cancelled entries skipped), or None. For the timer wheel."""
        while self._heap and self._heap[0][2].order_id in self._cancelled:
            _, _, order = heapq.heappop(self._heap)
            self._live -= 1
            self._cancelled.discard(order.order_id)
        return self._heap[0][0] if self._heap else None

    def in_flight(self) -> Iterator[InFlightOrder]:
        """Orders sent and not yet landed or cancelled (no particular order)."""
        return (o for _, _, o in self._heap if o.order_id not in self._cancelled)

    def in_flight_count(self) -> int:
        """Number of orders still in flight (in_flight() is a generator; the breaker needs the count)."""
        return sum(1 for _ in self.in_flight())

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
    role: str = ""


@dataclass
class _DecomposedMarkout:
    target_ts: float
    horizon_s: float
    venue: str
    side: str
    price: float
    qty: float
    role: str
    mid0: float
    fee: float
    tags: dict
    fill_ts: float


class MarkoutTracker:
    """Forward price markouts per fill, evaluated against the venue mid.

    Streaming rather than batch: pending markouts sit in a heap and resolve as
    the replay passes their horizon, so memory stays flat over a 10-day run.
    Sign convention: positive = price moved in our favour after the fill.

    `decompose=True` (CR-B, market-maker programme; default False, no arb path sets it) splits each fill into captured / adverse / fee
    at exact horizons. It has its own record and resolve path (`record_fill(mid_at_fill=...)`, `resolve_asof`), and `resolve` refuses.
    Sign convention (bps, positive = good for us; s = +1 buy, -1 sell; m0 = mid_at_fill, the venue mid on the exchange clock just
    before the event that caused the fill):
      captured  = s * (m0 - price) / m0 * 1e4
      adverse_h = s * (m_h - m0) / m0 * 1e4        m_h = the venue mid as of fill_ts + h
      fee       = -fee_paid / notional * 1e4       a maker rebate is positive
      net_h     = captured + adverse_h + fee
      m_h       = captured + adverse_h              the note's M(h) = C + D (fill price to the horizon mid, before fees; owner 11 Nov)
    The 09 Nov markout note divides captured and adverse by the FILL PRICE p, not by m0; the difference is under 1 % of the markout and the
    denominator is kept as m0 so the market-maker tracker stays bit-equal (recorded 11 Nov, to revisit with the MM side).
    `mid_max_age_s` (default None = off; owner 11 Nov): the age of a mid is the time since the last BOOK MESSAGE of any kind on that venue (the clock
    of the book gate, Q-G1), never the time since the last mid change. It is applied at both ends: `record_fill(mid_at_fill_ts=...)` (the last book
    message before the fill) and `resolve_asof(mid_ts=...)` (the last book message at or before the horizon). A fill whose mid is older than the limit
    leaves all its horizons out with reason "stale_mid_at_fill"; a horizon whose mid is older leaves that row out with reason "stale_mid". The
    caller must say what its timestamps are (`mid_ts_basis="last_book_message"`); a change-only mid path (a mid recorded only when it moves) cannot
    give them, and the rule is REFUSED there (ValueError: not evaluable), never applied by dropping rows. Rows left out, and why, are counted per
    horizon (`excluded_counts`, `support_counts`); rows resolved at the end of the run stay in `rows` flagged at_end and are counted as "at_end".
    Exactness: the caller runs `resolve_asof(now, mids)` BEFORE applying the event stamped `now`, so a markout whose target is
    before `now` is read on the mid that was valid at its target.
    """

    def __init__(self, horizons_s: tuple[float, ...] = (1.0, 5.0, 30.0), decompose: bool = False, mid_max_age_s: float | None = None) -> None:
        if mid_max_age_s is not None and not decompose:
            raise ValueError("mid_max_age_s is for MarkoutTracker(decompose=True) only")
        if mid_max_age_s is not None and mid_max_age_s < 0:
            raise ValueError("mid_max_age_s must not be negative")
        self.horizons_s = horizons_s
        self.decompose = decompose
        self.mid_max_age_s = mid_max_age_s
        self.rows: list[dict] = []            # decompose only: one row per (fill, horizon)
        self.excluded: list[dict] = []        # decompose only: rows left out (stale_mid, no_mid), with their tags; at_end rows stay in `rows`
        self._pending: list[tuple[float, int, _PendingMarkout]] = []
        self._counter = itertools.count()
        self.stats: dict[float, dict[str, float]] = {
            h: {"count": 0.0, "qty": 0.0, "bps_qty_sum": 0.0, "adverse": 0.0}
            for h in horizons_s
        }
        self.by_venue_role: dict[str, dict[float, dict[str, float]]] = {}

    def record_fill(self, ts: float, venue: str, side: str, price: float, qty: float, role: str = "", *,
                    mid_at_fill: float | None = None, fee: float | None = None, tags: dict | None = None,
                    mid_at_fill_ts: float | None = None, mid_ts_basis: str | None = None) -> None:
        if not self.decompose and (mid_at_fill is not None or fee is not None or tags is not None or mid_at_fill_ts is not None or mid_ts_basis is not None):
            raise ValueError("mid_at_fill, fee, tags, mid_at_fill_ts and mid_ts_basis are for MarkoutTracker(decompose=True) only")
        if price <= 0 or qty <= 0:
            return
        if self.decompose:
            if mid_at_fill is None or mid_at_fill <= 0:
                raise ValueError("decompose: record_fill needs the exchange-time venue mid at the fill (mid_at_fill)")
            if self.mid_max_age_s is not None:
                self._check_basis(mid_ts_basis)
                if mid_at_fill_ts is None:
                    raise ValueError("mid_max_age_s is set: record_fill needs mid_at_fill_ts (the last book message before the fill)")
                if ts - mid_at_fill_ts > self.mid_max_age_s:           # the fill's own mid is too old: every horizon of this fill is left out
                    for h in self.horizons_s:
                        self.excluded.append({"reason": "stale_mid_at_fill", "horizon_s": h, "ts": ts, "mid_age_s": ts - mid_at_fill_ts, **dict(tags or {})})
                    return
            for h in self.horizons_s:
                dm = _DecomposedMarkout(ts + h, h, venue, side, price, qty, role, mid_at_fill, fee or 0.0, dict(tags or {}), ts)
                heapq.heappush(self._pending, (dm.target_ts, next(self._counter), dm))
            return
        for h in self.horizons_s:
            pm = _PendingMarkout(ts + h, h, venue, side, price, qty, role)
            heapq.heappush(self._pending, (pm.target_ts, next(self._counter), pm))

    @staticmethod
    def _check_basis(basis: str | None) -> None:
        if basis != "last_book_message":
            raise ValueError(f"mid_max_age_s is not evaluable on mid_ts_basis={basis!r}: the age must be the time since the last book message of any kind "
                             "(the book gate's clock), not since the last mid change; pass mid_ts_basis='last_book_message' or leave the rule off")

    def due(self, now: float) -> bool:
        """True when at least one pending markout has reached its horizon at `now`."""
        return bool(self._pending) and self._pending[0][0] <= now

    def resolve(self, now: float, mids: dict[str, float], finalize: bool = False) -> None:
        if self.decompose:
            raise RuntimeError("MarkoutTracker(decompose=True) resolves with resolve_asof (exact horizons)")
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
            if pm.role:                       # the same number, split by (venue, role); the pooled stats above are untouched
                sv = self.by_venue_role.setdefault(f"{pm.venue}/{pm.role}", {}).setdefault(
                    pm.horizon_s, {"count": 0.0, "qty": 0.0, "bps_qty_sum": 0.0, "adverse": 0.0})
                sv["count"] += 1.0
                sv["qty"] += pm.qty
                sv["bps_qty_sum"] += bps * pm.qty
                sv["adverse"] += float(bps < 0.0)

    def resolve_asof(self, now: float, mids: dict[str, float], finalize: bool = False, mid_ts: dict[str, float] | None = None,
                     mid_ts_basis: str | None = None) -> None:
        """decompose only: resolve every markout whose target is strictly before `now` on `mids` (the state before the event at
        `now`, i.e. the mid as of the target). finalize: resolve the rest on the last mids, flagged at_end.
        `mid_ts`: the time of the last book message of each venue at or before the horizon; required when mid_max_age_s is set (the age at the horizon is
        target - mid_ts), together with mid_ts_basis='last_book_message'."""
        if not self.decompose:
            raise RuntimeError("resolve_asof is for MarkoutTracker(decompose=True); use resolve")
        if self.mid_max_age_s is not None:
            self._check_basis(mid_ts_basis)
        while self._pending and (finalize or self._pending[0][0] < now):
            _, _, pm = heapq.heappop(self._pending)
            m = mids.get(pm.venue, 0.0)
            if m <= 0:
                self.excluded.append({"reason": "no_mid", "horizon_s": pm.horizon_s, "ts": pm.fill_ts, **pm.tags})
                continue
            at_end = bool(finalize and pm.target_ts >= now)
            if self.mid_max_age_s is not None and not at_end:
                if mid_ts is None or pm.venue not in mid_ts:
                    raise ValueError("mid_max_age_s is set: resolve_asof needs mid_ts for every venue it resolves")
                if pm.target_ts - mid_ts[pm.venue] > self.mid_max_age_s:
                    self.excluded.append({"reason": "stale_mid", "horizon_s": pm.horizon_s, "ts": pm.fill_ts, "mid_age_s": pm.target_ts - mid_ts[pm.venue], **pm.tags})
                    continue
            s = 1.0 if pm.side == "buy" else -1.0
            captured = s * (pm.mid0 - pm.price) / pm.mid0 * 10_000.0
            adverse = s * (m - pm.mid0) / pm.mid0 * 10_000.0
            fee = -pm.fee / (pm.price * pm.qty) * 10_000.0
            self.rows.append({"ts": pm.fill_ts, "horizon_s": pm.horizon_s, "venue": pm.venue, "side": pm.side, "price": pm.price,
                              "qty": pm.qty, "role": pm.role, "mid_at_fill": pm.mid0, "mid_at_h": m, "captured_bps": captured,
                              "adverse_bps": adverse, "fee_bps": fee, "net_bps": captured + adverse + fee,
                              "m_bps": captured + adverse, "at_end": at_end, **pm.tags})

    def _group_key(self, r: dict, by: tuple[str, ...]) -> str:
        return "|".join(str(r.get(k)) for k in by) if by else "all"

    def excluded_counts(self) -> dict[str, dict[str, int]]:
        """{"1s": {"stale_mid": n, "no_mid": n, "at_end": n}, ...}: the rows that are not in the statistics, per horizon and reason."""
        out: dict[str, dict[str, int]] = {f"{h:g}s": {} for h in self.horizons_s}
        for r in self.rows:
            if r["at_end"]:
                cell = out.setdefault(f"{r['horizon_s']:g}s", {})
                cell["at_end"] = cell.get("at_end", 0) + 1
        for x in self.excluded:
            cell = out.setdefault(f"{x['horizon_s']:g}s", {})
            cell[x["reason"]] = cell.get(x["reason"], 0) + 1
        return out

    def support_counts(self, by: tuple[str, ...] = ()) -> dict:
        """Per group and horizon: rows kept in the statistics and rows left out by reason (stale_mid, no_mid, at_end). Groups with only excluded rows appear here."""
        out: dict = {}
        def cell(r):
            return out.setdefault(self._group_key(r, by), {}).setdefault(f"{r['horizon_s']:g}s", {"kept": 0, "excluded": {}})
        for r in self.rows:
            c = cell(r)
            if r["at_end"]:
                c["excluded"]["at_end"] = c["excluded"].get("at_end", 0) + 1
            else:
                c["kept"] += 1
        for x in self.excluded:
            c = cell(x)
            c["excluded"][x["reason"]] = c["excluded"].get(x["reason"], 0) + 1
        return out

    def decomposed_summary(self, by: tuple[str, ...] = (), *, block_s: float = 86_400.0, reps: int = 1000, seed: int = 0) -> dict:
        """Mean captured / adverse / fee / net (bps, plain and notional-weighted) per horizon, optionally grouped by tag keys.
        Rows resolved at the end of the run (at_end) are excluded. Added 11 Nov (09 Nov markout note), new keys only: `stats` (per column: n, mean,
        median, p5, p95 and a block-bootstrap 95 % interval of the mean, blocks of `block_s` seconds of fill time, default a day; with fewer than two
        blocks the interval is None and ci_status says insufficient_blocks) and `support` (rows kept and rows left out by reason)."""
        from ..metrics import distribution_stats
        groups: dict = {}
        cols: dict = {}
        for r in self.rows:
            if r["at_end"]:
                continue
            key = self._group_key(r, by)
            h = f"{r['horizon_s']:g}s"
            g = groups.setdefault(key, {}).setdefault(h, {"n": 0, "q": 0.0, "c": 0.0, "a": 0.0, "f": 0.0, "cq": 0.0, "aq": 0.0, "fq": 0.0})
            g["n"] += 1
            g["q"] += r["qty"] * r["price"]
            for k, col in (("c", "captured_bps"), ("a", "adverse_bps"), ("f", "fee_bps")):
                g[k] += r[col]
                g[k + "q"] += r[col] * r["qty"] * r["price"]
            c = cols.setdefault((key, h), {"captured_bps": [], "adverse_bps": [], "fee_bps": [], "net_bps": [], "m_bps": [], "blocks": []})
            for col in ("captured_bps", "adverse_bps", "fee_bps", "net_bps", "m_bps"):
                c[col].append(r[col])
            c["blocks"].append(int(r["ts"] // block_s))
        support = self.support_counts(by)
        out: dict = {}
        for key, per_h in groups.items():
            out[key] = {}
            for h, g in per_h.items():
                n, q = g["n"], g["q"]
                cap, adv, fee = g["c"] / n, g["a"] / n, g["f"] / n
                c = cols[(key, h)]
                out[key][h] = {"n": n, "captured_bps": cap, "adverse_bps": adv, "fee_bps": fee, "net_bps": cap + adv + fee,
                               "notional_weighted_net_bps": (g["cq"] + g["aq"] + g["fq"]) / q if q > 0 else 0.0, "m_bps": cap + adv,
                               "stats": {col: distribution_stats(c[col], c["blocks"], reps=reps, seed=seed) for col in ("captured_bps", "adverse_bps", "fee_bps", "net_bps", "m_bps")},
                               "support": support[key][h]}
        return out

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

    def summary_by_venue_role(self) -> dict[str, dict[str, dict[str, float]]]:
        """{"bybit/maker": {"1s": {...}, ...}, "binance/taker": ...}: same fields as summary(), one block per (venue, maker|taker)."""
        out: dict[str, dict[str, dict[str, float]]] = {}
        for key, per_h in sorted(self.by_venue_role.items()):
            out[key] = {}
            for h, st in sorted(per_h.items()):
                c, q = st["count"], st["qty"]
                out[key][f"{h:g}s"] = {"count": c, "qty_weighted_markout_bps": st["bps_qty_sum"] / q if q > 0 else 0.0,
                                       "adverse_rate": st["adverse"] / c if c > 0 else 0.0}
        return out
