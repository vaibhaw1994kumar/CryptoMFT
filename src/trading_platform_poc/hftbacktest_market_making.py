from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from functools import lru_cache
from math import isfinite
from pathlib import Path
from typing import Any, Sequence

from trading_platform_poc.optional_dependencies import (
    require_hftbacktest,
    require_hftbacktest_binance_futures,
    require_hftbacktest_snapshot,
    require_hftbacktest_stats,
    require_hftbacktest_tardis,
    require_numba,
    require_numpy,
)


class HftQueueModel(StrEnum):
    RISK_ADVERSE = "risk_adverse"
    LOG_PROBABILITY_2 = "log_probability_2"
    POWER_PROBABILITY = "power_probability"


class HftExchangeModel(StrEnum):
    NO_PARTIAL_FILL = "no_partial_fill"
    PARTIAL_FILL = "partial_fill"


class HftLatencyModel(StrEnum):
    CONSTANT = "constant"
    INTERPOLATED = "interpolated"


class HftDepthImplementation(StrEnum):
    HASH_MAP = "hash_map"
    ROI_VECTOR = "roi_vector"


class HftOrderScenario(StrEnum):
    LIMIT_CANCEL = "limit_cancel"
    MARKET = "market"
    POST_ONLY_REJECT = "post_only_reject"


class HftTardisSnapshotMode(StrEnum):
    PROCESS = "process"
    IGNORE_START_OF_DAY = "ignore_sod"
    IGNORE_ALL = "ignore"


@dataclass(frozen=True, slots=True)
class SyntheticHftConfig:
    duration_seconds: int = 300
    events_per_second: int = 20
    mid_price: float = 60_000.0
    tick_size: float = 0.1
    lot_size: float = 0.001
    levels: int = 20
    volatility_bps_per_sqrt_second: float = 0.35
    trade_probability: float = 0.30
    base_latency_us: float = 500.0
    latency_jitter_us: float = 100.0
    seed: int = 42
    start_ns: int = 1_725_000_000_000_000_000


@dataclass(frozen=True, slots=True)
class SyntheticHftDataset:
    data_path: Path
    initial_snapshot_path: Path
    preview_path: Path
    event_count: int
    depth_event_count: int
    trade_event_count: int
    minimum_latency_ns: int
    mean_latency_ns: float
    maximum_latency_ns: int


@dataclass(frozen=True, slots=True)
class HftBacktestAssetConfig:
    contract_size: float = 1.0
    tick_size: float = 0.1
    lot_size: float = 0.001
    maker_fee: float = -0.00005
    taker_fee: float = 0.0007
    entry_latency_ns: int = 500_000
    response_latency_ns: int = 500_000
    latency_model: HftLatencyModel = HftLatencyModel.CONSTANT
    order_latency_path: Path | None = None
    order_latency_paths: tuple[Path, ...] = ()
    queue_model: HftQueueModel = HftQueueModel.POWER_PROBABILITY
    queue_model_exponent: float = 2.0
    exchange_model: HftExchangeModel = HftExchangeModel.NO_PARTIAL_FILL
    depth_implementation: HftDepthImplementation = (
        HftDepthImplementation.ROI_VECTOR
    )
    roi_lower_bound: float | None = 50_000.0
    roi_upper_bound: float | None = 70_000.0
    last_trades_capacity: int = 100_000


@dataclass(frozen=True, slots=True)
class GridMarketMakingConfig:
    grid_levels: int = 20
    max_position: float = 5.0
    grid_interval_ticks: int = 10
    half_spread_ticks: int = 20
    grid_interval: float | None = None
    half_spread: float | None = None
    order_quantity: float = 0.1
    refresh_interval_ns: int = 100_000_000
    inventory_skew: float = 0.0
    inventory_skew_price: float | None = None
    normalize_inventory: bool = False
    recorder_capacity: int = 5_000_000
    recorder_output_path: Path | None = None
    statistics_book_size: int = 10_000
    statistics_resample: str | None = None
    generate_statistics: bool = True


@dataclass(frozen=True, slots=True)
class HftBacktestRunResult:
    records: Any
    statistics: Any | None
    summary: tuple[dict[str, Any], ...]
    final_position: float
    final_balance: float
    final_fee: float
    record_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": list(self.summary),
            "final_position": self.final_position,
            "final_balance": self.final_balance,
            "final_fee": self.final_fee,
            "record_count": self.record_count,
        }


@dataclass(frozen=True, slots=True)
class MultiAssetGridConfig:
    asset_name: str
    data_paths: tuple[Path, ...]
    initial_snapshot_path: Path
    mid_price: float
    tick_size: float
    lot_size: float
    order_latency_paths: tuple[Path, ...] = ()
    notional_order_quantity: float = 100.0
    capital: float = 2_000.0
    grid_levels: int = 20
    half_spread_fraction: float = 0.0008
    grid_interval_fraction: float = 0.0008
    inventory_skew_fraction: float = 0.000025
    recorder_capacity: int = 50_000_000
    statistics_resample: str = "5m"


@dataclass(frozen=True, slots=True)
class MultiAssetGridResult:
    runs: dict[str, HftBacktestRunResult]
    equity_curves: dict[str, Any]
    portfolio_equity: Any
    capital_per_asset: dict[str, float]


@dataclass(frozen=True, slots=True)
class HftOrderLifecycleResult:
    scenario: HftOrderScenario
    submitted_status: int
    submitted_request: int
    acknowledged_status: int
    acknowledged_request: int
    cancel_requested: bool
    cancel_request_state: int
    final_status: int
    final_request: int
    inactive_cleared: bool
    submission_timestamp_ns: int
    acknowledgment_timestamp_ns: int
    final_timestamp_ns: int
    final_position: float
    final_balance: float
    final_fee: float
    execution_price: float

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["scenario"] = self.scenario.value
        return data


def generate_synthetic_hft_dataset(
    output_directory: str | Path,
    config: SyntheticHftConfig | None = None,
) -> SyntheticHftDataset:
    effective = config or SyntheticHftConfig()
    _validate_synthetic_config(effective)
    hftbacktest = require_hftbacktest()
    np = require_numpy()
    rng = np.random.default_rng(effective.seed)
    dtype = hftbacktest.event_dtype
    base_flags = hftbacktest.EXCH_EVENT | hftbacktest.LOCAL_EVENT

    snapshot, bids, asks = _generate_initial_snapshot(
        np=np,
        dtype=dtype,
        base_flags=base_flags,
        buy_event=hftbacktest.BUY_EVENT,
        sell_event=hftbacktest.SELL_EVENT,
        snapshot_event=hftbacktest.DEPTH_SNAPSHOT_EVENT,
        config=effective,
        rng=rng,
    )
    total_steps = effective.duration_seconds * effective.events_per_second
    step_ns = max(1, int(1_000_000_000 / effective.events_per_second))
    dt_seconds = step_ns / 1_000_000_000
    sigma_fraction = effective.volatility_bps_per_sqrt_second / 10_000
    current_mid = effective.mid_price
    rows: list[tuple[int, int, int, float, float, int, int, float]] = []

    for step in range(total_steps):
        exchange_timestamp_ns = effective.start_ns + step * step_ns
        latency_us = max(
            1.0,
            effective.base_latency_us
            + rng.normal(0.0, effective.latency_jitter_us),
        )
        local_timestamp_ns = exchange_timestamp_ns + int(latency_us * 1_000)
        shock = rng.normal(0.0, sigma_fraction * np.sqrt(dt_seconds))
        current_mid = max(
            effective.tick_size * 4,
            current_mid * np.exp(shock),
        )
        current_mid = _round_to_increment(
            current_mid,
            effective.tick_size,
        )
        best_bid = _round_to_increment(
            current_mid - effective.tick_size,
            effective.tick_size,
        )
        best_ask = _round_to_increment(
            current_mid + effective.tick_size,
            effective.tick_size,
        )
        levels_to_update = max(
            1,
            min(effective.levels, int(rng.integers(1, 5))),
        )
        for level in rng.choice(
            effective.levels,
            size=levels_to_update,
            replace=False,
        ):
            level = int(level)
            bid_price = _round_to_increment(
                best_bid - level * effective.tick_size,
                effective.tick_size,
            )
            ask_price = _round_to_increment(
                best_ask + level * effective.tick_size,
                effective.tick_size,
            )
            depth_decay = max(
                0.25,
                np.exp(-level / max(effective.levels / 3, 1)),
            )
            bid_quantity = _round_positive_to_increment(
                rng.lognormal(-0.15, 0.65) * depth_decay,
                effective.lot_size,
            )
            ask_quantity = _round_positive_to_increment(
                rng.lognormal(-0.15, 0.65) * depth_decay,
                effective.lot_size,
            )
            bids[bid_price] = bid_quantity
            asks[ask_price] = ask_quantity
            rows.append(
                _hft_event(
                    base_flags
                    | hftbacktest.BUY_EVENT
                    | hftbacktest.DEPTH_EVENT,
                    exchange_timestamp_ns,
                    local_timestamp_ns,
                    bid_price,
                    bid_quantity,
                )
            )
            rows.append(
                _hft_event(
                    base_flags
                    | hftbacktest.SELL_EVENT
                    | hftbacktest.DEPTH_EVENT,
                    exchange_timestamp_ns,
                    local_timestamp_ns,
                    ask_price,
                    ask_quantity,
                )
            )

        if rng.random() < effective.trade_probability:
            buyer_initiated = bool(rng.integers(0, 2))
            trade_side = (
                hftbacktest.BUY_EVENT
                if buyer_initiated
                else hftbacktest.SELL_EVENT
            )
            trade_price = best_ask if buyer_initiated else best_bid
            trade_quantity = _round_positive_to_increment(
                rng.lognormal(-1.4, 0.8),
                effective.lot_size,
            )
            rows.append(
                _hft_event(
                    base_flags | trade_side | hftbacktest.TRADE_EVENT,
                    exchange_timestamp_ns,
                    local_timestamp_ns,
                    trade_price,
                    trade_quantity,
                )
            )
            book = asks if buyer_initiated else bids
            remaining = max(
                0.0,
                book.get(trade_price, trade_quantity) - trade_quantity,
            )
            remaining = (
                round(remaining / effective.lot_size) * effective.lot_size
            )
            book[trade_price] = remaining
            depth_side = (
                hftbacktest.SELL_EVENT
                if buyer_initiated
                else hftbacktest.BUY_EVENT
            )
            rows.append(
                _hft_event(
                    base_flags | depth_side | hftbacktest.DEPTH_EVENT,
                    exchange_timestamp_ns,
                    local_timestamp_ns,
                    trade_price,
                    remaining,
                )
            )

    data = np.array(rows, dtype=dtype)
    data = data[np.argsort(data["exch_ts"], kind="stable")]
    validate_hft_event_data(data)
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    data_path = output / "synthetic_btcusdt.npz"
    snapshot_path = output / "synthetic_btcusdt_initial_snapshot.npz"
    preview_path = output / "synthetic_btcusdt_preview.csv"
    _save_npz(data_path, data)
    _save_npz(snapshot_path, snapshot)
    _save_preview_csv(preview_path, data)
    trade_mask = (
        data["ev"] & hftbacktest.TRADE_EVENT
    ) == hftbacktest.TRADE_EVENT
    depth_mask = (
        data["ev"] & hftbacktest.DEPTH_EVENT
    ) == hftbacktest.DEPTH_EVENT
    latencies = data["local_ts"] - data["exch_ts"]
    return SyntheticHftDataset(
        data_path=data_path,
        initial_snapshot_path=snapshot_path,
        preview_path=preview_path,
        event_count=len(data),
        depth_event_count=int(depth_mask.sum()),
        trade_event_count=int(trade_mask.sum()),
        minimum_latency_ns=int(latencies.min()),
        mean_latency_ns=float(latencies.mean()),
        maximum_latency_ns=int(latencies.max()),
    )


def validate_hft_event_data(data: Any) -> None:
    hftbacktest = require_hftbacktest()
    np = require_numpy()
    if data.dtype != hftbacktest.event_dtype:
        raise ValueError(f"unexpected HftBacktest event dtype: {data.dtype}")
    if len(data) == 0:
        raise ValueError("HftBacktest event data is empty")
    if np.any(data["local_ts"] < data["exch_ts"]):
        raise ValueError("negative feed latency found")
    exchange_events = (
        data["ev"] & hftbacktest.EXCH_EVENT
    ) == hftbacktest.EXCH_EVENT
    local_events = (
        data["ev"] & hftbacktest.LOCAL_EVENT
    ) == hftbacktest.LOCAL_EVENT
    if np.any(np.diff(data["exch_ts"][exchange_events]) < 0):
        raise ValueError("exchange timestamps are not chronological")
    if np.any(np.diff(data["local_ts"][local_events]) < 0):
        raise ValueError("local timestamps are not chronological")
    if np.any(data["px"] <= 0):
        raise ValueError("non-positive price found")
    if np.any(data["qty"] < 0):
        raise ValueError("negative quantity found")


def load_hft_event_data(path: str | Path) -> Any:
    np = require_numpy()
    with np.load(Path(path)) as archive:
        if "data" in archive:
            return archive["data"].copy()
        if "arr_0" in archive:
            return archive["arr_0"].copy()
        raise ValueError(f"{path} contains neither 'data' nor 'arr_0'")


def convert_binance_futures_raw(
    input_path: str | Path,
    *,
    output_path: str | Path | None = None,
    combined_stream: bool = True,
    additional_streams: str = "",
    base_latency_ns: float = 0,
    buffer_size: int = 100_000_000,
) -> Any:
    if additional_streams not in {"", "m", "t", "mt"}:
        raise ValueError("additional_streams must be '', 'm', 't', or 'mt'")
    if buffer_size <= 0:
        raise ValueError("buffer_size must be positive")
    data = require_hftbacktest_binance_futures().convert(
        str(Path(input_path)),
        output_filename=(
            None if output_path is None else str(Path(output_path))
        ),
        opt=additional_streams,
        base_latency=base_latency_ns,
        combined_stream=combined_stream,
        buffer_size=buffer_size,
    )
    validate_hft_event_data(data)
    return data


def convert_tardis_csv(
    trade_paths: Sequence[str | Path],
    depth_paths: Sequence[str | Path],
    *,
    output_path: str | Path | None = None,
    buffer_size: int = 100_000_000,
    snapshot_buffer_size: int = 1_000_000,
    base_latency_ns: float = 0,
    snapshot_mode: HftTardisSnapshotMode = HftTardisSnapshotMode.PROCESS,
) -> Any:
    if not trade_paths:
        raise ValueError("at least one Tardis trade file is required")
    if not depth_paths:
        raise ValueError("at least one Tardis depth file is required")
    if buffer_size <= 0 or snapshot_buffer_size <= 0:
        raise ValueError("converter buffer sizes must be positive")
    ordered_paths = [
        *(str(Path(path)) for path in trade_paths),
        *(str(Path(path)) for path in depth_paths),
    ]
    data = require_hftbacktest_tardis().convert(
        ordered_paths,
        output_filename=(
            None if output_path is None else str(Path(output_path))
        ),
        buffer_size=buffer_size,
        ss_buffer_size=snapshot_buffer_size,
        base_latency=base_latency_ns,
        snapshot_mode=snapshot_mode.value,
    )
    validate_hft_event_data(data)
    return data


def create_hft_last_snapshot(
    data_paths: Sequence[str | Path],
    *,
    tick_size: float,
    lot_size: float,
    output_path: str | Path | None = None,
    initial_snapshot_path: str | Path | None = None,
) -> Any:
    create_last_snapshot = (
        require_hftbacktest_snapshot().create_last_snapshot
    )

    return create_last_snapshot(
        [str(Path(path)) for path in data_paths],
        tick_size=tick_size,
        lot_size=lot_size,
        initial_snapshot=(
            None
            if initial_snapshot_path is None
            else str(Path(initial_snapshot_path))
        ),
        output_snapshot_filename=(
            None if output_path is None else str(Path(output_path))
        ),
    )


def generate_feed_order_latency(
    data_path: str | Path,
    output_path: str | Path,
    *,
    sample_interval_ns: int = 1_000_000_000,
    entry_multiplier: float = 4.0,
    response_multiplier: float = 3.0,
    entry_offset_ns: int = 0,
    response_offset_ns: int = 0,
) -> Any:
    if sample_interval_ns <= 0:
        raise ValueError("sample_interval_ns must be positive")
    if entry_multiplier < 0 or response_multiplier < 0:
        raise ValueError("latency multipliers cannot be negative")
    hftbacktest = require_hftbacktest()
    np = require_numpy()
    events = load_hft_event_data(data_path)
    both = (
        (events["ev"] & hftbacktest.EXCH_EVENT)
        == hftbacktest.EXCH_EVENT
    ) & (
        (events["ev"] & hftbacktest.LOCAL_EVENT)
        == hftbacktest.LOCAL_EVENT
    )
    events = events[both]
    if len(events) == 0:
        raise ValueError("data contains no exchange/local timestamp pairs")
    bins = events["local_ts"] // sample_interval_ns
    last_indices = np.r_[
        np.flatnonzero(bins[1:] != bins[:-1]),
        len(events) - 1,
    ]
    sampled = events[last_indices]
    dtype = np.dtype(
        [
            ("req_ts", "<i8"),
            ("exch_ts", "<i8"),
            ("resp_ts", "<i8"),
            ("_padding", "<i8"),
        ]
    )
    result = np.empty(len(sampled), dtype=dtype)
    feed_latency = sampled["local_ts"] - sampled["exch_ts"]
    entry_latency = (
        feed_latency * entry_multiplier + entry_offset_ns
    ).astype(np.int64)
    response_latency = (
        feed_latency * response_multiplier + response_offset_ns
    ).astype(np.int64)
    if np.any(entry_latency <= 0) or np.any(response_latency <= 0):
        raise ValueError("generated order latency must be positive")
    result["req_ts"] = sampled["local_ts"]
    result["exch_ts"] = result["req_ts"] + entry_latency
    result["resp_ts"] = result["exch_ts"] + response_latency
    result["_padding"] = 0
    _save_npz(Path(output_path), result)
    return result


def build_hftbacktest_asset(
    data: str | Path | Any | Sequence[str | Path],
    initial_snapshot: str | Path | Any,
    config: HftBacktestAssetConfig | None = None,
) -> Any:
    effective = config or HftBacktestAssetConfig()
    _validate_asset_config(effective)
    hftbacktest = require_hftbacktest()
    asset = hftbacktest.BacktestAsset()
    asset = asset.data(_coerce_hft_data_input(data))
    asset = asset.initial_snapshot(_coerce_hft_snapshot(initial_snapshot))
    asset = asset.linear_asset(effective.contract_size)
    if effective.latency_model is HftLatencyModel.CONSTANT:
        asset = asset.constant_order_latency(
            effective.entry_latency_ns,
            effective.response_latency_ns,
        )
    else:
        latency_paths = effective.order_latency_paths
        if not latency_paths and effective.order_latency_path is not None:
            latency_paths = (effective.order_latency_path,)
        if not latency_paths:
            raise ValueError(
                "order_latency_path or order_latency_paths is required for "
                "interpolated latency"
            )
        asset = asset.intp_order_latency(
            [str(path) for path in latency_paths]
        )
    if effective.queue_model is HftQueueModel.RISK_ADVERSE:
        asset = asset.risk_adverse_queue_model()
    elif effective.queue_model is HftQueueModel.LOG_PROBABILITY_2:
        asset = asset.log_prob_queue_model2()
    else:
        asset = asset.power_prob_queue_model(
            effective.queue_model_exponent
        )
    if effective.exchange_model is HftExchangeModel.NO_PARTIAL_FILL:
        asset = asset.no_partial_fill_exchange()
    else:
        asset = asset.partial_fill_exchange()
    asset = asset.trading_value_fee_model(
        effective.maker_fee,
        effective.taker_fee,
    )
    asset = asset.tick_size(effective.tick_size)
    asset = asset.lot_size(effective.lot_size)
    asset = asset.last_trades_capacity(effective.last_trades_capacity)
    if effective.depth_implementation is HftDepthImplementation.ROI_VECTOR:
        if (
            effective.roi_lower_bound is None
            or effective.roi_upper_bound is None
        ):
            raise ValueError("ROI-vector depth requires both ROI bounds")
        asset = asset.roi_lb(effective.roi_lower_bound)
        asset = asset.roi_ub(effective.roi_upper_bound)
    return asset


def sample_hft_order_book(
    data: str | Path | Any | Sequence[str | Path],
    initial_snapshot: str | Path | Any,
    *,
    asset_config: HftBacktestAssetConfig | None = None,
    interval_ns: int = 60_000_000_000,
    depth_levels: int = 3,
    maximum_samples: int = 10_000,
    maximum_search_ticks: int = 10_000,
) -> dict[str, Any]:
    if interval_ns <= 0:
        raise ValueError("interval_ns must be positive")
    if depth_levels <= 0 or maximum_samples <= 0:
        raise ValueError("depth_levels and maximum_samples must be positive")
    config = asset_config or HftBacktestAssetConfig()
    hbt = _create_backtest(
        build_hftbacktest_asset(data, initial_snapshot, config),
        config.depth_implementation,
    )
    kernel = _book_sample_kernel()
    try:
        result = kernel(
            hbt,
            interval_ns,
            depth_levels,
            maximum_samples,
            maximum_search_ticks,
        )
    finally:
        hbt.close()
    count, timestamps, best_bid, best_ask, bid_px, bid_qty, ask_px, ask_qty = (
        result
    )
    return {
        "timestamps": timestamps[:count],
        "best_bid": best_bid[:count],
        "best_ask": best_ask[:count],
        "bid_prices": bid_px[:count],
        "bid_quantities": bid_qty[:count],
        "ask_prices": ask_px[:count],
        "ask_quantities": ask_qty[:count],
    }


def calculate_hft_order_book_imbalance(
    data: str | Path | Any | Sequence[str | Path],
    initial_snapshot: str | Path | Any,
    *,
    asset_config: HftBacktestAssetConfig | None = None,
    interval_ns: int = 10_000_000_000,
    near_fraction: float = 0.005,
    far_fraction: float = 0.01,
    maximum_samples: int = 100_000,
) -> Any:
    config = asset_config or HftBacktestAssetConfig()
    if config.depth_implementation is not HftDepthImplementation.ROI_VECTOR:
        raise ValueError("order-book imbalance requires ROI-vector depth")
    if not 0 < near_fraction <= far_fraction:
        raise ValueError("imbalance fractions must satisfy 0 < near <= far")
    hbt = _create_backtest(
        build_hftbacktest_asset(data, initial_snapshot, config),
        config.depth_implementation,
    )
    try:
        count, output = _imbalance_kernel()(
            hbt,
            interval_ns,
            near_fraction,
            far_fraction,
            maximum_samples,
        )
    finally:
        hbt.close()
    return output[:count]


def calculate_hft_rolling_vwap(
    data: str | Path | Any | Sequence[str | Path],
    initial_snapshot: str | Path | Any,
    *,
    asset_config: HftBacktestAssetConfig | None = None,
    interval_ns: int = 10_000_000_000,
    window_intervals: int = 6,
    maximum_samples: int = 100_000,
) -> Any:
    config = asset_config or HftBacktestAssetConfig()
    if config.last_trades_capacity <= 0:
        raise ValueError("rolling VWAP requires last_trades_capacity > 0")
    hbt = _create_backtest(
        build_hftbacktest_asset(data, initial_snapshot, config),
        config.depth_implementation,
    )
    try:
        count, output = _rolling_vwap_kernel()(
            hbt,
            interval_ns,
            window_intervals,
            maximum_samples,
        )
    finally:
        hbt.close()
    return output[:count]


def sample_hft_market_trades(
    data: str | Path | Any | Sequence[str | Path],
    initial_snapshot: str | Path | Any,
    *,
    asset_config: HftBacktestAssetConfig | None = None,
    interval_ns: int = 60_000_000_000,
    maximum_trades: int = 100_000,
) -> Any:
    config = asset_config or HftBacktestAssetConfig()
    if config.last_trades_capacity <= 0:
        raise ValueError("trade sampling requires last_trades_capacity > 0")
    hbt = _create_backtest(
        build_hftbacktest_asset(data, initial_snapshot, config),
        config.depth_implementation,
    )
    try:
        count, output = _trade_sample_kernel()(
            hbt,
            interval_ns,
            maximum_trades,
        )
    finally:
        hbt.close()
    return output[:count]


def run_hft_order_lifecycle(
    data: str | Path | Any | Sequence[str | Path],
    initial_snapshot: str | Path | Any,
    scenario: HftOrderScenario,
    *,
    asset_config: HftBacktestAssetConfig | None = None,
    order_quantity: float = 1.0,
    response_timeout_ns: int = 1_000_000_000,
) -> HftOrderLifecycleResult:
    config = asset_config or HftBacktestAssetConfig(
        depth_implementation=HftDepthImplementation.HASH_MAP,
        roi_lower_bound=None,
        roi_upper_bound=None,
    )
    hbt = _create_backtest(
        build_hftbacktest_asset(data, initial_snapshot, config),
        config.depth_implementation,
    )
    scenario_code = {
        HftOrderScenario.LIMIT_CANCEL: 1,
        HftOrderScenario.MARKET: 2,
        HftOrderScenario.POST_ONLY_REJECT: 3,
    }[scenario]
    try:
        values = _order_lifecycle_kernel()(
            hbt,
            scenario_code,
            order_quantity,
            response_timeout_ns,
        )
    finally:
        hbt.close()
    return HftOrderLifecycleResult(
        scenario=scenario,
        submitted_status=int(values[0]),
        submitted_request=int(values[1]),
        acknowledged_status=int(values[2]),
        acknowledged_request=int(values[3]),
        cancel_requested=bool(values[4]),
        cancel_request_state=int(values[5]),
        final_status=int(values[6]),
        final_request=int(values[7]),
        inactive_cleared=bool(values[8]),
        submission_timestamp_ns=int(values[9]),
        acknowledgment_timestamp_ns=int(values[10]),
        final_timestamp_ns=int(values[11]),
        final_position=float(values[12]),
        final_balance=float(values[13]),
        final_fee=float(values[14]),
        execution_price=float(values[15]),
    )


def run_grid_market_making_backtest(
    data: str | Path | Any | Sequence[str | Path],
    initial_snapshot: str | Path | Any,
    *,
    asset_config: HftBacktestAssetConfig | None = None,
    strategy_config: GridMarketMakingConfig | None = None,
) -> HftBacktestRunResult:
    hftbacktest = require_hftbacktest()
    config = asset_config or HftBacktestAssetConfig()
    strategy = strategy_config or GridMarketMakingConfig()
    _validate_grid_config(strategy)
    hbt = _create_backtest(
        build_hftbacktest_asset(data, initial_snapshot, config),
        config.depth_implementation,
    )
    recorder = hftbacktest.Recorder(1, strategy.recorder_capacity)
    grid_interval = (
        strategy.grid_interval
        if strategy.grid_interval is not None
        else config.tick_size * strategy.grid_interval_ticks
    )
    half_spread = (
        strategy.half_spread
        if strategy.half_spread is not None
        else config.tick_size * strategy.half_spread_ticks
    )
    inventory_skew_price = (
        strategy.inventory_skew_price
        if strategy.inventory_skew_price is not None
        else strategy.inventory_skew * config.tick_size
    )
    try:
        completed = _grid_market_making_kernel()(
            hbt,
            recorder.recorder,
            strategy.grid_levels,
            strategy.max_position,
            grid_interval,
            half_spread,
            strategy.order_quantity,
            strategy.refresh_interval_ns,
            inventory_skew_price,
            strategy.normalize_inventory,
        )
        if not completed:
            raise RuntimeError("grid market-making backtest did not complete")
        final_position = float(hbt.position(0))
        state = hbt.state_values(0)
        final_balance = float(state.balance)
        final_fee = float(state.fee)
    finally:
        hbt.close()
    records = recorder.get(0)
    if strategy.recorder_output_path is not None:
        output_path = Path(strategy.recorder_output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        recorder.to_npz(str(output_path))
    summary: tuple[dict[str, Any], ...] = ()
    statistics = None
    if strategy.generate_statistics:
        if len(records) < 2:
            raise ValueError(
                "statistics require at least two recorded backtest states"
            )
        recorded_span_ns = int(
            records[-1]["timestamp"] - records[0]["timestamp"]
        )
        if recorded_span_ns < 60_000_000_000:
            raise ValueError(
                "HftBacktest statistics require at least 60 seconds of "
                "recorded states; disable generate_statistics for shorter "
                "smoke runs"
            )
        record = require_hftbacktest_stats().LinearAssetRecord(records)
        if strategy.statistics_resample is not None:
            record = record.resample(strategy.statistics_resample)
        np = require_numpy()
        with np.errstate(divide="ignore", invalid="ignore"):
            statistics = record.stats(
                book_size=strategy.statistics_book_size
            )
            summary_frame = statistics.summary()
        summary = tuple(
            {
                key: (
                    None
                    if isinstance(value, float) and not isfinite(value)
                    else value
                )
                for key, value in row.items()
            }
            for row in summary_frame.to_dicts()
        )
    return HftBacktestRunResult(
        records=records,
        statistics=statistics,
        summary=summary,
        final_position=final_position,
        final_balance=final_balance,
        final_fee=final_fee,
        record_count=len(records),
    )


def run_multi_asset_grid_backtests(
    configs: Sequence[MultiAssetGridConfig],
    *,
    output_directory: str | Path | None = None,
) -> MultiAssetGridResult:
    if not configs:
        raise ValueError("at least one multi-asset grid config is required")
    runs: dict[str, HftBacktestRunResult] = {}
    equity_curves: dict[str, Any] = {}
    capitals: dict[str, float] = {}
    for config in configs:
        _validate_multi_asset_grid_config(config)
        if config.asset_name in runs:
            raise ValueError(f"duplicate asset_name: {config.asset_name}")
        order_quantity = max(
            round(
                (config.notional_order_quantity / config.mid_price)
                / config.lot_size
            ),
            1,
        ) * config.lot_size
        recorder_path = None
        if output_directory is not None:
            recorder_path = (
                Path(output_directory)
                / f"gridtrading_{config.asset_name}.npz"
            )
        result = run_grid_market_making_backtest(
            config.data_paths,
            config.initial_snapshot_path,
            asset_config=HftBacktestAssetConfig(
                tick_size=config.tick_size,
                lot_size=config.lot_size,
                latency_model=HftLatencyModel.INTERPOLATED,
                order_latency_paths=config.order_latency_paths,
                queue_model=HftQueueModel.LOG_PROBABILITY_2,
                roi_lower_bound=0,
                roi_upper_bound=config.mid_price * 5,
            ),
            strategy_config=GridMarketMakingConfig(
                grid_levels=config.grid_levels,
                max_position=config.grid_levels * order_quantity,
                grid_interval=config.mid_price
                * config.grid_interval_fraction,
                half_spread=config.mid_price * config.half_spread_fraction,
                order_quantity=order_quantity,
                inventory_skew_price=config.mid_price
                * config.inventory_skew_fraction,
                normalize_inventory=True,
                recorder_capacity=config.recorder_capacity,
                recorder_output_path=recorder_path,
                statistics_resample=config.statistics_resample,
            ),
        )
        runs[config.asset_name] = result
        capitals[config.asset_name] = config.capital
        equity_curves[config.asset_name] = _statistics_equity(
            result.statistics
        )
    return MultiAssetGridResult(
        runs=runs,
        equity_curves=equity_curves,
        portfolio_equity=_aggregate_equity_curves(equity_curves, capitals),
        capital_per_asset=capitals,
    )


def _create_backtest(asset: Any, implementation: HftDepthImplementation) -> Any:
    hftbacktest = require_hftbacktest()
    if implementation is HftDepthImplementation.HASH_MAP:
        return hftbacktest.HashMapMarketDepthBacktest([asset])
    return hftbacktest.ROIVectorMarketDepthBacktest([asset])


@lru_cache(maxsize=1)
def _book_sample_kernel() -> Any:
    numba = require_numba()
    np = require_numpy()

    @numba.njit
    def kernel(
        hbt,
        interval_ns,
        depth_levels,
        maximum_samples,
        maximum_search_ticks,
    ):
        timestamps = np.zeros(maximum_samples, np.int64)
        best_bid = np.full(maximum_samples, np.nan, np.float64)
        best_ask = np.full(maximum_samples, np.nan, np.float64)
        bid_prices = np.full(
            (maximum_samples, depth_levels),
            np.nan,
            np.float64,
        )
        bid_quantities = np.zeros(
            (maximum_samples, depth_levels),
            np.float64,
        )
        ask_prices = np.full(
            (maximum_samples, depth_levels),
            np.nan,
            np.float64,
        )
        ask_quantities = np.zeros(
            (maximum_samples, depth_levels),
            np.float64,
        )
        count = 0
        while count < maximum_samples and hbt.elapse(interval_ns) == 0:
            depth = hbt.depth(0)
            timestamps[count] = hbt.current_timestamp
            best_bid[count] = depth.best_bid
            best_ask[count] = depth.best_ask
            level = 0
            stop = depth.best_ask_tick + maximum_search_ticks
            for price_tick in range(depth.best_ask_tick, stop):
                quantity = depth.ask_qty_at_tick(price_tick)
                if quantity > 0:
                    ask_prices[count, level] = price_tick * depth.tick_size
                    ask_quantities[count, level] = quantity
                    level += 1
                    if level == depth_levels:
                        break
            level = 0
            stop = max(depth.best_bid_tick - maximum_search_ticks, 0)
            for price_tick in range(depth.best_bid_tick, stop, -1):
                quantity = depth.bid_qty_at_tick(price_tick)
                if quantity > 0:
                    bid_prices[count, level] = price_tick * depth.tick_size
                    bid_quantities[count, level] = quantity
                    level += 1
                    if level == depth_levels:
                        break
            count += 1
        return (
            count,
            timestamps,
            best_bid,
            best_ask,
            bid_prices,
            bid_quantities,
            ask_prices,
            ask_quantities,
        )

    return kernel


@lru_cache(maxsize=1)
def _imbalance_kernel() -> Any:
    numba = require_numba()
    np = require_numpy()

    @numba.njit
    def kernel(
        hbt,
        interval_ns,
        near_fraction,
        far_fraction,
        maximum_samples,
    ):
        output = np.full((maximum_samples, 4), np.nan, np.float64)
        count = 0
        while count < maximum_samples and hbt.elapse(interval_ns) == 0:
            depth = hbt.depth(0)
            mid_price = (depth.best_bid + depth.best_ask) / 2
            near_ask = 0.0
            far_ask = 0.0
            for index in range(len(depth.ask_depth)):
                price_tick = depth.roi_lb_tick + index
                price = price_tick * depth.tick_size
                distance = (price - mid_price) / mid_price
                if distance < 0:
                    continue
                if distance > far_fraction:
                    break
                far_ask += depth.ask_depth[index]
                if distance <= near_fraction:
                    near_ask = far_ask
            near_bid = 0.0
            far_bid = 0.0
            for index in range(len(depth.bid_depth) - 1, -1, -1):
                price_tick = depth.roi_lb_tick + index
                price = price_tick * depth.tick_size
                distance = (mid_price - price) / mid_price
                if distance < 0:
                    continue
                if distance > far_fraction:
                    break
                far_bid += depth.bid_depth[index]
                if distance <= near_fraction:
                    near_bid = far_bid
            best_bid_index = depth.best_bid_tick - depth.roi_lb_tick
            best_ask_index = depth.best_ask_tick - depth.roi_lb_tick
            top = (
                depth.bid_depth[best_bid_index]
                - depth.ask_depth[best_ask_index]
            )
            output[count, 0] = hbt.current_timestamp
            output[count, 1] = top
            output[count, 2] = near_bid - near_ask
            output[count, 3] = far_bid - far_ask
            count += 1
        return count, output

    return kernel


@lru_cache(maxsize=1)
def _rolling_vwap_kernel() -> Any:
    numba = require_numba()
    np = require_numpy()
    hftbacktest = require_hftbacktest()
    buy_event = hftbacktest.BUY_EVENT

    @numba.njit
    def kernel(hbt, interval_ns, window_intervals, maximum_samples):
        buy_amount = np.zeros(maximum_samples, np.float64)
        buy_quantity = np.zeros(maximum_samples, np.float64)
        sell_amount = np.zeros(maximum_samples, np.float64)
        sell_quantity = np.zeros(maximum_samples, np.float64)
        output = np.full((maximum_samples, 5), np.nan, np.float64)
        count = 0
        while count < maximum_samples and hbt.elapse(interval_ns) == 0:
            for trade in hbt.last_trades(0):
                if (trade.ev & buy_event) == buy_event:
                    buy_amount[count] += trade.px * trade.qty
                    buy_quantity[count] += trade.qty
                else:
                    sell_amount[count] += trade.px * trade.qty
                    sell_quantity[count] += trade.qty
            hbt.clear_last_trades(0)
            total_quantity = buy_quantity[count] + sell_quantity[count]
            if total_quantity > 0:
                output[count, 1] = (
                    buy_amount[count] + sell_amount[count]
                ) / total_quantity
            start = max(0, count + 1 - window_intervals)
            window_buy_amount = np.sum(buy_amount[start : count + 1])
            window_buy_quantity = np.sum(buy_quantity[start : count + 1])
            window_sell_amount = np.sum(sell_amount[start : count + 1])
            window_sell_quantity = np.sum(sell_quantity[start : count + 1])
            window_quantity = window_buy_quantity + window_sell_quantity
            if count + 1 >= window_intervals and window_quantity > 0:
                output[count, 2] = (
                    window_buy_amount + window_sell_amount
                ) / window_quantity
                if window_buy_quantity > 0:
                    output[count, 3] = (
                        window_buy_amount / window_buy_quantity
                    )
                if window_sell_quantity > 0:
                    output[count, 4] = (
                        window_sell_amount / window_sell_quantity
                    )
            output[count, 0] = hbt.current_timestamp
            count += 1
        return count, output

    return kernel


@lru_cache(maxsize=1)
def _trade_sample_kernel() -> Any:
    numba = require_numba()
    np = require_numpy()
    hftbacktest = require_hftbacktest()
    buy_event = hftbacktest.BUY_EVENT

    @numba.njit
    def kernel(hbt, interval_ns, maximum_trades):
        output = np.zeros((maximum_trades, 5), np.float64)
        count = 0
        while count < maximum_trades and hbt.elapse(interval_ns) == 0:
            for trade in hbt.last_trades(0):
                if count == maximum_trades:
                    break
                output[count, 0] = hbt.current_timestamp
                output[count, 1] = trade.exch_ts
                output[count, 2] = (
                    1.0 if (trade.ev & buy_event) == buy_event else -1.0
                )
                output[count, 3] = trade.px
                output[count, 4] = trade.qty
                count += 1
            hbt.clear_last_trades(0)
        return count, output

    return kernel


@lru_cache(maxsize=1)
def _order_lifecycle_kernel() -> Any:
    numba = require_numba()
    np = require_numpy()
    hftbacktest = require_hftbacktest()
    gtc = hftbacktest.GTC
    gtx = hftbacktest.GTX
    limit = hftbacktest.LIMIT
    market = hftbacktest.MARKET
    none = hftbacktest.NONE

    @numba.njit
    def kernel(hbt, scenario_code, order_quantity, response_timeout_ns):
        values = np.zeros(16, np.float64)
        if hbt.elapse(1) != 0:
            return values
        depth = hbt.depth(0)
        order_id = 1
        if scenario_code == 1:
            price = depth.best_bid - 100 * depth.tick_size
            hbt.submit_buy_order(
                0,
                order_id,
                price,
                order_quantity,
                gtc,
                limit,
                False,
            )
        elif scenario_code == 2:
            price = depth.best_bid
            hbt.submit_sell_order(
                0,
                order_id,
                price,
                order_quantity,
                gtc,
                market,
                False,
            )
        else:
            price = depth.best_bid - 100 * depth.tick_size
            hbt.submit_sell_order(
                0,
                order_id,
                price,
                order_quantity,
                gtx,
                limit,
                False,
            )
        order = hbt.orders(0).get(order_id)
        values[0] = order.status
        values[1] = order.req
        values[9] = hbt.current_timestamp
        hbt.wait_order_response(0, order_id, response_timeout_ns)
        order = hbt.orders(0).get(order_id)
        values[2] = order.status
        values[3] = order.req
        values[10] = hbt.current_timestamp
        if scenario_code == 1 and order.cancellable:
            hbt.cancel(0, order_id, False)
            values[4] = 1
            values[5] = hbt.orders(0).get(order_id).req
            hbt.wait_order_response(0, order_id, response_timeout_ns)
            order = hbt.orders(0).get(order_id)
        values[6] = order.status
        values[7] = order.req
        values[11] = hbt.current_timestamp
        values[15] = (
            order.exec_price if order.status != none else np.nan
        )
        hbt.clear_inactive_orders(0)
        values[8] = 1 if order_id not in hbt.orders(0) else 0
        values[12] = hbt.position(0)
        state = hbt.state_values(0)
        values[13] = state.balance
        values[14] = state.fee
        return values

    return kernel


@lru_cache(maxsize=1)
def _grid_market_making_kernel() -> Any:
    numba = require_numba()
    np = require_numpy()
    hftbacktest = require_hftbacktest()
    typed_dict = numba.typed.Dict
    buy = hftbacktest.BUY
    sell = hftbacktest.SELL
    gtx = hftbacktest.GTX
    limit = hftbacktest.LIMIT

    @numba.njit
    def kernel(
        hbt,
        recorder,
        grid_levels,
        max_position,
        grid_interval,
        half_spread,
        order_quantity,
        refresh_interval_ns,
        inventory_skew_price,
        normalize_inventory,
    ):
        asset_no = 0
        tick_size = hbt.depth(asset_no).tick_size
        grid_interval = max(
            np.round(grid_interval / tick_size) * tick_size,
            tick_size,
        )
        while hbt.elapse(refresh_interval_ns) == 0:
            hbt.clear_inactive_orders(asset_no)
            depth = hbt.depth(asset_no)
            position = hbt.position(asset_no)
            orders = hbt.orders(asset_no)
            best_bid = depth.best_bid
            best_ask = depth.best_ask
            if (
                not np.isfinite(best_bid)
                or not np.isfinite(best_ask)
                or best_bid <= 0
                or best_ask <= 0
                or best_bid >= best_ask
            ):
                recorder.record(hbt)
                continue
            mid_price = (best_bid + best_ask) / 2
            inventory = (
                position / order_quantity
                if normalize_inventory
                else position
            )
            reservation_price = mid_price - inventory_skew_price * inventory
            bid_price = min(
                reservation_price - half_spread,
                best_bid,
            )
            ask_price = max(
                reservation_price + half_spread,
                best_ask,
            )
            bid_price = (
                np.floor(bid_price / grid_interval) * grid_interval
            )
            ask_price = (
                np.ceil(ask_price / grid_interval) * grid_interval
            )
            new_bids = typed_dict.empty(
                key_type=np.uint64,
                value_type=np.float64,
            )
            if position < max_position and np.isfinite(bid_price):
                current = bid_price
                for _ in range(grid_levels):
                    price_tick = int(round(current / tick_size))
                    if price_tick > 0:
                        new_bids[np.uint64(price_tick)] = current
                    current -= grid_interval
            new_asks = typed_dict.empty(
                key_type=np.uint64,
                value_type=np.float64,
            )
            if position > -max_position and np.isfinite(ask_price):
                current = ask_price
                for _ in range(grid_levels):
                    price_tick = int(round(current / tick_size))
                    if price_tick > 0:
                        new_asks[np.uint64(price_tick)] = current
                    current += grid_interval
            order_values = orders.values()
            while order_values.has_next():
                order = order_values.get()
                if not order.cancellable:
                    continue
                remove_buy = (
                    order.side == buy and order.order_id not in new_bids
                )
                remove_sell = (
                    order.side == sell and order.order_id not in new_asks
                )
                if remove_buy or remove_sell:
                    hbt.cancel(asset_no, order.order_id, False)
            for order_id, order_price in new_bids.items():
                if order_id not in orders:
                    hbt.submit_buy_order(
                        asset_no,
                        order_id,
                        order_price,
                        order_quantity,
                        gtx,
                        limit,
                        False,
                    )
            for order_id, order_price in new_asks.items():
                if order_id not in orders:
                    hbt.submit_sell_order(
                        asset_no,
                        order_id,
                        order_price,
                        order_quantity,
                        gtx,
                        limit,
                        False,
                    )
            recorder.record(hbt)
        return True

    return kernel


def _generate_initial_snapshot(
    *,
    np: Any,
    dtype: Any,
    base_flags: int,
    buy_event: int,
    sell_event: int,
    snapshot_event: int,
    config: SyntheticHftConfig,
    rng: Any,
) -> tuple[Any, dict[float, float], dict[float, float]]:
    best_bid = _round_to_increment(
        config.mid_price - config.tick_size,
        config.tick_size,
    )
    best_ask = _round_to_increment(
        config.mid_price + config.tick_size,
        config.tick_size,
    )
    bids: dict[float, float] = {}
    asks: dict[float, float] = {}
    rows = []
    for level in range(config.levels):
        bid_price = _round_to_increment(
            best_bid - level * config.tick_size,
            config.tick_size,
        )
        ask_price = _round_to_increment(
            best_ask + level * config.tick_size,
            config.tick_size,
        )
        depth_scale = max(
            0.35,
            np.exp(-level / max(config.levels / 3, 1)),
        )
        bid_quantity = _round_positive_to_increment(
            rng.lognormal(-0.1, 0.55) * depth_scale,
            config.lot_size,
        )
        ask_quantity = _round_positive_to_increment(
            rng.lognormal(-0.1, 0.55) * depth_scale,
            config.lot_size,
        )
        bids[bid_price] = bid_quantity
        asks[ask_price] = ask_quantity
        rows.append(
            _hft_event(
                base_flags | buy_event | snapshot_event,
                0,
                0,
                bid_price,
                bid_quantity,
            )
        )
    for level in range(config.levels):
        ask_price = _round_to_increment(
            best_ask + level * config.tick_size,
            config.tick_size,
        )
        rows.append(
            _hft_event(
                base_flags | sell_event | snapshot_event,
                0,
                0,
                ask_price,
                asks[ask_price],
            )
        )
    return np.array(rows, dtype=dtype), bids, asks


def _hft_event(
    event_flags: int,
    exchange_timestamp_ns: int,
    local_timestamp_ns: int,
    price: float,
    quantity: float,
) -> tuple[int, int, int, float, float, int, int, float]:
    return (
        int(event_flags),
        int(exchange_timestamp_ns),
        int(local_timestamp_ns),
        float(price),
        float(quantity),
        0,
        0,
        0.0,
    )


def _round_to_increment(value: float, increment: float) -> float:
    return round(value / increment) * increment


def _round_positive_to_increment(value: float, increment: float) -> float:
    return max(increment, round(value / increment) * increment)


def _save_npz(path: Path, data: Any) -> None:
    np = require_numpy()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as output:
        np.savez_compressed(output, data=data)


def _save_preview_csv(path: Path, data: Any, rows: int = 500) -> None:
    np = require_numpy()
    names = data.dtype.names
    if names is None:
        raise ValueError("HftBacktest event data must have named fields")
    matrix = np.column_stack([data[name][:rows] for name in names])
    np.savetxt(
        path,
        matrix,
        delimiter=",",
        header=",".join(names),
        comments="",
        fmt=["%u", "%d", "%d", "%.10f", "%.10f", "%u", "%d", "%.10f"],
    )


def _coerce_hft_data_input(
    data: str | Path | Any | Sequence[str | Path],
) -> Any:
    if isinstance(data, (str, Path)):
        return [str(Path(data))]
    if isinstance(data, Sequence) and not hasattr(data, "dtype"):
        return [str(Path(path)) for path in data]
    return data


def _coerce_hft_snapshot(snapshot: str | Path | Any) -> Any:
    if isinstance(snapshot, (str, Path)):
        return str(Path(snapshot))
    return snapshot


def _validate_synthetic_config(config: SyntheticHftConfig) -> None:
    if config.duration_seconds <= 0 or config.events_per_second <= 0:
        raise ValueError("duration_seconds and events_per_second must be positive")
    if config.tick_size <= 0 or config.lot_size <= 0 or config.levels <= 0:
        raise ValueError("tick_size, lot_size, and levels must be positive")
    if not 0 <= config.trade_probability <= 1:
        raise ValueError("trade_probability must be between 0 and 1")
    if config.mid_price <= config.tick_size * 2:
        raise ValueError("mid_price is too small for the configured tick size")


def _validate_asset_config(config: HftBacktestAssetConfig) -> None:
    if config.contract_size <= 0:
        raise ValueError("contract_size must be positive")
    if config.tick_size <= 0 or config.lot_size <= 0:
        raise ValueError("tick_size and lot_size must be positive")
    if config.entry_latency_ns < 0 or config.response_latency_ns < 0:
        raise ValueError("constant latencies cannot be negative")
    if config.last_trades_capacity < 0:
        raise ValueError("last_trades_capacity cannot be negative")
    if (
        config.depth_implementation is HftDepthImplementation.ROI_VECTOR
        and config.roi_lower_bound is not None
        and config.roi_upper_bound is not None
        and config.roi_lower_bound >= config.roi_upper_bound
    ):
        raise ValueError("roi_lower_bound must be below roi_upper_bound")


def _validate_grid_config(config: GridMarketMakingConfig) -> None:
    if config.grid_levels <= 0:
        raise ValueError("grid_levels must be positive")
    if config.max_position <= 0 or config.order_quantity <= 0:
        raise ValueError("max_position and order_quantity must be positive")
    if config.grid_interval_ticks <= 0 or config.half_spread_ticks <= 0:
        raise ValueError("grid and spread ticks must be positive")
    if config.grid_interval is not None and config.grid_interval <= 0:
        raise ValueError("grid_interval must be positive")
    if config.half_spread is not None and config.half_spread <= 0:
        raise ValueError("half_spread must be positive")
    if (
        config.inventory_skew_price is not None
        and config.inventory_skew_price < 0
    ):
        raise ValueError("inventory_skew_price cannot be negative")
    if config.refresh_interval_ns <= 0 or config.recorder_capacity <= 0:
        raise ValueError("refresh interval and recorder capacity must be positive")


def _validate_multi_asset_grid_config(config: MultiAssetGridConfig) -> None:
    if not config.asset_name:
        raise ValueError("asset_name cannot be empty")
    if not config.data_paths:
        raise ValueError("multi-asset data_paths cannot be empty")
    if not config.order_latency_paths:
        raise ValueError("multi-asset order_latency_paths cannot be empty")
    positive_values = (
        config.mid_price,
        config.tick_size,
        config.lot_size,
        config.notional_order_quantity,
        config.capital,
        config.half_spread_fraction,
        config.grid_interval_fraction,
        config.recorder_capacity,
    )
    if any(value <= 0 for value in positive_values):
        raise ValueError("multi-asset numeric settings must be positive")
    if config.inventory_skew_fraction < 0:
        raise ValueError("inventory_skew_fraction cannot be negative")


def _statistics_equity(statistics: Any | None) -> Any:
    if statistics is None:
        raise ValueError("multi-asset runs require generated statistics")
    return statistics.entire.with_columns(
        (
            statistics.entire["equity_wo_fee"]
            - statistics.entire["fee"]
        ).alias("equity")
    ).select(["timestamp", "equity"])


def _aggregate_equity_curves(
    equity_curves: dict[str, Any],
    capitals: dict[str, float],
) -> Any:
    np = require_numpy()
    common_timestamps: set[Any] | None = None
    curve_values: dict[str, dict[Any, float]] = {}
    for asset_name, frame in equity_curves.items():
        timestamps = frame["timestamp"].to_list()
        values = frame["equity"].to_list()
        mapping = dict(zip(timestamps, values, strict=True))
        curve_values[asset_name] = mapping
        available = set(mapping)
        common_timestamps = (
            available
            if common_timestamps is None
            else common_timestamps & available
        )
    if not common_timestamps:
        raise ValueError("asset equity curves have no common timestamps")
    ordered_timestamps = sorted(common_timestamps)
    total_capital = sum(capitals.values())
    net_equity = np.array(
        [
            sum(
                curve_values[asset_name][timestamp]
                for asset_name in equity_curves
            )
            for timestamp in ordered_timestamps
        ],
        dtype=np.float64,
    )
    polars = type(next(iter(equity_curves.values())))
    return polars(
        {
            "timestamp": ordered_timestamps,
            "equity": net_equity,
            "cumulative_return_pct": net_equity / total_capital * 100,
        }
    )
