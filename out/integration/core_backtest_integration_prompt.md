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
1. Proposal 4A, the accounting-only shadow, approved, with new_backtest/core_shadow.py as the one new file. Delete it when Phase B ends.
2. Proposal 4B, the deferred hedge child, approved for Phase C.
3. Reference maker simulator: MakerExecutor + queue v2. Keep the C++ engine as a parity test only.
4. trading_platform_poc/opportunity.py is back in scope as a source. Port its fill-triggered path, leg recovery and reconciliation into the core OMS/EMS. Do NOT copy the file: it brings its own Order model, async coordinator and store, which would make a third OMS. Read tests/platform_kit/test_opportunity_fill_triggered.py and test_opportunity_hardening.py and carry their cases over. The rest of the package stays out of scope.
5. Requirements decide conflicts: query before replace (OMS03), invalidate on gaps (DAT05), lot and minimum checks (REF04), revalidate at dispatch (EMS01).
6. Book: apply each exchange message as one unit, then count and validate crossed books the core's way at message end. New behaviour goes behind a v1_2 switch; v1.1 stays identical.
7. Keep BT market_first for equal timestamps, recorded in the run manifest as a model difference.
8. Risk limits: report both base quantity and USDT notional. The core keeps notional limits and adds base limits in Phase E.
9. Fees: headline bybit_perp_vip0 + binance_perp_vip0 (Bybit 2.0/5.5, Binance 2.0/5.0 bps, BNB 1.8/4.5). bybit_perp_mm is a scenario. The owner still has to confirm these are the account's own rates, so label them ASSUMED until then.
10. Legacy_v0 and v1 results stay as reference, labelled as containing fills without trades.
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
- Write 12 deterministic two-venue scenarios, with expected orders, fills, position, fees, funding and cash worked out by hand first: clean fill and hedge; maker partial; maker never fills; hedge rejected, retry, unwind; lost ack then ghost fill at reconnect; cancel race; PostOnly reject; funding across settlement; risk refusal; breaker trip with position open; margin stress to liquidation; restart with an open position. Reuse tests/synthetic_days.py, the Harness in tests/test_perp_arb_legs.py and selftest.py. State which ones today's code cannot express. Include review Demonstrations 1, 3 and 4.
GATE: owner reviews.

PHASE A: Move shared code into the core, no logic change
- Move in dependency order, leaf modules first: edge, order_state_machine, postonly, circuit_breaker, hedge_faults, timing, valuation, then ems, then queue_v2 and fill_engine. Move LATENCY_PRESETS and HostClockAligner so latency.py and time_integrity.py no longer import new_backtest.
- Add a test that fails if crypto_stat_arb imports new_backtest, apart from the sdk.py exception.
- After each move: full suite, baselines byte-identical. Note the replay speed.
GATE.

PHASE B: Shared accounting, fees and book rules
- Build the accounting shadow (matrix 4A) and sort every difference: BUG_BACKTEST, BUG_CORE or BY_DESIGN. Then switch the backtester to the core's FeeSchedule, funding, margin and ledger. Port common-timestamp valuation into the core. Retire funding_settler, margin_model and fee presets.
- Behind v1_2: message-atomic book with crossed-book counting by the core rule, gap handling, tick/lot/minimum checks, query before replace.
- Measure Decimal cost at the simulator boundary. Keep floats inside the simulator if needed and record that in the manifest.
GATE.

PHASE C: Core fill-triggered hedging
- Add FILL_TRIGGERED (matrix 4B), porting behaviour from opportunity.py and backtester._on_fill: deferred hedge child, atomic package reservation, hedge for the filled quantity, one live hedge child, unknown block, EMS01 revalidation, durable send per OMS03. Settle the rule for a second partial fill while a hedge is in flight.
- DETERMINISTIC_SEQUENTIAL stays the default, with all its tests green.
GATE.

PHASE D: One runtime
- Build QueueAwareVenueAdapter on VenueExecutionAdapter, adding an on_trade hook beside on_book_update. Move the arb strategy to the SDK protocol. TradingSession runs it in backtest and paper.
- Parity: same synthetic data, then March slices and January days, old path vs new. Every difference is explained or recorded in the run manifest.
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
