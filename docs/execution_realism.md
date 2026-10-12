# Execution realism, data sanity, live paper and calibration

This document covers the changes that make backtest, live paper and testnet
results comparable. Every mode runs the same `TradingSession`
(`src/crypto_stat_arb/session.py`); only the event source and venue adapters
change.

| Mode | Data | Venue | Label on fills |
| --- | --- | --- | --- |
| `INTERNAL_SIMULATION` | recorded events (`backtest`) | deterministic simulator | `INTERNAL_SIMULATION` |
| `INTERNAL_PAPER` | live public books (`live-paper`) | deterministic simulator | `INTERNAL_PAPER` |
| `VENUE_TESTNET` | live public books | Binance Spot Testnet / Bybit v5 testnet | `VENUE_TESTNET` |

Start-up refuses mislabelled combinations: live data run as a simulation, paper
on recorded data, testnet without one adapter per venue, or testnet adapters
outside testnet mode.

## Latency model (`latency.py`)

Per-venue, per-stage, seeded distributions (uniform or lognormal, plus an
explicit tail):

| Stage | Event pair |
| --- | --- |
| `market_data` | exchange_ts → receive_ts (measured from data) |
| `strategy` | receive_ts → decision_ts |
| `risk` | decision_ts → route_decision_ts |
| `entry` | order send → venue match |
| `submit_ack` | venue match → ack/reject observed |
| `fill` | venue match → fill report observed |
| `cancel` | cancel send → cancel applied |

Config:

```json
"latency": {"enabled": true, "seed": 7, "preset": "singapore",
            "profiles": {"BINANCE": {"entry": {"base_ms": 35, "std_ms": 6, "model": "lognormal"}}}}
```

`preset` reuses `advanced_backtest.config.LATENCY_PRESETS` (singapore, tokyo,
neutral, colocated_both). Per-stage `profiles` override it. `enabled: false`
keeps the zero-latency path, and every summary shows the model source.
`latency_report.json` holds model draws, measured stages and wall-clock
stages. `order_timestamps.jsonl` records send, match, ack and fill times per
order.

## Order lifecycle in time (`execution.py`, `oms.py`)

- **Arrival:** orders are *in flight* for the entry latency. They execute
  against the book as it is at arrival; timers run before the next book update
  is applied. Reports reach the OMS after the ack or fill latency.
- **Liquidity consumption:** quantity we take is withheld until the published
  book reflects it (the level shrinks, or a snapshot or resync arrives).
- **IOC remainders:** they are cancelled with an explicit `CANCELLED` report.
- **Resting orders:** GTC orders fill when the book trades through their
  limit. Queue-position fills from prints stay in the advanced SDK backtester.
- **Unknown state:** a child without a terminal report `report_timeout_ms`
  after it was sent is queried at the venue every `query_interval_ms`. It is
  never resent, and a rejected cancel also triggers a query. After
  `max_queries` unanswered queries the child is `TIMED_OUT` and the kill
  switch trips.
- **No close with doubt:**
  - A plan never hedges or closes while a child is in flight, unknown, or timed
    out without venue confirmation.
  - Working orders are cancelled, and their cancels confirmed, before close.
  - Only a venue snapshot (`reconcile_plan`) clears an unconfirmed timeout.
- **Residual breach:** while any plan is in breach, new plans are rejected
  (`residual_breach_hold`). If the breach outlasts `kill_after_ms`, the kill
  switch trips.
- **Drain:** a backtest drains in-flight work for `backtest.drain_horizon_ms`
  after the last event.

Config: `oms.timeouts` (`report_timeout_ms`, `query_interval_ms`,
`max_queries`, `kill_on_unresolved`) and `oms.residual.hold_new_risk_on_breach`
/ `kill_after_ms`.

## Data sanity (`orderbook.py`, `live_feed.py`, `quality.py`)

- **Price jumps:** a mid move beyond `book_sanity.max_mid_jump_bps` marks the
  book `PRICE_ANOMALY`. It is valid again when the price reverts, or when the
  move persists for `jump_confirmation_updates` updates.
- **Wide spreads:** a spread beyond `max_spread_bps` marks the book
  `PRICE_ANOMALY`.
- **Malformed levels:** non-finite, non-positive prices or negative
  quantities are never applied. The book goes `INVALID_DATA` and the live feed
  resyncs.
- **Trade prints:** non-positive price or size, an unknown side, or a price
  outside `max_trade_deviation_bps` of a valid book mid are rejected into the
  rejection log.
- **Quality counters:** new counters and thresholds `price_jumps`,
  `wide_spreads`, `invalid_levels`, `invalid_trades`, `silent_feeds` and
  `corrected_negative_latency` feed the approval gate.

## Host clock offset (`time_integrity.py`)

With `time.correct_host_offset`, the monitor estimates the host clock offset
as a low quantile (`host_offset_quantile`) of `receive − exchange`.
`HostClockAligner` from the advanced backtester first discards backlogged
stamps. Negative latency that is non-negative after correction (within
`negative_latency_tolerance_ms`) is flagged
`NEGATIVE_RECEIVE_LATENCY_CORRECTED` and not blocked. Events that are still
negative, and events before the estimate warms up (`host_offset_min_samples`),
remain blocked.

## Effective dating (`instruments.py`, `fees.py`)

- **Instrument versions:** instruments may carry `effective_from`, and
  `instrument_status_events` append halts or rule changes. Every lookup on the
  order path uses the event or order timestamp.
- **Fee schedule:** `fees` is an effective-dated maker/taker schedule;
  negative bps is a rebate. Post-only children pay maker, others taker.
- **Status polling:** `instrument-status` and the live paper poller
  (`instrument_status.py`) read the public Binance/Bybit instrument endpoints
  and append a new version when status, tick, lot or minimum changes.

## Features (`sdk.py`)

`FeatureAPI` registers a version per feature and refuses a different version.
With the runtime clock it raises `LookaheadError` in three cases:
- a value is stamped after "now";
- a value is built from an event received after its timestamp;
- a value is published out of time order.

`latest(name, as_of_ns=...)` is point-in-time. The SDK version is 1.2.0.

## Live paper, calibration, comparison

```bash
crypto-stat-arb live-paper --config config/research.json --output-dir runs/paper \
  --session-id p1 --max-runtime-seconds 3600 --no-udp
crypto-stat-arb backtest --config config/research.json \
  --events runs/paper/p1/paper/canonical_events.jsonl --output runs/paper_replay
crypto-stat-arb compare-runs --replay runs/paper_replay --paper runs/paper/p1/paper \
  --output runs/replay_vs_paper.json
crypto-stat-arb calibrate-latency --run-dir runs/paper/p1/paper --output runs/latency_fit.json
```

- **Comparison:** `compare-runs` reports opportunities, closed plans, fills,
  fill ratio, rejects, cancels, missed opportunities, slippage, fees, net PnL
  and latency percentiles against tolerances. The decision is `CONSISTENT`,
  `DIVERGED` or `INSUFFICIENT_SAMPLE` (fewer than 30 forward opportunities).
- **Calibration:** `calibrate-latency` fits lognormal body plus tail per
  stage and writes a `latency` section with a p50/p95/p99 goodness-of-fit
  check. Internal paper only calibrates the data and decision stages. Order
  stages need a `VENUE_TESTNET` run, where `testnet_order_timestamps.jsonl`
  holds real round trips.

## Venue testnet (`testnet.py`)

- **Transports:** Binance Spot Testnet (HMAC query signing, IOC or
  LIMIT_MAKER, order query and cancel) and Bybit v5 testnet (header signing,
  create then query for fills).
- **Credentials:** injected per request by `CredentialBroker` (query uses
  `READ_ACCOUNT`).
- **Endpoints:** only the two testnet hosts are accepted.
- **Failures:** a transport failure leaves the order unknown, so the OMS
  queries it and never resends it.
- **Command line:** the CLI refuses `venue_testnet`, because credentials never
  come from the command line. Build adapters with `build_testnet_adapters` and
  run `LivePaperSession(..., execution_mode=VENUE_TESTNET)` from Python.
- **Verification:** the transports are tested against recorded HTTP responses
  only, not against the live testnets.

## Benchmark (MR-14)

`crypto-stat-arb benchmark --config config/research.json --output runs/bench`
reports:
- replay throughput;
- end-to-end backtest throughput;
- feed-to-strategy, strategy and route-decision p50/p99;
- paper per-frame overhead;
- peak RSS.

Each measure is checked against the config `performance` thresholds.

## Continuous paper-trading watcher (`scripts/paper_watcher.py`)

```bash
PYTHONPATH=src python scripts/paper_watcher.py --root runs/paper_watch \
  --first-cycle-minutes 5 --cycle-minutes 30 [--ssl-cert-file roots.pem]
```

The watcher repeats cycles until stopped with Ctrl+C or SIGTERM. The running
cycle flattens, finalizes and is audited before exit.

1. **Live paper trading** (`live-paper --maker-dry-run`, `config/paper_watch.json`):
   - Runs on live Binance and Bybit public books and trades as `INTERNAL_PAPER`.
   - Strategies:
     - cross-venue arbitrage (`cross_venue_btc_v1`);
     - basis mean reversion (`basis_mean_reversion_v1`, ported from unified `statarb.py`).
   - The queue-aware maker dry run runs alongside (`maker_sim.py`, see
     *Maker simulation* below).
   - Market data is stored as it arrives (wire, raw, canonical, trades,
     hash-chained with part manifests).
   - Mean-reversion entries stop and positions are closed
     `--flatten-lead-seconds` before the cycle ends.
2. **Audit** (`analyze-session`) runs in a separate process while the next
   cycle trades, and reports OK/WARN/FAIL per component:
   - **feed**: connections, parse errors, trade prints.
   - **order_books**: integrity counters, quality gate.
   - **time_integrity**: blocked events, host clock offset.
   - **historical_storage**: wire/raw hash chains, file SHA-256, counts, persistence lag.
   - **orderbook_replay**: stored events rebuild the exact live books.
   - **backtest_replay**: replay of the same period plus `compare-runs` against the forward test.
   - **simulation**: sim_engine replay on stored books and trades.
   - **dry_run**: open plans, unresolved orders, residual, kill switch, reconciliation, maker dry run.
   - **strategies**: warm-up, finite z-score, flat at cycle end, plans closed.
   - **errors**: stderr and structured logs.
3. **Reporting**:
   - `alerts.jsonl` holds every ERROR/WARN.
   - `status.json` holds live progress.
   - `report.md` has one row per cycle and the latest issues.
   - Stdout lines `ALERT`, `CYCLE` and `WATCHER` are for streaming to an operator.

No orders are sent and no credentials are read. Build the C++
`mm_matching_engine` for the running Python (below); without it the maker
simulation uses its bit-identical Python mirror, and the audit reports a WARN.

## Maker simulation

`maker_sim.py` quotes post-only at the touch and simulates the outcome on the
same abstractions as the OMS replay, so it is not a separate latency universe:

- **One event clock.** `sim_clock.SimulationClock` is driven by each canonical
  event's `receive_ts_ns`, in replay *and* live paper (never processing time).
  Arrivals, cancels, acknowledgements, fill reports and the quote scheduler
  are timers; a timer due at an event's timestamp fires before the event.
- **Platform latency.** The `latency` config section (preset and per-venue
  stage overrides) drives every delay, as a separate instance with a derived
  seed so the OMS draws do not move. Stage composition mirrors the OMS:
  decision `t` -> dispatch `t + strategy + risk` -> venue arrival
  `+ entry`, **where the queue position is assigned** against the book at
  arrival. A cancel applies at `send + cancel`; the order can still fill until
  then. Fill reports are observed at `match + fill`, acks/rejects at
  `+ submit_ack`; the strategy's inventory only changes when a fill report is
  observed. `maker_simulation.latency` overrides stages for sensitivity runs
  (e.g. `{"profiles": {"BYBIT": {"entry": {"base_ns": 300}}}}`).
  `maker_simulation.latency_ms` is rejected.
- **Quote cadence is separate from latency.** `quote_interval_ms` is strategy
  scheduling. An unchanged quote keeps resting (and keeps its queue priority);
  a moved quote is cancelled and replaced. A post-only order that would cross
  at arrival is rejected.
- **Canonical trades.** Public prints are `models.TradeEvent` envelopes
  (validated by `canonical.validate_canonical_event`, routed by the
  dispatcher). The live feed stamps one `arrival_ordinal` across book and
  trade streams and writes it to `trace.jsonl`; replay reproduces the live
  interleaving from the trace (older traces: line order). Without a trace,
  events are ordered by receive time, exchange time, then a declared per-venue
  kind priority (`maker_simulation.replay_tie_priority`, default trade before
  book); native book and trade sequences are never compared.
- **Print-driven fills.** `maker_engine` wraps the C++ `mm::MatchingEngine`
  (or its Python mirror). Level sizes come only from the book feed; prints
  burn queue ahead in FIFO order; a book update can only cap queue ahead (with
  a credit so a print and its book delta count once in either order); a print
  through our price fills the order. Every fill row in `*_fills.jsonl` names
  the print (`trade_event_id`) that caused it, its queue ahead at arrival and
  its decision/dispatch/arrival/match/report timestamps. A book crossing a
  resting order without a print is counted (`crossed_without_print`), never
  filled.

Two explicit assumptions are configurable under `maker_simulation` so they can
be swept and later calibrated:

- `queue_position_frac` (default `1.0`): share of the visible level assumed
  ahead of a newly arrived order (1.0 = back of the queue; the old
  `sim_engine` Python model hard-coded 0.7). Applied identically in C++ and
  Python.
- `feed_lag_ms` (default `0`; a number or `{venue: ms}`): market-data
  staleness. A venue-side instant (order arrival, cancel applied) is placed
  `feed_lag` later on the receive-time clock, so the queue is taken from the
  book received for the venue's state at arrival; reports are observed from
  the venue instant (`- feed_lag + report latency`).

Each fill also records signed markouts (`markout_horizons_ms`, default 1 s
and 5 s; positive = favourable) as an adverse-selection measure.

`scripts/maker_sensitivity.py` replays recorded cycles across a grid of
`feed_lag_ms` × `queue_position_frac` and writes `results.csv`,
`summary.json` and `SENSITIVITY.md` under `runs/maker_sensitivity/<stamp>/`.

Build the C++ engine for the Python that runs the platform:

```bash
python -m pip install pybind11
cmake -S src/crypto_stat_arb/sim_engine -B build/sim_engine -Dpybind11_DIR="$(python -m pybind11 --cmakedir)"
cmake --build build/sim_engine
```

The module is written into `src/crypto_stat_arb/sim_engine/`.
