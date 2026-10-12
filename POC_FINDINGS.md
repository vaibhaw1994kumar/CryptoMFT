# POC Findings

## Outcome

The PoC demonstrates a single Binance Spot strategy running through the same
normalization, L2 book, risk, OMS, paper exchange, portfolio, and reporting
components in deterministic replay and live-paper modes. It never submits real
exchange orders.

## What the PoC proves

- Binance depth snapshots and incremental updates can reconstruct a
  sequence-valid `BTCUSDT` L2 order book.
- Raw recordings can be replayed deterministically with stable book and result
  hashes.
- Timestamp/skew, sequence, price, trade quantity, depth, spread, and local
  checksum anomalies are detected before strategy processing.
- Rejected recording events are preserved in `quarantine.jsonl` with structured
  anomaly reasons, while accepted events remain in the analytical dataset.
- Strategy intents pass through explicit risk decisions and OMS transitions.
- The paper exchange supports post-only checks, marketable and resting orders,
  partial fills, fees, acknowledgement latency, and cancellation latency.
- Stale data, invalid books, sequence gaps, position limits, and a manual kill
  switch have explicit safe behavior.
- Run reports provide traceable opportunities, orders, updates, fills,
  positions, PnL, execution rates, latency measurements, and configuration
  hashes.

## Observed measurements

Measurements vary with network and market activity. The acceptance workflow
records them again on every run rather than treating these values as permanent
benchmarks.

- Live Binance connection and initial book synchronization completed within
  seconds in local testing.
- Repeated replay of the same capture produced identical result and order-book
  hashes.
- The automated unit and integration suite completes in a few seconds on the
  development machine.
- Max-speed replay processes small PoC captures effectively instantaneously;
  normal-speed replay intentionally follows recorded receive-time spacing.

## Known limitations

- The execution simulator has no calibrated queue-position model.
- Full order-book depth is available, but the strategy uses only top-of-book
  prices.
- Fill behavior is deterministic and respects aggressor direction and trade
  quantity, but it is not a production-quality exchange matching model.
- Tardis access outside its free sample dates may require an API key.
- HftBacktest is integrated as the path for exporting/calibrating historical
  events; model calibration remains a follow-on research activity.
- `level2-order-book` is not used as a runtime dependency because its data model
  is not Binance-specific. OrderBook-rs remains an optional future native
  adapter because it does not provide an official Python API.
- The service is single-process and maintains state in memory.
- Binance Spot Testnet signed REST execution is available for market, limit,
  post-only, cancel, query, and reconciliation workflows. Production/live
  Binance trading remains disabled.
- A modern authenticated Binance user-data WebSocket stream is not yet
  included; reconciliation currently uses signed REST account, open-order,
  order-query, and recent-trade endpoints.
- Binance Spot public depth messages do not provide an exchange checksum, so
  the PoC verifies deterministic local checksums and sequence consistency
  rather than comparing against a venue-supplied checksum.
- External notification delivery such as PagerDuty or Slack is intentionally
  deferred; local structured logs, Prometheus metrics, anomaly reports, and
  a thread-safe local alert queue is available.

## Production recommendations

1. Add authenticated exchange gateways only after testnet reconciliation and
   ambiguous-submission recovery are proven.
2. Calibrate latency and fill models against live observations using
   HftBacktest and out-of-sample validation.
3. Add durable checkpoints and restart reconciliation before using capital.
4. Move mandatory event persistence to managed durable storage with explicit
   backpressure and failure policies.
5. Add authentication and authorization before exposing operational controls
   outside localhost.
6. Keep real-money activation gated by independent risk, security, and
   operational review.
