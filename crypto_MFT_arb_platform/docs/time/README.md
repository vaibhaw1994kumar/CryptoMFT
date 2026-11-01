# Time handling

All canonical timestamps are UTC Unix epoch nanoseconds. The authoritative field meanings and formulas are published in `timestamp_dictionary.json`.

## Ordering and alignment

Replay uses the recorded local receive timestamp as its primary clock. Ties are resolved by exchange timestamp, source timestamp, venue, first and last sequence, then event ID. This is a stable total order and never depends on wall-clock processing speed.

Cross-venue alignment compares the latest local receive timestamps for the same canonical instrument. A configured tolerance produces a report breach; timestamps are never shifted, interpolated or silently repaired. Strategy freshness remains controlled by the book-integrity gate.

## Clock and latency quality

`event_latency_ns = receive_ts_ns - exchange_ts_ns`. Negative latency and latency above the configured impossible threshold are blocking time-integrity failures. Feed age is measured independently as monitor time minus local receive time. A future local timestamp is also blocking; stale feed age is counted and reported.

`HostClockMonitor` accepts NTP, PTP, exchange-server-time or other trusted reference samples. It reports `UNKNOWN` until sampled, `SYNCED` within limits, `DEGRADED` for excessive drift, and `UNSYNCED` for excessive absolute offset. Exchange-message latency is not mislabeled as host-clock offset.

Every replay writes `time_integrity_report.json` with per-venue latency distribution, feed-age maximum, negative/impossible/stale counts, cross-venue skew, host-clock state and the effective policy.
