"""Bayesian empirical fill model.

Vendored verbatim from altcoin_mm_v1/fill_model.py (only the import path
changed). Beta-Binomial posterior per bucket over eight feature dimensions
(symbol, side, distance from touch, queue position, depth regime, flow regime,
lambda regime, horizon), with a global-symbol prior that pair-specific stats
shrink toward. Learns online from every completed passive order; the event log
it writes can warm-start a second pass here or in altcoin_mm_v1 unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path

from .models import (
    BucketStats,
    FillModelBucket,
    FillProbabilityEstimate,
    LambdaBucket,
    LambdaRegime,
    LoggedOrderEvent,
    MarketState,
    Side,
)


@dataclass
class FillModelConfig:
    fill_prior_strength: float = 8.0
    adverse_prior_strength: float = 6.0
    source_prior_strength: float = 5.0
    pair_prior_strength: float = 6.0
    sell_heavy_threshold: float = -0.35
    buy_heavy_threshold: float = 0.35
    global_symbol: str = "*"
    spread_fill_haircut_start_bps: float = 20.0
    spread_fill_haircut_end_bps: float = 300.0
    min_spread_fill_multiplier: float = 0.10
    spread_adverse_uplift_start_bps: float = 20.0
    spread_adverse_uplift_end_bps: float = 300.0
    max_spread_adverse_uplift: float = 0.35
    # Distance-aware fill decay: fill_prob *= exp(-k * distance_bps)
    # Corrects phantom edge from quoting far outside the market spread
    distance_decay_k: float = 0.15


def _normalize_symbol(symbol: str | None) -> str:
    if not symbol:
        return ""
    return symbol.upper().replace("/", "").replace("-", "")


class EmpiricalFillModel:
    def __init__(self, config: FillModelConfig | None = None):
        self.config = config or FillModelConfig()
        self.fill_stats: dict[FillModelBucket, BucketStats] = {}
        self.adverse_stats: dict[FillModelBucket, BucketStats] = {}
        self.trade_fill_stats: dict[FillModelBucket, BucketStats] = {}
        self.book_event_fill_stats: dict[FillModelBucket, BucketStats] = {}
        self.snapshot_fill_stats: dict[FillModelBucket, BucketStats] = {}
        self.order_event_log: list[LoggedOrderEvent] = []

    @staticmethod
    def _linear_scale(value: float, start: float, end: float) -> float:
        if end <= start:
            return 1.0 if value >= end else 0.0
        if value <= start:
            return 0.0
        if value >= end:
            return 1.0
        return (value - start) / (end - start)

    def _queue_bucket(self, queue_ahead: float, local_depth: float) -> str:
        ratio = 0.0 if local_depth <= 0 else queue_ahead / local_depth
        if ratio < 0.10:
            return "0-10%"
        if ratio < 0.30:
            return "10-30%"
        if ratio < 0.60:
            return "30-60%"
        return "60%+"

    def _depth_bucket(self, depth_ratio: float) -> str:
        if depth_ratio < 0.60:
            return "thin"
        if depth_ratio < 1.20:
            return "normal"
        return "thick"

    def _flow_bucket(self, trade_sign: float) -> str:
        if trade_sign <= self.config.sell_heavy_threshold:
            return "sell-heavy"
        if trade_sign >= self.config.buy_heavy_threshold:
            return "buy-heavy"
        return "neutral"

    def bucket_for_quote(
        self,
        symbol: str,
        side: Side,
        relative_to_touch_ticks: int,
        queue_ahead: float,
        state: MarketState,
        lambda_regime: LambdaRegime,
        horizon_s: int,
    ) -> FillModelBucket:
        depth_ratio = state.top_depth / max(state.top_depth_baseline, 1e-9)
        return FillModelBucket(
            symbol=_normalize_symbol(symbol),
            side=side,
            relative_to_touch_ticks=relative_to_touch_ticks,
            queue_bucket=self._queue_bucket(queue_ahead, state.top_depth),
            depth_bucket=self._depth_bucket(depth_ratio),
            flow_bucket=self._flow_bucket(state.trade_sign),
            regime_bucket=lambda_regime.bucket,
            horizon_s=horizon_s,
        )

    def _global_bucket(self, bucket: FillModelBucket) -> FillModelBucket:
        return FillModelBucket(
            symbol=self.config.global_symbol,
            side=bucket.side,
            relative_to_touch_ticks=bucket.relative_to_touch_ticks,
            queue_bucket=bucket.queue_bucket,
            depth_bucket=bucket.depth_bucket,
            flow_bucket=bucket.flow_bucket,
            regime_bucket=bucket.regime_bucket,
            horizon_s=bucket.horizon_s,
        )

    def _fill_prior_mean(self, bucket: FillModelBucket) -> float:
        base = {
            -1: 0.65,
            0: 0.30,
            1: 0.08,
        }.get(bucket.relative_to_touch_ticks, 0.05)

        base += {
            "0-10%": 0.08,
            "10-30%": 0.02,
            "30-60%": -0.06,
            "60%+": -0.14,
        }[bucket.queue_bucket]

        if bucket.depth_bucket == "thin":
            base += 0.01
        elif bucket.depth_bucket == "thick":
            base -= 0.03

        if bucket.side == Side.BID and bucket.flow_bucket == "sell-heavy":
            base += 0.08
        elif bucket.side == Side.BID and bucket.flow_bucket == "buy-heavy":
            base -= 0.07
        elif bucket.side == Side.ASK and bucket.flow_bucket == "buy-heavy":
            base += 0.08
        elif bucket.side == Side.ASK and bucket.flow_bucket == "sell-heavy":
            base -= 0.07

        if bucket.regime_bucket == LambdaBucket.ELEVATED:
            base -= 0.03
        elif bucket.regime_bucket == LambdaBucket.TOXIC:
            base -= 0.07

        return max(0.01, min(0.90, base))

    def _adverse_prior_mean(self, bucket: FillModelBucket) -> float:
        base = 0.10

        if bucket.relative_to_touch_ticks == -1:
            base += 0.06
        elif bucket.relative_to_touch_ticks == 0:
            base += 0.03

        if bucket.depth_bucket == "thin":
            base += 0.08
        elif bucket.depth_bucket == "thick":
            base -= 0.03

        if bucket.side == Side.BID and bucket.flow_bucket == "sell-heavy":
            base += 0.12
        elif bucket.side == Side.BID and bucket.flow_bucket == "buy-heavy":
            base -= 0.04
        elif bucket.side == Side.ASK and bucket.flow_bucket == "buy-heavy":
            base += 0.12
        elif bucket.side == Side.ASK and bucket.flow_bucket == "sell-heavy":
            base -= 0.04

        if bucket.regime_bucket == LambdaBucket.ELEVATED:
            base += 0.05
        elif bucket.regime_bucket == LambdaBucket.TOXIC:
            base += 0.16

        return max(0.02, min(0.98, base))

    def _source_prior_mean(self, bucket: FillModelBucket, source: str) -> float:
        if source == "trade":
            base = {
                -1: 0.55,
                0: 0.20,
                1: 0.04,
            }.get(bucket.relative_to_touch_ticks, 0.02)
        elif source == "book_event":
            base = {
                -1: 0.08,
                0: 0.12,
                1: 0.03,
            }.get(bucket.relative_to_touch_ticks, 0.02)
        else:
            base = {
                -1: 0.05,
                0: 0.06,
                1: 0.02,
            }.get(bucket.relative_to_touch_ticks, 0.01)

        if bucket.regime_bucket == LambdaBucket.TOXIC and source != "trade":
            base -= 0.02
        if bucket.depth_bucket == "thin" and source == "trade":
            base += 0.03
        return max(0.005, min(0.85, base))

    def _posterior_mean(
        self,
        stats: BucketStats | None,
        prior_mean: float,
        prior_strength: float,
    ) -> float:
        alpha = prior_mean * prior_strength
        beta = (1.0 - prior_mean) * prior_strength
        attempts = 0 if stats is None else stats.attempts
        successes = 0 if stats is None else stats.successes
        return (alpha + successes) / (alpha + beta + attempts)

    def _record_bucket_stat(
        self,
        mapping: dict[FillModelBucket, BucketStats],
        bucket: FillModelBucket,
        success: bool,
    ) -> None:
        stats = mapping.setdefault(bucket, BucketStats())
        stats.attempts += 1
        stats.successes += int(success)

    def record_order_event(self, event: LoggedOrderEvent) -> None:
        self.order_event_log.append(event)
        depth_ratio = event.local_depth / max(event.local_depth_baseline, 1e-9)
        symbol = _normalize_symbol(event.symbol)
        bucket = FillModelBucket(
            symbol=symbol,
            side=event.side,
            relative_to_touch_ticks=event.relative_to_touch_ticks,
            queue_bucket=self._queue_bucket(event.queue_ahead, event.local_depth),
            depth_bucket=self._depth_bucket(depth_ratio),
            flow_bucket=self._flow_bucket(event.trade_sign),
            regime_bucket=event.regime_bucket,
            horizon_s=event.horizon_s,
        )
        global_bucket = self._global_bucket(bucket)

        for target_bucket in (global_bucket, bucket):
            self._record_bucket_stat(self.fill_stats, target_bucket, event.filled)
            self._record_bucket_stat(
                self.adverse_stats,
                target_bucket,
                event.price_moved_through,
            )
            self._record_bucket_stat(
                self.trade_fill_stats,
                target_bucket,
                event.trade_fill_qty > 0,
            )
            self._record_bucket_stat(
                self.book_event_fill_stats,
                target_bucket,
                event.book_event_fill_qty > 0,
            )
            self._record_bucket_stat(
                self.snapshot_fill_stats,
                target_bucket,
                event.snapshot_fill_qty > 0,
            )

    def _pair_adjusted_probability(
        self,
        mapping: dict[FillModelBucket, BucketStats],
        bucket: FillModelBucket,
        base_prior: float,
        prior_strength: float,
    ) -> float:
        global_bucket = self._global_bucket(bucket)
        global_stats = mapping.get(global_bucket)
        global_posterior = self._posterior_mean(
            global_stats,
            base_prior,
            prior_strength,
        )
        pair_stats = mapping.get(bucket)
        return self._posterior_mean(
            pair_stats,
            global_posterior,
            self.config.pair_prior_strength,
        )

    def predict(
        self,
        symbol: str,
        side: Side,
        relative_to_touch_ticks: int,
        queue_ahead: float,
        state: MarketState,
        lambda_regime: LambdaRegime,
        horizon_s: int = 5,
        distance_bps: float = 0.0,
    ) -> FillProbabilityEstimate:
        bucket = self.bucket_for_quote(
            symbol=symbol,
            side=side,
            relative_to_touch_ticks=relative_to_touch_ticks,
            queue_ahead=queue_ahead,
            state=state,
            lambda_regime=lambda_regime,
            horizon_s=horizon_s,
        )

        fill_prob = self._pair_adjusted_probability(
            self.fill_stats,
            bucket,
            self._fill_prior_mean(bucket),
            self.config.fill_prior_strength,
        )
        adverse_move_prob = self._pair_adjusted_probability(
            self.adverse_stats,
            bucket,
            self._adverse_prior_mean(bucket),
            self.config.adverse_prior_strength,
        )
        trade_fill_prob = self._pair_adjusted_probability(
            self.trade_fill_stats,
            bucket,
            self._source_prior_mean(bucket, "trade"),
            self.config.source_prior_strength,
        )
        book_event_fill_prob = self._pair_adjusted_probability(
            self.book_event_fill_stats,
            bucket,
            self._source_prior_mean(bucket, "book_event"),
            self.config.source_prior_strength,
        )
        snapshot_fill_prob = self._pair_adjusted_probability(
            self.snapshot_fill_stats,
            bucket,
            self._source_prior_mean(bucket, "snapshot"),
            self.config.source_prior_strength,
        )

        source_max = max(trade_fill_prob, book_event_fill_prob, snapshot_fill_prob)
        fill_prob = min(0.99, max(fill_prob, source_max))
        trade_fill_prob = min(fill_prob, trade_fill_prob)
        book_event_fill_prob = min(fill_prob, book_event_fill_prob)
        snapshot_fill_prob = min(fill_prob, snapshot_fill_prob)

        spread_stress = self._linear_scale(
            state.spread_bps,
            self.config.spread_fill_haircut_start_bps,
            self.config.spread_fill_haircut_end_bps,
        )
        fill_multiplier = (
            1.0
            - spread_stress * (1.0 - self.config.min_spread_fill_multiplier)
        )
        fill_prob *= fill_multiplier
        trade_fill_prob *= fill_multiplier
        book_event_fill_prob *= fill_multiplier
        snapshot_fill_prob *= fill_multiplier

        # Distance-aware decay: quotes far outside market spread have lower fill probability
        if distance_bps > 0.0 and self.config.distance_decay_k > 0.0:
            distance_multiplier = math.exp(-self.config.distance_decay_k * distance_bps)
            fill_prob *= distance_multiplier
            trade_fill_prob *= distance_multiplier
            book_event_fill_prob *= distance_multiplier
            snapshot_fill_prob *= distance_multiplier

        adverse_spread_stress = self._linear_scale(
            state.spread_bps,
            self.config.spread_adverse_uplift_start_bps,
            self.config.spread_adverse_uplift_end_bps,
        )
        adverse_move_prob = min(
            0.99,
            adverse_move_prob
            + adverse_spread_stress * self.config.max_spread_adverse_uplift,
        )

        return FillProbabilityEstimate(
            fill_prob=fill_prob,
            adverse_move_prob=adverse_move_prob,
            trade_fill_prob=trade_fill_prob,
            book_event_fill_prob=book_event_fill_prob,
            snapshot_fill_prob=snapshot_fill_prob,
            bucket=bucket,
        )

    def predict_fill_prob(
        self,
        symbol: str,
        side: Side,
        relative_to_touch_ticks: int,
        queue_ahead: float,
        state: MarketState,
        lambda_regime: LambdaRegime,
        horizon_s: int = 5,
        distance_bps: float = 0.0,
    ) -> float:
        return self.predict(
            symbol,
            side,
            relative_to_touch_ticks,
            queue_ahead,
            state,
            lambda_regime,
            horizon_s,
            distance_bps=distance_bps,
        ).fill_prob

    def predict_adverse_move_prob(
        self,
        symbol: str,
        side: Side,
        relative_to_touch_ticks: int,
        queue_ahead: float,
        state: MarketState,
        lambda_regime: LambdaRegime,
        horizon_s: int = 5,
        distance_bps: float = 0.0,
    ) -> float:
        return self.predict(
            symbol,
            side,
            relative_to_touch_ticks,
            queue_ahead,
            state,
            lambda_regime,
            horizon_s,
            distance_bps=distance_bps,
        ).adverse_move_prob

    @staticmethod
    def _bucket_to_dict(bucket: FillModelBucket) -> dict[str, str | int]:
        return {
            "symbol": bucket.symbol,
            "side": bucket.side.value,
            "relative_to_touch_ticks": bucket.relative_to_touch_ticks,
            "queue_bucket": bucket.queue_bucket,
            "depth_bucket": bucket.depth_bucket,
            "flow_bucket": bucket.flow_bucket,
            "regime_bucket": bucket.regime_bucket.value,
            "horizon_s": bucket.horizon_s,
        }

    @staticmethod
    def _bucket_from_dict(payload: dict[str, str | int]) -> FillModelBucket:
        return FillModelBucket(
            symbol=str(payload.get("symbol", "")),
            side=Side(str(payload["side"])),
            relative_to_touch_ticks=int(payload["relative_to_touch_ticks"]),
            queue_bucket=str(payload["queue_bucket"]),
            depth_bucket=str(payload["depth_bucket"]),
            flow_bucket=str(payload["flow_bucket"]),
            regime_bucket=LambdaBucket(str(payload["regime_bucket"])),
            horizon_s=int(payload["horizon_s"]),
        )

    @staticmethod
    def _event_to_dict(event: LoggedOrderEvent) -> dict[str, str | float | int | bool]:
        return {
            "timestamp": event.timestamp,
            "symbol": event.symbol,
            "side": event.side.value,
            "relative_to_touch_ticks": event.relative_to_touch_ticks,
            "queue_ahead": event.queue_ahead,
            "local_depth": event.local_depth,
            "local_depth_baseline": event.local_depth_baseline,
            "spread_pct": event.spread_pct,
            "short_vol": event.short_vol,
            "trade_count": event.trade_count,
            "trade_sign": event.trade_sign,
            "regime_bucket": event.regime_bucket.value,
            "horizon_s": event.horizon_s,
            "filled": event.filled,
            "price_moved_through": event.price_moved_through,
            "filled_qty": event.filled_qty,
            "fill_ratio": event.fill_ratio,
            "trade_fill_qty": event.trade_fill_qty,
            "book_event_fill_qty": event.book_event_fill_qty,
            "snapshot_fill_qty": event.snapshot_fill_qty,
            "terminal_reason": event.terminal_reason,
        }

    @staticmethod
    def _event_from_dict(payload: dict[str, str | float | int | bool]) -> LoggedOrderEvent:
        return LoggedOrderEvent(
            timestamp=float(payload["timestamp"]),
            symbol=str(payload["symbol"]),
            side=Side(str(payload["side"])),
            relative_to_touch_ticks=int(payload["relative_to_touch_ticks"]),
            queue_ahead=float(payload["queue_ahead"]),
            local_depth=float(payload["local_depth"]),
            local_depth_baseline=float(payload["local_depth_baseline"]),
            spread_pct=float(payload["spread_pct"]),
            short_vol=float(payload["short_vol"]),
            trade_count=float(payload["trade_count"]),
            trade_sign=float(payload["trade_sign"]),
            regime_bucket=LambdaBucket(str(payload["regime_bucket"])),
            horizon_s=int(payload["horizon_s"]),
            filled=bool(payload["filled"]),
            price_moved_through=bool(payload["price_moved_through"]),
            filled_qty=float(payload.get("filled_qty", 0.0)),
            fill_ratio=float(payload.get("fill_ratio", 0.0)),
            trade_fill_qty=float(payload.get("trade_fill_qty", 0.0)),
            book_event_fill_qty=float(payload.get("book_event_fill_qty", 0.0)),
            snapshot_fill_qty=float(payload.get("snapshot_fill_qty", 0.0)),
            terminal_reason=str(payload.get("terminal_reason", "")),
        )

    def bucket_stats_snapshot(self) -> list[dict[str, str | int]]:
        records: list[dict[str, str | int]] = []
        all_buckets = (
            set(self.fill_stats)
            | set(self.adverse_stats)
            | set(self.trade_fill_stats)
            | set(self.book_event_fill_stats)
            | set(self.snapshot_fill_stats)
        )
        for bucket in sorted(
            all_buckets,
            key=lambda item: (
                item.symbol,
                item.side.value,
                item.relative_to_touch_ticks,
                item.queue_bucket,
                item.depth_bucket,
                item.flow_bucket,
                item.regime_bucket.value,
                item.horizon_s,
            ),
        ):
            fill_stats = self.fill_stats.get(bucket, BucketStats())
            adverse_stats = self.adverse_stats.get(bucket, BucketStats())
            trade_stats = self.trade_fill_stats.get(bucket, BucketStats())
            book_event_stats = self.book_event_fill_stats.get(bucket, BucketStats())
            snapshot_stats = self.snapshot_fill_stats.get(bucket, BucketStats())
            record = self._bucket_to_dict(bucket)
            record.update(
                {
                    "fill_attempts": fill_stats.attempts,
                    "fill_successes": fill_stats.successes,
                    "adverse_attempts": adverse_stats.attempts,
                    "adverse_successes": adverse_stats.successes,
                    "trade_fill_attempts": trade_stats.attempts,
                    "trade_fill_successes": trade_stats.successes,
                    "book_event_fill_attempts": book_event_stats.attempts,
                    "book_event_fill_successes": book_event_stats.successes,
                    "snapshot_fill_attempts": snapshot_stats.attempts,
                    "snapshot_fill_successes": snapshot_stats.successes,
                }
            )
            records.append(record)
        return records

    def save_event_log(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            for event in self.order_event_log:
                handle.write(json.dumps(self._event_to_dict(event), sort_keys=True))
                handle.write("\n")

    def load_event_log(
        self, path: str | Path, until_ts: float | None = None
    ) -> None:
        """Load a serialised event log into this model's posterior.

        Args:
            path: Path to the fill_events.jsonl written by save_event_log.
            until_ts: If set, only load events whose label has matured by this
                epoch timestamp.  An event with placement time T and horizon H
                matures at T + H; loading events with maturity > until_ts would
                inject future information into the prior (temporal leakage).
        """
        target = Path(path)
        if not target.exists():
            return
        with target.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                ev = self._event_from_dict(json.loads(stripped))
                if until_ts is not None and ev.timestamp + ev.horizon_s > until_ts:
                    continue  # label not yet matured at until_ts — skip to avoid leakage
                self.record_order_event(ev)

    def save_bucket_stats(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            json.dump(self.bucket_stats_snapshot(), handle, indent=2, sort_keys=True)

    def load_bucket_stats(self, path: str | Path) -> None:
        target = Path(path)
        if not target.exists():
            return
        with target.open("r", encoding="utf-8") as handle:
            records = json.load(handle)
        for payload in records:
            bucket = self._bucket_from_dict(payload)
            self.fill_stats[bucket] = BucketStats(
                attempts=int(payload["fill_attempts"]),
                successes=int(payload["fill_successes"]),
            )
            self.adverse_stats[bucket] = BucketStats(
                attempts=int(payload["adverse_attempts"]),
                successes=int(payload["adverse_successes"]),
            )
            self.trade_fill_stats[bucket] = BucketStats(
                attempts=int(payload.get("trade_fill_attempts", 0)),
                successes=int(payload.get("trade_fill_successes", 0)),
            )
            self.book_event_fill_stats[bucket] = BucketStats(
                attempts=int(payload.get("book_event_fill_attempts", 0)),
                successes=int(payload.get("book_event_fill_successes", 0)),
            )
            self.snapshot_fill_stats[bucket] = BucketStats(
                attempts=int(payload.get("snapshot_fill_attempts", 0)),
                successes=int(payload.get("snapshot_fill_successes", 0)),
            )
