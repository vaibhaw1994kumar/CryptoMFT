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

    # Censoring fields (zero/False on old logs without observation_time_s).
    # observation_time_s: seconds the order was observable (fill_time_s or cancel_time_s).
    # is_censored: True when order was cancelled before this horizon without filling.
    #   Censored rows are excluded from the label y but used to estimate the censoring
    #   survival function G(t) for IPCW weighting (see compute_ipcw_weights).
    observation_time_s: float = 0.0
    is_censored: bool = False


def extract_labels(
    log_path: str | Path,
    until_ts: float | None = None,
) -> list[FillLabelRecord]:
    """Read fill_events.jsonl and return FillLabelRecords including censored rows.

    Parameters
    ----------
    log_path:
        Path to a fill_events.jsonl written by EmpiricalFillModel.
    until_ts:
        If given, skip any event where ``timestamp + horizon_s > until_ts``.
        This prevents label leakage across walk-forward fold boundaries.

    Returns
    -------
    List of label records, one per (event, horizon) pair.  Records where the
    order was cancelled before the horizon (``is_censored=True``) are included
    so that ``compute_ipcw_weights`` can estimate the censoring distribution G(t)
    and produce unbiased per-record weights for logistic-regression training.
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
            obs_time = float(payload.get("observation_time_s", 0.0))

            # Leakage guard: skip if label hasn't matured at until_ts
            if until_ts is not None and ts + horizon_s > until_ts:
                continue

            # Derive is_censored: order cancelled before this horizon without filling.
            # Old logs (obs_time == 0.0) default to is_censored=False so behaviour
            # is unchanged for logs that predate this field.
            is_censored = (
                not filled
                and terminal_reason == "cancel"
                and obs_time > 0.0
                and obs_time < horizon_s
            )

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
                observation_time_s=obs_time,
                is_censored=is_censored,
            ))

    return records


# ── IPCW censoring weights ───────────────────────────────────────────────────

def _km_censoring_survival(
    times: list[float],
    is_event: list[bool],
) -> "Callable[[float], float]":
    """Kaplan-Meier estimate of G(t) = P(not censored by t).

    times[i]     : observation time for subject i.
    is_event[i]  : True when subject i experienced the censoring event at times[i];
                   False when they were a competing event (fill) or survived past h.

    Returns a right-continuous step function G_hat(t) clamped to [1e-6, 1].
    """
    from typing import Callable  # noqa: PLC0415

    n = len(times)
    if n == 0:
        return lambda t: 1.0

    # Sort: events before non-events at ties (standard KM convention)
    pairs = sorted(zip(times, is_event), key=lambda x: (x[0], not x[1]))

    G = 1.0
    breakpoints: list[tuple[float, float]] = [(0.0, 1.0)]
    at_risk = n
    i = 0

    while i < len(pairs):
        t = pairs[i][0]
        d = 0   # censoring events at t
        tied = 0
        j = i
        while j < len(pairs) and pairs[j][0] == t:
            if pairs[j][1]:
                d += 1
            tied += 1
            j += 1
        if d > 0:
            G *= 1.0 - d / at_risk
        breakpoints.append((t, G))
        at_risk -= tied
        i = j

    bp_times = [b[0] for b in breakpoints]
    bp_vals = [b[1] for b in breakpoints]

    def G_hat(t: float) -> float:
        lo, hi, idx = 0, len(bp_times) - 1, 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if bp_times[mid] <= t:
                idx = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return max(bp_vals[idx], 1e-6)

    return G_hat


def compute_ipcw_weights(
    records: "list[FillLabelRecord]",
    horizon_s: int,
) -> list[float]:
    """IPCW weights for logistic regression at fixed horizon *horizon_s*.

    Weights correct for the bias introduced when censored observations (orders
    cancelled before the horizon without filling) are excluded from training.

    Returns a weight per record in the *same order* as the input list:
      - is_censored=True  → 0.0  (excluded; only used to build G_hat)
      - filled at t_fill  → 1 / G_hat(t_fill)
      - survived past h   → 1 / G_hat(h)

    Falls back to equal weights (1.0 / 0.0) when no record carries a positive
    observation_time_s (old logs that predate the field).
    """
    h_recs = [r for r in records if r.horizon_s == horizon_s]
    if not h_recs:
        return []

    # Backward-compat: old logs have observation_time_s == 0.0 everywhere
    if all(r.observation_time_s <= 0.0 for r in h_recs):
        return [0.0 if r.is_censored else 1.0 for r in h_recs]

    km_times: list[float] = []
    km_events: list[bool] = []
    for r in h_recs:
        if r.is_censored:
            km_times.append(r.observation_time_s)
            km_events.append(True)           # censoring event
        elif r.filled:
            km_times.append(r.observation_time_s)
            km_events.append(False)          # fill = competing event; "censored" for G
        else:
            km_times.append(float(horizon_s))
            km_events.append(False)          # survived past h, no censoring event

    G_hat = _km_censoring_survival(km_times, km_events)

    weights: list[float] = []
    for r in h_recs:
        if r.is_censored:
            weights.append(0.0)
        elif r.filled:
            weights.append(1.0 / G_hat(r.observation_time_s))
        else:
            weights.append(1.0 / G_hat(float(horizon_s)))
    return weights


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
