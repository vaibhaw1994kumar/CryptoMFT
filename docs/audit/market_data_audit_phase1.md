# Market-data production-readiness audit: Phase 1 (Steps 1–2)

*2026-10-22. This phase only read code and ran existing tests; no code was changed. All paths are under
`crypto_MFT_arb_platform/src/` unless stated otherwise.*

**Scope.** The live and replay path in `crypto_stat_arb`. It covers:

- Binance spot and USDⓈ-M linear depth;
- Bybit v5 spot and linear `orderbook.N`.

It also covers the `new_backtest` / `advanced_backtest` historical path, which uses Tardis `incremental_book_L2`.
The `trading_platform_poc` (platform-kit / `minreq`) stack is a separate implementation and was not audited here.

**Evidence used:**

- **Code** read along the call chain below.
- **Tests:** 78 tests across 11 targeted files, all passing.
- **CI:** the GitHub Actions results for runs `39812776522` and `39815345457`.
- **Live capture:** the 15-minute Bybit linear capture `data/live/perp_bybit_20261022`:
  - book versus 4 REST snapshots, aligned on `seq`;
  - trades versus the book;
  - the stall timeline.

---

## Step 1: Architecture map

```
                         ┌───────────── live_feed.LiveOrderBookService ─────────────┐
 Binance WS depth@100ms ─┤ _run_binance_connection                                 │
   (+ REST /depth 1000)  │   ws open → REST snapshot task in parallel              │
                         │   _drain_binance_until_snapshot (deltas buffered in book)│
 Bybit WS orderbook.N  ──┤ _run_bybit_connection (subscribe, no ack check)          │
                         │ _recv_fresh: timeout = max_book_age − age → STALE→resync │
                         │                                                          │
            [1] ENTRY    │ _capture_and_parse → recorder.capture_wire (wire.jsonl,  │
                         │   hash-chained) → json parse → PARSE rejection/quarantine│
                         │ → recorder.append_payload (raw.jsonl)                    │
                         │ _is_book_payload ─no→ control frame (recorded, counted)  │
                         │ adapters.normalize_{binance,bybit}_depth → MarketEvent   │
                         │   (NORMALIZE rejection on Key/Type/ValueError)           │
            [2] SEQ/TS   │ _apply_and_record → recorder.record_event (canonical)    │
                         │   → OrderBook.apply  (orderbook.py)                      │
            [3] APPLY    │      _invalid_level → INVALID_DATA                       │
                         │      snapshot: replace, prune buffered, replay buffer    │
                         │      delta: dup / out-of-order / _breaks_chain → GAP     │
            [4] STATE    │      _validate → VALID | SYNCING(empty side) |           │
                         │        CHECKSUM_FAILED(crossed/locked, checksum) |       │
                         │        PRICE_ANOMALY(spread, mid jump)                   │
                         │   book_gate.allow(all books, event.receive_ts) ──────────┼─► OMS.apply_feed_control
            [5] GATE     │   GAP/CHECKSUM/INVALID → ResyncRequired → reconnect      │    (per venue: cancel
                         │   (_venue_forever: backoff 1s→30s, resets on connect)    │     non-risk-reducing)
                         └───────────────┬──────────────────────────────────────────┘
                                         │ event_listener (same OrderBook objects)
                    paper_live.LivePaperSession._on_event
                         session.on_event(apply_book=False, gate_allowed=not gate.suspended)
                           time_monitor.observe → blocking flags skip *decisions* only
                           strategy.evaluate(books)  ← only if gate allowed
            [6] STORE    part-*/wire|raw|canonical|trades|derivatives|trace|quarantine.jsonl
                         + live_part_manifest.json (file sha256), live_manifest.json
                         ──► backtest: runner.run_backtest → replay.deterministic_order
                              → TradingSession(live_data=False) (own books + own gate)
                              → QualityGate report (APPROVED/REJECTED: *reported only*)
                         ──► new_backtest.CrossVenueFeed (Tardis L2 csv → jsonl.gz)
                              quality_preflight(report_only=True)
            [7] OPS      structured_logs (jsonl+sqlite), pipeline_monitoring.json,
                         pipeline_metrics.prom, alerting.py, session_audit (post-hoc)
```

| # | Question | Where |
|---|---|---|
| 1 | Raw messages enter | `live_feed._capture_and_parse` (WS) and `_process_binance_snapshot` (REST). The wire frame is persisted *before* parsing. |
| 2 | Timestamps / sequence validated | Sequence: `orderbook.OrderBook.apply` and `_breaks_chain`. Timestamps: **only in `session.on_event` → `TimeIntegrityMonitor.observe`** (not in the feed service). Field shape: `canonical.validate_canonical_event`, which runs on the replay dispatcher and on live *trades*, **not on live book events**. |
| 3 | Snapshots / deltas applied | `OrderBook._apply_snapshot` and `_apply_delta`. |
| 4 | Book valid / stale / invalid | `OrderBook._validate`, `OrderBook.mark_stale` (called by `BookIntegrityGate.allow` and `_mark_stale`), `start_resync`. |
| 5 | Eligibility enforced | `BookIntegrityGate.allow` → `session.on_event` returns before `strategy.evaluate`. Per venue, `OMS.apply_feed_control` moves the venue to RISK_REDUCING_ONLY. |
| 6 | Storage / backtest supply | `RotatingLiveRecorder` → `part-*`. Replay via `runner.run_backtest` and `sdk`. Historical data via `new_backtest.event_loader` / `fetch_tardis`. |
| 7 | Failure → recovery / alerts | `ResyncRequired` / exceptions → `_venue_forever` reconnect with backoff. Structured log events `RESYNC_REQUIRED`, `CONNECTOR_ERROR`, `BOOK_STATE`, `RECONNECT_SCHEDULED`. `alerting.py` threshold flags. |

---

## Step 2: Implementation inventory

Status legend:

- **PASS:** behaviour verified in the production path, and tested.
- **PARTIAL:** implemented, but with a gap in logic or tests.
- **FAIL:** wrong or unsafe behaviour.
- **NOT IMPL:** not implemented.
- **NOT VERIFIED:** could not be checked here.

### A. Ingestion and message integrity (Step 3)

| Capability | Status | Implementation | Tests | Gap / required change |
|---|---|---|---|---|
| Raw preservation before parsing | PASS | `capture_wire` writes a hash-chained `wire.jsonl` before `json.loads`; `verify-wire` / `verify-raw` | `test_live_feed::test_malformed_frame_is_preserved_before_parse_rejection`; `test_raw_capture` | — |
| Malformed JSON / non-object | PASS | `_capture_and_parse` → `parse_errors`, quarantine `stage=PARSE`, `FRAME_PARSE_REJECTED` log | as above | — |
| Missing fields / bad types | PARTIAL | normalizer `KeyError`/`TypeError`/`ValueError` → `NORMALIZE` quarantine | only indirect | The live book path **does not call `validate_canonical_event`** (live trades and replay do). No test of a book frame with fields missing. |
| Non-finite / non-positive levels | PASS | `OrderBook._invalid_level` → INVALID_DATA → resync | `test_orderbook`; platform_kit `test_order_book` | — |
| Unexpected message types | PARTIAL | `_is_book_payload` false → `control_frames` counter + `record_control` | — | Control frames are counted but never interpreted. |
| Subscription ack / failure (Bybit) | **FAIL** | `_run_bybit_connection` sends `subscribe` and never checks `{"success": false}` | none | A failed or unknown-symbol subscription looks like silence: initial sync times out → reconnect loop, with no reason code naming the failed subscription. |
| Heartbeat / ping | PARTIAL | `websockets` ping 20 s / 20 s; **no Bybit app-level `{"op":"ping"}`**; stall detected by `_recv_fresh` | none | Bybit asks for an app ping every 20 s. Silence is caught only by the freshness timeout. |
| Duplicates / out-of-order | PASS | `OrderBook.apply`: same id → dup; `sequence <= book.sequence` → dup/out-of-order (counted, not applied) | `test_orderbook` | — |
| Delta buffer overflow | PARTIAL | `_buffer`: > `max_pending_deltas` (10,000) → GAP `delta_buffer_overflow` | **none** | Untested. |
| Queue backpressure / slow consumer | **NOT IMPL** | Processing is synchronous inside the recv loop. `websockets max_queue=4096` frames, then TCP backpressure. | none | No lag metric (receive-to-processed exists only in paper `paper_timestamps`). Under overload, latency grows silently until the stale timer fires. |
| Persistence failure / disk full | **FAIL** | `recorder.capture_wire` / `record_event` are not wrapped. An `OSError` propagates to `_venue_forever` → logged as `CONNECTOR_ERROR` → **reconnect loop**. | platform_kit `test_recorder` covers the *other* stack only | Disk full turns into a reconnect storm, without a distinct reason code or a fail-closed stop. |
| Reconnect backoff / storms | PARTIAL | `_venue_forever`: exponential 1 s → 30 s, reset after a successful connect | `test_live_feed::test_sequence_gap_forces_reconnect...` | No retry limit, no storm alarm. Backoff resets on *connect*, not on *valid book*, so a connect-then-fail loop (e.g. bad subscription) retries every ~1 s forever. |
| Received / accepted / applied / persisted accounting | PARTIAL | `raw_frames`, `parse_errors`, `normalization_errors`, `control_frames`, `canonical_events`, book `stats`, manifest counts; `session_audit` checks canonical count = manifest | `test_session_audit_inputs` | No single reconciliation identity per venue (received = rejected + control + applied + dup + buffered + dropped). |
| Never "valid" after lost events | PASS (book path) | Any sequence break → GAP → `ResyncRequired` → `start_resync` | `test_live_feed`, `test_book_gate`, `test_replay_order` | — |

### B. Timestamps and clock (Step 4)

| Capability | Status | Implementation | Tests | Gap |
|---|---|---|---|---|
| Field semantics documented | PARTIAL | `docs/time/timestamp_dictionary.json`. Per event: `exchange_ts_ns` (Binance `E`, Bybit `ts`), `source_ts_ns` (Binance `T`, Bybit `cts`), `receive_ts_ns` (host `time.time_ns()`) | — | `receive_ts_ns` is **wall clock, not monotonic**: it stepped back 62 ms in the 2026-10-22 capture. |
| Negative / impossible latency | PASS | `TimeIntegrityMonitor.observe`; optional host-offset correction (`correct_host_offset`) | `test_time_integrity`, `test_replay_order` | Blocks decisions only; the book still applies (documented, correct). |
| Clock rollback / regression | PARTIAL | Replay order fixed in `99760d1`. **Live**: no detection of host-clock steps; `book.last_receive_ts_ns` can go backwards. | `test_replay_order` | Freshness = wall now − wall receive, so a step distorts staleness. Use `time.monotonic_ns()` for freshness and keep the wall clock for records. |
| Host clock sync monitoring | PARTIAL | `HostClockMonitor` exists; live samples come only from the exchange-timestamp quantile | `test_time_integrity` | No NTP query. This host is about 100 ms slow (measured with `sntp`) and nothing alerts on it. |
| Stale uses the right timestamp | PARTIAL | `mark_stale`: now − `last_receive_ts_ns` (host receive). That's correct in kind, but wall-clock based. | `test_book_gate` | As above. |
| Cross-venue skew separate from freshness | PASS | `TimeIntegrityMonitor` cross-venue skew vs `max_cross_venue_skew_ns`, reported separately | `test_time_integrity` | Reported only; not used for eligibility. |
| Fields not overwritten in normalization / replay | PASS | Frozen `MarketEvent`; replay reorders without changing fields | `test_replay_order::test_timestamps_are_not_modified` | — |

### C. Binance reconstruction (Step 5)

Products: spot `depth@100ms` + REST `/api/v3/depth?limit=1000`, and USDⓈ-M `depth@100ms` + `/fapi/v1/depth?limit=1000`.

| Item | Status | Notes |
|---|---|---|
| Subscribe before snapshot; buffer while the snapshot is in flight | PASS | WS opens first and the snapshot task runs concurrently. Deltas go to `pending_deltas` while `sequence is None`. Tested in `test_live_feed`. |
| Drop events covered by the snapshot; first applicable update | PASS (spot), PASS (futures) | Pruning keeps `u > lastUpdateId`. Bridging uses `U <= lastUpdateId+1` (spot rule; for futures it is implied by `pu+1`). |
| Continuity | PASS | Spot: `U == last+1` (`sequence_start`). Futures: `pu == last u` (`_breaks_chain`, `14ee025`). Verified live on 2026-10-22. |
| Missing range / overlap / out-of-order | PASS / PARTIAL | Gap → resync (tested). An overlapping range with `u > last` but `U <= last` is accepted, which matches the venue rule. |
| Snapshot timeout / failure | PARTIAL | HTTP timeout → exception → reconnect. Stale while fetching → `market_data_stale_while_fetching_snapshot`. **No test.** |
| Snapshot older than the buffered deltas (the race) | PARTIAL | If the first buffered delta has `U > lastUpdateId+1`, it's a GAP → resync. Correct, but **untested**. |
| Repeated gaps / snapshot storm | PARTIAL | Each gap means a reconnect plus a REST snapshot (weight 20 at limit 1000 on Binance). No rate guard; backoff resets on connect. |
| Process restart | PASS (by design) | A book is never restored from disk; it always needs a fresh snapshot. |

### D. Bybit reconstruction (Step 6)

Products: v5 `spot` / `linear`, `orderbook.{1,50,200,1000}`.

| Item | Status | Notes |
|---|---|---|
| Snapshot replaces the book | PASS | `_apply_snapshot` replaces both sides. |
| Insert / update / zero-qty delete | PASS | `_update`. On 2026-10-22, 4 REST snapshot comparisons all matched at the best bid and ask; level differences came from the REST/WS straddle plus one Bybit REST/WS disagreement. |
| `u` progression and gap rule | PARTIAL | Uses `u == last+1`. **This relies on behaviour Bybit doesn't document as a guarantee.** It was observed to hold over 131,763 deltas, with no false gaps. |
| `u = 1` restart | PASS | Arrives as a `type: snapshot`, which resets the book. |
| Reconnect | PASS | New snapshot on resubscribe. |
| REST reconciliation | NOT IMPL (live) | No periodic REST cross-check. Done offline this session (`perp_checks.py`, not in the repo). |
| Subscription failure / unknown symbol | **FAIL** | See section A. |

### E. Structural integrity (Step 7)

| Check | Status | Notes |
|---|---|---|
| Finite / positive prices; finite quantities | PASS | `_invalid_level` |
| Crossed / locked | PASS | → CHECKSUM_FAILED → resync (tested) |
| Empty side | PARTIAL | → SYNCING `empty_book_side`. **Untested**, and the live loop does *not* resync on SYNCING. It waits for the next snapshot or for the stale timer. |
| Spread / mid-jump sanity | PASS | PRICE_ANOMALY with confirmation (tested in `test_execution_realism`) |
| Checksum | PASS (n/a) | Neither Binance depth nor Bybit v5 orderbook carries a checksum. An unknown checksum fails closed. |
| Tick / lot conformance of book levels | **NOT IMPL** | Instrument rules are used for orders, not for validating feed levels. |
| Depth truncation | PARTIAL | Bybit depth N is maintained by the venue's deletes (verified). Binance keeps every level it has ever seen, beyond the 1000-level snapshot; deep levels can go stale (not used for top-of-book). |
| Inactive / delisted instruments | PARTIAL | `InstrumentStatusPoller` runs in live-paper **spot only** (`cli.py` gates on both markets being spot). |

### F. Freshness and eligibility (Step 8)

| Check | Status | Notes |
|---|---|---|
| Stale or invalid → no new signals | PASS | `session.on_event` returns before `strategy.evaluate` unless the gate allows. Tested (`test_book_gate`). |
| Two venues both required | PASS | Gate uses `cfg.venues`; any invalid book suspends. |
| No bypass path | PASS (crypto_stat_arb) | Live: shared `OrderBook` objects; paper passes `gate_allowed` from the service gate. `sdk.py` has its own gate call. |
| Recovery not valid prematurely | PASS | VALID only from `_validate` after a snapshot and chain. |
| Quiet market vs stall | PARTIAL | A fixed `max_book_age_ms` (1000 for the perp config). The 2026-10-22 stalls were real (74 and 5 trades arrived on other sockets meanwhile), but BTC's p99.9 inter-arrival is already 550 ms. Illiquid symbols would flap. No per-instrument threshold and no heartbeat-based liveness. |
| OMS on invalid feed | PASS / PARTIAL | `apply_feed_control` → RISK_REDUCING_ONLY, cancels non-risk-reducing orders; cancel-pending tracking exists (`test_maker_execution_path`). Fills during cancel and recovery: **not verified** end to end. |
| Thread safety | N/A | Single asyncio loop; no threads touch books. |

### G. Cross-venue consistency (Step 9)

| Check | Status | Notes |
|---|---|---|
| Instrument identity / product match | PARTIAL | `perpetual_feed_settings` refuses perpetual configs on spot books; instruments resolved by symbol. No check of multiplier, settlement or quote-asset equality between legs. |
| Divergence vs data fault | **NOT IMPL** | No cross-venue price-consistency monitor; only time skew. |
| No look-ahead in decisions | PASS | Replay is causal (`deterministic_order`, derivative tickers applied ≤ event time); strategy sees books as of the event. |

### H. Tardis (Step 10)

| Item | Status | Notes |
|---|---|---|
| Tardis download / normalise | PASS (tool) | `new_backtest/fetch_tardis.py` (`incremental_book_L2` → jsonl.gz) |
| Tardis vs own capture comparison | **NOT IMPL** | Nothing compares recorded feeds with Tardis. Tardis L2 has no venue sequence numbers, so `new_backtest` replays can't detect gaps. |

### I. Backtest data quality (Step 11)

| Check | Status | Notes |
|---|---|---|
| Quality gate on replay | **FAIL (enforcement)** | `runner.run_backtest` computes `QualityGate` (gaps, negative latency, silent feeds, …) and writes `data_quality_report.json`, but **a REJECTED dataset still produces full results and exit code 0** (observed 2026-10-22). `session_audit` flags it afterwards. |
| `new_backtest` preflight | PARTIAL | `run_preflight(report_only=True)` is the default, so it never aborts. |
| Ordering / clock steps | PASS | `99760d1` + `test_replay_order` |
| Recorded exclusions | NOT IMPL | No interval-exclusion policy; invalid intervals are just "gate suspended" periods inside the run. |
| Fill-model look-ahead | NOT VERIFIED | `fill_label` has calibration-day logic (other session's work); not audited in this phase. |

### J. Dataset manifest (Step 14)

PARTIAL. `live_manifest.json` and `live_part_manifest.json` hold file hashes, counts and adapter versions. The backtest `manifest.json` / `summary.json` hold config, source sha256 and the quality report. Missing:

- a single manifest per dataset covering instrument-metadata version, invalid or quarantined *intervals*, latency statistics and repair records;
- the software git revision, which is set only in CI.

---

## Highest-risk findings so far (for Phase 2 prioritisation)

| # | Sev | Finding |
|---|---|---|
| 1 | P1 | Backtests on REJECTED data run to completion and exit 0. Results from bad data can be mistaken for valid. |
| 2 | P1 | Disk-full or persistence errors become an endless reconnect loop instead of a fail-closed stop with a distinct reason. |
| 3 | P1 | Bybit subscription failures are not detected. A bad symbol means a silent reconnect loop at ~1 s, with backoff reset on every connect. |
| 4 | P2 | Freshness and staleness use the wall clock. Host-clock steps distort them. Host clock offset (about 100 ms here) is not alerted. |
| 5 | P2 | No overload / consumer-lag detection. The live book path skips `validate_canonical_event`. |
| 6 | P2 | No Tardis-vs-capture comparison; no live REST reconciliation; no cross-venue price-consistency monitor. |
| 7 | P2 | Untested branches: snapshot timeout, snapshot/delta race, buffer overflow, empty side (which also doesn't trigger a resync), Bybit `u` reset mid-stream. |
| 8 | P3 | Fixed 1 s staleness threshold for every instrument; Binance never truncates deep levels. |

## Environment limitations

- `tests/platform_kit` can't be collected locally: 12 collection errors from missing `tardis_dev` and similar packages. They run on CI, where on every OS one fails: `test_minreq_cli::test_paper_mode_uses_the_venue_testnet_execution_mode`. That's a pre-existing test/code mismatch.
- On Windows CI, 3 test files fail to import because `benchmark.py` uses the Unix-only `resource` module.
- **Binance is unreachable from this network:** the ISP's DNS returns a non-Binance address. No live Binance validation is possible until that's resolved.
- No Tardis API key is available here, so no Tardis comparison can be run.

---

## Phase 2: P1 fixes (2026-10-22)

| # | Finding | Fix | Regression tests |
|---|---|---|---|
| 1 | Backtests on REJECTED data exit 0 | `runner.enforce_data_quality`. `backtest` exits 1 on a REJECTED dataset and still writes every artifact as evidence. `--allow-rejected-data JUSTIFICATION` records `data_quality_override.json`. `economics_gate` fails `DATA_QUALITY REJECTED` unless there's an override or `--allow-incomplete DATA_QUALITY=…`. | `tests/test_data_quality_enforcement.py` (5) |
| 2 | Disk-full / I/O error turns into a reconnect loop | `live_feed.PersistenceFailure`: recorder writes (`@_persisting`) and structured-log writes translate `OSError`; network `OSError`s (ConnectionError, TimeoutError) are untouched. Feed loops re-raise it; `_fail_closed` then invalidates every book (gate suspends, cancels), logs `PERSISTENCE_FAILURE`, and stops. The recorder stays failed. Shutdown returns an unsealed summary if the manifest can't be written. `live-orderbook` / `live-paper` exit 2. | `tests/test_feed_failures.py::PersistenceFailureTests` (4) |
| 3 | Bybit subscription refusal looks like silence; backoff resets on connect | `_check_subscription_reply` on the book, trade and derivative sockets. `success: false` → `SubscriptionRejected` (Bybit's `ret_msg`), `SUBSCRIPTION_REJECTED` log, `subscription_rejections` / `subscription_acks` counters. A refused connection doesn't reset the backoff. `RECONNECT_SCHEDULED` now logs the backoff actually used. | `tests/test_feed_failures.py::SubscriptionTests` (3) |

**Verification:**

- **Mutation check:** each fix was broken on purpose in a scratch copy, and the matching tests failed: M1–M4.
- **Real data:** all 5 real Bybit subscribe replies in `perp_bybit_20261022` are read as acks.
- **Full suite:** 521 passed.
- **Spot regression:** identical to baseline.
- **`run_all_local`:** PASSED.

**Not verified:**

- A real Bybit *rejection* frame. That needs a live call with a bad symbol; the format is taken from Bybit's documentation.
- `live-paper`'s `finalize` writing on a full disk. It still raises, which also gives a non-zero exit.
