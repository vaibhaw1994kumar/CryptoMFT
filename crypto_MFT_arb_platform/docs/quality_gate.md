# Data-quality gate

## Approval contract

The quality gate is fail-closed. A dataset is `APPROVED` only when every configured threshold passes. Any active flag makes the decision `REJECTED`; `DEGRADED` versus `CRITICAL` communicates operational severity and does not weaken approval criteria.

| Metric | Default maximum | Health severity |
| --- | ---: | --- |
| Sequence gaps | 0 | Critical |
| Stale intervals | 0 | Critical |
| Crossed/locked books | 0 | Critical |
| Duplicate events | 0 | Degraded |
| Out-of-order events | 0 | Critical |
| Schema exceptions | 0 | Critical |
| Rejected partitions | 0 | Critical |
| Negative latency | 0 | Critical |
| Impossible latency | 0 | Critical |
| Unknown instrument rules | 0 | Critical |
| Persistence lag | 5 seconds | Degraded |
| Unpersisted bytes | 0 | Degraded |

The minimum event count defaults to one. All thresholds are explicit in configuration and embedded in every report.

## Real-time flags

`QualityGate.evaluate` accepts a current immutable metric snapshot and immediately returns active `HealthFlag` records containing code, level, metric, observed value, threshold, scope and message. Repeated evaluation updates current health while de-duplicating defects by flag, scope and correlation ID.

## Report and defect evidence

The JSON approval report contains counts, thresholds, active flags, limitations, decision, health, defect count, terminal defect hash, defect-log checksum and approval attestation SHA-256. The JSONL defect log is chained using `previous_defect_hash` and `defect_hash` so removal or reordering is detectable.

Backtest reports combine book-integrity and time-integrity metrics. Pipeline reports add schema exceptions, rejected records/partitions, persistence lag and unpersisted bytes. Book-level metrics for a raw partition are declared as a limitation until that canonical partition is replayed.
