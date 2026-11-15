# Step G tests T1, T2, T5: rules (owner 14 Nov; written BEFORE any G number exists; report only)

## Windows and discipline
- 00:00 to 02:00 UTC of each March 2024 day (the probe's window), SOLUSDT, read from data/ (read only): TRAIN 03-01..07, SELECT 03-08..12, HOLDOUT 03-13..15. TRAIN fits what has a fit, SELECT chooses among the fixed alternatives,
  HOLDOUT is read ONCE, after the settings are written here and the TRAIN/SELECT results are in. The 2026-10-29 capture (both venues L2, 14 h) is reported on its own as an extra, out-of-period; it is not the holdout.
- Block bootstrap everywhere: 10-minute blocks, 2,000 resamples, seed 1. Mean and 95% interval. Every number EXPLORATORY and PROXY: the replay and the feed carry the real book at arrival (vanished depth is in them)
  but not other participants' reaction to our orders.
- Binance March is best bid/ask with sizes only (no depth): everything that needs depth uses BYBIT in March; Binance depth exists on the capture only.

## T1 aggressive: does effective depth, then the cliff flag, reduce the error of the hedge cost prediction?
- Hypothetical market-style orders on Bybit (the venue with March depth), every 1 s after a 120 s warm-up, both sides, size Q = f x D10 with f in {0.05, 0.1, 0.25, 0.5, 1.0} and D10 the displayed size within 10 bps of the touch
  on the consumed side at the decision.
- Outcome ("simulated hedge cost at arrival"): the side-signed VWAP of a walk of the book as it stands 0.1 s after the decision, against the touch at the decision, in bps (positive = worse); an order the book cannot fill then is
  counted and left out. 0.1 s is the order of the hedge flight time; no other delay is tried.
- Predictions at the decision: P0 = the VWAP cost on the DISPLAYED depth (level 1, survival 1); P1 = the same on the EFFECTIVE depth (every displayed size times s, s = the MEAN survival of the consumed side as the tracker measures it,
  per-update ratio, step B), run with s as measured and with s x 0.8 and s x 1.2 (capped at 1); P2 = P1 plus the cliff flag (step B: status GAP and the size before the first gap smaller than Q). The gap run length is 3 bps as a PRICE distance (VenueStateConfig.dq_gap_min_bps = 3; the tick-based m = 3 flagged 97% of March samples because a 0.001 tick is 0.07 bps: a defect of the step B default, found by the first T1 run on 03-01 and fixed before any other window was run).
- Measures: MAE and mean signed error (prediction minus outcome) in bps, per f and pooled, with intervals; the DECISIONS that depend on the prediction, and how many flip between s x 0.8, 1.0, 1.2: (i) market-style allowed when the predicted cost is
  at most 2 bps; (ii) the CR-LQ answer (the effective depth within 10 bps covers Q: LQ_DEPTH_COVERS, or not); for the flag: the realised cost and its error among flagged and unflagged samples.
- SELECT chooses nothing here (no parameter): TRAIN and SELECT are shown for stability, HOLDOUT is the answer. "Reduces the error" = the HOLDOUT MAE difference interval excludes 0 and the point estimate is below 0.

## T5 split: E's market/limit hedge split against the current hedge (market-style at the touch), on real prints
- Hypothetical hedge of size S in {1, 10, 100} SOL on BINANCE every 1 s after a 120 s warm-up, both sides. Arrival delay 0.045 s (the probe's hedge delay).
- Current (M): the whole size at the touch at arrival: cost = side-signed (decision mid - touch at arrival) / mid in bps + the taker fee (1.7 bps, the v1_9 schedule).
- E: split_single_venue (closed form) with queue ahead = the resting side's touch size at the decision, outflow samples = (depleting aggressor volume over the next T) / (that side's volume over the previous 10 s, floor 1 SOL) taken from the TRAIN
  days' prints and scaled by the same quantity at the decision, half spread from the touch, taker fee 1.7, maker rebate 0 (v1_9), adverse selection 0, theta 0, lambda_u as below. The limit part rests at the touch (price fixed) and fills when the depleting
  aggressor volume since the decision reaches the queue ahead plus its size (first print that does it; partial fill proportional); the unfilled part is sent at market at T + 0.045 s. Cost of the limit fill = -(half spread) in bps (it sells at the ask) - rebate.
- Measures per size: hedge cost (bps per unit), unhedged time (volume-weighted seconds), and the risk-adjusted net = -cost - sqrt(unhedged time) x sigma_1s (sigma_1s from TRAIN prints' mid), with intervals; E minus M with an interval.
- TRAIN builds the outflow samples (per T). SELECT chooses (T, lambda_u) from T in {1, 2, 5} s and lambda_u in {1, 2.6, 5, 10} bps by the highest risk-adjusted net at S = 10 SOL. HOLDOUT is the answer.

## T2 venue: maker fixed on Bybit, fixed on Binance, chosen by effective depth, chosen by E (the capture only)
- Replays of v1_9 on the 2026-10-29 capture (the only day with both venues' L2, so NO TRAIN/SELECT/HOLDOUT split exists for this test), size 100 SOL, gross 500, participation limit OFF (labelled), arms maker=Bybit and maker=Binance.
- The two "chosen" arms are COUNTERFACTUAL selections from those two replays: at each quote time the rule names the maker venue and that quote's fills are taken from the matching arm. Effective depth rule: the venue with the larger effective depth
  within 10 bps (displayed x mean survival) is the HEDGE venue. E rule: the venue whose limit allocation is larger in split_multi_venue for the size. Net per fill with burst-block intervals. If an arm does not run, it is reported as NOT RUN with the reason.
