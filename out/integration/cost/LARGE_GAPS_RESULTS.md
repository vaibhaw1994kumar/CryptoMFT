# Large-size gaps + probe: results (owner order 15 Nov 2026; rules LARGE_GAPS_RULES.md, written first). EXPLORATORY / PROXY / MODELLED. Nothing committed, nothing pushed, HOLDOUT (03-13..15) not read.

## Step 8 (framing) and step 9 (fast-stream backups) and step 7 (probe shortfall): the cheap ones
- Step 8: the T1 caveat (100 ms books: the outcome 0.1 s later is often the same or the next snapshot, so the small error is partly by construction; T1 does not test others' reaction to our order or sub-100 ms book changes) is now attached at every place a T1 number is quoted: G_RESULTS.md (the T1 section, section 1a, section 3), single_platform_plan.md (the step G entry and the follow-up entry), strategy_contract.md (the follow-up entry).
- Step 9 (look only in LOCAL places; nothing restored; the VM was NOT checked: the key was refused before): NOT FOUND LOCALLY. The 2026-10-29 capture on this Mac holds, per part (1,452 parts, 4 files each), only `canonical.jsonl.gz` (book deltas), `trades.jsonl.gz`, `trace.jsonl.gz` and `live_part_manifest.json`. The part manifests LIST `bbo`, `derivatives`, `quarantine`, `raw` and `wire` files with sha256s, at `/var/lib/cryptomft/replay-capture/2026-10-29/SOLUSDT/<session>/part-NNNNNN/` (the recorder VM); none of those files is on this Mac (searched data/, _backups/, out/, ~/Downloads, ~ for bbo*, bookTicker*, orderbook.1.*). The trace shows 7,470 BBO dispositions in part 000292 alone (about 63% of its records), so the bbo streams of both venues were recorded on the VM. No Bybit 10 to 20 ms level-1 file is named anywhere locally. Restore needs the VM: for the owner to decide.
- Step 7: already built and reported (out/integration/cost/probe_shortfall/): the 7-day TRAIN probe (2,035 fills) and the SELECT days 03-08..11 (692 fills; 03-12 has no probe run), per day and pooled, burst-block and DAY-block 95% intervals, HOLDOUT untouched. The 115-fill result is marked SUPERSEDED in cost/shortfall/SUPERSEDED.md and in the plan (steps A and B entry and the step A follow-up entry). The "24-fill" figure of the order is the 115-fill result under another name (SUPERSEDED.md says so); G_RESULTS' "24 fills" is something else (the Binance-maker arm of T2). Not rerun.

## Step 1: fill-range rule in CR-LQ (core, OFF by default)
- ONE implementation, `simulation/impact.py` level 1: `fill_range_totals(displayed_in_cap, survival, own_unrefilled_qty, low 0.8, high 1.2)` = available at survival x 0.8 / x 1.0 / min(1, x 1.2): displayed x min(1, survival x m) less our own take not yet refilled (`own_unrefilled` = sum of take x exp(-age / tau); the part refilled is 1 - exp(-t / tau)), floored at 0; `DisplayedDepthImpact.fill_range` does it from the levels and a price cap. Expected fill = min(Q, the middle case).
- `choose_tactic`: `TacticChoiceConfig.fill_range` (default False; needs liquidity_aware) with `fill_range_low` / `fill_range_high` (0.8 / 1.2, ASSUMED); MarketState carries `displayed_depth_in_cap`, `survival_mean`, `own_unrefilled_qty` (None = unknown = no change). A market-style sweep (`IOC_CAPPED / LQ_DEPTH_COVERS`) only if the PESSIMISTIC case covers Q; otherwise IOC_CAPPED for the pessimistic amount and the rest as the carry (`LQ_PARTIAL_RESIDUAL`, or `LQ_DEPTH_LIMITED` when the first-gap cap reduced it); nothing available in the pessimistic case: PASSIVE with the whole order as the residual. `TacticChoice.fill_range` = (low, expected, high). With the range on, the flicker discount of gap_aware is NOT applied (the range replaces that single discount); the first-gap cap still is. MARKET_BANDED stays emergency-only: no soft rule reaches it.
- The arb hedge dispatch does NOT call choose_tactic (tactic_choice is refused with the arb pairing), so this rule does not reach arb hedges; it is for the market-making and exit paths, and the offline hedge studies call it directly.
- Tests (tests/test_impact_model.py FillRangeRule, 9): hand-calculated totals (14 displayed, survival 0.9, 0.5 own missing: 9.58 / 12.1 / 13.5), the exponential refill, sweep only when the pessimistic case covers, passive with the whole order as residual, never MARKET_BANDED, first-gap cap, no flicker discount, off = today's answer for 4 depths with the new fields given, missing inputs change nothing, bad inputs refused.
- GATES: full suite 3,741 passed, 92 failed + 1 collection error = EXACTLY the Phase 0 list (scripts/compare_baselines.py tests: UNCHANGED, no new failure, none fixed). The v1_9 1 SOL identity replay was NOT run for step 1 alone: no replay path calls choose_tactic or the fill-range code (it was run once after step 2, below).

## Step 2: own consumption with refill (extends the own_impact switch; no second mechanism)
- FINDING FIRST: the session replay's taker walk (`QueueAwareVenueAdapter` over `DeterministicVenueAdapter`) ALREADY withholds our own takes: a take is held back from later orders until the published level shrinks or a snapshot arrives, and it NEVER refills in between. So "two orders in a row cannot take the same liquidity" is already true today (a test now pins it), and the old behaviour is the "never refills" bound (REFILL tau infinity). The new mode REPLACES that mechanism, it is not added to it.
- `own_impact: refill` (profile / BacktestConfig value; needs an `impact:` block, whose new `own_refill_tau_s` is the refill constant, default 3.5 s ASSUMED, the isolated-run fit; refused at startup without it; off in every profile). In the session replay each venue adapter gets an `OwnTakeLedger` (core, `simulation/impact.py`): the taker walk reads a COPY of the core book with our unrefilled takes removed (level by level, in the unshifted frame, so it composes with the level-3 overlay's whole-tick shift), the real core book is never touched, the base adapter's withheld-liquidity mechanism is switched off for it (`consume_liquidity = False`), and the takes are recorded level by level after the fill. In the backtester path `OrderBook(own_refill_tau)` keeps the same ledger: a public update, trade repair or snapshot does NOT reset it (the overlay mode resets it at the next public update of the level).
- Tests (tests/test_impact_model.py, 12 new): ledger arithmetic (4 at t=0: 4 at 0 s, 4/e at tau, half at tau ln 2, takes add, per-side, dropped when refilled), float book refill survives a public update and a snapshot while the overlay does not, one synthetic 60-lot sell removes exactly 50 at 100.00 and 10 at 99.99 and the book is untouched, 3.5 s later 31.61 are back at 100.00 (hand-calculated vwap), two sells at the same instant cannot take the same 50 lots, the legacy adapter never refills (a minute later still withheld) while the ledger has refilled a minute later, the ledger records the unshifted frame under a -3 tick overlay shift, a replay with `refill` and no `impact:` block is refused, a replay records every aggressive fill and leaves signals and orders unchanged.
- ISSUE FOUND, NOT CHANGED: `new_backtest/session_replay.py` defines `_impact_sync` TWICE (lines about 500 and 568, identical bodies; the second wins). Left alone, because touching it could change the identity runs; for the owner to remove.
- GATES (after steps 1 and 2 together): full suite 3,749 passed, 92 failed + 1 collection error = EXACTLY the Phase 0 list (compare_baselines tests: UNCHANGED); the pinned-hash tests (v1_2 freeze, legacy_v0, power13 golden) are in that suite and pass. v1_9 1 SOL full-day identity replay (the 2026-10-29 capture, `--size 1 --gross 5.0`, the settings of the earlier identity runs): 13 of 14 output files BYTE-IDENTICAL to the step B, step C and step F identity runs; session_manifest.json differs in provenance only (code hash, paths, tracked-file counts, wall time). NOTE: a first identity run without `--gross` used the profile's own gross 2.0 and gave 2 signals / 5 orders / 6 fills against 5 / 13 / 16: a different configuration, not a regression (kept as identityL_gross2_1sol); the comparison above is the gross 5.0 run. With `own_impact: refill` off in every profile nothing in a replay changes.

## Step 3: worst-case cost of waiting (CVaR as lambda_u)
Run: `t5_split.py worst` -> t5w/worst.json, t5w/lambdas.json; analyses t5p / t5r (random moments, price-aware / pessimistic) and t5q / t5s (probe fill moments). Moments: (a) the probe's SIMULATED maker-fill times (TRAIN 2,035 fills, SELECT 692), (b) random prints, (c) prints right after a move of the venue's own mid of at least 3 bps in the previous 1 s (3 bps ASSUMED). (b) and (c) are market data alone and are what sets lambda_u; (a) is reported beside them and sets nothing. The side-signed mid move is "against a hedge that is still unhedged". Block-bootstrap intervals (10-min blocks, 2,000 resamples, seed 1) for every cell are in worst.json (CVaR5 intervals are wide: e.g. Binance capture random 1 s CVaR5 2.31 [2.00, 2.60]).

Adverse move after the moment, bps: mean / p95 / p99 / **CVaR(5%)**.
mid move after a moment, bps, side-signed (mean / p95 / p99 / CVaR5%), by horizon

| group | moments | venue | n | 0.1 s | 1 s | 5 s |
|---|---|---|---|---|---|---|
| TRAIN | random prints (market data) | binance | 98,686 | -0.00 / 1.1 / 2.3 / **2.0** | 0.00 / 4.0 / 7.3 / **6.2** | 0.00 / 9.7 / 16.6 / **14.0** |
| TRAIN | random prints (market data) | bybit | 14,098 | 0.00 / 1.3 / 2.8 / **2.4** | 0.00 / 4.8 / 9.1 / **7.6** | 0.00 / 11.7 / 20.5 / **17.1** |
| TRAIN | after a >=3 bps / 1 s move (ASSUMED threshold) | binance | 8,688 | 0.08 / 1.6 / 2.9 / **2.5** | 0.27 / 5.3 / 9.0 / **7.6** | 0.27 / 12.4 / 19.9 / **17.2** |
| TRAIN | after a >=3 bps / 1 s move (ASSUMED threshold) | bybit | 1,518 | 0.43 / 2.9 / 6.2 / **5.2** | 0.79 / 7.1 / 12.4 / **10.5** | 1.36 / 16.3 / 25.5 / **22.1** |
| TRAIN | SIMULATED probe maker fills | binance | 2,007 | 0.71 / 3.0 / 6.5 / **5.9** | 1.00 / 6.4 / 10.7 / **9.2** | 1.37 / 13.8 / 23.0 / **18.8** |
| TRAIN | SIMULATED probe maker fills | bybit | 314 | 1.01 / 3.6 / 5.6 / **4.9** | 2.56 / 10.4 / 15.3 / **13.5** | 4.67 / 20.8 / 31.8 / **27.4** |
| SELECT | random prints (market data) | binance | 70,490 | 0.00 / 0.7 / 1.8 / **1.5** | -0.00 / 2.8 / 5.3 / **4.6** | -0.00 / 6.6 / 12.1 / **10.6** |
| SELECT | random prints (market data) | bybit | 28,196 | 0.00 / 1.0 / 2.2 / **1.9** | -0.00 / 3.6 / 6.9 / **6.1** | -0.00 / 8.4 / 15.5 / **14.1** |
| SELECT | after a >=3 bps / 1 s move (ASSUMED threshold) | binance | 3,004 | 0.06 / 1.4 / 2.9 / **2.8** | 0.29 / 4.5 / 10.0 / **8.4** | 0.55 / 11.4 / 28.6 / **21.6** |
| SELECT | after a >=3 bps / 1 s move (ASSUMED threshold) | bybit | 1,791 | 0.40 / 2.5 / 5.2 / **4.4** | 0.97 / 6.8 / 12.1 / **12.1** | 1.29 / 14.4 / 40.9 / **29.2** |
| SELECT | SIMULATED probe maker fills | binance | 685 | 0.93 / 4.2 / 9.5 / **8.1** | 1.23 / 5.3 / 11.5 / **11.5** | 0.95 / 11.0 / 15.3 / **16.3** |
| SELECT | SIMULATED probe maker fills | bybit | 444 | 0.80 / 3.4 / 5.8 / **5.2** | 3.09 / 10.8 / 17.1 / **20.1** | 2.83 / 13.9 / 20.7 / **20.9** |
| CAPFIRST | random prints (market data) | binance | 50,468 | -0.00 / 0.0 / 1.0 / **0.4** | 0.00 / 1.0 / 2.9 / **2.3** | -0.00 / 3.9 / 6.0 / **5.2** |
| CAPFIRST | random prints (market data) | bybit | 50,468 | 0.00 / 0.0 / 1.0 / **0.5** | -0.00 / 1.9 / 3.0 / **2.5** | 0.00 / 3.9 / 6.0 / **5.3** |
| CAPFIRST | after a >=3 bps / 1 s move (ASSUMED threshold) | binance | 243 | 0.04 / 1.0 / 1.0 / **1.2** | 0.31 / 3.0 / 4.5 / **4.3** | 0.59 / 6.0 / 11.8 / **9.3** |
| CAPFIRST | after a >=3 bps / 1 s move (ASSUMED threshold) | bybit | 243 | 0.01 / 1.0 / 2.0 / **1.4** | 0.36 / 3.0 / 6.6 / **4.9** | 0.84 / 6.9 / 12.1 / **10.3** |
| CAPSECOND | random prints (market data) | binance | 50,408 | 0.00 / 0.0 / 2.1 / **1.4** | 0.00 / 3.1 / 6.1 / **4.8** | 0.00 / 7.0 / 13.4 / **11.0** |
| CAPSECOND | random prints (market data) | bybit | 50,408 | 0.00 / 0.0 / 2.1 / **1.5** | 0.00 / 3.1 / 6.1 / **4.9** | 0.00 / 7.1 / 13.5 / **11.4** |
| CAPSECOND | after a >=3 bps / 1 s move (ASSUMED threshold) | binance | 2,739 | 0.05 / 2.0 / 3.1 / **2.8** | 0.12 / 5.2 / 10.3 / **7.9** | 0.36 / 12.1 / 21.9 / **17.8** |
| CAPSECOND | after a >=3 bps / 1 s move (ASSUMED threshold) | bybit | 2,739 | 0.02 / 1.0 / 3.1 / **2.6** | 0.07 / 5.2 / 10.3 / **8.1** | 0.44 / 12.5 / 24.9 / **19.8** |


- Reading: at random moments the mean is 0 by construction (side-signed, both directions equally likely); the tail is the content. CVaR(5%) over 1 s is about 2.4 to 2.6 times sigma_1s x sqrt(1) (Binance capture first half 2.3 against sigma 0.90; March TRAIN 6.2 against 2.6). Right after a sharp move the mean is 0.1 to 1.0 bps against the hedge (continuation) and the tail is fatter (Bybit SELECT 5 s CVaR5 29 bps against 14 at random moments). At the SIMULATED fill moments the mean is +0.7 to +3.1 bps (adverse selection after a maker fill, as in the earlier shortfall work) and CVaR5 at 1 s is 9 to 20 bps. The fill-moment rows come from the simulated queue (unvalidated): they are an upper-bound illustration, not a measurement.
- lambda_u in Cont and Kukanov (lambdas.json; March = TRAIN/SELECT, capture first half = CAPFIRST), T = 1 s: sigma x sqrt(T) 2.62 (TRAIN) / 0.90 (CAPFIRST); CVaR from random moments 6.16 / 2.31; CVaR from random + sharp moments 6.32 / 2.33. T = 5 s: 5.85 / 2.01 against 14.0 / 5.25 and 14.4 / 5.27.
- Effect on E's split (SELECT day, price-aware rest-fill rule, random moments; same pattern with the PESSIMISTIC fill floor and at the simulated fill moments): with sigma x sqrt(T) E rests ALL of the order (rested share 1.00), saves 0.24 to 0.39 bps of cost against the all-market hedge (cost minus all-market, negative = cheaper; intervals about [-0.28, -0.20] to [-0.43, -0.34]) and loses on the risk-adjusted net: at weight 1 -1.4 (T = 1 s) to -2.6 bps (T = 5 s) with the sigma charge, -3.6 to -6.8 with the CVaR charge. With CVaR as lambda_u E rests only 0 to 1% of the order, so it IS the all-market hedge: cost difference -0.01 bps (interval [-0.03, 0.00]), net at weight 1 within 0.01 bps. At the simulated fill moments (t5q, n = 685 SELECT / 2,010 TRAIN) the CVaR cells rest 2 to 5% and every cost difference has an interval that includes 0 (NO EFFECT); the sigma cells cost +0.05 to +0.30 bps more than all-market (the TRAIN T1 and T2 intervals exclude 0) and lose 1.8 to 3.1 bps at weight 1.
- Choice on SELECT (highest net at weight 1, 10 SOL): CVaR from random moments and CVaR from random + sharp are tied within 0.002 bps (both far above sigma x sqrt(T)); the sharp-moment version is kept as the more conservative. HONEST SUMMARY: using CVaR as lambda_u does not make the split better, it makes it stop splitting. The tail of waiting is large enough that, at this fee and rebate, nothing is worth resting. This is a measure of the tail, not evidence that a split cannot pay at some other cost level.

## Step 4: large-size tests (29 Oct capture both venues + Bybit March, overlay off and on)
Method and arms: LARGE_GAPS_RULES.md section 4. All numbers EXPLORATORY / PROXY; every overlay row is MODELLED (reaction model, not an observation).

### 4a. T1 at 25 / 50 / 100 / 200 / 400% of depth within 10 bps (D10)
Rerun not needed for overlay OFF; the overlay ON column equals OFF for T1 because T1's hypothetical orders are single and the overlay only acts on LATER orders of ours (stated in the rules; the sequences in 4b are where it acts). From t1b/extra_*.json (by_fraction). Realised cost in bps; MAE of the prediction in bps (P0 = displayed depth, P1 = displayed depth scaled by the measured survival).

| group | f = 0.25 | 0.5 | 1 | 2 | 4 |
|---|---|---|---|---|---|
| Bybit March TRAIN: cost / MAE P0 / MAE P1 | 2.92 / 0.43 / 0.43 | 4.23 / 0.41 / 0.41 | 6.24 / 0.41 / 0.41 | 9.55 / 0.38 / 0.39 | 14.38 / 0.39 / 0.40 (4.7% unfillable) |
| Bybit March SELECT | 2.91 / 0.29 / 0.29 | 4.24 / 0.28 / 0.28 | 6.21 / 0.28 / 0.28 | 9.63 / 0.27 / 0.27 | 14.29 / 0.33 / 0.34 (24% unfillable) |
| Binance capture | 1.62 / 0.15 / 0.20 | 2.93 / 0.15 / 0.24 | 5.33 / 0.15 / 0.35 | 10.34 / 0.15 / 0.54 | 18.87 / 0.15 / 0.89 (0.05% unfillable) |
| Bybit capture | 2.01 / 0.16 / 0.17 | 3.38 / 0.15 / 0.18 | 5.74 / 0.14 / 0.21 | 10.04 / 0.14 / 0.26 | 16.08 / 0.19 / 0.48 (17.7% unfillable) |

- Displayed depth (P0) keeps its error flat from 25% to 400% of D10 (0.15 on both capture venues, 0.27 to 0.43 on March, up to 0.33 on SELECT and 0.19 on Bybit capture only at 400%, where a quarter or a sixth of the orders cannot be filled inside 10 bps). The measured-survival version (P1) gets worse with size on the capture and over-predicts the cost (signed error +0.86 bps at 400% on Binance), as in the follow-up.
- T1 CAVEAT (owner 15 Nov): with ~100 ms books the outcome 0.1 s later is often the same or the next snapshot, so the small error is partly by construction; T1 does not test others' reaction to our order or sub-100 ms changes.

### 4b. Repeated hedges: K = 10 hedges of f x D10 every s seconds, own consumption off vs on, overlay off vs on
Cost = side-signed VWAP of the walk 0.1 s after each decision against the untouched touch at that decision, mean over the 10 hedges, bps per unit. "off" = no own consumption (every hedge sees the published book); "refill" = the core ledger with tau 3.5 s (ASSUMED, isolated-run fit); "never refills" = the old base-adapter behaviour and the upper bound; overlay default / upper = core ImpactOverlay with the isolated-run / all-runs settings (cross shift on, MODELLED, whole ticks). Intervals are 95% block bootstrap on sampling only; Y and delta of the overlay have NO intervals, so the overlay columns do not carry parameter uncertainty.

**Binance perp, 29 Oct capture** (hedge cost, bps per unit, mean of the K=10 hedges, 95% block-bootstrap interval in brackets for 'off' and 'refill')

| f x D10 | spacing | off (no own consumption) | refill (tau 3.5 s) | never refills | refill + overlay default | refill + overlay upper |
|---|---|---|---|---|---|---|
| 0.1 | 1 s | 0.68 [0.65, 0.72] | 1.74 [1.68, 1.79] | 3.47 | 5.75 | 7.72 |
| 0.1 | 2 s | 0.69 [0.66, 0.72] | 1.17 [1.13, 1.21] | 2.83 | 5.18 | 7.14 |
| 0.1 | 5 s | 0.68 [0.65, 0.71] | 0.78 [0.75, 0.81] | 2.16 | 4.79 | 6.76 |
| 0.1 | 10 s | 0.68 [0.65, 0.71] | 0.69 [0.67, 0.72] | 1.69 | 4.70 | 6.67 |
| 0.5 | 1 s | 2.97 [2.91, 3.02] | 8.16 [8.01, 8.31] | 18.81 | 14.66 | 22.41 |
| 0.5 | 2 s | 2.98 [2.93, 3.03] | 5.31 [5.22, 5.40] | 16.94 | 11.81 | 19.56 |
| 0.5 | 5 s | 2.99 [2.93, 3.05] | 3.53 [3.47, 3.59] | 14.43 | 10.03 | 17.78 |
| 0.5 | 10 s | 3.00 [2.93, 3.07] | 3.09 [3.03, 3.17] | 12.24 | 9.59 | 17.34 |
| 1 | 1 s | 5.40 [5.34, 5.45] | 15.50 [15.18, 15.86] | 40.38 | 23.50 | 36.22 |
| 1 | 2 s | 5.42 [5.37, 5.48] | 10.39 [10.23, 10.56] | 36.25 | 18.39 | 31.11 |
| 1 | 5 s | 5.45 [5.39, 5.53] | 6.70 [6.60, 6.80] | 30.77 | 14.69 | 27.42 |
| 1 | 10 s | 5.49 [5.40, 5.60] | 5.71 [5.62, 5.82] | 26.00 | 13.71 | 26.43 |

**Bybit perp, 29 Oct capture** (hedge cost, bps per unit, mean of the K=10 hedges, 95% block-bootstrap interval in brackets for 'off' and 'refill')

| f x D10 | spacing | off (no own consumption) | refill (tau 3.5 s) | never refills | refill + overlay default | refill + overlay upper |
|---|---|---|---|---|---|---|
| 0.1 | 1 s | 1.04 [1.00, 1.09] | 2.10 [2.03, 2.17] | 3.96 | 6.03 | 7.90 |
| 0.1 | 2 s | 1.04 [1.00, 1.08] | 1.51 [1.46, 1.57] | 3.27 | 5.44 | 7.31 |
| 0.1 | 5 s | 1.03 [0.99, 1.08] | 1.14 [1.10, 1.19] | 2.57 | 5.07 | 6.94 |
| 0.1 | 10 s | 1.04 [1.00, 1.08] | 1.06 [1.02, 1.10] | 2.09 | 4.99 | 6.86 |
| 0.5 | 1 s | 3.41 [3.33, 3.48] | 8.12 [7.99, 8.24] | 17.48 | 14.49 | 21.95 |
| 0.5 | 2 s | 3.40 [3.33, 3.48] | 5.63 [5.56, 5.70] | 16.09 | 12.00 | 19.46 |
| 0.5 | 5 s | 3.39 [3.32, 3.47] | 3.95 [3.88, 4.02] | 14.05 | 10.32 | 17.79 |
| 0.5 | 10 s | 3.41 [3.34, 3.50] | 3.51 [3.44, 3.59] | 12.17 | 9.88 | 17.35 |
| 1 | 1 s | 5.77 [5.71, 5.84] | 14.85 [14.50, 15.17] | 21.10 | 22.58 | 34.60 |
| 1 | 2 s | 5.77 [5.71, 5.83] | 10.05 [9.90, 10.19] | 20.48 | 17.88 | 30.12 |
| 1 | 5 s | 5.77 [5.71, 5.83] | 6.84 [6.78, 6.90] | 19.56 | 14.68 | 26.93 |
| 1 | 10 s | 5.79 [5.72, 5.85] | 5.98 [5.92, 6.05] | 18.43 | 13.82 | 26.07 |

**Bybit perp, March 2024-03-08 00:00-02:00 UTC window (SELECT day)** (hedge cost, bps per unit, mean of the K=10 hedges, 95% block-bootstrap interval in brackets for 'off' and 'refill')

| f x D10 | spacing | off (no own consumption) | refill (tau 3.5 s) | never refills | refill + overlay default | refill + overlay upper |
|---|---|---|---|---|---|---|
| 0.1 | 1 s | 1.69 [1.58, 1.80] | 2.29 [2.19, 2.40] | 2.77 | 4.75 | 4.76 |
| 0.1 | 2 s | 1.73 [1.65, 1.81] | 1.99 [1.90, 2.08] | 2.43 | 4.45 | 4.46 |
| 0.1 | 5 s | 1.73 [1.65, 1.80] | 1.79 [1.71, 1.86] | 2.13 | 4.25 | 4.26 |
| 0.1 | 10 s | 1.75 [1.67, 1.82] | 1.76 [1.68, 1.83] | 2.01 | 4.22 | 4.23 |
| 0.5 | 1 s | 4.36 [4.26, 4.48] | 6.27 [6.13, 6.42] | 8.42 | 10.26 | 12.16 |
| 0.5 | 2 s | 4.39 [4.31, 4.49] | 5.31 [5.21, 5.43] | 7.46 | 9.30 | 11.20 |
| 0.5 | 5 s | 4.35 [4.27, 4.43] | 4.60 [4.53, 4.68] | 6.48 | 8.59 | 10.49 |
| 0.5 | 10 s | 4.36 [4.29, 4.43] | 4.41 [4.34, 4.48] | 5.83 | 8.39 | 10.29 |
| 1 | 1 s | 6.36 [6.27, 6.46] | 10.63 [10.39, 10.84] | 13.65 | 15.53 | 19.17 |
| 1 | 2 s | 6.39 [6.30, 6.49] | 8.46 [8.33, 8.60] | 13.00 | 13.36 | 17.02 |
| 1 | 5 s | 6.37 [6.29, 6.46] | 6.93 [6.86, 7.02] | 11.62 | 11.84 | 15.50 |
| 1 | 10 s | 6.37 [6.30, 6.45] | 6.48 [6.41, 6.56] | 10.35 | 11.39 | 15.03 |

**Bybit perp, March 2024-03-01 00:00-02:00 UTC window (TRAIN day)** (hedge cost, bps per unit, mean of the K=10 hedges, 95% block-bootstrap interval in brackets for 'off' and 'refill')

| f x D10 | spacing | off (no own consumption) | refill (tau 3.5 s) | never refills | refill + overlay default | refill + overlay upper |
|---|---|---|---|---|---|---|
| 0.1 | 1 s | 1.69 [1.60, 1.77] | 2.05 [1.94, 2.17] | 2.28 | 4.25 | 4.08 |
| 0.1 | 2 s | 1.67 [1.60, 1.74] | 1.83 [1.74, 1.93] | 2.05 | 4.03 | 3.85 |
| 0.1 | 5 s | 1.74 [1.65, 1.84] | 1.77 [1.68, 1.87] | 1.92 | 3.97 | 3.80 |
| 0.1 | 10 s | 1.74 [1.66, 1.82] | 1.75 [1.67, 1.83] | 1.85 | 3.94 | 3.77 |
| 0.5 | 1 s | 4.20 [4.09, 4.30] | 5.66 [5.44, 5.88] | 7.13 | 9.22 | 10.50 |
| 0.5 | 2 s | 4.23 [4.13, 4.35] | 4.88 [4.73, 5.06] | 6.25 | 8.44 | 9.71 |
| 0.5 | 5 s | 4.23 [4.10, 4.36] | 4.38 [4.23, 4.53] | 5.36 | 7.94 | 9.22 |
| 0.5 | 10 s | 4.24 [4.11, 4.36] | 4.26 [4.13, 4.38] | 4.95 | 7.82 | 9.09 |
| 1 | 1 s | 6.17 [6.04, 6.28] | 9.32 [8.84, 9.78] | 12.51 | 13.68 | 16.28 |
| 1 | 2 s | 6.20 [6.09, 6.32] | 7.67 [7.43, 7.90] | 11.15 | 12.03 | 14.64 |
| 1 | 5 s | 6.19 [6.02, 6.38] | 6.55 [6.32, 6.78] | 9.39 | 10.93 | 13.57 |
| 1 | 10 s | 6.19 [5.99, 6.36] | 6.25 [6.05, 6.43] | 8.25 | 10.62 | 13.26 |

**Bybit perp ADAUSDT 2026-04-01 hour 0** (hedge cost, bps per unit, mean of the K=10 hedges, 95% block-bootstrap interval in brackets for 'off' and 'refill')

| f x D10 | spacing | off (no own consumption) | refill (tau 3.5 s) | never refills | refill + overlay default | refill + overlay upper |
|---|---|---|---|---|---|---|
| 0.1 | 1 s | 0.53 [0.40, 0.67] | 2.41 [2.26, 2.56] | 4.33 | 14.91 | 48.36 |
| 0.1 | 2 s | 0.53 [0.38, 0.68] | 1.67 [1.48, 1.88] | 4.07 | 14.17 | 47.62 |
| 0.1 | 5 s | 0.58 [0.44, 0.73] | 0.87 [0.69, 1.05] | 3.40 | 13.36 | 46.81 |
| 0.1 | 10 s | 0.58 [0.46, 0.70] | 0.63 [0.50, 0.76] | 2.90 | 13.10 | 46.46 |
| 0.5 | 1 s | 3.16 [3.06, 3.26] | 8.36 [8.13, 8.59] | 22.62 | 28.62 | 117.94 |
| 0.5 | 2 s | 3.17 [3.07, 3.28] | 5.49 [5.32, 5.66] | 21.72 | 25.74 | 115.06 |
| 0.5 | 5 s | 3.21 [3.09, 3.33] | 3.74 [3.59, 3.89] | 19.34 | 23.99 | 113.31 |
| 0.5 | 10 s | 3.20 [3.10, 3.30] | 3.30 [3.18, 3.41] | 17.07 | 23.50 | 112.59 |
| 1 | 1 s | 5.36 [5.23, 5.48] | 16.52 [16.11, 16.91] | 43.79 | 41.46 | 175.84 |
| 1 | 2 s | 5.37 [5.23, 5.52] | 10.62 [10.37, 10.90] | 42.34 | 35.56 | 169.95 |
| 1 | 5 s | 5.43 [5.26, 5.60] | 6.77 [6.59, 6.96] | 39.19 | 31.70 | 166.09 |
| 1 | 10 s | 5.45 [5.30, 5.62] | 5.70 [5.55, 5.88] | 35.20 | 30.58 | 164.62 |

**Bybit perp MNTUSDT 2026-04-01 hour 0** (hedge cost, bps per unit, mean of the K=10 hedges, 95% block-bootstrap interval in brackets for 'off' and 'refill')

| f x D10 | spacing | off (no own consumption) | refill (tau 3.5 s) | never refills | refill + overlay default | refill + overlay upper |
|---|---|---|---|---|---|---|
| 0.1 | 1 s | 1.10 [0.94, 1.28] | 2.59 [2.41, 2.78] | 4.96 | 6.06 | 7.18 |
| 0.1 | 2 s | 1.12 [0.96, 1.29] | 1.90 [1.75, 2.07] | 4.73 | 5.38 | 6.50 |
| 0.1 | 5 s | 1.18 [1.03, 1.33] | 1.39 [1.24, 1.55] | 4.12 | 4.87 | 5.98 |
| 0.1 | 10 s | 1.15 [1.02, 1.28] | 1.19 [1.06, 1.32] | 3.53 | 4.66 | 5.78 |
| 0.5 | 1 s | 3.52 [3.37, 3.69] | 7.82 [7.71, 7.91] | 14.94 | 13.45 | 18.77 |
| 0.5 | 2 s | 3.56 [3.42, 3.72] | 5.69 [5.58, 5.80] | 14.60 | 11.32 | 16.65 |
| 0.5 | 5 s | 3.59 [3.49, 3.69] | 4.16 [4.07, 4.25] | 13.74 | 9.79 | 15.12 |
| 0.5 | 10 s | 3.57 [3.49, 3.64] | 3.68 [3.60, 3.75] | 12.73 | 9.30 | 14.62 |
| 1 | 1 s | 5.53 [5.41, 5.65] | 12.27 [12.09, 12.44] | 24.23 | 19.20 | 28.20 |
| 1 | 2 s | 5.57 [5.47, 5.69] | 9.30 [9.18, 9.41] | 23.60 | 16.23 | 25.23 |
| 1 | 5 s | 5.61 [5.52, 5.68] | 6.64 [6.57, 6.71] | 22.23 | 13.58 | 22.57 |
| 1 | 10 s | 5.61 [5.54, 5.66] | 5.81 [5.75, 5.86] | 20.57 | 12.73 | 21.72 |

- Own consumption alone (refill against off): large at 1 s spacing and gone by 10 s. Binance capture at a full-depth hedge: 5.40 off, 15.50 refill at 1 s; 5.49 and 5.71 at 10 s (+0.2). For f = 0.1 the cost is +1.1 bps at 1 s and +0.0 at 10 s. March SELECT is the same shape (6.36 / 10.63 at 1 s; 6.37 / 6.48 at 10 s). Spacing of about 3 tau (10 s) removes the effect; spacing of 1 s doubles or triples the cost per unit.
- "Never refills" (the old withheld-liquidity bound) is far too pessimistic: Binance capture f = 1, 10 s spacing: 26.0 against 5.7 with the fitted refill; even at 0.1 x D10 per hedge it is 1.7 against 0.7.
- Overlay (MODELLED): adds a cost that does not fade with spacing, because its permanent part accumulates over the sequence: Binance capture f = 1 at 10 s: 5.71 (refill) -> 13.71 (default) -> 26.43 (upper bound). Even tiny hedges show it (f = 0.1, 10 s: 0.69 -> 4.70 -> 6.67). Read these as a scenario, not a forecast: the overlay was fitted on isolated runs of a different size range and is extrapolated here. ADA overlay rows (14.9 / 48.4 at f = 0.1, 1 s) are probably dominated by the 4.1 bps tick (whole-tick shifts); I did not verify that, treat them as not meaningful.
- Thin books and thinner coins (ADA, MNT hour 0 of 2026-04-01, Bybit only) behave like SOL on own consumption (refill against off at 1 s: ADA 0.53 -> 2.41 at f = 0.1; MNT 1.10 -> 2.59) and are worse at f = 1 (ADA 5.36 -> 16.52).
- Cost per unit by hedge size AND spacing: the table above (f in 0.1 / 0.5 / 1 x spacing 1 / 2 / 5 / 10 s), every cell with its group.

### 4c. Hedge modes at 100 / 500 / 1000 SOL on the capture's real Binance depth
Source: t5l2/report_large2.txt (`t5_split.py large2`, `t5_analyze.py large2 ALL --dir t5l2`; the chosen E cell comes from the FIRST half, the table is the SECOND half; decisions every 5 s, n = 10,012 decisions per size; the 611 "after an adverse move" decisions are a subset). Arms: M = all-at-once sweep (single order: neither own consumption nor the overlay can act on it); E = Cont and Kukanov split, T = 5 s, lambda_u = CVaR from random + sharp moments (chosen on the first half), price-aware ("pa") or PESSIMISTIC ("pess") rest-fill rule; P = time-sliced plain slices (N slices over H s, Almgren-Chriss sinh trajectory; k = kappa H, k0 = linear); L = the same slices sized by rule 1 (fill range). Cost columns are minus the all-at-once cost, so negative = cheaper than M. Unhedged time is volume-weighted seconds; drift = side-signed mid move at the execution times, p99 / CVaR5 in bps.

| size | M cost (bps) / unhedged / drift p99, CVaR5 | E (pa, overlay off): cost-M / unhedged / fill rate of rested part | E net-M at weight 1: sigma, CVaR | P or L, 10 slices over 30 s, overlay off: cost-M / unhedged / drift p99 | same, overlay default |
|---|---|---|---|---|---|
| 100 SOL | 2.218 [2.213, 2.224] / 0.045 s / 1.04, 0.85 | -0.160 [-0.212, -0.109] / 0.50 s / 0.73 | -0.040, -0.399 | -0.011 [-0.014, -0.008] / 13.6 s / 17.3 | +0.685 [+0.657, +0.713] |
| 500 SOL | 2.299 [2.279, 2.321] / same / same | -0.130 [-0.173, -0.088] / 0.39 s / 0.73 | -0.015, -0.322 | -0.086 [-0.105, -0.070] / 13.6 s / 17.3 | +1.129 [+1.113, +1.143] |
| 1000 SOL | 2.464 [2.420, 2.513] / same / same | -0.119 [-0.162, -0.080] / 0.31 s / 0.73 | +0.013, -0.249 | -0.243 [-0.287, -0.206] / 13.6 s / 17.3 | +1.276 [+1.238, +1.310] |

- All-at-once is the benchmark to beat and nothing beats it in net terms. E saves 0.12 to 0.16 bps of cost (interval excludes 0) at 0.3 to 0.5 s of unhedged time, but the saving is eaten by the risk charge: net at weight 1 is -0.04 / -0.015 / +0.013 bps with sigma (zero within the interval) and -0.40 / -0.32 / -0.25 with CVaR. The PESSIMISTIC fill floor (only prints strictly through the level fill) changes E by 0.005 to 0.016 bps: no difference.
- Time-slicing buys no cost at all without a reaction (-0.01 / -0.09 / -0.24 bps, the capture's Binance book is deep enough that a 1000 SOL hedge walks only a few levels) and carries 2 to 14 s of unhedged exposure (drift p99 6 to 17 bps against 1.0 for M): net at weight 1 is -0.8 to -2.4 bps (sigma charge) at 100 SOL, and -3.0 to -8.1 with the CVaR charge. With the overlay reaction ON the slices cost MORE than all-at-once (+0.4 to +1.3 bps default), because each slice sees the impact of the previous ones; the all-at-once order has no later order to be hurt by. That comparison favours M by construction of the reaction model: a single large order's own permanent impact is outside the model.
- Own consumption with refill and no overlay does not change the slices (refill_off equals the "off off" row to 0.001 bps): at these sizes the slices stay within the top levels, where the fitted refill makes up the taken size between slices.
- Rule 1 (fill range) sized slices (L) are identical to the plain slices (P) to 0.001 bps in every row: the rule never reduced a slice, because the displayed depth within 10 bps covers a slice at 100 to 1000 SOL on this book. I did not check this slice by slice. The rule would only bind on a thinner book or in a much larger size; it has not been exercised here.
- After an adverse move (611 decisions, n small): E saves more (-0.16 / -0.20 / -0.28 bps, the 1000 SOL interval [-0.48, -0.10] excludes 0) at 0.57 to 0.78 s unhedged, net at weight 1 +0.064 at 1000 SOL (sigma) and -0.39 (CVaR); drift p99 16.5 bps. Slices after an adverse move are worse than slices in general.
- Overlay on E: default and upper settings move E's cost by 0.000 to 0.008 bps (E is two orders, so the overlay has almost nothing to act on).

### 4d. Overlay = reaction model, MODELLED
Settings: default = impact_overlay_settings.json (isolated runs: Y 0.029, delta 0.30, phi 1.0, tau 3.5 s, cross 0.78 / 0.22), upper bound = impact_overlay_settings_upper.json (all runs: Y 0.42, delta 0.54, tau 18 s, cross 0.25 / 0.16), cross shift on for both venues. Parameter intervals that exist: permanent fraction and cross coefficients (e.g. cross Bybit to Binance isolated 0.78 [0.64, 0.93], Binance to Bybit 0.22 [0.15, 0.32]; all-runs 0.25 [0.21, 0.57] and 0.16 [0.10, 0.38]; phi isolated Bybit 0.66 [-1.9, 4.3], Binance 1.12 [-0.33, 2.4]); NO intervals exist for Y and delta, so every overlay result above is a scenario with unquantified parameter error. Comparison with probe fills: NOT POSSIBLE. The probe fills are simulated and passive; there is no real aggressive fill with a book to fit against. Said, not done; it needs the mainnet probe (and its hedges) or the live bot's own fills.

## Step 5: resting-hedge realism (per size 1 / 10 / 100 SOL on March, 100 / 500 / 1000 SOL on the capture)
Sources: t5p (random moments, price-aware), t5r (random, PESSIMISTIC floor), t5q (probe SIMULATED maker-fill moments, price-aware), t5s (fill moments, PESSIMISTIC) for March TRAIN 03-01..07 and SELECT 03-08..11 (HOLDOUT not read); t5l2 for 500 and 1000 SOL on the capture (Binance depth). 500 and 1000 SOL are tested on the capture only, because Binance depth exists there only (the rule: no synthetic depth for any profit figure).
- Fill rate of the rested part at the simulated fill moments, SELECT / TRAIN, price-aware (t5q): T = 1 s 0.37 / 0.46, T = 2 s 0.50 / 0.58, T = 5 s 0.68 / 0.72; PESSIMISTIC floor (t5s): 0.36 / 0.43, 0.49 / 0.56, 0.68 / 0.71 (size 1 SOL; sizes 10 and 100 within 0.01). On the capture the rested part of E fills 0.73 (price-aware) and 0.72 (pessimistic) at 100 to 1000 SOL.
- Cost against all-market, bps (negative = cheaper), SELECT, sigma cells (everything rested), by size 1 / 10 / 100 SOL: at random moments price-aware T5 -0.394 / -0.379 / -0.338; PESSIMISTIC T5 -0.318 / -0.318 / -0.318 (the pessimistic floor takes 0.02 to 0.08 bps off the gain); T1 -0.285 / -0.273 / -0.248 against -0.240 / -0.240 / -0.240. CORRECTION to my earlier statement (the "under 0.03 bps" in the first summary of this order): the pessimistic floor changes the sigma cells by 0.00 to 0.08 bps at random moments (T5, 1 SOL: -0.394 to -0.318); at the simulated fill moments by 0.00 to 0.05 (T1, 1 SOL: +0.170 to +0.214). It is small next to the intervals (about +/- 0.04 at random moments, +/- 0.3 at fill moments) but it is not zero.
- At the simulated fill moments resting is no cheaper than all-market (cost minus all-market, size 1 SOL, price-aware t5q): SELECT T1 / T2 / T5 +0.17 / +0.21 / +0.04 bps (intervals include 0), TRAIN +0.155 [-0.04, +0.34] / +0.197 [-0.06, +0.46] / +0.006 [-0.31, +0.35] (all include 0 = NO EFFECT, but never negative); with the PESSIMISTIC floor (t5s) TRAIN becomes +0.254 [+0.07, +0.44] / +0.296 [+0.04, +0.55] / +0.119 [-0.19, +0.49], so T1 and T2 are then worse than all-market with intervals that exclude 0. After a maker fill the market has already moved against the hedge and resting does not recover it.
- Net at risk weights 0 / 0.5 / 1 (sigma charge), SELECT, 10 SOL, price-aware, random moments, T5 sigma cell: +0.379 / -1.109 / -2.597 bps against all-market; T1: +0.273 / -0.546 / -1.365. At the fill moments (S = 10, T5, SELECT, t5q): -0.041 / -1.546 / -3.052. With the CVaR charge (net(cvar)): T5 -3.2 at weight 0.5 and -6.8 at weight 1 (random moments); -3.7 and -7.3 at fill moments. Resting everything pays only at weight 0 and only at random moments.
- Capture, 100 / 500 / 1000 SOL (E, price-aware, overlay off, second half, CVaR from random + sharp moments): cost-M -0.160 / -0.130 / -0.119 bps, fill rate 0.73, net at weight 1 -0.04 / -0.015 / +0.013 (sigma) and -0.40 / -0.32 / -0.25 (CVaR); PESSIMISTIC floor: -0.144 / -0.121 / -0.114, fill 0.72.
- Plain summary of 5: for every size from 1 to 1000 SOL the hedge that rests part of the order is within 0.0 to 0.4 bps of the all-market hedge on cost, and does not beat it on the risk-adjusted net at any weight above 0 or with the CVaR charge. The fill rule (price-aware or pessimistic) does not change that conclusion; the queue model behind the fills is unvalidated on perps, and the mainnet probe is what would test it.

## Step 6: thinner books (ADA, MNT Bybit perp April 200 levels; Binance ADA/MNT depth confirmed absent: Binance USD-M has no MNTUSDT and no ADA depth exists locally)
Survival, cancel-to-trade and the cliff flag at 2 / 3 / 5 bps (3 ASSUMED) were reported in the follow-up (G_RESULTS and extra_*.json); the ADA cliff-flag defect is repeated, NOT fixed here: the ADA tick is 4.1 bps, above the 3 bps stretch limit, so the flag is meaningless on ADA. New splits, from t1b/extra_*.json and t1c/extra_*.json (`splits_flow_and_post_fill`); MAE of the T1 prediction (P0, displayed depth), bps; difference with the block-bootstrap 95% interval:

| group | volatile blocks | MAE volatile / normal, diff [95%] | one-sided flow, MAE / rest, diff [95%] |
|---|---|---|---|
| SOL March TRAIN | 21 of 84 | 0.575 / 0.347, +0.228 [+0.175, +0.280] | 0.401 / 0.492, -0.091 [-0.144, -0.035] |
| SOL March SELECT | 4 of 60 | 0.713 / 0.247, +0.466 [+0.316, +0.681] | 0.336 / 0.388, -0.052 [-0.101, -0.006] |
| SOL capture, Binance | 27 of 42 | 0.297 / 0.066, +0.231 [+0.159, +0.315] | 0.095 / 0.185, -0.090 [-0.129, -0.054] |
| SOL capture, Bybit | 27 of 42 | 0.289 / 0.065, +0.224 [+0.159, +0.304] | 0.114 / 0.196, -0.081 [-0.125, -0.043] |
| ADA first / second half | 24 of 96 / 21 of 72 | 0.115 / 0.055, +0.060 [+0.041, +0.079]; 0.151 / 0.068, +0.083 [+0.062, +0.105] | 0.073 / 0.107, -0.033 [-0.046, -0.020]; 0.095 / 0.131, -0.035 [-0.056, -0.018] |
| MNT first / second half | 24 of 96 / 21 of 72 | 0.096 / 0.055, +0.041 [+0.024, +0.057]; 0.079 / 0.049, +0.031 [+0.018, +0.045] | 0.079 / 0.089, -0.010 [-0.021, +0.002]; 0.068 / 0.073, -0.005 [-0.018, +0.008] |

- (i) Volatile against normal hours: the error is larger in volatile blocks on every coin and window, every interval excludes 0: 1.7 to 4.5 times larger on SOL (the SELECT day has only 4 volatile blocks of 60) and 1.6 to 2.2 times larger on ADA and MNT, though the absolute differences on ADA and MNT are small (0.03 to 0.08 bps). Volatile = the block's volatility above the 75th percentile of the first half's blocks (table on the second half).
- (ii) One-sided trade flow (|signed volume of the previous 10 s| / total >= 0.6, aligned and opposite reported in the json): the error is SMALLER in one-sided-flow periods than in the rest (by 0.03 to 0.09 bps on SOL and ADA, within the interval on MNT), the signed error is about 0 whichever way the flow runs (aligned -0.086 and opposite +0.103 at TRAIN, offsetting). So flow direction is not a risk indicator for the prediction; the split does not say why (time of day and volatility are confounded with it, not separated).
- (iii) Orders within 1 s after a maker fill (SOL March only, the simulated probe fills; ADA, MNT and the capture have no maker fills: N/A): TRAIN 10,115 decisions, MAE 0.627 against 0.400, +0.226 [+0.174, +0.285]; SELECT 3,839 decisions, 0.760 against 0.292, +0.468 [+0.270, +0.697]; for sell orders the signed error is -0.145 (TRAIN) and -0.324 (SELECT) against about 0 for the rest. The prediction is worse, by about 1.6 to 2.6 times, right after a simulated maker fill. Labelled SIMULATED: the fill times come from the simulated queue.
- T1 CAVEAT (owner 15 Nov) applies to all of the above.

## Step 10: mainnet probe (status; the plan is PERP_QUEUE_VALIDATION_PLAN.md, REVISION 15 Nov at its top)
Built (existing `order_lifecycle_logger/ollog`, no new runner; tests/test_perp_probe.py 12 new tests and 2 in test_capture.py, all pass): `fillprobe --plan perp_v2` with PostOnly at the touch and one tick back, minimum size (at most 25 USDT per order), cancels after 1 / 3 / 5 / 10 s on half of the orders, a one-tick price amend on 20%, at most 2 open orders per coin and venue, random placement times plus orders right after a sharp move (3 bps in 1 s, ASSUMED), inventory cap 200 USDT per coin and venue (above 100 only the reducing side is quoted; a reduce-only taker unwind only at the cap, counted in the loss account), loss cap 20 USDT per UTC day and 200 USDT in total (fees in the loss account ASSUMED 2 / 5.5 bps), kill switch on feed UNSAFE (book invalid for more than 5 s), an order in an unknown state, the private stream down with orders resting, a stop file and the total loss cap. Every order is logged AT SEND TIME (`order_sent`) and again to its terminal state, never-filled and rejected ones included. Mainnet needs `--confirm-mainnet I-ACCEPT-REAL-ORDERS`, `OLLOG_MAINNET=1` and mainnet keys in the environment, and must pass `mainnet.violations_v2`; I do not start it. Keys only from environment variables (testnet keys are loaded from the macOS Keychain by testnet_keys.sh; a `setclip` mode was added today because paste did not work in the panel).

TESTNET HOUR (15 Nov 2026, 12:26 to 13:26 UTC, testnet-measured; Bybit testnet linear and Binance USD-M testnet, SOLUSDT and ADAUSDT, fast streams on; MNT not tried on Bybit testnet; no Binance MNT market exists). Aggregates only; no order IDs.

| | Bybit testnet | Binance USD-M testnet |
|---|---|---|
| orders sent | 141 (SOL 68, ADA 73) | 130 (SOL 73, ADA 57) |
| filled / partial then cancelled / cancelled unfilled | 47 / 1 / 93 | 41 / 0 / 89 |
| fills (reports) | 48 | 45 |
| price amends / amend rejects | 27 / 1 | 26 / 0 |
| sharp-move orders / sharp waits that ended without a trigger (counters as logged) | 40 / 12 | 29 / 15 |
| errors, PostOnly rejects, probe-invalid | 0, 0, 0 | 0, 0, 0 |
| taker unwinds | 0 | 0 |
| cancel refused (order already gone) | 1 ("order not exists or too late to cancel") | 1 ("Unknown order sent") |
| held time median / max | 10 s / 120 s | 10 s / 120 s |
| largest position seen (notional) | 22.8 USDT (ADA), 21.9 (SOL) | 21.1 USDT (ADA), 29.2 (SOL) |
| loss account at the end (fees ASSUMED) | 0.077 USDT (max seen 0.139) | 0.159 USDT (max seen 0.171) |
| book check against REST | 231 of 232 exact, 0 sequence gaps, 0 resyncs | 236 of 236 exact, 0 gaps, 2 resyncs |
| fast streams recorded | 3,026 top-of-book updates | 21,111 bookTicker updates |
| stopped by | stop requested (the 60 min timer) | kill switch, manual stop file (the same timer) |
| end of run | flatten reduce-only: ADA -78, SOL +0.1 closed | flatten: ADA -34, SOL -0.16 closed |

- What it shows: the plumbing works end to end on both venues (send-time log, acks, fills, cancels, amends, kill switch, flatten, feed health, fast streams). The Bybit amend call now has a testnet proof (27 amends, 1 reject), closing one of the earlier UNVERIFIED items for testnet only. It does NOT show fill-model accuracy: Binance testnet fills ignore queue priority and testnet flow is not mainnet flow. The cancel-after-fill case occurred once per venue and was handled.
- Hit rate here (47 of 141, 41 of 130) says nothing about mainnet.
- Notes: Binance resynced twice with no sequence gap logged (I did not look at why). The earlier 1-hour PAPER run was interrupted at 641 orders by a machine restart and was NOT rerun, since the testnet hour covered the plumbing with real venue behaviour.
- Not done / owner's: mainnet start, sub-account trade-only keys, where it runs (Mac or VM), the day split for the gates, whether the live arb bot already logs full order lifecycles, and the cost cap decision (replaced by 20 USDT per day and 200 USDT in total). Recorder changes on the VM need their own approval.

## Index of this order
Step 1, 2: above. Step 3: sigma x sqrt(T) against CVaR lambda_u; CVaR makes E stop splitting. Step 4: displayed depth stays the better predictor out to 400% of D10; own consumption is a first-order cost at 1 s spacing and gone at 10 s; the overlay (MODELLED) adds a spacing-independent cost; no hedge mode beats all-at-once on the risk-adjusted net at 100 to 1000 SOL. Step 5: resting is within 0 to 0.4 bps of all-market on cost and does not pay at risk weights above 0. Step 6: errors are larger in volatile hours everywhere, smaller in one-sided flow, larger right after a simulated maker fill. Steps 7, 8, 9: top of this file. Step 10: built, tested, testnet hour done; mainnet is the owner's.
