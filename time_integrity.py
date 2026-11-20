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
    fields: dict | None = None          # Phase 2 F03 a: the chrony fields it was built from (reference, stratum, root delay / dispersion, ...); None for the server-time probe


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
    `replay=True`: the answer is "not_evaluable", which the session counts and never blocks on. Real-order modes (replay=False) block new quotes on "unknown" and "uncertain"; hedges continue.

    Phase 2 F03 (owner 20 Nov): with `max_offset_ns` set (ASSUMED, the owner sets it) the offset is checked SEPARATELY and FIRST: |offset| above it is the state "offset" (it wins over "uncertain": the
    chrony uncertainty contains |offset|, so a large offset raises both). The offset checked is the measurement's offset plus `session_gap_of()` (the session clock minus the wall clock: a wall-clock step
    the monotonic session clock did not follow) minus `correction_of()` when the host-offset correction is on (the residual after the correction; `residual` True says so in the report). Every evaluation
    is counted per state with the largest |offset| and uncertainty seen; `last` holds the newest evaluation's numbers. Without `max_offset_ns` the details and states are the A3 ones."""
    max_uncertainty_ns: int
    max_age_ns: int
    replay: bool = False
    measurement: ClockMeasurement | None = None
    history: list = field(default_factory=list)
    max_offset_ns: int | None = None
    correction_of: Any = None            # () -> the host-offset correction in force (ns, or None): set only when the profile applies it
    session_gap_of: Any = None           # () -> session clock minus wall clock (ns): set only with the monotonic session clock
    evaluations: dict = field(default_factory=dict)
    last: dict | None = None
    invalid_reason: str | None = None

    @property
    def residual(self) -> bool:
        return self.correction_of is not None

    def update(self, measurement: ClockMeasurement | None) -> None:
        if measurement is not None:
            self.measurement = measurement
            self.invalid_reason = None
            if len(self.history) < 200:
                self.history.append({"measured_ts_ns": measurement.measured_ts_ns, "offset_ms": measurement.offset_ns / 1e6, "uncertainty_ms": measurement.uncertainty_ns / 1e6, "source": measurement.source})

    def invalidate(self, reason: str) -> None:
        """Phase 2 F03 c: the source says the clock is not trustworthy NOW (chrony unsynchronised, absent or failing): the old measurement is dropped, so the gate is "unknown" at once instead of
        passing on a stale reading until it ages out. No fallback."""
        self.measurement = None
        self.invalid_reason = reason

    def checked_offset_ns(self) -> int | None:
        m = self.measurement
        if m is None:
            return None
        off = m.offset_ns + (int(self.session_gap_of() or 0) if self.session_gap_of is not None else 0)
        if self.correction_of is not None:
            off -= int(self.correction_of() or 0)
        return off

    def _count(self, state: str, offset_ns: int | None, unc_ns: int | None) -> None:
        row = self.evaluations.setdefault(state, {"n": 0, "max_abs_offset_ms": None, "max_uncertainty_ms": None})
        row["n"] += 1
        if offset_ns is not None:
            row["max_abs_offset_ms"] = max(row["max_abs_offset_ms"] or 0.0, abs(offset_ns) / 1e6)
        if unc_ns is not None:
            row["max_uncertainty_ms"] = max(row["max_uncertainty_ms"] or 0.0, unc_ns / 1e6)
        self.last = {"state": state, "offset_ms": None if offset_ns is None else offset_ns / 1e6, "uncertainty_ms": None if unc_ns is None else unc_ns / 1e6}

    def evaluate(self, now_ns: int) -> tuple[str, str]:
        state, detail = self._evaluate(now_ns)
        if self.max_offset_ns is not None:
            m = self.measurement
            self._count(state, self.checked_offset_ns(), None if m is None else m.uncertainty_ns)
        return state, detail

    def _evaluate(self, now_ns: int) -> tuple[str, str]:
        m = self.measurement
        if m is None:
            if self.invalid_reason is not None and not self.replay:
                return "unknown", self.invalid_reason
            return ("not_evaluable", "no measured clock in a replay") if self.replay else ("unknown", "no clock measurement")
        if now_ns - m.measured_ts_ns > self.max_age_ns:
            return ("not_evaluable", "measurement older than max_age in a replay") if self.replay else ("unknown", f"measurement {(now_ns - m.measured_ts_ns) / 1e9:.0f} s old")
        if self.max_offset_ns is not None:
            off = self.checked_offset_ns()
            numbers = f"offset {off / 1e6:.2f} ms{' (residual after the host-offset correction)' if self.residual else ''}, uncertainty {m.uncertainty_ns / 1e6:.2f} ms"
            if abs(off) > self.max_offset_ns:
                return "offset", f"{numbers}: offset above {self.max_offset_ns / 1e6:.2f} ms"
            if m.uncertainty_ns > self.max_uncertainty_ns:
                return "uncertain", f"{numbers}: uncertainty above {self.max_uncertainty_ns / 1e6:.2f} ms"
            return "ok", numbers
        if m.uncertainty_ns > self.max_uncertainty_ns:
            return "uncertain", f"uncertainty {m.uncertainty_ns / 1e6:.2f} ms above {self.max_uncertainty_ns / 1e6:.2f} ms"
        return "ok", f"uncertainty {m.uncertainty_ns / 1e6:.2f} ms"

    def report(self) -> dict:
        return {"max_offset_ms": None if self.max_offset_ns is None else self.max_offset_ns / 1e6, "max_offset_label": "ASSUMED (owner to set)",
                "offset_checked": ("the residual after the host-offset correction" if self.residual else "the measured offset") + (" plus the session-clock gap" if self.session_gap_of is not None else ""),
                "evaluations": {k: dict(v) for k, v in sorted(self.evaluations.items())}, "last": self.last, "invalid_reason": self.invalid_reason}


# ---------------------------------------------------------------------------------------------------------------------------------------------------
# Phase 2 F03 a (owner 20 Nov): the production clock source is the host's chrony. `chronyc tracking` (human-readable form: its "seconds fast/slow of NTP time" carries the sign in words) is parsed;
# offset = local minus NTP (fast = positive), uncertainty = |system offset| + root dispersion + root delay / 2 (the owner's rule), measured at chrony's Ref time (its last update), so the 900 s age rule
# also catches a chrony that stopped updating. Unsynchronised (leap status "Not synchronised", reference id 00000000, or stratum 0) or a failing chronyc gives NO measurement: the gate goes "unknown".
# The format is the documented one; the parser is UNVERIFIED against this platform's production host until `ollog hostmeasure` records real output there (this Mac's chronyc does not start).
# ---------------------------------------------------------------------------------------------------------------------------------------------------
def _chrony_seconds(value: str) -> float:
    return float(value.split()[0])


def parse_chronyc_tracking(text: str) -> dict:
    """The fields of `chronyc tracking` (human-readable). Keys present only when the line was there: reference_id, reference_name, stratum, ref_time_ns, system_offset_ns (local minus NTP: fast = +),
    last_offset_ns, rms_offset_ns, frequency_ppm, root_delay_ns, root_dispersion_ns, update_interval_s, leap_status. Unknown lines are ignored; nothing is defaulted."""
    from datetime import datetime, timezone

    out: dict = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, value = (x.strip() for x in line.split(":", 1))
        key = name.lower()
        try:
            if key == "reference id":
                parts = value.split(None, 1)
                out["reference_id"] = parts[0]
                if len(parts) > 1:
                    out["reference_name"] = parts[1].strip("()")
            elif key == "stratum":
                out["stratum"] = int(value)
            elif key == "ref time (utc)":
                out["ref_time_ns"] = int(datetime.strptime(value, "%a %b %d %H:%M:%S %Y").replace(tzinfo=timezone.utc).timestamp()) * 1_000_000_000
            elif key == "system time":
                words = value.split()
                sign = 1 if "fast" in words else (-1 if "slow" in words else None)
                if sign is not None:
                    out["system_offset_ns"] = sign * round(float(words[0]) * 1e9)
            elif key in ("last offset", "rms offset"):
                out[key.replace(" ", "_") + "_ns"] = round(_chrony_seconds(value) * 1e9)
            elif key == "frequency":
                words = value.split()
                out["frequency_ppm"] = float(words[0]) * (-1 if "slow" in words else 1)
            elif key == "root delay":
                out["root_delay_ns"] = round(_chrony_seconds(value) * 1e9)
            elif key == "root dispersion":
                out["root_dispersion_ns"] = round(_chrony_seconds(value) * 1e9)
            elif key == "update interval":
                out["update_interval_s"] = _chrony_seconds(value)
            elif key == "leap status":
                out["leap_status"] = value
        except (ValueError, IndexError):
            out.setdefault("unparsed", []).append(name)
    return out


def chrony_synchronised(fields: dict) -> tuple[bool, str]:
    if not fields:
        return False, "chronyc gave no tracking fields"
    if fields.get("leap_status", "").lower().startswith("not synchronised"):
        return False, "chrony is not synchronised (leap status)"
    if fields.get("reference_id", "").strip("0") == "" or fields.get("stratum", 0) == 0:
        return False, "chrony has no reference (reference id 00000000 / stratum 0)"
    for key in ("system_offset_ns", "root_delay_ns", "root_dispersion_ns", "ref_time_ns"):
        if key not in fields:
            return False, f"chronyc output lacks {key}"
    return True, ""


def chrony_measurement(fields: dict) -> ClockMeasurement | None:
    """The ClockMeasurement of one chrony reading, or None when chrony is not synchronised or a needed field is missing (the gate then goes "unknown")."""
    ok, _ = chrony_synchronised(fields)
    if not ok:
        return None
    off = fields["system_offset_ns"]
    unc = abs(off) + fields["root_dispersion_ns"] + fields["root_delay_ns"] // 2
    return ClockMeasurement(offset_ns=off, uncertainty_ns=unc, measured_ts_ns=fields["ref_time_ns"], source=f"chrony:{fields.get('reference_name') or fields.get('reference_id')}:stratum{fields.get('stratum')}",
                            samples=1, fields=dict(fields))


def read_chrony(binary: str = "chronyc", runner=None, timeout_s: float = 5.0) -> tuple[ClockMeasurement | None, dict, str]:
    """Run `chronyc tracking` (or `runner()` -> (exit code, stdout, stderr), for tests) and return (measurement or None, the parsed fields, why there is none). A chronyc that is absent, fails or times out
    is no measurement: there is no fallback."""
    import subprocess

    try:
        if runner is not None:
            rc, text, err = runner()
        else:
            r = subprocess.run([binary, "tracking"], capture_output=True, text=True, timeout=timeout_s)
            rc, text, err = r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return None, {}, f"chronyc could not be run ({type(exc).__name__})"
    if rc != 0:
        return None, {}, f"chronyc exited {rc}: {(err or '').strip()[:200]}"
    fields = parse_chronyc_tracking(text)
    ok, why = chrony_synchronised(fields)
    return (chrony_measurement(fields), fields, "") if ok else (None, fields, why)


class MonotonicSessionClock:
    """Phase 2 F03 b (owner 20 Nov): the live session's clock. Anchored ONCE to the wall clock and advanced by the monotonic clock, so every interval and deadline (OMS, session, feed) is immune to a
    wall-clock step. `check_step()` compares the wall clock with it; a change of their gap above `step_threshold_ns` (ASSUMED 50 ms) is a WALL_CLOCK_STEP, returned once and kept in `steps`. There is no
    re-anchoring (that would itself step the session timeline); `gap_ns()` (session minus wall) is added to the clock gate's offset, so a step that leaves the session clock wrong is caught there."""

    def __init__(self, wall=None, mono=None, step_threshold_ns: int = 50_000_000):
        import time as _time

        self._wall, self._mono = wall or _time.time_ns, mono or _time.monotonic_ns
        self.anchor_wall_ns, self.anchor_mono_ns = self._wall(), self._mono()
        self.step_threshold_ns = int(step_threshold_ns)
        self._last_gap_ns = 0
        self.steps: list[dict] = []

    def __call__(self) -> int:
        return self.anchor_wall_ns + (self._mono() - self.anchor_mono_ns)

    def gap_ns(self) -> int:
        """Session clock minus wall clock now."""
        return self() - self._wall()

    def check_step(self) -> dict | None:
        now_session, now_wall = self(), self._wall()
        gap = now_session - now_wall
        step = gap - self._last_gap_ns
        if abs(step) <= self.step_threshold_ns:
            return None
        event = {"session_ts_ns": now_session, "wall_ts_ns": now_wall, "step_ns": -step, "gap_before_ns": self._last_gap_ns, "gap_after_ns": gap, "threshold_ns": self.step_threshold_ns}
        self._last_gap_ns = gap
        if len(self.steps) < 200:
            self.steps.append(event)
        return event


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


# W1 feed health lives in feed_health.py (owner 16 Nov: its own module); every name is re-exported here so existing imports are unchanged.
from .feed_health import FEED_STATES, FeedHealthMonitor, FeedHealthPolicy, _FeedRollup, _SEVERITY, feed_health_scan  # noqa: E402,F401
