# Large-size gaps and the mainnet probe: rules (owner order 15 Nov, "Large-size gaps + probe"; written BEFORE any number of this order exists)

Everything is EXPLORATORY and PROXY (or MODELLED where the overlay is on). Core changes are OFF by default. One heavy job at a time. A report section is written after each step, before the next starts.

## Discipline carried over
- Windows: March 2024 00:00-02:00 UTC (the probe window), TRAIN 03-01..07, SELECT 03-08..12, HOLDOUT 03-13..15 NOT READ in this order (the G T5 HOLDOUT figures stay as they are, labelled upper bound). The 2026-10-29 capture (both venues' L2) is the large-size data; its FIRST half builds samples and chooses, its SECOND half answers (chronological halves, as in follow-up 1c).
- Block bootstrap everywhere: 10-minute blocks, 2,000 resamples, seed 1; "a gain smaller than its interval = NO EFFECT". The perp queue model is unvalidated.
- Never train a model on the simulator's own fills. Fill-moment statistics (steps 3, 5) are ALSO computed at moments taken from market data alone (random prints; prints right after a sharp move of the venue's own mid), and the value that feeds a parameter (lambda_u) is named, with its source, and chosen on SELECT.
- No synthetic depth for any profit figure. Binance has depth on the capture only; March hedge-size tests use Bybit.
- Backups to `crypto_MFT_arb_platform/_backups/large_gaps_<ts>/` before the first edit of each file. No new source file: the code goes into `simulation/impact.py`, `ems/tactics/base.py`, `new_backtest/*` and the existing cost scripts.
- Gates after each core change: the touched test files; `tests/` pinned hashes (v1_2 freeze, legacy_v0, power13 golden); the full suite against the Phase 0 list (scripts/compare_baselines.py); for a change that touches the replay, the v1_9 1 SOL full-day identity run (one heavy job at a time).

## 1. Fill-range rule (CR-LQ, optional, off by default)
- ONE implementation, in `simulation/impact.py` level 1 (`DisplayedDepthImpact`): `fill_range(levels_in_cap, survival, own_takes, now, tau)` returns the available size at survival multipliers 0.8 / 1.0 / min(1, 1.2): available_m = sum(displayed x min(1, survival x m)) - own_unrefilled, own_unrefilled = sum(take_i x exp(-(now - t_i) / tau)) (refill = 1 - exp(-t/tau)), floored at 0.
- Expected fill = min(Q, available at 1.0). A market-style SWEEP = `IOC_CAPPED / LQ_DEPTH_COVERS`, only if the PESSIMISTIC case (0.8) covers Q. Otherwise `IOC_CAPPED` for the pessimistic amount with the rest as the carry (`residual_qty`), reason `LQ_PARTIAL_RESIDUAL`; nothing covered: PASSIVE. MARKET_BANDED stays emergency-only (MARKET_BANDED_REASONS, owner 9 Nov); no soft rule reaches it.
- Switch: `TacticChoiceConfig.fill_range` (default False) with `fill_range_low = 0.8`, `fill_range_high = 1.2`, `own_refill_tau_s`. With it on, the flicker discount of gap_aware is NOT applied (the range replaces it); the first-gap cap still applies. The MarketState carries the inputs (`displayed_depth_in_cap`, `survival_mean`, `own_unrefilled_qty`); None = unknown = no change.
- The arb hedge dispatch does not call choose_tactic: this rule does not reach arb hedges. Said in the report.
- Tests: hand-calculated values; off = identical answers to today's for every existing test input.

## 2. Own consumption and refill in the replay (extends the own_impact switch; no second mechanism)
- `own_impact` gets a third value, `refill`: after our aggressive fill the taken size is removed at the levels it took; the removed part comes back as 1 - exp(-t/tau) (tau from `impact.own_refill_tau_s`, a fitted/ASSUMED setting; the isolated-run fit gives 3.5 s, ASSUMED). The public update of a level does NOT reset the ledger (that is the difference from `overlay`). Later orders of ours see the reduced book: it is applied to the walk's COPY of the core book (the one the taker walk reads) and to the view.
- Tests: off = byte-identical goldens (the v1_9 1 SOL identity run); one synthetic order removes exactly its size and refills on the configured curve; two orders in a row cannot both take the same liquidity (checked at the walk, where the recorded replay reads `session.books`).
- `_impact_sync` is defined twice in `new_backtest/session_replay.py` (the second wins): reported, NOT changed in this order.

## 3. Worst-case cost of waiting
- Moments: (a) the probe's simulated maker-fill times (qv2_probe fills, TRAIN 2,035 and SELECT 692; labelled SIMULATED), (b) random prints, (c) prints right after a sharp move (the venue's own mid moved at least 3 bps in the previous 1 s, ASSUMED threshold). For each moment and horizon h in {0.1, 0.5, 1, 2, 5} s: the adverse move (side-signed, for a hedge that is still unhedged: the hedge-venue mid moves AGAINST the hedge), per venue (Binance, Bybit). Mean, p95, p99, CVaR(5%) = mean of the worst 5%, with block-bootstrap intervals.
- lambda_u for Cont and Kukanov: CVaR(5%) over the horizon T of the split (T = 1 s chosen on SELECT earlier), next to the old sigma_1s x sqrt(T). lambda_u is set from moments (b)+(c) (market data alone); the fill-moment value (a) is reported beside it, not used to set anything. SELECT chooses which of {sigma sqrt(T), CVaR from (b), CVaR from (b)+(c)} by the highest net at weight 1 for 10 SOL; the answer is the second half of the capture and, for March, SELECT-days-only comparison (HOLDOUT untouched).

## 4. Large-size tests (capture both venues + Bybit March), overlay OFF and ON
- (a) T1 at f = 0.25 / 0.5 / 1 / 2 / 4 of D10 (the follow-up grid), unchanged method, rerun not needed for OFF (taken from t1b / the capture runs); ON = the prediction is made on the overlay-shifted book at the decision (prior own orders only; T1's hypothetical orders are single, so ON equals OFF at the first order: stated, and the sequences below are where the overlay acts).
- (b) Repeated hedges every 1 / 5 / 10 s (K = 10), own consumption OFF vs ON (the ledger of step 2, tau 3.5 s ASSUMED), overlay OFF vs ON (the core `ImpactOverlay` imported, not re-coded). Cost per unit by hedge size (f 0.1 / 0.5 / 1.0 of D10) AND by spacing.
- (c) Hedge modes at 100 / 500 / 1000 SOL on the capture's real Binance depth: all-at-once sweep (M); time-sliced (N slices over T, Almgren-Chriss sinh trajectory, N in {2, 5, 10}, T in {5, 30} s, each slice sized by rule 1: expected fill = min(slice, available), the carry goes to the next slice, the last slice sweeps); Cont-Kukanov split (E, price-aware fills). Cost per unit, unhedged time (volume-weighted seconds), worst-case adverse drift over the hedge time (p99 and CVaR(5%) of the side-signed mid move over the unhedged time).
- (d) Overlay = reaction model, labelled MODELLED: default = `impact_overlay_settings.json` (isolated runs: Y 0.029, delta 0.30, phi 1.0, tau 3.5 s, cross 0.78 / 0.22), UPPER BOUND = `impact_overlay_settings_upper.json` (all runs); cross shift ON for both venues; parameters with their intervals from impact_fit_follow.json. Probe comparison: NOT POSSIBLE (the probe fills are simulated and passive; no real aggressive fill exists with a book to fit against): said, not done.

## 5. Resting-hedge realism (per size 1 / 10 / 100 / 500 / 1000 SOL)
- Hedge moments from simulated maker-fill times (SELECT chooses what it chooses; moments (a) of step 3) and, to keep the control, random moments. Fill rules for the rested part: PRICE-AWARE (the follow-up rule) and PESSIMISTIC (a third fill mode: only prints STRICTLY BEYOND the rest price fill; no credit for the queue ahead clearing, no credit for prints at the price), as a floor. 500 and 1000 SOL need Binance depth: capture only.
- Per size and rule: fill rate of the rested part, hedge cost, unhedged time, net at risk weights 0 / 0.5 / 1 x sigma, and the CVaR-based version (net = -cost - CVaR(5%) over the unhedged time x weight).

## 6. Thinner books (ADA, MNT Bybit perp April; Binance ADA/MNT depth confirmed absent in the follow-up) - new splits only
- Survival, cancel-to-trade, cliff-flag 2/3/5 bps (3 ASSUMED) already reported; new: T1 prediction error split by (i) volatile vs normal hours (done), (ii) one-sided trade flow (the signed volume imbalance of the 10 s before the decision in the top and bottom quartile of the coin's own first-half), (iii) orders within 1 s after a maker fill vs others: maker fills exist only in the SOL March probe, so ADA and MNT are N/A for (iii) (no simulated or real maker fills for them). The ADA flag defect (4.1 bps tick above the 3 bps stretch limit) is repeated, not fixed in this order.

## 7. Probe shortfall (pending follow-up)
- Already built: `probe_shortfall/` (TRAIN 2,035 and SELECT 692 fills, per-day and pooled, burst-block and day-block intervals, HOLDOUT untouched). Only a check that the 24-fill and 115-fill results are marked superseded everywhere.

## 8. Framing caveat
- "With ~100 ms books the T1 outcome 0.1 s later is often the same or the next snapshot, so the small error is partly by construction; T1 does not test others' reaction to our order or sub-100 ms book changes": attached at every place a T1 number is quoted (G_RESULTS, the plan, the contract).

## 9. Fast-stream backups
- Look in LOCAL backups only (data/, _backups/, out/, ~/Downloads); list, do not restore; the VM is not checked (key refused before).

## 10. Mainnet probe
- The existing ollog mainnet module is extended (no new runner). Limits: SOL/ADA/MNT on Binance and Bybit perps; PostOnly at the touch and one tick back, minimum size; cancels after a few seconds and amends on a share; at most 2 open per coin and venue; mostly random times plus some right after sharp moves; inventory cap 200 USD per coin and venue (over it: stop and a PostOnly unwind; a taker unwind only AT the cap, counted in the loss cap); loss cap 20 USD per day and 200 USD in total (replaces the 300 USDT suggestion); kill switch on feed UNSAFE, unknown order state or a manual stop; 1 to 2 weeks; own lifecycle log at send time including never-filled; recorder with fast streams passing feed health; keys from environment only; order data local; no order IDs in reports.
- Run order: unit tests, paper, ONE hour on testnet (where a market exists; paper where it does not), then STOP and show the plan and the results to the owner. Mainnet orders are launched by the owner's typed confirm, never by me. No recorder change on the VM without a separate approval.
