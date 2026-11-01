# Market-data production-readiness audit: final report

*Binance and Bybit market data for `crypto_stat_arb` (live feed service, order-book reconstruction, timing, gating, OMS feed control, replay and backtest data quality).*

- **Report date:** 2026-10-25.
- **Code:** GitHub `vaibhaw1994kumar/CryptoMFT` at `2572b74`.
- **Working log:** the phase-by-phase log with every measurement is [`market_data_audit_phase1.md`](market_data_audit_phase1.md). This report summarises it, and doesn't add claims that the log doesn't support.

---

## A. Executive summary

**Recommendation: READY FOR PAPER TRADING ONLY**, for BTC on Binance and Bybit (spot and USDⓈ-M / linear perpetual public data). The recommendation and its conditions are in section H.

### What the audit established

- **Reconstruction is correct for every feed in scope:**
  - **Binance spot:** ranged updates, chained by `U = previous u + 1`;
  - **Binance USDⓈ-M:** chained by `pu`;
  - **Bybit v5:** `u + 1`, with a new snapshot on reconnect or restart.
- **A book can't be wrongly valid.** Across 150 randomised seeds × 3 rules × 400 messages with injected drops, swaps, duplicates and NaN levels, a VALID book always equalled an independently built true book, and was never crossed.
- **Checked against live venues.**
  - All four Bybit REST snapshots agreed with the live book at the best bid and ask. Deeper-level differences were explained by the snapshot landing between websocket pushes, except one level at depth 36 where Bybit's REST and websocket views disagreed.
  - 25,775 Bybit trades all printed within 5 bps of the rebuilt book.
  - Binance futures sequencing was verified live on 2026-10-22.
- **Invalid or stale books can't trade, and two layers enforce it:**
  - the book gate blocks strategy evaluation;
  - the OMS's own pre-trade `VENUE_STATUS` check refuses new risk on a venue whose feed is invalid.

  Orders in flight stay reserved, and late fills are booked.
- **Failures stop safely instead of looping:**
  - a disk-full error stops the session;
  - a refused subscription is named and backs off;
  - a failing REST snapshot backs off;
  - a backlog over 2 s blocks decisions.
- **Bad data no longer passes as good.** A rejected dataset makes `backtest` exit 1, and every run writes a dataset manifest with invalid and silent intervals and every repair made.

### Highest-risk findings (all fixed; section C)

| Finding | Effect |
|---|---|
| Binance USD-M books gapped after every snapshot | 15 resyncs a minute live |
| A buffer overflow could still produce a VALID book | The book was VALID while updates were known to be missing |
| Replay sorted by the host wall clock | A clock step created false gaps and rejected datasets |
| Rejected datasets exited 0 | Results from bad data could be used |
| Disk full and refused subscriptions | Reconnect loops |
| The timing monitor's memory grew without bound | ~0.5 GB/day |

### Why not "READY" for live trading

- **Binance hasn't been re-verified live since the fixes.** Binance is DNS-blocked on this network (Reliance Jio resolver).
- **No order has been placed on a testnet.** There are no keys.
- **No real Tardis comparison has been run.**
- **No multi-day soak run has been done.**
- **The host clock is ~100 ms slow.**

---

## B. Implementation matrix

Statuses are as of `2572b74`. "Sim" means verified with deterministic fixtures or recorded data, not against a live venue.

| Step / requirement | Status | Code | Tests | Remaining gap |
|---|---|---|---|---|
| **3 Ingestion** raw saved before parsing; malformed frames quarantined | PASS | `live_feed._capture_and_parse`, `RotatingLiveRecorder` | `test_live_feed`, `test_raw_capture` | — |
| Envelope validation on live book events | PASS | `LiveOrderBookService._validated` | `test_market_data_p2` | — |
| Subscription ack / refusal (Bybit) | PASS (sim + real acks) | `_check_subscription_reply` | `test_feed_failures` | A real refusal frame hasn't been observed |
| Heartbeat / idle | PARTIAL | websockets ping 20 s; freshness timeout | — | Bybit app-level `{"op":"ping"}` not sent |
| Duplicates / out-of-order / non-finite | PASS | `OrderBook.apply`, `_invalid_level` | `test_orderbook`, randomized | — |
| Buffer overflow | PASS | `OrderBook.dropped_through` | `test_recovery_paths`, `test_load_degradation` | — |
| Persistence failure | PASS | `PersistenceFailure`, `_fail_closed`, `RotatingLiveRecorder.abort` | `test_feed_failures` | `live-paper` finalize on a full disk raises (still a non-zero exit) |
| Backoff / reconnect storms | PASS | `_venue_forever` (resets on a VALID book) | `test_recovery_paths`, `test_feed_failures` | No retry cap or storm alarm |
| Received / accepted / applied / persisted accounting | PARTIAL | stats, manifests, dataset manifest | `test_dataset_manifest` | No single per-venue reconciliation identity |
| **4 Timestamps** field semantics | PARTIAL | `docs/time/timestamp_dictionary.json`, dataset manifest | — | Order-send / ack / fill and decision timestamps not audited |
| Negative / impossible latency, host-offset correction | PASS | `TimeIntegrityMonitor` | `test_time_integrity`, `test_execution_realism` | — |
| Clock steps (replay) | PASS | `replay.deterministic_order` | `test_replay_order` | — |
| Freshness on a monotonic clock (live) | PASS | `mark_stale_age`, `_book_ages` | `test_market_data_p2` | — |
| Backlog / delayed batches | PASS | `FEED_DELAYED` / `PEER_FEED_DELAYED` (`time.max_feed_delay_ms`=2000) | `test_market_data_p2`, `test_load_degradation` | — |
| Host-clock alert | PASS | `HOST_CLOCK_OFFSET` | `test_market_data_p2` | No NTP query (inferred from venue timestamps) |
| **5 Binance** spot and USD-M reconstruction | PASS (sim; USD-M live 2026-10-22) | `OrderBook._breaks_chain`, `_drain_binance_until_snapshot` | `test_orderbook`, `test_recovery_paths`, randomized | No live re-check after the fixes (DNS block) |
| Snapshot failure / timeout / race / overflow / restart | PASS (sim) | as above | `test_recovery_paths`, `test_failure_injection`, `test_load_degradation` | — |
| **6 Bybit** snapshot / delta / delete / reset | PASS (sim and the 2026-10-22 capture) | `normalize_bybit_depth`, `OrderBook` | `test_recovery_paths`, randomized | `u` contiguity is observed (131,763 deltas), not documented |
| REST reconciliation | PASS (recorded data) | `rest_reconcile.py` (`--rest-reconcile-seconds`) | `test_rest_reconcile` | Not run live; off by default |
| Deep channels (200/1000) | NOT VERIFIED | — | — | Only `orderbook.50` tested |
| **7 Structural** finite / positive levels, crossed / locked, empty side, spread / jump | PASS | `OrderBook._validate` | `test_orderbook`, `test_recovery_paths` | — |
| Tick / lot conformance of feed levels | NOT IMPLEMENTED | — | — | Instrument rules are applied to orders only |
| Depth truncation | PARTIAL | Bybit venue-maintained | — | Binance keeps every level seen (memory growth over days) |
| Inactive / delisted instruments | PARTIAL | `InstrumentStatusPoller` | `test_forward_testnet_calibration` | Polled for spot `live-paper` only |
| **8 Eligibility** gate, no bypass, two-venue requirement | PASS | `BookIntegrityGate`, `session.on_event` | `test_book_gate`, `test_failure_injection`, `test_load_degradation` | — |
| OMS on an invalid feed (cancel, reservation, late fill) | PASS (sim) | `OMS.apply_feed_control`, `RiskEngine.validate_pretrade` | `test_failure_injection` | — |
| Quiet vs stalled market | PARTIAL | fixed `max_book_age_ms` | — | No per-instrument thresholds |
| **9 Cross-venue** contract compatibility | PASS | `cross_venue.check_instrument_compatibility` | `test_cross_venue` | — |
| Divergence vs data fault | PASS (report-only) | `CrossVenueMonitor` | `test_cross_venue` | — |
| No look-ahead in decisions | PASS | `deterministic_order`, causal derivative merge | `test_replay_order`, `test_perpetual_path` | — |
| **10 Tardis** comparison tool | PASS (tool) | `tardis_compare.py`, `compare-tardis` | `test_tardis_compare` | **Not run against real Tardis data** |
| **11 Backtest** rejected data enforced | PASS | `runner.enforce_data_quality`, `economics_gate` | `test_data_quality_enforcement` | `new_backtest` preflight still report-only |
| Recorded exclusion policy | PASS | dataset manifest (`exclusions_and_repairs`) | `test_dataset_manifest` | Policy is "exclude nothing, block decisions" |
| Fill-model look-ahead | NOT VERIFIED | `fill_label` / `fill_model_v1` (other session's work) | — | Not audited |
| **12 Failure injection** | PASS (25 of 25 listed scenarios; "processing queue overflow" maps to FEED_DELAYED, as there is no internal queue) | — | 12 audit test files, 92 tests | Not every test asserts all 7 properties |
| **13 Load** throughput / latency / memory / reconnects | PASS (this host) | `scripts/load_test.py` | `test_load_degradation` | One instrument per service; no real network loss; no multi-day soak |
| **14 Dataset manifest** | PASS | `dataset_manifest.py` | `test_dataset_manifest` | The git revision is the nested checkout's; `source_sha256` is authoritative |
| **15 Static checks** | PARTIAL | pyflakes clean on changed files | — | mypy / type checks not run |

---

## C. Findings

All findings below are **fixed**, each with regression tests. Every fix was **mutation-checked**: each was broken on purpose (M1–M30) and the matching test failed. Severity follows the audit brief.

| # | Sev | File / function | Failure scenario | Root cause | Effect | Fix (commit) | Tests |
|---|---|---|---|---|---|---|---|
| 1 | P1 | `orderbook.OrderBook.apply` | Binance USD-M: a gap after every snapshot (15 resyncs/min live) | The spot rule `U == u+1` was applied to futures, where `U` jumps and continuity is `pu` | Futures book almost never VALID | `_breaks_chain` uses `pu` when present (`14ee025`) | `test_orderbook`, randomized |
| 2 | P1 | `replay.deterministic_order` | A 62 ms host clock step inverted 4 Bybit deltas | Replay sorted by the wall-clock receive time | A false gap, so the dataset was REJECTED | Venue-sequence order within a stream (`99760d1`) | `test_replay_order` (9) |
| 3 | P1 | `runner`, `cli backtest`, `economics_gate` | A dataset the quality gate REJECTS still exited 0 | The decision was reported, not enforced | Results from bad data looked usable | Exit 1, plus a written override (`c204d5e`) | `test_data_quality_enforcement` (5) |
| 4 | P1 | `live_feed` recorder | Disk full or an I/O error on a write | The `OSError` reached the generic reconnect handler | An endless reconnect loop, no reason code | `PersistenceFailure` → fail-closed stop, exit 2 (`c204d5e`) | `test_feed_failures` |
| 5 | P1 | `live_feed._run_bybit_connection` | Bybit refuses a subscription | `success:false` was ignored; the backoff reset on connect | Silent reconnects every ~1 s forever | `SubscriptionRejected`; backoff grows (`c204d5e`) | `test_feed_failures` |
| 6 | P1 | `OrderBook.apply` / `_apply_snapshot` | The delta buffer overflows before the snapshot; an older snapshot arrives | The overflow GAP was overwritten by SYNCING | **VALID with known-missing updates** | `dropped_through` (`d1e1be2`) | `test_recovery_paths`, `test_load_degradation` |
| 7 | P1 | `time_integrity._VenueTimeStats` | Long live session | Every latency and feed age was kept in lists | ~0.5 GB/day memory growth | Running stats plus a bounded window; flat at 13.06 MB (`2572b74`) | `test_load_degradation` |
| 8 | P2 | `live_feed._venue_forever` | A failing Binance REST snapshot | The backoff reset on websocket connect | REST weight storm (rate-limit risk) | Reset only on a VALID book (`d1e1be2`) | `test_recovery_paths` |
| 9 | P2 | `structured_logging` | Run directories on Windows | `with sqlite3.connect()` doesn't close | 40 Windows CI failures, locked files | `_connect()` closes (`a60df26`) | `test_sqlite_handles` |
| 10 | P2 | `live_feed` recorder, `runner` | Close after a write failure; Windows paths | Handles left open; OS path separators | Leaked handles; non-portable reports | `RotatingLiveRecorder.abort`; POSIX paths (`771ee3d`) | `test_feed_failures`, `test_tca` |
| 11 | P2 | `benchmark` | Windows import | Unix-only `import resource` | No Windows tests ever ran | Guarded import (`f9e2e2a`) | CI on Windows |
| 12 | P2 | `live_feed` book path | Malformed but parseable book event | `validate_canonical_event` was skipped on live books | An invalid envelope could be applied | `_validated` (`5583abb`) | `test_market_data_p2` |
| 13 | P2 | `book_gate`, `live_feed` | Host clock step | Freshness measured on the wall clock | False stale, or stale missed | Monotonic ages (`5583abb`) | `test_market_data_p2` |
| 14 | P2 | `time_integrity` | Backlog: cycle 0006 had 151 events >2 s late (max 4.4 s) | No delay check | Decisions on seconds-old data | `FEED_DELAYED` / `PEER_FEED_DELAYED`, 2000 ms (`5583abb`) | `test_market_data_p2`, `test_load_degradation` |
| 15 | P2 | `structured_logging.connector_record` | Two distinct records in one clock tick | The log id omitted source event, raw_ref and payload | Diagnostics silently dropped (common on Windows) | Id includes them (`2572b74`) | `test_load_degradation` |
| 16 | P2 | `live_feed` | Binance USD-M mark price; NA trades; TLS | `markPrice` only on `/market/ws`; `X="NA"` rows; empty CA store | No funding data; quarantine noise; no connection | Route, filter, certifi fallback (`14ee025`) | `test_perpetual_path` |
| 17 | P3 | `live_feed._apply_and_record` | Every book update | One SQLite log row per update | ~45% of frame processing time | Transitions only: 990 → 4,383 frames/s (`2572b74`) | `test_load_degradation` |

**Capabilities added** (not defects):

- **REST cross-check:** `886b3df`.
- **Cross-venue compatibility and divergence monitor:** `669422a`.
- **Tardis comparison tool:** `d92db24`.
- **Dataset manifest:** `e33f7a5`.
- **Failure-injection suite:** `594bd65`.
- **Load harness:** `2572b74`.

---

## D. Recovery verification

| Path | Binance spot | Binance USD-M | Bybit v5 | Tests |
|---|---|---|---|---|
| Subscribe before snapshot; buffer | ✓ | ✓ | n/a (WS snapshot) | `test_live_feed`, `test_load_degradation` |
| First update must bridge the snapshot | ✓ `U ≤ lastUpdateId+1` | ✓ | n/a | `test_orderbook`, randomized |
| Continuity | ✓ `U = u+1` | ✓ `pu = u` (live 2026-10-22) | ✓ `u+1` (131,763 live deltas) | randomized (150 seeds) |
| Gap → invalid → resync → VALID only after a snapshot | ✓ | ✓ | ✓ | `test_live_feed`, `test_recovery_paths` |
| Snapshot timeout / failure, with backoff | ✓ | ✓ | n/a | `test_recovery_paths` |
| Snapshot older than the buffer; overflow | ✓ | ✓ | n/a | `test_recovery_paths`, `test_load_degradation` |
| Snapshot replacement / `u` reset | ✓ | ✓ | ✓ | `test_recovery_paths` |
| Empty side → blocked, recovers in place | ✓ | ✓ | ✓ | `test_recovery_paths` |
| Reconnect cycles (60), both venues together | ✓ | ✓ | ✓ | `test_load_degradation` |
| Restart: never trusts prior state | ✓ | ✓ | ✓ | `test_failure_injection` |
| REST cross-check of the live book | ✓ (`lastUpdateId`) | ✓ | ✓ (`seq`; 4 real snapshots) | `test_rest_reconcile` |

---

## E. Tardis comparison results

| | |
|---|---|
| **Real Tardis data compared** | **None.** There's no API key, and Tardis's free datasets (the 1st of each month) overlap none of our captures. |
| Tool | `compare-tardis`: independent rebuilds, exchange-timestamp alignment, comparison within our depth, classified discrepancies with raw references and Tardis rows. |
| Self-check (real scale) | 2026-10-22 Bybit capture vs a Tardis-format CSV built from its own frames: **901 / 901 MATCH**. With 1% of updates dropped from the CSV, 92% of samples are flagged; our side shows 0 gaps. |
| Expected on real data | `DEPTH_OR_SOURCE_DIFFERENCE` where Tardis's channel depth differs from Bybit `orderbook.50`. Unresolved differences are listed with evidence for review. |

---

## F. Test evidence

All commands were run from `crypto_MFT_arb_platform/` on 2026-10-25, with macOS arm64 and Python 3.12.

| Command | Result |
|---|---|
| `PYTHONPATH=src python3.12 -m pytest tests --ignore=tests/platform_kit --ignore=tests/test_latency_model.py -q` | **661 passed**, 0 failed (5 of 5 consecutive runs after the Step 13 fix) |
| Audit test files (`test_replay_order`, `test_data_quality_enforcement`, `test_feed_failures`, `test_recovery_paths`, `test_sqlite_handles`, `test_market_data_p2`, `test_rest_reconcile`, `test_cross_venue`, `test_tardis_compare`, `test_dataset_manifest`, `test_failure_injection`, `test_load_degradation`) | **92 passed** |
| Regression baseline (`regress_oct22.py`: Bybit capture with derivatives, two-venue sample, controlled gap), GitHub `594bd65` vs `2572b74` | **Identical** on all three |
| `run_all_local.py` stages after its unittest step | **PASSED** |
| `scripts/load_test.py --capture data/live/perp_bybit_20261022 --config config/perp_research.json --venue BYBIT` | 4,383 frames/s (durable 3,046); p50 196 µs, p99 634 µs; memory flat at 13.06 MB after 318k frames |
| GitHub Actions `wheels`, run 40067337672 (`2572b74`) | Linux and macOS: 1,443 passed, 1 failed. Windows: 1,440 passed, 4 failed. Wheels, sdist and acceptance evidence passed. |

### Failures and limitations, reported as they are

- **`tests/platform_kit/test_minreq_cli.py::test_paper_mode_uses_the_venue_testnet_execution_mode`** fails on every OS. It's pre-existing (`d6d9369`, 2026-10-12): the test expects `venue_testnet`, while the code documents `internal_paper` as the default.
- **Windows, timing-sensitive tests.** Intermittent failures in `test_forward_testnet_calibration` (live-paper vs replay opportunities), `test_watch_pipeline` (replay economics), `test_book_display`, and the AT-14 speed threshold. They weren't reproducible locally, including with a Windows-like 15.6 ms clock.
- **`tests/test_latency_model.py`** (another session, untracked) fails at import, which blocks a full pytest run and `run_all_local`'s unittest step.
- **`tests/platform_kit`** can't be collected locally (`tardis_dev` and other dependencies missing), so it runs on CI only.
- **`runs/paper_watch/`** (the 2026-09-14 cycles and the original spot-regression baseline) was deleted outside the audit. It was replaced by the 2026-10-22 baseline.
- **Static checks:** pyflakes on changed files only (one pre-existing unused import in `runner.py`). mypy wasn't run.

---

## G. Outstanding risks

### Needs live validation

1. **Binance after the fixes.** Binance is DNS-blocked on this network, so books, snapshots, `pu` chaining and the REST cross-check need a fresh live run.
2. **REST cross-check live.** A Bybit run with `--rest-reconcile-seconds` has been tested on recorded data only.
3. **Testnet order lifecycle** for the futures transports, against real venues (needs keys).
4. **Real Tardis comparison.**
5. **A real Bybit subscription refusal frame.**
6. **A multi-day soak run.** The memory plateau was shown over ~2 hours of live-rate data. Binance's deep-level retention grows slowly over days.

### Not implemented or partial

- **Bybit app-level ping.**
- **Tick/lot conformance of feed levels.**
- **Per-instrument staleness thresholds.**
- **A retry cap and reconnect-storm alarm.**
- **A per-venue received / accepted / applied / persisted identity.**
- **`new_backtest` preflight is still report-only.**
- **Fill-model look-ahead not audited.**
- **Order / ack / fill timestamp audit.**
- **Deep Bybit channels.**
- **Several instruments per service.**

### Operational

- **The host clock is ~100 ms behind the venues.** It's corrected in analysis and alerted, but it should be fixed with NTP.
- **A rare ~220 ms single-frame pause** (GC or a file open).
- **The Bybit `u + 1` continuity rule is observed, not documented.**

---

## H. Production recommendation

### **READY FOR PAPER TRADING ONLY**

**Scope.** One instrument (BTC) on Binance and Bybit public market data, spot and USDⓈ-M / linear perpetual, in `live-paper` mode (internal paper execution), with:

- `time.max_feed_delay_ms = 2000` (as shipped);
- `--rest-reconcile-seconds 300` recommended;
- a monitored host clock.

**Supported by:**

- correct reconstruction on all three feed rules, randomized and against live data;
- two independent layers that block trading on invalid data;
- fail-closed handling of storage, subscription and snapshot failures;
- bounded memory and ~70–99× throughput headroom;
- enforced data quality with recorded manifests;
- 661 tests and 92 audit tests passing, mutation-checked;
- identical regression results.

**Before restricted live trading** (each item is required):

1. A live Binance validation run with the fixes (needs network access to Binance).
2. Testnet order-lifecycle evidence for both venues' futures transports.
3. A Tardis (or other independent source) comparison over a matching window, with no unresolved discrepancies.
4. A multi-day paper soak with memory, reconnect, delay and reconciliation statistics reviewed.
5. Host clock on NTP, and the Bybit app-level ping implemented.
6. The CI test failures resolved or formally waived. That means the `minreq` default decision, the Windows timing tests, and `test_latency_model` import-fixed.

This recommendation covers market-data integrity only. Strategy economics are a separate decision; the maker harness was unprofitable in all 28 earlier scenarios.
