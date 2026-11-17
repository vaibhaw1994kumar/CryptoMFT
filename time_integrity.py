from __future__ import annotations

from bisect import bisect_left, insort
from collections import deque
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any

from .simulation.latency_model import HostClockAligner
from .models import MarketEvent, utc_now_iso


ORDERING_RULE = (
    "receive_ts_ns",
    "exchange_ts_ns",
    "source_ts_ns",
    "venue",
    "sequence_start",
    "sequence",
    "event_id",
)


@dataclass(frozen=True, slots=True)
class TimePolicy:
    max_event_latency_ns: int = 60_000_000_000
    max_feed_age_ns: int = 1_000_000_000
    max_cross_venue_skew_ns: int = 1_000_000_000
    max_host_offset_ns: int = 100_000_000
    max_host_drift_ppm: float = 500.0
    # Negative receive latency is normally a host-clock offset, not bad data.
    # With ``correct_host_offset`` the monitor estimates the offset as a low
    # quantile of (receive - exchange) over a plausibility-filtered window
    # (HostClockAligner from the advanced backtester rejects backlogged
    # stamps) and only blocks events still negative beyond the tolerance
    # after correction. Defaults keep the original fail-closed behaviour.
    negative_latency_tolerance_ns: int = 0
    correct_host_offset: bool = False
    host_offset_quantile: float = 0.01
    host_offset_window: int = 2_000
    host_offset_min_samples: int = 20
    # Backlog / slow consumer: an event whose venue-to-host delay, corrected by the
    # host-offset estimate, exceeds this is FEED_DELAYED (blocking decisions on it).
    # None (default) keeps the original behaviour.
    max_feed_delay_ns: int | None = None


@dataclass(frozen=True, slots=True)
class TimeAssessment:
    latency_ns: int
    feed_age_ns: int
    flags: tuple[str, ...]

    @property
    def blocking(self) -> bool:
        return bool(set(self.flags) & {
            "NEGATIVE_RECEIVE_LATENCY",
            "IMPOSSIBLE_EVENT_LATENCY",
            "FUTURE_RECEIVE_TIMESTAMP",
            "FEED_DELAYED",
            "PEER_FEED_DELAYED",
        })


@dataclass(slots=True)
class HostClockMonitor:
    max_offset_ns: int
    max_drift_ppm: float
    source: str = "UNOBSERVED"
    state: str = "UNKNOWN"
    samples: int = 0
    latest_offset_ns: int | None = None
    latest_drift_ppm: float | None = None
    last_observed_ts_ns: int | None = None
    max_abs_offset_ns: int = 0
    max_abs_drift_ppm: float = 0.0

    def record_sample(
        self,
        *,
        local_ts_ns: int,
        reference_ts_ns: int,
        observed_ts_ns: int,
        source: str,
    ) -> str:
        offset = local_ts_ns - reference_ts_ns
        drift_ppm = None
        if self.latest_offset_ns is not None and self.last_observed_ts_ns is not None:
            elapsed = observed_ts_ns - self.last_observed_ts_ns
            if elapsed > 0:
                drift_ppm = (offset - self.latest_offset_ns) * 1_000_000 / elapsed
        self.samples += 1
        self.source = source
        self.latest_offset_ns = offset
        self.last_observed_ts_ns = observed_ts_ns
        self.max_abs_offset_ns = max(self.max_abs_offset_ns, abs(offset))
        if drift_ppm is not None:
            self.latest_drift_ppm = drift_ppm
            self.max_abs_drift_ppm = max(self.max_abs_drift_ppm, abs(drift_ppm))
        if abs(offset) > self.max_offset_ns:
            self.state = "UNSYNCED"
        elif drift_ppm is not None and abs(drift_ppm) > self.max_drift_ppm:
            self.state = "DEGRADED"
        else:
            self.state = "SYNCED"
        return self.state

    def report(self) -> dict[str, Any]:
        return asdict(self)


LATENCY_WINDOW = 200_000      # events per venue kept for percentiles (bounded memory)


@dataclass(slots=True)
class _VenueTimeStats:
    # Exact running statistics plus a bounded window for percentiles: a live
    # session runs for days (~4M events/day/venue), so per-event lists must not grow.
    events: int = 0
    latency_total_ns: int = 0
    latency_min_ns: int | None = None
    latency_max_ns: int | None = None
    feed_age_max_ns: int | None = None
    latency_window_ns: deque = field(default_factory=lambda: deque(maxlen=LATENCY_WINDOW))
    negative_latency: int = 0
    corrected_negative_latency: int = 0
    impossible_latency: int = 0
    stale_feed: int = 0
    feed_delayed: int = 0
    max_corrected_delay_ns: int | None = None


class TimeIntegrityMonitor:
    def __init__(self, policy: TimePolicy, venues: tuple[str, ...] = ()):
        self.policy = policy
        self.host_clock = HostClockMonitor(
            policy.max_host_offset_ns,
            policy.max_host_drift_ppm,
        )
        self._venues = {venue: _VenueTimeStats() for venue in venues}
        self._latest_receive: dict[tuple[str, str], int] = {}
        # Streams whose latest event was FEED_DELAYED: their books are behind, so a
        # decision on *any* venue of that instrument must wait until they catch up.
        self.delayed_streams: set[tuple[str, str]] = set()
        self.events = 0
        self.blocked_events = 0
        self.cross_venue_samples = 0
        self.cross_venue_skew_breaches = 0
        self.max_cross_venue_skew_ns = 0
        self.clock_offset_estimate_ns: int | None = None
        # W1 (audit A1 1.6): with an independent reference (the venues' server time, `record_clock_reference`) the host clock monitor is fed by THAT, and the low-quantile estimate above is kept
        # only as a cross-check. False (every run without a reference): the monitor is fed by its own estimate, as before (labelled circular in the audit).
        self.host_clock_external = False
        self.clock_cross_check: dict[str, Any] | None = None
        self._offset_window: deque[int] = deque()
        self._offset_sorted: list[int] = []
        self.aligner = (
            HostClockAligner(
                window=policy.host_offset_window,
                warmup=policy.host_offset_min_samples,
            )
            if policy.correct_host_offset else None
        )

    def _observe_offset(self, event: MarketEvent, latency_ns: int) -> None:
        assert self.aligner is not None
        exchange_s = event.exchange_ts_ns / 1e9
        receive_s = event.receive_ts_ns / 1e9
        plausible = self.aligner.is_plausible(exchange_s, receive_s)
        self.aligner.observe(exchange_s, receive_s)
        if not plausible:
            return
        self._offset_window.append(latency_ns)
        insort(self._offset_sorted, latency_ns)
        if len(self._offset_window) > self.policy.host_offset_window:
            old = self._offset_window.popleft()
            del self._offset_sorted[bisect_left(self._offset_sorted, old)]
        if len(self._offset_sorted) < self.policy.host_offset_min_samples:
            return
        index = int((len(self._offset_sorted) - 1) * self.policy.host_offset_quantile)
        # Only a host clock *behind* the venue makes latency look negative.
        self.clock_offset_estimate_ns = min(0, self._offset_sorted[index])
        if self.events % 100 == 0 and not self.host_clock_external:
            self.host_clock.record_sample(
                local_ts_ns=event.receive_ts_ns,
                reference_ts_ns=event.receive_ts_ns - self.clock_offset_estimate_ns,
                observed_ts_ns=event.receive_ts_ns,
                source="EXCHANGE_TIMESTAMP_LOW_QUANTILE",
            )

    def observe(self, event: MarketEvent, *, now_ns: int) -> TimeAssessment:
        stats = self._venues.setdefault(event.venue, _VenueTimeStats())
        latency = event.receive_ts_ns - event.exchange_ts_ns
        feed_age = now_ns - event.receive_ts_ns
        flags: list[str] = []
        if self.aligner is not None:
            self._observe_offset(event, latency)
        corrected = latency - (self.clock_offset_estimate_ns or 0)
        if latency < 0:
            if (
                self.clock_offset_estimate_ns is not None
                and corrected >= -self.policy.negative_latency_tolerance_ns
            ) or (
                self.aligner is None
                and latency >= -self.policy.negative_latency_tolerance_ns
                and self.policy.negative_latency_tolerance_ns > 0
            ):
                flags.append("NEGATIVE_RECEIVE_LATENCY_CORRECTED")
                stats.corrected_negative_latency += 1
            else:
                flags.append("NEGATIVE_RECEIVE_LATENCY")
                stats.negative_latency += 1
        elif latency > self.policy.max_event_latency_ns:
            flags.append("IMPOSSIBLE_EVENT_LATENCY")
            stats.impossible_latency += 1
        elif self.policy.max_feed_delay_ns is not None and corrected > self.policy.max_feed_delay_ns:
            flags.append("FEED_DELAYED")
            stats.feed_delayed += 1
        if (self.policy.max_feed_delay_ns is not None and "NEGATIVE_RECEIVE_LATENCY_CORRECTED" in flags and corrected > self.policy.max_feed_delay_ns):
            # W1 (audit A1 1.6 f): the branch above used to end the chain, so an event whose RAW latency was negative (host clock behind) was never tested for the delay limit even when its
            # corrected delay was beyond it. Only reachable with max_feed_delay set and a host offset larger than the limit.
            flags.append("FEED_DELAYED")
            stats.feed_delayed += 1
        if self.policy.max_feed_delay_ns is not None:
            stream = (event.venue, event.instrument_id)
            if "FEED_DELAYED" in flags:
                self.delayed_streams.add(stream)
            else:
                self.delayed_streams.discard(stream)
            if any(instrument == event.instrument_id and venue != event.venue
                   for venue, instrument in self.delayed_streams):
                flags.append("PEER_FEED_DELAYED")
        stats.max_corrected_delay_ns = (
            corrected if stats.max_corrected_delay_ns is None else max(stats.max_corrected_delay_ns, corrected)
        )
        if feed_age < 0:
            flags.append("FUTURE_RECEIVE_TIMESTAMP")
        elif feed_age > self.policy.max_feed_age_ns:
            flags.append("STALE_FEED")
            stats.stale_feed += 1
        stats.events += 1
        stats.latency_total_ns += latency
        stats.latency_min_ns = latency if stats.latency_min_ns is None else min(stats.latency_min_ns, latency)
        stats.latency_max_ns = latency if stats.latency_max_ns is None else max(stats.latency_max_ns, latency)
        stats.feed_age_max_ns = feed_age if stats.feed_age_max_ns is None else max(stats.feed_age_max_ns, feed_age)
        stats.latency_window_ns.append(latency)
        self.events += 1
        assessment = TimeAssessment(latency, feed_age, tuple(flags))
        if assessment.blocking:
            self.blocked_events += 1

        self._latest_receive[(event.venue, event.instrument_id)] = event.receive_ts_ns
        aligned = [
            ts for (venue, instrument), ts in self._latest_receive.items()
            if instrument == event.instrument_id and venue != event.venue
        ]
        if aligned:
            skew = max(abs(event.receive_ts_ns - ts) for ts in aligned)
            self.cross_venue_samples += 1
            self.max_cross_venue_skew_ns = max(self.max_cross_venue_skew_ns, skew)
            if skew > self.policy.max_cross_venue_skew_ns:
                self.cross_venue_skew_breaches += 1
        return assessment

    def record_clock_reference(self, measurement: "ClockMeasurement") -> str:
        """W1: one independent clock measurement (local minus server, the A3 server-time probe) feeds the host clock monitor; from the first call on the monitor ignores its own estimate.
        The cross-check keeps the low-quantile estimate beside it: `gap_ns` is their difference (the quantile estimate is one-sided: it can only see a host clock BEHIND the venue)."""
        self.host_clock_external = True
        state = self.host_clock.record_sample(local_ts_ns=measurement.measured_ts_ns, reference_ts_ns=measurement.measured_ts_ns - measurement.offset_ns,
                                              observed_ts_ns=measurement.measured_ts_ns, source=measurement.source)
        estimate = self.clock_offset_estimate_ns
        self.clock_cross_check = {"reference_offset_ns": measurement.offset_ns, "reference_uncertainty_ns": measurement.uncertainty_ns, "low_quantile_estimate_ns": estimate,
                                  "gap_ns": None if estimate is None else measurement.offset_ns - estimate, "source": measurement.source}
        return state

    @staticmethod
    def _percentile(values: list[int], percentile: float) -> int | None:
        if not values:
            return None
        ordered = sorted(values)
        index = min(len(ordered) - 1, int((len(ordered) - 1) * percentile))
        return ordered[index]

    def report(self) -> dict[str, Any]:
        venues = {}
        for venue, stats in sorted(self._venues.items()):
            venues[venue] = {
                "events": stats.events,
                "latency_ns_min": stats.latency_min_ns,
                "latency_ns_mean": int(stats.latency_total_ns / stats.events) if stats.events else None,
                # Percentiles over the last LATENCY_WINDOW events of the venue.
                "latency_ns_p50": self._percentile(list(stats.latency_window_ns), 0.50),
                "latency_ns_p95": self._percentile(list(stats.latency_window_ns), 0.95),
                "latency_percentile_window": len(stats.latency_window_ns),
                "latency_ns_max": stats.latency_max_ns,
                "feed_age_ns_max": stats.feed_age_max_ns,
                "negative_latency_events": stats.negative_latency,
                "corrected_negative_latency_events": stats.corrected_negative_latency,
                "impossible_latency_events": stats.impossible_latency,
                "stale_feed_events": stats.stale_feed,
                "feed_delayed_events": stats.feed_delayed,
                "max_corrected_delay_ns": stats.max_corrected_delay_ns,
            }
        return {
            "schema_version": "1.0.0",
            "created_at": utc_now_iso(),
            "timestamp_unit": "nanoseconds_since_unix_epoch_utc",
            "ordering_rule": list(ORDERING_RULE),
            "cross_venue_alignment": {
                "rule": "compare latest local receive timestamps; do not rewrite venue clocks",
                "samples": self.cross_venue_samples,
                "breaches": self.cross_venue_skew_breaches,
                "maximum_skew_ns": self.max_cross_venue_skew_ns,
                "threshold_ns": self.policy.max_cross_venue_skew_ns,
            },
            "events": self.events,
            "blocked_events": self.blocked_events,
            "venues": venues,
            "host_clock": self.host_clock.report(),
            "host_offset_correction": {
                "enabled": self.aligner is not None,
                "quantile": self.policy.host_offset_quantile,
                "tolerance_ns": self.policy.negative_latency_tolerance_ns,
                "clock_offset_estimate_ns": self.clock_offset_estimate_ns,
                "window_samples": len(self._offset_sorted),
                "aligner": self.aligner.report() if self.aligner is not None else None,
                **({"reference_cross_check": self.clock_cross_check} if self.clock_cross_check is not None else {}),
            },
            "policy": asdict(self.policy),
        }

    def write_report(self, path: Path) -> dict[str, Any]:
        report = self.report()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        return report


# ---------------------------------------------------------------------------------------------------------------------------------------------------
# Clock-offset gate source (owner 16 Nov, limit A3). The audit (A1, 1.6) found that HostClockMonitor has no independent reference: its "offset" is the estimate itself. The one reference the platform
# trusts is the venues' own server time, read the way the testnet logger (ollog) does: five probes, the one with the lowest round trip wins, offset = server time minus the local midpoint of the probe,
# uncertainty = half that round trip. Ported here (the core never imports ollog).
# ---------------------------------------------------------------------------------------------------------------------------------------------------
@dataclass(slots=True)
class ClockMeasurement:
    offset_ns: int                      # local minus server (positive: the local clock is ahead)
    uncertainty_ns: int                 # half the round trip of the best probe
    measured_ts_ns: int                 # local time at which it was taken
    source: str                         # "server_time:<venue>" or "ntp:<server>" or an injected label
    samples: int = 1


def best_probe(probes: list[tuple[int, int, int]], *, source: str) -> ClockMeasurement | None:
    """`probes` = (local_before_ns, server_ts_ns, local_after_ns) per request. The lowest round trip wins; None when there is no valid probe (a negative or zero round trip is not a probe)."""
    valid = [(after - before, before, server, after) for before, server, after in probes if after > before and server > 0]
    if not valid:
        return None
    rtt, before, server, after = min(valid)
    return ClockMeasurement(offset_ns=(before + after) // 2 - server, uncertainty_ns=rtt // 2, measured_ts_ns=after, source=source, samples=len(valid))


@dataclass(slots=True)
class ClockGate:
    """The A3 decision. `evaluate` returns (state, detail) with state "ok", "unknown" (no measurement, or one older than `max_age_ns`) or "uncertain" (uncertainty above the limit). Replays with no measured clock use
    `replay=True`: the answer is "not_evaluable", which the session counts and never blocks on. Real-order modes (replay=False) block new quotes on "unknown" and "uncertain"; hedges continue."""
    max_uncertainty_ns: int
    max_age_ns: int
    replay: bool = False
    measurement: ClockMeasurement | None = None
    history: list = field(default_factory=list)

    def update(self, measurement: ClockMeasurement | None) -> None:
        if measurement is not None:
            self.measurement = measurement
            if len(self.history) < 200:
                self.history.append({"measured_ts_ns": measurement.measured_ts_ns, "offset_ms": measurement.offset_ns / 1e6, "uncertainty_ms": measurement.uncertainty_ns / 1e6, "source": measurement.source})

    def evaluate(self, now_ns: int) -> tuple[str, str]:
        m = self.measurement
        if m is None:
            return ("not_evaluable", "no measured clock in a replay") if self.replay else ("unknown", "no clock measurement")
        if now_ns - m.measured_ts_ns > self.max_age_ns:
            return ("not_evaluable", "measurement older than max_age in a replay") if self.replay else ("unknown", f"measurement {(now_ns - m.measured_ts_ns) / 1e9:.0f} s old")
        if m.uncertainty_ns > self.max_uncertainty_ns:
            return "uncertain", f"uncertainty {m.uncertainty_ns / 1e6:.2f} ms above {self.max_uncertainty_ns / 1e6:.2f} ms"
        return "ok", f"uncertainty {m.uncertainty_ns / 1e6:.2f} ms"


SERVER_TIME_URLS = {"BINANCE": "https://fapi.binance.com/fapi/v1/time", "BYBIT": "https://api.bybit.com/v5/market/time"}


def parse_server_time_ns(venue: str, body: Any) -> int:
    """The venue's server time in nanoseconds from its time endpoint's body. Binance USD-M: {"serverTime": ms}. Bybit v5: {"result": {"timeNano": "..."}, "time": ms}; the nanosecond field is preferred, the
    millisecond one is the fallback. Anything else raises (a probe that cannot be read is not a probe)."""
    import json as _json

    doc = _json.loads(body if isinstance(body, (str, bytes, bytearray)) else _json.dumps(body))
    if venue == "BINANCE":
        return int(doc["serverTime"]) * 1_000_000
    if venue == "BYBIT":
        if doc.get("retCode", 0) != 0:
            raise ValueError(f"bybit server time error {doc.get('retMsg')}")
        nano = (doc.get("result") or {}).get("timeNano")
        return int(nano) if nano else int(doc["time"]) * 1_000_000
    raise ValueError(f"no server-time endpoint known for {venue}")


async def probe_server_clock(http_getter, venue: str, *, probes: int = 5, timeout_s: float = 2.0, clock_ns=None) -> ClockMeasurement | None:
    """Five round trips to the venue's time endpoint on the live HTTP path (`http_getter(url, timeout) -> (body, receive_ts_ns)`, the live service's own getter). The best probe is kept (`best_probe`).
    A probe that fails is dropped; no valid probe returns None."""
    import time as _time

    clock = clock_ns or _time.time_ns
    out: list[tuple[int, int, int]] = []
    for _ in range(probes):
        before = clock()
        try:
            body, _recv = await http_getter(SERVER_TIME_URLS[venue], timeout_s)
            after = clock()
            out.append((before, parse_server_time_ns(venue, body), after))
        except Exception:
            continue
    return best_probe(out, source=f"server_time:{venue}")


# ---------------------------------------------------------------------------------------------------------------------------------------------------
# W1 feed health (owner 17 Nov): one state per feed, HEALTHY / DEGRADED / UNSAFE, with hysteresis, and a rollup per (venue, instrument, stream). OBSERVATION: the monitor decides
# nothing and acts on nothing; whoever owns it (the session) may attach an action behind a switch. Built only when a profile has a `feed_health` block, so every earlier profile is
# byte-identical. It lives here because this module already owns the delay and the host-offset estimate (audit A1 1.2: extend, no parallel set).
#   delay    the corrected venue-to-host delay of an event (receive minus exchange minus the host-offset estimate)
#   silence  the time since the stream's last message, evaluated at every event of any stream and at every timer tick
#   own lag  the time between a frame's ARRIVAL (the A9 arrival stamp, which is the event's receive time when arrival stamping is on) and its processing: the processor is behind, not
#            the venue. Carried as the OWN_LAG flag beside the state, never as a state of its own.
#   clock    a host clock the monitor calls UNSYNCED makes every feed at least DEGRADED (UNKNOWN is not evaluable and changes nothing).
# Escalation is immediate; recovery needs `recover_ns` of clean observations (no flapping under a noisy threshold). One transition record per change.
# ---------------------------------------------------------------------------------------------------------------------------------------------------
FEED_STATES = ("HEALTHY", "DEGRADED", "UNSAFE")
_SEVERITY = {name: i for i, name in enumerate(FEED_STATES)}


@dataclass(frozen=True, slots=True)
class FeedHealthPolicy:
    delay_degraded_ns: int
    delay_unsafe_ns: int
    silence_degraded_ns: int
    silence_unsafe_ns: int
    own_lag_ns: int
    recover_ns: int
    burst_window_ns: int = 100_000_000
    burst_count: int = 200

    def __post_init__(self) -> None:
        if not (0 < self.delay_degraded_ns <= self.delay_unsafe_ns and 0 < self.silence_degraded_ns <= self.silence_unsafe_ns):
            raise ValueError("feed_health: the degraded limits must be positive and not above the unsafe limits")
        if self.own_lag_ns <= 0 or self.recover_ns < 0 or self.burst_window_ns <= 0 or self.burst_count < 1:
            raise ValueError("feed_health: own_lag, burst window and burst count must be positive and recover not negative")


@dataclass(slots=True)
class _FeedRollup:
    events: int = 0
    first_ts_ns: int | None = None
    last_ts_ns: int | None = None
    delay_total_ns: int = 0
    delay_count: int = 0
    delay_max_ns: int | None = None
    delay_window_ns: deque = field(default_factory=lambda: deque(maxlen=20_000))
    gap_max_ns: int = 0
    bursts: int = 0
    in_burst: bool = False
    recent_ns: deque = field(default_factory=deque)
    own_lag_max_ns: int = 0
    own_lag_events: int = 0
    check_silence: bool = True
    state: str = "HEALTHY"
    cause: str = ""
    clean_since_ns: int | None = None
    state_since_ns: int | None = None
    seconds_in_state: dict = field(default_factory=lambda: {name: 0.0 for name in FEED_STATES})
    episodes: dict = field(default_factory=lambda: {"DEGRADED": 0, "UNSAFE": 0})


class FeedHealthMonitor:
    def __init__(self, policy: FeedHealthPolicy, host_clock: HostClockMonitor | None = None) -> None:
        self.policy = policy
        self.host_clock = host_clock
        self._feeds: dict[tuple[str, str, str], _FeedRollup] = {}
        self.transitions: list[dict[str, Any]] = []
        self.transition_count = 0
        self.own_lag_active = False
        self.own_lag_episodes = 0

    # -- inputs ---------------------------------------------------------------------------------------------------------------------------------------
    def observe(self, venue: str, instrument_id: str, stream: str, *, receive_ts_ns: int, now_ns: int, corrected_delay_ns: int | None, check_silence: bool = True) -> list[dict[str, Any]]:
        """One message of one stream. `receive_ts_ns` is the event's own receive stamp, `now_ns` the time it is processed. `check_silence` False for a stream whose quiet is not a fault
        (a trade stream is silent whenever nobody trades): it is judged on delay only. Returns the transitions it caused (usually none)."""
        key = (venue, instrument_id, stream)
        r = self._feeds.get(key)
        if r is None:
            r = self._feeds[key] = _FeedRollup(first_ts_ns=receive_ts_ns, state_since_ns=receive_ts_ns, check_silence=check_silence)
        if r.last_ts_ns is not None and receive_ts_ns > r.last_ts_ns:
            r.gap_max_ns = max(r.gap_max_ns, receive_ts_ns - r.last_ts_ns)
        r.last_ts_ns = receive_ts_ns if r.last_ts_ns is None else max(r.last_ts_ns, receive_ts_ns)
        r.events += 1
        if corrected_delay_ns is not None:
            r.delay_total_ns += corrected_delay_ns
            r.delay_count += 1
            r.delay_max_ns = corrected_delay_ns if r.delay_max_ns is None else max(r.delay_max_ns, corrected_delay_ns)
            r.delay_window_ns.append(corrected_delay_ns)
        recent = r.recent_ns
        recent.append(receive_ts_ns)
        while recent and receive_ts_ns - recent[0] > self.policy.burst_window_ns:
            recent.popleft()
        burst = len(recent) >= self.policy.burst_count
        if burst and not r.in_burst:
            r.bursts += 1
        r.in_burst = burst
        lag = now_ns - receive_ts_ns
        if lag > 0:
            r.own_lag_max_ns = max(r.own_lag_max_ns, lag)
        lagging = lag > self.policy.own_lag_ns
        if lagging:
            r.own_lag_events += 1
        if lagging and not self.own_lag_active:
            self.own_lag_episodes += 1
        self.own_lag_active = lagging
        out = self._evaluate(key, r, now_ns, corrected_delay_ns, fresh=True)
        return out + self.tick(now_ns, skip=key)

    def tick(self, now_ns: int, skip: tuple[str, str, str] | None = None) -> list[dict[str, Any]]:
        """Silence and clock, for every feed (a feed that stops sending produces no event of its own)."""
        out: list[dict[str, Any]] = []
        for key, r in self._feeds.items():
            if key != skip:
                out += self._evaluate(key, r, now_ns, None)
        return out

    # -- the state rule -------------------------------------------------------------------------------------------------------------------------------
    def _target(self, r: _FeedRollup, now_ns: int, delay_ns: int | None, fresh: bool = False) -> tuple[str, str]:
        p = self.policy
        silence = now_ns - (r.last_ts_ns if r.last_ts_ns is not None else now_ns) if (r.check_silence or fresh) else 0
        quiet = "own_lag" if fresh else "silence"            # the age of a message that is being processed right now is the processor's lag, not the venue's silence
        if silence > p.silence_unsafe_ns:
            return "UNSAFE", quiet
        if delay_ns is not None and delay_ns > p.delay_unsafe_ns:
            return "UNSAFE", "delay"
        if silence > p.silence_degraded_ns:
            return "DEGRADED", quiet
        if delay_ns is not None and delay_ns > p.delay_degraded_ns:
            return "DEGRADED", "delay"
        if self.host_clock is not None and self.host_clock.state == "UNSYNCED":
            return "DEGRADED", "host_clock_unsynced"
        return "HEALTHY", ""

    def _evaluate(self, key: tuple[str, str, str], r: _FeedRollup, now_ns: int, delay_ns: int | None, fresh: bool = False) -> list[dict[str, Any]]:
        target, cause = self._target(r, now_ns, delay_ns, fresh)
        if _SEVERITY[target] > _SEVERITY[r.state]:
            return [self._move(key, r, target, cause, now_ns)]                       # worse: at once
        if _SEVERITY[target] == _SEVERITY[r.state]:
            r.clean_since_ns = None
            if target != "HEALTHY":
                r.cause = cause
            return []
        if r.clean_since_ns is None:                                                # better: only after recover_ns of observations that are all better than the current state
            r.clean_since_ns = now_ns
        if now_ns - r.clean_since_ns >= self.policy.recover_ns:
            return [self._move(key, r, target, cause or "recovered", now_ns)]
        return []

    def _move(self, key: tuple[str, str, str], r: _FeedRollup, target: str, cause: str, now_ns: int) -> dict[str, Any]:
        since = r.state_since_ns if r.state_since_ns is not None else now_ns
        r.seconds_in_state[r.state] += max(0, now_ns - since) / 1e9
        row = {"venue": key[0], "instrument_id": key[1], "stream": key[2], "previous": r.state, "state": target, "cause": cause, "ts_ns": now_ns, "own_lag": self.own_lag_active}
        r.state, r.cause, r.state_since_ns, r.clean_since_ns = target, cause if target != "HEALTHY" else "", now_ns, None
        if target in r.episodes and _SEVERITY[target] > _SEVERITY[row["previous"]]:
            r.episodes[target] += 1
        self.transition_count += 1
        if len(self.transitions) < 2_000:
            self.transitions.append(row)
        return row

    # -- reading --------------------------------------------------------------------------------------------------------------------------------------
    def state(self, venue: str, instrument_id: str | None = None) -> str:
        """The worst state over the venue's streams (of one instrument, or all): what an action would read."""
        worst = "HEALTHY"
        for (v, i, _stream), r in self._feeds.items():
            if v == venue and (instrument_id is None or i == instrument_id) and _SEVERITY[r.state] > _SEVERITY[worst]:
                worst = r.state
        return worst

    def worst(self) -> tuple[str, str]:
        worst, where = "HEALTHY", ""
        for (v, _i, stream), r in sorted(self._feeds.items()):
            if _SEVERITY[r.state] > _SEVERITY[worst]:
                worst, where = r.state, f"{v}:{stream}:{r.cause}"
        return worst, where

    def report(self, now_ns: int | None = None) -> dict[str, Any]:
        feeds = {}
        for (venue, instrument, stream), r in sorted(self._feeds.items()):
            window = sorted(r.delay_window_ns)
            pick = lambda q: window[min(len(window) - 1, int((len(window) - 1) * q))] if window else None
            span = (r.last_ts_ns - r.first_ts_ns) / 1e9 if r.events > 1 and r.last_ts_ns is not None and r.first_ts_ns is not None else 0.0
            seconds = dict(r.seconds_in_state)
            if now_ns is not None and r.state_since_ns is not None:
                seconds[r.state] += max(0, now_ns - r.state_since_ns) / 1e9
            feeds[f"{venue}|{instrument}|{stream}"] = {
                "events": r.events, "rate_per_s": round(r.events / span, 3) if span > 0 else None, "delay_ms": {"mean": round(r.delay_total_ns / r.delay_count / 1e6, 3) if r.delay_count else None,
                                                                                                             "p50": None if pick(0.5) is None else pick(0.5) / 1e6, "p99": None if pick(0.99) is None else pick(0.99) / 1e6,
                                                                                                             "max": None if r.delay_max_ns is None else r.delay_max_ns / 1e6, "window": len(window)},
                "max_gap_ms": r.gap_max_ns / 1e6, "bursts": r.bursts, "own_lag_max_ms": r.own_lag_max_ns / 1e6, "own_lag_events": r.own_lag_events,
                "state": r.state, "cause": r.cause, "episodes": dict(r.episodes), "seconds_in_state": {k: round(v, 6) for k, v in seconds.items()},
            }
        return {"policy": {k: v for k, v in asdict(self.policy).items()}, "feeds": feeds, "transitions": self.transition_count, "transition_rows": list(self.transitions),
                "own_lag_episodes": self.own_lag_episodes, "host_clock_state": None if self.host_clock is None else self.host_clock.state}


def feed_health_scan(events: Any, feed_policy: FeedHealthPolicy, time_policy: TimePolicy | None = None, *, now_of: Any = None) -> dict[str, Any]:
    """W1 (audit A1 1.7): the feed-health monitor run READ-ONLY over recorded canonical events (book events and, where the recording has them, trades as objects with venue,
    instrument_id, exchange_ts_ns, receive_ts_ns): the verdict a session would have recorded, with no session and no decision. `now_of(event)` gives the processing time of an event
    when the recording has one (own lag); without it now = the event's receive time, so OWN_LAG cannot be seen and says so. Returns the monitors' reports and the transitions."""
    time_monitor = TimeIntegrityMonitor(time_policy or TimePolicy(correct_host_offset=True), ())
    monitor = FeedHealthMonitor(feed_policy, time_monitor.host_clock)
    n = blocked = 0
    last = None
    for event in events:
        now = event.receive_ts_ns if now_of is None else now_of(event)
        is_book = getattr(event, "event_type", None) is not None
        if is_book:
            assessment = time_monitor.observe(event, now_ns=now)
            blocked += assessment.blocking
            delay = assessment.latency_ns - (time_monitor.clock_offset_estimate_ns or 0)
            monitor.observe(event.venue, event.instrument_id, "book", receive_ts_ns=event.receive_ts_ns, now_ns=now, corrected_delay_ns=delay)
        else:
            exchange = getattr(event, "exchange_ts_ns", None)
            monitor.observe(event.venue, event.instrument_id, "trade", receive_ts_ns=event.receive_ts_ns, now_ns=now,
                            corrected_delay_ns=None if not exchange else event.receive_ts_ns - exchange - (time_monitor.clock_offset_estimate_ns or 0), check_silence=False)
        n += 1
        last = now
    report = monitor.report(last)
    return {"events": n, "time_integrity_blocked_events": blocked, "own_lag_visible": now_of is not None, "feed_health": report,
            "clock_offset_estimate_ns": time_monitor.clock_offset_estimate_ns, "host_clock": time_monitor.host_clock.report()}
