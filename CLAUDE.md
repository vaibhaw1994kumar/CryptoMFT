# CryptoMFT: project guide for Claude Code

Cross-venue perpetual arbitrage: Bybit linear perp (maker, PostOnly) hedged on Binance USD-M perp (taker), SOLUSDT.
Goal of the backtester: accurate, reliable replay and simulation. A realistic loss beats a fake profit.

## Read this first: extend, don't create

Before writing any new file:
1. Search `crypto_MFT_arb_platform/src/` for the job. Use the map below, then grep.
2. If a module already does the job, change that module.
3. If nothing fits, say so and ask the owner before creating a new source file.
4. If two modules do the same job, say so. Do not add a third.

- No logic in `out/`. It holds results and short-lived run wrappers. Anything reusable goes into `src/new_backtest/`.
- No new copies of a runner. Do not make a `*_v2`, `*_new` or `*_final` file; change the original.
- Run settings belong in `crypto_MFT_arb_platform/profiles/*.yaml`, not in environment variables or hard-coded in a runner.
- Finished or abandoned files go to `discarded/<version>/` with a line in its `MANIFEST.md`. Never delete them.

## Where the code lives

Working copy: `crypto_MFT_arb_platform/` (git root is the parent, `Newproject/`; branch `integration-2026-11-01`).

Do not edit these:
- `Newproject/src/`, `Newproject/tests/`, `Newproject/scripts/`: an older tree, not the working copy
- `crypto_MFT_arb_platform/src/crypto_stat_arb/crypto_MFT_arb_platform/`: a stale nested copy
- `crypto_MFT_arb_platform/src/crypto_stat_arb/advanced_backtest/`: the older backtester
- `out/**/snap*/`: frozen code snapshots used by runs

## Core map

There are two parallel stacks. `new_backtest` uses only `crypto_stat_arb.quality` from the core, so a fix in one stack does not reach the other.

| Job | Core platform `src/crypto_stat_arb/` | Backtester `src/new_backtest/` |
|---|---|---|
| Orders / lifecycle | `oms.py` | `fill_engine.py`, `execution/layer.py`, `order_state_machine.py` (spec, compared by tests) |
| Order book | `orderbook.py`, `book_gate.py` | `order_book.py`, `queue_v2/rebuild.py` |
| Risk | `risk.py`, `rate_limits.py` | `execution/risk.py`, `circuit_breaker.py` |
| Ledger / economics | `ledger.py`, `economics.py`, `economics_gate.py` | `valuation.py`, `execution/reconcile.py` |
| Funding | `funding.py`, `funding_history.py` | `funding_settler.py` |
| Margin | `margin.py`, `transfers.py` | `margin_model.py` |
| Fees | `fees.py` | `config.py` |
| Latency / clock | `latency.py`, `sim_clock.py` | `latency_model.py`, `timing.py`, `virtual_clock.py` |
| Data quality | `quality.py`, `dataset_manifest.py` | `quality_preflight.py`, `data_manifest.py` |
| Release identity | `release.py` | `profiles.py` |

Backtester only:
- Replay engine: `backtester.py`; runner and walk-forward: `batch_backtest.py`
- Signal: `arb_strategy.py`, `edge.py` (the one place an edge is computed)
- Maker queue: `queue_v2/`; execution: `execution/` (intents, three tactics, risk reservation, reconciliation)
- Data: `event_loader.py`, `cross_venue_feed.py`, `venue_state.py`
- Faults: `hedge_faults.py`, `fault_presets.py`; PostOnly: `postonly.py`
- Fill models: `fill_model*.py`, `fill_label.py`; hedge move: `market_hedge_move.py`

Other packages: `src/trading_platform_poc/` has its own `oms.py`, `order_book.py` and `risk.py` (the live engine on the VM). `src/hft_platform_kit/` is the kit layer.

Key docs: `docs/strategy_contract.md` (the strategy rules), `docs/economic_ledger.md`, `docs/execution_realism.md`.

## Current state

- `profiles/perp_perp_maker_taker_v1.yaml` is FROZEN as a reference. New work goes on `perp_perp_maker_taker_v1_1.yaml`.
- `legacy_v0` must stay byte-identical. Do not change its defaults.
- Current run path: `out/queue_v2/qv2_march_run.py`, started by the pool scripts there. This is temporary; the plan is to merge the runners into `batch_backtest.py`.
- There is no working walk-forward yet. `run_sol_wf.py` is retired, in `discarded/v1.0/`.
- Latest status: the newest `execution_layer_progress_report.html` and `v1_1_fix_ladder_report.html`.

## Data facts

- Oct 2024: Binance "book" is a copy of the trades. Never use it as a book.
- March 2024: Bybit full book, Binance best bid/ask only. TRAIN 03-01..07, SELECT 03-08..12, HOLDOUT 03-13..15. No funding file, so funding is not in March P&L.
- Jan 2022: two partial days, 01-05 and 01-13, used as reference days.
- Never create synthetic depth for anything that produces a profit figure.

## Working rules

- One session edits the code at a time. Check for running jobs first, and stop only your own PIDs; never `pkill` by pattern.
- No commit, push or branch switch without the owner's go-ahead.
- Back up before editing, to `crypto_MFT_arb_platform/_backups/<task>_<timestamp>/`.
- No writes to `data/`, no downloads, and nothing applied to the VM without asking.
- Latency preset is `singapore`. `colocated_both` is only for a labelled latency sensitivity run.
- A fill needs a real trade. Depth changes may move the queue but never fill an order.
- Change one thing at a time and measure it. Label all current P&L EXPLORATORY, with profile, size, days and fees.
- Never train a model on the simulator's own fills.
- Run tests from `crypto_MFT_arb_platform/` with `PYTHONPATH=src python3.12 -m pytest`. Report the real count and any failures.

## Never open or print

- `FAST_MM/hft_scaffold/check_link_orders_history.py`
- `src/crypto_stat_arb/Documents` and `Documents.pub`

Keys come only from environment variables. Account order data stays local: aggregates only in reports, with no order IDs or raw API responses.
