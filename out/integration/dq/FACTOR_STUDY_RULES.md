# Depth-quality factors: what the report tests and when a factor is kept (owner 14 Nov, written BEFORE any table was computed)

Factors (core `VenueState`, settings ASSUMED: top 5 levels, 10 s window, 50 ms match window, gap stretch 3 bps, level size 0, feed-fault window 2 s):
`depth_to_first_gap` per side and `flicker` per side. FEED_FAULT and not-evaluable samples are counted and left out of every table.

## What data exists (checked 14 Nov)
- REAL FILLS: none usable. The only real passive fills are the account's Bybit SPOT PostOnly orders of 2026-04-19..25 (`bybit_30d.zip`), and their book data (`data/bybit-spot`, the recorder capture) is LOST;
  `data/bybit/*` holds empty folders. No real arb hedge fill exists (the arb never ran live; the testnet runs have no usable depth). So NO table below uses a real fill.
- REAL BOOKS AND PRINTS: the 2026-10-29 SOLUSDT capture, both venues, L2 at the feed's rate (Bybit 50 levels, Binance 1000), one day.
- SIMULATED hedge slippage (the replay's own fills) is MECHANICAL for these factors: the simulator walks the same book the factors measure. It never counts toward keeping a factor, and no such table is built.

## The tests (feed only, no orders, no simulator fills; every table is labelled PROXY, ONE DAY, EXPLORATORY)
1. HEDGE SLIPPAGE PROXY (hedge venue Binance, both consumed sides). A hypothetical market order of size Q in {5, 25, 100} SOL at time t on a 1 s grid. Outcome = the CHANGE in the walk's slippage between t and t + 0.2 s,
   dS = S(t+0.2) - S(t), with S = (VWAP of the walk - own touch) in bps, positive = worse, each against its own touch. This is the depth that left or appeared during the order's flight, which the book at t does not
   show; it removes the mechanical part (a hole already in the book at t is in S(t), not in dS). A sample whose walk cannot fill at t or at t+0.2 s is dropped and counted.
   Factors: (a) `gap_blocks` = status GAP and depth_to_first_gap < Q (two buckets: blocks / does not); (b) flicker buckets: F0 = 0, F1 = positive and at or below the TRAIN median of the positive values, F2 = above it.
2. MARKOUT PROXY FOR A MAKER AT THE TOUCH (maker venue Bybit). Every public print that trades at the touch (price equal to the best level of the side it depletes). Outcome = the passive side's markout at +5 s in bps
   from the print price to the mid, positive = favourable. This is a proxy for a resting order filled at that print, NOT our fills and not a fill probability.
   Factors: (a) own-side depth_to_first_gap buckets: NONE (no gap in the captured depth), FAR (GAP, depth at or above the TRAIN median of GAP depths), NEAR (below it); (b) own-side flicker buckets as above.

## Split and statistics
- Chronological: first half of the capture's time span = TRAIN (bucket edges are computed here), second half = HOLDOUT. One day only, so there is no day-level replication: that limit is stated in the report.
- A bucket needs at least 200 samples in each half, otherwise the factor is NOT TESTABLE for that half.
- Mean and 95% interval of the HOLDOUT difference between the extreme buckets from a block bootstrap over 5-minute blocks (2,000 resamples, fixed seed), because samples on a 1 s grid overlap in time.

## Keep rule (all must hold, per factor, per outcome, per size Q)
1. Monotonic: the bucket means are ordered the same way, low to high, in TRAIN and in HOLDOUT (two-bucket factors: the same sign).
2. Size: the extreme-bucket difference is at least 0.3 bps in HOLDOUT, and in the direction the factor claims (more gap risk or more flicker = worse for the hedge, worse markout for the maker).
3. The holdout 95% interval excludes 0.
A factor that fails any condition for every Q (or for the markout) is NOT KEPT. A factor kept for one Q only is reported as kept for that Q only. No HHI panel; venue choice uses depth at size.
Nothing is fitted; the numbers above are fixed before the run. Label of the result: EXPLORATORY.
