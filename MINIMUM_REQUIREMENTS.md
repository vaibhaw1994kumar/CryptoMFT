# Minimum Requirements: MFT Stat Arb Backtesting and Paper Trading

This document is the Section 10 developer deliverable for the *Minimal
Requirements* specification (document version 2.1). It describes the
implementation that lives in `src/trading_platform_poc/minreq/` and maps every
requirement MR-01 to MR-14 and every acceptance test AT-01 to AT-14 to the code
and the command that demonstrates it.

The acceptance suite runs offline: a deterministic multi-venue generator stands
in for the public feeds so the whole stack is reproducible on any machine. The
same stack also runs on live public WebSocket feeds (`mr-record`, `mr-paper
--feed live`) and can route to venue testnets (`--execution-mode
venue_testnet`); those paths need network access and, for testnets,
credentials in the process environment.

```bash
uv run trading-platform-poc mr-acceptance --output <workspace>
```

A scripted 15-minute walkthrough of the whole stack, with the expected output
of every command, is in [MR_DEMO_GUIDE.md](MR_DEMO_GUIDE.md).

---

## 1. Requirement map

| ID | Requirement | Module | Command |
| --- | --- | --- | --- |
| MR-01 | Multi-venue public capture and raw retention | `venues.py`, `ingest.py` | `mr-generate`, `mr-record` |
| MR-02 | Canonical schemas and versioning | `canonical.py` | `mr-schema` |
| MR-03 | Security master and effective-dated rules | `securitymaster.py` | `mr-instruments` |
| MR-04 | Ingestion, manifests and quality reports | `pipeline.py`, `quality.py`, `checksums.py` | `mr-generate`, `mr-record`, `mr-verify`, `mr-quality` |
| MR-05 | Deterministic multi-venue replay | `replay.py`, `latency.py`, `backtest.py` | `mr-backtest` |
| MR-06 | Cross-venue features without lookahead | `features.py` | `mr-schema` |
| MR-07 | Paper matching and order lifecycle | `matching.py`, `router.py`, `live.py`, `testnet.py` | `mr-paper` |
| MR-08 | OMS/EMS routing and residual handling | `router.py` | `mr-backtest`, `mr-paper` |
| MR-09 | Fees, economics and cost model | `fees.py` | `mr-fees` |
| MR-10 | Risk controls and kill switch | `riskcontrols.py` | `mr-schema`, `mr-backtest` |
| MR-11 | Append-only ledger and reconciliation | `ledger.py` | `mr-reconcile` |
| MR-12 | Monitoring and TCA | `monitoring.py`, `tca.py` | `mr-tca` |
| MR-13 | Reproducible runs from a manifest | `manifest.py` | `mr-backtest` |
| MR-14 | Performance benchmark | `benchmark.py` | `mr-benchmark` |

All modules live in `src/trading_platform_poc/minreq/`. Time integrity reuses
`crypto_stat_arb.time_integrity`; latency presets, host-clock alignment and
markouts reuse `new_backtest.latency_model` (see [INTEGRATION.md](INTEGRATION.md)).

---

## 2. Venue, channel, instrument and order-capability matrix

Emitted as data by `mr-schema` and written to
`venue_capability_matrix.json` by `mr-deliverables`.

| Venue | Depth channel | Trade channel | Instruments | Order types | Time in force | Depth | Orders/s |
| --- | --- | --- | --- | --- | --- | --- | --- |
| binance | `{symbol_lower}@depth@100ms` | `{symbol_lower}@trade` | `/api/v3/exchangeInfo` | market, limit, post_only | gtc, ioc, fok | 50 | 20 |
| okx | `books` | `trades` | `/api/v5/public/instruments` | market, limit, post_only | gtc, ioc, fok | 50 | 20 |
| bybit | `orderbook.50.{symbol_upper}` | `publicTrade.{symbol_upper}` | `/v5/market/instruments-info` | market, limit, post_only | gtc, ioc, fok | 50 | 20 |

The three adapters differ in exactly the ways that matter to a sequence-gap
detector, which is why all three are implemented rather than one:

* **binance** uses `U`/`u` first-and-final update identifiers per delta;
* **okx** uses `seqId`/`prevSeqId` back-links and a per-message checksum;
* **bybit** uses a monotonic `u` counter with an explicit `snapshot`/`delta`
  discriminator.

The instrument universe is BTC-USDT and ETH-USDT spot on all three venues, in
two economic groups, `BTC/USDT.SPOT` and `ETH/USDT.SPOT`.

## 3. Canonical schemas and versioning rules

`mr-schema` prints the whole machine-readable schema. The envelope carries
`schema_version`, `event_id`, `event_type`, venue, instrument, three clocks
(`exchange_timestamp_ns`, `receive_timestamp_ns`, `ingest_timestamp_ns`), the
venue sequence, a `RawReference` back to the exact raw record, a `Correlation`
block and the quality flags.

Twenty-one event types cover the whole life of an opportunity:
`book_snapshot`, `book_delta`, `trade`, `ticker`, `instrument_status`,
`funding`, `data_quality`, `control`, `feature`, `signal`, `risk_decision`,
`order_submit`, `order_ack`, `order_reject`, `order_cancel`, `order_timeout`,
`fill`, `position`, `balance`, `plan_state` and `accrual`.

Versioning rules (also published in the schema document):

1. `schema_version` is one integer for the envelope, stamped on every event. A
   reader refuses an unknown version instead of guessing.
2. Adding an optional payload field, an event type or a quality flag is
   backwards compatible and does not bump the version.
3. Removing or renaming a field, changing a unit or changing the ordering key
   bumps the version.
4. Prices, quantities and money travel as decimal strings, never floats.
5. Every event carries `adapter_version`, and where relevant the instrument
   and fee record versions in force, so a replay resolves the same reference
   data the live path resolved.
6. `event_id` is a uuid5 over venue, channel, sequence and payload digest, so
   re-ingesting the same bytes collapses instead of double-counting.
7. Datasets are immutable. A correction is a new dataset with a new checksum.

The ordering key is `(receive_timestamp_ns, venue, sequence, event_id)`, which
is total and stable, so a merge of several venues is byte-for-byte repeatable.

## 4. Security master and effective-dated economics

`mr-instruments --as-of-ns <t>` resolves the master at a point in time.
Records are effective-dated and versioned; `resolve()` returns the version in
force at `t` and never the newest one.

Each record carries the venue symbol, economic group, product type, base,
quote and settlement currencies, tick size, quantity step, minimum quantity,
minimum notional, quantity unit, contract multiplier, inverse flag, price band,
supported order types and time-in-force, and lifecycle status.

`size_order()` is the single place where an order size is made legal. It
rounds to the quantity step, enforces the minimum quantity and minimum
notional, applies the price band, and refuses instruments that are not in
`trading` status, returning a machine-readable reason each time.

`mr-fees --as-of-ns <t> --venue <v>` resolves the fee schedule the same way.
The default book deliberately contains a mid-run binance version bump (1.0 bps
taker before 2026-01-01, 0.9 bps after) so that an effective-dated lookup is
observable rather than theoretical. `CostModel` is the only component allowed
to do fee arithmetic; the strategy, the router and the TCA report all call it.

## 5. Ingestion, backfill, manifests and quality

`mr-generate --output <dir> [--steps N] [--seed S] [--venues ...]` captures raw
venue payloads from the deterministic generator; `mr-record --output <dir>
[--venues ...] [--symbol S] [--duration SECONDS]` captures them from live
public WebSocket feeds (`ingest.LiveCapture`, built on `venues.PublicFeed`).
Both persist raw records under `raw/<venue>/<date>/<channel>.jsonl`, normalize
them into canonical events under `canonical/`, run the quality monitor and
write `dataset_manifest.json`, so a live capture can be replayed with
`mr-backtest --dataset`.

Live synchronisation: Binance diff-depth updates arrive before the REST
snapshot that anchors them, so updates that precede a book's first snapshot are
ignored without being counted as gaps; OKX and Bybit send a snapshot first. A
dropped connection reconnects with bounded exponential backoff and the gap it
leaves is flagged, not hidden. Receive timestamps from all venues come from one
monotonic wall clock.

The manifest records the dataset id, adapter and schema versions, the venue and
instrument set, raw and canonical counts, per-partition state, the quality
report, and a `checksum` over the content. The checksum deliberately excludes
`created_at` and `duplicates_skipped` because those are operational, not
content: a dataset rebuilt after an interrupted capture has the same checksum
as one built in a single pass.

A raw frame that cannot be normalized without loss (for example a non-numeric
price) is never dropped and never aborts the dataset: it is kept in
`quarantine/quarantine.jsonl` with its raw digest and error, and graded as a
`schema_exception`.

Fault injection is first class, which is what makes the quality report
testable: `--sequence-gap-at`, `--stale-gap-at`, `--crossed-book-at` and
`--duplicate-at` on the command line, plus `checksum_mismatch_at`,
`price_jump_at`, `invalid_trade_at`, `schema_exception_at` and
`silent_venue_after` in `FaultInjection`.

Eighteen quality flags exist: `ok`, `sequence_gap`, `duplicate`,
`out_of_order`, `stale`, `crossed_book`, `empty_book`, `schema_exception`,
`negative_latency`, `impossible_latency`, `clock_regression`,
`snapshot_resync`, `checksum_mismatch`, `price_jump`, `wide_spread`,
`trade_outside_book`, `synthetic` and `instrument_unknown`. What raises them:

| Check | Behaviour |
| --- | --- |
| Sequencing | Gaps invalidate the book until a snapshot resync. A redelivery of an update already applied is `duplicate`; an older update never applied is `out_of_order`. |
| Book validity | `crossed_book` and `empty_book` invalidate the book. |
| Venue checksum | When a venue publishes a book checksum it is verified on every message (`checksums.py` implements OKX's CRC32 over the top 25 levels) and a mismatch invalidates the book until resync. In a live capture OKX's public `books` channel sent `checksum: 0` on every message, which is treated as not published; Binance spot and Bybit spot publish none. For those feeds integrity rests on sequence continuity (`U`/`u`, `seqId`/`prevSeqId`, `u`), and nothing more is claimed. |
| Economic sanity | Non-positive prices or negative sizes reject the update (`schema_exception`). A mid that moves more than `max_price_jump_bps` (500) in one update raises `price_jump`, and a spread wider than `max_spread_bps` (500) raises `wide_spread`; both mark the book invalid for that update. |
| Trades | Non-positive price or size, or an unknown aggressor side, is a `schema_exception`; a print more than `max_trade_deviation_bps` (300) from a valid mid is `trade_outside_book`. Flagged trades are ignored by the simulator. |
| Staleness | Every event's receive time is a clock for every book, so a feed that goes silent is flagged while the other venues keep trading, not only when it comes back. Streams still silent at the end of the data fail approval. The stale ratio is measured in time as well as in events. |
| Time integrity | `crypto_stat_arb`'s `TimeIntegrityMonitor` grades negative and impossible receive latency and cross-venue receive skew; `new_backtest`'s `HostClockAligner` estimates the host-clock offset. Both are embedded in the report under `time_integrity`, with wall-clock fields removed so the checksum stays reproducible. |
| Provenance | Every generator record is tagged `synthetic`; provenance is reported separately and is not an anomaly. |

A dataset is *approved* only when every threshold in `QualityThresholds` holds:
zero sequence gaps, crossed books, schema exceptions, checksum mismatches,
price jumps, invalid trades, impossible latencies and silent streams; and
stale (event and time), duplicate and out-of-order ratios at or below 5 %,
wide-spread ratio at or below 1 % and negative-latency ratio at or below 1 %.
`mr-quality` prints the verdict and `mr-verify` re-hashes the stored files and
re-checks the manifest.

Backfill is idempotent. Re-running an ingestion over the same raw records
re-derives identical `event_id`s, so the second pass reports
`duplicates_skipped` and changes nothing.

## 6. Replay and simulator configuration

`MultiVenueReplay` merges per-venue streams on the ordering key and refuses to
move backwards in receive time; `assert_no_forward_leakage()` is asserted in
the acceptance suite. Control events (`ControlSchedule`) are injected into the
same ordered stream, so a feed stall, a venue suspension, a rate-limit bind or
a kill switch arrives exactly where it would have arrived live.

`TradingSession` (in `backtest.py`) consumes that stream one event at a time
and advances execution time before applying each event, so the backtest and
the live paper runner (`live.py`) drive exactly the same object graph.

**Orders are executed when they arrive, not when they are decided.** A child
order is sent, spends a sampled `entry` latency in flight, and executes
against the book as it is at that instant; the acknowledgement and the fill
report then travel back with their own latency. An edge that disappears in
flight is therefore not there to fill, and every fill records the price the
router expected, the price it got and the resulting latency slippage.

The default simulator models, per venue and per stage:

| Stage | Base | Jitter | Tail probability | Tail |
| --- | --- | --- | --- | --- |
| market data | 1 ms | 1 ms | 1% | 40 ms |
| strategy | 200 µs | 100 µs | — | — |
| risk | 100 µs | 50 µs | — | — |
| entry (send to matching engine) | 12 ms | 3 ms | 2% | 250 ms |
| submit ack (matching engine to us) | 13 ms | 2 ms | — | — |
| cancel | 25 ms | 5 ms | 2% | 250 ms |
| fill report | 5 ms | 3 ms | 1% | 120 ms |

`--latency-preset singapore|tokyo|neutral|colocated_both` replaces those with
`new_backtest`'s per-venue lognormal co-location profiles (Bybit and Binance);
a venue a preset does not describe, such as OKX, keeps the default profile and
the manifest records which profile each venue used.

Liquidity is consumed, not copied. Quantity a taker order takes is withheld
from that price level until the venue's own book update shows the level has
shrunk (or a snapshot replaces the book), so the same liquidity can never be
sold twice. Route scoring reads this remaining depth.

Maker orders rest with a queue position equal to the visible size at their
price when they arrive. Printed trades at or through the price consume the
queue ahead first, and cancellations ahead are inferred pro rata from level
shrinkage that trades do not explain (the queue model from
`sim_engine/dry_run_core.py`, rewritten on the canonical book).

Fees come from `CostModel`. Rate limits, venue outages, post-only rejections,
fill-or-kill failures, duplicate client order identifiers and exhausted books
all produce explicit reject reasons: `rate_limit`, `no_liquidity`,
`fok_unfillable`, `post_only_would_cross`, `book_unavailable`,
`duplicate_client_order_id`, `venue_unavailable`, `unknown_order`.

Each run writes `execution_quality.json`: per-fill expected price, fill price
and latency slippage, 1 s, 5 s and 30 s markouts (`new_backtest`'s
`MarkoutTracker`), residual breaches and unresolved orders.

## 7. Features without lookahead

`CrossVenueFeatureEngine` computes the cross-venue basis, its rolling mean and
standard deviation, a z-score, per-venue microprice and imbalance, and the
staleness of each venue's book. Every feature is versioned, and
`feature_set_hash()` changes whenever a definition changes.

The engine holds an explicit as-of clock. Asking it for a snapshot at a
timestamp earlier than the last observed event raises `LookaheadError` rather
than silently answering, so a lookahead bug fails the run instead of improving
the backtest.

## 8. OMS/EMS routing, state machine and residual handling

`OrderRouter.submit_opportunity(intent)` is the public API, and
`OrderRouter.advance(now_ns)` moves execution time. Routes are scored on
expected value: net edge after fees, measured slippage on the *remaining*
depth and a latency penalty, weighted by the probability that both legs still
fill on arrival (depth coverage decayed over the entry latency). Risk is
reserved for the entire parent plan atomically before any child is sent and
released only when the plan closes.

Parent plan states: `created`, `risk_rejected`, `risk_reserved`, `dispatched`,
`partially_filled`, `filled`, `rejected`, `timed_out`, `hedging`, `unwinding`,
`residual_breach`, `reconciled`, `closed`.

Child order states: `created`, `risk_reserved`, `dispatched`, `unknown`,
`acknowledged`, `partially_filled`, `filled`, `rejected`, `timed_out`,
`cancelled`, `closed`.

Illegal transitions raise `StateTransitionError`; the transition table is
published by `mr-schema` and by `mr-deliverables`. The properties that matter:

* **A plan cannot close while exposed.** Moving a plan to `reconciled` or
  `closed` raises while it holds residual exposure or while any child is
  dispatched, unknown or timed out.
* **Silence is not a rejection.** When an acknowledgement misses
  `ack_timeout_ns` the child moves to `unknown` and the venue is queried
  (retried every `query_retry_ns`, up to `max_order_queries`). Whatever the
  venue reports, including fills, is recorded. The order is never resent while
  unknown; a venue that never confirms it moves the child to `timed_out`,
  which blocks the plan and trips the kill switch.
* **IOC remainders are cancelled explicitly** and recorded as `order_expired`.
* **Idempotency.** `client_order_id` is a uuid5 over run, plan, venue, role and
  attempt. A retry after a restart reuses the identifier, so the venue rejects
  a duplicate instead of doubling the position.
* **Leg policy.** `leg_dispatch=parallel` releases both legs together;
  `hardest_first` sends the leg least likely to fill (then the slower venue)
  and sizes the dependent leg to what actually filled. Sequential release falls
  back to parallel when a leg's notional exceeds `max_leg_exposure_notional`.
  `execution_style=maker_taker` rests the buy leg post-only at the touch and
  hedges each maker fill with an IOC taker order, cancelling the maker after
  `maker_ttl_ns`.
* **Owned residual.** `ResidualPolicy.HEDGE_THEN_UNWIND` hedges on the intended
  venue first and unwinds on the other venue if the hedge cannot complete.
  Hedges and unwinds are real orders with latency, rejects and retries; unwind
  retries escalate aggression and back off exponentially, never faster than a
  venue's rate-limit window. Residual size and age are checked continuously:
  a breach moves the plan to `residual_breach`, holds all new risk, forces the
  unwind, and escalates to the kill switch if still open after the escalation
  window.
* **Reduce-only means reduce-only.** A risk-reducing intent is capped to the
  position actually held, so a stale exit signal can never open the opposite
  exposure. The strategy's stance follows the ledger's positions whenever no
  plan is working.

`recover_router_state(ledger, run_id)` rebuilds open plans and open orders from
the ledger alone, which is what makes restart recovery a replay rather than a
guess.

## 9. Risk-limit matrix

Eighteen limits, each with a scope, a control, an order-state effect, an owner
and a severity; printed by `mr-schema` and written by `mr-deliverables`.

| Limit | Scope | Order-state effect |
| --- | --- | --- |
| `kill_switch` | strategy | Reject new; cancel open; risk-reducing only |
| `instrument_status` | instrument | Reject new orders on the instrument |
| `instrument_rules` | instrument | Resize or reject per the security master |
| `stale_market_data` | venue/instrument | Reject until the feed is fresh |
| `invalid_book` | venue/instrument | Reject until the book resyncs |
| `price_band` | order | Reject the order |
| `max_order_notional` | order | Reject or resize |
| `max_opportunity_notional` | parent plan | Reject the whole package |
| `max_open_orders` | strategy | Reject new orders |
| `max_gross_notional` | strategy | Reject non-reducing orders |
| `max_net_notional` | strategy | Reject non-reducing orders |
| `max_venue_notional` | venue | Reject on that venue |
| `max_message_rate` | venue | Throttle, then reject |
| `max_residual_notional` | parent plan | `residual_breach`; hold new risk; force unwind; kill if unresolved |
| `max_residual_duration` | parent plan | `residual_breach`; hold new risk; force unwind; kill if unresolved |
| `max_daily_loss` | strategy | Kill switch trips; cancel working orders; flatten positions |
| `venue_suspended` | venue | Reject new; cancel open; reducing only |
| `negative_edge` | parent plan | Reject the route |

The daily loss is measured on realised plus unrealised PnL, net of fees and
funding, marked to the live books and reset at the UTC day boundary. The
stale-data and invalid-book limits bind at order time: every leg carries its
book's age and validity.

`KillSwitchSupervisor` trips the kill switch automatically on: daily loss; a
residual breach still open after `residual_kill_after_ns`; an order the venue
never confirmed; a reconciliation break; no market data from a venue for
`stale_feed_ns`; a venue down for `venue_down_ns`; consecutive connectivity
rejects; and held gross exposure above the limit. On activation, working
orders are cancelled and, once no plan is working, every remaining position is
flattened by reduce-only plans. Exposure, loss and edge limits never block a
risk-reducing order. Clearing the kill switch requires a clean reconciliation;
a clear request without one is refused and recorded.

## 10. Ledger, reconciliation and restart

`EventLedger` is an append-only SQLite store. Every event carries a digest;
re-appending an identical event is a no-op that returns `False`, and appending
a different payload under an existing `event_id` raises `LedgerError`. There
is no update or delete path.

`derive()` rebuilds orders, positions, balances and realized PnL from the event
sequence alone, and `state_hash()` fingerprints the result.
`chain_digest()` fingerprints the event chain. Both are stable across a
close-and-reopen, which is the restart-recovery guarantee.

`reconcile(venue_state, ...)` performs a four-way comparison of orders, fills,
positions and balances against the venue view and classifies each difference as
a break with a scope, a cause and a severity. `mr-reconcile --ledger <path>
--run-id <id>` runs it against a finished run.

## 11. Monitoring, alerting and TCA

Twelve metrics with warn and critical thresholds and a named owner:
`feed_gap_count`, `feed_staleness_ns`, `reconnect_count`, `order_reject_rate`,
`fill_rate`, `residual_notional`, `residual_duration_ns`, `ack_latency_p99_ns`,
`risk_limit_utilization`, `reconciliation_breaks`, `missed_opportunities`,
`net_pnl`.

Seven alerts, each with an owner and a written response:

| Alert | Severity | Owner |
| --- | --- | --- |
| `feed_integrity_degraded` | critical | platform engineering |
| `venue_disconnected` | critical | platform engineering |
| `execution_quality_degraded` | high | strategy owner |
| `residual_exposure_open` | high | risk/operations |
| `risk_limit_near_breach` | medium | risk/operations |
| `reconciliation_break` | critical | risk/operations |
| `daily_loss_breach` | critical | risk/operations |

The TCA report is written as JSON, CSV and Markdown by every run and read back
by `mr-tca --run-dir <dir>`. It buckets by venue, by instrument and by order
type, and reports order and fill counts, rejects, filled quantity, notional,
fees, fee basis points, slippage cost, average slippage, fill rate, and the
maker/taker split. Opportunity-level attribution gives gross edge, fees,
slippage and net PnL per parent plan, plus a missed-opportunity list with the
reason each one was not taken.

## 12. Reproducibility

Every run writes `run_manifest.json` containing the run id, mode, seed, build
identity (package version and git revision), the full configuration, component
versions, the dataset identity, the input hash and the result hash.

`identity()` is a hash over the manifest with volatile keys removed
(`created_at`, `generated_at`, `wall_clock_ns`, `started_ns`, `finished_ns`),
so two runs of the same configuration over the same dataset compare equal even
though they happened at different times. `compare_manifests()` reports the
first differing field when they do not.

## 13. Runbooks

### Internal paper run (synthetic feed)

```bash
uv run trading-platform-poc mr-generate --output <dataset> --steps 400
uv run trading-platform-poc mr-verify   --dataset <dataset>
uv run trading-platform-poc mr-paper    --output <run> --dataset <dataset> --run-id paper-1
uv run trading-platform-poc mr-tca      --run-dir <run>
uv run trading-platform-poc mr-reconcile --ledger <run>/ledger.sqlite --run-id paper-1
```

Paper fills are labelled `internal_paper`; backtest fills are labelled
`internal_simulation`.

### Live paper run (public feeds) and forward-test comparison

```bash
uv run trading-platform-poc mr-paper --feed live --duration 3600 \
    --output <run> --record <captured-dataset> --run-id forward-1
uv run trading-platform-poc mr-backtest --dataset <captured-dataset> \
    --output <replay> --run-id replay-1
```

`--record` persists the exact raw frames the live session traded on, so the
backtest replays the same market. Compare `tca.json`,
`execution_quality.json` (latency slippage and markouts) and
`run_manifest.json` between `<run>` and `<replay>` to measure how far the
simulator is from the forward test.

Stop conditions: any reconciliation break, any critical alert, or any
automatic kill trigger. In all cases the kill switch engages, working orders
are cancelled, positions are flattened, and the run is not restarted until
`mr-reconcile` is clean.

### Venue testnet run

```bash
export BINANCE_TESTNET_API_KEY=... BINANCE_TESTNET_API_SECRET=...
export OKX_DEMO_API_KEY=... OKX_DEMO_API_SECRET=... OKX_DEMO_PASSPHRASE=...
uv run trading-platform-poc mr-paper --feed live --execution-mode venue_testnet \
    --venues binance okx --output <run> --run-id testnet-1
```

`minreq.execution_mode: venue_testnet` in `config.yaml` selects the same
routing without the flag. `testnet.py` wraps the existing guarded gateways
(`binance_execution.BinanceSpotTestnetGateway` and `okx_demo.OKXDemoAdapter`),
which read credentials only from the process environment and refuse
non-testnet endpoints. Startup is refused when a configured venue has no
testnet adapter (Bybit today), when `venue_testnet` is combined with a
synthetic feed or a backtest, or when the adapter that would execute is not a
testnet adapter. Every fill from this path is labelled `venue_testnet`; IOC is
emulated by cancelling any open remainder right after submit.

### Restart and recovery

1. Stop the process. The ledger is append-only, so no cleanup is required.
2. Run `mr-reconcile` and record the break list.
3. Restart with the same `--run-id`. `recover_router_state()` rebuilds open
   plans and orders from the ledger; deterministic client order identifiers
   mean any in-flight retry is rejected by the venue as a duplicate rather
   than doubling exposure.
4. Re-run `mr-reconcile` and confirm the break list is empty before releasing
   the kill switch.

## 14. Performance benchmark

`mr-benchmark --output <dir> [--steps N]` measures ingestion, normalization,
replay and simulation throughput, per-order overhead, feature overhead and peak
memory, and compares each against a threshold.

| Measurement | Threshold |
| --- | --- |
| Ingestion | ≥ 500 records/s |
| Normalization | ≥ 250 events/s |
| Replay | ≥ 2 000 events/s |
| Simulation | ≥ 100 events/s |
| Per-order overhead | ≤ 20 000 µs |
| Feature overhead | ≤ 5 000 µs |
| Peak simulation memory | ≤ 512 MB |

Thresholds are deliberately conservative so that the benchmark fails on a real
regression rather than on a shared or throttled CI host.

## 15. Acceptance tests

`mr-acceptance --output <workspace> [--only AT-06 AT-09]` runs the suite and
writes per-test evidence JSON plus a Markdown sign-off sheet.

| Test | What it proves |
| --- | --- |
| AT-01 | Multi-venue capture covers every canonical event type on every venue |
| AT-02 | Raw records map to the canonical schema with no loss of sequence, units, timestamps or native fields |
| AT-03 | Book reconstruction is deterministic, and gaps, staleness and crossed books are flagged and recovered |
| AT-04 | Security-master rules resize or reject orders with a stated reason |
| AT-05 | An interrupted ingestion backfills idempotently to the same checksum |
| AT-06 | Two identical runs produce identical manifests and result hashes |
| AT-07 | One fee and slippage model drives the route decision, the fills, TCA and PnL alike |
| AT-08 | A two-venue route completes, reconciles and ends with correct exposure, costs and net PnL |
| AT-09 | Liquidity consumed while a child is in flight causes a partial fill; the IOC remainder is cancelled, the hedge rejects and the unwind completes; the plan closes only when flat |
| AT-10 | Retry and restart create no duplicate orders; a lost acknowledgement moves orders to unknown, the venue is queried, its fills reach the ledger and reconciliation is clean |
| AT-11 | Control events (stall, suspend, rate limit, kill) change order state |
| AT-12 | Reconciliation is clean after normal operation and detects an injected break |
| AT-13 | Monitoring and TCA trace one opportunity from source event to final PnL |
| AT-14 | The performance benchmark meets every threshold |

`tests/test_minreq_hardening.py` pins every data-sanity, execution, risk,
live-feed and testnet behaviour described in sections 5 to 9 and 13.

Current status on the reference host: **14 of 14 passing**, no open defects,
and two accepted non-critical limitations (`LIM-01` paper feed source and
`LIM-02` benchmark host calibration) recorded with owners and remediation
dates in the generated `acceptance_report.md`.

## 16. Command reference

| Command | Purpose |
| --- | --- |
| `mr-instruments` | Print the effective-dated security master |
| `mr-fees` | Print the effective-dated fee schedule and resolved rates |
| `mr-schema` | Print every Section 10 machine-readable document |
| `mr-generate` | Capture and normalize a deterministic dataset |
| `mr-record` | Capture and normalize live public feeds into a dataset |
| `mr-datasets` | List captured datasets and their approval state |
| `mr-verify` | Re-hash a dataset and check its manifest |
| `mr-quality` | Print the data-quality report for a dataset |
| `mr-backtest` | Run the full stack in backtest mode |
| `mr-paper` | Run the full stack in paper mode on synthetic or live feeds, with internal or venue-testnet execution |
| `mr-reconcile` | Rebuild state from the ledger and reconcile |
| `mr-tca` | Print the TCA report written by a run |
| `mr-benchmark` | Measure throughput, overhead and memory |
| `mr-acceptance` | Run acceptance tests AT-01 to AT-14 |
| `mr-deliverables` | Write the Section 10 data deliverables |

## 17. Release status

**Backtest-ready.** MR-01 to MR-14 are implemented for historical mode,
AT-01 to AT-14 pass, runs are reproducible from a manifest, and every scenario
in Section 9 is deterministic and reconciled.

**Paper-ready, with one stated limitation.** Live public feeds
(`mr-paper --feed live`, `mr-record`) and venue-testnet routing
(`--execution-mode venue_testnet`) are wired through the same canonical model,
order states and EMS interfaces as the backtest. What remains is evidence: the
acceptance suite is hermetic, and a recorded live session, its backtest replay
and a venue-testnet session have not yet been attached to the acceptance
report (`LIM-01`). Until they are, simulator accuracy against live results is
a measurable quantity, not a verified one.

**Live-ready is not claimed**, and this document does not grant it.
