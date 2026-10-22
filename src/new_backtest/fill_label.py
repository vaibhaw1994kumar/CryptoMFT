"""Phase 1 fill-model training: label extraction and walk-forward calibration.

Label definition
----------------
For each logged order event at horizon h:

    filled_by_h = 1  if the order was filled within h seconds of placement
    filled_by_h = 0  if NOT filled AND (cancelled-before-h is NOT the reason)

Censored rows (cancelled before h, not filled) are excluded — they carry no
signal about whether the order *would* have filled had it stayed.

`terminal_reason` encodes why the order ended:
    full_fill           → filled at or before every h >= time_to_fill
    partial_then_cancel → filled partially; treat as filled for h >= first_fill_ts
    cancel              → never filled; emit label=0 only for h <= order lifetime

WalkForwardCalibrator
---------------------
Splits a fill_events.jsonl log into non-overlapping calibration/eval windows
and trains a logistic-regression hazard model per bucket.  Each eval window is
only trained on labels whose horizon matured before the eval window starts
(until_ts filter), preventing temporal leakage.

Usage::

    cal = WalkForwardCalibrator(
        log_path="out/fill_events.jsonl",
        window_days=30,
        step_days=7,
    )
    for fold in cal.folds():
        model = fold.fit()   # sklearn LogisticRegression
        preds = fold.predict(model)  # on eval set
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


# ── Label record ─────────────────────────────────────────────────────────────

@dataclass
class FillLabelRecord:
    """One training label extracted from a LoggedOrderEvent."""
    timestamp: float
    symbol: str
    side: str                    # "bid" / "ask"
    horizon_s: int
    filled: bool                 # label: filled within horizon_s?

    # Features (same as EmpiricalFillModel bucket dimensions)
    relative_to_touch_ticks: int
    queue_ahead: float           # initial_queue_ahead snapshot at placement
    local_depth: float
    local_depth_baseline: float
    spread_pct: float
    short_vol: float
    trade_count: float
    trade_sign: float
    regime_bucket: str           # "calm" / "elevated" / "toxic"

    terminal_reason: str         # "full_fill" / "partial_then_cancel" / "cancel"

    # Stage 4 outputs — read from fill_events.jsonl (zero/False when absent for
    # records written before Stage 4 was deployed).
    fill_ratio: float = 0.0          # terminal fill_qty / order_size (NOT at-horizon)
    price_moved_through: bool = False  # market moved through order within horizon_s


def extract_labels(
    log_path: str | Path,
    until_ts: float | None = None,
) -> list[FillLabelRecord]:
    """Read fill_events.jsonl and extract non-censored FillLabelRecords.

    Parameters
    ----------
    log_path:
        Path to a fill_events.jsonl written by EmpiricalFillModel.
    until_ts:
        If given, skip any event where ``timestamp + horizon_s > until_ts``.
        This prevents label leakage across walk-forward fold boundaries.

    Returns
    -------
    List of label records, one per (event, horizon) pair that is not censored.
    """
    records: list[FillLabelRecord] = []
    path = Path(log_path)
    if not path.exists():
        return records

    with path.open(encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue

            ts = float(payload.get("timestamp", 0.0))
            horizon_s = int(payload.get("horizon_s", 0))
            terminal_reason = str(payload.get("terminal_reason", ""))
            filled = bool(payload.get("filled", False))

            # Leakage guard: skip if label hasn't matured at until_ts
            if until_ts is not None and ts + horizon_s > until_ts:
                continue

            # Censoring: if the order was cancelled before this horizon and
            # not filled, we have no information about fill probability.
            # terminal_reason == "cancel" + filled==False → censored for h
            # where order lifetime < h.  Without order lifetime in the log we
            # rely on the fill_engine's censoring filter (it already skips
            # emitting these rows), so any row here is already non-censored.
            # We still guard: if cancelled and not filled, label is 0 (not censored,
            # since fill_engine only logs h <= lifetime for cancelled orders).

            records.append(FillLabelRecord(
                timestamp=ts,
                symbol=str(payload.get("symbol", "")),
                side=str(payload.get("side", "")),
                horizon_s=horizon_s,
                filled=filled,
                relative_to_touch_ticks=int(payload.get("relative_to_touch_ticks", 0)),
                queue_ahead=float(payload.get("queue_ahead", 0.0)),
                local_depth=float(payload.get("local_depth", 0.0)),
                local_depth_baseline=float(payload.get("local_depth_baseline", 1.0)),
                spread_pct=float(payload.get("spread_pct", 0.0)),
                short_vol=float(payload.get("short_vol", 0.0)),
                trade_count=float(payload.get("trade_count", 0.0)),
                trade_sign=float(payload.get("trade_sign", 0.0)),
                regime_bucket=str(payload.get("regime_bucket", "calm")),
                terminal_reason=terminal_reason,
                fill_ratio=float(payload.get("fill_ratio", 0.0)),
                price_moved_through=bool(payload.get("price_moved_through", False)),
            ))

    return records


# ── Walk-forward calibrator ───────────────────────────────────────────────────

@dataclass
class WalkForwardFold:
    """One fold: calibration labels + eval labels with their fold boundary."""
    fold_id: int
    cal_start_ts: float
    cal_end_ts: float    # == eval_start_ts; until_ts for label maturity filter
    eval_end_ts: float

    cal_records: list[FillLabelRecord] = field(default_factory=list)
    eval_records: list[FillLabelRecord] = field(default_factory=list)

    def feature_matrix(
        self,
        records: list[FillLabelRecord],
    ):
        """Return (X, y) numpy arrays.  Requires numpy; imported lazily."""
        import numpy as np  # noqa: PLC0415

        rows = []
        labels = []
        for r in records:
            rows.append([
                r.relative_to_touch_ticks,
                math.log1p(max(0.0, r.queue_ahead)),
                r.local_depth / max(r.local_depth_baseline, 1e-9),
                r.spread_pct,
                r.short_vol,
                r.trade_count,
                r.trade_sign,
                float(r.regime_bucket == "elevated"),
                float(r.regime_bucket == "toxic"),
            ])
            labels.append(int(r.filled))
        return np.array(rows, dtype=np.float32), np.array(labels, dtype=np.int8)

    def fit(self, C: float = 1.0, horizon_s: int | None = None):
        """Fit a logistic-regression hazard model on cal_records.

        Parameters
        ----------
        C:
            Inverse regularisation strength passed to LogisticRegression.
        horizon_s:
            When provided, only records whose ``horizon_s`` matches this value
            are used for training.  Without this filter, records at different
            horizons (e.g. 1s, 5s, 15s) are mixed: the label ``filled``
            changes meaning across horizons and the fitted probabilities are
            uninterpretable.  Always pass the target horizon when records from
            multiple horizons are present in the fold.

        Returns an sklearn LogisticRegression fitted on the filtered calibration
        set, or None when the filtered set is empty or single-class.
        Requires scikit-learn.
        """
        from sklearn.linear_model import LogisticRegression  # noqa: PLC0415
        from sklearn.preprocessing import StandardScaler     # noqa: PLC0415
        import numpy as np                                   # noqa: PLC0415

        records = (
            [r for r in self.cal_records if r.horizon_s == horizon_s]
            if horizon_s is not None
            else self.cal_records
        )

        X, y = self.feature_matrix(records)
        if len(X) == 0 or y.sum() == 0 or y.sum() == len(y):
            # Can't fit with all-one-class labels or empty set
            return None

        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)
        lr = LogisticRegression(C=C, max_iter=500, solver="lbfgs")
        lr.fit(X_scaled, y)
        lr._scaler = scaler  # attach scaler for predict convenience
        lr._horizon_s = horizon_s  # record for diagnostics
        return lr

    def predict(self, model, horizon_s: int | None = None) -> tuple:
        """Return (y_true, y_prob) for eval_records using a fitted model.

        Parameters
        ----------
        horizon_s:
            When provided, only eval records with matching ``horizon_s`` are
            scored.  Should match the horizon passed to ``fit()``.
        """
        import numpy as np  # noqa: PLC0415

        records = (
            [r for r in self.eval_records if r.horizon_s == horizon_s]
            if horizon_s is not None
            else self.eval_records
        )

        if model is None or not records:
            return np.array([]), np.array([])

        X, y = self.feature_matrix(records)
        X_scaled = model._scaler.transform(X)
        probs = model.predict_proba(X_scaled)[:, 1]
        return y, probs


@dataclass
class WalkForwardCalibrator:
    """Split a fill_events.jsonl into walk-forward folds and train Phase 1 models.

    Parameters
    ----------
    log_path:
        Path to fill_events.jsonl.
    window_days:
        Size of the calibration window in days.
    step_days:
        How far the fold boundary advances each step (eval window size).
    min_cal_records:
        Minimum number of non-censored calibration labels needed to attempt fit.
    """
    log_path: str | Path
    window_days: int = 30
    step_days: int = 7
    min_cal_records: int = 50

    def folds(self) -> Iterator[WalkForwardFold]:
        """Yield WalkForwardFold objects, one per step, oldest-first."""
        import datetime as _dt  # noqa: PLC0415

        all_records = extract_labels(self.log_path)
        if not all_records:
            return

        all_records.sort(key=lambda r: r.timestamp)
        t_min = all_records[0].timestamp
        t_max = all_records[-1].timestamp

        day_s = 86_400.0
        window_s = self.window_days * day_s
        step_s = self.step_days * day_s

        fold_id = 0
        cal_start = t_min
        while cal_start + window_s + step_s <= t_max:
            cal_end = cal_start + window_s     # == eval_start; until_ts boundary
            eval_end = cal_end + step_s

            cal_recs = [r for r in all_records
                        if cal_start <= r.timestamp < cal_end
                        and r.timestamp + r.horizon_s <= cal_end]  # matured
            eval_recs = [r for r in all_records
                         if cal_end <= r.timestamp < eval_end]

            if len(cal_recs) >= self.min_cal_records:
                yield WalkForwardFold(
                    fold_id=fold_id,
                    cal_start_ts=cal_start,
                    cal_end_ts=cal_end,
                    eval_end_ts=eval_end,
                    cal_records=cal_recs,
                    eval_records=eval_recs,
                )
                fold_id += 1

            cal_start += step_s
