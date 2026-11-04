TASK: Run the backtester in tandem with the core platform, first on synthetic data, so we know the whole system works together and no feature exists in only one place by accident.

Repo: /Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform.
Two git repos: outer (Newproject, branch integration-2026-11-01) and core (src/crypto_stat_arb, branch consolidate-new-backtest). Both have the tag restore-before-core-integration.
Backtester: src/new_backtest/. Core: src/crypto_stat_arb/.
Read Newproject/CLAUDE.md first and follow it. It holds the file map and the working rules.

WHAT IS ALREADY KNOWN (verify, do not assume)
- new_backtest imports only crypto_stat_arb.quality from the core.
- The core calls the backtester as a black box: sdk.py BacktestAPI -> CrossVenueArbBacktester / run_batch. latency.py and time_integrity.py import small pieces from new_backtest.
- crypto_stat_arb/session.py TradingSession runs core OMS, risk and ledger on replay events, with maker_sim.py as a separate maker simulator.
- tests/parity_adapters.py and tests/test_order_state_parity.py feed one event script to the spec, the core OMS and MakerExecutor (3 xfail mentions in that test).
- So the perp maker-taker strategy's orders and fills never pass through the core OMS, RiskEngine, Ledger or economics gate.

PRECONDITIONS. If any fails, STOP and say so.
- No other session is editing this tree and no jobs are running on it.
- Restore points exist: tag restore-before-core-integration in BOTH repos (done 5 Nov). Confirm it is there.
- legacy_v0 and perp_perp_maker_taker_v1 stay byte-identical throughout. v1.1 stays identical unless a step says otherwise and the owner approved it.

GUARDRAILS
- Extend existing modules. No new source file without owner approval, except the one bridge module approved at Gate 1.
- Reuse first: selftest.py, the test Harness in tests/test_perp_arb_legs.py, parity_adapters.py, TradingSession, sdk.py. Do not write a new synthetic generator if one of these can be extended.
- Back up to _backups/core_integration_<ts>/. No commits, pushes, VM changes, downloads, or writes to data/.
- Synthetic data proves wiring only. Never report a profit figure from it.

STEP 0: Feature comparison (READ-ONLY; report, change nothing)
Compare by FEATURE, not by file name. Read the code; file names and docstrings are not evidence. For each row give file:line in both stacks, what each actually does, and one class:
  SAME | BACKTEST_AHEAD (port to core) | CORE_AHEAD (backtester should use core) | CONFLICT (same job, different answer) | SIM_ONLY | LIVE_ONLY
Cover at least:
- Orders: states, late fill after cancel, reject after fill, UNKNOWN, ack timeout, unwind exhaustion alarm
- Execution: intents, three tactics, fill-triggered hedge, one live hedge child, unknown-intent block, price caps, fill journal and reconciliation
- Risk: atomic reservation, reachable exposure including UNKNOWN, net/gross/per-venue limits, breaker pause vs trip, cancel-on-trip, flatten, rate limits
- Book: snapshot/delta rules, sequence gaps, crossed/stale handling, book gate, floats vs Decimal, tape repair, own fills not changing the public book
- PostOnly at arrival, tick and lot rounding, truth book vs view book, three timelines, clock offset, staleness
- Accounting: cash vs equity, common-timestamp valuation, equity bridge, economics gate, fee schedule by date and product, funding (mark vs mid, sign, schedule, restart safety), margin tiers and liquidation
- Identity: release identity, profiles, mandatory switches, data manifest, preflight blocking
Also list dead or duplicate modules (for example, is advanced_backtest used at all?) without moving anything.
Output: out/integration/step0_feature_matrix.md, plus a short ranked list of CONFLICT rows where the two stacks would give a different money or position result.
GATE 1: STOP. The owner reviews the matrix and approves the bridge design and the port list.

STEP 1: Synthetic scenarios with hand-calculated answers
Small, deterministic two-venue scripts, each with the expected orders, fills, position, fees, funding and cash written down BEFORE running:
 1 clean maker fill, hedge fills in full
 2 maker partial fill, hedge for the filled quantity only
 3 maker never fills: no hedge, no position
 4 hedge rejected, retry, then unwind
 5 hedge lost ack, then ghost fill at reconnect
 6 cancel race: fill after cancel request
 7 PostOnly rejected at arrival
 8 funding across a settlement, position open on both legs
 9 risk limit refuses a signal: nothing changes
10 breaker trips with a position open
11 margin stress to liquidation
12 restart with an open position
State which scenarios the current code cannot express.

STEP 2: Shadow mode (observe only)
Off by default. A switch on the backtester feeds every order and fill event into the REAL core OMS, RiskEngine, Ledger, economics gate, funding and margin, using the same code paths live uses. The core only watches; it must not change any backtest result (test: byte-identical with the switch on and off).
Compare after every event: order state, booked quantity, position, fees, cash, funding, risk verdict. Any difference is logged with the event and classified as BUG_BACKTEST, BUG_CORE or BY_DESIGN.
Run all Step 1 scenarios. Mutation checks: inject a wrong fee, a dropped fill, a wrong funding sign and a skipped risk check; shadow mode must catch each one.
GATE 2: STOP. Report every difference. The owner decides each fix.

STEP 3: Port approved features, one at a time, one before/after per change
Order: (a) pure calculations into one shared version: fees, funding, economics; (b) BACKTEST_AHEAD features into the core; (c) OMS and risk, only where shadow mode is clean.
Do not merge the two order books in this task: keep the backtester's book and test agreement instead.
After each change: full test suite, identity check, shadow scenarios.

STEP 4: Real-data check
Only after Steps 1-3 are clean: shadow mode on a 2-hour March slice and one January reference day, v1.1, singapore. Report differences. All P&L EXPLORATORY.

OUTPUT: out/integration/core_integration_report.html, in plain language: feature matrix summary, scenario pass/fail, shadow differences and how each was resolved, what was ported, what still differs and why, what is not proven. Full test count at the end. Update Newproject/CLAUDE.md's map and current state in the same change.
