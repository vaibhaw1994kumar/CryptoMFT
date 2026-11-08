# One platform: making backtest and live the same system

**Status (7 Nov 2026):** Phases 0, A, B0 and B done; v1_2 frozen 7 Nov (tag `v1_2-freeze`, outer 0804b28, core 3569d0b). S0 built and off (v1_3). Current: Phase B2, steps 1-8 done and committed (step 8: outer a37977c), B2 gate APPROVED 8 Nov (tag phaseB2-gate: outer 09c9ecc, core c35e517, not pushed). After it: core_shadow.py renamed core_accounting.py (outer 3ffccac, identity on March 03-11 held), 2022-01-05 funding reported (core equals settler, C30), hedge execution and recovery audit done, report only (C31). PHASE C BUILT, gate run 8 Nov, owner review of 8 Nov completed (reports C33-C38; core b2ae96b then 1c123ad, outer c4b85ea then 02ebaba, tag phaseC-gate moved to the new commits, not pushed): 13 + 4 replays identical (earlier commits); the full suite equals the Phase 0 failing names plus the import-direction failure caused by the foreign untracked folder crypto_stat_arb/strategy_mm; with that folder moved aside the import test passes, so Phase C alone passes it. Review items done: S01-S03 and six more report cases through the core path with cash and fees compared (two defects found and fixed), concurrent chains capped by the package reservation, the owner decisions labelled DECISION_PENDING_CALIBRATION in config and profile identity. March 03-11 identity held after the review. STOPPED for owner review; Phase D not started. Waiting for the owner: approval at the B2 gate; the live bot's repricing rule or values (S0); whether the Bybit rebate is real; margin mode and capital per venue; permission to download the SOLUSDT margin tier tables; the account's own fee rates.

Written 5 Nov 2026. Every report claim is tracked in `report_claims_register.md`. Sources: Platform Requirements v1.0 (15 Oct 2026), issue register R1-R13, Issues and Fixes PDF, report summaries from 22 Oct to 4 Nov, git history of both repos, and the code at tag restore-before-core-integration. Past session transcripts were not available.

Updated 5 Nov after the first Step 0 feature matrix (`out/integration/step0_feature_matrix.md`). The direction did not change. Section 4 rows marked "(Step 0)" and the order of work in section 5 did.

## 1. What the requirements say

"Credible backtesting requires the core trading platform, including OMS, EMS, routing, risk and reconciliation, to operate inside the simulator."

"Use the same strategy, canonical events, OMS, risk rules and ledger across modes. Change the clock, market data source and execution adapter through explicit interfaces. Record any unavoidable model difference in the run manifest."

So the backtester should not run beside the core. The core runs, and only three things change by mode:

| Part that changes | Backtest | Paper | Testnet | Live |
|---|---|---|---|---|
| Clock | Replay clock | Real clock | Real clock | Real clock |
| Market data | Recorded events | Live feed | Live feed | Live feed |
| Execution adapter | Simulated exchange | Simulated exchange | Testnet API | Mainnet API |

Required path (requirements section 1):
public feeds -> raw capture -> canonical events and valid books -> features -> strategy intent -> parent plan -> atomic risk reservation -> child OMS orders -> venue router -> simulator or exchange -> private execution events -> ledger and risk -> strategy account view.

The requirements also separate three approvals: platform correctness, execution realism, strategy economics. The strategy being unprofitable today does not block platform work.

## 2. Where we are

| Stack | Runs | Missing |
|---|---|---|
| `crypto_stat_arb` `TradingSession` | Backtest, paper and testnet modes through one OMS, risk and ledger. `run_backtest` uses it. | The maker-taker strategy, fill-triggered dispatch (only `DETERMINISTIC_SEQUENTIAL`), queue fills (`DeterministicVenueAdapter` says so), a mainnet mode (`ExecutionMode` has none; hosts are testnet-only) |
| `new_backtest` `CrossVenueArbBacktester` | The real strategy and nearly all work since 22 Oct | Core OMS, risk, ledger. `sdk-backtest` starts it as a separate black box. |
| `trading_platform_poc` `opportunity.py` | Concurrent and FILL_TRIGGERED leg coordinator with reconciliation. Called the "live engine" in the register. | Not a live stack. `opportunity.py` logic is ported into the core; the rest is out of scope |
| `crypto_stat_arb` `maker_sim` + `sim_engine` | Paper maker simulation, `simulate-maker`, session audit | Not on the `TradingSession` order path; none of queue v2 |

The join already exists: `VenueExecutionAdapter.handle(request, book)` and the optional `on_book_update` hook in `execution.py`. It has no trade hook yet.

Requirement traceability marks many MR items Implemented. That is true of the `TradingSession` path, not of the strategy being researched.

## 3. Target

Owner's design rule, 5 Nov: every rule lives once, in `src/crypto_stat_arb`, and backtest and live both call it.

- `TradingSession` is the one runtime in every mode.
- The arb strategy and `edge.py` become core strategy code (SDK01).
- `new_backtest/execution` moves into the core as `crypto_stat_arb/ems/`, on top of the EMS.
- Queue v2, the fill engine and the other simulator code move to `crypto_stat_arb/simulation/`. They run only behind the simulated venue adapter, in backtest and paper. Live fills come from the exchange.
- `QueueAwareVenueAdapter` implements `VenueExecutionAdapter`, adds `on_trade`, and wraps `MakerExecutor` and queue v2. This is the "simulator" box in the required path.
- `new_backtest` keeps only the replay runner, the feed and research tools. It imports the core; the core never imports it, apart from a lazy `sdk.py` runner import until clean-up.

The simulated venue keeps its own exchange-time truth book. The strategy sees the delayed view book. Requirements section 1 asks for exactly this split.

## 4. Crucial changes and where each must end up

KEEP-SIM = moves to `crypto_stat_arb/simulation/`, runs only behind the simulated adapter. MOVE = must exist in the core for live. CORE-OWNS = core already has the job; retire the backtester copy after checking for gaps. RESEARCH = offline tool whose outputs are versioned parameters.

### Market data and books
| Change | Now in | Action |
|---|---|---|
| Data manifest and gzip truncation gate | `data_manifest.py` | MOVE into `dataset_manifest.py` |
| Preflight blocks rejected data, written override | `quality_preflight.py`, core `quality.py` | CORE-OWNS, keep blocking |
| Trade dedupe by native ID, labelled multiset fallback | `event_loader.py` | MOVE to canonical loading |
| Tape repair off | `order_book.py` switch | KEEP-SIM. Live books must never be edited from trades. |
| Own fills do not change the public book (overlay) | `fill_engine.py`, `order_book.py` | KEEP-SIM, compare with core adapter's withheld liquidity |
| Declared ticks checked against the data; product separation | `arb_strategy.py`, runner | MOVE to instrument master (REF01-03) |
| Oct 2024 run gate | runner, gates | CORE-OWNS data quality |
| Sequence gaps and crossed books (Step 0) | Backtester keeps applying deltas and trims crosses; core invalidates | Core rule wins (DAT05, L3 guide). Measured: every Bybit cross in three 20-minute March slices was transient inside one message, none at message end. So the backtester applies a message as one unit, then counts and validates crossed books the core's way at message end. Behind a v1_2 switch. |

### Strategy and opportunity
| Change | Now in | Action |
|---|---|---|
| One edge function, correct direction, depth-weighted | `edge.py` | MOVE, one module used by strategy and research |
| Conditional maker edge; markout not double counted | `arb_strategy.py`, `edge.py` | MOVE |
| Quote tick rounding, maker venue choice | `arb_strategy.py` | MOVE |
| Temporal coherence, book age and toxicity gates | `arb_strategy.py` | MOVE, check against core alignment policy |
| Opportunity record | `ArbSignal`, core `Opportunity` | Keep core `Opportunity`, extended. |
| Opportunity counting | `scripts/count_opportunities.py` | RESEARCH on the shared edge module |

### Execution and hedging
| Change | Now in | Action |
|---|---|---|
| Fill-triggered dispatch | backtester, not core | MOVE (top gap) |
| Intents, seven-state lifecycle, illegal moves raise | `execution/intent.py`, `layer.py` | MOVE, reconcile with core parent plans |
| Three tactics: PASSIVE, IOC_CAPPED, MARKET_BANDED | `execution/tactics/` | MOVE |
| One live hedge child, block on unknown | `execution/layer.py` | MOVE (EMS03, OMS03) |
| IOC cap rules, hedge limit at break-even + buffer | `execution/ioc_*`, `layer.py` | MOVE |
| Hedge retry, unwind, ghost hedge handling | backtester | MOVE. Ghost defect (3x limit) is still open. |
| Taker-taker and multi-leg | backtester, core sequential dispatch | MOVE; pairing brief not started |
| Rate limits by scope | core `rate_limits.py` | CORE-OWNS; simulator has none (Step 0) |
| Lot, minimum size and notional checks (Step 0) | core only; backtester has none | CORE-OWNS (REF04); backtester orders must pass them |
| Offline hedge evaluator, flicker fit | `execution/offline_hedge.py`, `flicker_fit.py` | RESEARCH |

### OMS, risk and recovery
| Change | Now in | Action |
|---|---|---|
| OMS02 states, cancel pending, fact-ordered resolution, reject-after-fill alarm, unwind exhausted alarm | core `oms.py` | CORE-OWNS |
| Order lifecycle spec, parity tests | `order_state_machine.py`, tests | Keep as tests until the simulator emits core events |
| Perp venue status mapping | `venue_semantics_perp.py` | MOVE beside testnet and live adapters |
| Atomic reservation | core `RiskEngine.reserve`, `execution/risk.py` | CORE-OWNS. Units differ: core USDT notional, backtester base quantity (Step 0). Report both. |
| Lost ack (Step 0) | Core queries and never resends; backtester retries, then unwinds | Core rule wins (OMS03): query before any replacement |
| Flatten on trip (Step 0) | core `open_flatten_plan`; backtester has none | CORE-OWNS |
| Reachable exposure including UNKNOWN | backtester, core partial | Compare, then MOVE gaps (EMS03) |
| Circuit breaker triggers, cancel on trip, pause vs trip | `circuit_breaker.py` | MOVE. Core has kill switches; no breaker class found in risk, oms or execution. |
| Fault injection, presets | `hedge_faults.py`, `fault_presets.py` | KEEP-SIM, inject at the adapter |

### Fill simulation
| Change | Now in | Action |
|---|---|---|
| Fill needs a real trade; depth moves the queue only | `fill_engine.py` | KEEP-SIM |
| Cross fills need a trade within 500 ms, capped at print | `fill_engine.py` | KEEP-SIM |
| Cancellation models, proportional chosen | `queue_v2/cancel_models.py`, `fill_engine.py` | KEEP-SIM |
| Cohort queue, fitted unknown queue, depletion ledger, epochs | `queue_v2/`, `fill_engine.py` | KEEP-SIM |
| PostOnly reject at arrival, amend loses priority | `postonly.py`, `fill_engine.py` | KEEP-SIM; live gets it from the exchange |
| Latency, truth vs view book, timers, three timelines | `latency_model.py`, `timing.py` | KEEP-SIM; compare with core `LatencyModel` so only one remains |
| Core maker simulator | `maker_sim`, `maker_engine`, `sim_engine` | Decision 2 |

### Accounting and P&L
| Change | Now in | Action |
|---|---|---|
| Cash ledger, equity bridge, common-timestamp valuation | `valuation.py`, backtester | CORE-OWNS (ECO06); port the common-timestamp check |
| Fill journal, rebuild books from journal | `execution/reconcile.py` | MOVE as an independent check |
| Fees and rebates by product | `config.py`, core `fees.py` | CORE-OWNS. Bybit maker is -0.5 bps in the backtest and +2.0 bps in the core config (Step 0). Owner confirms a rebate: headline -0.5, +2.0 as the conservative scenario (ECO01). The core config must be changed to match. |
| Funding on real schedule and mark, not mid | core `funding.py`, `funding_settler.py` | CORE-OWNS; March needs funding data |
| Margin, tiers, liquidation | core `margin.py`, `margin_model.py` | CORE-OWNS |
| Markouts by venue and role | `latency_model.py` tracker, core `tca.py` | MOVE to core metrics using the requirements definition |

### Research and control
| Change | Now in | Action |
|---|---|---|
| Fill models V1, V2, others | `fill_model*.py` | RESEARCH. Off in v1.1. Any gate used later runs inside the strategy. |
| Hedge move from market data; probe | `market_hedge_move.py`, `out/queue_v2/qv2_probe.py` | RESEARCH; output is a versioned parameter |
| Queue calibration, survival, rebuild acceptance | `queue_v2/`, scripts | RESEARCH |
| Profiles, mandatory switches, release identity | `profiles.py`, core `release.py` | MOVE to core config validation (SDK02) |
| Latency guard: `colocated_both` refused | `profiles.py` | MOVE |

## 5. Order of work

The full instructions are in `core_backtest_integration_prompt.md`. One session at a time, with the owner approving each gate.

0. Baselines and 12 hand-calculated synthetic scenarios.
A. Move shared code into the core with no logic change, checked byte-identical.
B0. Owner's open decisions: frame-atomic rebuild check, step 2 diagnosis, ledger feasibility, hedge move from the probe, bid rounded onto the best bid.
B. Accounting shadow, book rules, depletion ledger, data checks and the March stamp change (built, gate reported). Sensitivity range from full replays once, on the final v1_2, then the v1_2 freeze with owner approval. Done: v1_2 frozen 7 Nov 2026 (`perp_perp_maker_taker_v1_2.freeze.json`, tag `v1_2-freeze`, outer 0804b28, core 3569d0b).
B2. Owner 6 Nov: switch fees, funding, margin and ledger to the core (funding_settler, margin_model, fee presets, common-timestamp valuation in the core); own gate. Started 7 Nov, steps 1-8 done; see Phase B2 below.
S0. Quote repricing (owner 7 Nov), built and off; see Step S0 below. Measured inside v1_4.
C. Core fill-triggered hedging, porting behaviour from `opportunity.py` and the backtester.
D. One runtime: `QueueAwareVenueAdapter`, the strategy on the SDK, old vs new parity, then single-pass shadow queue trackers.
SV. Simulator validation (owner 7 Nov): after D, before S1; design only, report only until data exists; see Simulator validation below.
E. Risk gate, breaker pause vs trip, flatten on trip, ghost hedge fix, pairing, venue selector. Not built in Phase 0.
F. Clean up, then paper, testnet and a mainnet adapter under the requirements' stage gates.
   Phase D also (owner 8 Nov, B2 gate approval): the strategy's float inventory and cash, `_exec_reconcile`, and `trade_pnl_by_venue` / `legacy_fees_by_venue` into `reconcile` move to the core ledger here.
   Phase F also: (owner 8 Nov) the 37 unrelated tracked core changes stay untouched until Phase F, then are reviewed one by one.
   Phase F also: (owner 7 Nov, B2 gate) the REFERENCE_ONLY label for `scripts/perp_fault_run.py` and the `out/perp_lifecycle` grid copy (they print no profile label today).
S1-S3. Strategy work after F: adverse-selection table, hedge cap sweep, Albers features; see below.

Naming: phases 0 and A-F here are integration phases. The "Phase 0" and "Gate 0" in `canonical_execution_layer_simplified.html` are that design's step 1, already done in `new_backtest/execution`. Its later steps map as follows: step 3 (pairing, venue selector, risk gate) is phase E here. Steps 4 (phase 1-lite hedge limit sweep) and 5 (Albers features) run after phase F on the shared code, as strategy work. The adaptive hedge limit stays deferred.

### Phase B additions (owner 7 Nov)

- **Adverse-selection report (report only; done, C15).** The adverse-selection report on the existing probe (7 TRAIN days, v1_2 settings, 00:00-02:00 UTC): inventory of every adverse-selection component, the markout curve on the hedge venue (Binance mid) and on Bybit (30, 45, 60, 100, 250 ms, 1 s, 5 s, 30 s), splits by queue state, cause, distance from the touch, spread, Bybit flow sign and Binance 1 s volatility, what block_on_toxic would have blocked, and a proposed bucketed table (not built). It also answers the empty-level question. No strategy change, no freeze impact, HOLDOUT untouched, all figures EXPLORATORY.
- Through-print (owner 7 Nov, final): the engine fix is applied (one print budget across the cross, through and ordinary rules; a netted print still reaches the through rule) and the rule stays OFF in v1_2 and off the freeze list.

### Phase B2: one accounting in the core (owner 6 Nov: own gate; started 7 Nov)

Goal: one accounting, in the core, used by backtest and live. Working profile: `perp_perp_maker_taker_v1_4` (= v1_3 + the B2 switches).

Owner decisions for B2 (7 Nov):
  - **Order:** A2 common-timestamp valuation into the core first (done: it was already in the core, B2.1); then fees, funding, margin, ledger, one at a time. v1.1 stays byte-identical throughout. Every difference in the B2 profile is sorted BUG_BACKTEST, BUG_CORE or BY_DESIGN.
  - **Strict equity bridge:** the backtester's bridge (a run stops when cash and equity disagree beyond tolerance) is carried into the core reconciliation and economics gate (A3, A4), so no check is lost.
  - **Direct calls:** the backtester then calls the core Ledger, FeeSchedule, funding engine, margin and economics gate directly. Its own copies (valuation.py's accounting role, funding_settler, margin_model, fee presets) are no longer called by any v1_x run; kept only for legacy_v0 and the reference profiles until Phase F, then moved to discarded/ (with a MANIFEST line).
  - **Test:** a test fails if any run of the B2 profile books fees, funding, margin or equity outside the core.
  - **Fees (A5, C1):** Binance USD-M VIP 9 and Bybit linear VIP 5 (owner-confirmed). Add to the core FeeSchedule, PUBLIC_SCHEDULE_UNVERIFIED with the source date, until the account's own SOLUSDT rates are read: Binance maker 0.0, taker 1.7 bps (BNB taker 1.53); Bybit maker 1.0, taker 3.2 bps. Keep the core's `conservative_unverified` rule (an unverified negative maker fee is booked as 0); the Bybit -0.5 rebate stays a labelled scenario until verified (so `bybit_perp_mm` can only be a scenario, not the headline). Headline after B2: Bybit VIP 5 + Binance VIP 9 = 2.7 bps per maker-hedge pair; also report the MM-rebate scenario (1.2 bps) and the old ASSUMED set (4.5 bps). v1.1 and the frozen v1_2 fee settings do not change; the new headline belongs to the profile B2 produces.
  - **Funding (A7, C4):** use the exchange's recorded mark where present; otherwise `book_mid`, flagged FUNDING_MARK_FALLBACK and counted in every result. Never book nothing silently. The setting is recorded in the run manifest.
  - **Margin (A9, A10):** the backtester uses the core margin and risk code (tiered margin, collateral, liquidation); the v1_2 `above_last_tier` switch becomes redundant. Tiers stay ASSUMED until the SOLUSDT tier tables for both venues are in the instrument master (REF03); ASK THE OWNER BEFORE DOWNLOADING them. Margin mode (cross or isolated) and capital per venue stay the current assumptions, labelled, until the owner confirms them.
  - **Missing mark in `ledger.recalculate_account_state`:** a position with no mark price currently uses its average price as the mark (unrealized 0, no flag). Make it flagged: MARK_MISSING, counted in every result, and the economics gate INCOMPLETE, consistent with the funding engine. Compare runs on equity, not realized P&L alone. (A core change: its own commit in the core repo, measured alone.)
- **Fee presets for B2 step 4 (owner message 7 Nov; recorded now, applied when B2 moves fees to the core FeeSchedule; not applied in step 3):** the owner confirms Binance USD-M VIP 9 and Bybit linear VIP 5. Add presets labelled `PUBLIC_SCHEDULE_UNVERIFIED` with the source date, until the account's own SOLUSDT rates are read: `binance_perp_vip9` maker 0.0 bps, taker 1.7 bps (with BNB: taker 1.53); `bybit_perp_vip5` maker 1.0 bps, taker 3.2 bps. Bybit maker rebate is UNCONFIRMED (VIP 5 has a positive maker fee; a rebate needs the market maker program), so `bybit_perp_mm` (maker -0.5, taker 5.5) stays as a labelled scenario, not the headline. Headline after B2: `bybit_perp_vip5` + `binance_perp_vip9` = 2.7 bps per maker-hedge pair. Also report the same cells with an MM rebate (Bybit maker -0.5 + Binance taker 1.7 = 1.2 bps) and the old ASSUMED set (-0.5 + 5.0 = 4.5 bps), so the effect of fees is visible. v1.1 and the frozen v1_2 fee settings are NOT changed; the new headline belongs to the profile B2 produces (v1_3 or later).
- **Reference profiles (owner, step 8):** Reference profiles: `legacy_v0`, `perp_perp_maker_taker_v1`, `perp_perp_maker_taker_v1_1` (the CONTROL), `perp_perp_maker_taker_v1_2` (frozen), `perp_perp_maker_taker_v1_3`, `spot_perp_basis_v1`, `spot_spot_maker_taker_v1`. They keep the old accounting modules (`funding_settler`, `margin_model`, the fee presets, `valuation.py`'s accounting role) so their results reproduce byte for byte; every runner prints them as REFERENCE_ONLY, except v1.1, which prints CONTROL. Working profile: `perp_perp_maker_taker_v1_4` and every later version use only the core accounting. Any non-reference profile that would call an old accounting module is refused at startup (profile load and backtester start), with a test. The old modules move to `discarded/` in Phase F. From now on S0 (quote repricing) is measured inside v1_4, repricing on vs off, so fees and accounting are the same on both sides.

Steps (one change each; v1.1 byte-identical and the 13 + 4 baselines identical throughout):
- Step 1, valuation in the shadow: done (C22, outer 6bcab01). A2 resolved in the shadow with the core's own common_timestamp_valuation (equity gap 0.008 -> ~1e-14, runs unchanged).
- Step 2, funding in the shadow: done (C23, outer 3025542). funding in the shadow: core FundingEngine against funding_settler on January 01-13, 4 settlements, max payment difference 3.6e-18 USDT, run unchanged.
- Step 3, margin in the shadow: done (C24, outer f6ee4b0). margin in the shadow: core RiskEngine.evaluate_margin against margin_model at every check; January 01-13 (84,018 checks) and the March slices agree on every decision and to ~1e-13 on balance and requirements; no real check came near maintenance (breach path covered by synthetic tests only); runs unchanged, 13 + 4 baselines identical, failing tests equal the Phase 0 list.
- Step 4, fees from the core, profile v1_4: done (C25, outer 6af71a5, core 479d9c9). fees from the core FeeSchedule on the new profile v1_4 (`fee_source: core`; core `FEE_TIERS`, `FeeSource.PUBLIC_SCHEDULE_UNVERIFIED`). Identity cell (mm + vip0) byte-identical to v1_2 on the four March slices; headline Bybit VIP 5 + Binance VIP 9 (2.7 bps) and the 1.2 / 4.5 bps scenarios reported (a handful of trades, EXPLORATORY).
- Step 5, funding from the core: done (C26, outer 2cbf20c, core a2e9f0d). funding from the core FundingEngine (`funding_source: core`, `CoreFundingSettler`; core flag FUNDING_MARK_FALLBACK counted in every result). January 01-13: trades, fills and every numeric summary field equal to the settler run; 4 settlements all on book mid (FUNDING_MARK_FALLBACK 4); one bug (due time float round trip) found and fixed. Open: give the instrument its 8-hour funding schedule (core status reads INCOMPLETE without it); January 05 not measured.
- Step 6, margin from the core: done (C27, outer 3b41047). margin decided by the core (`margin_source: core`, `CoreMarginModel`: core RiskEngine breach state and worst-case admission; A9 redundant). March v1_4 and January 01-13: byte-identical to the model runs, margin statistics equal; no real check near maintenance, so breach and refusal are agreed by synthetic differential tests only. BY_DESIGN: worst-case gate (a reducing clip is not netted). Refused on the core path: transfers, isolated mode, utilisation limit. Not provided by the core: liquidation price/distance. Open: the liquidation fee is not in the core ledger (step 7).
- Step 7, ledger, strict bridge and economics gate from the core: done (C28, outer 24e829c, core c35e517). 7a funding schedule (8 h; core funding status COMPLETE), 7b MARK_MISSING in the core (ledger, reconcile, gate), 7c `ledger_source: core` (headline equity and cash P&L from the core ledger, strict bridge `economics.strict_bridge` incl. float inventory vs ledger, reconcile + economics gate written to the run directory, liquidation booked as LIQUIDATION). March and January: files byte-identical, headline scalars differ <= 6.6e-14, bridge OK, gate PASS. Open: `core_shadow.py` now holds the run's accounting object (a rename is a new file: ask at the gate); float inventory still drives the strategy; `_exec_reconcile` still reads float cash.
- Step 8, reference-profile registry and guard: done (C29, outer a37977c; core unchanged). Identity on March 03-11: legacy_v0, v1, v1.1 equal b1; v1_2 and v1_4 equal their earlier cells; 7 guard tests, 351 focused tests pass. Full 13 + 4 gate and full suite deferred to the B2 gate. registry in `profiles.py` (`REFERENCE_PROFILES`, `CONTROL_PROFILES`, `profile_status`, `profile_label`, `require_core_accounting`), called in `apply_profile` and at the top of `CrossVenueArbBacktester.__init__`; `release_identity.profile_status`; runners print the label (stdout only, result files unchanged). Guard tests: class `WorkingProfilesUseOnlyTheCore` in `tests/test_core_shadow.py` (7 tests, incl. every old entry point poisoned on a v1_4 backtester).
- B2 gate approved 8 Nov; rename, 01-05 funding and the hedge audit done (C30 to C32). Phase C built, gated and its owner review completed 8 Nov (C33 to C38). Next: owner review; Phase D not started.

### Step S0: quote repricing (owner 7 Nov; built 7 Nov, off, profile v1_3; measured inside v1_4 from B2 step 8)

- **Cause (C16):** resting quotes are never repriced. `maker_requote_bps` is declared and unused; a quote leaves the book only by a fill, the 15 s time in force or a breaker trip; `maker_max_live_orders` is 1. On 24 strategy fills the hedge-venue price drifted a mean &minus;4.8 bps between quote and fill, believed gross edge +8.15 bps against realised +3.83, net after fees &minus;2.17 bps (EXPLORATORY).
- **Order of work (as decided 7 Nov; S0 was then built before B2):** (1) finish the Phase B items: rerun the ledger and stamp cells on the final v1_2, the placement_book truth cell alone, the four-model range once, then the freeze proposal (content and release identity). **v1_2 is frozen as the simulator-correct baseline with the strategy unchanged, only after the owner approves.** Stop for review after the freeze proposal. (2) **S0, new: quote repricing as profile v1_3, placed right after the v1_2 freeze and before Phase B2.** (3) Phase B2, then Phases C to F, then S1 (adverse-selection table, fitted on fills of the repricing strategy), S2, S3. The adaptive hedge limit stays deferred.
- **S0 specification (design first, report only; build after the owner approves the design):**
  - Lives in the core strategy, so live uses the same rule. Remove or implement `maker_requote_bps`; no strategy setting may be declared and unused: add a test that fails on unused strategy settings.
  - Rule: cancel or reprice a resting maker quote when the hedge-venue price moves so that the edge recomputed at the current hedge price falls below the minimum edge, or by more than a set number of bps. It must include the cancel latency (the 35 ms floor to Binance), the fill-after-cancel race (catalogue H30), a minimum interval between reprices and the venue rate limits (a reprice is a cancel plus a new order).
  - Measured alone against the frozen v1_2, on the probe (7 TRAIN days) and on the strategy runs: quote age at fill, quote-to-fill drift, believed against realised edge, cancel and reprice counts, fills, and net P&amp;L after fees. Validated on SELECT only; HOLDOUT untouched; all figures EXPLORATORY.
  - **Answer from the owner's lead (7 Nov): YES, the live perp arbitrage bot reprices its Bybit quotes when Binance moves.** So the backtest is missing a live feature and S0 must copy the live rule exactly (trigger, stickiness band, debounce, maximum age, amend against cancel-and-replace, queue hold, minimum interval, rate-limit handling), with the live values recorded and labelled as live values. The live rule's source is not in this repository (`Bybit_MM/` and `FAST_MM/` are spot makers with no Binance hedge): **the owner must supply the live function or a written description before the S0 design can be finished.** The fit on the TRAIN probe days and the validation on SELECT then become a comparison of the live rule against alternatives, not the choice of the setting; v1_2 stays the frozen baseline without live repricing, and the backtest-versus-live gap is reported explicitly.
  - **S0 BUILT (7 Nov, report C21):** generic, modular, off by default, every number left to the trader or the live values: rule `crypto_stat_arb/ems/tactics/passive.py` (`decide_reprice`), target `ArbStrategy.requote_target`, simulator glue `new_backtest/requote.py`, setting `execution.primary.reprice`, profile `perp_perp_maker_taker_v1_3.yaml` (v1_2 + that block, disabled). Still open: the numbers, and the TRAIN fit / SELECT validation.
  - **S0 design (owner 7 Nov, recorded; report C19):** port the requote mechanics of `FAST_MM/Fast_MM_Codebase/bybit_order_manager.py` (`update_quotes`, `amend_order`; a spot altcoin maker: mechanics only, none of its numbers) into the core EMS passive tactic (`crypto_stat_arb/ems/tactics/passive.py`): amend first with cancel-and-replace on rejection, a stickiness band, a debounce after jumps, a maximum quote age and a queue hold. The trigger is the strategy's edge recomputed at the current hedge-venue (Binance) price, not Bybit's mid. Band, debounce, minimum interval and age are fitted on the 7 TRAIN probe days and validated on SELECT; amend is modelled as losing queue priority (`MakerExecutor.amend`); the reprice rate is reported against both venues' rate limits (design estimate in C19: the danger is bursts, so a minimum interval is part of the rule). No strategy setting may be declared and unused (`maker_requote_bps` and the EMS `primary["reprice"]` today).
- Open: the numbers (the live rule or its values, labelled LIVE_VALUES); the TRAIN fit and SELECT validation (needs a two-venue repricing probe); the cohort queue refuses amend, so S0 runs on the scalar queue.

### Phase C additions (owner 7 Nov)

- (Owner 8 Nov, B2 gate approval) The five EMS settings that nothing reads yet are settled in this phase: each is wired into the core path with a test or removed, none stays declared and unused.

- **Hedge execution and recovery audit (report only, before the Phase C build):** **DONE 8 Nov, report C31 (two preliminary findings corrected: the reject table is used by no run; the 5 s / 30 s residual numbers are core 5 s then 30 s kill, backtester 5 s breaker).** No code change. Audit each item in the backtester and in the core (file and function, whether v1_2 uses it, which test pins it, the catalogue case H01-H40): (1) submit with an idempotent client order id; (2) partial fill, only the residual is hedged; (3) reject classified by a venue code table, retry permitted or not; (4) timeout or unknown: query the venue before resubmitting, an unacknowledged order is never assumed failed; (5) retry limit and maximum residual-exposure duration; (6) escalation: deterministic emergency hedge or unwind when limits are breached; (7) reconcile positions and outstanding orders before recovery is declared complete. The core idempotency tests sit in tests/platform_kit and do not run (hft_platform_kit._ems is not built): say which of them cover items 1 and 4 and whether they can run without the native module.
  - Preliminary findings supplied by the owner on 7 Nov, NOT yet verified by this audit: (1) the backtester has no client order ids, the core has `idempotency_key`; (2) both hedge the residual, the core has no fill-triggered hedge yet; (3) the reject-code table (`venue_semantics_perp.py`) is backtester-only, the core has free-text `reject_reason` only; (4) query before resubmit exists for lost acks and, for timeouts in outages, only with the v1_2 switch `query_before_replace`, and in the core (OMS03, H15); (5) retry (`max_retries 2`, backoff 0.25 s) is backtester-only, the core goes straight to unwind (H03), residual limit core 5 s against backtester 30 s (H22); (6) unwind band 15 bps against 10 bps; flatten on breaker trip missing in both (H25, Phase E); (7) restart recovery never run end to end (H36). Nothing records per-event retry intervals and residual-exposure durations. Tests: duplicate submission core-only and not running; lost acknowledgement, partial fills, retry exhaustion, delayed reports exist; repeated rejections backtester-only; recovery after restart not exercised end to end.
- **Phase C build (the specification is in the prompt, PHASE C):** **BUILT 8 Nov (report C33 to C38; core b2ae96b and 1c123ad, outer c4b85ea and 02ebaba). Review of 8 Nov completed: S01-S03 and six extra report cases through the core path with cash and fees compared, the five reports read (calibration and statistics sections not line by line), concurrent chains capped by the reservation, decisions labelled. Left for later: retiring the PlanState and IntentState tables and per-event records in runner result files (Phase D); S06, S07, S13 (maker simulator, Phase D), S10 (breaker, Phase E).** one hedge lifecycle state machine in the core, one shared reject-code table, one unwind band and one residual limit (DECISION_PENDING_CALIBRATION), per-event recovery records, the seven tests through the core path and all 40 catalogue cases checked against the transition table. Built after the owner's approval.
- **p_fail and loss_fail_bps are relabelled SCENARIO (not calibrated).** They come from the measured markout curve at the retry and unwind times, labelled with their source and the TRAIN days used, and stay scenario values until real execution logs exist. Failure costs are reported per scenario. (`loss_fail_bps 7.8` was labelled calibrated but came from a simulated hedge-failure study, not execution logs; `p_fail 0.07` is a placeholder.)

### Simulator validation (owner 7 Nov; after Phase D, before S1)

- **Status:** design only, DO NOT BUILD (owner 7 Nov, at the B2 gate). Report only until data exists. Position: after Phase D, before S1 (and before any strategy result is treated as more than EXPLORATORY).
- **Roles:** the core simulates both roles on both venues: maker (queue simulator) and taker (book walk at arrival). Today the maker venue is Bybit and the hedge venue Binance.
- **Independent module (design first):** order-level replay validation in `new_backtest/validation/`, calling the core simulator with no strategy. Input: our real orders (send time, price, size, type) and the market data recorded at the same time. Output, per order: simulated vs real fill (yes or no, time, quantity, price), PostOnly reject, cancel race, partial fills; then rates and differences, with tolerances set before looking. Reuse the order lifecycle logger format (`ollog`) and the earlier real-order OMS replay and PostOnly validation work.
- **Evidence levels:** testnet = MEASURED_TESTNET, mechanics only (rules, statuses, PostOnly, cancel race), never fill rates, markouts or latency. Mainnet PostOnly probes and the live bot's fills = economics.
- **Data to collect on the production host:** full order books on both venues (including Binance depth), trades and mark prices with real receive times, plus our orders and fills for the same period.
- **Approval:** placing any order (testnet or mainnet), using any API key, touching the VM or changing a recorder needs the owner's explicit approval first. Keys only from environment variables; never printed or stored.
- **Isolation:** it may run in a separate Claude Code session only in its own git worktree, fixed at a tag, writing only to its own output folder.
- **Until it passes:** every result stays EXPLORATORY, and decisions must hold across the 4 cancel models and the latency presets.
- **No training on simulated fills:** no gate or strategy model may be trained on the simulator's own strategy fills.

### Strategy steps S1-S3 (after Phase F, on the shared code)

1. **S1, adverse-selection table in core `strategies/edge.py`.** It replaces the single expected hedge move and the uncalibrated block_on_toxic thresholds. Fitted on TRAIN, validated on SELECT, never HOLDOUT; shipped as a new profile version. **Fitted on the fills of the repricing strategy (S0, v1_3), not of v1_2.**
2. **S2, the Phase 1-lite hedge cap sweep,** run with the S1 curve.
3. **S3, Albers features.**
The adaptive hedge limit stays deferred.


## 6. Decisions for the owner

1. Decided 5 Nov: `crypto_stat_arb` is the only live stack. `trading_platform_poc` is out of scope.
2. Decided 5 Nov: `MakerExecutor` + queue v2 is the reference; the C++ engine stays as a parity test.
3. Is slower replay acceptable? Core uses `Decimal`; `fill_engine.py` uses floats, and one March day already takes about 2.5 hours. Measure at step 4 before committing.

4. Exact SOLUSDT fee rates. Owner confirms a Bybit maker rebate, remembered as -0.5 bps. Headline: `bybit_perp_mm` + `binance_perp_vip0` (4.5 bps per maker-hedge pair), which v1.1 already uses. `bybit_perp_vip0` is the conservative scenario (7.0 bps per pair). Open: the exact rebate, the Bybit taker rate, and the Binance tier. Figures given on 5 Nov (Binance 2.0/5.0, Bybit 2.0/5.5) equal public base rates, while the owner reports VIP 9; public VIP 9 on Binance is about 0/1.7 bps. Label verified with the date once taken from the account.
   Update 7 Nov: owner confirms Binance USD-M VIP 9 and Bybit linear VIP 5. Headline from B2 (profile v1_4): Bybit VIP 5 + Binance VIP 9 = 2.7 bps per pair, PUBLIC_SCHEDULE_UNVERIFIED; the Bybit -0.5 rebate is unconfirmed and stays a scenario (1.2 bps); the old ASSUMED set (4.5 bps) is reported beside it. Still open: the account's own rates.
5. Decided 5 Nov: `opportunity.py` logic is ported into the core. The file itself is not copied, and the rest of `trading_platform_poc` stays out of scope.

## 7. Not established

- Section 4 uses names, docstrings, imports and targeted reads. The first matrix confirmed the main gaps, but it lacks the row-by-row maker simulator comparison and the fill models, and its rows A8, A9, P5 and B4 are partly unverified.
- Step 2 rebuild: Binance January matches 100% of snapshots at the snapshot's own exchange time, so the 88-92% was event order. The January snapshots may be built from the same deltas, so this shows consistency, not independent proof that no update is missing. Independent REST checks exist only for the Oct 2026 mainnet capture.
- Crossed books: transient inside one message in all 35,902 messages of three 20-minute Bybit March slices. Message grouping by identical timestamps is an assumption, and the effect on signals and fills is not measured yet.
- Requirements: `Minimal Requirements v2.1` (17 Aug) covers risk, reconciliation and monitoring, with gates AT-01 to AT-14; checklist in `requirements_checklist.md`. The 15 Oct PDF has 5 pages, but the 29 Oct review cites 15 Oct groups `STR`, `CLK` and `SIM` that are not in it, and `SDK01-07` where the PDF stops at SDK05. A longer version may exist.
- The L3 guide, full and simplified execution design, Minimal Requirements and 29 Oct review are now in `out/integration/reports/`.
- `MINIMUM_REQUIREMENTS.md` describes `trading_platform_poc/minreq/`, which is out of scope. Traceability says the core also meets MR-01 to MR-17; that is unverified.
- No mainnet orders exist, so latency, rejects and queues on perps are uncalibrated. Queue parameters are a spot prior.
- Latency: the Binance 6-week PDF (received 6 Nov) measures market data delivery only. Binance feed delay fits about 2 ms, which suggests a host next to Binance, while the default preset is singapore at 36 ms. Host location, order ack, cancel and private report latency on both venues, and all Bybit figures, are still missing. The PDF says tick-to-trade is measured separately; those figures are requested. Handled after platform correctness, under the requirements' stages, and required before restricted live (Demonstration 5). Tool: `order_lifecycle_logger/`.


- 7 Nov (owner Step 8 re-check): through-print (fill_through_prints + through_print_volume_limited) is OFF in v1_2 until the owner approves report C12 item 3; the freeze list entry is conditional on it. Three engine changes proposed (through rule before the netted-print return; one print budget across cross/through/ordinary; both-order tests), not applied.

- 7 Nov (owner, final word on through-print): the three engine changes are applied (through rule before the netted-print return; one print budget across cross/through/ordinary; the four OPEN tests are ordinary tests). The rule stays OFF in v1_2 and is OFF the freeze list.


## Owner plan records (dated history; content now in section 5)

- 7 Nov (a): adverse-selection report and through-print -> 5, Phase B additions; hedge audit, Phase C build and p_fail / loss_fail_bps -> 5, Phase C additions; S1-S3 -> 5, Strategy steps.
- 7 Nov (b): C16 and S0 -> 5, Step S0; B2 decisions and steps 1-8 -> 5, Phase B2; v1_2 freeze -> status line and section 5, line B.
