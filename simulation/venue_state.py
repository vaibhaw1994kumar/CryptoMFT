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
    # Step B (owner 14 Nov, execution cost model). A tick is THIN when its level is absent or smaller than dq_thin_ratio (g) x the rolling median level size at that distance (same venue, side, distance bucket);
    # a GAP is dq_gap_min_ticks (m) or more consecutive thin ticks, or a jump of more than dq_gap_stretch_bps between two displayed levels. All ASSUMED.
    dq_thin_ratio: float = 0.25              # g
    dq_gap_min_ticks: int = 3                # m (needs the venue tick: VenueState.set_tick; without it only the jump rule and the absolute dq_gap_level_size apply)
    dq_gap_min_bps: float = 0.0              # ASSUMED (the T1 / T5 runs use 3.0 and report 2 and 5 as sensitivity). 0 = off. Above 0 the run length m is at least this many bps of the touch price, in the venue's own ticks (a run of 3 ticks is 0.2 bps on a 0.001 tick and 3 bps on a 0.01 tick: m is tick-dependent, this makes it a price distance)
    dq_gap_within_bps: float = 10.0          # N: gaps are counted within this many bps of the touch
    dq_gap_eval_s: float = 0.1               # the gap scan (and its persistence tracking) runs at most this often
    dq_median_every_s: float = 1.0           # the rolling median level sizes are updated at most this often
    dq_median_eta: float = 0.02              # geometric step of the median tracker per sample (about 35 samples to halve an error): APPROXIMATE rolling median
    dq_median_min_samples: int = 50          # a distance bucket's median is used once it has this many samples
    dq_survival_levels: int = 3              # survival: the best N levels of each side
    dq_survival_window_s: float = 10.0       # survival: rolling window of the per-update ratios; the median is reported
    dq_survival_min_samples: int = 20
    dq_baseline_tau_s: float = 600.0         # flicker spike: time constant of the rolling baseline (mean and variance) of log1p(cancel-to-trade) and of survival
    dq_baseline_every_s: float = 1.0
    dq_spike_z: float = 4.0                  # |z| at or above this = a spike (cancel-to-trade high, survival low)


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
    DQ_APPROX = "APPROXIMATE (stays so until checked against real hedge fills): L2 depth at the feed's own update rate (2026-10-29 capture: Bybit book mean 30 ms, Binance book mean 105 ms between updates); medians are a geometric tracker, a decrease is split by the depletion ledger's rule, not observed per order"

    def _dq_init(self) -> None:
        self._dq_prev: dict[str, dict[float, float]] = {"bid": {}, "ask": {}}
        self._dq_credit: dict[str, dict[tuple, deque]] = {"delta": {}, "trade": {}}      # the depletion ledger's two-way credit, per (side, price): a decrease waiting for a print, a print waiting for a decrease
        self._dq_pending: dict[str, deque] = {"bid": deque(), "ask": deque()}            # book-first credits in arrival order: one that outlives the match window is an UNEXPLAINED decrease
        self._dq_unexplained: dict[str, deque] = {"bid": deque(), "ask": deque()}        # (ts, volume) of decreases no print explained
        self._dq_traded: dict[str, deque] = {"bid": deque(), "ask": deque()}             # (ts, volume) of prints that deplete this side (a sell aggressor hits the bids)
        self._dq_first_ts: float | None = None
        self._dq_last_feed_gap_ts: float | None = None
        self._dq_tick: float = 0.0
        self._dq_med: dict[tuple, list] = {}                 # (side, distance bucket) -> [median, samples]
        self._dq_last_med_ts = -1e18
        self._dq_last_eval_ts = -1e18
        self._dq_gaps: dict[str, dict[float, list]] = {"bid": {}, "ask": {}}      # side -> start price -> [first_seen, last_seen]: the gaps now standing, for persistence
        self._dq_surv: dict[str, deque] = {"bid": deque(), "ask": deque()}        # (ts, min(size now, size at the next update) / size now) for the best levels
        self._dq_base: dict[tuple, dict] = {}                # (side, "c2t" | "surv") -> rolling baseline {mean, var, n, ts}
        self._dq_z: dict[tuple, float | None] = {}
        self._dq_last_base_ts = -1e18
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
            if cfg.dq_survival_levels > 0 and prev:
                order = sorted(prev, reverse=(side == "bid"))[:cfg.dq_survival_levels]
                for price in order:
                    old = prev[price]
                    if old > 1e-12:
                        self._dq_surv[side].append((ts, min(book.level_size(side, price), old) / old))       # ANY decrease counts: a cancel, a print or the level leaving
            self._dq_prev[side] = cur
        if ts - self._dq_last_eval_ts >= cfg.dq_gap_eval_s:
            self._dq_last_eval_ts = ts
            if ts - self._dq_last_med_ts >= cfg.dq_median_every_s:
                self._dq_last_med_ts = ts
                self._dq_update_medians(book)
            for side in ("bid", "ask"):
                found = self._dq_scan(book, side)
                if found is not None:
                    self._dq_track(side, found, ts)
        if ts - self._dq_last_base_ts >= cfg.dq_baseline_every_s:
            self._dq_last_base_ts = ts
            self._dq_baselines(ts)

    def set_tick(self, tick: float) -> None:
        """The venue's own price grid (instrument tick, NOT a strategy grid), for the thin-tick run rule. No effect with depth_quality off."""
        if self._dq_on:
            self._dq_tick = float(tick)

    def note_feed_gap(self, ts: float) -> None:
        """The venue's feed reported a sequence gap at `ts` (or its health flag went bad: same effect). A depth gap or a spike seen soon after is FEED_FAULT, not a cliff. No effect with depth_quality off."""
        if self._dq_on:
            self._dq_last_feed_gap_ts = ts

    note_feed_fault = note_feed_gap

    def _dq_faulty(self, book, now: float) -> bool:
        last = self._dq_last_feed_gap_ts
        return getattr(book, "state", "valid") == "recovering" or (last is not None and 0.0 <= now - last <= self.config.dq_feed_fault_window_s)

    # median level size by distance from the touch
    @staticmethod
    def _dq_bucket(k: int) -> int:
        return k if k < 4 else min(int(math.log2(k)) + 2, 9)           # 0,1,2,3 | 4-7 | 8-15 | 16-31 | 32-63 | 64-127 | 128+

    def _dq_update_medians(self, book) -> None:
        cfg, tick = self.config, self._dq_tick
        if tick <= 0:
            return
        for side in ("bid", "ask"):
            lv = book.levels(side, max(2, int(cfg.dq_gap_scan_levels)))
            if len(lv) < 2:
                continue
            touch = lv[0][0]
            for price, size in lv:
                cell = self._dq_med.setdefault((side, self._dq_bucket(int(round(abs(price - touch) / tick)))), [size, 0])
                cell[0] *= (1.0 + cfg.dq_median_eta) if size > cell[0] else (1.0 - cfg.dq_median_eta)
                cell[1] += 1

    def _dq_thin(self, side: str, k: int | None, size: float) -> bool:
        cell = self._dq_med.get((side, self._dq_bucket(k))) if k is not None else None
        if cell is not None and cell[1] >= self.config.dq_median_min_samples:
            return size < self.config.dq_thin_ratio * cell[0]
        return size < self.config.dq_gap_level_size            # warm-up (or no tick): the absolute rule, 0 = only an absent price is empty

    def _dq_scan(self, book, side: str) -> list | None:
        """The gaps of one side, from the touch outward: [(start price, size before the gap, distance of its start from the touch in bps)]. None when fewer than two levels are shown. A gap that would only end at the edge of
        the captured depth is not a gap (the edge is not a cliff)."""
        cfg, tick = self.config, self._dq_tick
        lv = book.levels(side, max(2, int(cfg.dq_gap_scan_levels)))
        if len(lv) < 2:
            return None
        touch = lv[0][0]
        gaps: dict[float, tuple] = {}
        m_eff = cfg.dq_gap_min_ticks
        if cfg.dq_gap_min_bps > 0 and tick > 0:
            m_eff = max(m_eff, int(math.ceil(cfg.dq_gap_min_bps * touch / 1e4 / tick)))
        cum = run = 0
        solid_px, solid_cum, prev_px, prev_k = touch, 0.0, touch, 0
        for price, size in lv:
            k = int(round(abs(price - touch) / tick)) if tick > 0 else None
            if abs(price - prev_px) / touch * 1e4 > cfg.dq_gap_stretch_bps:                       # a price jump between two displayed levels
                gaps.setdefault(prev_px, (prev_px, cum, abs(prev_px - touch) / touch * 1e4))
            if k is not None:
                run += max(0, k - prev_k - 1)                                                       # absent ticks are thin
            if self._dq_thin(side, k, size):
                run += 1
            else:
                if run >= m_eff > 0 and tick > 0:
                    gaps.setdefault(solid_px, (solid_px, solid_cum, abs(solid_px - touch) / touch * 1e4))
                run, solid_px, solid_cum = 0, price, cum + size
            cum += size
            prev_px = price
            if k is not None:
                prev_k = k
        return sorted(gaps.values(), key=lambda g: g[2])

    @staticmethod
    def _dq_class(age: float) -> str:
        return "<1s" if age < 1.0 else ("1-10s" if age < 10.0 else ("10-60s" if age < 60.0 else ">60s"))

    def _dq_track(self, side: str, found: list, ts: float) -> None:
        """Persistence: a gap is identified by the price of the last solid level before it. Seen again = it persists; gone = its lifetime is counted in dq_stats by class."""
        stand = self._dq_gaps[side]
        now_keys = set()
        for start_px, _, _ in found:
            key = round(start_px, 8)
            now_keys.add(key)
            cell = stand.get(key)
            if cell is None:
                stand[key] = [ts, ts]
            else:
                cell[1] = ts
        for key in [k for k in stand if k not in now_keys]:
            first, last = stand.pop(key)
            self._dq_note("gap_closed_" + self._dq_class(last - first), 1.0)

    # survival (availability) near the touch
    def survival(self, side: str, now: float) -> dict | None:
        """Median, mean and 10th percentile of min(size now, size at the next update at the same price) / size now over the best levels of `side` in the rolling window (any decrease counts). None before
        dq_survival_min_samples. The unit of time is the feed's own update, so it is not comparable between venues with different update rates (APPROXIMATE)."""
        if not self._dq_on:
            return None
        cut = now - self.config.dq_survival_window_s
        dq = self._dq_surv[side]
        while dq and dq[0][0] < cut:
            dq.popleft()
        if len(dq) < self.config.dq_survival_min_samples:
            return None
        vals = sorted(r for _, r in dq)
        return {"median": vals[len(vals) // 2], "mean": sum(vals) / len(vals), "p10": vals[len(vals) // 10], "n": len(vals)}

    def recent_volume(self, side: str, now: float, window_s: float) -> float | None:
        """Volume of the prints that deplete `side` (a sell aggressor hits the bids) over the last `window_s` seconds (at most the held window, dq_flicker_window_s). None with depth_quality off."""
        if not self._dq_on:
            return None
        if window_s > self.config.dq_flicker_window_s + 1e-9:
            raise ValueError(f"window_s {window_s} is longer than the held window dq_flicker_window_s {self.config.dq_flicker_window_s}")
        self._dq_sweep(now)
        cut = now - window_s
        return sum(v for t, v in self._dq_traded[side] if t >= cut)

    def cancel_to_trade(self, side: str, now: float) -> float | None:
        """Unexplained depth decreases over traded volume, the best N levels, rolling: the same figure as `flicker` (one name for the fakeness measure, one number)."""
        return self.flicker(side, now)

    # flicker spike: cancel-to-trade or survival far outside its own rolling baseline
    def _dq_baselines(self, ts: float) -> None:
        cfg = self.config
        for side in ("bid", "ask"):
            c2t, surv = self.flicker(side, ts), self.survival(side, ts)
            for name, x in (("c2t", None if c2t is None else math.log1p(c2t)), ("surv", None if surv is None else surv["median"])):
                if x is None:
                    continue
                st = self._dq_base.setdefault((side, name), {"mean": x, "var": 0.0, "n": 0, "ts": ts})
                self._dq_z[(side, name)] = None
                if st["n"] >= 60:
                    sd = math.sqrt(st["var"])
                    self._dq_z[(side, name)] = (x - st["mean"]) / sd if sd > 1e-12 else 0.0
                a = 1.0 - math.exp(-(ts - st["ts"]) / cfg.dq_baseline_tau_s) if st["n"] else 1.0
                d = x - st["mean"]
                st["mean"] += a * d
                st["var"] = (1.0 - a) * (st["var"] + a * d * d)
                st["n"] += 1
                st["ts"] = ts

    def spike(self, book, side: str, now: float) -> dict:
        """status SPIKE (log1p cancel-to-trade at least dq_spike_z standard deviations above its own rolling baseline, or survival that many below), NORMAL, WARMUP (baseline not ready) or FEED_FAULT (a sequence
        gap or an unhealthy feed within dq_feed_fault_window_s: not a spike). The z values are those of the last baseline update (at most dq_baseline_every_s old)."""
        z1, z2 = self._dq_z.get((side, "c2t")), self._dq_z.get((side, "surv"))
        out = {"status": "WARMUP", "cancel_to_trade_z": z1, "survival_z": z2}
        if self._dq_faulty(book, now):
            out["status"] = "FEED_FAULT"
        elif z1 is not None or z2 is not None:
            hi = (z1 is not None and z1 >= self.config.dq_spike_z) or (z2 is not None and z2 <= -self.config.dq_spike_z)
            out["status"] = "SPIKE" if hi else "NORMAL"
        return out

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
        """The gaps of `side` (the side an order would CONSUME: asks for a buy), from VenueStateConfig's g, m and jump rule, nearest first.
        status GAP                    depth = size before the first gap, distance_bps = from the touch to its start, count_within_bps = gaps within dq_gap_within_bps, persistence = class of the first gap's age
              NO_GAP_WITHIN_CAPTURE   no gap in the levels the capture holds: the book's edge is not a cliff; depth None
              FEED_FAULT              a gap, but the feed reported a sequence gap within dq_feed_fault_window_s (or the book is recovering): not a cliff; depth None
              NOT_EVALUABLE           fewer than two levels, or depth_quality off; depth None
        The distances are in bps of the touch price, whatever the venue's tick."""
        out = {"status": "NOT_EVALUABLE", "depth": None, "distance_bps": None, "levels": 0, "count_within_bps": None, "persistence": None, "first_gap_age_s": None}
        if not self._dq_on:
            return out
        found = self._dq_scan(book, side)
        lv = book.levels(side, max(2, int(self.config.dq_gap_scan_levels)))
        out["levels"] = len(lv)
        if found is None:
            return out
        if not found:
            out.update(status="NO_GAP_WITHIN_CAPTURE", count_within_bps=0, captured_depth=sum(x[1] for x in lv))
            return out
        if self._dq_faulty(book, now):
            out["status"] = "FEED_FAULT"
            return out
        start_px, depth, dist = found[0]
        cell = self._dq_gaps[side].get(round(start_px, 8))
        age = 0.0 if cell is None else max(0.0, now - cell[0])
        out.update(status="GAP", depth=depth, distance_bps=dist, count_within_bps=sum(1 for g in found if g[2] <= self.config.dq_gap_within_bps), persistence=self._dq_class(age), first_gap_age_s=age)
        return out

    def depth_quality(self, book, now: float, side: str | None = None) -> dict | None:
        """One decision's evidence for `side` ("bid" / "ask"; both when None), labelled APPROXIMATE: gaps (size before the first, distance, count within N bps, persistence class), the venue's gap asymmetry
        (bid gaps - ask gaps) / total within N bps (None when there are none, or a feed fault), survival, cancel-to-trade (also under the old key `flicker`) and the spike state. None when depth_quality is off."""
        if not self._dq_on:
            return None
        sides = ("bid", "ask") if side is None else (side,)
        both = {s: self.gap_depth(book, s, now) for s in ("bid", "ask")}
        counts = {s: g["count_within_bps"] for s, g in both.items()}
        total = sum(c for c in counts.values() if c)
        asym = None if any(g["status"] in ("FEED_FAULT", "NOT_EVALUABLE") for g in both.values()) or not total else (counts["bid"] - counts["ask"]) / total
        c2t = {s: self.cancel_to_trade(s, now) for s in sides}
        return {"approx": self.DQ_APPROX, "gap": {s: both[s] for s in sides}, "gap_asymmetry": asym, "flicker": dict(c2t), "cancel_to_trade": c2t,
                "survival": {s: self.survival(s, now) for s in sides}, "spike": {s: self.spike(book, s, now) for s in sides}}

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
