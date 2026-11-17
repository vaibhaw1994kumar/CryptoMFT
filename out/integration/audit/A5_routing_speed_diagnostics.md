# A5 - routing, speed, diagnostics (Phase 0 audit, read only)

Agent A5. Working copy only. Paths: core = `crypto_MFT_arb_platform/src/crypto_stat_arb/`, bt = `crypto_MFT_arb_platform/src/new_backtest/`, tests = `crypto_MFT_arb_platform/tests/`, OI = `out/integration/`.
Commands run (read only): `pytest tests/test_venue_selector.py tests/test_order_split.py` -> 35 passed; `pytest tests/test_markout_by_venue_role.py tests/test_shortfall_split.py` -> 28 passed (PYTHONDONTWRITEBYTECODE=1, `-p no:cacheprovider`); one `load_profile`/`apply_profile` call for v1_2, v1_9, v1_9s (output quoted below). No other job was run. No order ids or client ids are quoted in this file.

Effective config, resolved by `new_backtest.profiles.load_profile` + `apply_profile` (command run 16 Nov; every value comes from the profile yaml or the dataclass default):

| key | v1_2 | v1_9 | v1_9s |
|---|---|---|---|
| runtime | backtester (old loop) | session | session |
| config.split (SplitConfig) | None | None | None |
| config.impact | None | None | None |
| venue_state.depth_quality | False | False | False |
| execution.tactic_choice | off | off | off |
| execution.pairing | maker_taker | maker_taker | maker_taker |
| execution.risk_reservation | enforce | enforce | enforce |
| markout_horizons_s | (1.0, 5.0, 30.0) (dataclass default, `bt/backtester.py:156`) | same | same |
| timing.processing_ms | {} (ASSUMED_ZERO) | {} | {} |
| arb.maker_venues | ['bybit'] | ['bybit'] | ['bybit'] |

Consequences: the routing evidence (`depth_quality`, `impact`, `order_split`) is OFF in every arb profile; it is switched on only by the run wrapper flags `--depth-quality`, `--impact`, `--split` in `OI/day1/run_day.py:29-58`. v1_2 runs on the old backtester loop, so `ExecutionLayer.set_routing` (called only from `core/session.py:346`) never runs for v1_2. `layer_route_mode` is not a profile key: it is a `TradingSession` constructor argument, default "count" (`core/session.py:189`), and "enforce" is used only in tests (`tests/test_session_replay.py:737`).

---

## 0.2i - venue-selection code and Phase E status
- Status: PARTIAL
- Exists:
  - `core/ems/venue_selector.py` (347 lines), pure functions, no clock, no book. `assign_rest_hedge` :56 (modes fixed|dynamic; cost per filled unit = maker fee + hedge taker fee + hedge slippage + adverse + miss_rate x miss_loss, :51-53; fill probability reported beside, not in the cost, :29). `split_taker` :97 (marginal cost of the next unit, price moved by the taker fee, per-venue notional limit, unavailable venue skipped). Cont-Kukanov order split: `SplitVenue` :151, `split_single_venue` :187 (closed form p = (2h+f+r')/(lambda_u+theta+h+r')), `expected_split_cost` :218, `split_multi_venue` :251 (stochastic approximation, deterministic by seed), `SplitTable` :294 (lookup by queue and recent volume), `SplitConfig` :323.
  - `core/ems/layer.py`: `set_routing` :160 and `decide_route` :167 (fixed assignment only), `RouteDecision` :499, `IntentObserver.route_venue` ~:550, `set_evidence_provider` :135, `_evidence` :142, `suggest_tactic` :152.
  - `core/ems/tactics/base.py`: `MarketState` :68 (note: a different class from `core/simulation/models.py:30` with the same name), `TacticChoiceConfig` :110, `choose_tactic` :145 (reason codes `TACTIC_CHOICE_REASONS` :53 incl. `SPLIT_MARKET_LIMIT`, `LQ_WAIT_CHEAPER`, `LQ_DEPTH_LIMITED`), `_split_choice` :253. `tactics/passive.py` `decide_reprice` :101, `tactics/ioc.py`, `tactics/market_banded.py`.
  - Replay glue: `bt/session_replay.py` `_routing_evidence` ~:520, `_split_evidence` ~:560 (calls `split_single_venue` :570).
- Wired into: replay only, and only as observation. Fixed rest/hedge assignment is live in the session path (replay, paper via `LivePaperSession` -> `TradingSession`, `core/paper_live.py:127`; and any live use of TradingSession): `session.py:346` -> `layer.set_routing` -> `decide_route` -> `oms.py:3413` (count: counted; enforce: order rejected back). `assign_rest_hedge` `dynamic`, `split_taker`, `split_multi_venue`, `SplitTable`, `choose_tactic` are called by no run path (grep over `core/` and `bt/`: `choose_tactic` only via `suggest_tactic`, which is None while `execution.tactic_choice` is off and is REFUSED with pairing maker_taker, `core/ems/config.py:59`; so it can never run for an arb profile). The evidence provider is installed only at `bt/session_replay.py:295`, so paper and live never produce routing evidence.
- Effective config: see table at top (all off).
- Tests and evidence: `tests/test_venue_selector.py` (11 tests: hand-computed cost, fixed vs dynamic, unavailable venue never chosen, fill probability not folded in, taker split with notional limit), `tests/test_order_split.py` (24 tests incl. `test_every_decision_carries_the_evidence_and_the_run_is_unchanged` :209), `tests/test_session_replay.py:725-750` (fixed routing, enforce rejects a different venue). Recorded run: `OI/cost/f_window/` (one run with `--depth-quality --impact --split`; 6 intent records carry `order_split`, `depth_quality`, `impact`). Study arms: `OI/cost/t5_split.py`, `OI/cost/run_t2.sh`.
- Exact gap: (1) `set_routing` (`layer.py:164`) feeds `assign_rest_hedge` an all-zero cost venue pair and discards `alternatives` and `dynamic_choice`: the one wired call ignores fees, spread, impact and adverse selection, i.e. exactly the T2 warning. (2) No reroute: `route_rejected` (~:575) only journals; an order is never sent to another venue. (3) `split_taker` and the dynamic mode have no caller. (4) The T2 study line "chosen by E: NOT RUN" (`OI/cost/G_RESULTS.md:39`).
- Phase E status (from `OI/single_platform_plan.md:248-256`, :309): order E0, E-breaker, E1, E2, E4, E3, E5. E0 steps (a) intents, (c) risk gate, (d) fixed route decision, (e) intent id, (g) observation logging are present in code; breaker decisions of 13 Nov are recorded as built. The venue selector inside the session runtime is the part of Phase E that is NOT built beyond the fixed rule. The plan states "No live trading until E0, E-breaker, E1 and E2 pass"; I did not verify the E1/E2 gate results (UNVERIFIED, belongs to A2/A6).
- Implementation: extend `ems/layer.py` `set_routing`/`decide_route` to call `assign_rest_hedge` with real inputs (fees from `core/fees.py:88` FeeSchedule, spread/impact from `simulation/impact.py`, adverse from `strategies/edge.py` adverse table) in `fixed` mode first (report only), then `dynamic` behind a profile switch. No new file.
- Acceptance test: replay v1_9 on the 29 Oct capture with report-only mode: every route decision journaled with chosen + alternatives + cost difference; identity of orders/fills with the current run (0 differences).
- Dependencies and risk: 8.2, 8.4. Enabling `dynamic` changes which venue rests: strategy-policy change, owner decision.
- Completion evidence: a recorded run whose journal holds a route record per decision with real costs, plus the identity diff.

## 8.1 - Phase E status and the routing evidence kept today
- Status: PARTIAL
- Exists: what is kept today per run (arb session replay): `route_decisions` counter only (e.g. `OI/day1/runs/clean_day1_v19/session_manifest.json` -> `execution_layer.layer.counts.route_decisions` = 13; 5 primary + 8 hedge intents), intent journal `session_intent_journal.jsonl` (events intent/child/state/retarget; for clean_day1_v19: 13 intents, 13 children, 28 state rows), `session_ems_path_coverage.json` (tactic by order, placement), `maker_observations.jsonl` (maker_place with touch, queue_ahead; maker_fill; hedge_arrival with touch_at_arrival), `route_decision_differs` / `route_rejected` journal rows only when a decision differs (`layer.py` ~:560-575). With `--depth-quality/--impact/--split`: per intent `depth_quality`, `impact`, `order_split` keyed by venue (`layer.py:145-152`, `_create` :218-222); only `OI/cost/f_window` holds such records (checked all `OI/cost/*/session_intent_journal.jsonl`: split/dq/impact present only there).
- Wired into: replay (opt-in); paper/live: fixed routing only, no evidence provider (`set_evidence_provider` has one call site, `bt/session_replay.py:295`).
- Effective config: evidence off in v1_2, v1_9, v1_9s (table at top). `JOURNAL_MAX = 200_000` (`layer.py:31`): `_create`, `_move`, `retarget`, `route_*` all guard with `len(self.journal) < JOURNAL_MAX` and drop silently, with no dropped-count (grep `journal_dropped` finds nothing). Journal rows are also capped by the same constant for the evidence rows.
- Tests and evidence: `tests/test_depth_quality.py:491,505` (provider on/off), `tests/test_order_split.py:209`; `OI/cost/f_window`.
- Exact gap: a normal route decision (chosen venue equal to the order venue) is not journaled, only counted; no candidate list, no costs, no reason string stored for the normal case; no versions or snapshot refs.
- Implementation: extend `ExecutionLayer.decide_route` to append one journal record per decision (reason, chosen, alternatives, inputs); add a dropped counter beside `JOURNAL_MAX`.
- Acceptance test: replay of a day: count(route journal rows) == count(`route_decisions`); a forced journal overflow reports the dropped count.
- Dependencies and risk: 8.4; journaling is observation only, but it changes journal bytes (identity gates compare journals: exclude the new field as E0 did).
- Completion evidence: a day replay whose manifest shows journal rows == decisions and dropped == 0.

## 8.2 - venue selector for routable orders: inputs
- Status: PARTIAL
- Exists (input -> where it exists today; "in selector" = a field of `RestHedgeVenue`/`TakerVenue`/`SplitVenue`):
  - expected executable price and net edge: `core/strategies/edge.py`, `core/strategies/arb_strategy.py` (net edge, the one edge place); executable price through `core/simulation/impact.py:86` `fill_range_totals`, :153/:202 `cost_bps`; NOT in selector.
  - depth at size within cap: `core/simulation/venue_state.py` `gap_depth` :470, `depth_quality` :497; `DisplayedDepthImpact.effective_depth_within_cap` (used `session_replay.py:~565`); in selector as `max_market_qty`.
  - spread, impact: `SplitVenue.half_spread_bps`, `impact_bps` (selector); `RestHedgeVenue.hedge_slippage_bps` (input only, nothing computes it).
  - fees and rebates: `core/fees.py:88` FeeSchedule; selector fields `maker_bps`, `taker_bps`, `fee_bps`, `rebate_bps`. MANDATORY per T2; present in the selector, NOT passed by the one wired call (zeros at `layer.py:164`).
  - latency and variability: `core/simulation/timing.py:46` TimelineSpec (mean, jitter, source) and `core/latency.py:48,206`; values ASSUMED from the singapore preset (v1_2 `timing.from_latency_preset`); measured grids only from ollog testnet/paper probes. Not in selector.
  - recent fill quality / adverse selection: `core/strategies/edge.py:164,233` adverse table (v1_6+), `MarkoutTracker` (`simulation/latency_model.py:492`); selector `adverse_bps` (an input to be supplied; no wired source).
  - venue health, rejects, throttling headroom: `core/book_gate.py:17`, `core/time_integrity.py:67,134`, `core/circuit_breaker.py:176`, `core/rate_limits.py:109` (ScopedRateLimiter; `snapshot` used by `run_day.py` limits file), postonly reject model (`bt/postonly.py`); selector: `available` (bool), `miss_rate`, `miss_loss_bps` (inputs only; nothing measures them).
  - inventory/risk/hedge availability: `core/risk.py`, `core/ems/risk_gate.py`, `ReservationBook` (`layer.py`), `core/hedge_lifecycle.py:111`; selector: `max_notional` only.
  - liquidity concentration (HHI): MISSING (grep for hhi/herfindahl/concentration over core and bt: no match).
  - recent volume: `VenueState.recent_volume` :410 (used by `_split_evidence`).
  - resilience: `VenueState.survival` :396, `flicker` :456 (A3).
  - min size / precision: `core/instruments.py:156-158` (tick, min_notional), `core/contract_master.py`; `limit_on_grid`.
- Wired into: replay only, observation only (`_split_evidence` fills `SplitVenue` from the replay venue). Paper/live: not wired.
- Effective config: split/impact/dq None/off in all three profiles.
- Tests and evidence: `tests/test_venue_selector.py`, `tests/test_order_split.py`. T2 evidence `OI/cost/G_RESULTS.md:33-40` (100 SOL, 2026-10-29 capture only, 3 bursts, wide intervals): fixed on Bybit -0.62 bps net per fill (-2.43 to -0.03); fixed on Binance -6.18; chosen by effective depth -4.69. The depth-only rule picked the wrong venue (named Bybit hedge at 6 of 7 quotes of the Bybit arm). Arm "chosen by E" NOT RUN.
- Exact gap: no routable-order caller (unwinds are `OWN_VENUE` in `decide_route`; MM quotes use pairing none; nothing chooses among several venues); inputs for latency, reject/miss rate, throttle headroom, health, inventory are not assembled into one structure; HHI missing; no scoring across more than two venues (`assign_rest_hedge` requires exactly two venues, `venue_selector.py:60`); `split_taker`/`split_multi_venue` unused.
- Implementation: extend `ems/venue_selector.py` (add health and rate-limit headroom fields to `RestHedgeVenue`; HHI as a reported field, not a score input) and give `ExecutionLayer` an input-assembler method that reads the existing modules above. Overlap: `core/ems/ioc_policy.py` and `ioc_runtime.py` (IOC policy) must be checked before adding any chooser: not read in depth by A5 (UNVERIFIED overlap).
- Acceptance test: unit test with fee-aware inputs where depth alone picks venue X and fee-aware cost picks venue Y (the T2 case), asserting Y; replay in report-only mode (see 0.2i).
- Dependencies and risk: 0.2i, 8.4, A1 (health), A4 (dislocation). Do not enable `dynamic` for arb hedges (they stay opposite by construction).
- Completion evidence: input table in the journal record for each routable decision, `unwinds` routed through the selector in a recorded run.

## 8.3 - alternatives compared per decision; duplicate-exposure prevention
- Status: PARTIAL
- Exists: alternatives returned with cost: `AssignmentDecision.alternatives`/`dynamic_choice` (`venue_selector.py:41-48`), `SplitResult.alternatives` (:90-94), `OrderSplit.inputs`; tactic alternatives by reason code (`choose_tactic`: wait/rest = PASSIVE `LQ_WAIT_CHEAPER`, sweep = `LQ_DEPTH_COVERS`, split = `SPLIT_MARKET_LIMIT`, partial = `LQ_PARTIAL_RESIDUAL`; `base.py:53-58`). Duplicate exposure and conflicting orders: one live hedge child per intent (`layer.py` `hedge_blocked` ~:330, `one_hedge_child`), hedge only after a maker fill unless simultaneous (`_validate_open`), atomic reservation (`ReservationBook`, `open_primary_group`), retry covers the unfilled remainder only (`retarget`), maker lifetime and `maker_max_live_orders`; tests `tests/test_execution_layer.py`, Gate 0 mutations (plan E0 text).
- Wired into: one venue + fixed rest/hedge (all arb); duplicate-exposure rules wired in replay, paper and the session path (the layer sits in `TradingSession`). Alternatives "split", "sequential", "primary+fallback", "wait" are computed only in observation (`order_split`) or in study scripts; `choose_tactic` is refused for arb pairing.
- Effective config: `execution.tactic_choice off`, `order_split` flag False by default (`base.py:129`).
- Tests and evidence: `tests/test_order_split.py:152-175` (SPLIT_MARKET_LIMIT, emergencies never split); `OI/cost/G_RESULTS.md` T5 (E vs all-market; superseded by price-aware rerun, gains 0.02-0.09 bps, NO EFFECT at risk weight 1), LARGE_GAPS 4c/5 (covered by LARGE_GAPS step 4c, 5).
- Exact gap: no per-decision journal of all five alternatives with their expected costs; fallback is explicitly not supported (`route_rejected` never retries elsewhere); "wait" has no arb path.
- Implementation: extend `layer.py` decision record to hold the alternatives list (one-venue, split, sequential, fallback, wait) from the existing functions.
- Acceptance test: replay with a forced hedge-venue outage: the record shows the fallback alternative with its cost and the reason it was not taken (or is taken).
- Dependencies and risk: 8.2, 8.4; a fallback venue adds duplicate-exposure risk: the existing `hedge_blocked` and reservation rules must cover it first.
- Completion evidence: a journal showing five alternatives per decision and a passing duplicate-exposure mutation check on the fallback path.

## 8.4 - routing evidence: immutable record per decision
- Status: PARTIAL
- Exists: journal records (`layer.py:218-222`) hold: intent id, signal id, role, side, qty, venues, worst_price, urgency, tactic_hint, reason, parent id, created_ts, seq; optional per-venue `depth_quality`/`impact`/`order_split` (with a label string and the ASSUMED/PROXY caveats). Plain Python lists in memory, written by `write_result` as `session_intent_journal.jsonl` (`bt/session_replay.py:~985`).
- Wired into: replay (opt-in evidence); paper/live write nothing of this kind (no provider).
- Effective config: evidence off (table at top); `JOURNAL_MAX = 200_000` (`layer.py:31`).
- Tests and evidence: `tests/test_order_split.py:209`; `OI/cost/f_window` (6 records).
- Exact gap (each is a failure against the acceptance "reproduce a historical decision from the record without future data"): (1) NOT immutable: list of mutable dicts; no hash/sequence chain. (2) Records past `JOURNAL_MAX` are dropped silently, with no counter. (3) No code/profile/model version, no snapshot or feed reference, no book snapshot or levels in the record, no fees used, no health state, no candidates and scores, no rejected alternatives, no chosen-route reason for the normal case (only `decide_route` reason is returned, journaled only when different). (4) Evidence keyed by venue is derived from live view objects at decision time but the inputs (queue, recent volume, outflow samples file name) are summarised, not the book; the outflow ratio file is a path (`SplitConfig.outflow_ratio_file`), not a hash. (5) `maker_observations.jsonl` and the journal are separate streams with no shared decision id except `intent_id`.
- Implementation: extend `ems/layer.py` (journal record) and `bt/session_replay.py` `_routing_evidence` (inputs, snapshot ref = feed offset/sequence, `release.py` version, profile sha). No new file needed; hashing can reuse `core/release.py` and `core/dataset_manifest.py`.
- Acceptance test: take a recorded day, pick a decision, re-run `decide_route` + `_split_evidence` from the record only (no book access) and assert identical chosen route and costs; fail if any needed field is absent.
- Dependencies and risk: 8.1, A2 (journal/determinism), A1 (snapshot refs). Larger journals: memory; use the existing cap with a counter.
- Completion evidence: reproduction test passing on a 29 Oct 2026 replay decision, and a day with dropped == 0.

## 9.1 - Almgren-Chriss schedule as a hedge/unwind mode
- Status: PARTIAL (study script only; covered by LARGE_GAPS step 4c)
- Exists: only in the study script `OI/cost/t5_split.py`: sinh trajectory `x_i = S sinh(kappa H (1 - i/n))/sinh(kappa H)` at :476 and :750 (kappa H in {0 (linear), 2}), N in {2,5,10} slices over H in {5,30} s (:333), arms M (all at once), E (Cont-Kukanov), P (plain slices), L (slices sized by the fill-range rule 1). Core pieces the arm calls: `OwnTakeLedger` (`core/simulation/impact.py:99`), `ImpactOverlay` (:249), `fill_range_totals` (:86), `split_single_venue`. Result: `OI/cost/LARGE_GAPS_RESULTS.md:184-197` (4c): time slicing buys no cost (-0.01/-0.09/-0.24 bps at 100/500/1000 SOL), carries 2-14 s unhedged, net worse at risk weight 1; with the overlay reaction ON slices cost more than all at once; rule-1 sizing identical to plain slices (never bound).
- Wired into: nothing in core (grep for almgren/twap/schedule over core and bt: none). The `schedule_twap`/`schedule_vwap` in `trading_platform_poc/ems.py:1044,1063` are out of scope (POC stack), not a candidate.
- Effective config: none (no switch exists).
- Tests and evidence: no test in `tests/` for the trajectory; evidence is the study output `OI/cost/t5l2/report_large2.txt` (named in LARGE_GAPS_RESULTS.md:185; not re-opened by A5) on the 29 Oct 2026 capture only (single day, Binance hedge venue real depth, 10,012 decisions per size).
- Exact gap: no core `hedge_mode` (fast | N slices over T | wait); trajectory function lives only inside a script (CLAUDE.md: no logic in `out/`); the study's costs are a PROXY (own-consumption refill 3.5 s ASSUMED, overlay MODELLED).
- Implementation: move the trajectory into `core/ems/tactics/base.py` or `ems/venue_selector.py` as a pure function beside `split_single_venue`, with a `MarketState`/layer hook; wire only after the owner decides (LARGE_GAPS_RESULTS says no hedge mode beats all-at-once at 100-1000 SOL on the risk-adjusted net).
- Acceptance test: unit test of the trajectory (sums to S, kappa 0 equals linear); replay of a 100 SOL hedge on the capture reproducing the study's M-vs-P difference within tolerance.
- Dependencies and risk: 9.2, LARGE_GAPS 4c; unsafe to enable: more unhedged time (drift p99 6-17 bps against 1.0 for all-at-once).
- Completion evidence: core function + test + a replay row matching the t5l2 numbers.

## 9.2 - compare schedules on the same historical conditions
- Status: PARTIAL (study only)
- Exists: `OI/cost/t5_split.py large2` runs arms M/E/P/L on the same decision moments (decisions every 5 s, same capture, first half chooses, second half reports; `OI/cost/LARGE_GAPS_RESULTS.md:185`), incl. "after an adverse move" subset (611 decisions); March 2024 T5 version (Binance best bid/ask only, depth assumed) in `OI/cost/G_RESULTS.md:22-31`.
- Wired into: study scripts only; not part of replay, paper, or testnet.
- Effective config: none.
- Tests and evidence: `OI/cost/t5l2/*` (not opened); numbers quoted in LARGE_GAPS_RESULTS.md 4c. Bootstrap intervals exist in the study; one day only.
- Exact gap: one capture day (29 Oct 2026); no HOLDOUT read for the large sizes; paper/testnet runs do not compare schedules; no comparison harness in `src/`.
- Implementation: extend `bt/batch_backtest.py` (the planned runner home) with a schedule arm switch once 9.1 is in core.
- Acceptance test: same decision moments, identical inputs, arms differ only in the mode; result table reproducible from a command with a fixed seed.
- Dependencies and risk: 9.1; compute heavy (not run by A5).
- Completion evidence: a table per schedule from a core-path replay, equal to the study within a stated tolerance.

## 10.1 - existing outputs: markouts 1/5/30 s, cost breakdowns, ledger P&L, pair economics
- Status: PARTIAL (overlaps A4 0.2h; merge to compare)
- Exists and checked:
  - Markout, old path: `MarkoutTracker.resolve` (`core/simulation/latency_model.py:~560`): bps = sign x (mid_at_horizon - fill_price)/fill_price x 1e4, positive = favourable; qty-weighted; horizons (1, 5, 30) from `bt/backtester.py:156` default. Reported as `markout_summary` and `markout_by_venue_role` in the manifest. Timestamp basis: the mid passed at resolve time (event time when the horizon is passed; the run `finalize`s the rest at the last mid).
  - Markout, decomposed (`decompose=True`, `run_day.py` sets `decompose_markouts=True`): captured = s (m0 - price)/m0, adverse = s (m_h - m0)/m0, fee = -fee/(price qty), net = sum; m0 = the venue mid just before the event that caused the fill; m_h = mid as of fill_ts + h read strictly before the event at the target (`resolve_asof`); the denominator is m0, not the fill price (the 09 Nov note uses the fill price; documented difference < 1%, `latency_model.py` docstring); `mid_max_age_s` is None (stale-mid rule off in the profiles run). Output `session_markouts_decomposed.jsonl`.
  - Cost breakdowns: `metrics.implementation_shortfall` (`core/metrics.py:623`), `shortfall_split` :549 (delay_drift, hedge_latency_drift, hedge_impact, fee_diff + quote_price_term - believed_allowances; residual ~1e-9 checks arithmetic), `opportunity_cost` :602. Not called by any run (docstring :561; invoked from `OI/cost/shortfall_report.py`, `probe_shortfall.py`).
  - Ledger P&L: manifest `cash_by_venue`, `equity_at_book_mids`, ledger events (A4 owns formulas).
  - Pair economics: `metrics.pair_economics` :475 -> `session_pair_economics.jsonl/.json`: gross bps = (hedge avg - maker price)/maker price (sign by side), net = gross - (maker fee + hedge fees)/(price qty); net only for fully hedged pairs; funding and unwind cost excluded (note in `metadata_block`).
- Wired into: replay (session runtime) outputs; paper writes `metrics_report.json` (fill rates, `implementation_shortfall`, time-weighted exposure; D7d runs); testnet/ollog does not compute them.
- Effective config: horizons (1, 5, 30), decompose only via run wrapper flag, `mid_max_age_s` off, `processing_ms` {}.
- Tests and evidence: `tests/test_markout_by_venue_role.py` (28 tests with `test_shortfall_split.py`, all pass; hand-computed captured 2 bps, rebate +0.5 bps, a sell captured 3 bps / fee -2 bps), `tests/test_shortfall_split.py` (parts sum to the shortfall, both sides, fee difference). Recorded: `OI/day1/runs/clean_day1_v19/session_lifecycle_report.json` adverse tails 1 s/5 s/30 s with n = 8 each (small sample).
- Exact gap: completeness is only reported as counts per horizon (`excluded_counts`) in the decomposed path; the pooled `markout_summary` path has no exclusion counter for mid <= 0 (`resolve` just `continue`s). `pair_economics` reads only rows with reason "maker leg" (`metrics.py:495`), so market-making quotes (pairing none) get no pair rows. Samples are tiny (8 fills). Unwind cost and funding are outside the pair net. March 2024 has no funding file.
- Implementation: extend `metrics.py` (`pair_economics` for pairing none; exclusion counter in the old resolve).
- Acceptance test: replay day; every fill appears in exactly one of {kept, excluded with reason} per horizon; pair rows for MM quotes.
- Dependencies and risk: A4 0.2h, 10.2, 10.3.
- Completion evidence: a recorded day with 1/5/30 s coverage table (kept + excluded = fills) and MM pair rows.

## 10.2 - fill-time context snapshot per fill
- Status: PARTIAL
- Exists today at a fill: `maker_observations.jsonl` `maker_fill` rows: touch at the fill (both venues), `queue_ahead_at_placement`, `queue_class`, `relative_queue_position_at_fill`, trigger, `ts` (keys read from `OI/day1/runs/clean_day1_v19/maker_observations.jsonl`); `maker_place` rows: touch, level_size, queue_ahead; `hedge_arrival`: `touch_at_arrival`. `session_pair_economics.jsonl`: maker/hedge timing deltas. Decomposed markouts carry `mid_at_fill`. With `--depth-quality` per intent `depth_quality` (gap depth, survival, cancel-to-trade, spike) at the decision. `VenueState` has imbalance, microprice, short vol, trade sign, depth collapse (`core/simulation/venue_state.py:559-607`, `simulation/models.py:30-70`), `regime_sample` (vol, spread, liquidity terciles, `metrics.py:344`) for the lifecycle report.
- Wired into: replay. Not wired: paper/live per-fill snapshot.
- Effective config: depth_quality False; lifecycle report only when `regime_boundaries` file is passed (`run_day.py:--regime-boundaries`).
- Tests and evidence: no test that a fill carries a full context row; run output above.
- Exact gap: no per-fill snapshot row. VPIN is MISSING in the core (the `vpin` field exists, `simulation/models.py:69`, but `to_market_state` never fills it; `microprice_deviation_bps`, `microprice_drift_bps`, `trade_burst_flag`, `liquidity_evaporation`, `cancel_burst`, `queue_fragility`, `book_hole`, `sweep_risk` are hard-set to 0/False in `venue_state.py:575-600`); the FAST_MM implementations are MM-side evidence only (`FAST_MM/flow_features.py`, `regime_gate.py`, A3). No venue-health or feed-delay field at the fill; no inventory/risk snapshot at the fill; no single measurement timestamp.
- Implementation: extend `bt/session_replay.py` `_obs_fill`/maker_observations (the port target of E0 (g)) to write one context row per fill from `VenueState.to_market_state`, `depth_quality`, `HostClockMonitor`, risk snapshot; fill the zero-hard-set fields only from real computations (never synthetic).
- Acceptance test: every fill row has all context fields or an explicit "missing: reason"; replay of a day: fills == context rows.
- Dependencies and risk: A1 (feed delay, health), A3 (book shape), 10.3.
- Completion evidence: context rows for every fill of a recorded day with no silent nulls.

## 10.3 - favourable vs adverse: the MM "+2 captured / -4 adverse" definition
- Status: PARTIAL
- Exists: the literal text "+2 captured / -4 adverse" was NOT found: command `grep -nIi --exclude-dir=hft_scaffold --exclude-dir=Fast_MM_Codebase -E "captured.{0,30}adverse|\+2.{0,40}-4|-4.{0,30}adverse" FAST_MM/*.html FAST_MM/*.md` returned no lines. Definitions found:
  - Core (post-fill): `MarkoutTracker(decompose=True)` (`core/simulation/latency_model.py:492`; docstring "Sign convention (bps, positive = good for us ...)"), captured = s (m0 - price)/m0 x 1e4, adverse_h = s (m_h - m0)/m0 x 1e4 (negative = bad), M(h) = captured + adverse_h, net = M + fee. The only numerical example in the repo: `tests/test_markout_by_venue_role.py:43-45,76-82` (buy at 99.98 vs mid 100.00: captured +2 bps; adverse +2 when the mid rises 2 bps). A "+2 captured, -4 adverse" case would be M = -2.
  - FAST_MM (pre-fill, MM-side evidence): `FAST_MM/bybit_order_manager.py:3509-3530` (bid) and :3639-3654 (ask), the "[adv-fill]" diagnostic: adverse_bps = (placement_mid - fill-time mid)/placement_mid x 1e4 for a bid; positive = adverse; half-spread = (placement_mid - fill price)/placement_mid; ratio = adverse/half-spread. It is a PRINT line (`print(...)`), not a stored or aggregated metric.
- Wired into: core decomposed markout: replay with `decompose_markouts=True` (`run_day.py:78`), not paper/testnet. FAST_MM: MM live/dry-run print only.
- Effective config: decompose off by default (`session_replay.py:96`).
- Tests and evidence: `tests/test_markout_by_venue_role.py` (decompose tests). Recorded: `session_markouts_decomposed.jsonl` in `OI/cost/*` and `OI/day1/runs/*`; lifecycle report adverse tails (p5/p95 by horizon, n = 8 on clean_day1_v19). CIs: `metrics.distribution_stats` / `block_bootstrap_ci` (:185, :213) used in `pair_economics`; `decomposed_summary` carries notional-weighted net but no CI.
- Exact gap: (1) CONFLICT between definitions: window (placement-to-fill, FAST_MM, vs fill-to-horizon, core), sign (+ = adverse in FAST_MM, + = favourable in core), and denominator (m0 here vs fill price in the 09 Nov note). (2) The "+2 / -4" definition cannot be confirmed from text; do not reconstruct. (3) "split by conditions per milestone with CIs": the regime split exists (`lifecycle_report` by volatility, spread, liquidity, utc_hour; cells with fewer than 30 quotes show counts only, `min_bucket=30`) but only in count form for most cells, and the CI on the markout split is not computed; "milestone" is not defined in the repo.
- Implementation: extend `metrics.py` (`decomposed_summary` by regime with `distribution_stats` CIs); document the sign/window in `docs/metric_dictionary.md`. Never open `FAST_MM/hft_scaffold/check_link_orders_history.py`.
- Acceptance test: hand-computed fixture (+2 captured, -4 adverse -> M = -2, net after fee) through both tracker and report; regime cells with CI or "insufficient_support".
- Dependencies and risk: 10.1, 10.2, A3 (regime gate in FAST_MM), MM session M1-M4.
- Completion evidence: one report showing captured/adverse/M by regime with CIs on a recorded day, with the owner's definition pinned in the dictionary.

## 10.4 - one synchronized chart per session/day
- Status: MISSING
- Exists: no chart code for session or day. The only SVG in the repo reports are two design diagrams (`OI/reports/canonical_execution_layer_design.html`, `..._simplified.html`; counts of `<svg>`/`<canvas>`/`<img>` in all `OI/*.html` and `OI/reports/*.html` checked: zero in the run reports). No matplotlib/plotly import in `bt/` or `core/*.py` or `OI/` (grep).
- Wired into: not wired.
- Effective config: n/a.
- Tests and evidence: none.
- Exact gap: everything. Data needed for a synchronized chart exists as files: mid/spread (the feed), signals, orders, fills, markouts, exposure steps (`metrics.exposure_steps` :681), breaker/safety events (`session_safety_events.jsonl`).
- Implementation: extend `core/cycle_reports.py` (the HTML report writer already in core) or a report function inside `bt/` fed by the session output files; no new runner. Overlap: five report builders exist (see 10.8).
- Acceptance test: one HTML per run with a shared time axis: price + fills + hedge + exposure + feed health marks; opens offline.
- Dependencies and risk: 10.5, 10.2.
- Completion evidence: a chart file generated by the run for a recorded day.

## 10.5 - timing distributions per session
- Status: PARTIAL
- Exists, stage by stage (timestamps used; "assumed" = a configured model, not measured in that run):
  1. data receive -> processing: `TimingConfig.processing_ms["strategy"|"risk"]` (`core/simulation/timing.py:88-100`), a constant, default empty (ASSUMED_ZERO); event `receive_ts_ns` vs exchange ts exist on events; no measured distribution.
  2. processing -> decision: signals carry `ts_ns`, `observed_ts_ns` (`OI/day1/summarize_run.py:19`), `maker_place.ts`, `decision_ts` (strategy_quote_drift); not summarised as a distribution.
  3. decision -> send: configured `entry` timeline (mean, jitter, source) per venue (`timing.py:46,166-190`); measured on probes: ollog `orders.jsonl` has `t.send_*`, `t.csend_*` (mono and wall ns), exchange `ex.created_ms`.
  4. send -> ack: `ack_report` timeline (assumed); measured in ollog (`rest_*`, `wsnew_*`, `crest_*`, `wscan_*` stamps, `clock` offset and uncertainty).
  5. fill -> local report: `fill_report` timeline (assumed); ollog `wsnew`/exec stamps in `events.jsonl` (`ex_event_ms`, `recv_wall_ns`, `offset_ms`, `uncertainty_ms`).
  6. maker fill -> hedge decision and 7. hedge decision -> send: `pair_economics.hedge_send_delay_s` = first hedge child ts - maker fill ts (journal child ts vs ledger fill ts_ns; `metrics.py:~505`), reported with `distribution_stats`; no split into decision vs send.
  8. hedge send -> execution: `first_hedge_fill_delay_s` = first hedge fill ts - maker fill ts (so it is maker-fill-to-hedge-execution, not from send); `unhedged_interval_s` = last hedge fill - maker fill.
  9. execution -> confirmation: no output.
  Lifecycle report: `message_load_per_minute`, `residual_exposure.unhedged_interval_s` (p50/p99/max, clean_day1_v19: p50 0.036 s, n = 7), `hedge_completion`. `orders` rows for the lifecycle carry `sent_ts, ack_ts, cancel_req_ts, cancel_conf_ts` (`metrics.lifecycle_report` docstring :363-370) but these are not turned into distributions in the report body.
- Wired into: replay (session reports), ollog probes (raw stamps, `order_lifecycle_logger/ollog/summary.py`, `derive.py`: not read in depth by A5, UNVERIFIED); not paper (D7d writes `paper_timestamps.jsonl`, `core/paper_live.py:~140`, not inspected for distributions).
- Effective config: timing numbers ASSUMED from `latency_preset: singapore` in v1_2/v1_9/v1_9s (profile `timing.from_latency_preset`), `processing_ms` {}.
- Tests and evidence: `tests/test_wp3_timing.py`, `tests/test_wp3_timing_units.py` (model, not report). Evidence: the files above.
- Exact gap: no single section per session with the nine distributions and the timestamp pair for each; stages 1, 2, 3, 9 are not measured in any run; stage 8 definition mixes send and decision; replay timings are the configured model, so a replay report would only echo assumptions (must be labelled).
- Implementation: extend `metrics.lifecycle_report` (already receives sent/ack/cancel stamps) and `session_pair_economics` fields; add measured values from ollog `derive.py` for paper/testnet sessions.
- Acceptance test: per run a table of nine rows with n, p50/p95/p99, source (assumed | measured) and the two timestamp names.
- Dependencies and risk: A1 (clock offset, feed delay), A2 (timestamps), 10.6.
- Completion evidence: nine-row table in a recorded replay report and in a testnet probe report.

## 10.6 - cost section (pre-trade vs realised) and latency/operational health section
- Status: PARTIAL
- Exists: pre-trade estimate fields on the signal rows (`estimated_net_edge_bps`, `gross_edge_bps`; `OI/day1/summarize_run.py:19`); realised in `session_pair_economics` and `metrics_report.implementation_shortfall` (paper D7d). The Perold split exists but only through scripts (`OI/cost/shortfall_report.py`, `probe_shortfall.py`). Operational health: `session_limits_and_stops.json`, `session_safety_events.jsonl` (`OI/day1/run_day.py:90-110`), `core/monitoring.py`, `core/alerting.py`, `core/cycle_report.py` paper-watcher audit (`feed, order_books, time_integrity, ...` components, regenerated after each cycle).
- Wired into: replay via run wrapper in `out/`; paper cycle report automatic.
- Effective config: n/a.
- Tests and evidence: `tests/test_shortfall_split.py`; report examples `OI/e2e_rehearsal_2026-10-29.html`, `OI/v1_9_ems_acceptance_report.html` (mechanics, no pre-trade vs realised section).
- Exact gap: no report section that puts pre-trade estimate and realised side by side per run; the shortfall split is not called by a run (`metrics.py:561`) and needs a touch lookup (read from the capture, not from the replay's truth book: label in `shortfall_split.md`).
- Implementation: extend `session_replay.write_result` to call `shortfall_split` with the replay's own touch lookup; add the health section from the existing files.
- Acceptance test: report for a day shows Q1's four parts per fill and the health counters, with missing fields marked.
- Dependencies and risk: Q1, 10.5, A1.
- Completion evidence: a generated section on a recorded day.

## 10.7 - self-check page Q1-Q5 for sampled fills
- Status: MISSING
- Exists: nothing named Q1-Q5 in the repo reports (A5 grep of `OI/*.md`, `OI/reports`, `docs/` for a Q1-Q5 self-check page: none; `v1_9_TOLERANCES.md` and `report_claims_register.md` are different instruments). The five questions are defined only in the owner order (not in the repo), so A5 cannot confirm Q3-Q5 text.
- Wired into: not wired.
- Effective config: n/a.
- Tests and evidence: none.
- Exact gap: page, sampler (random fill sample with seed), and the Q-to-data mapping.
- Implementation: a report function in the same module that holds the other sections (10.8); sample from `session_pair_economics.jsonl` + context rows (10.2). `OI/reports/claim_rows.json` and `claims_checked.json` are an existing claim-checking mechanism (`report_claims_register.md`): check overlap before adding a second.
- Acceptance test: seeded sample of N fills, each with Q1-Q5 answered from recorded fields or "missing".
- Dependencies and risk: 10.2, Q1, Q2, A2 (Q3, Q5).
- Completion evidence: the page generated for a recorded day.

## 10.8 - generated automatically; arb and MM; asset-agnostic
- Status: PARTIAL
- Exists (report builders): (1) `OI/day1/build_report.py` (v1_9 acceptance, reads coverage matrix; absolute paths :9-10); (2) `OI/day1/build_rehearsal_report.py`; (3) `OI/day1/build_matrix.py`; (4) `core/cycle_reports.py` + `core/cycle_report.py` (paper-watcher cycles, HTML + PDF + zip; automatic after each cycle, `cycle_report.py:5`); (5) `bt/validation/build_report.py` (spot simulator validation); (6) `OI/day1/summarize_run.py` (24-line console summary); plus `core/scripts/build_maker_report.py`, `build_residual_inventory_report.py`. Replay outputs per run: `write_result` (`session_replay.py:~975-1012`) writes the jsonl/json files; no HTML.
- Wired into: replay: manual run wrappers in `out/`; paper: the cycle reports; testnet: none (ollog `summary.py`).
- Effective config: n/a.
- Tests and evidence: none for the builders.
- Exact gap (hard-coded assumptions found): `OI/day1/run_day.py:15` SESSION path `data/live_sessions/solusdt/2026-10-29/...`, `pair="SOLUSDT"`, `dataset="mar2024"`, `start_date/end_date="2026-10-29"`, `dated_ticks("2026-10-29")` (:70-71, :78); labels "no own orders... simulated fills", "S1 table out-of-period (SOL, fitted March 2024)" (:80); `profile_safety=(name == "perp_perp_maker_taker_v1_9")` (:78); `OI/day1/build_matrix.py:235` and `build_rehearsal_report.py:25` `LOT = 0.01` (Binance SOLUSDT lot) and "SOL"/"Bybit"/"Binance" in text; `OI/day1/build_report.py` body text "2026-10-29 SOLUSDT capture", "Binance has best bid/ask only", absolute `INT` path; `core/cycle_reports.py:112,447,473,510-512` BINANCE/BYBIT only; `OI/phase_b/strategy_quote_drift.py` venue flip bybit<->binance; `core/metrics.py:495` pair rows only for reason "maker leg" (arb); lifecycle_report is maker-venue specific; fee presets and hedge venue from the profile (not hard-coded, good). Logic sits in `out/` (CLAUDE.md breach) and five builders overlap in job.
- Implementation: one report module in `bt/` fed by the run's output dir and `session_manifest.json` (pair, venues, tick, lot from the manifest/profile), called by `write_result` and by paper/testnet teardown; retire the `out/` copies to `discarded/` per CLAUDE.md. State to the owner that this is "two or more modules doing the same job".
- Acceptance test: run the same builder on two assets (SOL and ADA or MNT) and on an MM session without code change; no literal asset/venue string in the builder.
- Dependencies and risk: 10.1-10.7, M1-M4 (MM).
- Completion evidence: generated reports for a SOL replay, an ADA replay and an MM session from one code path.

## Q1 - pre-trade estimate vs realised, four parts, last 30 days
- Status: PARTIAL
- Exists:
  - The "+8.15 believed / +3.83 realised" records, FOUND: report C16 in `OI/core_integration_report.html` (lines 228-239), `OI/integration_status_report.html:50`, `OI/single_platform_plan.md:205`, `OI/core_backtest_integration_prompt.md:141`; computed by `OI/phase_b/strategy_quote_drift.py` into `OI/phase_b/strategy_quote_drift.json` (file date 7 Nov 2026). Units: bps of the hedge-venue price at decision (drifts) and of the quote price (edges), before fees; adverse negative. Sample: 24 signals with a maker fill from EXISTING replay runs: 19 + 3 on 2022-01-13 / 2022-01-05 (v1.1), 2 on March 2024 (v1_2); 12 Bybit-maker, 12 Binance-maker. Means (SE over the 24 signals): believed gross edge +8.15 (0.29), realised hedge edge before fees +3.83 (1.02), shortfall -4.31 (0.89), quote->fill drift -4.80 (0.89); net after fees -2.17 (0.89); Bybit-maker n=12 believed +6.71, realised +1.08; Binance-maker n=12 believed +9.59, realised +6.59. Labelled EXPLORATORY there.
  - For the last 30 days (16 Oct - 16 Nov 2026): (a) 29 Oct 2026 capture replays, 115 fully hedged simulated fills: `OI/cost/shortfall/shortfall_split.md` (POOLED believed net 1.054, realised net -0.502, shortfall 1.557 = delay drift 2.508 + hedge-latency drift 0.346 + hedge impact -0.000 + fee diff 0.001 + quote-price term -0.258 - allowances 1.040; bps of the maker fill price; 4 runs: sizes 1, 5, 25c, 100 SOL; 7/10/38/60 fills); marked SUPERSEDED as a result by the probe follow-up (`OI/cost/shortfall/SUPERSEDED.md`, owner note: the 115-fill sample is not the "24-fill S0 sample"). (b) D7d paper-live run2 (8 Nov 2026, one live paper plan): `OI/paper_live/run2/d7d-run2/paper/metrics_report.json`: decision notional 85.735, paper P&L 0.0450, realised -0.0181, shortfall 0.0631 (1 plan). (c) Real orders: NONE. The ollog directory `order_lifecycle_logger/fillprobe_runs/fillprobe_mainnet_20261115T032830Z` (15 Nov) has `meta.json` label "PAPER-simulated-orders", `paper: True`, network mainnet (360 Bybit + 281 Binance order rows; fields: best bid/ask and size at send, top-5, pre-move 1 s/5 s, fills, outcome); no believed-edge or hedge fields, so no pre-trade estimate exists. The testnet run `fillprobe_testnet_20261115T122612Z` is testnet.
- Wired into: strategy_quote_drift and the shortfall scripts are post-hoc scripts over replay output; not wired into any run.
- Effective config: n/a (reads existing runs).
- Tests and evidence: `tests/test_shortfall_split.py` (28-test combined run passes). The four-part definition is not in the repo: `metrics.shortfall_split` splits into delay, hedge-latency, hedge-impact, fee, (+ quote-price and allowances); A5 does not know which four the owner means.
- Exact gap: no real recorded order in the last 30 days has the pre-trade fields (believed edge, hedge price at quote) needed, so the real-orders comparison is MISSING (not back-filled). The 24-signal sample is January 2022 + March 2024 replay fills (22 of the 24 from two 2022 days), which is outside the "last 30 days" and is not real orders. Missing fields for the sample: p_fail and loss_fail are not in the net (C16 text); the hedge-venue reference is the strategy's feed-delayed view.
- Implementation: extend `metrics.shortfall_split` calls into `session_replay.write_result` and the paper/live teardown, and have the live/paper strategy write the believed edge and hedge price with each quote (the signal rows already carry them in replay).
- Acceptance test: for every filled order of a day, a row with the four parts that sum to realised - believed (residual ~1e-9), or "missing: <field>".
- Dependencies and risk: 10.6, 10.7, owner definition of the four parts, real order data (first real arb orders).
- Completion evidence: table over a real 30-day window with n and units, no back-filled value.

## Q2 - immutable decision record (same as 8.4)
- Status: PARTIAL
- Exists / Wired / Gap / Implementation / Acceptance: see 8.4 (identical). Specific to Q2: the nearest recorded evidence is `OI/cost/f_window` (6 records; replay only; no versions, snapshot refs, candidates or scores; journal can drop rows past 200,000 without a counter).
- Tests and evidence: `tests/test_order_split.py:209`, `tests/test_session_replay.py:725-750`.
- Dependencies and risk: 8.4, A2.
- Completion evidence: the reproduction test of 8.4.

## W13 - three-stage question list mapped to components, gaps and acceptance tests
Status of the block: PARTIAL. Row status in the second column.

### W13-A: before sending
| Row | Question | Status | Component (file:line) | Gap | Acceptance test |
|---|---|---|---|---|---|
| W13-A1 | Expected total cost | PARTIAL | `strategies/edge.py`, `strategies/arb_strategy.py` (net edge, fees from `fees.py:88`); `assignment_cost` `venue_selector.py:51`; `impact.cost_bps` `simulation/impact.py:153`; signal rows `estimated_net_edge_bps` | impact and adverse not summed into one pre-trade number stored with the order | signal row holds fee, spread, impact, adverse, total; replay equals recomputation |
| W13-A2 | Venue(s) | PARTIAL | `layer.py:160-178` fixed rule; `assign_rest_hedge` unused beyond that | dynamic and routable orders not wired (0.2i, 8.2) | route record per decision (8.4) |
| W13-A3 | Size and speed | PARTIAL | size cap `size_aware` (v1_9), `choose_tactic` `base.py:145` (not for arb), `split_single_venue`, AC arms study only | no size/speed decision with evidence in session; AC not in core (9.1) | decision record lists size, speed mode and alternatives |
| W13-A4 | Book deep and reliable enough | PARTIAL | `book_gate.py:17`, `VenueState.gap_depth` :470 / `depth_quality` :497, `DisplayedDepthImpact` | `depth_quality` off in all profiles; reliability is APPROXIMATE (label in the evidence) | evidence on in a profile; hedge cap covered by depth in the record |
| W13-A5 | Edge after costs and risk | PARTIAL | `economics_gate.py:68` (run-level), `RiskEngine`, `ReservationBook`, `risk_gate.py` | no per-decision edge-after-risk figure stored | record has edge, risk headroom, reservation result |
| W13-A6 | Evidence and assumptions | PARTIAL | label strings (ASSUMED/PROXY) in `SplitConfig.label`, `_routing_evidence` | labels only in opt-in evidence; no versions | record carries versions and the ASSUMED list |

### W13-B: while the market moves
| Row | Question | Status | Component | Gap | Acceptance test |
|---|---|---|---|---|---|
| W13-B1 | Feed healthy/current | PARTIAL | `book_gate.py:17`, `time_integrity.py:67,134`, `timing.py:434` `feed_state`, `dead_feed_blocks` :443; silence timer late-firing is a recorded Phase E blocker (`single_platform_plan.md:260`) | status of the blocker not verified by A5 (A1 owns) | feed-delay injection replay blocks quoting and logs the state |
| W13-B2 | Book thinning/recovering | PARTIAL | `VenueState.survival` :396, `flicker` :456, `cancel_to_trade` :420, `spike` :444 | observation only; no consumer in arb decisions | thinning event changes a logged decision under a switch |
| W13-B3 | Toxicity rising | PARTIAL | `VenueState.is_toxic` :547 (trade sign threshold), adverse table `edge.py:233`; VPIN field never filled (`models.py:69`) | VPIN missing in core; no toxicity-triggered action in arb session | toxic regime test pauses or widens, in the journal |
| W13-B4 | Dislocation persisting | PARTIAL | `cross_venue.py` (A4), strategy `quote_withdraw`/`requote_target` | A4 owns | persistent-gap replay holds then exits per policy |
| W13-B5 | Order acked/partial/rejected/unknown | PARTIAL | `order_state_machine.py` (spec), `oms.py` states, `hedge_lifecycle.py:111`, `rest_reconcile.py`, `ems/reconcile.py:23`, fault presets | unknown-order resolution partially evidenced in replay faults (E2 gate not verified by A5) | fault replay: reject/unknown/partial each reaches a terminal state with journal rows |
| W13-B6 | Hedge exposure growing | PARTIAL | `circuit_breaker.py:176` (net-exposure timer, dust rule), `ReservationBook`, lifecycle `residual_exposure` | breaker and journal exist; no live display | exposure ramp replay trips at the limit |
| W13-B7 | Maintain/amend/cancel/reduce/pause per policy | PARTIAL | S0 controller `decide_reprice` `passive.py:101` + `oms.request_amend` `oms.py:2877` (v1_9s only), `quote_withdraw`, breaker flatten | reduce is only via breaker flatten/unwind; policy is not one table; `choose_tactic` not used for arb | policy table test: each market state -> one action with reason code |

### W13-C: after the fill
| Row | Question | Status | Component | Gap | Acceptance test |
|---|---|---|---|---|---|
| W13-C1 | What happened, timestamped | PARTIAL | `session_intent_journal`, `session_fills`, `session_orders`, `maker_observations` | stamps in different clocks and files; no single timeline (10.4, 10.5) | one timeline per fill from the files |
| W13-C2 | Cost vs the right benchmark | PARTIAL | `shortfall_split` `metrics.py:549`, `implementation_shortfall` :623 | not called by a run; benchmark (decision mid vs quote vs arrival) not fixed by the owner | Q1 table |
| W13-C3 | Conditions at fill | PARTIAL | `maker_fill` touch/queue, regime cells | no per-fill context row; VPIN missing (10.2) | context row per fill |
| W13-C4 | Favourable or adverse | PARTIAL | `MarkoutTracker` decompose, `lifecycle_report` tails | definition conflict with FAST_MM; small n; CI (10.3) | 10.3 fixture |
| W13-C5 | Hedge correct and prompt | PARTIAL | `pair_economics` (`hedge_send_delay_s`, `first_hedge_fill_delay_s`, `fully_hedged`), `lifecycle_report.hedge_completion` | decision-vs-send split missing (10.5) | every maker fill: hedge qty == fill qty and delays |
| W13-C6 | Ledger complete | PARTIAL | `ems/reconcile.py:23,126` (cash, position, fees from the intent journal; mode count/enforce), ledger event counts | reconciliation mode default is `count`, not enforce (`session_replay.py` kwarg `reconcile_mode: "count"`); real-account reconcile is A2/A4 | enforce-mode replay with 0 mismatches |
| W13-C7 | Reproducible and explainable | PARTIAL | determinism (A2), journal, reason codes | explainability = 8.4 gap (no candidates and costs) | rebuild a decision from the record (8.4 test) |

## Findings that need the owner
1. The only wired selector call (`layer.py:164`) passes zero costs and drops its own alternatives; the fee-aware dynamic mode is unwired. Enabling `dynamic` is a strategy-policy change (which venue rests) and is unsafe to enable before 8.2/8.4 evidence exists. T2 (100 SOL, one capture day, 3 bursts) showed depth-only picks the wrong venue; the fee-aware arm was NOT RUN.
2. Routing evidence is replay-only and opt-in: no profile enables it, paper/live never install the provider (`bt/session_replay.py:295` is the only call). "Routing evidence kept today" for a normal run is a counter (13 route decisions in `clean_day1_v19`).
3. The journal is mutable and silently capped at `JOURNAL_MAX = 200_000` (`layer.py:31`), with no dropped counter: this fails the Q2 acceptance on its own.
4. `order_lifecycle_logger/fillprobe_runs/fillprobe_mainnet_20261115T032830Z/*/meta.json` says `paper: True`, label "PAPER-simulated-orders", network mainnet. `OI/cost/LARGE_GAPS_RESULTS.md:255` says the mainnet start is not done. The data is simulated orders on mainnet streams, not real orders. Confirm.
5. "+8.15 / +3.83" is C16 (24 replay signals, 22 in January 2022, 2 in March 2024, bps before fees): not in the last 30 days and not real orders. The owner note in `OI/cost/shortfall/SUPERSEDED.md` calls it a "24-fill S0 sample" while that folder holds 115 fills (29 Oct 2026 replay).
6. Q1 "four parts": the repo's split has more than four parts (delay, hedge-latency, hedge-impact, fee, quote-price term, allowances). Name the four.
7. 10.3: the literal "+2 captured / -4 adverse" text is not in the repo; two definitions conflict (FAST_MM pre-fill drift, positive = adverse; core post-fill markout, positive = favourable; denominators differ from the 09 Nov note). Decide the single definition before the MM and arb reports share a table.
8. 10.8: five or more report builders overlap, mostly in `out/` (CLAUDE.md breach) with hard-coded SOL/Bybit/Binance/2026-10-29 and `LOT = 0.01`. `pair_economics` only reads "maker leg" rows, so MM quotes get no pair rows (arb/MM asymmetry). Per CLAUDE.md, do not add a sixth.
9. VPIN and several microstructure fields exist in `simulation/models.py` but are hard-set to 0/False (`venue_state.py:575-600`); a consumer must not read them as measurements.
10. Overlap with other agents: 10.1 with A4 0.2h (markout and pair formulas); 10.2/10.3 with A3 (regime gate, book shape); 8.4 with A2 (journal, determinism); 0.2i/8.2 `core/ems/ioc_policy.py` and `ioc_runtime.py` were not read in depth (a possible second chooser: check before adding code).
11. AC schedule exists only in a study script under `out/`; the TWAP/VWAP code in `trading_platform_poc/ems.py` is out of scope and must not be cited as the core implementation.

## Returned IDs
0.2i, 8.1, 8.2, 8.3, 8.4, 9.1, 9.2, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, Q1, Q2, W13-A (rows W13-A1..A6), W13-B (rows W13-B1..B7), W13-C (rows W13-C1..C7)

Status counts (20 IDs): PARTIAL 18 (0.2i, 8.1, 8.2, 8.3, 8.4, 9.1, 9.2, 10.1, 10.2, 10.3, 10.5, 10.6, 10.8, Q1, Q2, W13-A, W13-B, W13-C), MISSING 2 (10.4, 10.7), VERIFIED_COMPLETE 0, UNVERIFIED 0, FAILING 0. Inside W13: 20 rows, 20 PARTIAL.
