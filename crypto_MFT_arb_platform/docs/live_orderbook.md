# Continuous public order-book service

## Runtime contract

`live-orderbook` is a public market-data process. It has no credential loading and no order-entry path. It runs one supervised connection per selected venue and maintains the configured canonical instrument in memory while recording replayable partitions on disk.

## Binance synchronization

1. Open the diff-depth WebSocket.
2. Start the REST depth snapshot request without stopping WebSocket receipt.
3. Durably append each received wire frame before parsing.
4. Normalize and buffer WebSocket deltas while no snapshot sequence exists.
5. Persist and apply the REST snapshot.
6. Discard buffered updates at or below `lastUpdateId` and replay the contiguous remainder. A Binance `U…u` range may bridge the next expected sequence.
7. Continue applying live deltas. A gap or invalid book closes the connection and repeats the full procedure.

## Bybit synchronization

1. Open the selected public spot or linear WebSocket.
2. Subscribe to `orderbook.<depth>.<symbol>`.
3. Preserve subscription acknowledgements and heartbeat/control records as raw control frames.
4. Wait for the exchange snapshot, then apply sequential deltas.
5. A gap or invalid book closes the connection; reconnecting produces a new exchange snapshot before the book becomes tradable again.

## Persistence layout

```text
data/live/<session-id>/
  live_manifest.json
  ohlc_1m.csv
  ltp_ohlc_1m.csv
  pipeline_monitoring.json
  pipeline_metrics.prom
  part-000001/
    wire.jsonl
    raw.jsonl
    canonical.jsonl
    trades.jsonl
    trace.jsonl
    quarantine.jsonl
    raw_manifest.json
    live_part_manifest.json
```

The pre-parse wire and parsed raw streams are separately hash-chained. Every successful canonical event has a trace row linking `wire_ref`, `raw_ref` and `canonical_event_id`. Malformed frames stay in `wire.jsonl` and receive a quarantine record even though no parsed raw/canonical record can be created.

## Failure and restart behavior

- Protocol heartbeats detect dead sockets.
- Book receive-time age bounds each read; expiry marks the feed stale and reconnects.
- Each venue independently enters EMS `RISK_REDUCING_ONLY` while its book is stale or invalid. New opportunities and ordinary orders are blocked, ordinary resting orders are cancelled once, and hedge/unwind work remains eligible only against a valid book. A fresh synchronized snapshot restores `ACTIVE` mode.
- A connection is immediately non-tradable and must produce a fresh snapshot within the configurable initial-sync timeout.
- Sequence gaps, crossed/locked books and checksum failures force resynchronization.
- The shared book-integrity gate suspends strategy eligibility and invokes its OMS cancel callback once per safe-to-unsafe transition; it resumes only when all selected books are valid.
- Reconnect delay starts at one second and doubles to a configurable ceiling.
- Each process invocation uses a new immutable session ID. Existing session directories are never overwritten.
- `SIGINT` and `SIGTERM` cancel venue tasks and close all manifests cleanly.

## Operator command

```bash
PYTHONPATH=src python -m crypto_stat_arb.cli live-orderbook \
  --config config/research.json \
  --output-dir data/live \
  --session-id btc-two-venue-001
```

For a short smoke test, add `--max-runtime-seconds 60`. Do not use `--no-fsync` when durable incident replay is required.

Monitoring refreshes atomically once per second by default; change it with `--monitor-interval-seconds`. Feed age, reconnects, stale disconnects, book state, gaps, persistence latency, partition status and schema/instrument versions are published in JSON and Prometheus formats. Thresholds come from the config `monitoring` section.

## One-minute resampling

Each valid reconstructed book event supplies one top-of-book sample. Buckets are aligned to UTC epoch-minute boundaries using `receive_ts_ns`; exchange time is retained in the underlying canonical event but is not used to make the bar available early.

The CSV contains separate Binance and Bybit rows with bid, ask, midpoint and spread OHLC. It also records sample count and first/last canonical event IDs. Invalid/syncing/stale books do not contribute. A normal minute-close row has `is_final=true`; graceful shutdown writes the current incomplete minute as `is_final=false`.

An independent public-trade connection subscribes to Binance `<symbol>@trade` and Bybit `publicTrade.<symbol>`. Each frame is preserved raw-first and each normalized tick is written to `trades.jsonl`. `ltp_ohlc_1m.csv` uses only these actual trade prices: the first tick is open, maximum is high, minimum is low and final tick is close. It also includes trade count and exact summed base and quote volume. Receive time assigns the epoch-minute window; native exchange time is retained on the tick.

## UDP multicast fan-out

The default destination is `239.255.77.77:19001` with TTL 1. The publisher is non-blocking and emits compact schema-versioned JSON datagrams. `publisher_sequence` is global across `ORDERBOOK`, `OHLC_1M`, `TRADE` and `LTP_OHLC_1M` messages so every consumer can detect loss or reordering.

Every consumer must bind the port with address reuse and join the multicast group. The provided `examples/udp_multicast_consumer.py` does this and can be run simultaneously in multiple processes. UDP does not provide retransmission; use the session files to recover missed data.
