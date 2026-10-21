"""Phase 2: queue-reactive fill rate estimator.

Models the fill probability P(filled by h | features) using a Poisson-process
approximation conditioned on queue state.

Architecture
------------
The order book is discretised into (queue_bucket, depth_bucket, spread_bucket)
cells.  Within each cell we estimate the Poisson rate λ at which limit orders
at the touch get filled, using the empirical fill count / total exposure time
from the calibration window.

Given a new order:
    1. Bucket the current queue_ahead, local_depth, spread_pct.
    2. Look up λ for that bucket (fall back to global mean if unseen).
    3. P(filled by h) = 1 - exp(-λ * h).

This is more reactive than the Beta-Binomial posterior used by EmpiricalFillModel
because it distinguishes short-horizon from long-horizon signal via the rate
(rather than a binary filled/not-filled aggregated across horizons).

Usage::

    estimator = QueueReactiveFillEstimator()
    estimator.fit(records)          # list[FillLabelRecord] from fill_label.py
    prob = estimator.predict(
        queue_ahead=150.0,
        local_depth=500.0,
        local_depth_baseline=800.0,
        spread_pct=0.0012,
        horizon_s=10,
    )
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


# ── Bucket helpers ────────────────────────────────────────────────────────────

def _queue_bucket(queue_ahead: float) -> str:
    if queue_ahead < 50:
        return "q0"
    if queue_ahead < 200:
        return "q1"
    if queue_ahead < 500:
        return "q2"
    return "q3"


def _depth_bucket(depth_ratio: float) -> str:
    """depth_ratio = local_depth / local_depth_baseline."""
    if depth_ratio < 0.25:
        return "d0"
    if depth_ratio < 0.75:
        return "d1"
    return "d2"


def _spread_bucket(spread_pct: float) -> str:
    if spread_pct < 0.0005:
        return "s0"
    if spread_pct < 0.002:
        return "s1"
    return "s2"


# ── Rate cell ─────────────────────────────────────────────────────────────────

@dataclass
class _RateCell:
    total_exposure_s: float = 0.0   # sum of min(order_lifetime, horizon_s) over orders
    fill_count: float = 0.0         # number of orders filled within their horizon

    @property
    def rate(self) -> float:
        if self.total_exposure_s < 1e-9:
            return 0.0
        return self.fill_count / self.total_exposure_s


# ── Estimator ─────────────────────────────────────────────────────────────────

@dataclass
class QueueReactiveFillEstimator:
    """Poisson-rate fill model bucketed by queue / depth / spread state.

    Parameters
    ----------
    min_cell_count:
        Minimum fill count before a cell's own rate is used; otherwise falls
        back to the global rate.
    """
    min_cell_count: float = 5.0

    _cells: dict[tuple[str, str, str], _RateCell] = field(
        default_factory=dict, init=False, repr=False,
    )
    _global: _RateCell = field(default_factory=_RateCell, init=False, repr=False)

    def fit(self, records) -> None:
        """Fit Poisson rates from a list of FillLabelRecord.

        Each record represents one (order, horizon) pair; the exposure time is
        horizon_s (the full horizon, since cancelled orders are already filtered
        to h <= their lifetime by the fill_engine's censoring logic).
        """
        from .fill_label import FillLabelRecord  # noqa: PLC0415

        self._cells.clear()
        self._global = _RateCell()

        for r in records:
            if not isinstance(r, FillLabelRecord):
                continue
            depth_ratio = r.local_depth / max(r.local_depth_baseline, 1e-9)
            key = (
                _queue_bucket(r.queue_ahead),
                _depth_bucket(depth_ratio),
                _spread_bucket(r.spread_pct),
            )
            if key not in self._cells:
                self._cells[key] = _RateCell()

            exposure = float(r.horizon_s)
            fill_contrib = 1.0 if r.filled else 0.0

            self._cells[key].total_exposure_s += exposure
            self._cells[key].fill_count += fill_contrib
            self._global.total_exposure_s += exposure
            self._global.fill_count += fill_contrib

    def _get_rate(
        self,
        queue_ahead: float,
        depth_ratio: float,
        spread_pct: float,
    ) -> float:
        key = (
            _queue_bucket(queue_ahead),
            _depth_bucket(depth_ratio),
            _spread_bucket(spread_pct),
        )
        cell = self._cells.get(key)
        if cell is not None and cell.fill_count >= self.min_cell_count:
            return cell.rate
        return self._global.rate

    def predict(
        self,
        queue_ahead: float,
        local_depth: float,
        local_depth_baseline: float,
        spread_pct: float,
        horizon_s: float,
    ) -> float:
        """Return P(filled by horizon_s) for an order with the given state."""
        depth_ratio = local_depth / max(local_depth_baseline, 1e-9)
        lam = self._get_rate(queue_ahead, depth_ratio, spread_pct)
        if lam <= 0.0:
            return 0.0
        return 1.0 - math.exp(-lam * horizon_s)

    def bucket_summary(self) -> dict[str, dict]:
        """Return per-bucket rate stats for diagnostics."""
        rows = {}
        for (q, d, s), cell in sorted(self._cells.items()):
            key = f"{q}|{d}|{s}"
            rows[key] = {
                "fill_count": cell.fill_count,
                "exposure_s": round(cell.total_exposure_s, 1),
                "rate_per_s": round(cell.rate, 6),
                "mean_fill_prob_10s": round(1.0 - math.exp(-cell.rate * 10), 4),
            }
        return rows
