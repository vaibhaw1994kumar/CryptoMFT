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

        from .fill_label import compute_ipcw_weights  # noqa: PLC0415

        all_h_recs = [r for r in records
                      if isinstance(r, FillLabelRecord) and r.horizon_s == horizon_s]
        if not all_h_recs:
            return None

        # IPCW weights: 0 for censored rows (excluded from training), 1/G(t) for rest
        raw_weights = compute_ipcw_weights(all_h_recs, horizon_s)
        recs = [r for r, w in zip(all_h_recs, raw_weights) if w > 0]
        weights = [w for w in raw_weights if w > 0]
        if not recs:
            return None

        rows = [_features(
            r.queue_ahead, r.local_depth, r.local_depth_baseline,
            r.spread_pct, r.short_vol, r.trade_count, r.trade_sign,
            r.regime_bucket, r.relative_to_touch_ticks,
        ) for r in recs]
        X = np.array(rows, dtype=np.float32)
        y = np.array([int(r.filled) for r in recs], dtype=np.int8)
        w = np.array(weights, dtype=np.float32)

        n_pos = int(y.sum())
        if n_pos == 0 or n_pos == len(y):
            return None  # all one class — model cannot distinguish

        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)

        lr = LogisticRegression(C=C, max_iter=500, solver="lbfgs")
        lr.fit(X_scaled, y, sample_weight=w)

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

    # ── Adverse-selection variant ─────────────────────────────────────────────

    @classmethod
    def fit_adverse(
        cls,
        records,
        horizon_s: int,
        trained_until_ts: float = 0.0,
        C: float = 1.0,
    ) -> "FillModelV1 | None":
        """Fit P(price_moved_through | features) — same features, different label.

        Filters to records at *horizon_s* and uses *price_moved_through* as the
        binary target.  Returns None when the filtered set is empty or single-class.
        Requires scikit-learn (training only; inference path is pure Python).
        """
        import numpy as np  # noqa: PLC0415
        from sklearn.linear_model import LogisticRegression  # noqa: PLC0415
        from sklearn.preprocessing import StandardScaler     # noqa: PLC0415
        from .fill_label import FillLabelRecord              # noqa: PLC0415

        from .fill_label import compute_ipcw_weights  # noqa: PLC0415

        all_h_recs = [r for r in records
                      if isinstance(r, FillLabelRecord) and r.horizon_s == horizon_s]
        if not all_h_recs:
            return None

        raw_weights = compute_ipcw_weights(all_h_recs, horizon_s)
        recs = [r for r, w in zip(all_h_recs, raw_weights) if w > 0]
        weights = [w for w in raw_weights if w > 0]
        if not recs:
            return None

        rows = [_features(
            r.queue_ahead, r.local_depth, r.local_depth_baseline,
            r.spread_pct, r.short_vol, r.trade_count, r.trade_sign,
            r.regime_bucket, r.relative_to_touch_ticks,
        ) for r in recs]
        X = np.array(rows, dtype=np.float32)
        y = np.array([int(r.price_moved_through) for r in recs], dtype=np.int8)
        w = np.array(weights, dtype=np.float32)

        n_pos = int(y.sum())
        if n_pos == 0 or n_pos == len(y):
            return None

        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)
        lr = LogisticRegression(C=C, max_iter=500, solver="lbfgs")
        lr.fit(X_scaled, y, sample_weight=w)

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


# ── Stage 4: multi-horizon model with TTF and adverse selection ───────────────

@dataclass
class TTFPrediction:
    """Output of FillModelV2.predict_ttf()."""
    p_fill: float             # P(fill by gate_horizon_s) — primary gate signal
    p_by_h: dict              # {horizon_s: P(fill_by_h)}, monotone in horizon_s
    e_ttf: float              # E[min(TTF, H_max) | fill by H_max] (restricted mean, s)
    p_adverse: float          # P(price_moved_through at gate_horizon_s)
    e_fill_ratio: float       # E[terminal fill_ratio | filled]; 0 if not estimated


@dataclass
class FillModelV2:
    """Multi-horizon fill-probability model with TTF and adverse-selection outputs.

    Holds one FillModelV1 per calibrated horizon plus an optional adverse model
    (same features, label = price_moved_through).  Monotone enforcement (running
    max over horizons) is applied at predict time.

    predict() delegates to the gate-horizon model for duck-typing compatibility
    with FillModelV1 — existing code calling fill_model_v1.predict(...) works
    unchanged.
    """

    gate_horizon_s: int
    trained_until_ts: float
    horizons: list              # sorted list[int]
    per_horizon: dict           # {str(horizon_s): FillModelV1}
    adverse_model: "FillModelV1 | None"
    mean_fill_ratio: float      # E[fill_ratio | filled], 0 if no data
    n_train: int                # records at gate_horizon_s used for training
    version: int = 2

    # ── Duck-typing compat with FillModelV1 ──────────────────────────────────

    @property
    def horizon_s(self) -> int:
        return self.gate_horizon_s

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
        """P(fill by gate_horizon_s) — identical signature to FillModelV1.predict()."""
        key = str(self.gate_horizon_s)
        model = self.per_horizon.get(key)
        if model is None and self.horizons:
            model = self.per_horizon.get(str(max(self.horizons)))
        if model is None:
            return 0.5
        return model.predict(
            queue_ahead=queue_ahead, local_depth=local_depth,
            local_depth_baseline=local_depth_baseline, spread_pct=spread_pct,
            short_vol=short_vol, trade_count=trade_count, trade_sign=trade_sign,
            regime_bucket=regime_bucket,
            relative_to_touch_ticks=relative_to_touch_ticks,
        )

    # ── Full multi-horizon inference ──────────────────────────────────────────

    def predict_ttf(
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
    ) -> TTFPrediction:
        """Multi-horizon prediction: fill probs, E[TTF], and adverse probability.

        E[TTF] = E[min(T, H_max) | fill by H_max], the restricted mean survival
        time, approximated by trapezoidal integration of the survival function
        S(t) = 1 - P(fill_by_t) over the calibrated horizon points.  With three
        points (1s, 5s, 15s) this is a rough order-of-magnitude estimate.
        """
        feat = dict(
            queue_ahead=queue_ahead, local_depth=local_depth,
            local_depth_baseline=local_depth_baseline, spread_pct=spread_pct,
            short_vol=short_vol, trade_count=trade_count, trade_sign=trade_sign,
            regime_bucket=regime_bucket,
            relative_to_touch_ticks=relative_to_touch_ticks,
        )

        raw: dict[int, float] = {int(h): m.predict(**feat)
                                  for h, m in self.per_horizon.items()}

        # Monotone enforcement: running max over sorted horizons
        p_by_h: dict[int, float] = {}
        running_max = 0.0
        for h in sorted(raw):
            running_max = max(running_max, raw[h])
            p_by_h[h] = running_max

        p_fill = p_by_h.get(
            self.gate_horizon_s,
            p_by_h[max(p_by_h)] if p_by_h else 0.0,
        )

        # Trapezoidal integral of S(t) from 0 to H_max
        sorted_hs = sorted(p_by_h)
        t_vals = [0] + sorted_hs
        s_vals = [1.0] + [1.0 - p_by_h[h] for h in sorted_hs]
        rmst = sum(
            (t_vals[i + 1] - t_vals[i]) * (s_vals[i] + s_vals[i + 1]) / 2.0
            for i in range(len(t_vals) - 1)
        )
        p_H = p_by_h[sorted_hs[-1]] if sorted_hs else 0.0
        e_ttf = rmst / max(p_H, 1e-9)

        p_adverse = (
            self.adverse_model.predict(**feat)
            if self.adverse_model is not None else 0.0
        )

        return TTFPrediction(
            p_fill=p_fill, p_by_h=p_by_h, e_ttf=e_ttf,
            p_adverse=p_adverse, e_fill_ratio=self.mean_fill_ratio,
        )

    # ── Training ──────────────────────────────────────────────────────────────

    @classmethod
    def fit(
        cls,
        records,
        gate_horizon_s: int,
        trained_until_ts: float = 0.0,
        horizons: tuple = (1, 5, 15),
        C: float = 1.0,
    ) -> "FillModelV2 | None":
        """Fit FillModelV2 from FillLabelRecords (all horizons mixed).

        Trains one FillModelV1 per horizon in *horizons*, plus an adverse-
        selection model (label = price_moved_through) at *gate_horizon_s*.
        Returns None if no per-horizon model could be fitted.
        """
        recs = list(records)
        per_horizon: dict[str, FillModelV1] = {}
        for h in horizons:
            m = FillModelV1.fit(recs, horizon_s=h,
                                trained_until_ts=trained_until_ts, C=C)
            if m is not None:
                per_horizon[str(h)] = m

        if not per_horizon:
            return None

        adverse = FillModelV1.fit_adverse(
            recs, horizon_s=gate_horizon_s,
            trained_until_ts=trained_until_ts, C=C,
        )

        filled_recs = [r for r in recs
                       if getattr(r, "filled", False)
                       and getattr(r, "fill_ratio", 0.0) > 0
                       and r.horizon_s == gate_horizon_s]
        mean_fill_ratio = (
            sum(r.fill_ratio for r in filled_recs) / len(filled_recs)
            if filled_recs else 0.0
        )

        gate_recs = [r for r in recs if r.horizon_s == gate_horizon_s]
        return cls(
            gate_horizon_s=gate_horizon_s,
            trained_until_ts=trained_until_ts,
            horizons=sorted(int(k) for k in per_horizon),
            per_horizon=per_horizon,
            adverse_model=adverse,
            mean_fill_ratio=mean_fill_ratio,
            n_train=len(gate_recs),
        )

    # ── Serialisation ─────────────────────────────────────────────────────────

    def save_json(self, path: "str | Path") -> None:
        payload = {
            "version": self.version,
            "gate_horizon_s": self.gate_horizon_s,
            "trained_until_ts": self.trained_until_ts,
            "horizons": self.horizons,
            "per_horizon": {k: asdict(v) for k, v in self.per_horizon.items()},
            "adverse_model": asdict(self.adverse_model) if self.adverse_model else None,
            "mean_fill_ratio": self.mean_fill_ratio,
            "n_train": self.n_train,
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @classmethod
    def load_json(cls, path: "str | Path") -> "FillModelV2":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        per_horizon = {k: FillModelV1(**v) for k, v in data["per_horizon"].items()}
        adv_data = data.get("adverse_model")
        adverse = FillModelV1(**adv_data) if adv_data else None
        return cls(
            gate_horizon_s=data["gate_horizon_s"],
            trained_until_ts=data["trained_until_ts"],
            horizons=data["horizons"],
            per_horizon=per_horizon,
            adverse_model=adverse,
            mean_fill_ratio=data.get("mean_fill_ratio", 0.0),
            n_train=data.get("n_train", 0),
            version=data.get("version", 2),
        )


def load_fill_model(path: "str | Path") -> "FillModelV1 | FillModelV2":
    """Load a FillModelV1 or FillModelV2 from a JSON checkpoint (auto-detected).

    Files with ``"version": 2`` or a ``"per_horizon"`` key load as FillModelV2.
    All other files (including legacy V1 files without a version key) load as
    FillModelV1.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("version", 1) >= 2 or "per_horizon" in data:
        return FillModelV2.load_json(path)
    return FillModelV1.load_json(path)
