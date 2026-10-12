from pathlib import Path

from hft_platform_kit import (
    GridMarketMakingConfig,
    HftBacktestAssetConfig,
    HftOrderScenario,
    SyntheticHftConfig,
    calculate_hft_order_book_imbalance,
    calculate_hft_rolling_vwap,
    generate_feed_order_latency,
    generate_synthetic_hft_dataset,
    run_grid_market_making_backtest,
    run_hft_order_lifecycle,
    sample_hft_market_trades,
    sample_hft_order_book,
)


output = Path("data/synthetic_hft_data")
dataset = generate_synthetic_hft_dataset(
    output,
    SyntheticHftConfig(duration_seconds=65),
)
asset = HftBacktestAssetConfig(
    roi_lower_bound=50_000,
    roi_upper_bound=70_000,
)

book = sample_hft_order_book(
    dataset.data_path,
    dataset.initial_snapshot_path,
    asset_config=asset,
    interval_ns=10_000_000_000,
)
imbalance = calculate_hft_order_book_imbalance(
    dataset.data_path,
    dataset.initial_snapshot_path,
    asset_config=asset,
)
trades = sample_hft_market_trades(
    dataset.data_path,
    dataset.initial_snapshot_path,
    asset_config=asset,
)
vwap = calculate_hft_rolling_vwap(
    dataset.data_path,
    dataset.initial_snapshot_path,
    asset_config=asset,
)
latency = generate_feed_order_latency(
    dataset.data_path,
    output / "synthetic_btcusdt_latency.npz",
)
lifecycle = {
    scenario.value: run_hft_order_lifecycle(
        dataset.data_path,
        dataset.initial_snapshot_path,
        scenario,
    ).to_dict()
    for scenario in HftOrderScenario
}
result = run_grid_market_making_backtest(
    dataset.data_path,
    dataset.initial_snapshot_path,
    asset_config=asset,
    strategy_config=GridMarketMakingConfig(inventory_skew=10),
)

print(
    {
        "dataset": dataset,
        "book_samples": len(book["timestamps"]),
        "imbalance_samples": len(imbalance),
        "trade_samples": len(trades),
        "vwap_samples": len(vwap),
        "latency_samples": len(latency),
        "lifecycle": lifecycle,
        "market_making": result.to_dict(),
    }
)
