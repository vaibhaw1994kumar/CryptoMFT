"""Opt-in circuit breaker for the simulator path (CrossVenueArbBacktester).

When the breaker is NOT configured (None in ArbBacktestConfig.circuit_breaker) the
backtester behaves exactly as before — no new state, no changed timing.

When configured, the breaker monitors hedge failures and position risk in real time.
On a trip:
  - No new signals on the affected venue pair.
  - Resting maker orders are cancelled (with real cancel latency; fills during the
    cancel window are still counted).
  - The engine keeps trying to close open exposure within the configured budget.
  - The breaker stays tripped until explicitly reset (reset() or force_reset=True
    in a run that wants to continue from the tripped state — not done automatically).
  - Every trip is logged with cause, time, and state at the moment of the trip.

Triggers (each configurable; any one trips the breaker):
  consecutive_failures   N or more hedge failures in a row, no successful hedge
                         between them.
  failure_rate_window_s  If the fraction of hedges that failed in the last T seconds
  failure_rate_thresh    exceeds failure_rate_thresh, trip.
  failed_unwind          Any unwind that exhausted all retry attempts.
  net_exposure_s         |net| ≠ 0 continuously for longer than this.
  unknown_order_s        Any hedge order in UNKNOWN state for longer than this.
  gateway_down           Venue gateway considered down by the connectivity monitor.

Connectivity monitor (per venue): gateway "down" if no heartbeat for H seconds or
ack latency > k × rolling median. Strategy learns only via this monitor, not via oracle.
The monitor is a simple moving-window model: ack_latency_samples fed by the backtester,
latency_k_factor × median triggers the "high latency" threshold.

All thresholds are None by default → that trigger is disabled.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Deque


@dataclass
class CircuitBreakerConfig:
    """Configuration for every circuit-breaker trigger. None = trigger disabled."""

    # Trigger 1: N consecutive hedge failures
    consecutive_failures: int | None = None

    # Trigger 2: hedge failure rate over a rolling window
    failure_rate_window_s: float | None = None     # length of the rolling window
    failure_rate_thresh: float | None = None        # trip if failures/total > this

    # Trigger 3: any failed unwind (all attempts exhausted)
    trip_on_failed_unwind: bool = False

    # Trigger 4: |net| ≠ 0 continuously for longer than T seconds
    net_exposure_s: float | None = None

    # Trigger 5: any order in UNKNOWN state longer than T seconds
    unknown_order_s: float | None = None

    # Trigger 6: venue gateway down
    trip_on_gateway_down: bool = False

    # Connectivity monitor parameters
    heartbeat_timeout_s: float = 10.0    # gateway "down" if no heartbeat for this long
    ack_latency_k_factor: float = 5.0    # "down" if ack latency > k × rolling median
    ack_latency_window: int = 50         # rolling window size for median computation

    # Recovery budget: keep sending unwind attempts even when tripped, up to this many
    tripped_unwind_retries: int = 10

    def __post_init__(self) -> None:
        if self.consecutive_failures is not None and self.consecutive_failures < 1:
            raise ValueError("consecutive_failures must be >= 1")
        if self.failure_rate_thresh is not None:
            if not (0.0 < self.failure_rate_thresh <= 1.0):
                raise ValueError("failure_rate_thresh must be in (0, 1]")
        if self.failure_rate_window_s is not None and self.failure_rate_window_s <= 0:
            raise ValueError("failure_rate_window_s must be > 0")
        if self.net_exposure_s is not None and self.net_exposure_s <= 0:
            raise ValueError("net_exposure_s must be > 0")
        if self.unknown_order_s is not None and self.unknown_order_s <= 0:
            raise ValueError("unknown_order_s must be > 0")


@dataclass
class _HedgeEvent:
    ts: float
    failed: bool


@dataclass
class _UnknownOrder:
    order_id: int
    ts_entered: float
    venue: str


@dataclass
class BreakerTrip:
    ts: float
    cause: str
    net_at_trip: float
    pending_hedges: int
    consecutive_failures_at_trip: int
    detail: str = ""


@dataclass
class ConnectivityMonitor:
    """Per-venue ack-latency and heartbeat health tracker."""
    venue: str
    k_factor: float = 5.0
    window: int = 50
    heartbeat_timeout_s: float = 10.0

    _samples: Deque[float] = field(default_factory=deque, init=False, repr=False)
    _last_heartbeat_ts: float = field(default=float("-inf"), init=False, repr=False)
    _down_since: float | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._samples = deque(maxlen=self.window)

    def record_heartbeat(self, ts: float) -> None:
        self._last_heartbeat_ts = ts
        if self._down_since is not None:
            self._down_since = None   # recovered

    def record_ack(self, ts: float, latency_s: float) -> None:
        self._samples.append(latency_s)
        self.record_heartbeat(ts)

    def is_down(self, now: float) -> bool:
        if now - self._last_heartbeat_ts > self.heartbeat_timeout_s:
            return True
        if len(self._samples) >= 5:
            med = sorted(self._samples)[len(self._samples) // 2]
            if med > 0 and self._samples[-1] > self.k_factor * med:
                return True
        return False

    def down_since(self, now: float) -> float | None:
        """Return the ts when this venue first appeared down, or None if it looks up."""
        if self.is_down(now):
            if self._down_since is None:
                self._down_since = now
            return self._down_since
        self._down_since = None
        return None


class CircuitBreaker:
    """Runtime state machine for the circuit breaker.

    The backtester creates one instance per venue pair when CircuitBreakerConfig is set.
    It calls on_hedge_result, on_net_update, on_unknown_order, on_gateway_status and
    is_tripped / allow_signal at each step.
    """

    def __init__(self, config: CircuitBreakerConfig, venue_a: str, venue_b: str) -> None:
        self.config = config
        self.venue_a = venue_a
        self.venue_b = venue_b

        self._tripped = False
        self._consecutive_failures = 0
        self._hedge_window: Deque[_HedgeEvent] = deque()
        self._unknown_orders: dict[int, _UnknownOrder] = {}
        self._net_nonzero_since: float | None = None
        self.trips: list[BreakerTrip] = []
        self._unwind_attempts_while_tripped = 0

        self.connectivity: dict[str, ConnectivityMonitor] = {
            venue_a: ConnectivityMonitor(
                venue_a, config.ack_latency_k_factor, config.ack_latency_window,
                config.heartbeat_timeout_s,
            ),
            venue_b: ConnectivityMonitor(
                venue_b, config.ack_latency_k_factor, config.ack_latency_window,
                config.heartbeat_timeout_s,
            ),
        }

    # ── External feed ─────────────────────────────────────────────────────────

    def on_hedge_result(self, ts: float, failed: bool, net_abs: float, pending_hedges: int) -> None:
        cfg = self.config
        if failed:
            self._consecutive_failures += 1
        else:
            self._consecutive_failures = 0
        if cfg.failure_rate_window_s is not None:
            self._hedge_window.append(_HedgeEvent(ts, failed))
            cutoff = ts - cfg.failure_rate_window_s
            while self._hedge_window and self._hedge_window[0].ts < cutoff:
                self._hedge_window.popleft()

        if not self._tripped:
            cause = self._check_triggers(ts, net_abs, pending_hedges, check_failed_unwind=False)
            if failed and cfg.trip_on_failed_unwind is False:
                pass   # failed unwind handled separately
            if cause:
                self._trip(ts, cause, net_abs, pending_hedges)

    def on_failed_unwind(self, ts: float, net_abs: float, pending_hedges: int) -> None:
        if not self._tripped and self.config.trip_on_failed_unwind:
            self._trip(ts, "failed_unwind: all attempts exhausted", net_abs, pending_hedges)

    def on_net_update(self, ts: float, net_abs: float, pending_hedges: int) -> None:
        if net_abs > 1e-10:
            if self._net_nonzero_since is None:
                self._net_nonzero_since = ts
        else:
            self._net_nonzero_since = None

        if not self._tripped:
            cfg = self.config
            if cfg.net_exposure_s is not None and self._net_nonzero_since is not None:
                if ts - self._net_nonzero_since > cfg.net_exposure_s:
                    self._trip(ts, f"net_exposure: |net|>0 for >{cfg.net_exposure_s}s", net_abs, pending_hedges)

    def on_unknown_order(self, order_id: int, ts: float, venue: str) -> None:
        self._unknown_orders[order_id] = _UnknownOrder(order_id, ts, venue)

    def on_order_resolved(self, order_id: int) -> None:
        self._unknown_orders.pop(order_id, None)

    def on_gateway_status(self, ts: float, net_abs: float, pending_hedges: int) -> None:
        if not self._tripped and self.config.trip_on_gateway_down:
            for venue, mon in self.connectivity.items():
                if mon.is_down(ts):
                    self._trip(ts, f"gateway_down: {venue}", net_abs, pending_hedges)
                    break

    # ── Polling ───────────────────────────────────────────────────────────────

    def check_unknown_orders(self, ts: float, net_abs: float, pending_hedges: int) -> None:
        if self._tripped or self.config.unknown_order_s is None:
            return
        for unk in self._unknown_orders.values():
            if ts - unk.ts_entered > self.config.unknown_order_s:
                self._trip(ts, f"unknown_order: order {unk.order_id} on {unk.venue} unresolved >{self.config.unknown_order_s}s",
                           net_abs, pending_hedges)
                return

    def is_tripped(self) -> bool:
        return self._tripped

    def allow_signal(self, ts: float, net_abs: float) -> bool:
        return not self._tripped

    def allow_unwind(self) -> bool:
        """Tripped breakers allow unwind attempts up to the configured budget."""
        cfg = self.config
        if not self._tripped:
            return True
        if self._unwind_attempts_while_tripped < cfg.tripped_unwind_retries:
            self._unwind_attempts_while_tripped += 1
            return True
        return False

    def reset(self) -> None:
        self._tripped = False
        self._consecutive_failures = 0
        self._hedge_window.clear()
        self._unknown_orders.clear()
        self._net_nonzero_since = None
        self._unwind_attempts_while_tripped = 0

    # ── Internal ──────────────────────────────────────────────────────────────

    def _check_triggers(self, ts: float, net_abs: float, pending_hedges: int,
                        check_failed_unwind: bool = False) -> str:
        cfg = self.config
        if (cfg.consecutive_failures is not None
                and self._consecutive_failures >= cfg.consecutive_failures):
            return f"consecutive_failures: {self._consecutive_failures} >= {cfg.consecutive_failures}"
        if (cfg.failure_rate_thresh is not None and cfg.failure_rate_window_s is not None
                and len(self._hedge_window) >= 3):
            n = len(self._hedge_window)
            failed = sum(1 for e in self._hedge_window if e.failed)
            rate = failed / n
            if rate > cfg.failure_rate_thresh:
                return f"failure_rate: {failed}/{n} = {rate:.2%} > {cfg.failure_rate_thresh:.2%} in last {cfg.failure_rate_window_s}s"
        return ""

    def _trip(self, ts: float, cause: str, net_abs: float, pending_hedges: int) -> None:
        self._tripped = True
        trip = BreakerTrip(
            ts=ts, cause=cause, net_at_trip=net_abs, pending_hedges=pending_hedges,
            consecutive_failures_at_trip=self._consecutive_failures,
            detail=f"trips={len(self.trips) + 1}",
        )
        self.trips.append(trip)

    def summary(self) -> dict:
        return {
            "tripped": self._tripped,
            "trip_count": len(self.trips),
            "consecutive_failures_current": self._consecutive_failures,
            "unknown_orders_current": len(self._unknown_orders),
            "trips": [
                {"ts": t.ts, "cause": t.cause, "net_at_trip": t.net_at_trip,
                 "pending_hedges": t.pending_hedges}
                for t in self.trips
            ],
        }
