# Deliverable B: architecture and data-flow map (16 Nov 2026)

1. Source: the Phase 0 audit (95 rows: 82 PARTIAL, 11 MISSING, 2 UNVERIFIED, 0 VERIFIED_COMPLETE) plus my own read-only checks of call paths on the working copy (outer 3e0f8bb, core 0436b3a).
2. The map has 11 stages, capture to replay, each with modules in the working copy; the audit found 4 missing stages (incident capture, dislocation/reference price, health-to-strategy notification and alert delivery, one synchronized report) and 2 advisory-only pieces (book risk / depth quality, routing evidence).
3. CLAUDE.md's "two parallel stacks" table is STALE: 28 `new_backtest` files are now `sys.modules` shims onto the core (section 3). The real divergence is two runtimes, two accounting sets, two session builders, and live-only / replay-only components.
4. The core is being edited right now by the build session (6 core files modified in the working tree, 07:00 IST). Line numbers below are against core 0436b3a / outer 3e0f8bb unless marked WT (working tree, may drift by a few lines).
5. Mode wiring is stated per stage: live = public-feed service; paper = `LivePaperSession`; testnet = ollog probes plus `TradingSession(VENUE_TESTNET)` (tests only); replay = `SessionReplay` and the old loop. The live TRADING service is UNVERIFIED (not in the audited paths, A2 2.1).

Notation: C: = `crypto_MFT_arb_platform/src/crypto_stat_arb/`, B: = `src/new_backtest/`, O: = `order_lifecycle_logger/ollog/`, OI: = `out/integration/`. IDs (1.4, 2.2, ...) are audit rows; A1..A6 are the area files in this folder. UNVERIFIED = not checked or not checkable here.

## 1. Plain-text data flow

```
 Binance USD-M / Bybit linear: public WS + REST          (private streams: testnet adapter only)
        |
 [1 CAPTURE]   C:live_feed.py WireFrame(:102) -> RotatingLiveRecorder(:268): wire / raw / canonical /
        |        trades / derivatives / trace per part + live_part_manifest.json (_close_part :601)
        |        also C:raw_capture.py (hash chain), O:capture.py (ollog probes, recv_wall + recv_mono)
        |   MarketEvent: exchange_ts_ns, source_ts_ns, receive_ts_ns (wall), processing_ordinal
        v
 [2 HEALTH]    C:time_integrity.py TimeIntegrityMonitor(:134) + HostClockMonitor(:67); C:book_gate.py
        |        BookIntegrityGate(:17); C:quality.py; C:monitoring.py PipelineMonitor; C:alerting.py (ownership only)
        |   TimeAssessment flags (FEED_DELAYED, NEGATIVE_RECEIVE_LATENCY[_CORRECTED], STALE_FEED), BookState, HealthLevel
        |   -- two TimeIntegrityMonitor copies (service log-only, session decision-blocking) --
        v
 [3 BOOK]      C:orderbook.py OrderBook (live book, BookState, snapshot_hash)   C:simulation/order_book.py (sim book)
        |   book view per venue; book age (live: monotonic, _book_alive_mono_ns)
        v
 [4 STRATEGY]  C:session.py TradingSession.on_event(:467): time gate -> book gate -> C:strategies/arb_strategy.py
        |        CrossVenueArbStrategy(:346) + C:strategies/edge.py (the one edge computation)
        |   ArbSignal / OPPORTUNITY row, DECISION row (decision_ts_ns, route_decision_ts_ns)
        v
 [5 ROUTING]   C:ems/layer.py ExecutionLayer.set_routing(:160) / decide_route(:167) -> C:ems/venue_selector.py
        |        assign_rest_hedge (fixed rest/hedge; dynamic + split unwired)   journal capped JOURNAL_MAX=200_000
        v
 [6 OMS]       C:oms.py OMS(:248): plans, children, risk reservation (C:ems/risk.py, risk_gate.py), rate limits
        |        (C:rate_limits.py), apply_feed_control, request_amend, advance_time (event-time timers)
        v
 [7 EXECUTION] C:execution.py VenueExecutionAdapter(:493): DeterministicVenueAdapter(:502) /
        |        C:simulation/queue_adapter.py QueueAwareVenueAdapter(:49) [paper + replay, simulated venue]
        |        C:sdk.py TestnetVenueAdapter(:193) via C:testnet.py build_testnet_adapters(:826) [testnet only]
        v
 [8 HEDGING]   C:hedge_lifecycle.py HedgeLifecycle(:113) + OMS _hedge_* (WT :3696+), C:circuit_breaker.py,
        |        C:ems/ioc_policy.py + ioc_runtime.py; unwind / flatten; reservation of risk
        v
 [9 LEDGER]    C:ledger.py Ledger(:90), C:economics.py EconomicLedger(:197), C:ems/reconcile.py (shadow ledger,
        |        count mode), C:funding.py, C:margin.py, C:fees.py; session marks C:session.py _margin_marks(:1379)
        v
 [10 DIAGNOSTICS] C:metrics.py (markouts, pair_economics :475, shortfall_split :549 [no run calls it], DICTIONARY :48),
        |        C:session_audit.py, C:cycle_reports.py, B:session_replay.py write_result(:973); out/ wrappers (logic in out/)
        v
 [11 REPLAY]   C:replay.py event_order_key(:9) / replay_order(:33); B:session_replay.py SessionReplay.run_recorded(:355)
                 + iter_recorded_events(:1196); old loop B:backtester.py (reference profiles); scripts/compare_baselines.py

 MISSING STAGES (no module):  [a] incident capture (2.2)   [b] dislocation + reference price (6.3, 6.4)
                              [c] health -> strategy notification and alert delivery (1.4)   [d] one synchronized report (10.4)
 ADVISORY ONLY (never reaches an arb decision):  book_risk / depth_quality (3.2, 0.2e), choose_tactic (0.2d), routing evidence (8.1)
```

## 2. Stage by stage

### Stage 1: capture
- Modules: C:live_feed.py (`WireFrame` :102, `RotatingLiveRecorder` :268, `LiveFeedSettings` :161), C:raw_capture.py (`RawCaptureWriter`), C:adapters.py (normalisers), C:models.py `MarketEvent`, C:market_data_output.py; ollog capture O:capture.py (testnet/mainnet probes, fast streams); the legacy MM collector (MM-side evidence, cited through A1 1.5 and A6 f10, not read by me). The VM recorder that wrote the 29 Oct capture is NOT in the repo (UNVERIFIED).
- In: exchange frames. Out: per-part wire.jsonl (hash chain, terminal hash), raw.jsonl, canonical.jsonl, trades.jsonl, derivatives.jsonl, trace.jsonl, quarantine.jsonl, `live_part_manifest.json` (per-file sha256, stage counts), rotation 3600 s / 256 MiB.
- Timestamps carried: `exchange_ts_ns` (ms resolution), `source_ts_ns` (matching engine), `receive_ts_ns` (wall, taken at coroutine resume so loop lag is folded in), `processing_ordinal`. Not carried in the core: monotonic receive stamp, offset estimate, uncertainty (1.5, 0.2b). ollog has recv_wall + recv_mono but raw lines hold only `[recv_wall_ns, text]`. Silent substitution of receive time when the venue omits an exchange stamp (C:adapters.py:24, :64; testnet C:sdk.py:248).
- Wiring: live yes (service); paper yes (`LivePaperSession` builds the service, C:paper_live.py:106); testnet: ollog only; replay reads captures (`iter_recorded_events`).
- Breaks: 0.2b, 1.5 (stamps); 1.8 (manifest has integrity but no health); 2.1 (full-session recorder, not rolling; ledger, order trace, canonical events and intent journal written only at `finalize`, C:paper_live.py:258); 2.2 (no trigger or window capture, MISSING); M1 (MM recorder not on this path, MISSING).

### Stage 2: health
- Modules: C:time_integrity.py (`TimeIntegrityMonitor` :134, `observe` :188, `HostClockMonitor` :67), C:book_gate.py, C:orderbook.py `BookSanityPolicy`, C:live_feed.py (silence, reconnect storm, trade sanity), C:quality.py (APPROVED/REJECTED, HealthLevel), C:monitoring.py (`PipelineMonitor`, 9 metrics), C:alerting.py (ownership matrix only, no delivery).
- In: events, book state. Out: `TimeAssessment` (flags incl. FEED_DELAYED, STALE_FEED), gate verdict, `pipeline_monitoring.json`, `time_integrity_report.json`, ledger `TIME_INTEGRITY_BLOCK`.
- Timestamps: `now_ns` (service: = receive_ts, so feed age is always 0 and STALE_FEED cannot fire there, C:live_feed.py:1617-1619; session: `clock_ns()`, C:session.py:486); monotonic book-alive clock (C:live_feed.py:1815).
- Wiring: live and paper (service copy log-only + gate + monitor file); paper and replay session copy blocks decisions (C:session.py:486-501); replay has the gate and the time block in the session runtime only. FEED_DELAYED is OFF in v1_2, v1_9, v1_9s and the D7d config; ON (2,000 ms) only in three JSON configs no recorded run used (A1 finding 1).
- Breaks: 1.2 (no per venue x symbol x stream rollup), 1.3 (no HEALTHY/DEGRADED/UNSAFE/OWN_LAG, no hysteresis; five state vocabularies), 1.4 (strategy sees only a gate boolean; no notification channel; quotes not withdrawn during a time block or gate block, C:session.py:486-501 vs :575-576; hedge-feed-stale case UNVERIFIED), 1.6 (HostClockMonitor circular, one-sided; reported SYNCED with a 1,068 ppm drift sample), 1.7 (no verdict on April / 04-23 / 29 Oct / D7d windows), 1.8 (`paper_summary.json` `book_integrity_gate` is the session gate that never ran; the acting gate is `live_manifest.service.strategy_gate`). Two same-job monitors (service and session): extend, do not add a third.

### Stage 3: book
- Modules: C:orderbook.py `OrderBook` (live; `BookState` SYNCING/VALID/STALE/GAP_DETECTED/CHECKSUM_FAILED/PRICE_ANOMALY/INVALID_DATA; `snapshot_hash` :350), C:simulation/order_book.py (simulated book; `new_backtest/order_book.py` is a shim), C:rest_reconcile.py (off by default), C:simulation/venue_state.py (`VenueState`: depth_quality, survival, flicker, toxic flag; shim in B:), `queue_v2/` (C:simulation/queue_v2).
- In: canonical events. Out: book views, `BookState`, gate input; evidence fields (depth_quality) in replay journal only.
- Timestamps: event receive time; book age.
- Wiring: live/paper yes (OrderBook); replay yes; `depth_quality` provider replay only and OFF in v1_2, v1_9, v1_9s (C:simulation/venue_state.py:42); paper builds a default `VenueState` with no provider (C:paper_live.py:126).
- Breaks: 0.2e / 3.x (observation only, thresholds ASSUMED, ADA tick defect, `_depth_evidence` dead code), 3.2 (`book_risk` MISSING), 4.1 / 4.2 (resilience unmeasured, tau one assumed number), 5.1 (MarketState fields hard-set to 0/False; VPIN absent: `vpin` exists in C:simulation/models.py:69, never filled). Two book classes (live `OrderBook`, simulated `OrderBook`) in one core: a book fix in one does not reach the other.

### Stage 4: strategy
- Modules: C:session.py `TradingSession` (:162, `on_event` :467), C:strategies/arb_strategy.py (`CrossVenueArbStrategy` :346, `requote_target` :488, `ArbSessionStrategy` :1257), C:strategies/edge.py, C:ems/tactics/passive.py (`decide_reprice`, S0), B:requote.py (simulator glue for S0), C:cross_venue.py `CrossVenueMonitor` (:60; live service only), C:valuation.py (marks), C:session.py `_margin_marks` (:1379, the 5 s last-good-mark rule).
- In: events + books + derivative ticks (`on_derivative` :1485). Out: OPPORTUNITY, DECISION rows, quotes (maker) and hedge plans.
- Timestamps: `decision_ts_ns`, `route_decision_ts_ns` (C:session.py:728-741; wall in paper, event time in replay); processing wall via `StageTimer` (reported only in `latency_report`, C:session.py:1545).
- Wiring: replay (v1_5+), paper, testnet (tests). `CrossVenueMonitor` live service only, not in `TradingSession`, not in replay, no strategy reader (A4 0.2g). `block_on_toxic` False in v1_7..v1_9s, True in v1_2 (A3).
- Breaks: 6.2-6.5 / Q4 (episodes, executable price, classes, independent reference price: MISSING), 6.4 (re-stamp makes a 5 s mark usable up to about 10 s, C:session.py:1393-1394; `MarginConfig.missing_price_action` default "breach" C:margin.py:186 vs builder "pause" C:config.py:604), 7.1 (vwap with nonzero slippage double count, no guard), 7.2 (pre-trade estimate not persisted as one record), 1.4 (no feed-state input), 0.2d (reject trace off by default, not a profile key, no inputs).

### Stage 5: routing
- Modules: C:ems/layer.py (`ExecutionLayer`, `set_routing` :160, `decide_route` :167, `suggest_tactic` :152, `JOURNAL_MAX` :31), C:ems/venue_selector.py (`assign_rest_hedge`, `split_*`), C:ems/tactics/base.py (`choose_tactic` :145, `MarketState`), C:ems/ioc_policy.py + ioc_runtime.py (possible second chooser: UNVERIFIED overlap, A5), C:ems/config.py (refuses `tactic_choice` with maker_taker).
- In: role, passive flag, order venue. Out: `RouteDecision` (fixed rest/hedge; counts only for normal decisions), journal records `intent / state / child / retarget / route_decision_differs / route_rejected`.
- Timestamps: journal `ts` per record; `created_ts` on intents.
- Wiring: fixed routing in replay, paper, testnet (one `TradingSession`); evidence provider installed only at B:session_replay.py:295 (replay); `choose_tactic` called by no run path (arb pairing refuses it).
- Breaks: 0.2i (the one wired call passes zero costs and drops `alternatives`), 8.1-8.4 (no per-decision record, mutable list, silent cap, no versions, no hash chain), 8.2 / 8.3 (no routable-order caller; no reroute), W13-A, W13-B7 (policy not one table).

### Stage 6: OMS
- Modules: C:oms.py (`OMS` :248; `apply_feed_control` WT :695; `request_amend` WT :2896; `advance_time` :2424; `write_execution_trace` WT :736), C:order_state_machine.py (spec; B: shim), C:risk.py, C:ems/risk.py + risk_gate.py, C:rate_limits.py, C:ems/intent.py. Working tree: the dust-carry diff (oms.py +321 lines, `HedgePolicy.dust_carry` in hedge_lifecycle.py) is uncommitted.
- In: intents from the layer, quote requests, feed-control callbacks. Out: orders and child orders, ledger events, `order_trace.jsonl`, `order_timestamps.jsonl`.
- Timestamps: OMS and ledger use one `ts_ns` of unstated clock (1.5); `created_ts_ns`; timers fire from event time.
- Wiring: replay, paper, testnet (all `TradingSession`). The old loop B:backtester.py does NOT use this OMS (section 3).
- Breaks: 1.4 (`apply_feed_control` only from the book gate), 2.1 (trace written at finalize), 12.1 (no per-chain hedge start / complete stamp), A6 finding 4 (MM-side defects C1 `request_amend` without a book, C2, C3, C4: status on the integration branch UNVERIFIED; C1 matters for S0).

### Stage 7: execution (venue adapters)
- Modules: C:execution.py (`CanonicalOrderRequest` :143, `DeterministicVenueAdapter` :502, `StrategyDirectedRouter` :821), C:simulation/queue_adapter.py (`QueueAwareVenueAdapter` :49, `SimulatedVenueFeeder` :543), C:simulation/fill_engine.py (maker fills; a fill needs a real trade), C:simulation/latency_model.py + timing.py (latency preset `singapore`), C:sdk.py `TestnetVenueAdapter` (:193), C:testnet.py transports (Binance futures, Bybit linear), O: probes (own runner, Keychain keys). Older core path C:maker_sim.py, C:maker_engine.py, `sim_engine/` (C++) used by `simulate-maker` and the earlier paper path (NOT by the session replay).
- Timestamps: in simulated adapters venue ack / fill times are model draws added to the host clock (12.1); testnet adapter stamps `order_send_ts_ns`, `response_ts_ns` (synchronous call, so private-stream receipt is not separate, C:sdk.py:229-248); ollog stamps send wall + mono and exchange times.
- Wiring: replay and paper: simulated adapter; testnet: `TradingSession(VENUE_TESTNET)` in tests only (CLI refuses, C:cli.py:113-120), no recorded bot run (12.3); live: UNVERIFIED.
- Breaks: 12.3 (no recorded bot run on testnet), 12.1 (paper times are modelled, not measured), perp queue model unvalidated (PERP_QUEUE_VALIDATION_PLAN.md:17: checked on Bybit SPOT only).

### Stage 8: hedging
- Modules: C:hedge_lifecycle.py (`HedgePolicy` :87, `HedgeLifecycle` :113), OMS `_hedge_for_maker_fill`, `_hedge_try_dispatch`, `_hedge_cap_value` (WT), C:circuit_breaker.py (net-exposure timer, dust rule), C:ems/ioc_runtime.py, hedge faults C:simulation/hedge_faults.py + fault_presets.py (replay).
- In: maker fills. Out: HEDGE_* ledger events, unwinds, breaker alarms, `session_safety_events.jsonl` (written by `OI:day1/run_day.py`, not by `write_result`).
- Wiring: replay, paper, testnet (tests). Wiring `book_risk` into hedge dispatch is an OPEN owner decision (policy).
- Breaks: 0.2d (safety and limits files exist only when the caller is `run_day.py`), 9.1 (no schedule mode in core), W13-B6 (no live exposure display), A3 finding 1 (depth-quality and `choose_tactic` never reach the hedge).

### Stage 9: ledger and economics
- Modules: C:ledger.py, C:economics.py (`EconomicLedger`, `reconcile`, `strict_bridge`), C:economics_gate.py, C:ems/reconcile.py (shadow ledger from the intent journal, mode `count`), C:funding.py, C:margin.py, C:fees.py, C:valuation.py (common-timestamp equity). Reference-profile accounting in B: (`funding_settler`, `margin_model`, `core_accounting.py` bridge, fee presets in B:config.py).
- Timestamps: ledger `ts_ns` (replay clock = receive time; host clock UNKNOWN on the 29 Oct capture).
- Wiring: paper / legacy runner path runs `economics.reconcile` + `economics_gate` (C:runner.py:238); the session replay does NOT (no `economic_reconciliation.json` in replay dirs; replay reconciles to the shadow ledger only). The manifest `equity_at_book_mids` is a last-mark figure, not P&L (docs/strategy_contract.md:83-88).
- Breaks: 0.2h, 7.1 (not reconciled on replay), recorded `exec_reconciliation ok: false` in four manifests (A4 finding 6; cumulative-vs-delta fee signature, likely fixed at C:ems/reconcile.py:140; needs a re-run: UNVERIFIED), W13-C6 (`reconcile_mode` default `count`), 29 Oct margin checks priced from MARK_FROM_MID (809,026 ticks, 0 exchange marks), no funding file for March.

### Stage 10: diagnostics
- Modules: C:metrics.py (`DICTIONARY` :48, `lifecycle_report` :363, `pair_economics` :475, `shortfall_split` :549, `implementation_shortfall` :623, `exposure_steps` :681), C:session_audit.py, C:cycle_reports.py, C:tca.py; B:session_replay.py `write_result` (:973); five or more report builders, mostly in `out/` (`OI:day1/run_day.py`, `build_report.py`, `build_matrix.py`, `build_rehearsal_report.py`, `phase_b/strategy_quote_drift.py`) with hard-coded SOL / Bybit / Binance / 2026-10-29 and `LOT = 0.01` (10.8).
- Timestamps: markout horizons 1 / 5 / 30 s; `processed_ts_ns` (paper); stage timers (wall); pair join by `created_ts` within 1e-4 s, not an id (C:metrics.py:503).
- Wiring: replay writes markouts, pair economics, shadow reconcile; `metrics_report.json`, `tca_report.json`, `economic_reconciliation.json` only on the paper / runner path; shortfall split wired into NO run.
- Breaks: 7.2-7.5, 10.1-10.8 (10.4 chart and 10.7 self-check page MISSING), Q1 (no real order with pre-trade fields; the +8.15 / +3.83 figure is a replay result), Q2 (= 8.4), 10.3 (two conflicting favourable / adverse definitions), W13-C.

### Stage 11: replay
- Modules: C:replay.py (`event_order_key` :9, `replay_order` :33; ordering is a pure function of recorded data), B:session_replay.py (`SessionReplay` :92, `run_recorded` :355, `iter_recorded_events` :1196, `compare_parity` :1054, `live_paper_setup` :1147), B:backtester.py (3,576 lines, the old loop for profiles with no `runtime:` key), B:batch_backtest.py, B:profiles.py (`release_identity` :484), scripts/compare_baselines.py (backtester-loop file set only), C:calibration.py `compare_runs` (:266).
- Timestamps: the session clock is the event `receive_ts_ns` (C:session.py:460); only wall read is `StageTimer` (kept out of outputs); randomness seeded in code (7, 11, queue seeds; not in profiles).
- Wiring: replay only. Determinism observed (H1, five identity runs byte-identical on 13 of 14 files) but no tool, no input hash, no output hash in the manifest (0.2c, 2.5).
- Breaks: 2.3 / Q3 (no capture window to replay), 2.4 (no interlock; `write_result` can write to any dir), 2.5 (hashes), 12.2 (parity only run once, v1_5, 1 trade; 860 vs 837 ledger rows unexplained), Q5 (no merged cross-clock timeline).

## 3. The two stacks today (CLAUDE.md map is stale)

Facts (my check, 16 Nov): of the new_backtest files, 28 are 5-line `sys.modules` shims onto the core (arb_strategy, edge, circuit_breaker, valuation, order_state_machine, venue_semantics_perp, fault_presets, fill_engine, hedge_faults, lambda_regime, latency_model, models, order_book, postonly, timing, venue_state, all of `execution/` (7) and `queue_v2/` (5)). The CLAUDE.md sentence "new_backtest uses only crypto_stat_arb.quality" no longer holds (B:session_replay.py:30-34, B:backtester.py:27-28 import the core). The core imports `new_backtest` only by a string given at run time (C:cli.py:131-132, help text :349 names `new_backtest.session_replay:live_paper_setup`): a coupling that breaks "the core never imports new_backtest".

| Layer | Shared (a core fix reaches both) | NOT shared (a fix in one does not reach the other) |
|---|---|---|
| Simulation, strategy, EMS | the 28 shimmed modules: a core simulation or strategy edit ALSO changes the old loop that runs v1_2 and legacy_v0 | none for those files |
| Runtime | none | `TradingSession` + `OMS` + `ExecutionLayer` (C:session.py, C:oms.py) serve profiles with `runtime: session` (v1_5..v1_9s; v1_10 draft) in replay and paper; `B:backtester.py` (own loop, `MakerExecutor` / `TakerExecutor`; v1_2 has no execution layer, A5) serves profiles with no `runtime:` key (legacy_v0, v1, v1_1, v1_2). v1_3 and v1_4 also run on the old loop: no `runtime:` key, and a read-only `load_profile` call (16 Nov) returned runtime None for v1_2, v1_3, v1_4 and "session" for v1_5; `new_backtest/profiles.py:74-81` makes "session" a WORKING-profile-only runtime. An edit to oms.py or session.py never reaches v1_2; an edit to backtester.py never reaches paper |
| Accounting | v1_4 and later use core fees / funding / margin / ledger (`fee_source` ... `core`). v1_4 reaches them INSIDE THE OLD LOOP through B:core_accounting.py (imports C:fees, ledger, funding, economics, margin, risk); v1_5+ through `TradingSession`. So a fix in C:fees.py, C:funding.py, C:margin.py, C:ledger.py reaches v1_4 and every session profile | reference profiles (legacy_v0, v1, v1_1, v1_2, v1_3) keep B:funding_settler.py, B:margin_model.py and B:config.py fee presets; a core accounting fix does not reach them (by design: byte reproduction) |
| Session builder | none | `SessionReplay.__init__` (B:session_replay.py:243-253: intent journal, breaker, execution config, layer risk mode, resync, markout, observations, `profile_safety`) vs `live_paper_setup` (:1147-1186) which omits them: a v1_9 paper run would not run the profile's safety values (A2 0.2d, 12.2) |
| Output set | none | replay writes `write_result` files; paper writes `runner.write_session_artifacts` (ledger.jsonl, economic_reconciliation.json, tca_report.json, metrics_report.json). Different file sets for the same session |
| Live-feed-only | none | `CrossVenueMonitor`, the service-side `TimeIntegrityMonitor` (log-only, age 0), recorder part manifests, `PipelineMonitor`: never in replay |
| Replay-only | none | routing evidence provider (B:session_replay.py:295), `intent_journal` (:251), `MARK_FROM_MID` (:96,:136-142), `depth_quality` provider (:290-295), reject trace, `exec_reconciliation` shadow ledger |
| Old paper / maker sim | none | C:maker_sim.py, C:maker_engine.py, `sim_engine/` (C++), used by `simulate-maker` and the earlier paper path; not by the session replay |
| Data quality / manifest / release | none | B:quality_preflight.py vs C:quality.py; B:data_manifest.py vs C:dataset_manifest.py; B:profiles.py `release_identity` vs C:release.py |
| Duplicates inside the core | none | live `OrderBook` (C:orderbook.py) vs simulated `OrderBook` (C:simulation/order_book.py); two `MarketState` classes (simulation vs ems); two "flicker" meanings; `compare_parity` (B:) vs `calibration.compare_runs` (C:) both do parity |

Consequence for every phase in Deliverable C: a core edit is not "for the new profile only". It lands in the old loop through the shims and can move the bytes of frozen v1_2 and legacy_v0. Every phase therefore needs the identity gate on the reference profiles, not only on v1_9.

## 4. Mode wiring matrix

| Stage | live (public feed service) | paper | testnet | replay |
|---|---|---|---|---|
| 1 capture | yes (VM recorder not in repo) | yes, full session | ollog probes | reads captures |
| 2 health | service monitor + gate | service + session monitors | adapter only | session monitor + gate; March stamps constructed |
| 3 book | OrderBook | OrderBook | probe book (ollog) | OrderBook + queue_v2 |
| 4 strategy | trading service UNVERIFIED | yes | tests only | yes |
| 5 routing | UNVERIFIED | fixed, no evidence | tests only | fixed + opt-in evidence |
| 6 OMS | UNVERIFIED | yes | tests only | yes (v1_5+) / old loop (v1_2-) |
| 7 execution | UNVERIFIED | simulated adapter | adapter in tests; probes real | simulated adapter |
| 8 hedging | UNVERIFIED | yes (profile safety values NOT applied) | tests only | yes |
| 9 ledger | UNVERIFIED | ledger at finalize | none from the bot | shadow ledger |
| 10 diagnostics | monitor files | cycle reports | ollog summary | write_result + out/ wrappers |
| 11 replay | n/a | recording is replayable | n/a | yes |

## 5. Breaks and missing stages, by audit ID

| Where | Break | Audit IDs |
|---|---|---|
| capture to replay | no incident window, no ring buffer, no trigger | 2.1, 2.2, 2.3, Q3 |
| capture | no monotonic stamp, no uncertainty, silent receive-time substitution | 0.2b, 1.5, Q5 |
| health to strategy | no feed-state input to the strategy, no alert delivery, quotes not withdrawn on a block | 1.2-1.4, 1.6, W13-B1 |
| book to strategy | no dislocation or reference-price stage; monitor live-only | 0.2g, 6.2-6.5, Q4 |
| book to routing | depth quality and book risk advisory only | 0.2e, 3.1-3.5, 4.1, 4.2, 5.1, W13-A4, W13-B2/B3 |
| routing | zero-cost call, no per-decision record, mutable capped journal | 0.2i, 8.1-8.4, Q2, W13-A |
| OMS / execution | paper vs replay session differ; durable writes at finalize; no bot run on testnet | 0.2d, 2.1, 12.2, 12.3 |
| hedging | no schedule mode; breaker and journal without display | 9.1, 9.2, W13-B6 |
| ledger | replay not reconciled to the economic ledger; fee-delta signature | 0.2h, 7.1, W13-C6 |
| diagnostics | shortfall split not called; no per-fill context; five builders in out/ | 7.2-7.5, 10.1-10.8, Q1, W13-C |
| replay | no hashes, no tool, no interlock, no parity rule | 0.2c, 2.4, 2.5, 12.2, 12.4 |
| data and gates | datasets and gate definitions | 0.3a-0.3e, 0.4 |
| MM | recorder, actions, parity not on core | M1, M2, M3, M4, M5, M6 |

## 6. Not verified here
- Live trading service path (A2 2.1); the VM recorder code; whether `ioc_policy.py` / `ioc_runtime.py` duplicate the chooser (A5).
- The v1_10 profile draft (`profiles/perp_perp_maker_taker_v1_10.yaml`, present 16 Nov 06:59, ignored by .gitignore) was not read for content.
- All line numbers marked WT may shift while the build session edits oms.py, session.py, ems/layer.py, ems/intent.py, config.py, hedge_lifecycle.py and B:session_replay.py, B:profiles.py.
