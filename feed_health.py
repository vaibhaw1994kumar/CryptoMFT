"""Feed health (W1, owner 17 Nov; its own module by the owner's decision of 16 Nov): one state per feed, HEALTHY / DEGRADED / UNSAFE, with hysteresis, a rollup per (venue, instrument,
stream), and the read-only scan of a recording. Moved here unchanged from time_integrity.py, which re-exports every name, so existing imports keep working. The delay it judges is the
one time_integrity computes (the host-offset estimate stays there: extend, no parallel set)."""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any


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


def feed_health_scan(events: Any, feed_policy: FeedHealthPolicy, time_policy: "TimePolicy | None" = None, *, now_of: Any = None) -> dict[str, Any]:
    """W1 (audit A1 1.7): the feed-health monitor run READ-ONLY over recorded canonical events (book events and, where the recording has them, trades as objects with venue,
    instrument_id, exchange_ts_ns, receive_ts_ns): the verdict a session would have recorded, with no session and no decision. `now_of(event)` gives the processing time of an event
    when the recording has one (own lag); without it now = the event's receive time, so OWN_LAG cannot be seen and says so. Returns the monitors' reports and the transitions."""
    from .time_integrity import TimeIntegrityMonitor, TimePolicy          # here, not at the top: time_integrity re-exports this module
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
