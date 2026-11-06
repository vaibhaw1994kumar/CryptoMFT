# One platform: making backtest and live the same system

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
B. Accounting shadow, book rules, depletion ledger, data checks and the March stamp change (built, gate reported). Sensitivity range from full replays once, on the final v1_2, then the v1_2 freeze with owner approval (not frozen yet).
B2. Owner 6 Nov: switch fees, funding, margin and ledger to the core (funding_settler, margin_model, fee presets, common-timestamp valuation in the core); own gate; unstarted.
C. Core fill-triggered hedging, porting behaviour from `opportunity.py` and the backtester.
D. One runtime: `QueueAwareVenueAdapter`, the strategy on the SDK, old vs new parity, then single-pass shadow queue trackers.
E. Risk gate, breaker pause vs trip, flatten on trip, ghost hedge fix, pairing, venue selector. Not built in Phase 0.
F. Clean up, then paper, testnet and a mainnet adapter under the requirements' stage gates.

Naming: phases 0 and A-F here are integration phases. The "Phase 0" and "Gate 0" in `canonical_execution_layer_simplified.html` are that design's step 1, already done in `new_backtest/execution`. Its later steps map as follows: step 3 (pairing, venue selector, risk gate) is phase E here. Steps 4 (phase 1-lite hedge limit sweep) and 5 (Albers features) run after phase F on the shared code, as strategy work. The adaptive hedge limit stays deferred.

## 6. Decisions for the owner

1. Decided 5 Nov: `crypto_stat_arb` is the only live stack. `trading_platform_poc` is out of scope.
2. Decided 5 Nov: `MakerExecutor` + queue v2 is the reference; the C++ engine stays as a parity test.
3. Is slower replay acceptable? Core uses `Decimal`; `fill_engine.py` uses floats, and one March day already takes about 2.5 hours. Measure at step 4 before committing.

4. Exact SOLUSDT fee rates. Owner confirms a Bybit maker rebate, remembered as -0.5 bps. Headline: `bybit_perp_mm` + `binance_perp_vip0` (4.5 bps per maker-hedge pair), which v1.1 already uses. `bybit_perp_vip0` is the conservative scenario (7.0 bps per pair). Open: the exact rebate, the Bybit taker rate, and the Binance tier. Figures given on 5 Nov (Binance 2.0/5.0, Bybit 2.0/5.5) equal public base rates, while the owner reports VIP 9; public VIP 9 on Binance is about 0/1.7 bps. Label verified with the date once taken from the account.
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


## OWNER PLAN RECORD, 7 Nov 2026: adverse selection and the strategy sequence
- **Phase B (now): report only.** The adverse-selection report on the existing probe (7 TRAIN days, v1_2 settings, 00:00-02:00 UTC): inventory of every adverse-selection component, the markout curve on the hedge venue (Binance mid) and on Bybit (30, 45, 60, 100, 250 ms, 1 s, 5 s, 30 s), splits by queue state, cause, distance from the touch, spread, Bybit flow sign and Binance 1 s volatility, what block_on_toxic would have blocked, and a proposed bucketed table (not built). It also answers the empty-level question. No strategy change, no freeze impact, HOLDOUT untouched, all figures EXPLORATORY.
- **Phase C: p_fail and loss_fail_bps are relabelled SCENARIO (not calibrated).** They come from the measured markout curve at the retry and unwind times, labelled with their source and the TRAIN days used, and stay scenario values until real execution logs exist. Failure costs are reported per scenario. (`loss_fail_bps 7.8` was labelled calibrated but came from a simulated hedge-failure study, not execution logs; `p_fail 0.07` is a placeholder.)
- **Phase B (now), report only: hedge execution and recovery audit.** No code change. Audit each item in the backtester and in the core (file and function, whether v1_2 uses it, which test pins it, the catalogue case H01-H40): (1) submit with an idempotent client order id; (2) partial fill, only the residual is hedged; (3) reject classified by a venue code table, retry permitted or not; (4) timeout or unknown: query the venue before resubmitting, an unacknowledged order is never assumed failed; (5) retry limit and maximum residual-exposure duration; (6) escalation: deterministic emergency hedge or unwind when limits are breached; (7) reconcile positions and outstanding orders before recovery is declared complete. The core idempotency tests sit in tests/platform_kit and do not run (hft_platform_kit._ems is not built): say which of them cover items 1 and 4 and whether they can run without the native module.
  - Preliminary findings supplied by the owner on 7 Nov, NOT yet verified by this audit: (1) the backtester has no client order ids, the core has `idempotency_key`; (2) both hedge the residual, the core has no fill-triggered hedge yet; (3) the reject-code table (`venue_semantics_perp.py`) is backtester-only, the core has free-text `reject_reason` only; (4) query before resubmit exists for lost acks and, for timeouts in outages, only with the v1_2 switch `query_before_replace`, and in the core (OMS03, H15); (5) retry (`max_retries 2`, backoff 0.25 s) is backtester-only, the core goes straight to unwind (H03), residual limit core 5 s against backtester 30 s (H22); (6) unwind band 15 bps against 10 bps; flatten on breaker trip missing in both (H25, Phase E); (7) restart recovery never run end to end (H36). Nothing records per-event retry intervals and residual-exposure durations. Tests: duplicate submission core-only and not running; lost acknowledgement, partial fills, retry exhaustion, delayed reports exist; repeated rejections backtester-only; recovery after restart not exercised end to end.
- **Phase C build (the specification is in the prompt, PHASE C):** one hedge lifecycle state machine in the core, one shared reject-code table, one unwind band and one residual limit (DECISION_PENDING_CALIBRATION), per-event recovery records, the seven tests through the core path and all 40 catalogue cases checked against the transition table. Built after the owner's approval.

- **After Phase F, strategy work in this order:**
  1. **S1, adverse-selection table in core `strategies/edge.py`.** It replaces the single expected hedge move and the uncalibrated block_on_toxic thresholds. Fitted on TRAIN, validated on SELECT, never HOLDOUT; shipped as a new profile version. **Fitted on the fills of the repricing strategy (S0, v1_3), not of v1_2.**
  2. **S2, the Phase 1-lite hedge cap sweep,** run with the S1 curve.
  3. **S3, Albers features.**
  The adaptive hedge limit stays deferred.
- Through-print (owner 7 Nov, final): the engine fix is applied (one print budget across the cross, through and ordinary rules; a netted print still reaches the through rule) and the rule stays OFF in v1_2 and off the freeze list.


## OWNER PLAN RECORD, 7 Nov 2026 (b): C16 accepted, step S0 quote repricing
- **Cause (C16):** resting quotes are never repriced. `maker_requote_bps` is declared and unused; a quote leaves the book only by a fill, the 15 s time in force or a breaker trip; `maker_max_live_orders` is 1. On 24 strategy fills the hedge-venue price drifted a mean &minus;4.8 bps between quote and fill, believed gross edge +8.15 bps against realised +3.83, net after fees &minus;2.17 bps (EXPLORATORY).
- **Order of work:** (1) finish the Phase B items: rerun the ledger and stamp cells on the final v1_2, the placement_book truth cell alone, the four-model range once, then the freeze proposal (content and release identity). **v1_2 is frozen as the simulator-correct baseline with the strategy unchanged, only after the owner approves.** Stop for review after the freeze proposal. (2) **S0, new: quote repricing as profile v1_3, placed right after the v1_2 freeze and before Phase B2.** (3) Phase B2, then Phases C to F, then S1 (adverse-selection table, fitted on fills of the repricing strategy), S2, S3. The adaptive hedge limit stays deferred.
- **S0 specification (design first, report only; build after the owner approves the design):**
  - Lives in the core strategy, so live uses the same rule. Remove or implement `maker_requote_bps`; no strategy setting may be declared and unused: add a test that fails on unused strategy settings.
  - Rule: cancel or reprice a resting maker quote when the hedge-venue price moves so that the edge recomputed at the current hedge price falls below the minimum edge, or by more than a set number of bps. It must include the cancel latency (the 35 ms floor to Binance), the fill-after-cancel race (catalogue H30), a minimum interval between reprices and the venue rate limits (a reprice is a cancel plus a new order).
  - Measured alone against the frozen v1_2, on the probe (7 TRAIN days) and on the strategy runs: quote age at fill, quote-to-fill drift, believed against realised edge, cancel and reprice counts, fills, and net P&amp;L after fees. Validated on SELECT only; HOLDOUT untouched; all figures EXPLORATORY.
  - Open question for the owner's lead before building: does the live bot already reprice its Bybit quotes when Binance moves? If it does, S0 copies the live rule exactly; if it does not, live has the same loss.
  - **S0 design (owner 7 Nov, recorded; report C19):** port the requote mechanics of `FAST_MM/Fast_MM_Codebase/bybit_order_manager.py` (`update_quotes`, `amend_order`; a spot altcoin maker: mechanics only, none of its numbers) into the core EMS passive tactic (`crypto_stat_arb/ems/tactics/passive.py`): amend first with cancel-and-replace on rejection, a stickiness band, a debounce after jumps, a maximum quote age and a queue hold. The trigger is the strategy's edge recomputed at the current hedge-venue (Binance) price, not Bybit's mid. Band, debounce, minimum interval and age are fitted on the 7 TRAIN probe days and validated on SELECT; amend is modelled as losing queue priority (`MakerExecutor.amend`); the reprice rate is reported against both venues' rate limits (design estimate in C19: the danger is bursts, so a minimum interval is part of the rule). No strategy setting may be declared and unused (`maker_requote_bps` and the EMS `primary["reprice"]` today).
