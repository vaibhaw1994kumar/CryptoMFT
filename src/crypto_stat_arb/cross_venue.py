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
