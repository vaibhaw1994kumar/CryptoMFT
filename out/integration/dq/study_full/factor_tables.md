# Depth-quality factors: tables (EXPLORATORY: ONE DAY, PROXY (hypothetical hedge walks and public prints; no real fill, no simulator fill))

Train 7.0 h, holdout 7.0 h (chronological halves of one day). Keep rule in FACTOR_STUDY_RULES.md; fixed before the run.

## hedge slippage change, Q=5: gap_blocks  ->  NOT KEPT
Outcome: dS(Q=5) = S(t+0.2s) - S(t), bps, positive = worse

| bucket | train n | train mean | holdout n | holdout mean |
|---|---|---|---|---|
| clear | 50511 | 0.000 | 50241 | -0.000 |
| blocks | 0 | - | 0 | - |

not testable (a bucket under 200 samples in a half) or not monotonic in both halves

## hedge slippage change, Q=5: flicker  ->  NOT KEPT
Outcome: dS(Q=5) = S(t+0.2s) - S(t), bps, positive = worse

| bucket | train n | train mean | holdout n | holdout mean |
|---|---|---|---|---|
| F0 | 0 | - | 0 | - |
| F1 | 25256 | 0.000 | 35959 | -0.000 |
| F2 | 25255 | 0.000 | 14282 | -0.000 |

not testable (a bucket under 200 samples in a half) or not monotonic in both halves

## hedge slippage change, Q=25: gap_blocks  ->  NOT KEPT
Outcome: dS(Q=25) = S(t+0.2s) - S(t), bps, positive = worse

| bucket | train n | train mean | holdout n | holdout mean |
|---|---|---|---|---|
| clear | 50511 | 0.000 | 50241 | 0.000 |
| blocks | 0 | - | 0 | - |

not testable (a bucket under 200 samples in a half) or not monotonic in both halves

## hedge slippage change, Q=25: flicker  ->  NOT KEPT
Outcome: dS(Q=25) = S(t+0.2s) - S(t), bps, positive = worse

| bucket | train n | train mean | holdout n | holdout mean |
|---|---|---|---|---|
| F0 | 0 | - | 0 | - |
| F1 | 25256 | 0.000 | 35959 | 0.000 |
| F2 | 25255 | -0.000 | 14282 | -0.000 |

not testable (a bucket under 200 samples in a half) or not monotonic in both halves

## hedge slippage change, Q=100: gap_blocks  ->  NOT KEPT
Outcome: dS(Q=100) = S(t+0.2s) - S(t), bps, positive = worse

| bucket | train n | train mean | holdout n | holdout mean |
|---|---|---|---|---|
| clear | 50511 | 0.000 | 50241 | -0.000 |
| blocks | 0 | - | 0 | - |

not testable (a bucket under 200 samples in a half) or not monotonic in both halves

## hedge slippage change, Q=100: flicker  ->  NOT KEPT
Outcome: dS(Q=100) = S(t+0.2s) - S(t), bps, positive = worse

| bucket | train n | train mean | holdout n | holdout mean |
|---|---|---|---|---|
| F0 | 0 | - | 0 | - |
| F1 | 25256 | 0.000 | 35959 | 0.000 |
| F2 | 25255 | -0.000 | 14282 | -0.001 |

not testable (a bucket under 200 samples in a half) or not monotonic in both halves

## maker markout +5 s at the touch: own-side depth_to_first_gap  ->  NOT KEPT
Outcome: markout of the passive side at +5 s, bps, positive = favourable (proxy: public prints at the touch)

| bucket | train n | train mean | holdout n | holdout mean |
|---|---|---|---|---|
| NONE | 96701 | -0.477 | 198371 | -0.622 |
| FAR | 0 | - | 0 | - |
| NEAR | 0 | - | 0 | - |

not testable (a bucket under 200 samples in a half) or not monotonic in both halves

## maker markout +5 s at the touch: own-side flicker  ->  NOT KEPT
Outcome: markout of the passive side at +5 s, bps, positive = favourable (proxy: public prints at the touch)

| bucket | train n | train mean | holdout n | holdout mean |
|---|---|---|---|---|
| F0 | 0 | - | 0 | - |
| F1 | 48351 | -0.509 | 145629 | -0.696 |
| F2 | 48350 | -0.445 | 52770 | -0.422 |

not testable (a bucket under 200 samples in a half) or not monotonic in both halves
