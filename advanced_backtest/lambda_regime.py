"""Price-impact regime classifier (calm / elevated / toxic).

Vendored verbatim from altcoin_mm_v1/lambda_regime.py. Lambda here is realised
mid move per unit of signed flow — Kyle's lambda in streaming form. A book being
run over shows a large move for little flow; a calm book absorbs flow without
moving. EmpiricalFillModel's priors are conditioned on the bucket this emits.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .models import LambdaBucket, LambdaRegime


# Minimum ticks before hysteresis allows downgrade
_MIN_TICKS_TOXIC = 8
_MIN_TICKS_ELEVATED = 4


@dataclass
class LambdaRegimeConfig:
    alpha: float = 0.28
    epsilon: float = 1e-9
    max_raw_lambda: float = 1_000.0
    history_window: int = 200
    min_history_for_quantiles: int = 15
    calm_quantile: float = 0.65
    toxic_quantile: float = 0.90
    max_pressure_scale: float = 3.0
    # Spread-conditioned classification
    spread_pressure_start_bps: float = 3.0
    spread_pressure_scale_bps: float = 15.0
    max_spread_pressure: float = 1.5


class LambdaRegimeEngine:
    def __init__(self, config: LambdaRegimeConfig | None = None):
        self.config = config or LambdaRegimeConfig()
        self.history: deque[float] = deque(maxlen=self.config.history_window)
        self.smoothed_lambda = 0.0
        self.last_raw_lambda = 0.0
        # Hysteresis state
        self.last_spread_bps: float = 0.0
        self.last_bucket: LambdaBucket = LambdaBucket.CALM
        self.ticks_in_bucket: int = 0

    @staticmethod
    def _quantile(values: list[float], q: float) -> float:
        if not values:
            return 0.0
        sorted_values = sorted(values)
        index = min(len(sorted_values) - 1, max(0, int(q * (len(sorted_values) - 1))))
        return sorted_values[index]

    def _bucket_for_value(self, value: float, spread_bps: float = 0.0) -> LambdaBucket:
        history_values = list(self.history)
        if len(history_values) < self.config.min_history_for_quantiles:
            baseline = max(sum(history_values) / max(len(history_values), 1), 1e-6)
            if value <= 1.25 * baseline:
                return LambdaBucket.CALM
            if value <= 2.0 * baseline:
                return LambdaBucket.ELEVATED
            return LambdaBucket.TOXIC

        calm_threshold = self._quantile(history_values, self.config.calm_quantile)
        toxic_threshold = self._quantile(history_values, self.config.toxic_quantile)

        # Spread-conditioned stress: widen effective thresholds under spread pressure
        spread_score = min(
            max((spread_bps - self.config.spread_pressure_start_bps) / self.config.spread_pressure_scale_bps, 0.0),
            1.0,
        )
        vol_score = min(value / max(toxic_threshold, 1e-9), 1.0)
        stress = 0.7 * vol_score + 0.3 * spread_score
        spread_mult = 1.0 + stress * self.config.max_spread_pressure

        effective_calm = calm_threshold * spread_mult
        effective_toxic = toxic_threshold * spread_mult

        if value <= effective_calm:
            return LambdaBucket.CALM
        if value <= effective_toxic:
            return LambdaBucket.ELEVATED
        return LambdaBucket.TOXIC

    def _apply_hysteresis(self, raw_bucket: LambdaBucket) -> LambdaBucket:
        """Enforce asymmetric hysteresis: fast to escalate, slow to degrade."""
        prev = self.last_bucket

        # Escalation is always immediate
        if raw_bucket.value > prev.value:  # CALM < ELEVATED < TOXIC (by enum ordering)
            return raw_bucket

        # Degradation requires minimum ticks in current bucket
        if prev == LambdaBucket.TOXIC:
            if self.ticks_in_bucket < _MIN_TICKS_TOXIC:
                return prev  # stay toxic
            # Can only degrade to ELEVATED, never skip to CALM
            if raw_bucket == LambdaBucket.CALM:
                return LambdaBucket.ELEVATED
        elif prev == LambdaBucket.ELEVATED:
            if self.ticks_in_bucket < _MIN_TICKS_ELEVATED:
                return prev  # stay elevated

        return raw_bucket

    def update(
        self,
        realized_mid_move: float,
        signed_flow: float,
        regime_pressure: float = 0.0,
        spread_bps: float = 0.0,
    ) -> LambdaRegime:
        raw_lambda = abs(realized_mid_move) / (abs(signed_flow) + self.config.epsilon)
        pressure_scale = 1.0 + min(max(regime_pressure, 0.0), self.config.max_pressure_scale)
        raw_lambda *= pressure_scale
        raw_lambda = min(raw_lambda, self.config.max_raw_lambda)

        # Adaptive alpha: faster EMA under spread/vol stress
        _stress_scale = min(
            max(spread_bps / 8.0, 0.0) + max(raw_lambda / max(self.smoothed_lambda, 1e-6) - 1.0, 0.0) * 0.3,
            2.5,
        )
        alpha = max(self.config.alpha, min(self.config.alpha * (1.0 + _stress_scale), 0.55))

        self.smoothed_lambda = alpha * raw_lambda + (1.0 - alpha) * self.smoothed_lambda
        self.last_raw_lambda = raw_lambda
        self.history.append(self.smoothed_lambda)
        self.last_spread_bps = spread_bps

        return self.current_regime()

    def current_regime(self) -> LambdaRegime:
        raw_bucket = self._bucket_for_value(self.smoothed_lambda, self.last_spread_bps)
        bucket = self._apply_hysteresis(raw_bucket)

        # Update hysteresis state
        if bucket == self.last_bucket:
            self.ticks_in_bucket += 1
        else:
            self.last_bucket = bucket
            self.ticks_in_bucket = 1

        scalar = {
            LambdaBucket.CALM: 0.0,
            LambdaBucket.ELEVATED: 1.0,
            LambdaBucket.TOXIC: 2.0,
        }[bucket]
        return LambdaRegime(
            raw_value=self.last_raw_lambda,
            smoothed_value=self.smoothed_lambda,
            bucket=bucket,
            scalar=scalar,
        )

    def current_scalar(self) -> float:
        return self.current_regime().scalar

    def value(self) -> LambdaRegime:
        return self.current_regime()
