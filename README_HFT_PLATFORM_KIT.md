# hft-platform-kit

Typed, installable Python SDK and CLI for deterministic Binance Spot market
data, replay, strategy extension, risk, OMS, paper execution, reporting, and a
native C++ execution-management core.

The distribution is published as `hft-platform-kit` and imported as:

```python
from hft_platform_kit import AppConfig, create_engine, replay_dataset
```

See [SDK_API.md](SDK_API.md) for the reusable library API. The original PoC
CLI remains available alongside the new `hft-platform-kit` command.

## Integrated projects

This repository also contains the crypto stat-arb platform
(`src/crypto_stat_arb`, `crypto-stat-arb` CLI, see
[README_CRYPTO_STAT_ARB.md](README_CRYPTO_STAT_ARB.md)) and the Bybit x Binance
cross-venue arbitrage backtester (`src/new_backtest`, `python -m
new_backtest.backtester`). See [INTEGRATION.md](INTEGRATION.md) for where each
original file now lives.

## Install as a library

After publication:

```bash
pip install hft-platform-kit
```

From a local wheel:

```bash
uv build --wheel --clear
pip install dist/hft_platform_kit-*.whl
```

The EMS extension is native code, so the wheel has Python, ABI, and platform
tags (for example, `cp312-cp312-macosx_...`, `cp312-cp312-manylinux_...`, or
`cp312-cp312-win_amd64`); it is not a `py3-none-any` wheel.

### Platform support

| Platform | Python | Notes |
| --- | --- | --- |
| Linux (x86_64, aarch64) | 3.12, 3.13 | manylinux (glibc) wheels |
| macOS (arm64) | 3.12, 3.13 | Apple Silicon only; see note below |
| Windows (x64) | 3.12, 3.13 | `win_amd64` wheels; Windows on ARM must build from source |

Intel Macs (x86_64) are not supported. The dependency `llvmlite`, reached
via `hftbacktest` → `numba`, publishes no `macosx_x86_64` wheels for any
Python version, so installing on an Intel Mac would have to compile LLVM
from source. An x86_64 wheel of this package builds cleanly but cannot be
installed, so none is published.

Wheels for all three platforms are built by
[`.github/workflows/wheels.yml`](.github/workflows/wheels.yml) with
`cibuildwheel`. Building from the sdist instead requires a C++20 compiler and
CMake (MSVC Build Tools on Windows).

32-bit targets, musl/Alpine, and Python 3.14 are not supported: `pyarrow` and
`hftbacktest` publish no 32-bit wheels, `hftbacktest` depends on
numba/`llvmlite` which publish no musllinux wheels, and upstream `numpy` pins
currently block the dependency set on 3.14.

The event loop is selected per platform: `uvloop` on Linux and macOS, and
`winloop` on Windows, where uvloop has no support. If neither is importable the
CLI falls back to the standard library event loop rather than failing to start.

### Native dependencies are loaded on demand

`pyarrow`, `numpy`, and `hftbacktest` ship compiled extension modules and are
imported only when a Parquet or backtest-export feature is actually used.
Importing `hft_platform_kit` therefore keeps working when those binaries cannot
be loaded, and the pure-Python config, order book, OMS, risk, and EMS surfaces
stay available.

If a native library is blocked you will see an error such as:

```text
ImportError: DLL load failed while importing lib:
An Application Control policy has blocked this file.
```

On Windows this means an Application Control policy (WDAC, AppLocker, or Smart
App Control) is blocking the dependency's `.pyd`, not that the install is
broken. Either ask your administrator to allow the affected files under
`site-packages`, or stay on the JSONL dataset paths, which need no native
dependency.

Embed it:

```python
import asyncio

from hft_platform_kit import AppConfig, create_engine, replay_dataset

engine = create_engine(AppConfig())


async def main() -> None:
    replayed = await replay_dataset("data/my-recording")
    print(replayed.book.checksum())


asyncio.run(main())
```

Custom strategies implement the exported `Strategy` protocol and are injected
with `create_engine(strategy=...)`. Order books, risk engines, OMS instances,
execution venues, and portfolios can also be replaced.

> **Alpha warning:** version `0.x` is suitable for research, backtesting,
> integration, and paper execution. It is not a production real-money trading
> engine.

Client-facing documentation is available in
[CLIENT_POC_OVERVIEW.md](CLIENT_POC_OVERVIEW.md).

The step-by-step client demonstration is available in
[CLIENT_DEMO_RUNBOOK.md](CLIENT_DEMO_RUNBOOK.md).

Markdown is the only source of truth. Word copies are generated on demand and
are deliberately not tracked, so a stale binary cannot contradict its source:

```bash
uv run --with python-docx python scripts/build_client_doc.py \
  --source CLIENT_DEMO_RUNBOOK.md --output CLIENT_DEMO_RUNBOOK.docx
```

[DEMO_GUIDE.md](DEMO_GUIDE.md) walks through every feature end to end —
recording, deterministic replay, backtest export, historical import, live paper
trading with `/status` and `/metrics`, the native C++ SOR and TWAP scheduler,
guarded Testnet execution, and embedding the SDK — with verified commands and
real output.

[CLIENT_DEMO_30MIN.md](CLIENT_DEMO_30MIN.md) is the current presenter's
running order for a 30-minute client demo of the 0.3.0 platform — ten
highlight features built around four claims (it is real, it is reproducible,
it is safe, it is deliverable), in PowerShell, with every command timed and
every printed value captured from a real run.

[DEMO_PLAN.md](DEMO_PLAN.md) is the earlier presenter's running order for a
30-minute business demo — a timed sequence built around three claims (it
works, it is safe, it recovers), with every command verified and a
fallback for each way the session can go wrong. It predates the
minimum-requirements layer and assumes a bash shell with `jq`; for a demo of
the current platform on Windows use CLIENT_DEMO_30MIN.md above.

[CLIENT_TESTNET_TEST_PLAN.md](CLIENT_TESTNET_TEST_PLAN.md) is the hands-on
plan to hand to a client so they can verify the platform themselves against
a real exchange. Unlike every other demo here, it sends signed orders to
Binance Spot Testnet and walks through submit, modify, cancel, and
multi-leg dispatch with expected output and a pass/fail checklist for each
step. Virtual funds only.

Native EMS and guarded exchange evidence is indexed in
[HFT_VERIFICATION_MATRIX.md](HFT_VERIFICATION_MATRIX.md). Adapter details and
official protocol references are in [MULTI_EXCHANGE.md](MULTI_EXCHANGE.md).

The persistent two-venue concurrent-leg state machine — idempotent dispatch,
per-leg timeouts, reject classification, deterministic unwind, restart
recovery, and four-way reconciliation — is documented in
[TWO_VENUE_EXECUTION.md](TWO_VENUE_EXECUTION.md) and runnable offline with
`trading-platform-poc opportunity-demo`.

Venue rate limits, the firm-wide throttle, stale-feed handling, the kill
switch, and atomic multi-resource reservation are documented in
[EXECUTION_CONTROLS.md](EXECUTION_CONTROLS.md) and runnable offline with
`trading-platform-poc controls-demo`.

## Implemented POC

- Binance BTCUSDT depth and trade WebSocket ingestion
- REST snapshot plus incremental L2 book synchronization
- Timestamp/skew, sequence, price, quantity, depth, spread, checksum,
  crossed-book, abnormal-movement, and stale-feed checks
- Message-level crossed/locked market, bid/ask monotonic ordering, duplicate
  price level, internal processing latency, and feed heartbeat detection
- Structured anomaly logging, quarantine, reports, metrics, and a local alert queue
- Raw exchange and normalized event recording
- Zstandard-compressed Parquet plus checksummed dataset manifests
- Deterministic max-speed, real-time, and accelerated replay
- Tardis historical Binance import through the same normalizer
- HftBacktest-compatible NPZ export and package health verification
- Spread-capture strategy with quote cooldown
- Notional, position, open-order, PnL, price-collar, stale-feed, and kill-switch risk controls
- Explicit OMS state transitions
- Configurable paper acknowledgements and cancellations, post-only rejection, partial/resting/marketable fills, and fees
- Positions, exposure, PnL, fill/reject rates, execution latency, CSV files, and run manifests
- FastAPI `/status` and Prometheus `/metrics` endpoints
- Live paper trading using the same strategy, risk, OMS, and paper exchange as replay
- Guarded Binance Spot Testnet market, limit, post-only, cancel, query,
  account, open-order, trade-history, and reconciliation integration
- Native C++ single-loop EMS, atomic multi-leg risk reservation, TWAP/VWAP
  scheduling, fee/latency-aware smart order routing, and recovery states
- Guarded Binance Testnet, OKX Demo, and Deribit Test execution adapters
- Persistent two-venue concurrent-leg opportunity state machine with a SQLite
  WAL ledger, deterministic idempotency keys, per-leg timeouts, reject
  classification, deterministic unwind, restart recovery, and four-way
  reconciliation
- Offline simulated venues so the full execution lifecycle can be demonstrated
  without credentials, network access, or native extensions

The packaged OKX Demo adapter is US-targeted and permits only the exact
`https://us.okx.com` REST origin while always sending
`x-simulated-trading: 1`. Global and EEA accounts require their separately
documented regional origins and a corresponding regional configuration; the
adapter does not silently fall back between regions.

## Setup

```bash
cd /Users/ankusin/trading-platform-poc
uv sync
uv run pytest -q
uv run trading-platform-poc --config config.yaml libraries
```

## Record live Binance data

```bash
uv run trading-platform-poc --config config.yaml record --duration-seconds 30
```

The command prints its output directory, for example:

```bash
data/ccde555a-8457-46f6-aa53-fc44ea397123
```

## Display the live L2 order book

Render the maintained level-2 book in the terminal. No credentials are
required, and the display runs for five minutes by default:

```bash
uv run trading-platform-poc --config config.yaml book \
  --symbol BTCUSDT --depth 10 2>/dev/null
```

Structured logs are written to stderr, so `2>/dev/null` leaves a clean
redrawing table showing both sides of the book, the mid, the spread in basis
points, per-side level counts, and live feed-health state. Use
`--duration-seconds` to shorten the run, `--refresh-ms` to change the redraw
rate, and `--json` to emit one machine-readable state object per refresh.

The stream is connected before the REST snapshot is taken, and buffered
updates already covered by the snapshot are discarded, which is the ordering
Binance requires for a correct book; sequence gaps trigger a resynchronize
rather than silent drift.

## Replay a recording

```bash
uv run trading-platform-poc --config config.yaml datasets
DATASET="$(ls -td data/*/ | head -n 1)"
uv run trading-platform-poc --config config.yaml replay \
  --data "$DATASET" \
  --speed max
```

Use `--speed real-time`, `--speed 10x`, or another positive multiplier for
paced replay. `--data` also accepts `events.parquet` directly.

For a complete client-facing walkthrough covering dataset selection,
deterministic result comparison, report inspection, replay pacing,
HftBacktest export, SDK embedding, and pass/fail criteria, see
[BACKTESTING_DEMO_PLAN.md](BACKTESTING_DEMO_PLAN.md).

## Import Tardis historical data

The first day of each month is available without an API key. Other dates may
require `TARDIS_API_KEY`.

```bash
uv run trading-platform-poc --config config.yaml tardis-import \
  --from-date 2024-03-01 \
  --to-date 2024-03-02 \
  --filter trade \
  --filter depth \
  --max-events 1000
```

## HftBacktest integration

```bash
uv run trading-platform-poc --config config.yaml hft-health
DATASET="$(ls -td data/*/ | head -n 1)"
uv run trading-platform-poc --config config.yaml hft-export \
  --data "$DATASET" \
  --output "${DATASET%/}/hftbacktest-events.npz"
```

The SDK also includes the complete reusable market-making workflow from
`market_making_backtest.ipynb`: deterministic synthetic L2/trade generation,
initial/last snapshots, constant or feed-derived order latency, hash-map and
ROI-vector depth, order lifecycle scenarios, depth imbalance, trade sampling,
rolling VWAP, and an inventory-skewed GTX grid strategy with
`LinearAssetRecord` statistics. It also implements the official HftBacktest
Data Preparation and High-Frequency Grid Trading tutorial paths.

Convert raw Binance Futures collector data. HftBacktest performs its timestamp
latency correction and stable event reordering during this conversion:

```bash
uv run hft-platform-kit hft-convert-binance \
  --input usdm/btcusdt_20240808.gz \
  --output usdm/btcusdt_20240808.npz
```

Convert Tardis files. The CLI accepts trades and depth separately so trades
are always passed to HftBacktest before the associated depth updates, avoiding
double queue depletion. Use `--snapshot-mode ignore_sod` or `ignore` when the
Tardis snapshot policy requires it.

```bash
uv run hft-platform-kit hft-convert-tardis \
  --trade trades.csv.gz \
  --depth incremental_book_L2.csv.gz \
  --snapshot-mode ignore_sod \
  --output btcusdt_20240808.npz
```

Build an end-of-day snapshot from one or several chronological days. For the
next day, pass the prior EOD file with `--initial-snapshot`.

```bash
uv run hft-platform-kit hft-snapshot \
  --data usdm/btcusdt_20240808.npz \
  --tick-size 0.1 --lot-size 0.001 \
  --output usdm/btcusdt_20240808_eod.npz
```

```bash
uv run hft-platform-kit hft-synthetic \
  --output-dir data/synthetic_hft_data \
  --duration-seconds 300

uv run hft-platform-kit hft-grid \
  --data data/synthetic_hft_data/synthetic_btcusdt.npz \
  --snapshot data/synthetic_hft_data/synthetic_btcusdt_initial_snapshot.npz \
  --inventory-skew 10
```

Repeat `--data` and `--latency` for multi-day runs. The official tutorial's
multi-asset profile is exposed by `MultiAssetGridConfig` and
`run_multi_asset_grid_backtests`; it uses notional-derived order sizing,
normalized-position skew, `log_prob_queue_model2`, per-asset recorder NPZs,
five-minute statistics, and combined portfolio equity.

See [SDK_API.md](SDK_API.md#hftbacktest-market-making) and
[`examples/hft_market_making.py`](examples/hft_market_making.py). Run the
complete acceptance workflow in
[HFTBACKTEST_TUTORIAL_TEST_PLAN.md](HFTBACKTEST_TUTORIAL_TEST_PLAN.md).

## Run live paper trading

This sends no real exchange orders.

```bash
uv run trading-platform-poc --config config.yaml paper \
  --duration-seconds 30 \
  --status-port 8000 \
  --kill-after-seconds 20
```

While it runs:

```bash
curl http://127.0.0.1:8000/status
curl http://127.0.0.1:8000/metrics
curl -X POST http://127.0.0.1:8000/kill
```

## Binance Spot Testnet execution

Create Testnet API credentials at <https://testnet.binance.vision/> and export
them without writing secrets to the repository:

```bash
export BINANCE_TESTNET_API_KEY="..."
export BINANCE_TESTNET_API_SECRET="..."
```

Check public connectivity and symbol filters:

```bash
uv run hft-platform-kit \
  --config config.testnet.example.yaml \
  binance-testnet ping

uv run hft-platform-kit \
  --config config.testnet.example.yaml \
  binance-testnet rules --symbol BTCUSDT
```

Validate a signed market order without creating it:

```bash
uv run hft-platform-kit \
  --config config.testnet.example.yaml \
  binance-testnet submit \
  --symbol BTCUSDT \
  --side buy \
  --type market \
  --quantity 0.0001 \
  --daily-net-pnl 0 \
  --test-only
```

Create an actual virtual Testnet order only with explicit confirmation:

```bash
uv run hft-platform-kit \
  --config config.testnet.example.yaml \
  binance-testnet submit \
  --symbol BTCUSDT \
  --side buy \
  --type market \
  --quantity 0.0001 \
  --daily-net-pnl 0 \
  --confirm TESTNET
```

Amend a resting order, or dispatch several legs atomically:

```bash
uv run hft-platform-kit \
  --config config.testnet.example.yaml \
  binance-testnet modify \
  --symbol BTCUSDT \
  --cancel-client-order-id "<id>" \
  --side buy \
  --type limit \
  --quantity 0.002 \
  --price 21000 \
  --daily-net-pnl 0 \
  --confirm TESTNET

uv run hft-platform-kit \
  --config config.testnet.example.yaml \
  binance-testnet multi-leg \
  --leg BTCUSDT:BUY:LIMIT:0.002:20000 \
  --leg ETHUSDT:SELL:LIMIT:0.05:5000 \
  --daily-net-pnl 0 \
  --confirm TESTNET
```

`modify` issues a single Binance `cancelReplace` in `STOP_ON_FAILURE` mode, so
a failed cancel can never leave the replacement resting alongside the
original; only resting orders may be modified. Removing a resting order is
`cancel` — there is no separate delete verb. `multi-leg` validates, risk-checks
and balance-checks every leg before transmitting any of them, summing the
requirement across all legs, then dispatches the admitted legs concurrently.
Atomicity covers admission only: once both orders are live, either can still
fill on its own.

The gateway validates Binance tick, step, quantity, and minimum-notional
filters; signs requests; tracks rate-limit headers; synchronizes server time;
and queries by deterministic client order ID after ambiguous outcomes,
including Binance `-1006` and `-1007` responses regardless of HTTP status.
These outcomes are never blindly retried; unresolved submissions remain open
as `SUBMITTED`, and unresolved cancels remain open as
`RECONCILIATION_REQUIRED` for operator recovery and risk accounting. See the
[official Binance Spot error codes](https://developers.binance.com/docs/binance-spot-api-docs/errors).
The CLI builds position and open-order risk state from the authenticated
Testnet account. `--daily-net-pnl` is supplied explicitly because the CLI does
not own an external portfolio accounting system.

The shipped `BinanceSpotTestnetGateway` is Testnet-only and rejects live
configuration. Typed configuration also requires explicit live enablement
before accepting the canonical live Binance origin; no live gateway is exposed
by this package. OKX and Deribit adapters accept only Demo/Test endpoints.

Reports are written beneath `reports/RUN_ID/`:

```text
orders.csv
order_updates.csv
fills.csv
opportunities.csv
anomalies.jsonl
pnl_summary.json
run_manifest.json
```

Recorded datasets contain:

```text
raw.jsonl
events.jsonl
events.parquet
quarantine.jsonl
dataset_manifest.json
```

## Minimum-requirements stat-arb platform (`mr-*`)

The `mr-*` commands implement the *Minimal Requirements: MFT Stat Arb
Backtesting and Paper Trading* specification (document version 2.1) end to
end: multi-venue capture, canonical schemas, an effective-dated security
master and fee book, deterministic replay, cross-venue features, an OMS/EMS
router with atomic risk reservation and residual handling, a paper matching
engine, an append-only ledger with four-way reconciliation, TCA, monitoring,
reproducible run manifests, a performance benchmark, and acceptance tests
AT-01 to AT-14.

Everything runs offline against a deterministic multi-venue generator, so no
credentials and no network access are required.

```bash
uv run trading-platform-poc mr-generate --output /tmp/mr/dataset --steps 400
uv run trading-platform-poc mr-verify   --dataset /tmp/mr/dataset
uv run trading-platform-poc mr-backtest --output /tmp/mr/run --dataset /tmp/mr/dataset
uv run trading-platform-poc mr-tca      --run-dir /tmp/mr/run
uv run trading-platform-poc mr-acceptance --output /tmp/mr/acceptance
```

| Command | Purpose |
| --- | --- |
| `mr-instruments` | Effective-dated security master |
| `mr-fees` | Effective-dated fee schedule and resolved rates |
| `mr-schema` | Canonical schemas, venue matrix, features, risk, states, monitoring |
| `mr-generate` | Capture and normalize a deterministic dataset |
| `mr-datasets` | List captured datasets and their approval state |
| `mr-verify` | Re-hash a dataset and check its manifest |
| `mr-quality` | Data-quality report for a dataset |
| `mr-backtest` | Full stack in backtest mode |
| `mr-record` | Capture live public feeds into a dataset |
| `mr-paper` | Full stack in paper mode on synthetic or live feeds, with internal or venue-testnet execution |
| `mr-reconcile` | Rebuild state from the ledger and reconcile |
| `mr-tca` | TCA report written by a run |
| `mr-benchmark` | Throughput, overhead and memory |
| `mr-acceptance` | Acceptance tests AT-01 to AT-14 |
| `mr-deliverables` | Section 10 data deliverables |

Defaults live under `minreq:` in `config.yaml`. Fault injection
(`--sequence-gap-at`, `--stale-gap-at`, `--crossed-book-at`, `--duplicate-at`)
makes the quality report and the recovery paths demonstrable.

The full design, requirement map, risk-limit matrix, runbooks and acceptance
sign-off are in
[MINIMUM_REQUIREMENTS.md](MINIMUM_REQUIREMENTS.md). For a scripted 15-minute
walkthrough with verified expected output, see
[MR_DEMO_GUIDE.md](MR_DEMO_GUIDE.md). To hand this to a client, see
[CLIENT_DELIVERY.md](CLIENT_DELIVERY.md).

## Referenced libraries
```bash
uv run trading-platform-poc --config config.yaml libraries
```

Tardis and HftBacktest are active integrations. `level2-order-book` remains an
evaluated optional adapter because it is not Binance-specific. OrderBook-rs
remains an evaluated future PyO3 adapter because the upstream Rust crate does
not ship an official Python module.

## Docker

```bash
docker build -t trading-platform-poc .
docker run --rm trading-platform-poc --config config.yaml hft-health
```

## Reproducibility check

Replay the same recording twice and compare the `result_hash` values in the
two generated `pnl_summary.json` files. They must match.

## Safety

- The POC never submits real Binance orders.
- API credentials are not required for public data, replay, paper trading, or
  the inspection-only EMS demo. Signed Testnet/Demo operations require
  environment-provided virtual credentials.
- `paper` uses live public data and a local simulated exchange.
- This is not a production trading system.

See [POC_FINDINGS.md](POC_FINDINGS.md) for measurements, limitations, and
production recommendations.