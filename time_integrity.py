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
        if self.events % 100 == 0:
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
