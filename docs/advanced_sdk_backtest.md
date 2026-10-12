# Advanced backtesting through Strategy SDK 1.1

The supplied event-driven cross-venue backtester is integrated under
`crypto_stat_arb.advanced_backtest` and exposed through the stable SDK as
`BacktestAPI`. It is an offline-only service: `PAPER` and `TESTNET` contexts
fail closed if code attempts to invoke it.

## Simulation capabilities

- Receive-time merge across Binance and Bybit.
- Measured or sampled feed, entry and cancellation latency.
- Singapore, Tokyo, neutral and deliberately optimistic co-located presets.
- Full snapshots, incremental L2 updates and public trades.
- Queue-ahead maker fills and empirical fill-probability learning.
- Taker depth walking with price limits and optional liquidity consumption.
- Taker/taker and maker/taker cross-venue execution modes.
- Book/touch freshness, crossed-book, toxicity and visible-depth gates.
- Per-venue inventory and aggregate position limits.
- Partial-leg unwind and explicit leg-risk attribution.
- Fees/rebates, slippage, expected/realized edge and edge-capture reporting.
- Fill markouts at configurable horizons.
- Single-pair and two-pass batch backtests.

## Expected historical-data layout

```text
<data-root>/
  bybit/
    book_snapshots/pair=BTCUSDT/date=2026-04-16/*.jsonl.gz
    book_events/pair=BTCUSDT/date=2026-04-16/*.jsonl.gz
    trades/pair=BTCUSDT/date=2026-04-16/*.jsonl.gz
  binance/
    book_snapshots/pair=BTCUSDT/date=2026-04-16/*.jsonl.gz
    book_events/pair=BTCUSDT/date=2026-04-16/*.jsonl.gz
    trades/pair=BTCUSDT/date=2026-04-16/*.jsonl.gz
```

The loader tolerates truncated gzip tails, merges overlapping collector files,
deduplicates exchange events, and keeps memory bounded by processing one day at
a time.

## Python SDK

```python
from pathlib import Path

from crypto_stat_arb.sdk import AdvancedBacktestSpec, BacktestAPI

spec = AdvancedBacktestSpec(
    pair="BTCUSDT",
    data_root=Path("data/tardis"),
    start_date="2026-04-16",
    end_date="2026-04-25",
    execution_mode="taker_taker",  # or maker_taker
    latency_preset="singapore",
    min_net_edge_bps=1.0,
    max_walk_bps=2.0,
    max_clip_notional=25_000.0,
    output_dir=Path("runs/sdk_backtest/BTCUSDT"),
)

result = BacktestAPI().run(spec)
print(result["realized_pnl"])
print(result["edge_capture_ratio"])
```

Inside an SDK runtime created with `RuntimeMode.BACKTEST`, the same facade is
available as `runtime.context.backtest`.

Batch mode uses the same engine:

```python
summary = BacktestAPI().run_batch(
    spec,
    ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    two_pass=False,
)
```

For `maker_taker`, omit `two_pass` to train the empirical fill model on pass 1
and warm-start pass 2 automatically. Pass 2 is the reportable result.

## CLI

```bash
crypto-stat-arb sdk-backtest \
  --pair BTCUSDT \
  --data-root data/tardis \
  --start 2026-04-16 \
  --end 2026-04-25 \
  --mode taker_taker \
  --latency-preset singapore \
  --min-edge-bps 1.0 \
  --max-walk-bps 2.0 \
  --max-clip-notional 25000 \
  --output runs/sdk_backtest/BTCUSDT
```

Key outputs are `result.json`, `trades.jsonl`, `fill_events.jsonl`, and
`fill_bucket_stats.json`.

## Tardis preparation

The imported normalizer/downloader is available at:

```bash
python -m crypto_stat_arb.advanced_backtest.fetch_tardis --help
```

Downloading requires the user's own Tardis data access. The API key is a data
vendor credential, not an exchange-trading credential. Never place it in
strategy configuration, release manifests, logs, or committed files.

## Important interpretation

The engine measures execution feasibility and PnL under historical replay; it
does not prove live profitability. Fee tiers, latency distributions, inventory,
Tardis venue/product mapping and fill assumptions must match the intended
deployment. `colocated_both` is physically optimistic and should be treated
only as an upper-bound sensitivity scenario.
