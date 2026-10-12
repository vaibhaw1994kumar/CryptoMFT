# Alert ownership

Every pipeline and paper-trading alert is enriched in the common monitoring envelope with a named owner, severity, response target, response expectation and coverage model. The initial release assumes research-hours coverage; it does not claim a 24/7 response service.

## Alert matrix

| Alert type | Default owner | Severity | Target | Required response |
| --- | --- | --- | ---: | --- |
| Failed ingestion | `market-data-owner` | Critical | 30 min | Stop dependent paper strategies, preserve the failed partition, diagnose and restart/backfill |
| Invalid book | `market-data-owner` | Critical | 15 min | Verify strategy gating and ordinary-order cancellation, then force clean snapshot resynchronization |
| Replay failure | `research-infrastructure-owner` | High | 240 min | Quarantine the run, retain checkpoint/raw lineage and prove deterministic replay before resume |
| Risk breach | `paper-risk-owner` | Critical | 15 min | Verify kill state, cancel new risk, allow only approved risk reduction and reconcile before resume |
| Other pipeline threshold | `research-infrastructure-owner` | Warning | 240 min | Investigate and record disposition before the next paper session |
| Other execution threshold | `paper-trading-owner` | High | 30 min | Inspect order/residual/connector state and suspend new risk when safety is uncertain |

`PipelineMonitor` generates explicit failed-ingestion or replay-failure incidents when the job enters `FAILED`. Invalid-book metrics are classified under the market-data owner. An active kill switch or excess open residual is classified as a risk breach. Every other threshold uses the pipeline or execution fallback, ensuring no paper alert is ownerless.

## Forced-alert control test

Pass one or more values in `forced_alert_types` to `PipelineMonitor.build()` to exercise the complete alert-rendering path without manufacturing corrupt market data. Supported values are `FAILED_INGESTION`, `INVALID_BOOK`, `REPLAY_FAILURE`, `RISK_BREACH`, `PIPELINE_THRESHOLD` and `EXECUTION_THRESHOLD`.

Forced rows use `scope=CONTROL_TEST` and `forced_test=true`, affect overall health, appear in `pipeline_monitoring.json`, and are counted in `crypto_pipeline_alerts` and `crypto_pipeline_alerts_by_owner`. The automated test forces all four required incident types and verifies owner, severity, target and expected response. A representative passing result is in `data/samples/alerts/forced_alerts.json`.

The contracts are `docs/schemas/alert_ownership.schema.json` and the alert section of `docs/schemas/pipeline_monitoring.schema.json`.
