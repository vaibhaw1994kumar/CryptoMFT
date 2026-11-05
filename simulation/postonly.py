"""PostOnly acceptance rule and the arrival-latency source used with it.

would_cross() is the exchange's rule for a PostOnly (maker-only) limit order, evaluated against the book AT THE EXCHANGE at the
moment the order arrives: a buy at or above the best ask, or a sell at or below the best bid, would execute immediately and is
rejected instead. An order inside the spread (price improvement) or behind the touch rests.

AckLatencySource draws the send-to-arrival time of a passive order. "preset" keeps the venue's configured entry latency (the
legacy behaviour; this class is then not used). "empirical" samples the calibrated distribution measured from real orders by
scripts/lifecycle_calibration_bybit.py (config/lifecycle_calibration_bybit.json, one-way client stamp -> exchange-created,
including the unknown clock offset). "fixed" uses one number. It has its own RNG stream, so switching it never moves the
draws of the VENUE latency samplers (hedge entry, cancel). The reject notice (ArbBacktestConfig.postonly_notice_s = None) draws from
this same stream: with an empirical source, turning the reject rule on or off shifts later arrival draws; with "preset" no ack
draw is made at all.
"""
from __future__ import annotations

import bisect
import json
import random
from dataclasses import dataclass
from pathlib import Path

SOURCES = ("preset", "empirical", "fixed")
DEFAULT_PERIOD = "before_2026-04-19"      # the only calibration window that precedes the 2026-04-19..25 evaluation period


def would_cross(side: str, price: float, best_bid: float, best_ask: float) -> bool:
    """True if a PostOnly order at `price` would take liquidity against this book. An empty side cannot be crossed."""
    if side == "buy":
        return best_ask > 0.0 and price >= best_ask
    if side == "sell":
        return best_bid > 0.0 and price <= best_bid
    raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")


@dataclass
class AckLatencyConfig:
    source: str = "preset"
    fixed_ms: float = 0.0
    calibration_path: str = ""
    period: str = DEFAULT_PERIOD
    scale: float = 1.0            # multiplies every sample (stress scenarios)
    max_ms: float = 2000.0        # same cap the preset samplers use
    seed: int = 5

    def __post_init__(self) -> None:
        if self.source not in SOURCES:
            raise ValueError(f"ack latency source must be one of {SOURCES}, got {self.source!r}")
        if self.source == "fixed" and not self.fixed_ms > 0.0:
            raise ValueError("fixed ack latency needs fixed_ms > 0")
        if self.source == "empirical" and not self.calibration_path:
            raise ValueError("empirical ack latency needs calibration_path")
        if not self.scale > 0.0:
            raise ValueError("scale must be > 0")


def load_quantile_grid(path: str | Path, period: str) -> tuple[list[float], list[float], int]:
    """(quantiles, milliseconds, n_samples) of one calibration period."""
    doc = json.loads(Path(path).read_text())
    grids = doc["ack_latency_proxy"]["empirical_quantile_grids"]
    if period not in grids:
        raise KeyError(f"calibration period {period!r} not in {sorted(grids)}")
    g = grids[period]
    return list(g["q"]), list(g["ms"]), int(g["n"])


class AckLatencySource:
    def __init__(self, cfg: AckLatencyConfig) -> None:
        self.cfg = cfg
        self._rng = random.Random(cfg.seed)
        self.n_calibration = 0
        self._q: list[float] = []
        self._ms: list[float] = []
        if cfg.source == "empirical":
            self._q, self._ms, self.n_calibration = load_quantile_grid(cfg.calibration_path, cfg.period)

    @property
    def active(self) -> bool:
        return self.cfg.source != "preset"

    def sample_s(self) -> float:
        c = self.cfg
        if c.source == "fixed":
            ms = c.fixed_ms
        else:
            u = self._rng.random()
            i = bisect.bisect_right(self._q, u) - 1
            i = min(max(i, 0), len(self._q) - 2)
            q0, q1 = self._q[i], self._q[i + 1]
            w = (u - q0) / (q1 - q0) if q1 > q0 else 0.0
            ms = self._ms[i] + (self._ms[i + 1] - self._ms[i]) * w
        return min(ms * c.scale, c.max_ms * c.scale) / 1000.0

    def describe(self) -> dict:
        c = self.cfg
        return {"source": c.source, "fixed_ms": c.fixed_ms if c.source == "fixed" else None, "period": c.period if c.source == "empirical" else None,
                "n_calibration": self.n_calibration or None, "scale": c.scale, "max_ms": c.max_ms}
