# Phase 2 results: time source and size (F03, F02), the F06 change, G1 for Phase 1

Owner order of 20 Nov 2026. The difference report was written before any change (`DIFFERENCES_FROM_ORDER.md`); the owner answered "use the proposals".

Profile: `perp_perp_maker_taker_v1_17`, NOT FROZEN, sha256 `08199ed5900ad785ccbeb7a19f391632ed07278adfddf8155da13a0121b8f965`. It is listed in GATES.md. v1_9 to v1_16 are unchanged.

Commits:
- core `776c2a9`
- outer `a415bbd`, plus the results commit
- nothing pushed

Base code of every Phase 2 run (the coordinator's condition 1 for the parallel N order):
- G3 `g3_p2`, G4 `g4_v117_off`, `f06_close_on_v117` and rungs i_01k / i_05k / i_10k: core 776c2a9's code before the open-item age fix (a reporting line only; the fix went into 776c2a9 itself).
- `f06_close_on_v117b` and rungs i_20k / i_30k / i_50k: core 776c2a9 + outer a415bbd, committed.
- The N-order commits (core 027c111 and later) came after every Phase 2 run had started; a running process had already imported its code.

Every P&L figure is EXPLORATORY:
- data: the 29 Oct 2026 capture, which is REJECTED / CRITICAL and waived for mechanics only;
- latency: the `singapore` preset, ASSUMED and pre-migration;
- fees: the profile's;
- size: as stated per run.

## G1 for Phase 1 (first, as ordered)

Each of the four extra failures was run alone, serially, three times (`g1_alone.tsv`). All 12 runs PASS:

| Test | Runs passed |
|---|---|
| `test_minreq_execution` [Darwin-True] | 3/3 |
| `test_minreq_execution` [Linux-False] | 3/3 |
| `test_minreq_execution` [Windows-True] | 3/3 |
| `test_live_feed_a9` ArrivalStamp | 3/3 |

None is a new failure.

The full suite was then run serially once, with nothing else running, against the 93 baseline (`g1_compare_baselines_serial.txt`). Result: 93 failing (92 failed plus 1 error), 3997 passed, in 943 s. No new failure, none fixed: UNCHANGED.

## F06 change: the owner's decision on the latch

**Built behind `hedge_faults.dust_bound_rule`.**
- `latch` is the default and keeps the Phase 1 rule, so v1_16 is unchanged.
- v1_17 sets `aggregate`.

**What `aggregate` does:**
1. **Age bound.** The age bound is an alarm only. A balance raises ONE `DUST_AGE_REACHED` and stops nothing. The balance appears in the end-of-day report (`hedge_dust_carry.bound`) and in the operator summary (`paper_summary.operator_open_items`).
2. **Notional bound.** Only the aggregate notional bound stops new risk. It is measured per direction over every venue and symbol, with the same instrument netted across venues.
   - While it is active, the strategy is still asked. Its opportunity goes out only when the maker fill's hedge REDUCES the carried dust. Long dust owes a SELL hedge, so a maker BUY passes.
   - At the bound, a resting dust-reducing quote is kept. Other resting opening quotes are withdrawn once.
   - It clears by itself, with one event per state change.
3. **End of session.** Every carried balance is an open item: venue, symbol, signed base, USDT and age. No automatic order is sent.

**Tests:**
- `DustBoundAggregate`:
  - an aged 1 USDT balance does not block (one alarm, an open item);
  - above the bound only dust-reducing quotes pass; the resting reducing quote is kept; it clears by itself;
  - a resting quote that would add to the dust is withdrawn;
  - the rule key is parsed.
- `F06AggregateDustLetsOnlyReducingQuotesThrough`: the session path, long and short.
- Switch off: G4 below.

**The pair-close day rerun** (`f06_pair_close_rerun.sh`): v1_17 with the same overrides as Phase 1's `f05_close_on`, run `f06_close_on_v117b`. Against the first rerun (`f06_close_on_v117`, made before the age fix) only `session_limits_and_stops.json` differs (the open item's age).
- Signals blocked by the dust rule: **0** (Phase 1: 370,535). Opportunities blocked 0, loop passes while active 0. The notional bound was never reached: the largest aggregate dust was **0.978 USDT**, against the 50 USDT bound.
- Dust alarms:
  - one `DUST_AGE_REACHED`, about 300 s after the 0.01 SOL balance was carried at 21:49:01 UTC;
  - one `HEDGE_DUST_ALARM` at 60 s;
  - no `DUST_BOUND_REACHED`.
- End balance: 0.01 SOL long carried on Binance, about 0.956 USDT, listed as an open item with age **7,859.8 s** (2 h 11 min: carried 21:49:01 UTC, measured at the session end).
- What the 370,535 was: the Phase 1 figure counted strategy-loop passes after 21:54. Unblocked, the strategy found NO signal after 21:49 on this day.
  - The signals (70), orders (91) and fills are byte-identical to the Phase 1 run.
  - Only the dust report, the ledger counts and the safety-event ids differ (`compare_sessions`).
  - So on this day the latch cost no trade; it would have cost trades on a day with edge after the carry.
- Found and fixed during the rerun: the open item's age was first measured at the last execution (0.05 s) instead of the session end. It is now measured at the last event. This is a reporting change only, made after G3 and G4: G3 has no dust bound, and G4 runs with the bound off.

## F03: production time source

**Offset limit.**
- `clock_offset.max_offset_ms` is ASSUMED 5.0 in v1_17; the owner sets it.
- The offset and the uncertainty are checked separately, with a new state `offset`. The offset is checked first, because the chrony uncertainty contains |offset|.
- Both numbers are recorded with every evaluation:
  - per-state counters with the largest |offset| and uncertainty;
  - the newest values in `last`;
  - both on every `LIMIT_STATE`;
  - one `CLOCK_MEASUREMENT` row per chrony reading.
- With the host-offset correction on (every session profile), the limit applies to the residual after it, and the limits report says so.
- Tests (`F03ClockOffsetLimit`):
  - tight measurement, 20 ms offset: `offset`;
  - 1 ms offset, 20 ms uncertainty: `uncertain`;
  - offset wins when both exceed;
  - the residual is used with the correction;
  - A3 is unchanged without the key;
  - an invalidated source gives `unknown`;
  - a session-clock gap counts as offset.

**a. Chrony source.**
- `parse_chronyc_tracking`, `chrony_measurement` and `read_chrony` in `time_integrity.py`.
- Offset = local minus NTP (fast = +). Uncertainty = |system offset| + root dispersion + root delay / 2. The measurement is taken at chrony's Ref time.
- The 5 ms and 900 s limits stay.
- Venue server time is a cross-check only: one `CLOCK_CROSS_CHECK_DISAGREE` per episode beyond the probe's own uncertainty.
- Tests: `F03Chrony` and `F03PaperChrony`.
- The parser follows the documented format: UNVERIFIED until hostmeasure runs on the production host. This Mac's chronyc does not start (a missing library), which is a real case of the "absent" path.

**b. Monotonic clock.**
- `MonotonicSessionClock` covers the live paper/live path only; replays are unchanged.
- Intervals and deadlines use the monotonic clock. A wall step above 50 ms (ASSUMED) is recorded once as `WALL_CLOCK_STEP`.
- The gap is added to the gate's offset.
- Tests: `F03MonotonicSessionClock`.

**c. No fallback.** If chrony is unsynchronised, absent or failing, the gate is invalidated at once ("unknown") and quoting stops. Server time never replaces chrony. A laptop does not pass.

**d. Measurement script.**
- `python3.12 -m ollog hostmeasure --hours 24` records the following on the same clocks:
  - chrony every 60 s (raw);
  - REST round trips per venue;
  - the public book, trade and top-of-book capture.
- `ollog hostmeasure-derive` computes:
  - chrony figures;
  - REST round trip and the venue clock against the host and NTP;
  - websocket latency per venue and stream, raw and corrected by chrony;
  - book publication interval;
  - cross-venue lead-lag of the top-of-book mid.
- PUBLIC mainnet data only: no keys, no orders. It has NOT been run here; you run it on the production host.
- Tests: `order_lifecycle_logger/tests/test_hostmeasure.py`, 3, offline.
- Until it runs, the latency figures stay ASSUMED and are labelled pre-migration in v1_17.
- Unrelated pre-existing failures: 2 ollog `test_replay` tests fail on a backtester quality preflight (INSUFFICIENT_EVENTS for bybit/ETHUSDT 2026-10-26). They have nothing to do with this change.

**e. alignment.py.**
- It is not a time source:
  - it merges per-venue streams by `receive_ts_ns`, then exchange time, source time, venue, sequence and event id (`ORDERING_RULE`);
  - it judges book age as now minus the last receive time;
  - it never reads a clock and never rewrites a venue timestamp.
- With the monotonic-anchored session clock, the receive stamps stay on one timeline, so it needs no change.

## F02: size envelope, size ladder, capital

**a. Envelope** (`f02/ENVELOPE.md`, `f02/envelope.json`).
- One row per symbol for 30 symbols: the 22 spot symbols plus LDO LINK LIT TON TRX VVV WLD ONDO. The owner names the remaining 10 to 15.
- Every cell is labelled ASSUMED, MEASURED, APPROVED or NO DATA.
- Venue filters come from one public read of 20 Nov (with your yes):
  - Binance exchangeInfo: limit and market maxima, min notional, open-order limit;
  - Bybit instruments-info: post-only and market maxima;
  - Bybit public risk-limit tiers, giving initial and maintenance margin at 20k / 30k / 50k with the account leverage at 10 (ASSUMED).
  - The read was made with `curl` (TLS verified), because this Mac's Python certificate store does not hold the CA of the HTTPS path. The raw dump is kept locally under `config/contract_master/raw/20261120T083931Z/` and not committed (7 MB).
- Binance tiers need a signed call: NO DATA, except SOL, which uses the ASSUMED v1_17 table.
- Depth and exit cells exist for SOLUSDT only; everything else is NO DATA, with nothing extrapolated.
- Listings: MNTUSDT is not a Binance USD-M perpetual, and TONUSDT is listed on neither venue (NO DATA).
- Found:
  - Bybit's public SOL tier 1 is 50k at 1% initial / 0.5% maintenance, while the v1_17 ASSUMED table is 2% / 1%.
  - Several symbols have different lot steps on the two venues (AVAX 0.1 vs 1, AAVE 0.01 vs 0.1, and others), which is a dust source.

**b. Size ladder: series i only** (owner follow-up: "cut the ladder to series i only"). See `f02/LADDER.md` and `f02/ladder_summary.json`.

Verdict rule: a rung is FAILED if any of the following happened, or a plan left more than 0.06 SOL (about one Binance minimum) unhedged, judged per plan from the fills:
- a kill switch;
- a residual breach;
- an unwind;
- the dust bound reached.

Capacity limits followed the rung: the parent (net ceiling), gross per venue at 2x the parent, and capital per venue. Risk limits stayed at their v1_17 values: order rate 2/s burst 5, the hedge chain, the dust bound, the margin multiple, missing-mark kill, the clock gate and participation.

| Rung | Parent SOL | Child SOL | Verdict | Maker fills (buy/sell) | Pair net bps p10 / p50 | Hedge cost bps p50 / p90 | Fully hedged | Unhedged s p50 / p99 | Bursts (longest) | Peak net SOL | Peak notional / IM per venue | Max orders 1 s | Closing signals | Bound / failed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| i_01k | 10.2 | 10.2 | PASS | 24 (15/9) | 1.4553 / 3.3768 | 0.0 / 2.0786 | 0.9167 | 0.0386 / 0.0494 | 17 (9) | 10.2 | BINANCE: 1983 / 39.66, BYBIT: 1981 / 39.62 | 3 | 19 | - |
| i_05k | 51.0 | 51.0 | PASS | 80 (67/13) | 1.4553 / 3.4731 | -0.0 / 0.0 | 0.9375 | 0.0398 / 0.0646 | 68 (20) | 51.0 | BINANCE: 9925 / 198.49, BYBIT: 9915 / 198.31 | 5 | 45 | - |
| i_10k | 102.0 | 102.0 | PASS | 111 (100/11) | -4.7651 / 3.4731 | -0.0 / 7.2254 | 0.9459 | 0.0393 / 0.0646 | 97 (29) | 102.0 | BINANCE: 19846 / 396.91, BYBIT: 19829 / 396.59 | 5 | 73 | - |
| i_20k | 204.1 | 204.1 | PASS | 176 (165/11) | 1.4082 / 5.5248 | -2.0553 / 1.0267 | 0.9489 | 0.0398 / 0.0646 | 158 (29) | 109.0 | BINANCE: 39755 / 795.09, BYBIT: 39738 / 794.77 | 5 | 71 | - |
| i_30k | 306.1 | 255.1 | PASS | 239 (228/11) | 1.4082 / 3.4718 | -1.0125 / 1.0267 | 0.9456 | 0.0394 / 0.064 | 216 (29) | 109.0 | BINANCE: 59592 / 2979.58, BYBIT: 59555 / 2977.74 | 5 | 72 | - |
| i_50k | 510.2 | 255.1 | PASS | 306 (295/11) | 1.4381 / 3.4667 | -0.0 / 1.0267 | 0.9477 | 0.0395 / 0.064 | 279 (40) | 139.5 | BINANCE: 99346 / 4967.31, BYBIT: 99295 / 4964.76 | 5 | 72 | - |


**Result: all six rungs PASS.**
- No kill switch, no residual breach, no unwind, no dust bound.
- Every plan is hedged within dust.
- All closing reservations were released, and nothing went beyond flat.

**Correction made while writing this:** a first verdict pass used the pair-economics `hedged_qty` and reported 20k / 30k / 50k as FAILED. That figure misses a hedge that also took in carried dust: on i_20k, a 35.01 SOL maker fill was hedged by a 35.02 SOL sell 37 ms later but read `hedged_qty` 0. The verdict now compares maker against hedge fills per plan from `session_fills`.

What the table says, EXPLORATORY:
- **Cost.**
  - The hedge cost against the hedge-venue touch at the decision is p50 about 0 bps and p90 at most 7.2 bps (10k); 20k to 50k are at about 1 bp.
  - Pair net p50 is 3.4 to 5.5 bps.
  - The Binance book on this day absorbs these sizes at the touch or one tick. The capture is REJECTED, so this is mechanics, not evidence of cost at size.
- **Completion.**
  - The unhedged interval is p50 about 39 ms and p99 at most 65 ms on every rung.
  - The fully-hedged share per maker fill is 0.92 to 0.95: the remainder is carried dust and lot remainders, later included.
- **Burst fills.**
  - Up to 279 maker fills arrived within 50 ms of the previous one; the longest burst was 40.
  - The simulator awards large maker fills from single prints (for example 35.01 SOL at once), which is the queue model's trade share on big prints.
- **Residual and margin.**
  - Peak net is 10.2 / 51 / 102 / 109 / 109 / 139.5 SOL, the maker size open during the hedge interval.
  - End positions equal the gross per-venue cap (2x the parent) on every rung, with net 0. The book is long Bybit and short Binance, and it filled up to its cap: the fills are skewed to maker BUYs (165 of 176 on 20k), and pair close only closes on the edge trigger.
  - Peak initial margin per venue, at the ASSUMED tiers, is 40 / 198 / 397 / 795 / 2,980 / 4,967 USDT.
- **Throughput.**
  - At most 5 maker orders ended in any 1 s.
  - The A1 order rate skipped 4 to 12 quotes on 5k and above. It bound and was kept.
  - The legging limit bound 7 to 27 times, and package dispatch was throttled 72 to 139 times.
- **Exits.** 19 to 73 closing signals; all reservations released; CLOSE_UNFILLED 0.

**Not covered:**
- Series ii: dropped at your request.
- Child sizes above 25k USDT: the 25k clip cap was kept, not raised.
- Both directions only as the day produced them, skewed to buys.
- A size where the hedge would walk past the captured Binance depth: not reached on this day.

**c. Capital.**
- `margin_warning.stressed_move_bps` 190 (ASSUMED: the largest 60 s trade move of 29 Oct, 188.9 bps Bybit / 183.5 Binance; `f02/max_move_60s.json`).
- No new entry unless EACH venue funds, without a transfer:
  - the initial margin of the hard cap;
  - plus every resting maker quote filling;
  - plus their exit at that move.
- The need per venue is in the limits report.
- Tests: `F02StressedHeadroom`.

Capital each rung implies per venue, at the ASSUMED tiers (`f02/capital_by_rung.json`). The rule is the larger of:
- the initial margin used at the peak plus the A4 free margin (10 x the initial margin at the hard cap, where the hard cap = 2x the parent);
- the stressed need.

| Rung | Hard cap, SOL | Capital per venue, USDT | Both venues, USDT | Set in the run, per venue |
|---|---|---|---|---|
| 1k | 20.4 | 437 | 873 | 1,000 |
| 5k | 102 | 2,183 | 4,367 | 3,000 |
| 10k | 204 | 4,367 | 8,734 | 6,000 |
| 20k | 408 | 8,739 | 17,477 | 12,000 |
| 30k | 612 | 32,763 | 65,526 | 45,000 |
| 50k | 1,020 | 54,610 | 109,220 | 75,000 |

- Above 50k notional per venue, the ASSUMED 5% tier makes the A4 multiple dominate.
- Bybit's public SOL tiers are lower: 1% initial and 0.5% maintenance up to 50k.
- At the owner's target of 20k to 30k parents on 40 to 45 symbols, about 17k to 66k USDT per symbol on two venues, the envelope implies roughly 0.7M to 3M USDT. That is SOL's figure multiplied by the symbol count, a SCALE INDICATION ONLY. Every other symbol is NO DATA for depth and Binance tiers, and nothing is extrapolated into its row.
- No stop fired for capital or stressed headroom on any rung: the capital set was above the need.

## Phase gates

- **G1 Phase 2:** the full suite on the Phase 2 code (`g1p2_compare_early.txt`) gave 93 failing, equal to the baseline: UNCHANGED, 4035 passed (+38 new tests).
  - It ran with replays alongside, and the four timing tests passed there too.
  - The one change after it (the open-item age, reporting only) was checked with its two test files (161 passed).
  - The second serial run was skipped at your request to save time.
- **G2:** v1_17 is listed NOT_FROZEN with its hash; `compare_baselines.py hashes` gives G0 PASS (pass 30, fail 0).
- **G3 v1_9 identity:** 14 of 14 files identical (`g3_p2.json`).
- **G4:** v1_17 with every Phase 1 and Phase 2 switch off, fresh on the full 29 Oct day, against Phase 1's v1_15 run (`g4_v117_vs_v115.json`):
  - 16 of 17 files identical;
  - `session_manifest.json` differs only in the profile name, checked field by field.

## Per item

- **G1 for Phase 1: DONE.** 12/12 PASS alone; serial suite 93 = baseline.
- **F06 change: DONE.**
  - Built behind the `dust_bound_rule` switch; v1_16 is unchanged.
  - Rerun: 0 blocked, 1 age alarm, largest aggregate 0.978 USDT, end balance 0.01 SOL with an open item at 7,859.8 s.
- **F03: built and tested; NOT RUN on the production host.**
  - Built: the offset limit, chrony, the monotonic clock, no fallback, the hostmeasure script, and the alignment report.
  - The chrony parse is UNVERIFIED until you run hostmeasure there.
  - max_offset_ms is ASSUMED at 5 until you set it.
- **F02:**
  - a: DONE for 30 symbols, with the venue filters and Bybit tiers MEASURED. Binance tiers and depth are NO DATA, except SOL. 10 to 15 symbols are still to be named by you.
  - b: DONE for series i: 6 rungs, all PASS. Series ii NOT RUN at your request.
  - c: DONE: the stressed headroom stop and the capital per rung.
