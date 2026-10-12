# Pipeline monitoring

The platform publishes one monitoring envelope across replay/backtest, batch/backfill orchestration and continuous live feeds. It is designed for machines first: `pipeline_monitoring.json` contains the complete state and configured thresholds, while `pipeline_metrics.prom` exposes numeric gauges in Prometheus text format.

## Coverage

| Area | Published state |
| --- | --- |
| Job | Mode and current `RUNNING`, `CAUGHT_UP`, `COMPLETE`, paused or failed state |
| Data | Event counts, latest receive/persist timestamp and clock definition |
| Partitions | Total, complete, active, rejected, state by partition and byte-level completeness ratio |
| Book integrity | Per-venue state, feed age, sequence, gaps, duplicates, out-of-order and checksum failures |
| Versions | Expected/observed canonical schema, wire schema, adapter versions and deterministic instrument-master version hash |
| Replay | Processing duration and events per second |
| Live | Feed age, reconnects, stale disconnects, synchronous persistence lag and durable-fsync mode |
| Restart resilience | Active fenced owner, heartbeat lease, connector recovery, replay checkpoints and reconcile-before-resume state |
| Strategy | Opportunity count and expected net edge, route decisions and venue distribution |
| Execution | Current order states, fill/reject/cancel outcomes, residual exposure, per-child dispatch/callback latency and gross/fee/net PnL |
| Trading controls | Per-venue rate-limit utilization/headroom, EMS connector admission health, positions, balances, margin and kill-switch state |
| Basic TCA | Per-run and daily venue/instrument/order-type fills, slippage, fees, rejects, residual/unwind, missed opportunities and raw lineage |
| Structured logs | Central JSONL/SQLite connector and paper-host records, correlation-chain search, severity counts and failed-opportunity traces |
| Alert ownership | Incident classification, named owner, severity, response target/expectation, research-hours coverage and forced-test marker |

## Thresholds and health

Thresholds are configured under `monitoring` in `config/research.json`. Every metric publishes its value, unit, comparator, threshold, breach flag and breach severity. The report health is the worst active state:

- `HEALTHY`: no thresholds breached;
- `DEGRADED`: operational capacity or performance warning, such as excess reconnects, replay slowdown or persistence lag;
- `CRITICAL`: stale data, an invalid book, a sequence gap, incomplete partition, stale disconnect or schema mismatch.

The same overall health also becomes `CRITICAL` when an order reject, excess residual, unhealthy connector or active kill switch breaches policy. Dispatch latency and rate-limit utilization are degraded-capacity warnings by default. Trading thresholds are configured beside pipeline thresholds, including `max_order_rejects`, `max_open_residual_base`, `max_dispatch_latency_ms`, `max_rate_limit_utilization`, `max_unhealthy_connectors` and `max_active_kill_switches`.

The live service atomically refreshes both files every `--monitor-interval-seconds` (default one second) and writes a final `COMPLETE` report during graceful shutdown. Temporary files are replaced atomically, so readers never observe partial JSON or metric output.

Replay writes the same files into its run directory. Orchestrated partitions write `<output>.monitoring.json` and `<output>.metrics.prom` alongside the existing partition and quality reports.

## Instrument version

`InstrumentMaster.version_snapshot()` publishes schema version, instrument count and a SHA-256 hash over the normalized sorted instrument rules. An operator can therefore distinguish a feed/schema incident from a run made with a different tick, lot, status, product or leverage definition.

## Prometheus metrics

The exposition includes `crypto_pipeline_monitor_health{level=...}` plus gauges such as:

- `crypto_pipeline_data_age_ns`
- `crypto_pipeline_partition_completeness`
- `crypto_pipeline_sequence_gaps`
- `crypto_pipeline_invalid_books`
- `crypto_pipeline_schema_mismatches`
- `crypto_pipeline_replay_events_per_second`
- `crypto_pipeline_reconnects`
- `crypto_pipeline_stale_disconnects`
- `crypto_pipeline_persistence_lag_ns`
- `crypto_pipeline_order_rejects`
- `crypto_pipeline_open_residual_base`
- `crypto_pipeline_max_dispatch_latency_ns`
- `crypto_pipeline_rate_limit_utilization`
- `crypto_pipeline_unhealthy_connectors`
- `crypto_pipeline_active_kill_switches`
- `crypto_pipeline_active_order_owner`
- `crypto_pipeline_restart_resume_ready`
- `crypto_pipeline_restart_disconnected_connectors`

Paper/backtest sessions additionally publish observed `crypto_strategy_*` gauges for opportunity count, expected edge, route decisions, orders and outcomes, gross/fee/net PnL, positions and each current order state.

The embedded `tca` report also publishes `crypto_tca_*` gauges for detected/executed/missed opportunities, fills, rejects, cancels, unwind orders, residual, weighted slippage, fees and net PnL. The calculation contract and daily-report behavior are documented in `docs/basic_tca.md`.

`structured_logs` reports central record/error/warning and failed-opportunity counts. Corresponding `crypto_structured_logs_*` gauges are emitted to Prometheus. The searchable record contract and query CLI are documented in `docs/structured_logs.md`.

All breached metrics are assigned an accountable alert policy. Failed ingestion, invalid books, replay failure and risk breach have explicit incident classifications; fallback pipeline and execution policies prevent ownerless paper alerts. Prometheus publishes total alerts plus counts by type, owner and severity. The alert matrix and forced-alert procedure are documented in `docs/alert_ownership.md`.

## End-to-end paper evidence

The deterministic paper adapter and production-shaped OMS/EMS interface produce the same monitoring section used by a future live paper connector. `tests/test_monitoring.py::test_paper_simulation_is_monitored_end_to_end` runs the supplied two-venue session through opportunity detection, risk admission, routing, callbacks, fills, reconciliation and account/margin controls. It verifies one opportunity, one route to each venue, two fills, zero residual, inactive kill switch and net PnL of `0.11976`, then verifies both JSON and Prometheus output.

The JSON contract is `docs/schemas/pipeline_monitoring.schema.json`; a representative payload is `data/samples/pipeline_monitoring.json`.
