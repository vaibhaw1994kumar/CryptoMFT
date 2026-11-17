# 2i item 10: the denser arb case on v1_10 (build session, 17 Nov 2026)

EXPLORATORY: mechanics only, 2-hour window 11:00-13:00 UTC of the 29 Oct capture, 1 SOL clip, singapore latency, hedge minimum 5 USDT, on the code with the 2i fixes 1-8 (core c27a463).
INJECTED (not market events): the Binance book shifted 2 bps (two ticks) every 20 s for 8 s. Case settings (labelled arb_overrides): entry threshold -2.0 bps, participation limit off,
validity tolerance 2.5 bps, and the pair-close switch ON (arb.close_pairs reduce_only_quote, NOT in v1_10).

Why the pair-close switch: without it the 5 SOL inventory fills within minutes and then nothing rests, so the injected moves meet no quote (10-minute probes: 1, 1, 0 and 1 amend with a
one-tick-inside placement, a larger and more frequent move, a lower threshold, an 8 bp move; the arb prices its quote by the edge rule, so placement does not move it). With it, an open pair keeps
a closing quote resting all day.

| Variant | Amends requested | Confirmed | Rejected (reasons) | Timed out (lost) | Owed queries (answered) | Fills during a pending amend | Booked twice | Lineage overfill | Two live orders |
|---|---|---|---|---|---|---|---|---|---|
| dense | 10 | 4 | 6 (replace target is not open: 6) | 0 | 0 | 7 | 0 | 0 | 0 |
| reject (every amend) | 10 | 0 | 10 (injected 4, not open 6) | 0 | 0 | 7 | 0 | 0 | 0 |
| timeout (every amend lost) | 10 | 0 | 6 (not open 6) | 4 (4 lost) | 4 (4) | 15 | 0 | 0 | 0 |
| disconnect (a 30 s gateway outage opens as each amend arrives) | 1 | 0 | 0 | 1 | 1 (1) | 0 | 0 | 0 | 0 |
| duplicate Filled (every Filled report twice) | 10 | 4 | 6 (not open 6) | 0 | 0 | 7 | 0 | 0 | 0 |

Case 2i is COVERED on real amends: 10 per run (was 1), every outcome type present (confirmed, rejected by the venue, rejected by injection, lost and timed out with the owed query answered,
duplicate Filled across amends, a disconnect over an amend followed by the query after the reconnect).

Read with care:
- "replace target is not open" (6 in every variant): the amend reached the venue after the quote had filled or been cancelled (fills and cancels during the pending amend in the same runs); the venue
  rejects an amend of an order it no longer has, and the old order is queried. In the timeout variant these 6 were not caught by the injected loss: they did not reach the venue's amend handler
  (the target was no longer one of the venue's resting orders). Not traced row by row (the scenario writes aggregates, not the ledger).
- Disconnect: the first amend's outage changed the rest of the day (the quote it hit was lost to the outage), so this variant has ONE amend. It is the case the 17 Nov report could not show: the
  timeout is followed by the venue query after the reconnect.
- The audit's "hedge filled" counts HEDGE orders only; the maker fills it does not match were closed by unwind orders. End positions: Bybit +2.7649, Binance -2.76 (a 0.0049 SOL dust residual, below the 0.01 lot).
- `two_live_orders` is computed with the in-place amend reading (a REPLACED report ends the old order at that instant): the venue never held two orders.

Item 9 (MARGIN_STATE_TRANSITION 4 vs 3 in the MM g1s golden): NOT EXPLAINED. The golden is not in this working copy, nor in the outputs or branches of the MM worktrees on this Mac
(`mm_wt`, `strategy_mm_wt`), and no MM session runs on this Mac to ask.
Item 6 timing: 1 July ADA NOT RUN (not in this tree); 29 Oct NOT MEASURED (a before/after pair of full days costs more than 15 minutes).
Full suite after the hash-test update: UNCHANGED at 93 (3,909 passed), `compare_baselines_2i_rerun.txt`.
