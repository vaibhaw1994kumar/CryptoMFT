"""Hedge-leg failure injection for the cross-venue maker -> taker backtest. Opt-in: without an `ArbBacktestConfig.hedge_faults`
the backtester behaves exactly as before.

The scenario: the maker order fills on venue A, the strategy sends the taker hedge to venue B, and the hedge is rejected,
arrives late, or is sent while the order gateway is down. Whatever the strategy then does about it (`response`) happens in
simulated time with real latencies, so the inventory it carries in between is measured, not assumed away.

Faults (all decided when the hedge is SENT, from a private RNG so the latency stream of the baseline is untouched):
  reject_prob      the hedge reaches the exchange and is rejected; the strategy learns one feed delay later.
  delay_prob/extra_delay_s   extra flight time added to the hedge.
  partial_prob/partial_fraction   only that share of the hedge fills; the remainder fails like a rejection.
  outages          windows in which orders sent to a venue do not work (`Outage`), handled by `outage_policy`.
  outage_on_fill_s  an outage of that length starts on the hedge venue at every maker fill (one conditional sample per
                   fill instead of waiting for random windows to overlap the rare fills).

Outage kinds: "gateway" = order entry and execution reports are down, market data still flows (the strategy keeps quoting:
the dangerous case); "full" = gateway down and the venue's feed dark (the strategy stops evaluating; execution reports of
fills on that venue arrive at reconnect).

Outage policies for an order sent into an outage: "reject" (send fails locally, known at once), "blackhole" (lost silently,
known after `ack_timeout_s`), "queue" (delivered at reconnect, the strategy just waits), "ghost" (the strategy gives up
after `ack_timeout_s`, but the order is delivered at reconnect and can fill after a retry or unwind has already covered it).

Responses once a failure is known: "legacy" (the original code: instant, latency-free flatten on the maker venue),
"none" (carry the inventory), "unwind" (sell it back on the maker venue as a real order with entry latency),
"retry" (resend the hedge up to `max_retries` times, then carry), "retry_unwind" (retry, then unwind).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

OUTAGE_KINDS = ("gateway", "full")
OUTAGE_POLICIES = ("reject", "blackhole", "queue", "ghost")
RESPONSES = ("legacy", "none", "unwind", "retry", "retry_unwind")


@dataclass(frozen=True)
class Outage:
    venue: str
    start_ts: float          # host clock, same scale as event receive_ts
    end_ts: float
    kind: str = "gateway"

    def __post_init__(self) -> None:
        if self.kind not in OUTAGE_KINDS:
            raise ValueError(f"Outage.kind must be one of {OUTAGE_KINDS}, got {self.kind!r}")
        if not (math.isfinite(self.start_ts) and math.isfinite(self.end_ts)) or self.end_ts <= self.start_ts:
            raise ValueError("Outage needs finite start_ts < end_ts")

    def covers(self, ts: float) -> bool:
        return self.start_ts <= ts < self.end_ts


def periodic_outages(venue: str, first_ts: float, last_ts: float, period_s: float, duration_s: float, kind: str = "gateway",
                     offset_s: float = 0.0) -> tuple[Outage, ...]:
    """One outage of `duration_s` at the start of every `period_s` between first_ts and last_ts."""
    if period_s <= duration_s or duration_s <= 0:
        raise ValueError("need 0 < duration_s < period_s")
    out, t = [], first_ts + offset_s
    while t < last_ts:
        out.append(Outage(venue, t, t + duration_s, kind))
        t += period_s
    return tuple(out)


@dataclass(frozen=True)
class DisconnectSpec:
    """Random connection losses on one venue: a Poisson process (`rate_per_day`), plus `guaranteed_per_day` windows at seeded
    uniform times in every UTC day. Duration is drawn from `durations_s` (uniformly, an empirical set) when given, else from a
    lognormal with median `median_s` and log-sd `sigma`. Windows are built from the fault seed, so a seed fixes them exactly."""
    venue: str
    rate_per_day: float = 0.0
    guaranteed_per_day: int = 0
    durations_s: tuple[float, ...] = ()
    median_s: float = 60.0
    sigma: float = 0.5
    kind: str = "gateway"

    def __post_init__(self) -> None:
        if self.kind not in OUTAGE_KINDS:
            raise ValueError(f"DisconnectSpec.kind must be one of {OUTAGE_KINDS}")
        if self.rate_per_day < 0 or self.guaranteed_per_day < 0 or self.median_s <= 0 or self.sigma < 0:
            raise ValueError("DisconnectSpec rates must be >= 0 and median_s > 0")
        if any(d <= 0 for d in self.durations_s):
            raise ValueError("durations_s must be > 0")


class DisconnectGen:
    """Lazily extends one venue's disconnect windows as the replay clock advances (windows are known before they are needed)."""

    def __init__(self, spec: DisconnectSpec, rng) -> None:
        self.spec = spec
        self._rng = rng
        self._horizon: float | None = None       # windows are generated for all of [t0, horizon)
        self._day_done: set[int] = set()
        self.windows: list[Outage] = []

    def _duration(self) -> float:
        sp = self.spec
        if sp.durations_s:
            return sp.durations_s[int(self._rng.random() * len(sp.durations_s)) % len(sp.durations_s)]
        return math.exp(math.log(sp.median_s) + sp.sigma * self._rng.gauss(0.0, 1.0))

    def extend(self, upto_ts: float) -> None:
        """Generate every window starting before `upto_ts`, one UTC day at a time (a day's windows depend only on the seed and the day)."""
        day = int(upto_ts // 86400.0)
        first = day if self._horizon is None else int(self._horizon // 86400.0) + 1
        for d in range(first, day + 1):
            if d in self._day_done:
                continue
            self._day_done.add(d)
            t0 = d * 86400.0
            starts = [t0 + self._rng.random() * 86400.0 for _ in range(self.spec.guaranteed_per_day)]
            if self.spec.rate_per_day > 0:
                t = t0
                while True:
                    t += self._rng.expovariate(self.spec.rate_per_day / 86400.0)
                    if t >= t0 + 86400.0:
                        break
                    starts.append(t)
            for st in sorted(starts):
                self.windows.append(Outage(self.spec.venue, st, st + self._duration(), self.spec.kind))
        self._horizon = (day + 1) * 86400.0

    def active(self, ts: float) -> Outage | None:
        self.extend(ts)
        best = None
        for w in self.windows:
            if w.covers(ts) and (best is None or w.end_ts > best.end_ts):
                best = w
        return best


@dataclass
class HedgeFaultConfig:
    seed: int = 11
    reject_prob: float = 0.0
    delay_prob: float = 0.0
    extra_delay_s: float = 0.0
    partial_prob: float = 0.0
    partial_fraction: float = 0.5
    outages: tuple[Outage, ...] = ()
    outage_on_fill_s: float = 0.0
    outage_on_fill_kind: str = "gateway"
    outage_policy: str = "blackhole"
    ack_timeout_s: float = 2.0          # how long the strategy waits for an unacknowledged order before it treats it as lost
    local_reject_s: float = 0.0         # a send that fails locally is known after this long
    response: str = "legacy"
    max_retries: int = 2
    retry_backoff_s: float = 0.25
    entry_guard: bool = False           # the strategy knows a venue's gateway is down and does not open new signals involving it
    # ── Strategy-level fault layer (Task 5). All defaults are inert, and every random draw below comes from its own RNG
    # stream (seed + a fixed offset), so the hedge draws above, and the results built on them, do not move.
    reject_prob_by_venue: tuple[tuple[str, float], ...] = ()   # non-PostOnly exchange rejects (rate limits, errors): passive orders, and hedges / hedge retries ON TOP of reject_prob (so a hedge can be hit twice); NOT unwind orders
    disconnects: tuple[DisconnectSpec, ...] = ()               # random connection losses, per venue
    latency_scale: float = 1.0                                 # multiplies entry and cancel latencies (stress)
    ack_unknown_prob: float = 0.0                              # the acknowledgement is lost: the order sits in 'unknown' until reconciled
    unknown_resolve_s: float = 10.0                            # an unknown order is resolved by a venue query this long after ack_timeout_s
    fast_move_reject_prob: float = 0.0                         # hedge rejected with this probability during a fast move on the hedge venue
    fast_move_bps: float = 0.0                                 # a fast move: |mid change| >= this many bps ...
    fast_move_window_s: float = 1.0                            # ... within this many seconds
    label: str = ""                                            # preset name, carried into reports
    # WP6. venue_reject_applies_to_hedges: legacy True = reject_prob_by_venue also hits hedges ON TOP of reject_prob (a hedge can be
    # rejected twice); False = by-venue rejects are for passive orders only (the stressed preset uses this: hedges carry reject_prob
    # once). unwind_band_bps: an unwind is a limit order at mid x (1 -/+ band) instead of a market order (0 = market, legacy).
    # unknown_query_*: the venue status query that resolves an UNKNOWN order has its own failure probability and a bounded number
    # of attempts; exhaustion raises a persistent ALARM.
    venue_reject_applies_to_hedges: bool = True
    unwind_band_bps: float = 0.0
    unknown_query_fail_prob: float = 0.0
    unknown_query_max_attempts: int = 3
    # r12. unwind_max_attempts: TOTAL unwind sends per failed hedge chain (None = max_retries + 1, the legacy pairing); exhaustion
    # raises the persistent ALARM. Hedge retries keep using max_retries.
    unwind_max_attempts: int | None = None
    # r12 MODELLED restart (not the live RestartCoordinator path): at the maker fill numbers listed (1-based, per run) the strategy
    # process dies right after the fill and is back restart_s later. Both venues are dark for the strategy for that time (feed and
    # gateway: no evaluation, no sends), the fill's execution report and the hedge are only handled at restart end (reconcile, then
    # hedge), resting orders keep their exchange-side life, and the circuit breaker's state persists across the restart.
    restart_s: float = 0.0
    restart_at_fills: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        for name in ("reject_prob", "delay_prob", "partial_prob"):
            if not 0.0 <= getattr(self, name) <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        if not 0.0 < self.partial_fraction < 1.0:
            raise ValueError("partial_fraction must be in (0, 1)")
        if self.extra_delay_s < 0 or self.ack_timeout_s < 0 or self.local_reject_s < 0 or self.retry_backoff_s < 0 or self.outage_on_fill_s < 0:
            raise ValueError("times must be >= 0")
        if self.outage_policy not in OUTAGE_POLICIES:
            raise ValueError(f"outage_policy must be one of {OUTAGE_POLICIES}")
        if self.response not in RESPONSES:
            raise ValueError(f"response must be one of {RESPONSES}")
        if self.outage_on_fill_kind not in OUTAGE_KINDS:
            raise ValueError(f"outage_on_fill_kind must be one of {OUTAGE_KINDS}")
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if self.unwind_band_bps < 0.0 or not (0.0 <= self.unknown_query_fail_prob <= 1.0) or self.unknown_query_max_attempts < 1:
            raise ValueError("unwind_band_bps >= 0, unknown_query_fail_prob in [0, 1], unknown_query_max_attempts >= 1")
        if self.unwind_max_attempts is not None and self.unwind_max_attempts < 1:
            raise ValueError("unwind_max_attempts must be >= 1 or None")
        if self.restart_s < 0.0 or any(n < 1 for n in self.restart_at_fills):
            raise ValueError("restart_s >= 0 and restart_at_fills are 1-based fill numbers")
        for name in ("ack_unknown_prob", "fast_move_reject_prob"):
            if not 0.0 <= getattr(self, name) <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        for v, p in self.reject_prob_by_venue:
            if not 0.0 <= p <= 1.0:
                raise ValueError(f"reject_prob_by_venue[{v!r}] must be in [0, 1]")
        if not self.latency_scale > 0.0 or self.unknown_resolve_s < 0 or self.fast_move_bps < 0 or self.fast_move_window_s <= 0:
            raise ValueError("latency_scale > 0, unknown_resolve_s >= 0, fast_move_bps >= 0, fast_move_window_s > 0 required")

    @property
    def injects(self) -> bool:
        return bool(self.reject_prob or (self.delay_prob and self.extra_delay_s) or self.partial_prob
                    or self.outages or self.outage_on_fill_s or any(p for _, p in self.reject_prob_by_venue) or self.disconnects
                    or self.ack_unknown_prob or (self.fast_move_reject_prob and self.fast_move_bps) or self.latency_scale != 1.0
                    or (self.restart_s and self.restart_at_fills))

    def as_dict(self) -> dict:
        return {
            "seed": self.seed, "reject_prob": self.reject_prob, "delay_prob": self.delay_prob, "extra_delay_s": self.extra_delay_s,
            "partial_prob": self.partial_prob, "partial_fraction": self.partial_fraction, "n_static_outages": len(self.outages),
            "outage_on_fill_s": self.outage_on_fill_s, "outage_on_fill_kind": self.outage_on_fill_kind,
            "outage_policy": self.outage_policy, "ack_timeout_s": self.ack_timeout_s, "local_reject_s": self.local_reject_s,
            "response": self.response, "max_retries": self.max_retries, "retry_backoff_s": self.retry_backoff_s,
            "entry_guard": self.entry_guard, "unwind_max_attempts": self.unwind_max_attempts, "unwind_band_bps": self.unwind_band_bps,
            "restart_s": self.restart_s, "restart_at_fills": list(self.restart_at_fills),
            "reject_prob_by_venue": dict(self.reject_prob_by_venue), "latency_scale": self.latency_scale,
            "disconnects": [{"venue": d.venue, "rate_per_day": d.rate_per_day, "guaranteed_per_day": d.guaranteed_per_day,
                             "durations_s": list(d.durations_s), "median_s": d.median_s, "sigma": d.sigma, "kind": d.kind} for d in self.disconnects],
            "ack_unknown_prob": self.ack_unknown_prob, "unknown_resolve_s": self.unknown_resolve_s,
            "fast_move_reject_prob": self.fast_move_reject_prob, "fast_move_bps": self.fast_move_bps,
            "fast_move_window_s": self.fast_move_window_s, "label": self.label,
        }


@dataclass
class ExposureTracker:
    """The inventory path: updated after every event, so it sees the exposure between a maker fill and its hedge.

    net = sum of base over venues (what the hedge is supposed to keep at zero). Equity = cash + net marked at one reference mid
    (the freshest book's), so a hedged pair contributes only its realised cash and the cross-venue basis of the inventory split
    between venues is not counted as profit or loss. Time is the replay clock; state is held from one update to the next.
    """
    tol: float = 1e-9
    last_ts: float | None = None
    first_ts: float | None = None
    net: float = 0.0
    peak_abs_net: float = 0.0
    peak_abs_net_ts: float = 0.0
    peak_abs_net_notional: float = 0.0
    peak_abs_venue: dict[str, float] = field(default_factory=dict)
    time_net_nonzero_s: float = 0.0
    longest_net_nonzero_s: float = 0.0
    net_time_integral: float = 0.0          # base * seconds
    _run_start: float | None = None
    equity: float = 0.0
    equity_peak: float = 0.0
    max_drawdown: float = 0.0
    min_equity: float = 0.0
    long_peak: float = 0.0                  # largest net long / net short ever held
    short_peak: float = 0.0
    updates: int = 0

    def update(self, now: float, base: dict[str, float], cash: float, mids: dict[str, float], ref_mid: float = 0.0) -> None:
        if self.first_ts is None:
            self.first_ts = now
        if self.last_ts is not None and now > self.last_ts:
            dt = now - self.last_ts
            if abs(self.net) > self.tol:
                self.time_net_nonzero_s += dt
                self.net_time_integral += abs(self.net) * dt
        self.last_ts = now
        self.updates += 1
        net = sum(base.values())
        if abs(net) > self.tol:
            if self._run_start is None:
                self._run_start = now
            self.longest_net_nonzero_s = max(self.longest_net_nonzero_s, now - self._run_start)
        else:
            if self._run_start is not None:                  # flat again at `now`: the exposed stretch lasted until this update
                self.longest_net_nonzero_s = max(self.longest_net_nonzero_s, now - self._run_start)
            self._run_start = None
        self.net = net
        if net > self.long_peak:
            self.long_peak = net
        if -net > self.short_peak:
            self.short_peak = -net
        for v, b in base.items():
            if abs(b) > self.peak_abs_venue.get(v, 0.0):
                self.peak_abs_venue[v] = abs(b)
        if not ref_mid:
            live = [m for m in mids.values() if m > 0]
            ref_mid = sum(live) / len(live) if live else 0.0
        if abs(net) > self.peak_abs_net:
            self.peak_abs_net, self.peak_abs_net_ts = abs(net), now
            self.peak_abs_net_notional = abs(net) * ref_mid
        eq = cash + net * ref_mid
        self.equity = eq
        if eq > self.equity_peak:
            self.equity_peak = eq
        self.max_drawdown = max(self.max_drawdown, self.equity_peak - eq)
        self.min_equity = min(self.min_equity, eq)

    def report(self, end_ts: float | None = None) -> dict[str, float]:
        span = (end_ts if end_ts is not None else (self.last_ts or 0.0)) - (self.first_ts or 0.0)
        return {
            "span_s": span,
            "peak_abs_net_base": self.peak_abs_net, "peak_abs_net_ts": self.peak_abs_net_ts, "peak_abs_net_notional": self.peak_abs_net_notional,
            "peak_net_long_base": self.long_peak, "peak_net_short_base": self.short_peak,
            **{f"peak_abs_{v}_base": x for v, x in sorted(self.peak_abs_venue.items())},
            "time_net_nonzero_s": self.time_net_nonzero_s, "longest_net_nonzero_s": self.longest_net_nonzero_s,
            "share_of_time_net_nonzero": self.time_net_nonzero_s / span if span > 0 else 0.0,
            "mean_abs_net_base": self.net_time_integral / span if span > 0 else 0.0,
            "max_equity_drawdown": self.max_drawdown, "min_equity": self.min_equity, "final_equity": self.equity,
        }
