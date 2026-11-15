# Impact fits (step C): data, definitions and what is reported (owner 14 Nov; written BEFORE any fitted number exists)

## Data (checked 14 Nov; no download, no write to data/)
- TRAIN: 2024-03-01, SELECT: 2024-03-08 and 2024-03-11 (CLAUDE.md fixes TRAIN 03-01..07, SELECT 03-08..12, HOLDOUT 03-13..15). Only these three 2 h slices of SOLUSDT exist locally (out/queue_v2/slices_mar2024):
  Bybit full book (500 levels) + prints, Binance best bid/ask + prints. THERE IS NO MARCH HOLDOUT LOCALLY and none is relabelled.
- HOLDOUT, used ONCE after every setting is fixed: the 2026-10-29 capture (14.04 h, both venues L2, Bybit 50 levels, Binance 1000), labelled OUT-OF-PERIOD (a different year and regime; not the owner's March holdout).
- Form (a) needs the depth D_k at the start of a run, so it is fitted on BYBIT only (March has no Binance depth). Binance results are on the capture alone and descriptive.
- Form (b) needs V_daily: a 2 h slice gives 2 h of volume; V_daily = window volume x 24 / window hours (ASSUMED, labelled; the capture uses its own 14.04 h). Y per day therefore has 3 March windows + the capture: its sd is over 3 to 4 windows only.

## Definitions
- Aggressor run (metaorder PROXY): consecutive prints of one venue, same aggressor side, each less than dt after the previous. Runs below 1 SOL in total are dropped. dt in {0.1, 0.5, 1.0} s, chosen on SELECT.
- Isolated run: in the window [start - 60 s, end + 60 s] the run's signed volume is at least 75% of the net signed volume of that venue's prints (same sign, net not zero).
- Mid: the venue's book mid on a 100 ms grid (the last book state at or before the grid time).
- Impact (the run's "price move"): side-signed (mid at the run's last print - mid just before its first print) / mid before x 10,000 bps, positive = against the aggressor. This is the PEAK. Residual at h = 1, 10, 60 s: the same move measured at the end of the run + h.
- Path: the move when cumulative run volume first reaches 25 / 50 / 75 / 100% of the run's volume, against the peak of runs whose total size is within 20% of that fraction of the run's size.
- Cross-impact coefficient: regression through the origin of the OTHER venue's mid move over the same interval (start to end + h, same sign convention) on the run venue's move, for h = 0, 1, 10, 60 s.
- Depth D_k: size within k bps of the touch on the side the run consumes, at the run's start. k in {2, 5, 10}, chosen on SELECT.
- sigma (form b): standard deviation of 1-minute mid log returns of the window, scaled to the day by sqrt(1440), in bps.

## Forms and fit (nothing else is tried)
- (a) cost_bps = a + b x (Q / D_k)^delta, delta on a grid 0.2 to 1.5 step 0.01, (a, b) by least squares for each delta (b not negative).
- (b) cost_bps = Y x sigma_daily_bps x (Q / V_daily)^delta, delta on the same grid, Y by least squares through the origin; Y estimated per window.
- Both on the peak impact, all runs of the TRAIN day. dt and k are chosen on SELECT by the lowest MAE of form (a) (form (b) uses the same dt). delta is also fitted separately in two size bands: small = Q / D_k below the TRAIN median, large = above.
- NULL baseline: the TRAIN mean peak in bps.

## What is reported (once, on the HOLDOUT)
- MAE in bps of form (a), form (b) and the null, with a 95% block-bootstrap interval of each MAE difference (10-minute blocks, 2,000 resamples, seed 1); which form predicts better.
- delta for each form, all runs and the two size bands; a/b; Y (mean and sd across windows); permanent fraction phi = mean residual(60 s) / mean peak, all vs isolated; tau of the exponential transient (option A) and beta of the
  power-law transient (option B, G(u) = (1 + u / 1 s)^-beta) fitted to the mean residual path at 1, 10, 60 s; cross-impact coefficient. 95% intervals by block bootstrap. Label of every number: PROXY (public prints, no trader ids),
  EXPLORATORY, few windows.
- The size at which the predicted peak reaches one tick (about 1 bp on SOL) is computed from the fit and decides the run sizes of the large-size replays: a peak below half a tick rounds to no shift.
