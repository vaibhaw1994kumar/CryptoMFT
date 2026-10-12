# Changes in this folder, and what was reused from `unified_trading_platform`

Date: 2026-09-13. The original folder was backed up before editing, and
nothing was committed. Details are in `docs/execution_realism.md`.

## Reused or adapted from `unified_trading_platform` (minreq layer)

| Unified code | Here | How |
| --- | --- | --- |
| `minreq/latency.py` (stages, lognormal + tail, presets, event-pair report) | `latency.py` | Ported. Presets come from the bundled `advanced_backtest.config`. Added per-stage config overrides, bounded sample store and measured-sample observation |
| `minreq/matching.py` `_refresh` / `_consume` (withhold taken liquidity until the book reflects it) | `DeterministicVenueAdapter` in `execution.py` | Ported to the order book and adapter of this folder |
| `minreq/matching.py` in-flight `send` / `release`, fill on arrival | `StrategyDirectedRouter` in-flight queue and report deliveries | Re-implemented on this folder's existing throttle queue |
| `minreq/router.py` unknown state, venue query, no resend, no close with exposure, residual-breach hold + kill escalation | `OMS.check_order_deadlines`, `_finalize_plan` guards, `_escalate_residual_breach` | Re-implemented in this folder's parent-plan state machine and reducer |
| `minreq/quality.py` price-jump, wide-spread, trade validation, `HostClockAligner` time integrity | `orderbook.py`, `live_feed._trade_rejection`, `time_integrity.py` | Adapted. The jump check gained a confirmation rule; host offset uses a low quantile |
| `minreq/backtest.py` `TradingSession`, `assert_execution_mode` | `session.py` | Adapted to this folder's strategy, RMS and OMS |
| `minreq/live.py` (live paper on real feeds) | `paper_live.py` | Re-implemented on this folder's `LiveOrderBookService` (Binance/Bybit) |
| `minreq/testnet.py` (venue testnet behind the matcher) | `testnet.py` | New Binance Spot / Bybit v5 transports using this folder's credential broker (unified had Binance / OKX) |

Not taken from unified: OKX checksums and connectors (this folder has no OKX
feed), and the synthetic generator (a benchmark generator was written instead).

## New in this folder only

- **Effective dating:** effective-dated instruments and status events (`instruments.py`), the fee schedule (`fees.py`) and the public status poller (`instrument_status.py`).
- **Features:** versioned point-in-time features with `LookaheadError` (SDK 1.2.0).
- **Calibration:** `calibration.py` (`calibrate-latency`, `compare-runs`).
- **Benchmark:** `benchmark.py` (MR-14).
- **Session artifacts:** `runner.write_session_artifacts`, shared by backtest and live paper.
- **Testnet adapter:** order query, send/response timings, and transport failures treated as unknown state.

## Tests and verification

- **Unit tests:** 157 existing plus 26 new, 183 in total, all passing.
  - The existing test changes are limited to the SDK version pin (`1.1.0` → `1.2.0`).
  - New files: `tests/test_execution_realism.py` and `tests/test_forward_testnet_calibration.py`.
- **Offline acceptance:** `scripts/run_all_local.py` passes offline. The sample backtest is unchanged (1 closed plan, net PnL 0.11976) with latency enabled, and the benchmark reports `PASS`.
- **Real public feeds (60 s `live-paper`):**
  - 1,614 events; only 18 were blocked by negative latency, all during warm-up. The audit measured about 98% blocked before this change.
  - Estimated host offset: −256 ms.
  - No opportunities, so the comparison reported `INSUFFICIENT_SAMPLE`.
  - The quality gate correctly rejected the session for one Bybit reconnect (stale intervals) and the warm-up events.

## Still open

- **Testnet:** the transports have not run against the live testnets, which needs testnet keys in a secret manager.
- **Replay-vs-paper accuracy:** not yet measured. It needs a forward sample of more than 30 opportunities; the current strategy thresholds rarely trigger on live BTCUSDT spreads.
- **Order-stage calibration:** entry, ack and fill latencies need a `VENUE_TESTNET` run.
- **Maker fills:** queue-position fills from trade prints exist only in the advanced SDK backtester. The OMS simulator fills resting orders only when the book trades through them.
- **Host clock:** it should be NTP-synced. The correction masks skew but still reports the host clock as `UNSYNCED`.
