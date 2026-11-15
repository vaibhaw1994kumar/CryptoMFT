# Step G results (owner 14 Nov; report only; rules in G_RULES.md written before the numbers; EXPLORATORY, PROXY)

Windows: 00:00-02:00 UTC of March 2024 days; TRAIN 03-01..07, SELECT 03-08..12, HOLDOUT 03-13..15 read ONCE (T1 and T5) after the settings were fixed; the 2026-10-29 capture is an extra, out-of-period. Block bootstrap: 10-minute blocks.

## T1 aggressive (hypothetical market-style orders, outcome = a walk of the book 0.1 s later against the touch at the decision)
- T1 CAVEAT (owner 15 Nov): with ~100 ms books the outcome 0.1 s later is often the same or the next snapshot (97% on the Binance capture, ADA and MNT; on Bybit March it is read after about 64 updates), so the small error is partly BY CONSTRUCTION; T1 does not test other participants' reaction to our order, nor book changes below 100 ms.
HOLDOUT, Bybit March, 212,399 orders: realised cost 1.16 / 1.76 / 2.91 / 4.22 / 6.20 bps at 5 / 10 / 25 / 50 / 100% of the displayed depth within 10 bps (D10).
| prediction | MAE (bps) | signed error |
|---|---|---|
| displayed depth (P0) | 0.3702 | 0.000 |
| effective depth, survival as measured (P1) | 0.3715 | +0.007 |
| effective depth, survival x0.8 | 0.6649 | +0.463 |
| effective depth, survival x1.2 (capped at 1 = P0) | 0.3702 | 0.000 |
- EFFECTIVE DEPTH DOES NOT REDUCE THE ERROR: P1 minus P0 = +0.0013 bps (interval 0.0011 to 0.0014): marginally worse. The measured survival is 0.97 to 0.99 per update, so the effective depth is almost the displayed depth. Survival x0.8 over-predicts and costs +0.293 bps of MAE (0.276 to 0.311). TRAIN and SELECT give the same picture (+0.0010 and +0.0015; x0.8 +0.27 and +0.32).
- DECISIONS: "market-style allowed" (predicted cost at most 2 bps) flips for 5.2% of orders between x0.8 and x1.2 and 0.09% between the measured value and displayed; the CR-LQ answer (effective depth within 10 bps covers the order) flips for 20% between x0.8 and x1.2, all of them at full depth. The orders on which x0.8 and x1.2 disagree cost 1.87 bps, against 3.32 where they agree.
- CLIFF FLAG (gap run length 3 bps, ASSUMED): flagged 0.03% of orders (73); their realised cost is not higher (3.35 against 3.25 bps) but their prediction error is 5 times larger (MAE 1.82 against 0.37); they hold 0.17% of the total absolute error, so excluding them changes the overall MAE by at most 0.001 bps: the flag marks rare large-error orders and cannot reduce the error in any material way. Defect found by the first run (97% flagged with the tick-based run length m = 3 on a 0.001 tick) and fixed before any other window: `dq_gap_min_bps`.
- COST CURVE cost_bps = a + b (Q / D10)^delta (block-bootstrap 95% intervals): Bybit March, all 15 windows (1,061,999 orders): a -0.17 (-0.24 to -0.15), b 6.39 (6.35 to 6.47), delta 0.53 (0.52 to 0.53); HOLDOUT alone a -0.07, b 6.26, delta 0.54 (0.53 to 0.55); TRAIN 0.53, SELECT 0.52. Capture 2026-10-29: Bybit delta 0.76 (0.74 to 0.79), b 5.72; BINANCE delta 0.80 (0.79 to 0.82), b 5.55, a -0.23. Binance March has no depth (best bid/ask only), so T1 on Binance exists for the capture only; the delta of the capture (0.76 to 0.80) is clearly above March's 0.53.
- BINANCE (capture): survival (mean per update) median 0.966, 5th percentile 0.851, 1st 0.746, 11.1% of samples below 0.9 (Bybit: median 0.982, 1.1% below 0.9). Predicting with it makes the Binance error WORSE: MAE 0.148 displayed, 0.219 with the measured survival (+0.071, 0.060 to 0.083), 0.700 at x0.8, 0.151 at x1.2; Bybit capture 0.155 / 0.181 / 0.613 / 0.155. The per-update survival is pessimistic about what is lost over 0.1 s.
- CLIFF FLAG FREQUENCY by gap run length (capture, share of orders): Bybit 0.25% at 2 bps, 0.16% at 3 bps, 0.02% at 5 bps; Binance 0.04% at 2 and 3 bps, 0.01% at 5. Flagged orders cost more (Bybit 4.57 against 2.55 bps, Binance 3.71 against 2.16) and are rare. dq_gap_min_bps = 3 is ASSUMED.
- Label: APPROXIMATE stays on survival and the depth-quality fields until checked against real hedge fills.

## T5 split: E against all-market hedges (SUPERSEDED by the price-aware rerun in the follow-ups below: these gains are an UPPER BOUND; hypothetical hedges on Binance March prints; the touch is assumed to fit the market size; fees v1_9: taker 1.7, maker 0)
SELECT chose T = 1 s and lambda_u = 10 bps (the most cautious cell; highest risk-adjusted net at S = 10 SOL, weight 1 sigma, sigma_1s = 2.58 bps per sqrt(s) from TRAIN). HOLDOUT, 42,420 hedges per size:
| S | rested share | cost M | cost E | E minus M (bps) | unhedged time E | net E minus M at risk weight 0 / 0.5 / 1 | rested-fill markout +1 s / +5 s |
|---|---|---|---|---|---|---|---|
| 1 SOL | 12% | 1.738 | 1.562 | -0.176 (-0.233 to -0.126) | 0.07 s | +0.176 / +0.143 / +0.110 (0.068 to 0.158) | -0.39 / -0.44 |
| 10 SOL | 7% | 1.738 | 1.633 | -0.105 (-0.152 to -0.067) | 0.06 s | +0.105 / +0.082 / +0.060 (0.029 to 0.099) | -0.41 / -0.45 |
| 100 SOL | 1% | 1.738 | 1.717 | -0.021 (-0.035 to -0.011) | 0.05 s | +0.021 / +0.015 / +0.010 (0.003 to 0.020) | -0.41 / -0.44 |
- SELECT gave +0.059 / +0.035 / +0.016 at weight 1 (the 100 SOL interval includes 0: NO EFFECT), TRAIN +0.100 / +0.048 / +0.008. Every larger resting share loses at weight 1 (resting everything: -0.5 to -1.5 bps at 10 to 100 SOL, because of the unhedged time at sigma 2.6 bps per sqrt(s)).
- THE RESTING "COST" DOES NOT INCLUDE THE MARKOUT OF THE RESTED FILLS: it is the fill price against the decision mid (so the move between the decision and the fill IS in it) and the hedge is flat once it fills, so what the market does after the fill is not a cost of the hedge. The markout is reported apart: it is ADVERSE, -0.39 to -0.45 bps at +1 s and +5 s (the market moves against a resting order after it fills, the usual adverse selection).
- GAINS ARE SMALL: 0.01 to 0.18 bps on a 1.74 bps hedge (the taker fee is 1.7 of it), and the queue model is perp-unvalidated: treat as NO EFFECT until the queue is validated; the intervals separate from 0 in the bootstrap only.

## T2 venue (the 2026-10-29 capture only; NO TRAIN/SELECT/HOLDOUT split is possible: no other day has both venues' depth; size 100 SOL, gross 500, participation limit OFF)
| arm | fills (fully hedged) | bursts | net per fill (bps) |
|---|---|---|---|
| maker fixed on Bybit | 68 | 3 | -0.62 (-2.43 to -0.03) |
| maker fixed on Binance | 24 of 41 | 3 | -6.18 (-6.47 to -5.04), gross -2.27 |
| chosen by effective depth | 34 | 2 | -4.69 (-6.47 to -2.43) |
| chosen by E | NOT RUN | | needs the queue and recent volume of BOTH venues at each quote time; the arms were run without the split evidence |
- The Binance-maker arm hedges on Bybit (taker 3.2 bps against Binance 1.7) and uses the profile's Bybit-specific lifecycle and ack calibrations unchanged; 7 and 6 quotes in all. The effective-depth rule (the venue with more effective depth within 10 bps hedges) names Bybit as the hedge venue at 6 of the 7 quotes of the Bybit arm, because Bybit's depth within 10 bps is usually larger on the bid side, so it moves most quotes to the worse arm. Every interval is wide (3 bursts); the ordering fixed Bybit better than Binance is the only robust statement.


---
# FOLLOW-UPS 1-5 (owner 15 Nov; rules in FOLLOWUP_RULES.md, written before any number; EXPLORATORY, PROXY; HOLDOUT 03-13..15 NOT READ again: available on the owner's go)

## CORRECTION TO T5 ABOVE (found while doing follow-up 2): the G T5 fill rule was optimistic
The G run counted EVERY print of the depleting side (buy prints for a resting sell) as depleting our queue, whatever its price. A buy print BELOW the resting sell's price does not touch it, and the market drifts away from a resting order exactly when that happens.
The fills are now PRICE-AWARE (`--price-aware`): a print at the rest price counts its size, a print beyond it clears the level (the order is filled), a print inside it counts 0; the outflow samples that size the limit part are built the same way. The default (G run) is unchanged and kept in t5b/.
- The T5 gains reported above (0.176 / 0.105 / 0.021 bps) are therefore an UPPER BOUND. They were not recomputed on the HOLDOUT (that would be a second read); the same days under the corrected rule are below. Treat the G T5 section as superseded by this one.

## 2. T5 at the maker-fill moments (probe fills, SIMULATED; clock check passed: the probe and Binance series agree within 0.004 bps, t5c/clock_check.txt)
Hedge = sell on Binance at the fill time of the 1 SOL probe bid, arriving 0.045 s later; TRAIN 03-01..07 (2,035 fills), SELECT 03-08..11 (692); no HOLDOUT fills exist (not run). Price-aware fills. The cell is re-chosen on SELECT random moments under the corrected rule (t5e/chosen.json): T = 1 s, lambda_u = 5 bps (G chose lambda_u 10 on the old rule).
- THE HEDGE IS DEARER RIGHT AFTER A MAKER FILL: all-market cost 2.30 bps at the fill moments against 1.76 at random moments (sell hedges, same days): the Binance mid has moved by 7.5 bps against the hedge in the 5 s before the fill on average (terciles of the 5 s move: 5.9 and 9.8 bps).
| S | rested share | fill rate of the rested part | E minus M cost (all 3,000 fills) | net E minus M at weight 0 / 0.5 / 1 | same on random moments (price-aware) at weight 0 / 1 |
|---|---|---|---|---|---|
| 1 SOL | 15% | 65% | -0.093 (-0.160 to -0.020) | +0.093 (0.021 to 0.160) / +0.020 / -0.053 (NO EFFECT) | +0.109 / +0.011 |
| 10 SOL | 10% | 63% | -0.062 (-0.120 to -0.009) | +0.062 (0.007 to 0.115) / +0.006 / -0.050 (NO EFFECT) | +0.061 / +0.003 |
| 100 SOL | 5% | 55% | -0.018 (-0.050 to 0.014) | +0.018 (NO EFFECT) / -0.014 / -0.046 (-0.085 to -0.007: E WORSE) | +0.019 / +0.001 |
- Under the corrected rule the rested part fills 55 to 65% of the time (97% on the old rule), and E's advantage nearly disappears: at risk weight 1 it is no better than all-market (10 SOL: NO EFFECT; 100 SOL: worse). At weight 0 (cost only) it is 0.02 to 0.09 bps, about as on random moments.
- Does E do better right after an adverse move? Cost: yes (E minus M cost -0.20 bps for 1 SOL in the most adverse tercile against -0.07 in the least, share rested 24% against 11%). Net at weight 1: no (+0.04 [-0.14, 0.19], NO EFFECT; the middle tercile is worse, -0.12 [-0.23, -0.03]). The extra rested volume is paid for in unhedged time.
- Rested-fill markout (apart, not a hedge cost): +0.08 bps at +5 s (interval -1.6 to 2.1): not identified at this sample.
- Choice check: on the fill moments the 12-cell grid at 10 SOL picks T = 1 s, lambda_u = 10 on TRAIN (net -2.80), the same cautious end as SELECT random moments; every cell that rests more than 20% loses (-3.1 to -6.0 at weight 1).

## 1c. T5 at 100 and 500 SOL, capture, Binance as the hedge venue with its REAL depth (the touch does not fit the size); chronological halves of one day (the first half chooses, the second is the answer; sigma_1s = 1.05 bps per sqrt(s) of the first half); price-aware fills; 10,012 decisions per size in the second half
All-market cost (taker 1.7 included): 2.22 bps at 100 SOL, 2.30 at 500; the depth adds only 0.5 to 0.6 bps over the fee at these sizes on this day. Chosen on the first half at 100 SOL: E T = 5 s, lambda_u = 5; slices N = 2, H = 5 s, kappa H = 2 (REFILL and OFF).
| arm (second half) | 100 SOL cost minus M | net minus M at w 0 / 0.5 / 1 (vw time) | 500 SOL cost minus M | net minus M at w 0 / 0.5 / 1 |
|---|---|---|---|---|
| E (rests 28% / 23%, fill rate 73% / 72%) | -0.218 (-0.287 to -0.151) | +0.218 / +0.060 (0.000 to 0.113) / -0.098 (-0.177 to -0.035) | -0.172 (-0.230 to -0.117) | +0.172 / +0.038 (NO EFFECT) / -0.096 |
| time-sliced AC, own consumption REFILL tau 3.5 s | -0.005 | +0.005 / -0.370 / -0.746 | -0.044 | +0.044 / -0.331 / -0.706 |
| time-sliced AC, own consumption OFF (bound) | -0.006 | +0.006 / -0.370 / -0.745 | -0.050 | +0.050 / -0.326 / -0.701 |
- TIME-SLICING DOES NOT PAY: its saving is 0.005 to 0.05 bps (the impact of the whole size is only 0.5 bps, so there is little to save by spreading), and the extra unhedged time costs 0.33 to 0.75 bps at weights 0.5 to 1. The same with the inventory-risk time (sqrt of the integral of the remaining share squared) in the JSON; the verdicts agree. Own consumption (REFILL against OFF) changes the cost by about 0.001 bps: negligible here.
- E: a cost saving of 0.17 to 0.22 bps (intervals exclude 0) that turns into a LOSS at weight 1 (-0.10); at weight 0.5 it is +0.06 at 100 SOL and no effect at 500. The same pattern as follow-up 2. The queue model is perp-unvalidated.
- One day, one regime (sigma_1s 1.05 against 2.58 in March), and the first half is not the owner's TRAIN. All 36 arms on both halves are in t5c/result_large.json.

## 1a. T1 at 25 / 50 / 100 / 200 / 400 % of D10 (Bybit March TRAIN + SELECT, 12 days; the capture on both venues; HOLDOUT NOT READ)
- T1 CAVEAT (owner 15 Nov): with ~100 ms books the outcome 0.1 s later is often the same or the next snapshot (97% on the Binance capture, ADA and MNT; on Bybit March it is read after about 64 updates), so the small error is partly BY CONSTRUCTION; T1 does not test other participants' reaction to our order, nor book changes below 100 ms.
Identity check passed first: with sequences, extras and the 500-level view on, every old column of 2024-03-01 equals the G run exactly (t1b_check). The view keeps 500 levels (the G run's view kept 200), so 4 x D10 can be walked; unfillable shares: SOL March 0.0 / 0.0 / 0.0 / 0.1 / 4.7% (TRAIN) and 24% at 400% (SELECT, thinner days); capture Bybit (50 levels) 17.7% at 400%; Binance 0%.
The error comparisons at 400% are conditional on the order being fillable with every prediction (effective depth x0.8 included), so they drop some of the costliest orders: read the 400% rows as optimistic.
| realised cost (bps) | 25% | 50% | 100% | 200% | 400% |
|---|---|---|---|---|---|
| Bybit March TRAIN | 2.92 | 4.23 | 6.24 | 9.55 | 14.4 |
| Bybit March SELECT | 2.91 | 4.24 | 6.21 | 9.63 | 14.3 |
| capture Bybit | 2.01 | 3.38 | 5.74 | 10.04 | 16.1 (18.1 in the fit) |
| capture Binance | 1.62 | 2.93 | 5.33 | 10.34 | 18.9 |
- PREDICTION ERROR (MAE, displayed depth P0): flat in size on March (0.43 to 0.39 TRAIN, 0.29 to 0.33 SELECT), 0.14 to 0.19 on the capture. EFFECTIVE DEPTH IS WORSE THAN DISPLAYED EVERYWHERE, and the gap grows with size and with the coarser feed: P1 minus P0 MAE March TRAIN +0.003 (0.003 to 0.003), SELECT +0.004, capture Bybit +0.079 (0.069 to 0.090; +0.29 at 400%), capture Binance +0.290 (0.240 to 0.343; 0.89 against 0.15 at 400%). The signed error of P0 is about 0 at every size.
- COST CURVE a + b (Q/D10)^delta over f = 0.25 to 4 (block-bootstrap 95%): Bybit March TRAIN+SELECT a 0.85, b 5.40, delta 0.69 (0.68 to 0.70); capture Bybit delta 0.88 (0.85 to 0.90), Binance 0.90 (0.88 to 0.92), ADA 1.11 (1.10 to 1.11), MNT 0.64 (0.62 to 0.66). Out to 400% the curve is closer to linear than the 0.53 found for f up to 100%: do not extrapolate the small-size delta.
- Cliff flag at 2 / 3 (ASSUMED) / 5 bps and the 3-tick rule, share of orders: March TRAIN 0.76 / 0.07 / 0.04% and 98.8% for 3 ticks (a 0.001 tick: the defect found in G); capture Bybit 1.44 / 0.28 / 0.03% (3 ticks 0.39%); Binance 0.49 / 0.10 / 0.02%. Flagged orders cost more (March 8.2 against 7.1 bps; Binance 30 against 7.8).

## 1b. Repeated hedging: 10 hedges of f x D10 every s seconds, own consumption OFF / REFILL (tau 3.5 s from the overlay file; a per-level size refill, NOT the overlay's permanent shift) / NEVER (bound); a virtual offset in the walk, the live book is never changed
Mean cost per unit (bps), Bybit March TRAIN (SELECT within 0.1 to 0.5 bps of it; capture, ADA, MNT in t1b/extra_*.json and t1c/extra_*.json):
| f x D10 | spacing | OFF | REFILL | REFILL minus OFF | NEVER |
|---|---|---|---|---|---|
| 0.1 | 1 s | 1.74 | 2.14 | +0.39 (0.37 to 0.42) | 2.43 |
| 0.1 | 10 s | 1.76 | 1.76 | +0.01 | 1.91 |
| 0.5 | 1 s | 4.27 | 6.09 | +1.82 (1.72 to 1.93) | 8.02 |
| 0.5 | 10 s | 4.31 | 4.34 | +0.03 | 5.47 |
| 1.0 | 1 s | 6.31 | 10.01 | +3.70 (3.53 to 3.87) | 13.38 |
| 1.0 | 2 s | 6.33 | 8.11 | +1.78 | 12.10 |
| 1.0 | 5 s | 6.33 | 6.78 | +0.45 | 10.45 |
| 1.0 | 10 s | 6.36 | 6.44 | +0.08 | 9.37 |
- Without own consumption the cost per unit does not depend on the spacing (it is the single-order cost). With it, a sequence at 1 s spacing pays 60% more per unit at full depth (10.0 against 6.3 bps) and the hedge number k matters: k = 1 costs 6.3, k = 10 costs 11.3 (REFILL). The extra cost halves each time the spacing doubles (tau 3.5 s), and is under 0.1 bps at 10 s for sizes up to D10.
- Thinner books and the capture are worse: at f = 1, s = 1 REFILL minus OFF is +9.1 (capture Bybit), +10.1 (Binance), +11.0 (ADA), +7.2 (MNT) bps. NEVER (no refill inside the sequence) is the bound: 13 to 42 bps at f = 1, s = 1; its mean EXCLUDES the unfillable hedges (9.5% to 26% on Bybit March, 40% on capture Bybit at f = 1, s = 1), so it is a floor.
- THE REFILL CONSTANT IS AN ASSUMPTION (tau 3.5 s from the overlay fit; refilling is other participants' reaction, which this feed does not contain). The answer to "what spacing makes repeated hedging free" is therefore a function of tau: spacing of about 3 tau for small sizes. Measuring tau needs our own hedges (follow-up 5).

## 3. ADA and MNT (Bybit linear perp, April 2026, 14 two-hour windows per coin: days 04-01..13 odd, 00-02 and 13-15 UTC; data from the owner's ~/Downloads/bybit_data, read in place, ob200, about 100 ms; data/ has nothing for them)
- T1 CAVEAT (owner 15 Nov): the ADA/MNT archive is a ~100 ms feed: 91 to 93% of the 0.1 s outcomes are the next snapshot, so the small errors below are partly BY CONSTRUCTION; T1 does not test others' reaction to our order or changes below 100 ms.
- BINANCE ADA / MNT DEPTH: NONE anywhere locally (data/binance has only BTCUSDT and SOLUSDT). Everything below is Bybit only.
| | ADA | MNT | SOL Bybit capture | SOL Bybit March |
|---|---|---|---|---|
| survival, mean per update | 0.990 | 0.975 to 0.979 | 0.977 | 0.996 |
| samples with survival < 0.9 | 0.0% | 0.7 to 1.3% | 1.2% | 0% |
| cancel-to-trade, median (unexplained depth decrease over printed volume, 10 s; UNITS differ by coin, compare within a coin only) | 1,004 to 1,414 | 155 to 235 | 138 | 6.9 (TRAIN) |
| cliff flag 2 / 3 / 5 bps | NOT MEANINGFUL: 99 to 100% | 2.9 / 1.1 / 0.07% (first half) | 1.44 / 0.28 / 0.03% | 0.76 / 0.07 / 0.04% |
| P1 minus P0 MAE (effective depth worse) | +0.063 / +0.079 | +0.097 / +0.076 | +0.079 | +0.003 |
- ADA's FLAG IS A DEFECT OF THE RULE, NOT A FINDING: one tick is 4.1 bps (price 0.24, tick 0.0001), above the 3 bps stretch limit of the gap rule, so every adjacent pair of levels is a "jump" and almost every sample is flagged (99.9%). The gap rule needs the stretch limit in ticks for coarse-tick coins; not changed here (core file, out of scope), reported.
- MNT (tick 1.4 bps): the flag at 3 bps marks 1.1 to 1.5% of orders; flagged orders cost 12 against 7 bps in the first half, but only 9.9 against 7.0 in the second: not stable.
- T1 error, volatile against normal 10-minute blocks (volatile = above the 75th percentile of the first-half blocks of the same coin; SOL March: TRAIN blocks, 4 of 60 SELECT blocks only; capture: its own first half), MAE of the displayed-depth prediction, volatile minus normal with intervals:
| group | MAE volatile | MAE normal | difference |
|---|---|---|---|
| SOL March TRAIN | 0.575 | 0.347 | +0.228 (0.175 to 0.280) |
| SOL March SELECT | 0.713 | 0.247 | +0.466 (0.316 to 0.681), only 4 volatile blocks |
| SOL capture Bybit | 0.289 | 0.065 | +0.224 (0.159 to 0.304) |
| SOL capture Binance | 0.297 | 0.066 | +0.231 (0.159 to 0.315) |
| ADA, second half | 0.151 | 0.068 | +0.083 (0.062 to 0.105) |
| MNT, second half | 0.079 | 0.049 | +0.031 (0.018 to 0.045) |
  The signed error stays near 0 in both regimes for the displayed prediction (so the cost is predicted right on average and with 2 to 4 times the spread in volatile blocks); the effective-depth prediction is biased high in volatile blocks (+0.67 bps on Binance, +0.20 on Bybit capture, +0.15 on ADA).
- Thinner books do NOT make effective depth work: it is worse than displayed on both coins and in the second half as in the first. The realised cost at the same fraction of D10 is similar across SOL, ADA and MNT (5.1 to 5.8 bps at 100%), since D10 itself scales with the book.

## 4. How much of the small T1 error is by construction (framing)
The outcome is read 0.1 s after the decision. Share of samples with NO book update between the decision and the arrival, with exactly one, with two or more:
| feed | none | one | two or more |
|---|---|---|---|
| Bybit March (event feed) | 0.1% | 0.2% | 99.7% (median 64 updates) |
| Bybit capture (50 levels, about 20 ms) | 5.1% | 9.9% | 85.0% (median 3) |
| Binance capture (100 ms) | 9.0% | 87.3% | 3.7% |
| ADA / MNT archive (about 100 ms) | 1 to 4% | 91 to 93% | 3 to 9% |
- On the 100 ms feeds (Binance capture, ADA, MNT) the book at the arrival is the same state or the NEXT snapshot in 97% of samples: the displayed-depth prediction cannot be wrong by much, because the next snapshot is the same book plus one update. The MAE of 0.15 bps for Binance and 0.06 to 0.1 for ADA / MNT is partly by construction. On Bybit March and the 20 ms Bybit capture the outcome is read after many updates, so the 0.3 to 0.4 bps (March) and 0.15 bps (capture) are real changes of the book.
- T1 does NOT test the reaction of other participants to our order (the book at the arrival is the book without our order), nor price moves below 100 ms on the 100 ms feeds, nor hidden liquidity or fees. It tests whether the displayed depth predicts the walk of the book a moment later.

## 5. Perp queue validation: a PLAN only (PERP_QUEUE_VALIDATION_PLAN.md); the question to the owner is in it and in the report: does the live perp arb bot log full order lifecycles?
