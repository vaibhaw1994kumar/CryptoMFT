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

## Phase 2b: untested recovery paths (2026-10-22)

`tests/test_recovery_paths.py`: 10 tests that drive the live service (fake sockets and REST) and `OrderBook`. The tests were written against the expected behaviour first. Three failed on the existing code and exposed real defects:

| Defect | Sev | Scenario | Root cause | Fix |
|---|---|---|---|---|
| Overflow forgotten | P1 | The Binance delta buffer overflows while the REST snapshot is in flight. A snapshot older than the dropped updates replays what was kept and reports **VALID with updates known to be missing**, until the next delta happens to expose the gap. | `apply()` overwrote the overflow GAP with SYNCING `awaiting_snapshot`, and nothing remembered what was dropped. | `OrderBook.dropped_through`: the overflow stays a GAP (counted once per episode). A snapshot below it is `GAP snapshot_does_not_cover_dropped_deltas:<snap><<dropped>`; one at or above it recovers. |
| REST snapshot storm | P2 | A Binance REST snapshot that keeps failing (503 / timeout) was retried at the minimum backoff on every attempt. | The backoff reset on every websocket *connect*. | The backoff resets only when the connection produced a valid book (`VenueLiveStats.book_syncs`). This also generalises the P1-3 subscription-refusal rule. |

**Verified correct, now with tests:**

- **Snapshot hangs while the feed is silent** → `market_data_stale_while_fetching_snapshot`, then recovery.
- **Snapshot older than the buffered deltas** → `buffered_sequence_gap`, never VALID before recovery.
- **Binance REST snapshot with an empty side** → resync.
- **Bybit delta empties a side** → SYNCING: the gate suspends and orders are cancelled. When the side refills it goes VALID with the sequence intact and no reconnect. *This corrects Phase 1, which listed "empty side does not trigger a resync" as a gap. Within an intact sequence, an empty side is a real book state and a resync isn't needed.*
- **Bybit `u` reset by a new snapshot** → the book is replaced and old levels removed.
- **Repeated gaps after valid books** → each recovery starts from the minimum backoff.

**Verification:**

- **Mutation check:** M5–M7, each fix broken on purpose; the matching test fails.
- **Full suite:** 531 passed.
- **Spot regression:** identical to baseline.
- **Bybit capture replay:** byte-identical events, book hashes and quality counts.
- **`run_all_local`:** PASSED.

## Phase 2c: CI on Windows (2026-10-22)

After `f9e2e2a` removed the Windows import errors, the Windows test job ran the suite for the first time. Before that, collection errors had stopped pytest before any test ran. It showed **40 failures with the same cause**: `PermissionError [WinError 32] … structured_logs.sqlite`.

- **Root cause:** `StructuredLogStore` used `with sqlite3.connect(...) as connection:`. In Python that commits or rolls back but **does not close** the connection.
  - On macOS, `lsof` shows `structured_logs.sqlite` still open after `run_backtest` returns.
  - Windows can't delete an open file, so every test that cleaned up a run directory failed.
- **Fix:** `structured_logging._connect()` commits or rolls back, then always closes. It's used at all 5 sites.
  - Afterwards, `lsof` shows 0 run files open.
  - There's no performance cost: a 69,799-event replay took 45.5 s before and 45.1 s after, with identical log rows.
- **Regression test:** `tests/test_sqlite_handles.py` tracks every connection the store opens during a backtest and asserts it is closed. It fails on the old code on every OS, not only Windows.

**Windows CI follow-up, after `a60df26`.** Windows went from 42 failures to 7. Of those 7:

- **Fixed, recorder failure path:** after a recording failure, `recorder.close()` failed partway and left the part files open, so the handles leaked. `RotatingLiveRecorder.abort()` now releases every handle without sealing the part.
- **Fixed, TCA report paths:** `summary.tca.daily_reports` used OS separators (`tca_daily\…` on Windows). They're now written as POSIX paths.
- **Fixed, test cleanup:** my two perp tests never closed the OHLC/LTP CSV writers or the files they read.
- **Open, Windows-only hang:** `test_maker_execution_path::LiveReplayParityTests` takes 0.18 s on macOS but exceeds its 10 s `wait_for` on Windows. The `paper_timestamps.jsonl` lock is only a side effect of the timeout. It wasn't reproducible here; the likely areas are Windows' asyncio Proactor loop or its clock resolution. Not patched: raising the timeout would hide it.
- **Open, existing failures:** `test_minreq_cli` paper-mode default, and AT-14 timing on the slower Windows runner.

## Phase 3: P2 fixes (2026-10-23)

| # | Finding (Phase 1) | Fix | Tests |
|---|---|---|---|
| 1 | Live book events skipped `validate_canonical_event` | `LiveOrderBookService._validated()` runs it on every book event (websocket and REST snapshot) before it is applied. A rejected event is quarantined (`stage=VALIDATE`, `validation_errors`) and never applied, so a real hole shows up as a sequence gap. All 39,792 events in the 2026-10-22 Bybit capture pass it. | `test_market_data_p2::LiveValidationTests` |
| 2 | Freshness used the wall clock | `OrderBook.mark_stale_age()`. `BookIntegrityGate.allow(..., ages_ns)` takes per-venue ages, which the live service measures on `time.monotonic_ns()` from the last applied update (`_book_ages`, `_remaining_fresh_seconds`). Recorded timestamps are unchanged; replay passes no ages and keeps recorded time. | `MonotonicFreshnessTests` (step forward: no false stale; step back: silence still stale; replay unchanged) |
| 3 | No backlog / slow-consumer detection; host-clock offset not alerted | `TimePolicy.max_feed_delay_ns` (config `time.max_feed_delay_ms`). The venue-to-host delay is corrected by the host-offset estimate. Above the limit, an event is `FEED_DELAYED`, and every other venue of that instrument gets `PEER_FEED_DELAYED` until the delayed stream catches up. Both block decisions, in live paper and in replay. The live service logs `FEED_DELAYED` / `FEED_CAUGHT_UP` and `HOST_CLOCK_OFFSET` (offset beyond `max_host_offset_ms`), and reports them under `service.time_integrity`. | `FeedDelayTests` (off by default; own and peer blocking; catch-up; offset-corrected), `LiveTimingAlertTests` |

**Setting the limit to 2000 ms** (`research`, `paper_watch`, `perp_research`):

- **A real backlog in the recordings.** Cycle 0006 (2026-09-14) has a backlog: 151 events more than 2 s late (worst 4.4 s, corrected).
- **Normal tails stay below it.** Elsewhere the worst corrected delay is 1.3 s, and 1.08 s in the 2026-10-22 capture.
- **No change to regression results.** With the limit on, cycles 0006–0010 have identical opportunities, fills, P&L and economics. No opportunity fell inside a delayed window.
- **The host clock was already off in September.** The offset estimates for those cycles are −150 to −230 ms: the host clock was behind then too.

**Mutation check:** M8–M11 fail the matching tests.

**Verification:**

- **Full suite:** 555 passed.
- **Spot regression:** identical to baseline.
- **Bybit capture replay:** identical.
- **`run_all_local`:** PASSED.

**Still open (P2):**

- a Tardis vs own-capture comparison;
- live REST reconciliation;
- a cross-venue price-consistency monitor.

## Phase 3b: live REST cross-check (2026-10-23)

`src/crypto_stat_arb/rest_reconcile.py`, wired into `LiveOrderBookService`. It's off by default; turn it on with `--rest-reconcile-seconds N` (`LiveFeedSettings.rest_reconcile_seconds`).

- **Sequence alignment.** A REST book carries a sequence on the feed's own counter:
  - Binance: `lastUpdateId`, on the websocket `u` counter;
  - Bybit: the cross-sequence `seq` (`data.seq`), because Bybit's REST `u` is a different counter.
- **Comparing against the right book state.** By the time the response arrives, the feed has normally moved past that sequence (one request takes ~300 ms). So while a request is in flight, and only then, `BookHistory` keeps the top 100 levels (Bybit) or 200 (Binance) after every applied update. The snapshot is compared with the states just before and just after the update that crossed its sequence, and the closer match counts.
- **Comparison method.** Price by price, inside the REST window.
- **Outcomes:**
  - `RECONCILE_OK`;
  - `RECONCILE_DEEP_DIFF`: the best bid and ask agree, deeper levels differ. This is a warning only, because the venue's own REST and websocket can disagree there (seen on 2026-10-22);
  - `RECONCILE_MISMATCH`: the best bid or ask disagrees on both sides. The book becomes `GAP_DETECTED` `rest_reconcile_touch_mismatch`, which forces a resync and blocks trading;
  - `RECONCILE_SKIPPED`: the check can't be done, because the book resynced, the snapshot is older than the history, the history overflowed, or there's no sequence;
  - fetch and parse failures are counted (`reconcile_fetch_errors`) and are not treated as book faults.
- **Request size and evidence.** Snapshots are small (Binance `limit=100`, Bybit `limit=50`) and recorded raw as control frames for evidence.

**Real-data check.** I replayed the 2026-10-22 Bybit capture through the live service, delivering the 4 real REST snapshots with realistic send/arrive timing (two timing assumptions). Result:

- snapshots 1 and 3: OK;
- snapshot 2: DEEP_DIFF (2 levels);
- snapshot 4: DEEP_DIFF (20 levels, a busy straddling push);
- 0 mismatches, 0 false resyncs, and the book stayed VALID with 0 gaps.

This matches the offline analysis. A first design that compared against the *current* book skipped 3 of the 4 snapshots, which is why the history was added.

**Tests:** `tests/test_rest_reconcile.py` (10), mutation check M12–M14.

**Verification:**

- **Full suite:** 605 passed.
- **Spot regression:** identical (the check is off by default).
- **`run_all_local`:** PASSED.

## Phase 3c: cross-venue consistency (2026-10-23)

`src/crypto_stat_arb/cross_venue.py`:

- **Instrument compatibility at config load.** `check_instrument_compatibility` is called from `load_config`. Every venue leg of the strategy instrument must match on base, quote, product type, settlement asset and multiplier. A spot leg against a perpetual, or USDT against USDC, is refused. All shipped configs pass.
- **`CrossVenueMonitor` in the live service** (two venues). After each applied update, while both books are VALID, it measures the mid divergence (bps). Above `cross_venue_divergence_bps` (default 25) the divergence is classified, logged on each transition (`CROSS_VENUE_DIVERGENCE` / `CROSS_VENUE_CONVERGED`) and summarised under `service.cross_venue`. **It never blocks:** arbitrage *is* divergence, and stale or delayed data already block trading elsewhere. The classes:
  - `STALE_SIDE`: a venue is FEED_DELAYED;
  - `STALLED_VENUE`: one venue's mid hasn't changed for `cross_venue_stall_seconds` (5 s) while the other's has. This is a frozen feed that still looks VALID, which no other check catches;
  - `MARKET_DIVERGENCE`: both sides are current, so it's a real price difference.
- **Choosing the default from data.** Over the recorded BTC spot cycles 0006 and 0007 (147,467 two-venue samples), the divergence was p50 0.39 bps, p99 1.3–7 bps, max 13 bps. There was no breach at 25 bps, even through cycle 0006's backlog. A delayed feed doesn't necessarily show up as divergence; FEED_DELAYED is the check for that.

**Tests:** `tests/test_cross_venue.py` (10), mutation check M15–M17.

**Verification:**

- **Full suite:** 615 passed.
- **Spot regression:** identical.
- **`run_all_local`:** PASSED.

## Phase 3d: Tardis comparison (2026-10-23)

`src/crypto_stat_arb/tardis_compare.py` and the CLI command `compare-tardis --capture <session> --venue BYBIT --tardis <incremental_book_L2.csv.gz> --output report.json`.

This is an offline research tool: it isn't on the live path and adds no dependencies to it. Neither source is treated as ground truth.

- **Reconstruction.** Both books are rebuilt independently: ours with `OrderBook` (sequence-checked), and Tardis's from the raw CSV with exact decimals. Rows are grouped back into venue messages, and a run of snapshot rows resets the book.
- **Alignment.** Samples are taken on the exchange timestamp, every 1 s by default. Comparison is touch first, then level by level inside our depth, since Tardis may record deeper books.
- **Classification:** `MATCH`, `LOCAL_RECOVERY` (excluded), `TIMESTAMP_ALIGNMENT`, `LOCAL_INGESTION_LOSS`, `DEPTH_OR_SOURCE_DIFFERENCE`, `UNRESOLVED`.
- **Report contents:**
  - input SHA-256 hashes;
  - counts per class and the mismatch share;
  - event counts;
  - arrival-minus-exchange percentiles for both sources;
  - our side's integrity (gaps, resyncs);
  - discrepancies with our `raw_ref` and the Tardis CSV row;
  - a reproduction command.

**Evidence:**

- **Unit tests:** `tests/test_tardis_compare.py` (8), mutation check M18–M20.
- **Real-scale self-check.** The 2026-10-22 Bybit capture (39,792 updates) was compared against a Tardis-format CSV built from its own websocket frames: 901/901 samples MATCH. With 1% of updates dropped from that CSV, 92% of samples are flagged and our side shows 0 gaps.

**NOT VERIFIED against real Tardis data.**

- No Tardis API key is available here, and Tardis's free datasets are the first day of each month, which overlaps none of our captures.
- To run it: capture on the 1st of a month (or use a key), download `incremental_book_L2` for the same symbol and window, then run `compare-tardis`.
- Expect `DEPTH_OR_SOURCE_DIFFERENCE` where Tardis's channel depth differs from ours (Bybit `orderbook.50` vs Tardis's deeper book).

## Phase 4: dataset quality manifest, Step 14 (2026-10-23)

`src/crypto_stat_arb/dataset_manifest.py`. Every `run_backtest` writes `dataset_manifest.json`. It's a separate file so that `summary.json` and its `deterministic_result_sha256` don't change. It records:

- **Dataset identity.** A dataset SHA-256 built from the input files (events, plus derivatives if present). Provenance back to the live session: session id and hash, wire and rejected counts, quarantined rows per part.
- **Venues.** Per venue:
  - instruments;
  - feed (BINANCE_SPOT / BINANCE_USDM / BYBIT);
  - the reconstruction rule applied;
  - event-type counts;
  - adapter and schema versions;
  - first and last exchange and receive times (ns and ISO UTC);
  - `raw_ref` coverage.
- **Instrument metadata version.** SHA-256 of the instrument and fee config, plus the effective-dated rows.
- **Sequence and recovery.** From an independent rebuild of each venue's book: gaps, resyncs, duplicates, crossed books, final state.
- **`invalid_intervals`.** Every stretch in which a venue's book wasn't VALID, with reasons, duration and the starting `raw_ref`.
- **`silent_intervals`.** No book update for longer than the max book age. A reconnect leaves no sequence gap in the data, only this silence.
- **Timestamp anomalies and latency.** Per-venue negative, corrected, impossible and delayed counts, latency percentiles, the max corrected delay, and the host-clock offset and state.
- **`source_comparisons`.** Summaries of attached `compare-tardis` reports (`backtest --comparison-report`).
- **`exclusions_and_repairs`.** The explicit policy (nothing is excluded; invalid intervals are replayed with decisions blocked). It also lists every repair: within-stream reorders after host clock steps, with the count moved and the input left unchanged. Truncation semantics, the data-quality decision and any override justification are here too; the override is added by `backtest --allow-rejected-data`.
- **Run.** Config path and SHA-256, platform version, a `source_sha256` of the running package's source, git revision / checkout / dirty flag, Python and host.
- **`manifest_sha256`.** It doesn't depend on the output path, so the same inputs give the same manifest (tested).

**Real data:**

- **Cycle 0006 (2026-09-14):** it shows Binance invalid intervals of 14.5 s (waiting for the first snapshot), plus two real sequence gaps of 0.4 s and **19.9 s**, and 151 feed-delayed events.
- **The 2026-10-22 Bybit capture:** its two stalls appear as silent intervals (3.66 s and 3.47 s), with the 8-event clock-step reorder repair, provenance and the attached Tardis self-check.
- **Cost:** about 1.2 s per 22k events.
- **Git revision quirk.** The git revision it reports comes from the nested checkout inside the package directory, so `dirty` is true there. `source_sha256` identifies the code that ran regardless.

**Tests:** `tests/test_dataset_manifest.py` (7), mutation check M21–M23.

**Verification:**

- **Full suite:** 640 passed. That excludes `tests/test_latency_model.py`, a new untracked file from another session whose import (`crypto_MFT_arb_platform.src.new_backtest...`) fails at collection.
- **Spot regression:** identical; manifests written for all 5 cycles.
- **`run_all_local`:** every stage after its unittest step PASSED. That unittest step is blocked by the same file.
