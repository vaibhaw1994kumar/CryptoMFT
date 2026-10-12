# Crypto Stat-Arb Platform

Deterministic research platform for a simple two-venue crypto spread/mean-reversion strategy. The same canonical event, instrument, risk, order and ledger contracts are intended to carry forward into live-data paper and testnet adapters.

For complete Windows, macOS and Linux setup commands, the one-command offline
acceptance runner, optional bounded public-feed checks, service launch commands,
and a compact system architecture, see `docs/LOCAL_RUNBOOK.md`.

Strategy SDK 1.1 also contains the latency-, queue-, depth- and empirical-fill
cross-venue backtester imported from `new_backtest.zip`. Use the
`sdk-backtest` CLI or `BacktestAPI`; see `docs/advanced_sdk_backtest.md`.

## Latency, live paper, calibration and benchmark

Backtests model per-venue order latency (`latency` config, default preset
`singapore`): orders execute against the book at arrival, reports return later,
and unknown order states are resolved by venue query. The same session runs on
live public books:

```bash
crypto-stat-arb live-paper --config config/research.json --output-dir runs/paper \
  --session-id p1 --max-runtime-seconds 600 --no-udp
crypto-stat-arb backtest --config config/research.json \
  --events runs/paper/p1/paper/canonical_events.jsonl --output runs/paper_replay
crypto-stat-arb compare-runs --replay runs/paper_replay --paper runs/paper/p1/paper --output runs/compare.json
crypto-stat-arb calibrate-latency --run-dir runs/paper/p1/paper --output runs/latency_fit.json
crypto-stat-arb benchmark --config config/research.json --output runs/bench
crypto-stat-arb instrument-status --config config/research.json
```

Details: `docs/execution_realism.md`.

## Run the sample backtest

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
crypto-stat-arb normalize \
  --config config/research.json \
  --raw data/samples/raw_depth.jsonl \
  --output data/canonical/sample.jsonl
crypto-stat-arb backtest \
  --config config/research.json \
  --events data/samples/two_venue_books.jsonl \
  --output runs/sample
PYTHONPATH=src python -m unittest discover -s tests -v
```

The run produces:

- `canonical_events.jsonl`: the exact immutable envelope delivered to the strategy path.
- `time_integrity_report.json`: timestamp policy, venue latency/feed-age distributions, alignment breaches and host-clock state.
- `data_quality_report.json`: approval decision, health state, counts, thresholds, active flags, limitations and attestation hash.
- `data_quality_defects.jsonl`: hash-chained defects linked to the run or partition.
- `ledger.jsonl`: traceable opportunity, child fill and parent-plan events.
- `account_state.json`: ledger-rebuilt position, average-price, PnL, balance and capacity state.
- `rms_reconciliation_report.json`: fail-closed end-of-run OMS/RMS consistency decision.
- `pretrade_validation.json`: complete pass/fail risk matrix for the last opportunity.
- `exposure_utilization.json`: strategy gross/net and instrument/venue gross exposure, limits, utilization, binding events and state transitions.
- `kill_switch_status.json`: PM-facing strategy-stop state, activation reason, risk-reduction policy, reconciliation lock and audit history.
- `margin_status.json`: derivative collateral, initial/maintenance margin, liquidation buffer, venue report and alert state.
- `pipeline_monitoring.json` and `pipeline_metrics.prom`: one thresholded view of pipeline health plus opportunity/edge, route, order outcome, residual, child latency, PnL, rate-limit, connector, account/margin and kill-switch state.
- `tca_report.json` and `tca_daily/YYYY-MM-DD.json`: reproducible run/daily fills, slippage, fee, reject, residual/unwind and missed-opportunity analysis by venue, instrument and order type, with raw-data lineage.
- `structured_logs.jsonl` and `structured_logs.sqlite`: central searchable connector/paper-host logs correlating source event, feature state, opportunity, risk decision, reservation, parent plan, child order, request and route.
- `failed_opportunity_traces.jsonl`: expanded end-to-end traces for every failed opportunity.
- `summary.json`: PnL, fees, data-quality counts and final book hashes.
- `manifest.json`: exact config, input hashes, build version and deterministic result hash.

## Preserve and trace raw market-data frames

Capture public feed envelopes into a new immutable session:

```bash
PYTHONPATH=src python -m crypto_stat_arb.cli raw-capture \
  --input data/samples/raw_depth.jsonl \
  --output-dir data/raw \
  --session-id session-001
```

Verify payload checksums and the record hash chain:

```bash
PYTHONPATH=src python -m crypto_stat_arb.cli verify-raw \
  --raw data/raw/session-001/raw.jsonl
```

Normalize the captured session:

```bash
PYTHONPATH=src python -m crypto_stat_arb.cli normalize \
  --config config/research.json \
  --raw data/raw/session-001/raw.jsonl \
  --output data/canonical/session-001/events.jsonl
```

Each raw record preserves the untouched payload, venue, vendor, channel, host, transport, local receive timestamp, native sequence, adapter version and raw schema version. Normalization writes canonical events, quarantine records, a checksummed manifest and a `raw_ref` to canonical `event_id` trace.

## Continuously download Binance and Bybit order books

Install the package once so the asynchronous WebSocket dependency is available, then start the public market-data service:

```bash
python -m pip install -e .
PYTHONPATH=src python -m crypto_stat_arb.cli live-orderbook \
  --config config/research.json \
  --output-dir data/live
```

The command runs until `Ctrl-C` or `SIGTERM`. It connects to Binance and Bybit concurrently, records every received wire frame durably before JSON parsing, fetches the Binance REST snapshot while WebSocket deltas are buffered, and subscribes to a Bybit order-book stream that supplies its initial snapshot. It then maintains one independent sequence-aware L2 book per venue.

On a connection failure, feed-age breach, sequence gap, crossed book or checksum failure, the affected venue is disconnected and reconnected with bounded exponential backoff. Binance fetches a new REST snapshot; Bybit reconnects and waits for a new exchange snapshot. No invalid book is silently repaired. The existing `BookIntegrityGate` evaluates after every book event: it suspends strategy eligibility when any required book is unsafe, emits one cancel-all callback per safe-to-unsafe transition, and resumes only after all selected books recover. The standalone CLI has no working orders, so its default cancel callback reports zero cancellations; the paper runtime can inject its OMS cancel callback.

Each rotating session part contains:

- `wire.jsonl`: exact pre-parse WebSocket/HTTP response frames with checksums and a hash chain;
- `raw.jsonl`: parsed payloads plus venue/vendor/channel/host, receive timestamp, sequence, adapter version and `wire_ref`;
- `canonical.jsonl`: immutable canonical book events used by reconstruction;
- `trades.jsonl`: normalized public trade/LTP ticks with raw lineage;
- `trace.jsonl`: `wire_ref → raw_ref → canonical_event_id` lineage;
- `quarantine.jsonl`: parse or normalization failures without loss of the original wire frame;
- raw and live part manifests with SHA-256 checksums.

The live session also atomically refreshes `pipeline_monitoring.json` and Prometheus-compatible `pipeline_metrics.prom` every second. These expose live feed age, reconnects, stale/invalid book state, gaps, persistence lag, partition state and canonical/instrument versions. When a paper OMS is attached, the same envelope adds correlated strategy/execution and trading-control telemetry. See `docs/pipeline_monitoring.md`.

Every emitted pipeline or paper alert includes a named owner, severity, response target, response expectation and research-hours coverage declaration. Failed ingestion, invalid books, replay failures and risk breaches have explicit policies, and a forced-alert path validates the machine-readable JSON and Prometheus output. See `docs/alert_ownership.md`.

The versioned Python Strategy SDK exposes canonical books/events, deterministic replay, source-linked features, ledger-derived portfolio state, RMS and OMS/EMS through one mode-neutral context. The supplied cross-venue strategy runs unchanged with paper or injected testnet adapters; the acceptance test verifies identical order requests and portfolio results. See `docs/strategy_sdk.md` and `examples/strategy_sdk.py`.

Paper releases are immutable, content-addressed bundles of strategy code, feature definitions, models, parameters, data manifest and simulator configuration. Activation requires a named approval owner; build/environment/deployment metadata is hash-chain audited, and only a verified approved release can be reproduced or selected as a rollback target. See `docs/versioning_configuration.md`.

Offline backtests reject all exchange credential references and plaintext secret-shaped configuration. Paper/testnet access uses an external secret-manager interface, venue/environment-scoped least privilege, a permanent no-withdrawal rule, ephemeral adapter injection, versioned rotation/revocation and a secret-free hash-chained access audit. See `docs/credential_security.md`.

The session closes with `live_manifest.json`, containing connection/reconnect/resync counters, final book states and hashes, integrity statistics and checksums for every part. Files rotate hourly or after 256 MiB by default; configure this with `--rotate-seconds` and `--rotate-bytes`.

### One-minute order-book and LTP OHLC, plus UDP multicast

The live command also writes `data/live/<session-id>/ohlc_1m.csv`. Bars are constructed independently for each venue from **valid reconstructed books**, using UTC local receive time. Every row contains bid, ask, midpoint and spread OHLC, sample count, first/last source event IDs and an `is_final` flag. Completed bars are written at the minute boundary; an in-progress bar is written with `is_final=false` during graceful shutdown. Order-book data does not contain traded volume, so this dump intentionally reports update/sample count rather than inventing volume.

It separately subscribes to each venue's public trade stream and writes `ltp_ohlc_1m.csv`. These bars use actual last-traded prices—not bid, ask or midpoint—and contain LTP open/high/low/close, trade count, base volume, quote volume and first/last trade event IDs. Buckets are UTC epoch minutes selected by local `receive_ts_ns`; exchange timestamps remain on every tick for audit. Disable trade capture with `--no-trades`; set its silence timeout with `--trade-stale-seconds`.

By default the same command publishes four typed JSON messages to IPv4 UDP multicast group `239.255.77.77`, port `19001`:

- `ORDERBOOK`: reconstructed top 20 bids/asks, validity state, sequence, timestamps, source event ID and book hash;
- `OHLC_1M`: the completed or shutdown-partial CSV bar represented as JSON.
- `TRADE`: every normalized public trade/LTP tick;
- `LTP_OHLC_1M`: completed or shutdown-partial one-minute LTP OHLC.

Start any number of independent consumers on the same machine or multicast-enabled network:

```bash
PYTHONPATH=src python examples/udp_multicast_consumer.py \
  --group 239.255.77.77 \
  --port 19001
```

Run that consumer command in multiple terminals; each joined multicast subscriber receives the feed. Filter with `--type ORDERBOOK`, `OHLC_1M`, `TRADE`, or `LTP_OHLC_1M`. Publisher sequence numbers let consumers detect dropped or out-of-order UDP datagrams. UDP is intentionally non-blocking and best-effort—the persisted wire/raw/canonical/trade/OHLC files remain the authoritative replay source.

Publisher options include `--udp-group`, `--udp-port`, `--udp-interface`, `--udp-ttl`, `--udp-depth` and `--udp-max-datagram-bytes`. Use `--no-udp` to retain continuous capture and OHLC generation without network publication.

Verify an exact pre-parse wire partition or its parsed raw records:

```bash
PYTHONPATH=src python -m crypto_stat_arb.cli verify-wire \
  --wire data/live/<session-id>/part-000001/wire.jsonl
PYTHONPATH=src python -m crypto_stat_arb.cli verify-raw \
  --raw data/live/<session-id>/part-000001/raw.jsonl
```

Use `--max-runtime-seconds 60` for a bounded connectivity check, or `--max-events 1000` for a bounded event capture. Add repeated `--venue BINANCE` / `--venue BYBIT` flags to select one or both venues. Spot is the default. `--binance-market linear --bybit-market linear` selects public linear-perpetual endpoints; the supplied config must be changed to identify the matching derivative contracts rather than the sample spot instrument.

## Canonical event envelope

Every normalized Binance and Bybit message becomes the same frozen `MarketEvent` envelope. Schema version 1.2.0 includes the common event type, venue, instrument ID, source transaction time, exchange publication time, local receive time, sequence, quality flags, optional venue checksum, adapter/schema versions and immutable raw reference. Impossible receive latency is retained and tagged `NEGATIVE_RECEIVE_LATENCY` rather than silently repaired.

`CanonicalEventDispatcher` validates each envelope once and passes the same object instance to both the strategy callback and `CanonicalEventRecorder`. The published JSON Schema is at `docs/schemas/canonical_event_envelope.schema.json`; venue examples are in `data/samples/canonical/`.

## Book integrity and strategy safety

`OrderBook` buffers deltas until a snapshot arrives, then applies only the contiguous sequence after that snapshot. Duplicate and older updates are counted and ignored; a forward gap invalidates the book without applying the packet and requires a new snapshot. Crossed/locked books and supplied checksum failures are invalid. Checksum verification is adapter-injected because the selected Binance and Bybit channels do not consistently publish the same checksum format.

The explicit states are `SYNCING`, `VALID`, `STALE`, `GAP_DETECTED` and `CHECKSUM_FAILED`; only `VALID` is tradable. `BookIntegrityGate` watches every required venue, blocks strategy evaluation on any unsafe state, issues one cancel-all callback on the valid-to-unsafe transition, suppresses repeated cancel storms and resumes only after every book is valid again. Integrity counters are written into the run summary.

## Common OMS/EMS and strategy-directed routing

Backtesting and paper adapters share the same immutable `CanonicalOrderRequest` for `SUBMIT`, `CANCEL` and `REPLACE`. It contains instrument, venue, side, price, quantity, order type, time-in-force, post/reduce-only flags, parent opportunity/plan IDs and an idempotency key. `StrategyDirectedRouter` validates the request against the instrument master, sends that exact object to the selected venue adapter and returns correlated acknowledgement, partial-fill, fill, reject, cancel or replace callbacks.

The deterministic adapter implements limit/market, GTC/IOC/FOK and post-only behavior against reconstructed L2 books. Each backtest writes `order_trace.jsonl`, joining opportunity → parent → child → request → callback → fill without duplicate adapter actions. See `docs/order_api.md`, the published schema at `docs/schemas/order_api.schema.json`, and samples in `data/samples/orders/`.

Strategies can alternatively submit a `RoutingInstruction` with eligible venues, route priority, maker/taker intent and an optional eligible-venue override. Before dispatch, the router records account and venue state, rate-limit headroom, supported order semantics, instrument support and book availability. An unavailable preferred route falls through to the next eligible venue; overrides never bypass safety validation. The routing schema and two-venue override sample are in `docs/schemas/routing_instruction.schema.json` and `data/samples/orders/two_venue_route_override.json`.

Paper execution can attach a `ParentPlanStore` backed by SQLite WAL. It atomically persists one parent and all deterministic children through created, risk-reserved, dispatch-intent, partial/filled, hedge/unwind, reconciled and closed states. Nonterminal plans are restored on process restart; the same opportunity returns the recovered plan without resubmission. `VenueOrderSnapshot` reconciliation converges local children to authoritative venue state and recomputes residual, fees and PnL before closure. See `docs/schemas/parent_plan.schema.json` and `data/samples/orders/parent_plan_restart.json`.

Package admission is all-or-none. Before the first child dispatch, `RiskEngine` reserves total notional, buy-side cash, initial margin, per-venue simulated position, planned residual exposure and outbound message capacity. `AccountRiskSnapshot` supplies paper balances and venue/account limits through the same rule. If any leg fails a local limit, the plan is rejected with zero adapter calls and no partially mutated reservation state. Reservation creation/release is included in the ledger and durable plan history; see `docs/schemas/risk_reservation.schema.json`.

Accepted packages use a documented deterministic sequencing policy: dispatch children in stable parent-list order after the entire package is risk-reserved. Every child has independent dispatch-start/completion/duration measurements, and every callback has separate adapter-event and local-observation timestamps. `PLAN_STATE_TRANSITION` records make the parent lifecycle explicit. Backtests use deterministic logical nanoseconds; paper mode can inject its host receive clock. See `data/samples/orders/coordinated_dispatch_ledger.jsonl`.

The OMS lifecycle is now a validated state machine: `CREATED → RISK_RESERVED → DISPATCHED → ACKNOWLEDGED / PARTIAL / FILLED / REJECTED / TIMED_OUT → HEDGE_UNWIND → RECONCILED → CLOSED`. `CanonicalOrderUpdate` makes adapter callbacks, private-stream events, REST order snapshots and local timeouts enter one deterministic reducer. Updates are correlation-checked and duplicate-suppressed; cumulative fills never regress, conflicting terminal claims use published precedence, and `CLOSED` is the sole immutable parent terminal state. The separate `terminal_outcome` preserves whether the plan filled, rejected, timed out or closed after a partial result. See `docs/schemas/order_state_update.schema.json` and `data/samples/orders/converged_state_machine_ledger.jsonl`.

Per-venue rate limits now have explicit `REJECT`, `QUEUE` and `DELAY` simulation policies. Every route check exposes fixed-window capacity, current usage, headroom, whether the limit bound, the exact action and scheduled release time. Queues are bounded and FIFO; capacity is consumed only when a request reaches the adapter. A throttled OMS package stays open with risk reserved until `process_throttled_routes()` releases all children and converges their callbacks. See `docs/schemas/rate_limit_status.schema.json` and `data/samples/orders/rate_limit_burst_ledger.jsonl`.

Residual exposure is now enforced by a configurable OMS policy. Failed or asymmetric packages create durable reduce-only `UNWIND` children using the configured hedge-venue priority and passive, normal or aggressive price/urgency rule. Residual age expiry escalates to market IOC. Risk-reducing orders can continue under a strategy stop only when explicitly allowed, while a package that remains outside the approved residual bound stays open in `HEDGE_UNWIND` with risk reserved. See `docs/schemas/residual_policy.schema.json` and `data/samples/orders/residual_unwind_ledger.jsonl`.

Legging control runs after every child fill, tracking maximum one-sided marked notional, residual base size and residual duration. When a limit binds, the ledger records the binding limits and explicitly chooses the remaining hedge leg or deterministic unwind. A risk-reducing hedge may cross a global kill switch, a locally disabled venue or exhausted ordinary message capacity under the emergency policy; the router records every such override and never labels a cross-venue primary hedge as exchange `reduce_only`. See `docs/legging_controls.md` and `data/samples/orders/legging_control_ledger.jsonl`.

Stale, syncing, gapped, checksum-failed or crossed books cannot create opportunities or support ordinary routing. The integrity gate projects each book state into a venue EMS mode: `ACTIVE` for a valid fresh book and `RISK_REDUCING_ONLY` otherwise. On degradation it cancels ordinary venue orders once, preserves exposure-reducing work, and records the reason; a fresh valid snapshot restores ordinary routing automatically. Risk reduction never uses an invalid book for pricing and falls through to an eligible valid hedge venue. See `docs/stale_invalid_feed_control.md`.

The strategy kill switch now has an audited operational lifecycle. `OMS.activate_kill_switch()` blocks new package reservations, cancels adapter-resting and EMS-queued children, records the PM/source/reason and publishes whether risk-reducing orders may continue. Reset is impossible until the latest RMS order/fill/position/control reconciliation is approved, exposure limits are normal and no venue remains suspended. `OMS.recover_kill_switch()` performs that reconciliation before recording the PM reset. See `docs/kill_switches.md` and `kill_switch_status.json`.

The order/fill ledger persists every submit, acknowledgement, partial/full fill, cancel, reject, timeout and synthetic reconciliation control with complete correlation IDs. Callback, REST and private-stream fills use one cumulative-delta accountant, preventing duplicate position or cash effects. The ledger rebuilds quantity, average price, realized/unrealized PnL, cash/equity and available/reserved capacity; nonterminal paper plans restore both fill state and risk reservations after restart. `OMS.reconcile_risk_state()` compares these fields with venue/account state and fails closed on any unexplained mismatch. Backtests publish `account_state.json` and `rms_reconciliation_report.json`. See `docs/rms_reconciliation.md`.

Trade breaks distinguish missing local fills, extra local fills, conflicting order states, missing/orphan venue orders and incomplete opportunities. The affected venue is suspended for new package risk while late REST/private-stream corrections remain admissible. A clean observed reconciliation resolves the venue automatically; unrelated venues are not stopped.

RMS exposure control values every ledger-derived position at the current mark and separately tracks strategy gross, absolute strategy net, per-instrument gross and per-venue gross notional. Global defaults can be overridden by strategy, canonical instrument ID or venue. The same projected calculation runs before package admission, while post-fill monitoring publishes utilization and binding events. A breach durably transitions the control from `NORMAL` to `BREACHED` and activates the kill switch; recovery is recorded but operator reset remains explicit. See `docs/rms_exposure_limits.md` and `docs/schemas/exposure_utilization.schema.json`.

Derivative and leveraged paper accounts now have an explicit margin/collateral layer. The RMS marks gross notional, initial margin, maintenance margin and liquidation buffer per venue, incorporates venue-reported margin conservatively, warns before the configured buffer is exhausted, and fails closed below maintenance. Cash spot portfolios remain `NOT_APPLICABLE`. Margin balance and requirement mismatches participate in normal venue reconciliation. See `docs/margin_collateral.md`, `docs/schemas/margin_status.schema.json` and `data/samples/orders/margin_status.json`.

Every package now passes one pre-trade matrix before reservation or child dispatch. It checks instrument/status, price band, tick and lot size, minimum quantity/notional, platform/venue/strategy notional limits, live-book validity and age, trade-break state, venue/account/strategy controls, and available paper cash/margin. Any failed row rejects the complete package with zero child dispatches; the full matrix is persisted in the ledger and `pretrade_validation.json`.

## Time handling

All canonical times are UTC Unix epoch nanoseconds. Replay is ordered by recorded local receive time, then exchange time, source time, venue, sequence range and event ID; processing speed never changes simulated time or results. Cross-venue alignment compares local receive times for the same instrument and reports threshold breaches without rewriting source data.

`TimeIntegrityMonitor` blocks impossible/negative latency from the strategy path and reports excessive latency, future timestamps, feed age and cross-venue skew. `HostClockMonitor` accepts trusted NTP/PTP/server-time samples and reports offset, drift and `UNKNOWN`, `SYNCED`, `DEGRADED` or `UNSYNCED` state. See `docs/time/timestamp_dictionary.json` for the published timestamp dictionary and formulas.

## Pipeline orchestration

Run or resume one raw-to-canonical partition:

```bash
PYTHONPATH=src python -m crypto_stat_arb.cli pipeline-run \
  --config config/research.json \
  --state-db data/pipeline/state.sqlite \
  --job-id depth-20260904 \
  --partition-id 2026-09-04T10 \
  --raw data/raw/session-001/raw.jsonl \
  --output data/canonical/2026-09-04T10/events.jsonl
```

Add `--continuous` to tail the growing partition, with `--poll-seconds` controlling cadence. `--max-polls` is available for bounded service checks. Repeating the same command resumes from its committed byte offset and does not duplicate canonical records.

`PipelineOrchestrator` uses SQLite WAL with full synchronous commits. The canonical record and input checkpoint advance in the same transaction, and JSONL is rebuilt by atomic replacement from committed rows. It supports bounded transient retries, sorted multi-partition backfills, appended late data, permanent-error quarantine and explicit `PENDING`, `RUNNING`, `PAUSED`, `RETRY_WAIT`, `CAUGHT_UP`, `COMPLETE`, `COMPLETE_WITH_REJECTIONS` and `FAILED` states. Every output has `.partition.json` and `.quarantine.jsonl` sidecars; the partition report includes unpersisted bytes and persistence lag.

Paper-process restarts are fenced by a durable single-owner lease. Every takeover receives a higher fencing token, restores exact per-stream checkpoints and nonterminal OMS parents, reconnects all required venues, and must pass OMS/RMS reconciliation before new strategy risk is enabled. Heartbeats extend ownership; stale processes cannot route after takeover. Connector retry uses bounded exponential backoff, while recovered opportunity IDs remain submission-idempotent. See `docs/restart_resilience.md`.

## Data-quality gate

`QualityGate` consolidates event counts, sequence gaps, stale intervals, crossed/locked books, duplicates, out-of-order packets, schema exceptions, rejected records/partitions, negative/impossible latency, unknown instrument rules, persistence lag and unpersisted bytes. Thresholds are versioned in `config/research.json`.

Every evaluation publishes `HEALTHY`, `DEGRADED` or `CRITICAL` health and explicit active flags. Dataset approval is fail-closed: any threshold breach produces `REJECTED`; only a flag-free dataset is `APPROVED`. Reports include declared limitations, the complete effective threshold set, a defect-log SHA-256 and an approval attestation hash. Defects are correlation-linked and hash-chained. Pipeline quality sidecars are refreshed on pause, retry, caught-up and completion states.

`runs/verified/data_quality_report.json` is the approved baseline. `data/evidence/quality_gate/controlled_gap/` is a deliberately rejected example showing a sequence-gap defect and blocked strategy execution.

## Cross-venue alignment

Merge canonical venue streams without changing their native state:

```bash
PYTHONPATH=src python -m crypto_stat_arb.cli align-streams \
  --config config/research.json \
  --input BINANCE=data/evidence/alignment/input/binance.jsonl \
  --input BYBIT=data/evidence/alignment/input/bybit.jsonl \
  --output data/evidence/alignment/merged.jsonl
```

The stage uses the same recorded-time total ordering as replay and writes an eligibility decision after every event. An opportunity is eligible only when every required venue book is `VALID` and within `max_book_age_ns`. The same policy, exact source event IDs and decision fingerprint are checked again by OMS before routing. Reports retain per-event native-state hashes and deterministic input/output checksums.

## Dump the Binance and Bybit contract master

The default dump is derivatives-first: Binance USD-M futures, COIN-M futures and options; Bybit linear, inverse and options. It uses only public REST endpoints and requires no API keys.

```bash
PYTHONPATH=src python -m crypto_stat_arb.cli contract-master \
  --output-dir data/contract_master
```

Add `--include-spot` to include both exchanges' spot instruments. Strict mode is the default: if any requested source fails, no incomplete CSV is published. Successful runs create a timestamped CSV, `contract_master_binance_bybit_latest.csv`, raw response snapshots, and a checksummed manifest in `data/contract_master/`.

`data/contract_master/contract_master_schema_sample.csv` is a schema/example file only; it is not presented as a live exchange snapshot.

The contract master also contains economic fields for maker/taker fees, maker rebates, delivery fees, funding applicability/interval/current rate/caps/floors/next timestamp, borrow applicability/rate scope, and margin/leverage rules. Public exchange values are populated directly. Account-tier or authenticated values are left blank and tagged with scopes such as `ACCOUNT_SPECIFIC_AUTH_REQUIRED`; the dumper never invents a fee, borrow rate, or margin tier.

## Current safety boundary

This version can receive and persist live **public market data**, but it contains no real exchange credentials and cannot place live orders. The testnet adapter and credential-manager boundaries are exercised only with injected test transports; real authenticated venue connectors remain outside Phase 1.

## Package layout

- `models.py`: canonical market, instrument, opportunity and order schemas.
- `canonical.py`: envelope validation, same-object fan-out and canonical JSONL recorder.
- `adapters.py` / `pipeline.py`: Binance and Bybit depth normalization with native payload retention and checksummed manifests.
- `raw_capture.py`: append-only raw frames, deterministic references, payload checksums and tamper-evident record hash chains.
- `live_feed.py`: concurrent public WebSocket capture, pre-parse wire WAL, rotation, snapshot synchronization, stale/gap reconnect and live book reconstruction.
- `market_data_output.py`: receive-time OHLC resampling, durable CSV output and non-blocking typed UDP multicast publication.
- `orderbook.py`: sequence-aware L2 reconstruction and validity state.
- `book_gate.py`: stale/invalid strategy suspension, cancel-all signalling and controlled resume.
- `execution.py`: common submit/cancel/replace API, adapter protocol, callbacks and strategy-directed routing.
- `plan_store.py`: durable parent/child snapshots, lifecycle audit log, restart recovery and venue reconciliation.
- `time_integrity.py`: deterministic ordering policy, latency/feed-age monitoring and host offset/drift reporting.
- `orchestration.py`: durable jobs, partition checkpoints, retries, late data, backfills and continuous ingestion.
- `resilience.py`: fenced process ownership, heartbeat, reconnect scheduling, replay checkpoints and reconcile-before-resume gating.
- `quality.py`: configurable real-time flags, fail-closed approval reports and hash-chained defect logs.
- `alignment.py`: immutable multi-stream merge, shared signal/routing eligibility and reproducible alignment evidence.
- `strategy.py`: fee-aware cross-venue opportunity detection.
- `risk.py`: package reservation, gross/net exposure limits, utilization, binding events and kill switch.
- `oms.py`: deterministic parent/child execution state machine.
- `ledger.py`: fills, positions and plan audit trail.
- `runner.py`: replay, artifacts and reproducibility manifest.

See `docs/architecture.md` for the Phase 1 design and extension path.
