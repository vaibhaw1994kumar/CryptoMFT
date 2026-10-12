# Pipeline orchestration

## Commit and restart contract

SQLite is the authoritative commit log. For each raw line, the idempotency key, canonical event, source line number and next byte offset commit in one transaction. The key is `raw_ref` when available and the raw-line SHA-256 otherwise. A restart seeks to the durable byte offset. If a process stops after the database commit but before output publication, the next run rematerializes JSONL from committed rows; it neither repeats nor omits the record.

Source truncation below a checkpoint is a hard failure and is never silently rewound. Raw hash-chain verification remains the mechanism for detecting mutation of already committed source content.

## Partition lifecycle

| State | Meaning |
| --- | --- |
| `PENDING` | Registered but not started |
| `RUNNING` | Batch attempt is processing |
| `PAUSED` | Deliberately interrupted at a committed checkpoint |
| `RETRY_WAIT` | Transient failure; failing input was not checkpointed |
| `CAUGHT_UP` | Continuous reader persisted all bytes visible at the poll |
| `LATE_REOPENED` | A completed partition grew and is processing appended data |
| `COMPLETE` | End of file reached with no rejected records |
| `COMPLETE_WITH_REJECTIONS` | End reached, but the quarantine is non-empty |
| `FAILED` | Retry budget exhausted or source integrity violated |

## Backfill, retry and late data

`run_backfill` processes partition specifications in deterministic partition-ID order and publishes job/partition completion state. Only `RetryablePipelineError` consumes the bounded retry budget; parsing, schema and mapping failures are permanent rejections and advance to quarantine so one poison record cannot stall a partition. Appended bytes reopen a completed partition and increment `late_records`; already committed idempotency keys remain no-ops.

## Continuous ingestion and lag

`run_continuous` repeatedly polls a growing raw JSONL partition and resumes from SQLite after restart. `unpersisted_bytes` measures source bytes visible at the poll but beyond the checkpoint. `persistence_lag_ns` is monitor time minus the latest committed receive timestamp. These fields are available through the API and the output `.partition.json` sidecar.

Every partition update also writes `<output>.monitoring.json` and `<output>.metrics.prom`. The unified report includes job and partition state, byte-level completeness, committed/rejected counts, latest receive time, persistence lag, canonical schema version and a deterministic instrument-master version hash. Thresholds and alerts use the same contract as replay and live market-data monitoring; see `docs/pipeline_monitoring.md`.
