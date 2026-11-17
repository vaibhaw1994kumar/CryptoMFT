"""Cross-venue consistency (market-data audit P2).

Two checks, kept apart on purpose:

* ``check_instrument_compatibility`` (config load): every venue leg of the strategy
  instrument must be the same contract (base, quote, product type, settlement asset,
  multiplier). A spot leg against a perpetual, or USDT against USDC, makes every price
  comparison meaningless, so it fails closed.
* ``CrossVenueMonitor`` (live): the mid-price divergence between venues whose books
  are VALID. A divergence is *not* a data fault by itself (arbitrage is divergence),
  so a breach of the threshold is classified, reported and never used to block:

  - ``STALE_SIDE``: a venue is FEED_DELAYED (its book reflects old data);
  - ``STALLED_VENUE``: one venue's mid has not changed for ``stall_ns`` while the
    other's has (a frozen feed that still looks VALID);
  - ``MARKET_DIVERGENCE``: both sides current: a real price difference.

Stale and delayed data already block trading (book gate, FEED_DELAYED); this
monitor explains divergences and surfaces frozen feeds the other checks cannot see.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from .models import BookState
from .orderbook import OrderBook


def check_instrument_compatibility(instruments: Iterable, instrument_id: str, venues: Iterable[str]) -> None:
    def terms(instrument) -> tuple:
        return (instrument.base, instrument.quote, instrument.product_type,
                getattr(instrument, "settlement_asset", None) or instrument.quote,
                getattr(instrument, "contract_multiplier", Decimal("1")))

    by_venue: dict[str, set[tuple]] = {}
    for instrument in instruments:
        if instrument.instrument_id == instrument_id and instrument.venue in set(venues):
            by_venue.setdefault(instrument.venue, set()).add(terms(instrument))
    distinct = set().union(*by_venue.values()) if by_venue else set()
    if len(distinct) > 1:
        detail = "; ".join(
            f"{venue}: base={t[0]} quote={t[1]} product={t[2]} settles={t[3]} multiplier={t[4]}"
            for venue, rows in sorted(by_venue.items()) for t in sorted(rows, key=str)
        )
        raise ValueError(f"{instrument_id}: venue legs are not the same contract ({detail})")


@dataclass(frozen=True, slots=True)
class Divergence:
    bps: Decimal                 # (mid_a - mid_b) / mean mid, first venue minus second
    classification: str          # STALE_SIDE | STALLED_VENUE | MARKET_DIVERGENCE | WITHIN_THRESHOLD
    venue: str = ""              # the stale/stalled venue, when there is one


@dataclass(slots=True)
class CrossVenueMonitor:
    venues: tuple[str, str]
    threshold_bps: Decimal
    stall_ns: int
    window: int = 10_000
    _last_mid: dict[str, Decimal] = field(default_factory=dict)
    _last_change_ns: dict[str, int] = field(default_factory=dict)
    _recent_abs_bps: deque = field(default_factory=deque)
    samples: int = 0
    max_abs_bps: Decimal = Decimal("0")
    breaches: dict[str, int] = field(default_factory=dict)
    state: str = "WITHIN_THRESHOLD"

    def observe(self, books: dict[str, OrderBook], now_ns: int, delayed: Iterable[str] = ()) -> Divergence | None:
        """Called after each applied update (``now_ns`` monotonic). None until both books are VALID."""
        mids = {}
        for venue in self.venues:
            book = books.get(venue)
            if book is None or book.state != BookState.VALID or not book.bids or not book.asks:
                return None
            mids[venue] = (book.best_bid + book.best_ask) / 2
            if self._last_mid.get(venue) != mids[venue]:
                self._last_mid[venue] = mids[venue]
                self._last_change_ns[venue] = now_ns
        first, second = self.venues
        bps = (mids[first] - mids[second]) / ((mids[first] + mids[second]) / 2) * Decimal("10000")
        self.samples += 1
        self.max_abs_bps = max(self.max_abs_bps, abs(bps))
        self._recent_abs_bps.append(abs(bps))
        if len(self._recent_abs_bps) > self.window:
            self._recent_abs_bps.popleft()
        if abs(bps) <= self.threshold_bps:
            return Divergence(bps, "WITHIN_THRESHOLD")
        delayed = [venue for venue in self.venues if venue in set(delayed)]
        if delayed:
            result = Divergence(bps, "STALE_SIDE", delayed[0])
        else:
            quiet = [venue for venue in self.venues
                     if now_ns - self._last_change_ns.get(venue, now_ns) > self.stall_ns]
            if len(quiet) == 1:
                result = Divergence(bps, "STALLED_VENUE", quiet[0])
            else:
                result = Divergence(bps, "MARKET_DIVERGENCE")
        self.breaches[result.classification] = self.breaches.get(result.classification, 0) + 1
        return result

    def report(self) -> dict:
        ordered = sorted(self._recent_abs_bps)

        def pct(q: float) -> str | None:
            return None if not ordered else str(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * q))])

        return {"venues": list(self.venues), "threshold_bps": str(self.threshold_bps),
                "stall_ns": self.stall_ns, "samples": self.samples, "max_abs_bps": str(self.max_abs_bps),
                "recent_abs_bps_p50": pct(0.5), "recent_abs_bps_p99": pct(0.99),
                "breaches": dict(sorted(self.breaches.items())), "state": self.state}


# ---- W6 (owner decision 5 of 16 Nov; out/integration/designs/W6_design.md, sub-items A and B): dislocation EPISODES, classified, NOTIFY-ONLY ----------------------------------------------
# The gap between the two venues' mids, minus a rolling baseline (one gap value per second over `baseline_window_s`, frozen while an episode is open), opens an episode above `open_bps` and closes
# it at or below `close_bps` held for `close_hold_s` (or after `max_episode_s`: REBASED; or after `max_unobservable_s` with a book not usable: UNOBSERVABLE). Each closed episode is assessed into
# DATA / VENUE_EVENT / TRADEABLE / UNCLASSIFIED from evidence read at open and while open; FAIL_CLOSED_RULE says what a fail-closed rule WOULD do and is a DEFINITION only in this version: nothing
# calls it, no quote, hedge, risk or kill-switch decision reads an episode. Episodes stay OUT of the ledger (their own list and file), so a run with the monitor on differs from one without only
# in the new file and the manifest. This module imports no oms, risk or ems module (a test pins that).
FAIL_CLOSED_RULE = {"DATA": "no new quotes", "UNCLASSIFIED": "no new quotes", "VENUE_EVENT": "no new risk on that venue plus an alarm (any flatten stays manual)", "TRADEABLE": "normal limits"}
DISLOCATION_CLASSES = ("DATA", "VENUE_EVENT", "TRADEABLE", "UNCLASSIFIED")
DISLOCATION_DEFAULTS = {"open_bps": 10.0, "close_bps": 8.0, "close_hold_s": 1.0, "baseline_window_s": 3600.0, "max_episode_s": 600.0, "pair_max_skew_ms": 500.0, "sample_every_s": 1.0,
                        "max_unobservable_s": 5.0, "data_lookback_s": 5.0, "venue_event_min_baseline_n": 30, "size_base": 1.0}


def dislocation_policy(block: dict | None, *, size_base: float | None = None) -> dict | None:
    """The `dislocation` profile block, validated; None when absent or `enabled` is false (nothing is built). `fail_closed.enabled: true` is refused: the rule is defined, not built."""
    if not block or not block.get("enabled", False):
        return None
    allowed = set(DISLOCATION_DEFAULTS) | {"enabled", "fail_closed", "reference", "stall_age_basis", "size_from"}
    unknown = sorted(set(block) - allowed)
    if unknown:
        raise ValueError(f"dislocation: unknown key(s) {unknown}")
    if (block.get("fail_closed") or {}).get("enabled"):
        raise ValueError("dislocation.fail_closed is defined, not built in this version: it cannot be enabled (owner decision 5: notify-only)")
    if (block.get("reference") or {}).get("enabled"):
        raise ValueError("dislocation.reference is not built in this version (W6-C: no independent reference source on this Mac)")
    out = {k: type(v)(block.get(k, v)) for k, v in DISLOCATION_DEFAULTS.items()}
    if size_base is not None and block.get("size_from", "clip") == "clip":
        out["size_base"] = float(size_base)
    if not (out["open_bps"] > out["close_bps"] >= 0) or out["baseline_window_s"] <= 0 or out["max_episode_s"] <= 0:
        raise ValueError("dislocation: open_bps must exceed close_bps (not negative); baseline_window_s and max_episode_s positive")
    return out


@dataclass(slots=True)
class Episode:
    episode_id: int
    venues: tuple[str, str]
    start_ns: int
    baseline_bps: float | None                 # None while the baseline is still WARMING_UP
    gap_open_bps: float
    gap_extreme_bps: float
    deviation_extreme_bps: float
    end_ns: int | None = None
    end_reason: str = ""
    gap_close_bps: float | None = None
    rich_venue: str = ""
    unobservable_ns: int = 0
    evidence: dict = field(default_factory=dict)
    executable: dict = field(default_factory=dict)
    assessment: dict = field(default_factory=dict)

    def record(self) -> dict:
        return {"episode_id": self.episode_id, "venues": list(self.venues), "start_ns": self.start_ns, "end_ns": self.end_ns,
                "duration_s": None if self.end_ns is None else (self.end_ns - self.start_ns) / 1e9, "end_reason": self.end_reason,
                "baseline_bps": self.baseline_bps, "baseline": "WARMING_UP" if self.baseline_bps is None else "READY", "gap_open_bps": self.gap_open_bps, "gap_extreme_bps": self.gap_extreme_bps,
                "gap_close_bps": self.gap_close_bps, "deviation_extreme_bps": self.deviation_extreme_bps, "rich_venue": self.rich_venue, "unobservable_s": self.unobservable_ns / 1e9,
                "evidence": dict(self.evidence), "executable": dict(self.executable), **self.assessment, "decision": "NOTIFY_ONLY"}


def assess_episode(episode: Episode, evidence: dict, *, durations_non_data: list[float], min_baseline_n: int) -> dict:
    """The class of a closed episode (pure). DATA: any feed or clock fault in the lookback or while open, a book not usable, a sequence gap, OWN_LAG, an unobservable stretch, or no feed-health
    monitor at all. VENUE_EVENT: not DATA, longer than the p99 of this run's non-DATA durations (at least `min_baseline_n` of them) AND a venue-side signal (mark gap); with no venue signal it is
    UNCLASSIFIED (NO_VENUE_SIGNAL), never VENUE_EVENT by persistence alone. TRADEABLE: not DATA or VENUE_EVENT, both books valid at open and a positive taker/taker net edge at the clip size.
    Anything else UNCLASSIFIED, which the fail-closed definition treats as DATA."""
    reasons = []
    if not evidence.get("feed_health_monitor"):
        reasons.append("NO_FEED_HEALTH_MONITOR")
    if evidence.get("feed_not_healthy"):
        reasons.append("FEED_NOT_HEALTHY")
    if evidence.get("clock_not_synced"):
        reasons.append("HOST_CLOCK_NOT_SYNCED")
    if evidence.get("book_not_valid"):
        reasons.append("BOOK_NOT_VALID")
    if evidence.get("sequence_gaps"):
        reasons.append("SEQUENCE_GAP")
    if evidence.get("own_lag"):
        reasons.append("OWN_LAG")
    if evidence.get("feed_delayed"):
        reasons.append("FEED_DELAYED")
    if episode.unobservable_ns > 0 or episode.end_reason == "UNOBSERVABLE":
        reasons.append("UNOBSERVABLE")
    if reasons:
        cls = "DATA" if reasons != ["NO_FEED_HEALTH_MONITOR"] else "UNCLASSIFIED"
        return {"class": cls, "reason_codes": reasons, "would_be_action": FAIL_CLOSED_RULE[cls], "confidence": "RULE"}
    duration = (episode.end_ns - episode.start_ns) / 1e9 if episode.end_ns is not None else 0.0
    ordered = sorted(durations_non_data)
    p99 = ordered[min(len(ordered) - 1, int((len(ordered) - 1) * 0.99))] if ordered else None
    persistent = p99 is not None and len(ordered) >= min_baseline_n and duration > p99
    if persistent:
        if evidence.get("venue_signal"):
            return {"class": "VENUE_EVENT", "reason_codes": ["PERSISTENT_BEYOND_P99", "VENUE_SIGNAL"], "would_be_action": FAIL_CLOSED_RULE["VENUE_EVENT"], "confidence": "RULE",
                    "persistence": {"duration_s": duration, "p99_s": p99, "baseline_n": len(ordered)}}
        return {"class": "UNCLASSIFIED", "reason_codes": ["PERSISTENT_BEYOND_P99", "NO_VENUE_SIGNAL"], "would_be_action": FAIL_CLOSED_RULE["UNCLASSIFIED"], "confidence": "RULE",
                "persistence": {"duration_s": duration, "p99_s": p99, "baseline_n": len(ordered)}}
    net = (episode.executable or {}).get("net_edge_bps_at_open")
    if net is not None and net > 0 and evidence.get("books_valid_at_open"):
        return {"class": "TRADEABLE", "reason_codes": ["NET_EDGE_AT_SIZE_POSITIVE"], "would_be_action": FAIL_CLOSED_RULE["TRADEABLE"], "confidence": "RULE"}
    why = "NET_EDGE_NOT_POSITIVE" if net is not None else "NO_EXECUTABLE_PRICE_AT_SIZE"
    return {"class": "UNCLASSIFIED", "reason_codes": [why], "would_be_action": FAIL_CLOSED_RULE["UNCLASSIFIED"], "confidence": "RULE"}


def executable_at_size(books: dict[str, OrderBook], venues: tuple[str, str], size: Decimal, taker_bps: dict[str, float]) -> dict:
    """Executable prices at `size` on both venues (`OrderBook.walk`, a size-weighted average; null with a reason when the side does not hold the size) and the net edge of the taker/taker cross:
    sell the rich venue's bid side, buy the cheap venue's ask side, both venues' taker fees. Read-only."""
    from .models import Side
    out: dict = {"size_base": str(size)}
    prices = {}
    for venue in venues:
        book = books.get(venue)
        if book is None or not book.bids or not book.asks:
            out[venue] = {"buy": None, "sell": None, "reason": "NO_BOOK"}
            continue
        buy_filled, buy_avg = book.walk(Side.BUY, size, Decimal("Infinity"))
        sell_filled, sell_avg = book.walk(Side.SELL, size, Decimal("0"))
        prices[venue] = (buy_avg if buy_filled >= size else None, sell_avg if sell_filled >= size else None)
        out[venue] = {"buy": None if prices[venue][0] is None else str(prices[venue][0]), "sell": None if prices[venue][1] is None else str(prices[venue][1]),
                      **({"reason": "INSUFFICIENT_DEPTH"} if None in prices[venue] else {})}
    net = None
    if len(prices) == 2 and all(None not in v for v in prices.values()):
        a, b = venues
        best = None
        for rich, cheap in ((a, b), (b, a)):
            sell, buy = prices[rich][1], prices[cheap][0]
            gross = float((sell - buy) / ((sell + buy) / 2) * 10000)
            edge = gross - float(taker_bps.get(rich, 0.0)) - float(taker_bps.get(cheap, 0.0))
            best = edge if best is None else max(best, edge)
        net = best
    out["net_edge_bps_at_open"] = net
    return out


class DislocationMonitor:
    """The episode monitor (see the block comment above). `observe(books, venue, now_ns, health)` after a book event of `venue`; `health()` returns the DATA evidence of the moment
    ({"feed_health_monitor", "feed_not_healthy", "clock_not_synced", "own_lag", "feed_delayed"}); `on_derivative(ticker)` keeps the venues' marks (the only venue signal there is)."""

    def __init__(self, venues: tuple[str, str], policy: dict, *, taker_bps: dict[str, float] | None = None) -> None:
        self.venues, self.policy = tuple(venues), policy
        self.taker_bps = dict(taker_bps or {})
        self.size = Decimal(str(policy["size_base"]))
        self.episodes: list[Episode] = []
        self.open: Episode | None = None
        self._mid: dict[str, tuple[int, float]] = {}
        self._per_second: deque = deque()           # (second, gap) one per second
        self._last_second: int | None = None
        self._baseline: float | None = None
        self._frozen_until_ns = 0
        self._close_since_ns: int | None = None
        self._unobservable_since: int | None = None
        self._last_sample_ns = 0
        self._next_id = 0
        self._marks: dict[str, tuple[int, float]] = {}
        self._data_marks_ns: deque = deque(maxlen=10_000)      # times at which DATA evidence was seen (for the lookback)
        self.samples = 0
        self.unobservable_ns_total = 0

    # -- inputs
    def on_derivative(self, ticker) -> None:
        if getattr(ticker, "mark_price", None) is not None:
            self._marks[ticker.venue] = (ticker.receive_ts_ns, float(ticker.mark_price))

    def _baseline_now(self, now_ns: int) -> float | None:
        if not self._per_second:
            return None
        span = self._per_second[-1][0] - self._per_second[0][0]
        if span < self.policy["baseline_window_s"] * 0.5:                # WARMING_UP until half the window is covered
            return None
        values = sorted(g for _, g in self._per_second)
        return values[len(values) // 2]

    def observe(self, books: dict[str, OrderBook], venue: str, now_ns: int, health) -> None:
        book = books.get(venue)
        usable = book is not None and book.state == BookState.VALID and book.bids and book.asks
        if usable:
            self._mid[venue] = (now_ns, float((book.best_bid + book.best_ask) / 2))
        evidence = health()
        bad_data = bool(evidence.get("feed_not_healthy") or evidence.get("clock_not_synced") or evidence.get("own_lag") or evidence.get("feed_delayed"))
        if bad_data:
            self._data_marks_ns.append(now_ns)
        a, b = self.venues
        skew = self.policy["pair_max_skew_ms"] * 1e6
        if not usable or a not in self._mid or b not in self._mid or abs(self._mid[a][0] - self._mid[b][0]) > skew:
            if self.open is not None:
                self._unobservable_since = self._unobservable_since or now_ns
                if now_ns - self._unobservable_since > self.policy["max_unobservable_s"] * 1e9:
                    self._close(now_ns, "UNOBSERVABLE", None, books, evidence)
            return
        if self._unobservable_since is not None and self.open is not None:
            self.open.unobservable_ns += now_ns - self._unobservable_since
            self.unobservable_ns_total += now_ns - self._unobservable_since
        self._unobservable_since = None
        ma, mb = self._mid[a][1], self._mid[b][1]
        gap = (ma - mb) / ((ma + mb) / 2) * 10000
        self.samples += 1
        second = now_ns // 1_000_000_000
        if second != self._last_second:
            self._last_second = second
            if self.open is None and now_ns >= self._frozen_until_ns and not bad_data:           # one gap value per second; frozen during an episode; DATA seconds left out
                self._per_second.append((second, gap))
                while self._per_second and second - self._per_second[0][0] > self.policy["baseline_window_s"]:
                    self._per_second.popleft()
                self._baseline = self._baseline_now(now_ns)
        base = self._baseline
        deviation = gap - (base or 0.0)
        if self.open is None:
            if abs(deviation) > self.policy["open_bps"]:
                self._next_id += 1
                ep = Episode(self._next_id, self.venues, now_ns, base, gap, gap, deviation, rich_venue=a if deviation > 0 else b)
                ep.evidence = {"feed_health_monitor": bool(evidence.get("feed_health_monitor")), "books_valid_at_open": True,
                               "data_in_lookback": any(now_ns - t <= self.policy["data_lookback_s"] * 1e9 for t in self._data_marks_ns),
                               "gaps_at_open": {v: (books[v].stats.gaps if v in books else None) for v in self.venues}}
                for key in ("feed_not_healthy", "clock_not_synced", "own_lag", "feed_delayed"):
                    ep.evidence[key] = bool(evidence.get(key)) or (key == "feed_not_healthy" and ep.evidence["data_in_lookback"])
                ep.executable = executable_at_size(books, self.venues, self.size, self.taker_bps)
                self.open, self._close_since_ns, self._last_sample_ns = ep, None, now_ns
            return
        ep = self.open
        for key in ("feed_not_healthy", "clock_not_synced", "own_lag", "feed_delayed"):
            if evidence.get(key):
                ep.evidence[key] = True
        if abs(deviation) > abs(ep.deviation_extreme_bps):
            ep.deviation_extreme_bps, ep.gap_extreme_bps = deviation, gap
        if abs(deviation) <= self.policy["close_bps"]:
            self._close_since_ns = self._close_since_ns or now_ns
            if now_ns - self._close_since_ns >= self.policy["close_hold_s"] * 1e9:
                self._close(now_ns, "RECOVERED", gap, books, evidence)
                return
        else:
            self._close_since_ns = None
        if now_ns - ep.start_ns > self.policy["max_episode_s"] * 1e9:
            self._close(now_ns, "REBASED", gap, books, evidence)
            self._per_second.clear()                                             # the baseline restarts (WARMING_UP)
            self._baseline = None

    def _close(self, now_ns: int, reason: str, gap: float | None, books, evidence) -> None:
        ep = self.open
        ep.end_ns, ep.end_reason, ep.gap_close_bps = now_ns, reason, gap
        ep.evidence["gaps_during"] = {v: ((books[v].stats.gaps if v in books else 0) - (ep.evidence["gaps_at_open"].get(v) or 0)) for v in self.venues}
        ep.evidence["sequence_gaps"] = any(n and n > 0 for n in ep.evidence["gaps_during"].values())
        ep.evidence["book_not_valid"] = reason == "UNOBSERVABLE"
        ep.evidence["venue_signal"] = self._venue_signal(now_ns)
        non_data = [(e.end_ns - e.start_ns) / 1e9 for e in self.episodes if e.assessment.get("class") not in ("DATA",) and e.end_ns is not None]
        ep.assessment = assess_episode(ep, ep.evidence, durations_non_data=non_data, min_baseline_n=int(self.policy["venue_event_min_baseline_n"]))
        self.episodes.append(ep)
        self.open = None
        self._frozen_until_ns = now_ns + int(self.policy["close_hold_s"] * 1e9)

    def _venue_signal(self, now_ns: int) -> dict | None:
        fresh = {v: m for v, (t, m) in self._marks.items() if now_ns - t <= 5_000_000_000}
        if len(fresh) < 2:
            return None
        a, b = self.venues
        if a not in fresh or b not in fresh:
            return None
        return {"mark_gap_bps": (fresh[a] - fresh[b]) / ((fresh[a] + fresh[b]) / 2) * 10000}

    def finish(self, now_ns: int, books, health) -> None:
        if self.open is not None:
            self._close(now_ns, "END_OF_RUN", None, books, health())

    def report(self) -> dict:
        by_class: dict[str, int] = {c: 0 for c in DISLOCATION_CLASSES}
        buckets = {"<1s": 0, "1-10s": 0, "10-60s": 0, "60-600s": 0, ">=600s": 0}
        for ep in self.episodes:
            by_class[ep.assessment.get("class", "UNCLASSIFIED")] += 1
            d = (ep.end_ns - ep.start_ns) / 1e9
            buckets["<1s" if d < 1 else "1-10s" if d < 10 else "10-60s" if d < 60 else "60-600s" if d < 600 else ">=600s"] += 1
        return {"label": "NOTIFY-ONLY: dislocation episodes are recorded and classed; nothing reads them (no quote, hedge, risk or kill-switch decision)", "policy": dict(self.policy),
                "episodes": len(self.episodes), "by_class": by_class, "by_duration": buckets, "samples": self.samples, "unobservable_s": self.unobservable_ns_total / 1e9,
                "venue_signal_available": bool(self._marks), "fail_closed_rule": dict(FAIL_CLOSED_RULE), "fail_closed_enabled": False,
                "data_limit": None if self._marks else "no derivatives (mark) stream: VENUE_EVENT cannot be assigned; persistent non-DATA episodes are UNCLASSIFIED (NO_VENUE_SIGNAL)"}


def false_alarm_report(episodes: list[dict], signal_ts_s: list[float]) -> dict:
    """Post-hoc (not live): how often the fail-closed DEFINITION would have blocked, from an episodes file and the run's signal times. Counts only, no P&L."""
    blocking = [(e["start_ns"] / 1e9, e["end_ns"] / 1e9) for e in episodes if e.get("class") != "TRADEABLE" and e.get("end_ns") is not None]
    return {"episodes": len(episodes), "would_block_episodes": len(blocking), "would_block_seconds": sum(b - a for a, b in blocking),
            "signals_inside_would_block_windows": sum(1 for s in signal_ts_s if any(a <= s <= b for a, b in blocking))}


def dislocation_scan(events, policy: dict, *, feed_policy=None, taker_bps: dict[str, float] | None = None, venues: tuple[str, str] | None = None) -> dict:
    """READ-ONLY over recorded canonical book events (no session, no decision): the same monitor, with a feed-health monitor when `feed_policy` is given (else every episode is UNCLASSIFIED or DATA
    by the definition). Returns the report and the episode records."""
    from .feed_health import FeedHealthMonitor
    from .orderbook import OrderBook as _Book
    books: dict[str, _Book] = {}
    fh = FeedHealthMonitor(feed_policy) if feed_policy is not None else None
    monitor = None
    last = 0
    for event in events:
        if getattr(event, "event_type", None) is None:
            continue
        book = books.setdefault(event.venue, _Book(event.venue, event.instrument_id))
        book.apply(event)
        now = event.receive_ts_ns
        last = now
        if fh is not None:
            fh.observe(event.venue, event.instrument_id, "book", receive_ts_ns=now, now_ns=now, corrected_delay_ns=None)
        if monitor is None and len(books) >= 2:
            monitor = DislocationMonitor(venues or tuple(sorted(books))[:2], policy, taker_bps=taker_bps)
        if monitor is not None:
            monitor.observe(books, event.venue, now, lambda: {"feed_health_monitor": fh is not None, "feed_not_healthy": fh is not None and fh.worst()[0] != "HEALTHY"})
    if monitor is None:
        return {"episodes": 0, "records": []}
    monitor.finish(last, books, lambda: {"feed_health_monitor": fh is not None})
    return {**monitor.report(), "records": [e.record() for e in monitor.episodes]}
