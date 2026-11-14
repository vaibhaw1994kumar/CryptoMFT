"""Lightweight per-venue market state.

EmpiricalFillModel (altcoin_mm_v1/fill_model.py) takes a MarketState, but only
reads four of its ~35 fields: top_depth, top_depth_baseline, trade_sign and
spread_bps. Running the full altcoin_mm_v1 MarketStateBuilder per venue would
compute VPIN, whale detection, split detection, book features and short-vol on
every event of a two-venue replay — for four numbers.

So this module tracks exactly what is needed, cheaply, and emits a MarketState
with the real fields populated and the rest at defaults. LambdaRegimeEngine is
used as-is: it is a deque plus an EWMA, and it gives genuine calm/elevated/toxic
classification that the fill model's priors are conditioned on.

It also carries the toxicity signals the arb gate needs — trade burst intensity
and depth collapse — which are the two conditions under which a visible
cross-venue spread is usually not an opportunity but a venue about to reprice.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from .lambda_regime import LambdaRegimeEngine
from .models import LambdaBucket, LambdaRegime, MarketState
from .order_book import OrderBook


@dataclass
class VenueStateConfig:
    flow_window_s: float = 2.0
    depth_baseline_alpha: float = 0.02      # EWMA over top-of-book depth
    vol_alpha: float = 0.05                 # EWMA over squared mid returns
    burst_window_s: float = 1.0
    # Lambda = |mid move| / |signed flow|. Evaluated per book delta, the flow
    # denominator is ~0 almost always and any mid tick saturates lambda at its
    # cap, pinning the regime at TOXIC. So it is updated on an interval, on the
    # return accumulated since the last update, and only when flow was nonzero.
    lambda_update_interval_s: float = 0.25
    # Depth quality (owner 14 Nov; core, OFF by default): the gap-depth and flicker fields the EMS routing evidence and choose_tactic read. With depth_quality False nothing below is computed, called or stored.
    # EVERY number is ASSUMED (a starting point, not a fit) until measured on real fills. L2 at 100 ms updates: both fields are APPROXIMATE, never a book reconstruction.
    depth_quality: bool = False
    dq_top_levels: int = 5                   # flicker: the best N levels of each side
    dq_flicker_window_s: float = 10.0        # flicker: rolling window of unexplained depth decrease and traded volume
    dq_match_window_s: float = 0.05          # a decrease and a print at one level that are within this many seconds explain each other: the depletion ledger's own window (MakerConfig.dedupe_window_s)
    dq_flicker_min_volume: float = 1.0       # base units: the traded volume in the denominator is never below this (a window with no prints and a vanished level reads as high flicker, not as undefined)
    dq_gap_stretch_bps: float = 3.0          # a near-empty stretch longer than this many bps of the touch price is a gap
    dq_gap_level_size: float = 0.0           # base units: a level smaller than this counts as near-empty (0 = only an absent price is empty)
    dq_gap_scan_levels: int = 200            # levels read from the touch outward (the capture's own depth bounds it: Bybit 50, Binance 1000 on 2026-10-29)
    dq_feed_fault_window_s: float = 2.0      # a gap seen this soon after a sequence gap on the venue's feed is FEED_FAULT, not a cliff


class VenueState:
    """Rolling trade-flow and depth statistics for one venue."""

    def __init__(self, venue: str, config: VenueStateConfig | None = None) -> None:
        self.venue = venue
        self.config = config or VenueStateConfig()
        self.lambda_engine = LambdaRegimeEngine()

        self._trades: deque[tuple[float, float, int]] = deque()   # (ts, volume, sign)
        self._level_window_s = 0.0                                # E3 participation limit: 0 = per-level volume is not tracked
        self._level_trades: dict[float, deque] = {}               # price -> (ts, volume) prints inside the window
        self.top_depth_baseline: float = 0.0
        self.short_vol: float = 0.0
        self.last_mid: float = 0.0
        self.last_mid_ts: float = 0.0
        self.trades_seen: int = 0
        self.last_regime: LambdaRegime = LambdaRegime(0.0, 0.0, LambdaBucket.CALM, 0.0)
        self._lambda_ref_mid: float = 0.0
        self._lambda_last_ts: float = 0.0
        self.lambda_updates: int = 0
        self._dq_on = bool(getattr(self.config, "depth_quality", False))
        if self._dq_on:
            self._dq_init()

    # ── Updates ──────────────────────────────────────────────────────────────

    def on_trade(self, ts: float, price: float, volume: float, sign: int) -> None:
        self._trades.append((ts, volume, sign))
        self.trades_seen += 1
        if self._dq_on:
            self._dq_trade(ts, price, volume, sign)
        self._trim(ts)
        if self._level_window_s > 0:
            key = round(price, 9)
            dq = self._level_trades.get(key)
            if dq is None:
                dq = self._level_trades[key] = deque()
            dq.append((ts, volume))
            cutoff = ts - self._level_window_s
            while dq and dq[0][0] < cutoff:
                dq.popleft()
            if len(self._level_trades) > 4000:                       # drop the levels that have gone quiet, so the map stays small over a day
                for k in [k for k, d in self._level_trades.items() if not d or d[-1][0] < cutoff]:
                    del self._level_trades[k]

    # Public volume traded at one price level over a recent window (E3 participation limit, owner 13 Nov). Off until a strategy asks for a window.
    def track_level_volume(self, window_s: float) -> None:
        self._level_window_s = max(self._level_window_s, float(window_s))

    def level_volume(self, price: float, now: float, window_s: float) -> float:
        """Public volume (both aggressor sides) printed at exactly `price` in the last `window_s` seconds up to `now`."""
        dq = self._level_trades.get(round(price, 9))
        if not dq:
            return 0.0
        cutoff = now - window_s
        return sum(v for t, v in dq if t >= cutoff)

    def _trim(self, now: float) -> None:
        cutoff = now - self.config.flow_window_s
        while self._trades and self._trades[0][0] < cutoff:
            self._trades.popleft()

    def on_book(self, book: OrderBook, ts: float) -> None:
        if not book.ready:
            return
        if self._dq_on:
            self._dq_book(book, ts)
        depth = book.best_bid_size + book.best_ask_size
        a = self.config.depth_baseline_alpha
        self.top_depth_baseline = depth if self.top_depth_baseline <= 0 else a * depth + (1 - a) * self.top_depth_baseline

        mid = book.mid
        if self.last_mid > 0 and mid > 0:
            ret = (mid - self.last_mid) / self.last_mid
            av = self.config.vol_alpha
            self.short_vol = math.sqrt(av * ret * ret + (1 - av) * self.short_vol ** 2)

        if self._lambda_ref_mid <= 0 and mid > 0:
            self._lambda_ref_mid = mid
            self._lambda_last_ts = ts
        elif mid > 0 and ts - self._lambda_last_ts >= self.config.lambda_update_interval_s:
            self._trim(ts)
            flow = self.signed_volume
            if flow != 0.0:
                # Realised move over the interval against the signed flow that
                # arrived in it: price impact per unit of flow, which is what
                # separates a calm book from one being run over.
                self.lambda_engine.update(
                    realized_mid_move=(mid - self._lambda_ref_mid) / self._lambda_ref_mid,
                    signed_flow=flow,
                    regime_pressure=self.depth_collapse(book),
                    spread_bps=book.spread_bps,
                )
                self.last_regime = self.lambda_engine.current_regime()
                self.lambda_updates += 1
            self._lambda_ref_mid = mid
            self._lambda_last_ts = ts

        self.last_mid = mid
        self.last_mid_ts = ts

    # ── Depth quality: gap depth and flicker (owner 14 Nov; opt-in, VenueStateConfig.depth_quality) ───────
    DQ_APPROX = "APPROXIMATE: L2 depth at the feed's update rate (100 ms on these captures); a decrease is split by the depletion ledger's rule, not observed per order"

    def _dq_init(self) -> None:
        self._dq_prev: dict[str, dict[float, float]] = {"bid": {}, "ask": {}}
        self._dq_credit: dict[str, dict[tuple, deque]] = {"delta": {}, "trade": {}}      # the depletion ledger's two-way credit, per (side, price): a decrease waiting for a print, a print waiting for a decrease
        self._dq_pending: dict[str, deque] = {"bid": deque(), "ask": deque()}            # book-first credits in arrival order: one that outlives the match window is an UNEXPLAINED decrease
        self._dq_unexplained: dict[str, deque] = {"bid": deque(), "ask": deque()}        # (ts, volume) of decreases no print explained
        self._dq_traded: dict[str, deque] = {"bid": deque(), "ask": deque()}             # (ts, volume) of prints that deplete this side (a sell aggressor hits the bids)
        self._dq_first_ts: float | None = None
        self._dq_last_feed_gap_ts: float | None = None
        self.dq_stats: dict[str, float] = {}                 # print_volume_netted / decrease_volume_netted / unexplained_volume: the same names the ledger's ledger_stats uses

    def _dq_note(self, key: str, v: float) -> None:
        self.dq_stats[key] = self.dq_stats.get(key, 0.0) + v

    def _dq_take(self, kind: str, key: tuple, now: float, want: float) -> float:
        credits = self._dq_credit[kind].get(key)
        if not credits:
            return 0.0
        horizon = now - self.config.dq_match_window_s
        while credits and credits[0][0] < horizon:
            credits.popleft()
        taken = 0.0
        while credits and taken < want - 1e-12:
            use = min(credits[0][1], want - taken)
            taken += use
            credits[0][1] -= use
            if credits[0][1] <= 1e-12:
                credits.popleft()
        if not credits:
            del self._dq_credit[kind][key]
        return taken

    def _dq_add(self, kind: str, key: tuple, now: float, amount: float) -> list | None:
        if amount <= 1e-12:
            return None
        book = self._dq_credit[kind]
        if len(book) > 4096:
            horizon = now - self.config.dq_match_window_s
            for k in [k for k, d in book.items() if not d or d[-1][0] < horizon]:
                del book[k]
        entry = [now, amount]
        book.setdefault(key, deque(maxlen=64)).append(entry)
        return entry

    def _dq_sweep(self, now: float) -> None:
        """A book-first credit older than the match window is a decrease no print explained: it joins the unexplained total (at its own time). Called on every update and query, so the figure lags by one window at most."""
        horizon = now - self.config.dq_match_window_s
        keep = now - self.config.dq_flicker_window_s - self.config.dq_match_window_s
        for side in ("bid", "ask"):
            pend = self._dq_pending[side]
            while pend and pend[0][0] < horizon:
                entry = pend.popleft()
                if entry[1] > 1e-12:
                    self._dq_unexplained[side].append((entry[0], entry[1]))
                    self._dq_note("unexplained_volume", entry[1])
                    entry[1] = 0.0
            for dq in (self._dq_unexplained[side], self._dq_traded[side]):
                while dq and dq[0][0] < keep:
                    dq.popleft()

    def _dq_trade(self, ts: float, price: float, volume: float, sign: int) -> None:
        side = "bid" if sign < 0 else "ask"
        if self._dq_first_ts is None:
            self._dq_first_ts = ts
        self._dq_sweep(ts)
        key = (side, round(price, 8))
        credited = self._dq_take("delta", key, ts, volume)            # book-first: the decrease that already happened at this level was this print
        if credited > 1e-12:
            self._dq_note("print_volume_netted", credited)
        self._dq_add("trade", key, ts, volume - credited)             # the rest waits for a decrease still to come
        self._dq_traded[side].append((ts, volume))

    def _dq_book(self, book, ts: float) -> None:
        cfg = self.config
        if self._dq_first_ts is None:
            self._dq_first_ts = ts
        self._dq_sweep(ts)
        n = max(1, int(cfg.dq_top_levels))
        for side in ("bid", "ask"):
            cur = dict(book.levels(side, n))
            prev = self._dq_prev[side]
            if prev and cur:
                far = min(cur) if side == "bid" else max(cur)          # the worst level still inside the window; a level that left it moved out of the top N, it did not shrink
                full = len(cur) >= n
                for price, old in prev.items():
                    new = cur.get(price)
                    if new is None:
                        if full and ((price < far) if side == "bid" else (price > far)):
                            continue
                        dec = old
                    elif new < old - 1e-12:
                        dec = old - new
                    else:
                        continue
                    key = (side, round(price, 8))
                    explained = self._dq_take("trade", key, ts, dec)        # trade-first: the print already happened
                    if explained > 1e-12:
                        self._dq_note("decrease_volume_netted", explained)
                    entry = self._dq_add("delta", key, ts, dec - explained)
                    if entry is not None:
                        self._dq_pending[side].append(entry)
            self._dq_prev[side] = cur

    def note_feed_gap(self, ts: float) -> None:
        """The venue's feed reported a sequence gap at `ts`. A depth gap seen soon after is a feed fault, not a cliff. No effect with depth_quality off."""
        if self._dq_on:
            self._dq_last_feed_gap_ts = ts

    def flicker(self, side: str, now: float) -> float | None:
        """Unexplained decrease of the best N levels of `side` over the rolling window, divided by the volume printed against that side in it (never less than dq_flicker_min_volume). None before one window of data
        has been seen, with depth_quality off, or when the side has no book. APPROXIMATE (see DQ_APPROX): a decrease still waiting for its print is not counted until the match window has passed."""
        if not self._dq_on or self._dq_first_ts is None:
            return None
        cfg = self.config
        if now - self._dq_first_ts < cfg.dq_flicker_window_s:
            return None
        self._dq_sweep(now)
        cut = now - cfg.dq_flicker_window_s
        unexplained = sum(v for t, v in self._dq_unexplained[side] if t >= cut)
        traded = sum(v for t, v in self._dq_traded[side] if t >= cut)
        return unexplained / max(traded, cfg.dq_flicker_min_volume)

    def gap_depth(self, book, side: str, now: float) -> dict:
        """The size resting between the touch and the first near-empty stretch of `side` (the side an order would CONSUME: asks for a buy), and where that stretch starts.
        status GAP                    depth = size before the gap, distance_bps = from the touch to the last solid level before it
              NO_GAP_WITHIN_CAPTURE   no gap in the levels the capture holds: the book's edge is not a cliff; depth None
              FEED_FAULT              a gap, but the feed reported a sequence gap within dq_feed_fault_window_s (or the book is recovering): not a cliff; depth None
              NOT_EVALUABLE           fewer than two levels, or depth_quality off; depth None
        A level is solid when its size is at least dq_gap_level_size; the stretch between two solid levels is measured in bps of the touch price, whatever the venue's tick."""
        out = {"status": "NOT_EVALUABLE", "depth": None, "distance_bps": None, "levels": 0}
        if not self._dq_on:
            return out
        cfg = self.config
        lv = book.levels(side, max(2, int(cfg.dq_gap_scan_levels)))
        out["levels"] = len(lv)
        if len(lv) < 2:
            return out
        touch = lv[0][0]
        solid_px, cum = touch, 0.0
        found = None
        for price, size in lv:
            if size >= cfg.dq_gap_level_size:
                if abs(price - solid_px) / touch * 1e4 > cfg.dq_gap_stretch_bps:
                    found = (cum, abs(solid_px - touch) / touch * 1e4)
                    break
                solid_px = price
            cum += size
        if found is None:
            out["status"] = "NO_GAP_WITHIN_CAPTURE"
            out["captured_depth"] = cum
            return out
        last = self._dq_last_feed_gap_ts
        if getattr(book, "state", "valid") == "recovering" or (last is not None and 0.0 <= now - last <= cfg.dq_feed_fault_window_s):
            out["status"] = "FEED_FAULT"
            return out
        out.update(status="GAP", depth=found[0], distance_bps=found[1])
        return out

    def depth_quality(self, book, now: float, side: str | None = None) -> dict | None:
        """One decision's evidence: gap depth and flicker for `side` ("bid" / "ask"; both when None), labelled APPROXIMATE. None when depth_quality is off (the caller then writes nothing)."""
        if not self._dq_on:
            return None
        sides = ("bid", "ask") if side is None else (side,)
        return {"approx": self.DQ_APPROX, "gap": {s: self.gap_depth(book, s, now) for s in sides}, "flicker": {s: self.flicker(s, now) for s in sides}}

    # ── Derived signals ──────────────────────────────────────────────────────

    @property
    def signed_volume(self) -> float:
        return sum(v * s for _, v, s in self._trades)

    def signed_volume_within(self, now: float, window_s: float) -> float:
        """Signed volume (+ = buy aggressor) of the prints in [now - window_s, now]; the held window is `flow_window_s`, so a longer ask sees only what is held."""
        cut = now - window_s
        return sum(v * s for t, v, s in self._trades if cut <= t <= now)

    @property
    def total_volume(self) -> float:
        return sum(v for _, v, _ in self._trades)

    @property
    def trade_sign(self) -> float:
        """Signed volume share in [-1, 1]. +1 = every recent trade lifted offers."""
        total = self.total_volume
        return 0.0 if total <= 0 else self.signed_volume / total

    @property
    def trade_rate(self) -> float:
        return len(self._trades) / max(self.config.flow_window_s, 1e-9)

    def depth_collapse(self, book: OrderBook) -> float:
        """Fraction of baseline top-of-book depth that has vanished, in [0, 1].

        High values mean makers pulled. A spread that opens at the same moment
        depth collapses is not an arb — it is the other venue about to follow.
        """
        if self.top_depth_baseline <= 0 or not book.ready:
            return 0.0
        depth = book.best_bid_size + book.best_ask_size
        return max(0.0, min(1.0, 1.0 - depth / self.top_depth_baseline))

    def is_toxic(self, book: OrderBook, trade_sign_threshold: float = 0.75,
                 depth_collapse_threshold: float = 0.60) -> bool:
        """Directional sweep in progress on this venue."""
        if self.last_regime.bucket == LambdaBucket.TOXIC:
            return True
        return (
            abs(self.trade_sign) > trade_sign_threshold
            and self.depth_collapse(book) > depth_collapse_threshold
        )

    # ── MarketState for EmpiricalFillModel ───────────────────────────────────

    def to_market_state(self, book: OrderBook, ts: float) -> MarketState:
        """Populate the four fields EmpiricalFillModel reads; default the rest.

        Fields left at zero are genuinely unused by the fill model — this is not
        an approximation of them, it is an explicit statement that they do not
        enter the computation.
        """
        top_depth = book.best_bid_size + book.best_ask_size
        mid = book.mid
        spread_bps = book.spread_bps
        return MarketState(
            timestamp=ts,
            best_bid=book.best_bid,
            best_ask=book.best_ask,
            bid_size=book.best_bid_size,
            ask_size=book.best_ask_size,
            mid=mid,
            microprice=book.microprice,
            spread_bps=spread_bps,                      # read: spread haircut
            imbalance=book.imbalance(1),
            top5_imbalance=book.imbalance(5),
            depth_weighted_imbalance=book.imbalance(5),
            spread_pct=spread_bps / 100.0,
            microprice_deviation_bps=0.0,
            microprice_drift_bps=0.0,
            short_vol=self.short_vol,
            short_vol_bps=self.short_vol * 10_000.0,
            trade_sign=self.trade_sign,                 # read: flow bucket
            impact_trade_sign=self.trade_sign,
            flow_toxicity=abs(self.trade_sign),
            recent_trade_rate=self.trade_rate,
            recent_signed_volume=self.signed_volume,
            trade_burst_flag=False,
            trade_cluster_score=0.0,
            depth_collapse=self.depth_collapse(book),
            liquidity_evaporation=0.0,
            cancel_burst=0.0,
            cancel_imbalance=0.0,
            replenish_imbalance=0.0,
            queue_fragility=0.0,
            book_hole=False,
            recent_sweep_flag=False,
            sweep_risk=False,
            top_depth=top_depth,                        # read: queue + depth bucket
            top_depth_baseline=max(self.top_depth_baseline, 1e-9),   # read: depth bucket
            total_depth=book.total_depth(5),
            tick_size=0.0,
        )

    def regime(self) -> LambdaRegime:
        return self.last_regime
