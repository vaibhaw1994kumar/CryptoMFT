"""WP3 timing model: separate timelines, a measured host clock offset, a timer wheel and a dead-feed classifier.

Used by CrossVenueArbBacktester under ArbBacktestConfig.timing (profile perp_perp_maker_taker_v1). legacy_v0 never constructs
these objects, so its results are unchanged.

Timelines (each its own sampled distribution, its own RNG stream, per venue; all one-way, in milliseconds):
    entry          strategy decision -> dispatch -> order arrives at the venue
    ack_report     venue processed the order (accept / post-only reject) -> report reaches the strategy
    fill_report    venue matched (part of) the order -> private execution report reaches the strategy (then hedge dispatch)
    cancel         cancel request sent -> venue applies it
    cancel_report  venue applied the cancel -> cancel report reaches the strategy (slot released)
    feed           public market data: venue event time -> our process receives it (records without a local stamp)
Nothing is charged twice: an arrival is `entry`; what the strategy learns later is `ack_report` or `fill_report`; no round trip is
ever used as an arrival time.

Clock: records keep their raw exchange_ts and local_ts. The host-vs-exchange offset is a MEASURED input with an uncertainty
(ClockConfig), never the median of (local - exchange) — that median contains every venue's delivery delay, which must stay in the
data. FixedOffsetAligner exposes the same interface as latency_model.HostClockAligner (ready / observe / is_plausible / to_host /
report) plus to_exchange(), so the feed and the backtester need no special cases.

Timer wheel: deadlines in integer nanoseconds; `due(before_ns)` yields every timer strictly before a market event's time, so
funding settlements, order lifetimes, cancel deadlines, breaker and UNKNOWN timeouts fire at their own times even when no market
event arrives. Equal-time policy (a timer and a market event with the same timestamp):
    "cancel_first"  (legacy_v0): the timer is processed before the event — a cancel effective at T beats a print stamped T.
    "market_first"  (perp_perp_maker_taker_v1 default): the event is applied first — a print stamped T can still fill an order
                    whose cancel is effective at T, because our cancel time is an estimate and the venue's sequence is unknown.

Dead feed: per venue on ABSOLUTE age of the last update: age > max_book_age -> stale (entry blocked, as before);
age > dead_feed_s -> DEAD (counted separately, breaker informed). Both venues stale at once blocks entry on its own, independent
of their relative skew.
"""
from __future__ import annotations

import heapq
import math
import random
from dataclasses import dataclass, field, replace
from typing import Any, Iterator

NS = 1_000_000_000
TIMELINES = ("entry", "ack_report", "fill_report", "cancel", "cancel_report", "feed")
EQUAL_TIME_POLICIES = ("cancel_first", "market_first")


@dataclass(frozen=True)
class TimelineSpec:
    mean_ms: float
    jitter_ms: float = 0.0           # std-dev of the lognormal around the mean; 0 = fixed
    max_ms: float = 2000.0
    source: str = "ASSUMED"          # ASSUMED | CALIBRATED:<where> | MEASURED:<where>

    def __post_init__(self) -> None:
        if not (self.mean_ms >= 0.0) or not (self.jitter_ms >= 0.0) or not (self.max_ms > 0.0):
            raise ValueError(f"TimelineSpec needs mean_ms >= 0, jitter_ms >= 0, max_ms > 0, got {self}")


@dataclass(frozen=True)
class VenueTimelines:
    entry: TimelineSpec
    ack_report: TimelineSpec
    fill_report: TimelineSpec
    cancel: TimelineSpec
    cancel_report: TimelineSpec
    feed: TimelineSpec

    def get(self, name: str) -> TimelineSpec:
        if name not in TIMELINES:
            raise KeyError(name)
        return getattr(self, name)


@dataclass(frozen=True)
class ClockConfig:
    host_offset_ns: int = 0          # host clock minus exchange clock, measured (ollog clock_sync); sign: positive = host ahead
    uncertainty_ns: int = 0          # half the best server-time round trip at measurement
    plausible_ns: int = 300_000_000  # a recorded local_ts further than this from exchange_ts + offset is a backlogged stamp, not a receive time
    source: str = "ASSUMED"

    def __post_init__(self) -> None:
        if self.uncertainty_ns < 0 or self.plausible_ns <= 0:
            raise ValueError("ClockConfig: uncertainty_ns >= 0 and plausible_ns > 0")


@dataclass(frozen=True)
class TimingConfig:
    venues: dict[str, VenueTimelines]
    clock: ClockConfig = ClockConfig()
    equal_time_policy: str = "market_first"
    dead_feed_s: float = 5.0
    max_book_age_s: float | None = None     # None: the strategy's max_book_age_s is the stale threshold
    seed: int = 11
    timer_wheel: bool = True
    truth_books: bool = True

    def __post_init__(self) -> None:
        if self.equal_time_policy not in EQUAL_TIME_POLICIES:
            raise ValueError(f"equal_time_policy must be one of {EQUAL_TIME_POLICIES}")
        if not (self.dead_feed_s > 0.0):
            raise ValueError("dead_feed_s must be > 0")
        if not self.venues:
            raise ValueError("TimingConfig needs at least one venue")

    @classmethod
    def from_latency_preset(cls, specs: dict[str, Any], **kw) -> "TimingConfig":
        """Start from the legacy per-venue preset (feed_ms / entry_ms / cancel_ms / jitter_ms): entry and cancel keep their
        numbers, feed keeps its number, and the three report timelines start at the feed delay (the legacy engine used the feed
        delay as the report delay). Every timeline is its own stream from here on; the numbers are ASSUMED until calibrated."""
        venues = {}
        for v, s in specs.items():
            feed = TimelineSpec(s.feed_ms, s.jitter_ms, source="ASSUMED:latency_preset")
            venues[v] = VenueTimelines(entry=TimelineSpec(s.entry_ms, s.jitter_ms, source="ASSUMED:latency_preset"), ack_report=feed,
                                       fill_report=feed, cancel=TimelineSpec(s.cancel_ms, s.jitter_ms, source="ASSUMED:latency_preset"),
                                       cancel_report=feed, feed=feed)
        return cls(venues=venues, **kw)

    def describe(self) -> dict:
        return {"clock": {"host_offset_ns": self.clock.host_offset_ns, "uncertainty_ns": self.clock.uncertainty_ns, "source": self.clock.source},
                "equal_time_policy": self.equal_time_policy, "dead_feed_s": self.dead_feed_s, "timer_wheel": self.timer_wheel,
                "truth_books": self.truth_books, "seed": self.seed,
                "venues": {v: {n: {"mean_ms": t.get(n).mean_ms, "jitter_ms": t.get(n).jitter_ms, "source": t.get(n).source} for n in TIMELINES}
                           for v, t in self.venues.items()}}


class _Sampler:
    """Lognormal around the mean with the given std-dev, capped; a fixed delay when jitter is 0. Own RNG stream."""

    def __init__(self, spec: TimelineSpec, seed: int):
        self.spec = spec
        self._rng = random.Random(seed)
        m, s = spec.mean_ms, spec.jitter_ms
        if m > 0 and s > 0:
            var = math.log(1.0 + (s * s) / (m * m))
            self._mu, self._sigma = math.log(m) - var / 2.0, math.sqrt(var)
        else:
            self._mu = self._sigma = None

    def sample_ms(self) -> float:
        if self._mu is None:
            return self.spec.mean_ms
        return min(self.spec.max_ms, self._rng.lognormvariate(self._mu, self._sigma))

    def sample_ns(self) -> int:
        return int(round(self.sample_ms() * 1_000_000))

    def sample(self) -> float:
        """Seconds; the surface latency_model.VenueLatency expects of its feed sampler."""
        return self.sample_ns() / NS


class Timelines:
    """Runtime samplers: one independent RNG stream per (venue, timeline), seeded from the config so a switch on one timeline
    never moves another's draws."""

    def __init__(self, cfg: TimingConfig):
        self.cfg = cfg
        self._s: dict[tuple[str, str], _Sampler] = {}
        for vi, (v, tl) in enumerate(sorted(cfg.venues.items())):
            for ti, name in enumerate(TIMELINES):
                self._s[(v, name)] = _Sampler(tl.get(name), cfg.seed * 1000 + vi * 100 + ti)
        self.draws: dict[tuple[str, str], int] = {k: 0 for k in self._s}

    def sampler(self, venue: str, name: str) -> _Sampler:
        return self._s[(venue, name)]

    def sample_ns(self, venue: str, name: str) -> int:
        self.draws[(venue, name)] += 1
        return self._s[(venue, name)].sample_ns()

    def sample_s(self, venue: str, name: str) -> float:
        return self.sample_ns(venue, name) / NS

    def report(self) -> dict:
        return {f"{v}.{n}": {"mean_ms": self._s[(v, n)].spec.mean_ms, "jitter_ms": self._s[(v, n)].spec.jitter_ms, "draws": self.draws[(v, n)],
                             "source": self._s[(v, n)].spec.source} for (v, n) in sorted(self._s)}


class FixedOffsetAligner:
    """Host-clock aligner with a MEASURED offset. Same surface as latency_model.HostClockAligner. observe() only classifies a
    recorded local_ts as a plausible receive time (within plausible_ns of exchange_ts + offset) and never changes the offset;
    rejected stamps are backlogged collector writes, counted and projected from exchange time by the feed as before."""

    def __init__(self, clock: ClockConfig):
        self.clock = clock
        self.offset_s = clock.host_offset_ns / NS
        self.samples = 0
        self.rejected = 0
        self.warmup_records = 0
        self.reseeds = 0
        self.negative_samples = 0

    @property
    def ready(self) -> bool:
        return True

    def is_plausible(self, exchange_ts: float, local_ts: float) -> bool:
        return abs((local_ts - exchange_ts) - self.offset_s) * NS <= self.clock.plausible_ns

    def observe(self, exchange_ts: float, local_ts: float) -> bool:
        if not self.is_plausible(exchange_ts, local_ts):
            self.rejected += 1
            return False
        self.samples += 1
        if local_ts - exchange_ts < 0:
            self.negative_samples += 1
        return True

    def to_host(self, exchange_ts: float) -> float:
        return exchange_ts + self.offset_s

    def to_exchange(self, host_ts: float) -> float:
        return host_ts - self.offset_s

    def report(self) -> dict[str, float]:
        return {"host_minus_exchange_ms": self.offset_s * 1000.0, "uncertainty_ms": self.clock.uncertainty_ns / 1e6, "source": self.clock.source,
                "accepted_samples": float(self.samples), "rejected_backlogged": float(self.rejected), "warmup_records": 0.0, "reseeds": 0.0,
                "negative_raw_drift_share": self.negative_samples / max(1, self.samples)}


@dataclass(order=True)
class _Timer:
    deadline_ns: int
    seq: int
    kind: str = field(compare=False)
    payload: Any = field(compare=False, default=None)
    cancelled: bool = field(compare=False, default=False)


class TimerWheel:
    """Deadlines in integer ns. `due(before_ns)` yields timers with deadline < before_ns in deadline order (FIFO within a
    deadline); `due_at(ts_ns)` yields those with deadline == ts_ns, so the run loop can apply the equal-time policy explicitly."""

    def __init__(self):
        self._heap: list[_Timer] = []
        self._seq = 0
        self.fired = 0
        self.scheduled = 0

    def schedule(self, deadline_ns: int, kind: str, payload: Any = None) -> _Timer:
        if not isinstance(deadline_ns, int):
            raise TypeError("deadline_ns must be an int (nanoseconds)")
        self._seq += 1
        t = _Timer(deadline_ns, self._seq, kind, payload)
        heapq.heappush(self._heap, t)
        self.scheduled += 1
        return t

    def cancel(self, timer: _Timer) -> None:
        timer.cancelled = True

    def peek_ns(self) -> int | None:
        while self._heap and self._heap[0].cancelled:
            heapq.heappop(self._heap)
        return self._heap[0].deadline_ns if self._heap else None

    def _pop_while(self, pred) -> Iterator[_Timer]:
        while True:
            nxt = self.peek_ns()
            if nxt is None or not pred(nxt):
                return
            t = heapq.heappop(self._heap)
            self.fired += 1
            yield t

    def due(self, before_ns: int) -> Iterator[_Timer]:
        return self._pop_while(lambda d: d < before_ns)

    def due_at(self, ts_ns: int) -> Iterator[_Timer]:
        return self._pop_while(lambda d: d == ts_ns)

    def __len__(self) -> int:
        return sum(1 for t in self._heap if not t.cancelled)


def feed_state(age_s: float, max_book_age_s: float, dead_feed_s: float) -> str:
    """valid | stale | dead on the absolute age of a venue's last update (inf when it never updated)."""
    if age_s > dead_feed_s:
        return "dead"
    if age_s > max_book_age_s:
        return "stale"
    return "valid"


def s_to_ns(seconds: float) -> int:
    return int(round(seconds * NS))
