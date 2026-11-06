TASK: Make one shared platform. Backtest and live use the same rules and the same code. Work through the phases below, one gate at a time.

Repo: /Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform.
Two git repos: outer (Newproject, branch integration-2026-11-01) and core (src/crypto_stat_arb, branch consolidate-new-backtest). Both have the tag restore-before-core-integration.

Read first, in this order: Newproject/CLAUDE.md, out/integration/single_platform_plan.md, out/integration/step0_feature_matrix.md (revision 2), out/integration/report_claims_register.md, out/integration/requirements_checklist.md.
Governing documents are in out/integration/reports/ and out/integration/Platform_Requirements_v1_15_Oct_2026.pdf.

DESIGN RULE (owner, 5 Nov)
- Every rule that defines behaviour lives once, in src/crypto_stat_arb, and both backtest and live call it. A rule added once applies everywhere.
- The import direction is one way: new_backtest may import crypto_stat_arb; crypto_stat_arb never imports new_backtest. Only sdk.py BacktestAPI may keep a lazy import of the runner until Phase F.
- Only the clock, the market data source and the execution adapter differ by mode.
- Simulated fills stay simulated: queue v2 and the fill rules live in the core but run only behind the simulated venue adapter, in backtest and paper. Live fills come from the exchange.

TARGET LAYOUT in src/crypto_stat_arb/
- ems/: from new_backtest/execution (intent, layer, risk, reconcile, ioc_*, tactics/, config) plus new risk_gate.py, pairing.py, venue_selector.py. `execution.py` already exists in the core, so the package is ems/, and the existing StrategyDirectedRouter stays until merged.
- strategies/: arb_strategy.py as the maker-taker strategy, on the SDK StrategyProtocol. edge.py beside it.
- simulation/: fill_engine.py, queue_v2/, postonly.py, hedge_faults.py, fault_presets.py, timing.py, latency_model.py, and the new QueueAwareVenueAdapter.
- circuit_breaker.py, profiles (merged with config validation and release.py), order_state_machine.py.
- new_backtest keeps only: replay runner, feed, and research tools (fill models, calibration, probes, sensitivity).
Adjust a name only with a stated reason.

GATE 1 DECISIONS (owner)
1. Proposal 4A, the accounting-only shadow, approved, with new_backtest/core_shadow.py as the one new file, not the selftest-only alternative. Delete it when Phase B ends.
2. Proposal 4B, the deferred hedge child, approved for Phase C.
3. Reference maker simulator: MakerExecutor + queue v2. Keep the C++ engine as a parity test only. `sim_engine/*.py`, fill_model_deep.py and fill_model_queue.py (nothing produces its model file) go to discarded/ in Phase F.
4. trading_platform_poc/opportunity.py is back in scope as a source. Port its fill-triggered path, leg recovery and reconciliation into the core OMS/EMS. Do NOT copy the file: it brings its own Order model, async coordinator and store, which would make a third OMS. Read tests/platform_kit/test_opportunity_fill_triggered.py and test_opportunity_hardening.py and carry their cases over. The rest of the package stays out of scope. This replaces the earlier message telling you to skip opportunity.py; the matrix itself stays as it is.
5. Requirements decide conflicts: query before replace (OMS03), invalidate on gaps (DAT05), lot and minimum checks (REF04), revalidate at dispatch (EMS01).
6. Port order as in matrix section 5 is approved. Book: apply each exchange message as one unit, then count and validate crossed books the core's way at message end. Do not invalidate the book on a mid-message cross. The message-atomic book and query before replace change results, so both go behind a v1_2 switch; v1.1 stays identical.
7. Equal timestamps (P5): keep BT market_first as the one simulator rule, recorded in the run manifest. The core's cancel-first tie rule goes when maker_sim retires. Live has no tie: the exchange decides.
8. Risk limits: report both base quantity and USDT notional. The core keeps notional limits and adds base limits in Phase E.
9. Fees: do not ask for a tier per data period; the account did not trade those periods. The question is the rate we will trade on. Owner confirms a Bybit maker rebate. Headline: bybit_perp_mm (maker -0.5 bps, taker 5.5) + binance_perp_vip0 (2.0/5.0; BNB 1.8/4.5), which is what v1.1 already uses, so v1.1 needs no fee change. bybit_perp_vip0 (+2.0 maker) is the conservative scenario (ECO01). Label all rates ASSUMED until the owner gives the account's own SOLUSDT figures: the rebate size and the Binance tier are unconfirmed. In Phase B, the core FeeSchedule and config/perp_research.json must carry the same rates, because the core config still says Bybit maker +2.0.
10. Legacy_v0 and v1 results stay as reference, labelled as containing fills without trades (C9); they are never headline numbers.
11. Leave the 37 uncommitted core deletions alone unless the owner says otherwise. Do not stage them.
12. Dead modules move to discarded/<version>/ in Phase F.

IMPORTANT FACT: Phase 0 of the execution layer built intents, tactics, atomic reservation, one hedge child, the unknown block and journal reconciliation. It did NOT build pairing, venue selector, risk gate, breaker pause or flatten on trip. Only some settings for them exist in execution/config.py. Confirm this at the start, then build them in Phase E.

GUARDRAILS
- One session on this tree. Check for running jobs first. Stop only your own PIDs.
- Back up to _backups/shared_core_<ts>/. No commit, push or branch switch without the owner's go-ahead. No writes to data/, no VM, no downloads.
- legacy_v0, v1 and v1.1 stay byte-identical unless a phase says behaviour changes on purpose.
- Move first, change later: a phase that moves code changes no logic. Never mix a move with a behaviour change.
- One change at a time, with a before and after.
- Do not leave two copies of a rule. A temporary re-export in new_backtest is allowed and is removed in Phase F.
- Synthetic data proves wiring only: no profit figure from it. All P&L is EXPLORATORY with profile, size, days and fees.
- Never open FAST_MM/hft_scaffold/check_link_orders_history.py.

PHASE 0: Baseline and scenarios
- Record byte-level baselines: trade log, fill events, result, for legacy_v0, v1 and v1.1 on 2022-01-05, 2022-01-13 and one 2-hour March slice.
- Run the parity tests the matrix only quoted: tests/test_order_state_parity.py and tests/test_maker_executor_engine_parity.py, and report the result.
- Write 12 deterministic two-venue scenarios, with expected orders, fills, position, fees, funding and cash worked out by hand first: clean fill and hedge; maker partial; maker never fills; hedge rejected, retry, unwind; lost ack then ghost fill at reconnect; cancel race; PostOnly reject; funding across settlement; risk refusal; breaker trip with position open; margin stress to liquidation; restart with an open position. Reuse tests/synthetic_days.py, the Harness in tests/test_perp_arb_legs.py and selftest.py. State which ones today's code cannot express. Include review Demonstrations 1, 3 and 4. Add a scenario for a crash between "persisted" and "sent" (OMS03), which has no test today.
GATE: owner reviews.

PHASE A: Move shared code into the core, no logic change
- Move in dependency order, leaf modules first: edge, order_state_machine, postonly, circuit_breaker, hedge_faults, timing, valuation, then ems, then queue_v2 and fill_engine. Move LATENCY_PRESETS and HostClockAligner so latency.py and time_integrity.py no longer import new_backtest.
- Add a test that fails if crypto_stat_arb imports new_backtest, apart from the sdk.py exception.
- After each move: full suite, baselines byte-identical. Note the replay speed.
GATE.

PHASE B0: Owner's open decisions (one session, one change at a time, before Phase B)
- Frame-atomic rebuild check, first because it touches no strategy: run the rebuild acceptance on the 4 January venue-days (Bybit and Binance, 2022-01-05 and 2022-01-13), with and without frame-atomic application. Report touch match %. No strategy runs.
- Hedge move: use the probe estimate. Compare it with realised hedge moves on the SELECT slices and report the gap and n. No haircut unless the owner approves one after seeing the gap. Realised hedges in the final setup are about one fill across the four slices, so also run the existing probe on the same SELECT slices and report that n. State that the probe window starts at the fill and is 1 s, not hedge arrival.
- Bid rounded onto the best bid: new switch. Join at the back of the queue if the edge recomputed at the rounded price still passes; otherwise refuse. Test it. Re-run the four 2-hour slices and report refused vs joined. Profile: on in v1_2 and off in v1.1 (owner decision, 5 Nov), so v1.1 stays the byte-identical control. Results depend on fees, which stay ASSUMED.
- Queue v2 evidence: in the slice runs and the SELECT probe runs, report under v1.1: % of fills and % of queue-position updates carrying ambiguous_trade_depth, and how many fills came via the exact-price rule (trade_after_front) now that quotes are on the tick grid. Report n; strategy fills are few, so the probe runs are the usable sample. If no counter exists for queue-position updates, add one as observation only, keeping v1.1 outputs byte-identical.
- Step 2 diagnosis, Binance 01-05 and 01-13, report only, no code changes: re-score the rebuild against each snapshot, also allowing a match with the rebuilt book at any point within +-50 ms and +-200 ms of the snapshot time. If the match reaches about 99%, snapshot timing is the cause: mark step 2 done. If not, list 20 sample mismatches with the surrounding updates and say whether updates look missing.
- Ledger feasibility, report only: the converted March files on disk carry no trade id, update id or sequence field (data/*/trades and book_events have only timestamps, price, size and side). Find out whether the raw public source files still exist and which ids they hold. State whether anything links a trade to a depth update. A trade id alone removes duplicates; it does not pair a trade with a depth change. Binance March has no depth, so only Bybit matters.
- Profile roles (owner, 5 Nov): v1 and legacy_v0 are reference only, never for decisions. v1.1 stays the byte-identical control. v1_2 (v1.1 plus approved deliberate changes) is the working profile.
- Default profile: every runner and script that picks a profile defaults to v1.1 now and to v1_2 once frozen. v1 and legacy_v0 run only with an explicit --reference flag and are labelled REFERENCE_ONLY. Do not change engine config defaults; legacy byte-identity depends on them.
- Move every setting that defines a run into the profile, for v1_2: maker_venues, venue_ticks, block_on_toxic, max_cross_venue_skew_s, fill_gate_horizon_s, and a pinned hedge-move calibration file with its hash. Today the runner sets these and the profile carries hedge move 0.0, so a profile hash does not identify a run.
- List v1_2 switches in three groups: simulator-only (fill evidence, queue, ledger), must match in the live path (tick rounding, maker venue, hedge cap, hedge pricing at arrival), and research. Live path means crypto_stat_arb only. Report which the live path lacks. Report only; no live changes. Today's hedge cap is touch_slack; break-even plus buffer is not chosen until the Phase 1-lite sweep.
- HOLDOUT days stay untouched. All P&L EXPLORATORY.
GATE.

PHASE B: Shared accounting, fees and book rules
- Build the accounting shadow (matrix 4A) and sort every difference: BUG_BACKTEST, BUG_CORE or BY_DESIGN. (Owner 6 Nov: the rest of this bullet is Phase B2, its own gate, unstarted.) Then switch the backtester to the core's FeeSchedule, funding, margin and ledger. Port common-timestamp valuation into the core. Retire funding_settler, margin_model and fee presets.
- Behind v1_2: message-atomic book with crossed-book counting by the core rule, gap handling, tick/lot/minimum checks, query before replace.
- Ledger native ids: deferred by the owner's gate decision. No raw March files exist on the Mac, ambiguous fills are 0.72% (5 of 692), and the depth-driven queue updates depend on the cancel model, which ids would not fix. The 50 ms window stays and is stated in the report. Do not download or build.
- Make signals and maker queue updates run after a whole message, not after each delta. The touch differs from the end-of-message touch on 18-22% of deltas. Measure what half-applied messages did to signals and fills: v1.1 vs v1_2, one change at a time.
- Extend the crossed-book measurement beyond three 20-minute Bybit slices: more hours and days, and January. Prove the message boundary from the data, since identical exchange_ts and local_ts as one message is an assumption. Binance March is best bid/ask only; say so.
- Margin: settle A9, where the backtester never liquidates a position above its last tier and the core treats it as a breach. The core rule wins unless the requirements say otherwise.
- Measure Decimal cost at the simulator boundary. Keep floats inside the simulator if needed and record that in the manifest.
GATE.

SENSITIVITY RANGE at the Phase B gate, before the freeze (owner 6 Nov: it runs ONCE, on the final v1_2, not on interim versions; the depletion-ledger work ran proportional only): report v1_2 cash P&L, fills and Bybit maker markout under behind_first, proportional, ahead_first and cohort_uniform, from separate full replays. Headline stays proportional, with min-max beside it. No new engine code.

DATASET SETTINGS: v1_2 is March-only because venue_ticks and the hedge-move file are per dataset. Before the freeze, propose separating strategy rules from a dataset block, with ticks heading to the instrument master (REF03). January reference checks in Phase B use v1.1 plus the switch under test.

RELEASE FREEZE at the Phase B gate, with owner approval: freeze v1_2 after the rounding switch, probe hedge move, message-atomic book, query before replace, depletion ledger (owner 6 Nov, built, no effect on the measured slices) and the March stamp change (trust_local_ts false, ASSUMED, built) are in. The freeze list also waits for the owner's decision on the trade/depth matching windows and on the placement book (both below). Release identity: both repos' git hashes, dirty flags, profile hash, code hash, fee snapshot, calibration file hashes, calibration cutoff. Every result carries it. A later behaviour change means a new profile version.

OWNER DECISIONS (6 Nov, after the Phase B first-pass gate and the depletion-ledger pass):
- v1_2 is NOT frozen and Phase C does not start. Fees, funding, margin and ledger switch-over (funding_settler, margin_model, fee presets, common-timestamp valuation in the core) is Phase B2, with its own gate, unstarted.
- Core `perp_research.json` stays, with max_unwind_attempts 2 until Phase C. The platform copy (`crypto_MFT_arb_platform/config/perp_research.json`, max_unwind_attempts 3) was read by no replay, accounting shadow or baseline run; only tests and `scripts/load_test.py` read it. The shadow read the core copy.
- The manifest override and the preflight override are separate: `allow_manifest_gap` (runner flag `--allow-manifest-gap`) covers the manifest gate only; the preflight blocks unless `allow_rejected_data` is also set. v1_2 runners refuse `--allow-rejected-data`. Legacy runs keep the old single flag, whose result keys are unchanged.
- Depletion ledger (guide step 4), behind `maker.depletion_ledger` (v1_2 true, v1.1 false, v1.1 byte-identical): each depth decrease records the ahead movement it applied; its print moves the queue only by the rest; `trade_after_front` uses only the print left after that movement; netted prints and decreases with no other proof (stamps more than 1 ms apart AND sizes unequal) are counted apart from competing credits. Tests run every fixture on the v1.1 settings, both arrival orders, 0/10/30 behind.
- March stamps, v1_2 only, its own change after the ledger: trust_local_ts false on both venues (ASSUMED). The March local stamps are exchange time + exactly 2.000 ms, so the singapore feed delay (Bybit 2 ms, Binance 36 ms) applies; the deltas of one exchange message keep one receive time.
- Trade/depth matching windows (dedupe_window_s 50 ms, evidence_window_ms 20 ms): measured on TRAIN days (report only). The owner approves any window change; the ledger cells are not rerun until then. The four-model sensitivity range runs once on the final v1_2.
- Step-3 check (report only): hypothetical-order tests pass on the v1.1 and v1_2 settings; no placed order used the unknown-queue fallback of 25; whether placement should use the truth book, as PostOnly does, awaits the owner's decision.

LATENCY EVIDENCE (owner, 6 Nov): Binance_Futures_Latency_6_weeks.pdf measures Binance market data delivery only. It has no order ack, cancel or hedge figures and no Bybit figures, and it says so itself. Depth, Trade and Ticker Tx-to-Client P50 is about 1.4-2.5 ms, P99 about 4-5 ms on Depth, with rare spikes (20 ms Depth, 145 ms Ticker, 1.61 s Trade). The values are hourly maxima of percentiles, not averages. AggTrade, Ticker24h, ForceOrder and MarkPrice figures are excluded: those feeds run on exchange cadence.
- These figures fit the tokyo preset's Binance feed (2 ms), not the singapore preset's (36 ms). Host location is not stated. Do not change any preset, calibration file or v1.1 until the owner confirms it.
- Never use a market data figure as entry, ack, cancel or hedge latency.
- Before Phase B, report only: (a) break down simulated hedge arrival from fill into private report, decision and entry time, and explain why the observed 31 ms is below the singapore preset's expected sum of about 45 ms. (b) Hedge move probe on the 7 TRAIN days at fixed delays of 30, 45, 60, 100, 130 and 200 ms from the fill, plus 1 s, with n, mean and standard error for each. (c) Re-run the four v1_2 slices under the singapore and tokyo presets and report P&L, fills and maker markout. All three are EXPLORATORY.
- Add a Binance public feed stall to the Phase C scenarios: no Binance update for 1.6 s just after a Bybit fill.

PHASE C: Core fill-triggered hedging
- Add FILL_TRIGGERED (matrix 4B), porting behaviour from opportunity.py and backtester._on_fill: deferred hedge child, atomic package reservation, hedge for the filled quantity, one live hedge child, unknown block, EMS01 revalidation, durable send per OMS03. Settle the rule for a second partial fill while a hedge is in flight.
- EMS01 is unmet in both stacks: a request queued for rate limits is later sent against the book captured when it was queued. At release, re-check price, freshness and limits, and expire it when the opportunity has passed. This covers every queued route, not only hedges.
- Add the skipped risk check mutation here: risk is not in the Phase B shadow.
- The hedging work already exists: port it, do not reinvent it. Sources: backtester._on_fill and the hedge retry/unwind paths, execution/layer.py, execution/reconcile.py, hedge_faults.py, circuit_breaker.py, fill_engine.py lifecycle records, and the core's residual, unwind, alarm and deadline paths in oms.py. Reports to read: hedge_failure_study_report, hedge_failure_deep_dive_report, lifecycle_realism_report, perp_lifecycle_report, perp_mechanics_report.
- Rule-versus-simulation split: rules go into core OMS/EMS (partial-fill hedging, retry, unwind, UNKNOWN, ghost handling, recovery, reconciliation). Injected failures stay in simulation/. Every failure case in those reports must become a scenario that passes through the core path, with the old result shown beside it. Where a report's behaviour conflicts with OMS03, the requirement wins.
- DETERMINISTIC_SEQUENTIAL stays the default, with all its tests green.
GATE.

PHASE D: One runtime
- Build QueueAwareVenueAdapter on VenueExecutionAdapter, adding an on_trade hook beside on_book_update. Move the arb strategy to the SDK protocol. TradingSession runs it in backtest and paper. DeterministicVenueAdapter resting fills come from a book cross, not a print (F9), so they must not carry the arb maker leg; keep that adapter for taker fills only.
- Parity: same synthetic data, then March slices and January days, old path vs new. Every difference is explained or recorded in the run manifest.
- Shadow queue trackers: only after the adapter is stable, build a single-pass tracker for all models and add the range to every result. Fills differ by model, so hedges, inventory and later signals differ too: first show it agrees with the full replays, or label it an approximation.
GATE.

PHASE E: Risk gate, pairing, venue selector
- Use the brief at the end of out/integration/reports/execution_layer_progress_report.html, but build in crypto_stat_arb/ems/, on the core RiskEngine. Includes breaker pause vs trip, flatten on trip, ghost hedge fix (stay within 1x on 2022-01-05), a refusal shown on real replay data, pairing and venue selector. Mutation checks as listed there.
GATE.

PHASE F: Clean up
- Retire duplicates to discarded/<version>/, remove re-exports, merge the runners into batch_backtest.py, move hard-coded run settings into profiles, fix result.json and exact-time markouts, move the calibration harness into scripts/.
- Update CLAUDE.md, single_platform_plan.md and every row of report_claims_register.md.
- Check against requirements_checklist.md: Backtest-ready means AT-01 to AT-11 and AT-14.

OUTPUT: keep one report, out/integration/core_integration_report.html, updated at each gate in plain language: what moved, what changed, differences found, what is not proven. Full test count at the end. Do not create other report files.

START: confirm the preconditions and the IMPORTANT FACT, do Phase 0, and stop at its gate.
