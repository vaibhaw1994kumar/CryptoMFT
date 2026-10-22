"""Stochastic background taker-flow model for arb-backtester stress testing.

Ported from AMM/Kraken/altcoin_mm_v1/latency_model.py and adapted for the
cross-venue arb event loop.  Three defects fixed relative to the original:

  1. Size floor: `max(mean_sz * 0.01, raw)` rather than `max(1.0, raw)`.
     The original hardcodes 1 base-unit minimum, which is catastrophic for BTC
     (floor = $60 000) and wrong for all other assets.

  2. Touch-side pricing: buys use best_ask, sells use best_bid.  The original
     uses mid for both sides, which overstates queue depletion for bid orders
     (a sell aggressor priced at mid never passes the price check in on_trade).

  3. Lognormal mean preservation: `exp(gauss(0, σ) - 0.5*σ²)` so E[noise] = 1
     and the empirical mean notional matches typical_size_usdt.

Design note on what this model does and does NOT model
------------------------------------------------------
Binance and Bybit publish every matched trade; there is no un-taped retail flow.
Symmetric synthetic flow therefore drains queue faster, raising the simulated
fill rate — it makes the backtest MORE optimistic, not more conservative.

To model adverse selection specifically, tie buy_fraction to the current trade
sign:
    cfg.buy_fraction = clip(0.5 + k * state.trade_sign, 0, 1)
Sell-heavy synthetic flow then hits your bid precisely when real sellers dominate.

The default is enabled=False so the baseline backtest is unaffected. Enable for
sensitivity analysis and always compare results with and without the agent.

Synthetic fills deliberately DO NOT feed EmpiricalFillModel training labels (the
is_synthetic flag on Fill prevents _record_outcome() from logging). They DO
affect PnL, inventory and markouts, and DO fire hedge legs — they represent real
volume that happened to arrive in that window, so their downstream consequences
are real.

Integration in CrossVenueArbBacktester._apply_event():

    # Before processing a real TRADE event on ev.venue:
    if self.agent.config.enabled and self.maker.live_orders(ev.venue) and book.ready:
        last = self._last_trade_ts.get(ev.venue, now)
        if last < now:
            for syn in self.agent.generate(last, now, book.best_bid, book.best_ask):
                for f in self.maker.on_trade(
                    ev.venue, syn.price, syn.volume, syn.sign, syn.timestamp,
                    spec.fees.maker_bps, is_synthetic=True,
                ):
                    self._on_fill(f)
        self._last_trade_ts[ev.venue] = now
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass


# ── RNG with Poisson sampler ──────────────────────────────────────────────────

class _PoissonRNG(random.Random):
    """random.Random subclass that adds Poisson sampling."""

    def poisson(self, lam: float) -> int:
        if lam <= 0:
            return 0
        if lam < 30:
            # Knuth's algorithm (exact)
            threshold = math.exp(-lam)
            k, p = 0, 1.0
            while p > threshold:
                k += 1
                p *= self.random()
            return k - 1
        # Normal approximation for large lambda (lam >= 30)
        return max(0, round(self.gauss(lam, math.sqrt(lam))))


# ── Configuration ─────────────────────────────────────────────────────────────

@dataclass
class AgentFlowConfig:
    """
    Background Poisson taker agents injected between real exchange trades.

    Calibrate from your logs:
        taker_rate_per_s: mean arrival rate of background taker events
        typical_size_usdt: mean notional per synthetic trade in USD
        buy_fraction: fraction of synthetic takers that are buys [0, 1]
            0.5  = symmetric (use for queue-depletion stress test)
            clip(0.5 + k * trade_sign, 0, 1) = directional (adverse selection)
        size_cv: coefficient of variation for the size distribution (>0 adds
            lognormal noise around the exponential mean)

    Enabled is False by default; flip to True only for sensitivity runs.
    """
    taker_rate_per_s:  float = 2.0    # background takers / second
    typical_size_usdt: float = 50.0   # mean notional per synthetic trade (USD)
    buy_fraction:      float = 0.50   # fraction of synthetic trades that are buys
    size_cv:           float = 1.0    # CV for trade-size lognormal noise
    seed:              int | None = None
    enabled:           bool = False   # off by default — enable for sensitivity analysis


# ── Output ────────────────────────────────────────────────────────────────────

@dataclass
class SyntheticTrade:
    timestamp: float
    price:     float    # best_ask for buys, best_bid for sells
    volume:    float    # base-asset units
    side:      str      # "buy" | "sell"
    sign:      int      # +1 for buy aggressor, -1 for sell aggressor
    is_synthetic: bool = True


# ── Model ─────────────────────────────────────────────────────────────────────

class BackgroundAgentModel:
    """
    Generate stochastic background taker events between replay events.

    Usage (see module docstring for full integration):
        agent = BackgroundAgentModel(AgentFlowConfig(taker_rate_per_s=2.0,
                                                     enabled=True))
        for syn in agent.generate(ts_prev, ts_now, book.best_bid, book.best_ask):
            maker.on_trade(venue, syn.price, syn.volume, syn.sign,
                           syn.timestamp, fee_bps, is_synthetic=True)
    """

    def __init__(self, config: AgentFlowConfig | None = None) -> None:
        self.config = config or AgentFlowConfig()
        self._rng   = _PoissonRNG(self.config.seed)

    def generate(
        self,
        ts_start:  float,
        ts_end:    float,
        best_bid:  float,
        best_ask:  float,
    ) -> list[SyntheticTrade]:
        """Return synthetic trades sorted by timestamp.

        Parameters
        ----------
        ts_start, ts_end:
            Half-open interval [ts_start, ts_end) in seconds.
        best_bid, best_ask:
            Current touch prices on the maker venue.  Buys execute at best_ask,
            sells at best_bid.
        """
        cfg = self.config
        if not cfg.enabled or ts_end <= ts_start or best_bid <= 0 or best_ask <= 0:
            return []

        mid = (best_bid + best_ask) / 2.0
        dt  = ts_end - ts_start
        n   = self._rng.poisson(cfg.taker_rate_per_s * dt)
        if n == 0:
            return []

        # mean base-asset size; epsilon guard for extreme prices
        mean_sz = cfg.typical_size_usdt / max(mid, 1e-9)
        sigma   = cfg.size_cv * 0.5  # lognormal σ (CV ≈ σ for small σ)

        trades: list[SyntheticTrade] = []
        for _ in range(n):
            t = ts_start + self._rng.random() * dt

            # Size: exponential × mean-preserving lognormal noise
            # E[exp(gauss(0,σ) - 0.5σ²)] = 1  ← preserves mean
            raw = self._rng.expovariate(1.0 / max(mean_sz, 1e-12))
            if sigma > 0:
                raw *= math.exp(self._rng.gauss(0.0, sigma) - 0.5 * sigma * sigma)
            # Floor at 1% of mean to avoid near-zero noise artefacts
            volume = max(mean_sz * 0.01, raw)

            is_buy = self._rng.random() < cfg.buy_fraction
            side  = "buy"  if is_buy else "sell"
            price = best_ask if is_buy else best_bid
            sign  = 1       if is_buy else -1

            trades.append(SyntheticTrade(
                timestamp=t, price=price, volume=volume, side=side, sign=sign,
            ))

        trades.sort(key=lambda x: x.timestamp)
        return trades
