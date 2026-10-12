# Phase 1 build status

## Execution realism, live paper, calibration and testnet (2026-09-13)

See `docs/execution_realism.md` and `CHANGES_FROM_UNIFIED.md`.

- Shared `TradingSession` for backtest, live paper and testnet, with a fail-closed execution-mode label.
- Per-venue, per-stage seeded latency model. Orders execute against the book at arrival, and reports return after the ack or fill latency.
- Liquidity consumption, explicit IOC remainder cancel and resting-order fills.
- Unknown order state is resolved by venue query (never resent). No hedge or close happens while a child is in flight, unknown or unconfirmed. Residual breaches hold new risk and escalate to the kill switch.
- Book price-jump, wide-spread and malformed-level checks, trade-print validation and new quality-gate counters.
- Host-offset correction for negative receive latency. On a 60 s real Binance/Bybit session, 18 of 1,614 events were blocked (warm-up only); the estimated offset was −256 ms.
- Effective-dated instruments and fees, public instrument-status poller, and versioned point-in-time features with a lookahead guard (SDK 1.2.0).
- Commands: `live-paper`, `compare-runs`, `calibrate-latency`, `benchmark` and `instrument-status`.
- Binance Spot and Bybit v5 testnet transports, verified against recorded HTTP only.
- 183 tests pass. `scripts/run_all_local.py` passes offline (benchmark decision `PASS`).

## Strategy SDK 1.1 advanced backtesting

- Integrated the supplied `new_backtest.zip` engine under
  `crypto_stat_arb.advanced_backtest`.
- Added `AdvancedBacktestSpec`, offline-only `BacktestAPI`,
  `StrategyContext.backtest`, and the `sdk-backtest` CLI.
- Added measured/modelled cross-venue latency, receive-time replay, queue-aware
  maker fills, taker depth walking, liquidity consumption, empirical fill
  calibration, toxicity/freshness/depth/inventory gates, leg-risk unwind,
  markouts, edge capture, and batch/two-pass support.
- SDK-owned cleanup prevents the imported process-global virtual clock from
  leaking into later paper or testnet work.
- Three focused SDK acceptance tests added; all 157 platform tests pass.

## Local all-function runner

- Added `scripts/run_all_local.py` with PowerShell and Bash wrappers.
- Default execution is credential-free and covers all 154 automated controls,
  raw capture/verification, normalization, checkpointed orchestration,
  cross-venue alignment, the paper OMS/RMS session, Strategy SDK, structured
  log query, and paper-release create/approve/deploy/verify/reproduce.
- `--public-network --live-seconds N` additionally runs the public contract
  master and a bounded Binance/Bybit WebSocket, OHLC, monitoring, persistence,
  and UDP check.
- The complete local runner passed on 2026-09-13 with one closed plan, zero
  residual, healthy monitoring, approved reconciliation, and `0.11976` net PnL.
- Added `docs/LOCAL_RUNBOOK.md` with Windows, macOS, and Linux setup and service
  commands plus a compact end-to-end architecture diagram.

## Working vertical slice

- Binance and Bybit L2 raw-message normalization into one canonical event model.
- Native payload retention, source/output checksums and normalization manifest.
- Stable multi-venue replay ordering and deterministic event identifiers.
- Sequence-aware books with duplicate suppression, gap detection, stale state and crossed-book detection.
- Effective configuration for venue symbols, tick/quantity steps, minimums and taker fees.
- Fee-aware cross-venue opportunity detection with book-freshness and executable-depth checks.
- Package-level risk reservation before child dispatch and a strategy kill-switch control.
- Idempotent parent/child OMS, deterministic depth fills, fill ledger, positions and opportunity PnL.
- Run summary and manifest with exact input hashes and deterministic result hash.
- Passive Binance/Bybit contract-master dumper with normalized CSV, raw-response retention, pagination and strict completeness controls.
- Contract economics enrichment for public funding state/rules, option and delivery fees, borrow applicability, leverage attributes and explicit account-specific data scopes.
- Append-only raw frame preservation with payload checksums, record hash chaining, session manifests, quarantine output and raw-to-canonical trace evidence.
- Published canonical event envelope v1.2 with source/exchange/local timestamps, sequence, quality flags, optional venue checksum and raw reference.
- Validated same-object delivery to the strategy callback and canonical event recorder.
- Snapshot/delta buffering and reconciliation with duplicate, out-of-order, controlled-gap, checksum and stale-state handling.
- Automatic strategy gating with one-shot cancel-all signalling and controlled resume after fresh valid books.
- Deterministic recorded-time replay ordering, cross-venue receive-time alignment and impossible/negative latency blocking.
- Host-clock offset/drift monitor, feed-age metrics, timestamp dictionary and per-run time-integrity report.
- SQLite-backed idempotent orchestration with transactional checkpoints, bounded retry, deterministic backfill and late-data reopening.
- Restartable continuous ingestion with atomic JSONL publication, partition completion sidecars, unpersisted-byte and persistence-lag monitoring.
- Fail-closed data-quality approval with configurable real-time health flags and consolidated book/time/pipeline metrics.
- Attested approval reports, declared limitations and hash-chained correlation-level defect logs.
- Immutable deterministic cross-venue merge with native-state hashes and per-event eligibility traces.
- Shared validity/age/source-state fingerprint enforced before strategy opportunity creation and OMS routing.
- Concurrent Binance and Bybit public WebSocket collection with graceful shutdown and bounded reconnect backoff.
- Pre-parse wire persistence, wire-to-raw-to-canonical lineage, hourly/size rotation and per-part/session manifests.
- Live Binance REST snapshot plus buffered WebSocket delta reconciliation and Bybit snapshot subscription/recovery.
- Automatic reconnect/resynchronization after stale feeds, sequence gaps, crossed books or checksum failures.
- Per-venue UTC one-minute bid/ask/mid/spread OHLC resampling with durable CSV output and partial-bar shutdown semantics.
- Non-blocking schema-versioned UDP multicast for top-N reconstructed books and OHLC bars, with loss-detection sequence numbers and a multi-consumer example.
- Validated OMS/EMS parent state machine with explicit acknowledgement and timeout states, deterministic hedge/unwind, reconciliation and `CLOSED` as the sole immutable terminal state.
- One durable canonical order-update reducer shared by adapter callbacks, private streams, REST queries and timers, with restart-safe deduplication and deterministic terminal-conflict resolution.
- Explicit per-venue EMS rate-limit policies for reject, bounded FIFO queue and deterministic delay, with usage/headroom/release telemetry and OMS package-risk retention until throttled children complete.
- Configurable residual size/time limits, hedge-venue priority and unwind urgency with persistent reduce-only unwind children, controlled risk-reducing continuation under strategy stop and fail-closed residual enforcement.
- Correlation-complete order/fill ledger with idempotent cumulative-fill accounting, testnet REST/private-stream reconciliation and fail-closed RMS checks for order, fill, position, balance-delta and control consistency.
- Ledger-derived average-cost positions, realized/unrealized PnL, cash/equity and available/reserved capacity with restart rehydration and end-of-run account-state publication.
- Venue-scoped trade-break controls for missing/extra/late fills, conflicting order states and incomplete opportunities, with automatic clean-reconciliation recovery.
- Pre-trade OMS/RMS matrix covering instrument, order, price-band, live-data, venue/account/strategy and paper-capacity limits before any child dispatch.
- Ledger-derived strategy gross/net and per-instrument/per-venue gross exposure controls, with key-specific overrides, utilization telemetry, binding events and durable breach/recovery transitions.
- Per-child legging controls for one-sided marked notional, residual size and duration, with explicit hedge/unwind decisions and audited global/venue/rate-limit overrides for risk-reducing legs.
- Venue-scoped stale/invalid-feed controls that block opportunities and ordinary routing, cancel ordinary open orders once, preserve risk-reducing work and automatically recover on a fresh valid snapshot.
- Audited strategy kill-switch activation, adapter/EMS-queue cancel-all, configurable risk-reducing continuation, PM status artifact and reconciliation-gated recovery.
- Derivative-aware margin/collateral monitoring with initial and maintenance requirements, configurable liquidation buffer, conservative venue-reported state, pre-breach alerts, fail-closed breach handling and margin-state reconciliation.
- Unified pipeline monitoring for replay, batch/backfill and live sessions, with job/freshness/partition/book/version/performance metrics, configurable thresholds, atomic JSON status and Prometheus exposition.
- Correlated strategy/execution monitoring for opportunities and expected edge, venue routes, order/fill outcomes, residuals, child latency, PnL, rate-limit headroom, connector health, account/margin state and kill-switch state in that same envelope.
- Reproducible Basic TCA by venue/instrument/order type for fills, signed slippage, fees, rejects, residual/unwind and missed opportunities, with automatic UTC daily paper reports and canonical-to-raw lineage.
- Central structured connector and paper-host logs in JSONL plus indexed SQLite, with full source/feature/opportunity/risk/parent/child correlation, query CLI, monitoring gauges and automatic failed-opportunity traces.
- Owned alert policies for failed ingestion, invalid books, replay failures, risk breaches and fallback paper thresholds, with severity, response expectations, research-hours coverage, Prometheus labels and a forced-alert control test.
- Versioned Python Strategy SDK for canonical data/replay, source-linked features, ledger portfolio state, RMS and OMS/EMS, with an unchanged cross-venue strategy across deterministic paper and injected testnet adapters.
- Content-addressed paper-release registry covering strategy/features/models/parameters/data/simulator versions, build/environment and named approvals, with tamper-proof activation audit plus verified reproduction and rollback.
- Minimal credential security with credential-free offline backtests, plaintext-config rejection, external secret-manager boundary, least-privilege venue/environment scopes, no withdrawal permission, ephemeral injection, rotation/revocation and secret-free hash-chained audit.
- Restart-resilient paper control with durable heartbeat leases, monotonic fencing tokens, bounded auto-reconnect, exact replay checkpoints, nonterminal parent recovery, single active order ownership and mandatory reconcile-before-resume.

## Verification completed

- 154 unit and end-to-end tests pass, including restart-state publication through the shared pipeline monitor.
- Same inputs produce byte-identical event ledgers.
- The included two-venue example produces one closed opportunity with zero residual exposure.
- Gross PnL: 0.60000 USDT; fees: 0.48024 USDT; net PnL: 0.11976 USDT.
- Normal sample has zero sequence gaps and zero invalid books.

## Next milestone: paper-ready adapter layer

1. Run long `live-paper` sessions and `compare-runs` until the forward sample exceeds 30 opportunities; calibrate order stages from a `VENUE_TESTNET` run.
2. Add external Telegram/webhook delivery and richer implementation-shortfall benchmarks on top of the owned alert matrix, daily Basic TCA, Prometheus strategy/execution monitoring and paper-session reconciliation.
3. Add live-session quality approval and network fault injection.
4. Exercise the testnet transports against the real Binance/Bybit testnets with testnet keys in the secret manager.

No production order path exists. Testnet order transports accept only the Binance/Bybit testnet hosts and secrets injected by the credential broker; the CLI never accepts credentials.
