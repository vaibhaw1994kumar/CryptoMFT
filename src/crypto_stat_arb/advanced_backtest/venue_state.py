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


class VenueState:
    """Rolling trade-flow and depth statistics for one venue."""

    def __init__(self, venue: str, config: VenueStateConfig | None = None) -> None:
        self.venue = venue
        self.config = config or VenueStateConfig()
        self.lambda_engine = LambdaRegimeEngine()

        self._trades: deque[tuple[float, float, int]] = deque()   # (ts, volume, sign)
        self.top_depth_baseline: float = 0.0
        self.short_vol: float = 0.0
        self.last_mid: float = 0.0
        self.last_mid_ts: float = 0.0
        self.trades_seen: int = 0
        self.last_regime: LambdaRegime = LambdaRegime(0.0, 0.0, LambdaBucket.CALM, 0.0)
        self._lambda_ref_mid: float = 0.0
        self._lambda_last_ts: float = 0.0
        self.lambda_updates: int = 0

    # ── Updates ──────────────────────────────────────────────────────────────

    def on_trade(self, ts: float, price: float, volume: float, sign: int) -> None:
        self._trades.append((ts, volume, sign))
        self.trades_seen += 1
        self._trim(ts)

    def _trim(self, now: float) -> None:
        cutoff = now - self.config.flow_window_s
        while self._trades and self._trades[0][0] < cutoff:
            self._trades.popleft()

    def on_book(self, book: OrderBook, ts: float) -> None:
        if not book.ready:
            return
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

    # ── Derived signals ──────────────────────────────────────────────────────

    @property
    def signed_volume(self) -> float:
        return sum(v * s for _, v, s in self._trades)

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
