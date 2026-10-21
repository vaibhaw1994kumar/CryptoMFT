from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import hftbacktest
import numpy as np
import pytest

from trading_platform_poc.hftbacktest_market_making import (
    GridMarketMakingConfig,
    HftBacktestAssetConfig,
    HftDepthImplementation,
    HftLatencyModel,
    HftOrderScenario,
    HftQueueModel,
    HftTardisSnapshotMode,
    MultiAssetGridConfig,
    SyntheticHftConfig,
    calculate_hft_order_book_imbalance,
    calculate_hft_rolling_vwap,
    convert_binance_futures_raw,
    convert_tardis_csv,
    create_hft_last_snapshot,
    generate_feed_order_latency,
    generate_synthetic_hft_dataset,
    load_hft_event_data,
    run_grid_market_making_backtest,
    run_hft_order_lifecycle,
    run_multi_asset_grid_backtests,
    sample_hft_market_trades,
    sample_hft_order_book,
    validate_hft_event_data,
)


def _one_valid_event() -> np.ndarray:
    data = np.zeros(1, dtype=hftbacktest.event_dtype)
    data["ev"] = (
        hftbacktest.EXCH_EVENT
        | hftbacktest.LOCAL_EVENT
        | hftbacktest.BUY_EVENT
        | hftbacktest.DEPTH_EVENT
    )
    data["exch_ts"] = 1
    data["local_ts"] = 2
    data["px"] = 100
    data["qty"] = 1
    return data


def test_validation_accepts_hftbacktest_split_event_order() -> None:
    data = np.zeros(4, dtype=hftbacktest.event_dtype)
    depth_flags = hftbacktest.BUY_EVENT | hftbacktest.DEPTH_EVENT
    data["ev"] = [
        hftbacktest.EXCH_EVENT | depth_flags,
        hftbacktest.EXCH_EVENT | depth_flags,
        hftbacktest.LOCAL_EVENT | depth_flags,
        hftbacktest.LOCAL_EVENT | depth_flags,
    ]
    data["exch_ts"] = [100, 200, 200, 100]
    data["local_ts"] = [300, 250, 250, 300]
    data["px"] = 100
    data["qty"] = 1

    validate_hft_event_data(data)


def test_official_raw_converters_preserve_required_options(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from trading_platform_poc import hftbacktest_market_making as module

    calls: dict[str, tuple[tuple, dict]] = {}

    class Converter:
        def __init__(self, name: str) -> None:
            self.name = name

        def convert(self, *args, **kwargs):
            calls[self.name] = (args, kwargs)
            return _one_valid_event()

    monkeypatch.setattr(
        module,
        "require_hftbacktest_binance_futures",
        lambda: Converter("binance"),
    )
    monkeypatch.setattr(
        module,
        "require_hftbacktest_tardis",
        lambda: Converter("tardis"),
    )

    convert_binance_futures_raw(
        tmp_path / "feed.gz",
        output_path=tmp_path / "feed.npz",
        combined_stream=False,
        additional_streams="mt",
        buffer_size=123,
    )
    _, binance_options = calls["binance"]
    assert binance_options["combined_stream"] is False
    assert binance_options["opt"] == "mt"
    assert binance_options["buffer_size"] == 123

    convert_tardis_csv(
        [tmp_path / "trades-1.csv.gz", tmp_path / "trades-2.csv.gz"],
        [tmp_path / "depth-1.csv.gz"],
        output_path=tmp_path / "tardis.npz",
        buffer_size=456,
        snapshot_buffer_size=789,
        snapshot_mode=HftTardisSnapshotMode.IGNORE_START_OF_DAY,
    )
    tardis_paths = calls["tardis"][0][0]
    _, tardis_options = calls["tardis"]
    assert "trades-1" in tardis_paths[0]
    assert "trades-2" in tardis_paths[1]
    assert "depth-1" in tardis_paths[2]
    assert tardis_options["snapshot_mode"] == "ignore_sod"
    assert tardis_options["ss_buffer_size"] == 789


@pytest.fixture(scope="module")
def synthetic_dataset(tmp_path_factory):
    output = tmp_path_factory.mktemp("hft-market-making")
    return generate_synthetic_hft_dataset(
        output,
        SyntheticHftConfig(
            duration_seconds=65,
            events_per_second=5,
            levels=10,
            seed=19,
        ),
    )


@pytest.fixture(scope="module")
def roi_config() -> HftBacktestAssetConfig:
    return HftBacktestAssetConfig(
        roi_lower_bound=50_000,
        roi_upper_bound=70_000,
        last_trades_capacity=10_000,
    )


def test_synthetic_generator_is_deterministic_and_valid(tmp_path) -> None:
    config = SyntheticHftConfig(
        duration_seconds=2,
        events_per_second=5,
        levels=5,
        seed=31,
    )
    first = generate_synthetic_hft_dataset(tmp_path / "first", config)
    second = generate_synthetic_hft_dataset(tmp_path / "second", config)
    first_data = load_hft_event_data(first.data_path)
    second_data = load_hft_event_data(second.data_path)

    validate_hft_event_data(first_data)
    assert first_data.dtype == hftbacktest.event_dtype
    assert np.array_equal(first_data, second_data)
    assert np.all(first_data["local_ts"] > first_data["exch_ts"])
    assert first.trade_event_count > 0
    assert first.initial_snapshot_path.is_file()
    assert first.preview_path.is_file()


def test_snapshot_book_sampling_and_imbalance(
    synthetic_dataset,
    roi_config,
) -> None:
    rebuilt_snapshot = synthetic_dataset.data_path.with_name("last.npz")
    snapshot = create_hft_last_snapshot(
        [synthetic_dataset.data_path],
        tick_size=roi_config.tick_size,
        lot_size=roi_config.lot_size,
        initial_snapshot_path=synthetic_dataset.initial_snapshot_path,
        output_path=rebuilt_snapshot,
    )
    assert len(snapshot) > 0
    assert rebuilt_snapshot.is_file()

    book = sample_hft_order_book(
        synthetic_dataset.data_path,
        synthetic_dataset.initial_snapshot_path,
        asset_config=roi_config,
        interval_ns=5_000_000_000,
        maximum_samples=20,
    )
    assert len(book["timestamps"]) > 0
    assert np.all(book["best_bid"] < book["best_ask"])
    assert np.isfinite(book["bid_prices"][:, 0]).all()
    assert np.isfinite(book["ask_prices"][:, 0]).all()

    imbalance = calculate_hft_order_book_imbalance(
        synthetic_dataset.data_path,
        synthetic_dataset.initial_snapshot_path,
        asset_config=roi_config,
        interval_ns=5_000_000_000,
        maximum_samples=20,
    )
    assert imbalance.shape[1] == 4
    assert np.isfinite(imbalance).all()


def test_hash_map_book_sampling(synthetic_dataset) -> None:
    config = HftBacktestAssetConfig(
        depth_implementation=HftDepthImplementation.HASH_MAP,
        roi_lower_bound=None,
        roi_upper_bound=None,
        last_trades_capacity=1_000,
    )
    book = sample_hft_order_book(
        synthetic_dataset.data_path,
        synthetic_dataset.initial_snapshot_path,
        asset_config=config,
        interval_ns=10_000_000_000,
        maximum_samples=10,
    )

    assert len(book["timestamps"]) > 0
    assert np.all(book["best_bid"] < book["best_ask"])


def test_trade_sampling_and_rolling_vwap(
    synthetic_dataset,
    roi_config,
) -> None:
    trades = sample_hft_market_trades(
        synthetic_dataset.data_path,
        synthetic_dataset.initial_snapshot_path,
        asset_config=roi_config,
        interval_ns=5_000_000_000,
    )
    assert len(trades) > 0
    assert set(np.unique(trades[:, 2])) <= {-1.0, 1.0}
    assert np.all(trades[:, 3] > 0)
    assert np.all(trades[:, 4] > 0)

    vwap = calculate_hft_rolling_vwap(
        synthetic_dataset.data_path,
        synthetic_dataset.initial_snapshot_path,
        asset_config=roi_config,
        interval_ns=5_000_000_000,
        window_intervals=2,
        maximum_samples=20,
    )
    assert vwap.shape[1] == 5
    assert np.isfinite(vwap[:, 1][~np.isnan(vwap[:, 1])]).all()
    one_minute = vwap[:, 2][~np.isnan(vwap[:, 2])]
    assert len(one_minute) > 0
    assert np.all((one_minute > 57_000) & (one_minute < 63_000))


def test_feed_latency_generation_supports_interpolated_asset(
    synthetic_dataset,
    roi_config,
) -> None:
    latency_path = synthetic_dataset.data_path.with_name("latency.npz")
    latency = generate_feed_order_latency(
        synthetic_dataset.data_path,
        latency_path,
    )
    assert latency_path.is_file()
    assert np.all(latency["req_ts"] < latency["exch_ts"])
    assert np.all(latency["exch_ts"] < latency["resp_ts"])

    interpolated = replace(
        roi_config,
        latency_model=HftLatencyModel.INTERPOLATED,
        order_latency_paths=(latency_path, latency_path),
        queue_model=HftQueueModel.LOG_PROBABILITY_2,
    )
    book = sample_hft_order_book(
        synthetic_dataset.data_path,
        synthetic_dataset.initial_snapshot_path,
        asset_config=interpolated,
        interval_ns=10_000_000_000,
        maximum_samples=10,
    )
    assert len(book["timestamps"]) > 0


@pytest.mark.parametrize(
    ("scenario", "acknowledged", "final"),
    [
        (HftOrderScenario.LIMIT_CANCEL, hftbacktest.NEW, hftbacktest.CANCELED),
        (HftOrderScenario.MARKET, hftbacktest.FILLED, hftbacktest.FILLED),
        (
            HftOrderScenario.POST_ONLY_REJECT,
            hftbacktest.EXPIRED,
            hftbacktest.EXPIRED,
        ),
    ],
)
def test_order_lifecycle_matches_notebook(
    synthetic_dataset,
    scenario,
    acknowledged,
    final,
) -> None:
    config = HftBacktestAssetConfig(
        depth_implementation=HftDepthImplementation.HASH_MAP,
        roi_lower_bound=None,
        roi_upper_bound=None,
    )
    result = run_hft_order_lifecycle(
        synthetic_dataset.data_path,
        synthetic_dataset.initial_snapshot_path,
        scenario,
        asset_config=config,
    )

    assert result.submitted_status == hftbacktest.NONE
    assert result.submitted_request == hftbacktest.NEW
    assert result.acknowledged_status == acknowledged
    assert result.final_status == final
    assert result.acknowledgment_timestamp_ns > result.submission_timestamp_ns
    assert result.inactive_cleared
    if scenario is HftOrderScenario.LIMIT_CANCEL:
        assert result.cancel_requested
        assert result.cancel_request_state == hftbacktest.CANCELED
    if scenario is HftOrderScenario.MARKET:
        assert result.execution_price > 0
        assert result.final_position == -1


def test_grid_market_making_records_and_reports_statistics(
    synthetic_dataset,
    roi_config,
    recwarn,
) -> None:
    recorder_path = synthetic_dataset.data_path.with_name("grid-stats.npz")
    result = run_grid_market_making_backtest(
        synthetic_dataset.data_path,
        synthetic_dataset.initial_snapshot_path,
        asset_config=roi_config,
        strategy_config=GridMarketMakingConfig(
            grid_levels=5,
            max_position=1,
            order_quantity=0.01,
            recorder_capacity=10_000,
            grid_interval=1,
            half_spread=2,
            inventory_skew_price=1,
            normalize_inventory=True,
            recorder_output_path=recorder_path,
            statistics_resample="5s",
        ),
    )

    assert result.record_count > 100
    assert result.records.dtype.names is not None
    assert result.summary
    assert "SR" in result.summary[0]
    assert result.statistics is not None
    assert callable(result.statistics.plot)
    assert recorder_path.is_file()
    json.dumps(result.to_dict(), default=str, allow_nan=False)
    assert not [
        warning
        for warning in recwarn
        if issubclass(warning.category, RuntimeWarning)
    ]


def test_multi_asset_tutorial_profile_builds_portfolio_equity(
    synthetic_dataset,
    recwarn,
) -> None:
    latency_path = synthetic_dataset.data_path.with_name(
        "multi-asset-latency.npz"
    )
    generate_feed_order_latency(synthetic_dataset.data_path, latency_path)
    result = run_multi_asset_grid_backtests(
        [
            MultiAssetGridConfig(
                asset_name="BTCUSDT",
                data_paths=(synthetic_dataset.data_path,),
                initial_snapshot_path=synthetic_dataset.initial_snapshot_path,
                mid_price=60_000,
                tick_size=0.1,
                lot_size=0.001,
                order_latency_paths=(latency_path,),
                recorder_capacity=10_000,
                statistics_resample="5s",
            )
        ],
        output_directory=synthetic_dataset.data_path.parent / "portfolio",
    )

    assert set(result.runs) == {"BTCUSDT"}
    assert len(result.portfolio_equity) > 0
    assert "cumulative_return_pct" in result.portfolio_equity.columns
    assert not [
        warning
        for warning in recwarn
        if issubclass(warning.category, RuntimeWarning)
    ]


def test_short_grid_run_requires_explicit_statistics_opt_out(
    tmp_path: Path,
    roi_config,
) -> None:
    dataset = generate_synthetic_hft_dataset(
        tmp_path / "short",
        SyntheticHftConfig(
            duration_seconds=2,
            events_per_second=5,
            levels=5,
        ),
    )
    with pytest.raises(ValueError, match="at least 60 seconds"):
        run_grid_market_making_backtest(
            dataset.data_path,
            dataset.initial_snapshot_path,
            asset_config=roi_config,
            strategy_config=GridMarketMakingConfig(
                grid_levels=2,
                recorder_capacity=100,
            ),
        )

    result = run_grid_market_making_backtest(
        dataset.data_path,
        dataset.initial_snapshot_path,
        asset_config=roi_config,
        strategy_config=GridMarketMakingConfig(
            grid_levels=2,
            recorder_capacity=100,
            generate_statistics=False,
        ),
    )
    assert result.record_count > 0
    assert result.summary == ()
