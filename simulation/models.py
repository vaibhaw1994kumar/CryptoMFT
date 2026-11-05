"""Shared dataclasses for the fill model and regime engine.

Vendored from altcoin_mm_v1/models.py, trimmed to what new_backtest uses.
Kept field-for-field identical so fill_events.jsonl written here is readable by
the altcoin_mm_v1 EmpiricalFillModel and vice versa — the two can warm-start
each other.

Vendored rather than imported because altcoin_mm_v1/__init__.py eagerly pulls
in pyarrow, Parquet loaders and exchange collectors; none of that belongs in a
replay engine's dependency set.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Side(str, Enum):
    BID = "bid"
    ASK = "ask"


class LambdaBucket(str, Enum):
    CALM = "calm"
    ELEVATED = "elevated"
    TOXIC = "toxic"


@dataclass(frozen=True)
class MarketState:
    timestamp: float
    best_bid: float
    best_ask: float
    bid_size: float
    ask_size: float
    mid: float
    microprice: float
    spread_bps: float
    imbalance: float
    top5_imbalance: float
    depth_weighted_imbalance: float
    spread_pct: float
    microprice_deviation_bps: float
    microprice_drift_bps: float
    short_vol: float
    short_vol_bps: float
    trade_sign: float
    impact_trade_sign: float
    flow_toxicity: float
    recent_trade_rate: float
    recent_signed_volume: float
    trade_burst_flag: bool
    trade_cluster_score: float
    depth_collapse: float
    liquidity_evaporation: float
    cancel_burst: float
    cancel_imbalance: float
    replenish_imbalance: float
    queue_fragility: float
    book_hole: bool
    recent_sweep_flag: bool
    sweep_risk: bool
    top_depth: float
    top_depth_baseline: float
    total_depth: float
    tick_size: float
    trade_split_regime: float = 0.0
    trade_split_flow: float = 0.0
    vpin: float = 0.0
    whale_regime: float = 0.0


@dataclass(frozen=True)
class LambdaRegime:
    raw_value: float
    smoothed_value: float
    bucket: LambdaBucket
    scalar: float


@dataclass
class BucketStats:
    attempts: int = 0
    successes: int = 0


@dataclass(frozen=True)
class FillModelBucket:
    symbol: str
    side: Side
    relative_to_touch_ticks: int
    queue_bucket: str
    depth_bucket: str
    flow_bucket: str
    regime_bucket: LambdaBucket
    horizon_s: int


@dataclass(frozen=True)
class LoggedOrderEvent:
    timestamp: float
    symbol: str
    side: Side
    relative_to_touch_ticks: int
    queue_ahead: float
    local_depth: float
    local_depth_baseline: float
    spread_pct: float
    short_vol: float
    trade_count: float
    trade_sign: float
    regime_bucket: LambdaBucket
    horizon_s: int
    filled: bool
    price_moved_through: bool
    filled_qty: float = 0.0
    fill_ratio: float = 0.0
    trade_fill_qty: float = 0.0
    book_event_fill_qty: float = 0.0
    snapshot_fill_qty: float = 0.0
    # terminal_reason: why the order ended (full_fill / partial_then_cancel / cancel)
    # Enables censoring-aware training: skip horizon h if cancelled before h and not filled.
    terminal_reason: str = ""
    # How long the order was observable: fill_time_s for fills, cancel_time_s for cancels.
    # Used by IPCW censoring weights in FillModelV1.fit().
    observation_time_s: float = 0.0
    # True when the price level was absent from the book at placement time.
    # Used to segment absent-level vs present-level fill rates and to validate D2.
    level_was_absent: bool = False


@dataclass(frozen=True)
class FillProbabilityEstimate:
    fill_prob: float
    adverse_move_prob: float
    trade_fill_prob: float
    book_event_fill_prob: float
    snapshot_fill_prob: float
    bucket: FillModelBucket
