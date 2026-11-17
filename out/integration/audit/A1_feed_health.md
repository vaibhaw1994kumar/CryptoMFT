# A1 audit: feed health and clock (Phase 0, read-only, 16 Nov 2026)

Scope: IDs 0.2a, 0.2b, 1.1 to 1.8. Working copy only. Paths: core = `crypto_MFT_arb_platform/src/crypto_stat_arb/` (C:), backtester = `crypto_MFT_arb_platform/src/new_backtest/` (B:), tests = `crypto_MFT_arb_platform/tests/` (T:), ollog = `order_lifecycle_logger/ollog/` (O:).

What I ran (all read-only, nothing written outside this file):
- Resolved profiles through the loaders: `new_backtest.profiles.load_profile/apply_profile` and `crypto_stat_arb.config.raw_config_from_profile` + `config_from_raw` for v1_2, v1_9, v1_9s (output summarised in 1.1).
- `load_config` on `config.yaml` and `config.testnet.example.yaml`: both FAIL with `JSONDecodeError` (C:config.py:135-136 is `json.loads`). See 1.1.
- Targeted tests, one command: `PYTHONPATH=src python3.12 -m pytest -q -p no:cacheprovider tests/test_time_integrity.py tests/test_market_data_p2.py tests/test_book_gate.py tests/test_load_degradation.py tests/test_monitoring.py tests/test_quality.py tests/test_alerting.py tests/test_raw_capture.py` -> `55 passed in 19.49s`. A passing unit test is never counted as end-to-end evidence below.
- Read recorded evidence: `out/integration/paper_live/run2/d7d-run2/*` (live paper session, 3,285.6 s, started 2026-11-08T22:15:40Z), `out/integration/day1/*` (29 Oct 2026 recorder day), `out/integration/paper_live/silence_analysis_run2.md`, `out/sim_validation_spot/notes.md`, `out/fill_evidence_rebuild/prep.log`, `order_lifecycle_logger/latency_logs`, `fillprobe_runs`.

Two things to keep apart: "D7d runs" are 8-9 Nov 2026 live paper sessions (`out/integration/paper_live/run1`, `run2`, `check1`, `shakedown*`); "29 Oct" is the day-1 recorder session replayed under `out/integration/day1`. The order text mixes them ("29 Oct D7d silences"); I report both separately (1.7).

## Component inventory (what each existing module does; used by 0.2a, 1.2, 1.3, 1.4)

| Module | What it does | Evidence (file:line) |
|---|---|---|
| `time_integrity.py` | Per-event latency (receive minus exchange), host-offset estimate, flags NEGATIVE_RECEIVE_LATENCY[_CORRECTED], IMPOSSIBLE_EVENT_LATENCY, FEED_DELAYED, PEER_FEED_DELAYED, STALE_FEED, FUTURE_RECEIVE_TIMESTAMP, cross-venue receive skew; report `time_integrity_report.json` | C:time_integrity.py:188-255 (observe), :162-186 (offset), :265-310 (report) |
| `HostClockMonitor` | SYNCED / DEGRADED / UNSYNCED / UNKNOWN from offset and drift samples | C:time_integrity.py:66-110; only caller is the monitor itself at :181 (see 1.6) |
| `book_gate.py` `BookIntegrityGate` | Suspends strategy evaluation while any required book is not VALID; per-venue ACTIVE / RISK_REDUCING_ONLY; cancels orders; suspended seconds by UTC day and cause; suspension intervals | C:book_gate.py:16-114 |
| `orderbook.py` `OrderBook` + `BookSanityPolicy` | `BookState` SYNCING/VALID/STALE/GAP_DETECTED/CHECKSUM_FAILED/PRICE_ANOMALY/INVALID_DATA; crossed/locked, mid-jump 500 bps, wide spread 500 bps, stale by age; any-message age | C:models.py:18-25, C:orderbook.py:14-19, :81-86, :293-306 |
| `live_feed.py` | Websocket service: raw/wire capture, per-venue stats, book-stream liveness on a monotonic clock, silence handling, reconnect storm alarm, FEED_DELAYED / HOST_CLOCK logging, trade sanity (`trade_outside_book_band` 500 bps), REST reconcile (off by default) | C:live_feed.py:118-158 (stats), :161-210 (settings), :1285-1312 (storm), :1617-1647 (timing), :1815-1860 (liveness/silence), :2022-2038 (trade check) |
| `quality.py` | End-of-run defect counters -> flags -> `HealthLevel` HEALTHY/DEGRADED/CRITICAL, hash-chained defect log, APPROVED/REJECTED | C:quality.py:14-198 |
| `monitoring.py` | `PipelineMonitor.build`: 9 pipeline metrics (+ reconnect storms, restart, strategy-execution) with thresholds, `MonitorHealth` HEALTHY/DEGRADED/CRITICAL, JSON + Prometheus files, refreshed every `monitor_interval_seconds` 1.0 | C:monitoring.py:23-38, :78-263, :370-461; C:live_feed.py:1150-1205 |
| `alerting.py` | Alert OWNERSHIP matrix only (type, owner, severity, response target); no delivery channel | C:alerting.py:39-160 |
| `raw_capture.py` | Append-only, hash-chained raw frames, `raw_manifest.json` (counts, first/last receive ts, hashes, sources) | C:raw_capture.py:44-205 |
| `session.py` | The decision-side time gate: `TimeIntegrityMonitor.observe` per event; `assessment.blocking` -> ledger `TIME_INTEGRITY_BLOCK`, return without decision; then `book_gate` check | C:session.py:408-412, :486-501, :566-572 |

## 1.1 first, because every other block uses its values

## 1.1 - Report-only config audit of every feed-health component per profile
- Status: PARTIAL
- Exists: effective values are resolvable through the loaders (below); `profiles.py` has a `switch_table` (B:profiles.py:522) and `SWITCHES` (B:profiles.py:96-168) but it lists NO feed-health switch except `timing.dead_feed_s` (MANDATORY, B:profiles.py:112). The time policy, quality, monitoring and book-sanity sections are not profile keys: they are core defaults chosen in C:config.py:551-616 (`raw_config_from_profile`), recorded only as free-text `notes` (C:config.py:587-588) in the run manifest. There is no single artifact that lists feed-health components and their effective values per profile; this file is the first.
- Wired into: replay (session runtime only), paper, testnet (Python-only), live capture. Call paths: profile -> `SessionReplay.__init__` (B:session_replay.py:155) -> `config_from_raw`; paper config = the same `SessionReplay.raw_config` written to JSON by `out/integration/paper_live/make_config.py:8-15` (D7d used v1_5), read by `live-paper --config` -> `LivePaperSession` -> `LiveOrderBookService` (C:paper_live.py:93-114).
- Effective config per profile (resolved by running the loaders; source in brackets):

| Component / key | v1_2 | v1_9 | v1_9s | D7d paper config (`config_v1_5.json`) | `config/research.json`, `perp_research.json`, `paper_watch.json` |
|---|---|---|---|---|---|
| runtime | `backtester` (own loop; core TimePolicy NOT used) | `session` | `session` | n/a | n/a (core `run_backtest`) |
| `time.correct_host_offset` | n/a on its runtime; a session-runtime run would get True | True [C:config.py:598, owner 8 Nov Q-G4] | True | True | True |
| `time.max_feed_delay_ms` (FEED_DELAYED) | n/a | None = OFF | None = OFF | None = OFF (`run2 time_integrity_report.json`: `"max_feed_delay_ns": null`) | 2000 = ON (all three files, `time` section) |
| `time.max_feed_age_ms` (STALE_FEED, non-blocking) | n/a | 500 [= max_book_age] | 500 | 500 | 1000 |
| `time.max_cross_venue_skew_ms` (report only) | n/a | 500 | 500 | 500 | 1000 |
| `time.max_host_offset_ms` / drift ppm | n/a | 100 / 500 | 100 / 500 | 100 / 500 | 100 / 500 |
| `time.negative_latency_tolerance_ms` | n/a | 0 | 0 | 0 | 5 |
| offset quantile / window / min samples | n/a | 0.01 / 2000 / 20 | same | same | same |
| book gate `max_book_age` (strategy.max_book_age_ms) | 5.0 s dead-feed only (`timing.dead_feed_s`) | 500 ms | 500 ms | 500 | 1000 |
| `timing.dead_feed_s` (resubscribe trigger, not a blocking gate, C:session.py:216) | 5.0 [v1_2.yaml, MANDATORY] | 5.0 | 5.0 | 5.0 (replay-only argument) | n/a |
| `resync_delay_s` | absent | 5.0 ASSUMED [v1_9.yaml:12] | 5.0 | n/a | n/a |
| `trust_local_ts` | bybit False, binance False | same | same | n/a | n/a |
| `book_sanity` (jump / spread / confirm) | n/a | 500 bps / 500 bps / 3 [core defaults] | same | same | same |
| `quality` thresholds (core defaults) | n/a | all 0, persistence lag 5 s | same | same | `paper_watch`: max_gaps 5, stale 10, duplicates 20, out_of_order 20, negative_latency 200, wide_spreads 5; the other two all 0 |
| `monitoring` thresholds | n/a | data age 500 ms, reconnects 5, stale_disconnects 0, residual 0.1 | same | same defaults | data age 1000 ms; paper_watch reconnects 10, stale_disconnects 5 |
| breaker `heartbeat_timeout_s` (gateway silence) | 10.0 ASSUMED | 10.0 | 10.0 | n/a | n/a |
| preflight in replay | `quality_preflight=True`, blocks on CRITICAL unless a justification override (B:backtester.py:2895, B:cross_venue_feed.py:84) | same, B:session_replay.py:330-332 | same | n/a | n/a |

Live-service settings (`LiveFeedSettings`, C:live_feed.py:161-210; CLI defaults C:cli.py:40-76, :203-212): `book_heartbeat_seconds` 0.0 = OFF; `rest_reconcile_seconds` None = OFF; `trade_stale_seconds` 30; `derivative_stale_seconds` 30; `reconnect_alarm_count` 5 in 300 s; `cross_venue_divergence_bps` 25 and `cross_venue_stall_seconds` 5 (report only, never blocking); `max_trade_deviation_bps` 500; `durable` fsync True; `initial_sync_timeout_seconds` 15; UDP publisher on unless `--no-udp`.

Other config files: `src/crypto_stat_arb/config.yaml` and `config.testnet.example.yaml` cannot be loaded by `load_config` (JSON loader; `JSONDecodeError` on both) and I found no module of the working copy that reads their `data_quality.*` keys (`stale_after_ms`, `max_market_data_delay_ms`, `reject_stale_market_data` ...): they are orphan or legacy files for another stack. `testnet.py` builds execution adapters only (C:testnet.py:837-848); it has no feed settings. A venue-testnet session is `LivePaperSession(execution_mode=VENUE_TESTNET)` from Python (the CLI refuses it, C:cli.py:113-118), so it uses the same feed and time values as paper. ollog has its own feed rule: `unsafe_feed_s = 5.0` -> kill switch (O:probe.py:58, :463-468).
- Why disabled: `max_feed_delay_ns` defaults to None "keeps the original behaviour" (C:time_integrity.py:43-46); `raw_config_from_profile` sets only `correct_host_offset` (C:config.py:598) and no recorded owner decision picks a FEED_DELAYED value for profile-derived configs. `book_heartbeat_seconds` and `rest_reconcile_seconds` default off in the CLI; D7d silence analysis says the silence timer fired 0.12 to 2.24 s late (C:live_feed.py:1848-1853 comment, `silence_analysis_run2.md`). Nothing was enabled by this audit.
- What changes when enabled: FEED_DELAYED on -> an event whose offset-corrected delay is above the limit is a blocking flag (C:time_integrity.py:57-63, :213-215); the session returns before any decision and writes `TIME_INTEGRITY_BLOCK` (C:session.py:486-501); the other venue of the same instrument gets PEER_FEED_DELAYED until the late stream delivers a non-delayed event (:216-224). Live logs `FEED_DELAYED` / `FEED_CAUGHT_UP` once per transition (C:live_feed.py:1617-1637). It does NOT cancel resting quotes (see 1.4). `book_heartbeat_seconds` > 0: Bybit app ping every N s so a quiet book keeps producing frames (C:live_feed.py:1611 `_book_heartbeat`). Turning FEED_DELAYED on in a reference profile changes results; on March 2024 replays the receive stamps are constructed (`trust_local_ts` False), so it would measure the latency model, not the feed (see 1.6, 1.7).
- Tests and evidence: `T:test_market_data_p2.py:218-248` (FeedDelayTests, 3 cases), `T:test_load_degradation.py:52-73`; end-to-end: D7d run2 `time_integrity_report.json` policy block shows the effective values above.
- Exact gap: (a) no per-profile feed-health config report or release-identity entry; (b) `config.yaml` and the testnet example are unloadable and unread; (c) FEED_DELAYED is on only in three JSON configs that no recorded live or replay run used (the D7d config has it off); (d) the loader-resolved v1_2 values apply only if v1_2 were run on the session runtime, which the frozen profile never is.
- Implementation: add a `feed_health` section to `release_identity` (B:profiles.py:484) and to `SessionReplay.config_notes` that dumps the resolved `time_policy`, `LiveFeedSettings` and `max_book_age`; keep keys in the profile (CLAUDE.md: settings in profiles/*.yaml) with core defaults, extend `raw_config_from_profile` (C:config.py:551) to read them. Do not edit v1_2 or v1_9; new keys go in the next version.
- Acceptance test: loader test that resolves each reference and working profile and asserts the printed table equals a golden; a run manifest per live session carrying the same dump.
- Dependencies and risk: owner decision on FEED_DELAYED threshold (1.6); enabling it in a reference profile breaks byte reproduction.
- Completion evidence: the table above produced by code and stored in each result and live manifest.

## 0.2a - Feed-health pieces, specific checks
- Status: PARTIAL
- Exists: see the component inventory. Specific checks (each against code):
  - `time_integrity` correct_host_offset: exists (C:time_integrity.py:39-41, :162-186); estimate = `min(0, 1% quantile)` of receive minus exchange over a plausibility-filtered window of 2,000 (`HostClockAligner`, C:simulation/latency_model.py:137-207). One-sided: cannot represent a host clock AHEAD of the venue (:179).
  - `max_feed_delay_ns` / FEED_DELAYED: exists, default None; strict `>` at C:time_integrity.py:213; checked only in the `elif` after the negative / impossible branches, so an event that is `NEGATIVE_RECEIVE_LATENCY_CORRECTED` skips the delay test (:196-215).
  - `HostClockMonitor`: exists but has no independent reference (1.6).
  - `book_gate`: exists and is decision-changing (1.4).
  - `live_feed` silence limits: book liveness is any frame on the book connection on a monotonic clock, receive timeout armed to `max_book_age` (500 ms) (C:live_feed.py:1815-1860, :2049-2051); trade stream silence 30 s -> `TRADE_FEED_STALE` + reconnect (C:live_feed.py:1345-1348); derivative 30 s.
  - `quality.py`: end-of-run snapshot -> flags; never streaming (C:quality.py:92-140; called from `runner.write_session_artifacts`).
  - `monitoring.py`: per-second snapshot in live (C:live_feed.py:1150), alerts are rows in `pipeline_monitoring.json`, no action.
  - `raw_capture`: hash chain + manifest only; no health content (C:raw_capture.py:176-205).
- Wired into: live and paper (service-side monitor + book gate + `PipelineMonitor`), paper/replay session (decision-side monitor + `TIME_INTEGRITY_BLOCK`), replay (core session only; v1_2's backtester runtime has none of them).
- Effective config per profile: see 1.1.
- Tests and evidence: the 55 passing tests listed above; recorded run: D7d run2 (`pipeline_monitoring.json` health CRITICAL with `DATA_AGE_NS` 2.03 s, `RECONNECTS` 12, `STALE_DISCONNECTS` 12; `time_integrity_report.json`: 659 blocked events, host clock SYNCED; `live_manifest.json`: `strategy_gate` suspend_count 23, resume_count 24, cancel_requests 26, cancelled_orders 0).
- Exact gap: no run exercised an enabled FEED_DELAYED; no boundary test (equal to the limit; limit exactly at max); recovery from FEED_DELAYED is the first non-delayed event, no hysteresis; `cancelled_orders` 0 in the only live evidence, so cancel-on-gate is shown only by unit tests (T:test_book_gate.py:38-90, `T:test_failure_injection.py:74`).
- Implementation: extend the existing modules (see 1.2).
- Acceptance test / Completion evidence: see 1.2 to 1.8.
- Dependencies and risk: none of these switches should be turned on in a reference profile.

## 0.2b - Recorder timestamp fields
- Status: PARTIAL
- Exists:
  - Core live recorder (`RotatingLiveRecorder`, C:live_feed.py:268-710): per wire frame `receive_ts_ns` (wall clock, taken when the coroutine processes the frame, C:live_feed.py:2048), raw frame bytes untouched (`RawFrame.frame_data`, `payload`, C:raw_capture.py:44-65); canonical `MarketEvent` with `exchange_ts_ns` (Binance `E`, Bybit `ts`), `source_ts_ns` (Binance `T`, Bybit `cts`), `receive_ts_ns` (C:models.py:90-107, C:adapters.py:24-37, :64-77); `trace.jsonl` rows with `arrival_ordinal` (C:live_feed.py:491-518); trades keep their own stamps (`trades.jsonl`).
  - ollog public capture (O:capture.py:96-150): book and trade records carry `exchange_ts`, `local_ts` (wall, seconds) and `local_mono_ns` (monotonic); raw frames `[recv_wall_ns, text]` (O:capture.py:54-59); `models.stamp()` takes wall and monotonic back to back (O:models.py:9-11); order records carry send/return wall+monotonic stamps and the sampled clock offset (`clock_sync`, O:runner.py:116-126).
  - Documented dictionary: `docs/time/timestamp_dictionary.json` (source / exchange / receive / observed).
- Wired into: live capture, paper, testnet (core recorder); ollog fillprobe and perp probe (ollog capture).
- Effective config per profile: not profile-driven.
- Tests and evidence: `T:test_raw_capture.py`, `T:test_live_feed.py`; recorded: day1 `intake.json` (5,808 files checked, 0 mismatch; 1,452 parts), `quality_scan.json`.
- Exact gap: (1) the core canonical event and `RawFrame` have NO monotonic receive stamp and no offset estimate or uncertainty; ollog has the monotonic stamp but not in the raw lines (only `[recv_wall_ns, text]`); (2) `capture_notes.jsonl` rows carry only `t_wall` (O:capture.py:116-118); (3) when a venue omits exchange time the adapters silently substitute receive time (C:adapters.py:24, :64) and the dictionary says so, but no flag marks it; (4) `exchange_ts` is in milliseconds (adapters multiply ms by 1e6), so sub-ms ordering is lost; (5) the core receive stamp is taken at coroutine resume, so event-loop lag is folded into "delivery delay" (supported by `silence_analysis_run2.md`: 3 of 10 stalls froze every channel on both venues).
- Implementation: add `receive_mono_ns` to the wire frame and canonical event in the core recorder (extend C:live_feed.py `WireFrame`/`RotatingLiveRecorder`, C:models.py `MarketEvent`) and the offset estimate + uncertainty to the part manifest; add the monotonic stamp to ollog raw lines. Keep the raw payload untouched.
- Acceptance test: record a short mock session; assert every wire frame and canonical event carries wall + monotonic receive stamps, that a missing exchange time is flagged, and that replay ordering is unchanged (T:test_replay_order).
- Dependencies and risk: changing the event schema changes `schema_version` and the replay hashes (golden files, A6's gate list); do it additively.
- Completion evidence: a recorded part where all fields are present plus a parity replay.

## 1.2 - core feed_health.py per venue x symbol x stream
- Status: PARTIAL (no `feed_health.py` exists; many of the measurements exist in other modules)
- Exists, component by component (what each already does and where a new module would overlap):

| Required metric | Exists where | Overlap / gap |
|---|---|---|
| message rate avg, peak | day1 offline scripts `out/integration/day1/event_rate.py` and `quality_scan.py` (results `event_rate.json`: all-kinds p50 60 /s, p99 562, max 3089; trades p99 510 /s). Live: only counters (`raw_frames`, `canonical_events`, `trade_ticks`, C:live_feed.py:118-158) | rate logic lives in `out/` (CLAUDE.md: no logic in out/); no live rate. Move into core |
| book-to-trade ratio | none (counts exist per stream) | trivial from counters |
| delivery delay (receive - exchange - host offset) | `TimeIntegrityMonitor` (C:time_integrity.py:195, `max_corrected_delay_ns`, min/mean/p50/p95 over a 200,000 window :113, :265-285) | exists, per venue, not per stream (book vs trade) and not per symbol; the live service and the session each run their own copy (see Findings) |
| inter-update interval | day1 `quality_scan.json` gaps >0.5/1/5 s per venue and stream; live: `_book_ages` (C:live_feed.py:1820) only as a limit | offline only |
| burst counter, backlog-run detector | `HostClockAligner.is_plausible` rejects backlogged stamps (C:simulation/latency_model.py:185-191; `rejected_backlogged` 934 in run2); `fill_evidence_rebuild/study/fe_prep.py:73-75` counts records with receive minus exchange > 1 s; `FEED_DELAYED` per event | no run detector (consecutive late events), no burst count |
| queue backlog, own processing lag | `feed_age` = session monitor time minus receive (C:time_integrity.py:191, :228-232), STALE_FEED; `StageTimer` (C:session.py:72-97: `feed_to_strategy_ns`, `paper_frame_overhead_ns`); `max_persistence_lag_ns` (C:live_feed.py:491-518) | run2: `feed_age_ns_max` 1.34 s Binance, 1.94 s Bybit; `max_persistence_lag_ns` 1.34 s. No event-loop lag probe (named "NOT built yet" in `silence_analysis_run2.md`); no websocket buffer depth |
| sequence gaps / resets | `BookIntegrityStats` gaps, duplicates, out_of_order, checksum_failures, resyncs (C:orderbook.py:34-48); ollog `bbo_u_regression` (O:capture.py:143-150) | per venue/book only; trade stream has no sequence |
| silence | `book_stream_silences`, `BOOK_STREAM_SILENCE` log (C:live_feed.py:121, :1845-1860); `trade_stale_disconnects` | exists; late firing (0.12 to 2.24 s) |
| errors / reconnects | `reconnects`, `disconnects`, `reconnect_storms`, `RECONNECT_STORM` / `_CLEARED` (C:live_feed.py:1285-1312) | exists, per venue book and trade |
| book validity and recovery | `BookState`, `book_syncs`, `BOOK_STATE` transitions logged (C:live_feed.py:2220-2234), gate suspended seconds by cause | exists |
| book sanity (crossed / stale touch, trades outside touch) | crossed/locked, price jump, wide spread (C:orderbook.py:254-278, `BookSanityPolicy`); `trade_outside_book_band` (C:live_feed.py:2022-2038); `CrossVenueMonitor` (A4) | exists; "stale touch" is only age |
| baselines median/MAD by time of day, versioned file, frozen in replay | none. Nearest: `config/regime_boundaries_*.json` (regime thresholds by hour, A3) | MISSING |

- Wired into: live and paper only (counters and logs); not replay.
- Effective config per profile: see 1.1.
- Tests and evidence: `T:test_load_degradation.py:76-97` (bounded latency memory), `T:test_time_integrity.py`; recorded: D7d run2 `time_integrity_report.json`, day1 `quality_scan.json` and `event_rate.json`.
- Exact gap: no per venue x symbol x stream rollup, no burst or run detector, no queue/loop-lag measurement, no time-of-day baseline, no versioned baseline file.
- Implementation recommendation: EXTEND, do not create a parallel set. Put the per-stream metrics in `time_integrity.py` (already holds the host-offset estimate and per-venue delay stats; add a `(venue, symbol, stream)` key to `_VenueTimeStats`) and surface them through `live_feed.py` and `monitoring.py` (`PipelineMonitor.build` already has a `live` block). A new `feed_health.py` is justified ONLY for the state machine and baselines if the owner wants one place for states (see 1.3). Overlap list if a new file is created: `time_integrity.py` (delay, offset, skew), `monitoring.py` (thresholds, alerts), `quality.py` (counters, flags), `book_gate.py` (validity, suspended time), `live_feed.py` (counters, silence, storm). Ask the owner before creating it (CLAUDE.md rule 3).
- Acceptance test: replay a recorded session (day1 part or run2) through the metric code and compare with the offline `quality_scan.json` / `event_rate.json` numbers; synthetic burst test; frozen-baseline replay is deterministic.
- Dependencies and risk: needs 1.5 (monotonic receive stamp) for loop lag; baselines need several live days (data, not yet recorded); must not change decisions until 1.4 actions are approved.
- Completion evidence: per-stream health rows in the live manifest and replay manifest, and a baseline file with version and hash.

## 1.3 - States HEALTHY / DEGRADED / UNSAFE (+OWN_LAG) per feed
- Status: PARTIAL (the three-state model with hysteresis does not exist; precursors do)
- Exists: five different state vocabularies: `BookState` (C:models.py:18-25), `quality.HealthLevel` HEALTHY/DEGRADED/CRITICAL (C:quality.py:14-17), `monitoring.MonitorHealth` the same three words (C:monitoring.py:16-19), `HostClockMonitor.state` (C:time_integrity.py:70-71), gate venue state ACTIVE / RISK_REDUCING_ONLY (C:book_gate.py:113-114). Duplicate suppression and explicit recovery exist only for: RECONNECT_STORM once per episode and `RECONNECT_STORM_CLEARED` on a VALID book (C:live_feed.py:1285-1312), `FEED_DELAYED` / `FEED_CAUGHT_UP` per transition (C:live_feed.py:1617-1637), `BOOK_STATE` per transition (C:live_feed.py:2220-2234), the gate suspending once (T:test_book_gate.py:55-62).
- Wired into: live and paper logs and `pipeline_monitoring.json`. Not replay.
- Effective config per profile: thresholds are the ones in 1.1; none is a feed state threshold.
- Tests and evidence: `T:test_book_gate.py`, `T:test_market_data_p2.py:251-290`; recorded run2 `structured_logs.jsonl` event counts: BOOK_STATE 29, BOOK_STREAM_SILENCE 12, RESYNC_REQUIRED 12, RECONNECT_SCHEDULED 12, HOST_CLOCK_STATE 5, FEED_DELAYED 0 (feature off); transitions are logged once, not per update.
- Exact gap: no HEALTHY/DEGRADED/UNSAFE/OWN_LAG per feed; no thresholds table; no hysteresis (FEED_DELAYED clears on the first non-delayed event, C:time_integrity.py:216-221; HostClockMonitor state is the LAST sample only: run2 had `max_abs_drift_ppm` 1068 against a 500 limit and ended SYNCED); a `DEGRADED` host clock is not propagated.
- Implementation: one new enum is a sixth vocabulary: ask the owner whether UNSAFE should map onto `BookState != VALID` and `HealthLevel.CRITICAL` (reuse), with OWN_LAG as an added flag on `TimeAssessment`. Put the state machine next to `BookIntegrityGate` (C:book_gate.py) because it already owns transitions, cause strings and suspended time.
- Acceptance test: scripted feed (burst, quiet, loop stall, reconnect) asserting exact transition sequence, one alert per episode, one recovery event, no flapping under a noisy threshold.
- Dependencies and risk: 1.2 metrics; owner approval of thresholds (policy).
- Completion evidence: transition log from a recorded session replay plus the unit scenario.

## 1.4 - Propagation of every transition to strategy layer and alerts; default actions off; no stale quotes live
- Status: PARTIAL
- Exists (verified paths):
  - Book gate -> OMS: `BookIntegrityGate.allow` calls `venue_control_callback` on each venue validity change (C:book_gate.py:44-63) -> `oms.apply_feed_control` sets the venue RISK_REDUCING_ONLY and cancels that venue's non-risk-reducing orders (C:oms.py:683-703; ledger `FEED_CONTROL_STATE_TRANSITION`, `FEED_CONTROL_VENUE_CANCEL`). Wired in live/paper at C:paper_live.py:109-111, :170-172 and in the session at C:session.py:408-411. In the session `gate_allowed` False returns before any strategy evaluation (C:session.py:566-572), live passes `gate_allowed=not service.book_gate.suspended` (C:paper_live.py:187-192). Proven by T:test_book_gate.py:64-90 (unit) and `T:test_failure_injection.py:74`; live evidence run2 `strategy_gate`: 23 suspends, 26 cancel requests, 0 orders cancelled (no resting orders, so the cancel effect is not shown live).
  - Time integrity: blocking flags -> `TIME_INTEGRITY_BLOCK` ledger event and no decision (C:session.py:486-501); counted in the replay manifest (`time_integrity_blocks`, B:session_replay.py:858); run2: 659 blocked events (NEGATIVE_RECEIVE_LATENCY after correction: 657 Bybit, 2 Binance); day1 clean replay: 0.
  - Alerts: `PipelineMonitor` alerts with owner/severity in `pipeline_monitoring.json` and Prometheus text (C:monitoring.py:222-263, :370-395).
- Wired into: live and paper (gate, time block, monitor file); replay (gate and time block in the session runtime only).
- Effective config per profile: no profile switch for these actions; they are unconditional ("default actions off" does not exist as a concept: the gate is always on; FEED_DELAYED and `book_heartbeat` are off).
- Tests and evidence: as above. Run2 `pipeline_monitoring.json` was CRITICAL for the whole run and nothing consumed it.
- Exact gap (each with its evidence):
  1. No event or callback to the strategy layer on a feed transition other than the gate boolean; the strategy sees nothing about FEED_DELAYED / DEGRADED.
  2. No notification channel: the working copy has no Slack/Telegram/email/webhook code (grep over core, backtester and ollog found none); alerting is ownership metadata only (C:alerting.py:39-160).
  3. No incident buffer (ring of events around a transition).
  4. A FEED_DELAYED / `TIME_INTEGRITY_BLOCK` returns before `_withdraw_invalid_quotes` and `_reprice_quotes` (C:session.py:486-501 versus :575-576): resting quotes are NOT cancelled or withdrawn during a delay block. Same for the gate: `gate_allowed` False returns before the quote withdrawal (C:session.py:571-576).
  5. The gate cancels only the invalid venue's orders: with the hedge venue (Binance) stale, `venue_state("BYBIT")` stays ACTIVE (asserted at T:test_book_gate.py:76-77) so a resting maker quote on Bybit stays. Whether another rule covers it is UNVERIFIED (15 s maker lifetime `arb.maker_max_age_s`; quote validity is skipped while the gate blocks). Needs a test.
  6. Book-gate blocks are not ledger decision events (only `TIME_INTEGRITY_BLOCK` is); the replay result carries intervals and seconds instead (1.8).
- Implementation: extend `session.py` (feed-control entry beside `apply_feed_control`) and `oms.py`; a feed-state change callback on `BookIntegrityGate`; a notifier hook in `live_feed._connector_log` (C:live_feed.py:876). Profile keys for actions default off in the next profile version.
- Acceptance test: end-to-end on a recorded session with a resting quote on each venue: inject each transition (hedge-feed stale, loop stall, FEED_DELAYED), assert no quote is live after the transition and one alert is emitted.
- Dependencies and risk: 1.3; cancelling quotes on DEGRADED is a trading-policy change -> owner decision; do not enable by default.
- Completion evidence: the acceptance run's ledger plus alert log.

## 1.5 - Timestamp model: which timestamps exist where
- Status: PARTIAL
- Exists / missing, per required timestamp:

| Timestamp | Exists where | Missing or inconsistent |
|---|---|---|
| exchange event ts | `MarketEvent.exchange_ts_ns` (Binance E, Bybit ts) C:adapters.py:24,64; ollog `exchange_ts` (O:capture.py:127-141) | ms resolution; silent fallback to receive time when absent (C:adapters.py:24, :64) |
| exchange publication / transaction ts | `source_ts_ns` (Binance T, Bybit cts); dictionary says matching-engine time | the delay/offset logic uses `exchange_ts_ns`, not the matching-engine stamp; trades set `source_ts_ns = exchange_ts_ns` (C:market_data_output.py:107-108) |
| local receive ts | `receive_ts_ns` wall (C:live_feed.py:2048); ollog wall + monotonic | core: no monotonic stamp stored; taken at coroutine resume |
| local processing ts | paper rows `processed_ts_ns` (C:paper_live.py:199-203, `paper_timestamps.jsonl`); `StageTimer` wall `perf_counter_ns` (C:session.py:174) | not in the canonical event; replay has no processing time |
| decision ts | `DECISION` row `decision_ts_ns`, `route_decision_ts_ns` (C:session.py:728-741) | wall clock in paper; in replay it is simulated time |
| order send ts | testnet adapter `order_send_ts_ns` (C:sdk.py:229-235); ollog send wall+mono (O:models.py:29-35) | simulated adapters have none |
| venue ack ts | ollog `ex_time_ms`, `ex_event_ms`, `ex_txn_ms` (O:models.py:21-44); callbacks `event_ts_ns` | testnet adapter falls back to `request.created_ts_ns` when the response lacks one (C:sdk.py:248) |
| exchange execution ts | ollog `ex_txn_ms` | core callbacks carry `event_ts_ns` only |
| local receipt ts of acks and fills | adapter `response_ts_ns` (wall, after the synchronous call, C:sdk.py:233); ollog `recv_wall_ns/recv_mono_ns` | sdk adapter is synchronous request/response: private-stream receipt is not separate |
| monotonic vs wall | live freshness is monotonic (C:live_feed.py:793-799, T:test_market_data_p2.py:79-115); ollog stamps both | records, events and OMS use wall only |
| raw never overwritten; corrected + uncertainty stored separately | raw frames and payload are stored untouched (C:raw_capture.py:98-174); the time monitor keeps the estimate in its report (`clock_offset_estimate_ns`, `aligner`) | no per-event corrected value is stored; no uncertainty (the profile `timing.clock` block is `host_offset_ns: 0 / uncertainty_ns: 0 / source ASSUMED`, v1_9.yaml:173-176; ollog `clock_sync` uncertainty is ~85-94 ms RTT/2 for testnet) |
| same semantics in recorder, market data, strategy, OMS, diagnostics, replay | `docs/time/timestamp_dictionary.json` defines `source`, `exchange`, `receive`, `observed` only | the dictionary has no decision, send, ack, execution or processing stamps; collector stamping differs by recorder: core and ollog stamp on receipt; the older collector behind the Jan-2022 data stamped at write time under backpressure (docstring of `HostClockAligner`, C:simulation/latency_model.py:137-159: tail p90 119 s); the MM collector stamps `time.time()` after receipt (`FAST_MM/bybit_market_data_collector.py:256`, MM-side evidence) |

- Wired into: recorder (core, ollog), live service, paper session rows, testnet adapter, replay (receive stamp is the data's own; March stamps are constructed, `trust_local_ts` False).
- Effective config per profile: `trust_local_ts` bybit False, binance False in v1_2, v1_9, v1_9s; `timing.clock` as above.
- Tests and evidence: `T:test_replay_order.py`, `T:test_market_data_p2.py`; D7d run2 `paper_timestamps.jsonl.gz` has EVENT and DECISION rows (record schema at C:paper_live.py:199-203).
- Exact gap: as the right-hand column; the OMS and ledger use a single "ts_ns" of unstated clock.
- Implementation: extend `docs/time/timestamp_dictionary.json` with the missing names, add `receive_mono_ns` (0.2b), and store corrected value + uncertainty next to, not over, the raw stamp (extend `TimeAssessment` C:time_integrity.py:50-63). No new module.
- Acceptance test: a dictionary-vs-code test that every stamp named in the dictionary exists in the recorder row, the canonical event and the paper rows; round trip through replay.
- Dependencies and risk: 0.2b; schema versioning; A5 diagnostics owns send/ack stamps in the OMS.
- Completion evidence: dictionary + the cross-check test green + one recorded session.

## 1.6 - Clock: correct_host_offset validated then enabled; HostClockMonitor; NTP; UNSYNCED -> DEGRADED; FEED_DELAYED threshold; blocks stale data; boundary tests
- Status: PARTIAL
- Exists:
  - `correct_host_offset` is already ON in every profile-derived config (C:config.py:598) and in `research.json`, `perp_research.json`, `paper_watch.json`; there is no validation record that it is correct on live data (run2 below suggests a problem).
  - `HostClockMonitor` has no independent reference. Its only caller feeds it `reference_ts_ns = receive_ts - clock_offset_estimate` (C:time_integrity.py:181-186) so its "offset" IS the estimate, clamped to `<= 0` by `min(0, ...)` (:179). It cannot see a host clock ahead of the venue, it cannot separate network latency from offset (the 1% quantile of receive minus exchange includes the one-way delay floor), and it samples every 100th event (:180). `record_sample` accepts an NTP/PTP/server-time reference by design (docs/time/README.md), but no code supplies one in core. `docs/audit/market_data_audit_final.md:79` records "No NTP query (inferred from venue timestamps)".
  - An exchange server-time sampler exists only in ollog (`server_time_ms`: O:venues.py:165, :314, :460; `sync_clock` picks the lowest-RTT of 5, offset = server - local midpoint, uncertainty = RTT/2: O:runner.py:116-126, every 300 s).
  - UNSYNCED does not block and does not set DEGRADED: `blocking` lists NEGATIVE/IMPOSSIBLE/FUTURE/FEED_DELAYED/PEER_FEED_DELAYED only (C:time_integrity.py:55-63); UNSYNCED is a WARNING log `HOST_CLOCK_OFFSET` (C:live_feed.py:1639-1647).
  - Mac NTP status: `pgrep -x timed` shows the macOS time daemon running; `/Library/Preferences/com.apple.timed.plist` does not exist (system default settings). Actual offset and sync source: UNVERIFIED (needs network `sntp` or admin `systemsetup`, both not allowed here). `chrony` is not installed (no `/etc/chrony*`).
- Wired into: live and paper (service and session monitors), replay (session runtime; the replay's receive stamps are constructed for March, so offset correction and FEED_DELAYED measure the latency model draws, not clock behaviour).
- Effective config per profile: see 1.1 (`max_host_offset_ms` 100, `max_host_drift_ppm` 500, FEED_DELAYED None).
- Tests and evidence: `T:test_time_integrity.py:48-52` (HostClockMonitor states with synthetic "NTP" samples), `T:test_market_data_p2.py:239-248` (delay after offset correction), `:272-287` (host clock far behind raises alert), `T:test_load_degradation.py:52-73` (backlog flagged, no resync storm). Recorded runs:
  - D7d run2 time integrity: Bybit raw latency p50 -36.3 ms, p95 +19.0 ms, max 2,450 ms, `max_corrected_delay_ns` 2.50 s, 97,945 events corrected and 657 still blocked as NEGATIVE_RECEIVE_LATENCY; Binance p50 -5.3 ms, p95 54.7 ms, max 1,713 ms, corrected max 1.76 s, 2 blocked; `host_clock`: 1,555 samples, `max_abs_offset_ns` 75.9 ms, `max_abs_drift_ppm` 1,068 (limit 500), final state SYNCED, `clock_offset_estimate_ns` at the end 0 while the aligner's final host-minus-exchange was +14.5 ms (and run-long p50 was negative). This pattern fits a host-clock change during the run, but I did not diagnose it (UNVERIFIED). With FEED_DELAYED at the shipped 2,000 ms the Bybit 2.50 s excursion would have blocked; at 1,000 ms both venues' excursions would have.
  - ollog REST offsets (server minus local, testnet-measured, all 2026-10-29 to 31): min / median / max (ms) = binance_futures 32.8 / 149.5 / 229.2, bybit_linear 25.2 / 160.4 / 326.8, bybit_spot 25.8 / 89.8 / 233.9, binance_spot 22.8 / 110.9 / 116.8; uncertainty about 84 to 94 ms. Inconclusive for the true offset because the uncertainty is as large as the signal. The `fillprobe_mainnet_20261115T032830Z` `clock_sync` rows are labelled `PAPER-simulated-orders` with offset about 0 and uncertainty 0: simulated client, NOT clock evidence. No real mainnet offset has been recorded.
- Exact gap: (a) no independent reference sampling in the core; (b) no "validated then enabled" record; (c) DEGRADED from UNSYNCED not implemented; (d) FEED_DELAYED threshold not derived from a delay distribution (core stores only max corrected delay and raw latency percentiles; the corrected-delay histogram is not stored); (e) boundary tests missing: delay exactly at the limit (code is strict `>`), offset exactly 100 ms, tolerance at 0; (f) the `elif` chain skips the delay check for corrected-negative events (C:time_integrity.py:196-215); (g) a feed silent for longer than the limit raises no FEED_DELAYED (event-driven only), the book age gate covers it.
- Verify it blocks decisions on stale data: yes for FEED_DELAYED when enabled: `assessment.blocking` -> `TIME_INTEGRITY_BLOCK` -> return None (C:session.py:486-501), shown by T:test_market_data_p2.py:224-237 (own and peer block, then catch-up). End-to-end with the flag on: none recorded.
- Implementation: extend `time_integrity.py`: feed `HostClockMonitor.record_sample` from an exchange server-time poll (reuse the ollog method; poll in `live_feed.py`'s monitor loop, C:live_feed.py:1150), keep the low-quantile estimate as a cross-check, store both and their gap as the uncertainty; add the DEGRADED mapping (1.3); derive the FEED_DELAYED limit from recorded live delay distributions (run2, the day1 recording) not from replays.
- Acceptance test: replayed recorded live session with and without the correction; boundary tests; fault injection of a host step (T:test_replay_order has the 62 ms case) and of a backlog burst.
- Dependencies and risk: 1.2/1.3/1.5; network polling is a new live dependency (not run here); flipping FEED_DELAYED on changes decisions: keep it in new profile versions only, label as policy, owner decision.
- Completion evidence: server-time offset samples with uncertainty stored per session, a delay-distribution report with the chosen limit, boundary tests, one recorded live session showing a FEED_DELAYED block.

## 1.7 - Historical acceptance (April ADA/MNT backlog bursts, 04-23 06:09 restart, 29 Oct and D7d silences)
- Status: PARTIAL (recorded evidence exists for three of four items; no monitor run was made, by design)
- Exists:
  - April ADA/MNT backlog bursts: `out/fill_evidence_rebuild/prep.log` counts, from `study/fe_prep.py:73-75`, depth rows with receive minus exchange > 1 s ("backlog") and trade rows likewise. ADAUSDT: 04-19 99,554 of 234,952 depth rows and 23,227 of 38,094 trades; 04-20 185,795 of 526,085 and 29,718 of 75,673; 04-21 125,917 / 388,935 and 6,424 / 15,275; 04-22 146,781 / 758,790 and 2,556 / 11,004; 04-23 163,025 / 569,294 and 3,947 / 11,514; 04-24 42,695 / 384,852 and 1,684 / 10,353; 04-25 18,751 / 210,080 and 622 / 6,055. MNTUSDT: 04-19 1,171,182 / 2,228,989 and 573,909 / 926,063; 04-20 1,212,766 / 3,023,903 and 458,870 / 988,851; 04-21 437,487 / 1,224,147 and 120,131 / 257,734; 04-22 110,657 / 696,320 and 12,102 / 72,623; 04-23 108,384 / 392,291 and 9,393 / 35,241; 04-24 89,224 / 843,903 and 6,319 / 36,687; 04-25 54,656 / 781,664 and 1,638 / 29,349. These are per-record counts, not burst episodes (no start/end times); `stage_a.log` shows 4,053 of 6,792 orders excluded as "stale/backlogged book". This is an ad hoc filter in a study script, not a feed-health monitor.
  - 04-23 06:09 UTC restart: `out/sim_validation_spot/notes.md:64-76` (update id restarts at 1, book differs after it; ADA last resync snapshot 06:08:58.321, MNT last old update 06:09:03.423 and resync snapshot 06:09:08.302 UTC); exclusion window and its rationale in `out/sim_validation_spot/TOLERANCES.md:48-49` (orders placed 06:00-06:20 UTC and any order resting across the resync; no order of ours was placed 05:00-07:30, so it removes nothing). Detection by a monitor: none run.
  - 29 Oct day-1 recorder session (SOL perps): `out/integration/day1/quality_scan.json` gaps per venue and stream (e.g. Bybit CANONICAL 336 gaps >0.5 s, 93 >1 s, 8 >5 s, longest 8.41 s; Binance CANONICAL 355 / 112 / 13, longest 9.31 s; Bybit TRADE 697 gaps >5 s, longest 29.9 s), `event_rate.json`, `intake.json` (files verified), `mid_gap.json` label "recorder quality REJECTED; host clock UNKNOWN; receive-time clock"; the clean replay `day1/runs/clean_day1_v19/session_manifest.json` records 133 gate suspension intervals with causes and 146 s suspended in the common window (BINANCE:STALE 70.0 s, BYBIT:STALE 75.4 s, GAP_DETECTED 0.6 s) and 0 `TIME_INTEGRITY_BLOCK`. 0.3c (A6) owns the dataset facts.
  - D7d silences (8-9 Nov live paper): `out/integration/paper_live/silence_analysis_run2.md` (22 book-frame gaps of at least 1 s: 12 exchange-side quiets of 2.5-6.5 s that all fired a silence; 10 delivery stalls of 1.05-1.94 s that fired none; in 3 of them every channel on both venues froze, i.e. a client-side stall) plus run2 `structured_logs.jsonl` (12 BOOK_STREAM_SILENCE, 12 RESYNC_REQUIRED) and `live_manifest.json` (6 silences per venue).
- Wired into: n/a (evidence only). The monitor-over-data acceptance is a build-phase task.
- Effective config per profile: run2 used the 1.1 D7d column.
- Tests and evidence: as listed; no test.
- Exact gap: no recorded feed-health verdict on any of these periods; April has no gap/burst episode list; the 04-23 restart is not detected by code (a sequence reset `u == 1` is handled by Bybit book logic but no event is raised); silence timer lateness not fixed (`book_heartbeat_seconds` off).
- Implementation: when 1.2/1.3 exist, run the monitor over these exact windows (read-only) and store verdicts under `out/integration/`.
- Acceptance test: monitor reports UNSAFE for the 04-23 06:08-06:10 window, backlog runs on ADA/MNT 04-19/20, STREAM_QUIET vs LOOP_STALL classes for run2's 22 gaps; no UNSAFE in clean windows.
- Dependencies and risk: A6 datasets 0.3a to 0.3c; April data is spot (not the perp book), so only feed-level claims transfer.
- Completion evidence: verdict files for each window.

## 1.8 - Wiring: replay preflight, recorder health manifest, live/paper -> book gate + notifications + incident buffer
- Status: PARTIAL
- Exists:
  - Replay preflight: `quality_preflight.run_preflight` is called by `CrossVenueFeed` with `quality_preflight=True` in both the backtester runtime (B:backtester.py:2895) and the session runtime (B:session_replay.py:330-332); a CRITICAL gate aborts unless a justification override is supplied (`quality_report_only`), tested by `T:test_new_backtest_preflight.py:36-77`. It scans whole days and counts price jumps, wide spreads, invalid levels, crossed books, stale intervals (>30 s no event), silent feeds (B:quality_preflight.py:44-47); it does NOT track sequence gaps or latency (B:quality_preflight.py:49-52, header lines 14-20). It produces no excluded windows.
  - Excluded windows and reasons in the result: the session replay manifest carries `invalid_book_policy` with `suspension_intervals_ns` as (start, end, cause) triples, `suspended_seconds_by_day` per cause, every resync with its reason (C:session.py:652-665) and `coverage.excluded_span` (B:session_replay.py:652-654, :849-855). Present in `day1/runs/clean_day1_v19/session_manifest.json` (133 intervals, resyncs by source DATA_SNAPSHOT 3). The backtester runtime (v1_2) writes `book_only_ns_by_day` and coverage, not core intervals.
  - Recorder health manifest per file: `live_part_manifest.json` per part has file sha256, `stage_counts` recorded vs on disk with a consistency flag (C:live_feed.py:636-656); `raw_manifest.json` per part (C:raw_capture.py:176-205); verified end to end for day1 by `intake.json` (1,452 parts, 5,808 checks, 0 mismatch). ollog: `recon_checks.jsonl` (529 rows in the 15 Nov mainnet run) and `capture_notes.jsonl` (0 rows in that run).
  - Live/paper -> book gate: yes (1.4). Notifications and incident buffer: none (1.4).
- Wired into: replay (preflight and manifest), live and paper (gate, monitor file), recorder (integrity manifests).
- Effective config per profile: see 1.1; preflight thresholds `_BACKTEST_THRESHOLDS` are lenient backtest defaults (B:quality_preflight.py:55-79).
- Tests and evidence: `T:test_new_backtest_preflight.py`, `T:test_preflight_cache.py`, `T:test_data_quality_enforcement.py`, `T:test_book_gate.py`; recorded: day1 manifest and intake, run2 `live_manifest.json`.
- Whether enabled gates change decisions, not only log: book gate YES (suspends strategy and cancels, 1.4); `TIME_INTEGRITY_BLOCK` YES (blocks decisions, ledger event); quality gate YES for replay start (rejects data); `PipelineMonitor` alerts NO (file only; run2 CRITICAL had no effect); `CrossVenueMonitor` divergence NO by design (report only; run2: 1,416 cross-venue skew breaches, none blocking); recorder health: observation only.
- Exact gap: (1) the recorder manifest has integrity but no health (rates, gaps, delay stats, silence, reconnects per file); (2) replay results carry gate intervals but no feed-health verdicts or preflight excluded windows; (3) no notification and no incident buffer; (4) `paper_summary.json` `book_integrity_gate` in run2 shows `INITIAL_SYNC`, 0 transitions, while the gate that acted is `live_manifest.service.strategy_gate` (23 suspends): live passes `gate_allowed` from the service so the session's own gate is never evaluated, and its summary block is misleading.
- Implementation: add the health block to `live_part_manifest.json` (extend `RotatingLiveRecorder._close_part`, C:live_feed.py:601) from the 1.2 metrics; add feed-health verdicts to `book_policy_report` (C:session.py:652); fix the `paper_summary` block to report the service gate; notification hook per 1.4.
- Acceptance test: replay of a recorded part reproduces the manifest numbers; a seeded live session shows a notification and an incident file.
- Dependencies and risk: 1.2 to 1.4; manifest format change needs a schema bump.
- Completion evidence: per-part health in a real recording and a result file listing excluded windows with reasons for a v1_9 run.

## Findings that need the owner
1. FEED_DELAYED is OFF everywhere that matters: v1_9, v1_9s (through the loader), the D7d paper config and every profile-derived config. It is ON (2,000 ms) only in `config/research.json`, `perp_research.json`, `paper_watch.json`, which no recorded live run used. D7d run2 had a Bybit corrected delay of 2.50 s that passed unblocked. Threshold choice is a policy decision (1,000 ms vs 2,000 ms; max_book_age is 500 ms).
2. `HostClockMonitor` is circular and one-sided (1.6): it reports SYNCED while run2 shows a 1,068 ppm drift sample and 659 blocked negative-latency events. No real offset measurement against a server clock exists for the Mac; the 15 Nov mainnet `clock_sync` rows are simulated (PAPER client). Mac NTP status UNVERIFIED (a `timed` process is running).
3. Replay acceptance of correct_host_offset or FEED_DELAYED on March 2024 measures the latency model, not the feed (`trust_local_ts` False, receive times constructed). Acceptance needs recorded live sessions (the 29 Oct recording, the D7d runs).
4. Same-job overlap (CLAUDE.md rule 4): the paper path runs TWO `TimeIntegrityMonitor` instances with one policy: the service copy (log-only, called with `now_ns = receive_ts` so feed age is always 0 and STALE_FEED can never fire there, C:live_feed.py:1617-1619) and the session copy (decision-blocking, `now = clock_ns()`, C:session.py:486). Also five state vocabularies (1.3) and offline rate/gap logic in `out/integration/day1/*.py` (CLAUDE.md: no logic in out/). I recommend EXTEND `time_integrity.py` and `live_feed.py`, not a new `feed_health.py`, unless you want one home for the state machine; I need your answer before any new file.
5. Safety gaps in 1.4 that are policy-relevant: quotes are not withdrawn during a FEED_DELAYED block or a gate suspension; the gate cancels only the invalid venue's orders, so a Bybit maker quote survives a stale Binance hedge feed (UNVERIFIED whether another rule covers it). Any cancel-on-DEGRADED is a trading-policy change.
6. D7d run2 pipeline monitoring was CRITICAL for the run and nothing acted on it; there is no notification channel or incident buffer in the working copy.
7. `paper_summary.json` `book_integrity_gate` reports a gate that never ran (INITIAL_SYNC, 0 transitions); the acting gate is `live_manifest.service.strategy_gate`. Misleading for any reader of the summary.
8. The order text "29 Oct D7d silences" mixes two events: D7d live paper runs are 8-9 Nov; 29 Oct is the day-1 recorder session. Both have evidence (1.7). April ADA/MNT "backlog bursts" exist only as per-record counts from a study script, with no episode list.
9. Silent substitution of receive time when a venue omits the exchange timestamp (C:adapters.py:24, :64), and the testnet adapter's `event_ts_ns` fallback to the creation time (C:sdk.py:248), can hide a missing stamp; they should be flagged (0.2b/1.5).
10. `config.yaml` and `config.testnet.example.yaml` do not load with `load_config` and no module reads their `data_quality` keys: confirm they are legacy and can be moved to `discarded/` (CLAUDE.md: never delete).
11. Unsafe to enable without a decision: FEED_DELAYED, `book_heartbeat_seconds`, `rest_reconcile_seconds`, any cancel-on-state action, baselines in replay. None was enabled by this audit.

## Returned IDs
0.2a, 0.2b, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8
