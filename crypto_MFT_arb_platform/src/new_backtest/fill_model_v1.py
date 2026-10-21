"""FillModelV1: serialisable logistic-regression fill-probability gate.

Trained offline from fill_events.jsonl using WalkForwardFold.fit_v1();
loaded at backtester startup and queried once per maker-quote evaluation
to decide whether P(fill | features, horizon) >= min_fill_prob.

Design constraints
------------------
* No sklearn at inference time.  Scoring is a dot product + sigmoid, so every
  production call costs ~10 μs regardless of training library.
* Separate model per decision horizon.  Mixing h=1/5/15 in one dataset trains
  an average across horizons; the gate is evaluated at a single horizon
  (ArbConfig.fill_gate_horizon_s), so we only need one model.
* JSON serialisation.  The checkpoint stores feature names, scaler parameters,
  coefficients, intercept, trained_until_ts and n_train — everything needed to
  reproduce the exact scores without sklearn.
* Leakage guard.  The backtester refuses a model whose trained_until_ts is
  after the run's start epoch (epoch(start_date)).

Feature vector  (dim = 9, matches WalkForwardFold.feature_matrix)
--------------
  [0] log1p(queue_ahead)
  [1] local_depth / local_depth_baseline
  [2] spread_pct
  [3] short_vol
  [4] trade_count
  [5] trade_sign
  [6] relative_to_touch_ticks  (int, cast to float)
  [7] float(regime == "elevated")
  [8] float(regime == "toxic")
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Sequence


FEATURE_NAMES = (
    "log1p_queue_ahead",
    "depth_ratio",
    "spread_pct",
    "short_vol",
    "trade_count",
    "trade_sign",
    "rel_ticks",
    "regime_elevated",
    "regime_toxic",
)
FEATURE_DIM = len(FEATURE_NAMES)  # 9


def _features(
    queue_ahead: float,
    local_depth: float,
    local_depth_baseline: float,
    spread_pct: float,
    short_vol: float,
    trade_count: float,
    trade_sign: float,
    regime_bucket: str,
    relative_to_touch_ticks: int = 0,
) -> list[float]:
    return [
        math.log1p(max(0.0, queue_ahead)),
        local_depth / max(local_depth_baseline, 1e-9),
        spread_pct,
        short_vol,
        trade_count,
        trade_sign,
        float(relative_to_touch_ticks),
        float(regime_bucket == "elevated"),
        float(regime_bucket == "toxic"),
    ]


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


@dataclass
class FillModelV1:
    """Plain-Python fill-probability scorer, trained per decision horizon."""

    horizon_s: int
    trained_until_ts: float          # epoch of calibration window end
    n_train: int                     # number of training labels used
    base_rate: float                 # fraction of training labels that were filled

    # StandardScaler parameters (len == FEATURE_DIM)
    feature_mean: list[float]
    feature_scale: list[float]

    # LogisticRegression parameters
    coef: list[float]                # len == FEATURE_DIM
    intercept: float

    # ── Inference (no sklearn) ────────────────────────────────────────────────

    def predict(
        self,
        queue_ahead: float,
        local_depth: float,
        local_depth_baseline: float,
        spread_pct: float,
        short_vol: float,
        trade_count: float,
        trade_sign: float,
        regime_bucket: str,
        relative_to_touch_ticks: int = 0,
    ) -> float:
        """Return P(fill within horizon_s) ∈ [0, 1]."""
        raw = _features(
            queue_ahead, local_depth, local_depth_baseline,
            spread_pct, short_vol, trade_count, trade_sign,
            regime_bucket, relative_to_touch_ticks,
        )
        logit = self.intercept + sum(
            self.coef[i] * (raw[i] - self.feature_mean[i]) / max(self.feature_scale[i], 1e-12)
            for i in range(FEATURE_DIM)
        )
        return _sigmoid(logit)

    # ── Training ─────────────────────────────────────────────────────────────

    @classmethod
    def fit(
        cls,
        records,                  # Iterable[FillLabelRecord]
        horizon_s: int,
        trained_until_ts: float = 0.0,
        C: float = 1.0,
    ) -> "FillModelV1 | None":
        """Fit from a list of FillLabelRecord filtered to a single horizon.

        Returns None when the training set is empty or all one class
        (logistic regression cannot be fit).
        """
        import numpy as np  # noqa: PLC0415
        from sklearn.linear_model import LogisticRegression  # noqa: PLC0415
        from sklearn.preprocessing import StandardScaler     # noqa: PLC0415
        from .fill_label import FillLabelRecord              # noqa: PLC0415

        recs = [r for r in records
                if isinstance(r, FillLabelRecord) and r.horizon_s == horizon_s]
        if not recs:
            return None

        rows = [_features(
            r.queue_ahead, r.local_depth, r.local_depth_baseline,
            r.spread_pct, r.short_vol, r.trade_count, r.trade_sign,
            r.regime_bucket, r.relative_to_touch_ticks,
        ) for r in recs]
        X = np.array(rows, dtype=np.float32)
        y = np.array([int(r.filled) for r in recs], dtype=np.int8)

        n_pos = int(y.sum())
        if n_pos == 0 or n_pos == len(y):
            return None  # all one class — model cannot distinguish

        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)

        lr = LogisticRegression(C=C, max_iter=500, solver="lbfgs")
        lr.fit(X_scaled, y)

        return cls(
            horizon_s=horizon_s,
            trained_until_ts=trained_until_ts,
            n_train=len(recs),
            base_rate=float(n_pos / len(recs)),
            feature_mean=scaler.mean_.tolist(),
            feature_scale=scaler.scale_.tolist(),
            coef=lr.coef_[0].tolist(),
            intercept=float(lr.intercept_[0]),
        )

    # ── Serialisation ─────────────────────────────────────────────────────────

    def save_json(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(asdict(self), indent=2, sort_keys=True),
            encoding="utf-8",
        )

    @classmethod
    def load_json(cls, path: str | Path) -> "FillModelV1":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(**data)
