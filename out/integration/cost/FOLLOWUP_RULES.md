# Step G follow-ups 1-5: rules (owner 15 Nov; written BEFORE any follow-up number exists; report only)

Same discipline as G_RULES.md: 00:00-02:00 UTC March 2024 days read from data/ (read only), TRAIN 03-01..07, SELECT 03-08..12, HOLDOUT 03-13..15, the 2026-10-29 capture reported on its own;
10-minute block bootstrap, 2,000 resamples, seed 1; every number EXPLORATORY and PROXY (the feed carries the real book at arrival, not other participants' reaction to our orders); a gain smaller than its interval is NO EFFECT;
the queue model is perp-unvalidated. HOLDOUT 03-13..15 was already read once for G and is NOT READ AGAIN here (CLAUDE.md: once): items 1a and 1b run on TRAIN + SELECT (03-01..12, 12 days) and the
capture; "HOLDOUT not read; available on the owner's go" is written in the report. Item 2 has no HOLDOUT (see there). No core file changes; the run wrappers in out/integration/cost/ get new optional flags only (defaults byte-identical).

## 1a. T1 at large sizes (t1_hedge_cost.py --fracs, outputs in t1b/)
- Fractions of D10 (displayed size within 10 bps of the touch on the consumed side): 0.25, 0.5, 1, 2, 4 (= 25 / 50 / 100 / 200 / 400 %). Everything else as T1 in G_RULES.md (Bybit every 1 s, 120 s warm-up, outcome = walk of the book 0.1 s later against the
  touch at the decision, predictions P0 / P1 measured / x0.8 / x1.2, cliff flag at 2 / 3 / 5 bps with 3 ASSUMED). Windows: Bybit March TRAIN + SELECT (03-01..12; HOLDOUT NOT READ); the capture on Bybit and on Binance.
- An order the book cannot fill at the arrival walk is counted and left out of the cost (selection effect: reported as a share per fraction; at 400% it can be large). Predictions undefined (P0 unfillable) are counted the same way.
- Identity check before anything is read: the fractions 0.25, 0.5, 1.0 of 2024-03-01 must reproduce the G run's rows (t1/march_2024-03-01.npz) exactly; if not, the new run is wrong and nothing is reported.
- Reported per group (TRAIN / SELECT / HOLDOUT / capture venues) and per fraction: realised cost, MAE and signed error of each prediction, the cost curve fit a + b (Q/D10)^delta over all fractions with intervals (t1_fit.py), the unfillable share.
- Extra columns saved by the new run (they change no prediction): the book mid at the decision, and the number of book updates applied between the decision and the arrival (item 4, item 3).

## 1b. Repeated hedging (same pass, sequences)
- A sequence starts every 60 s after the 120 s warm-up, alternating the consumed side (ask, bid, ask, ...). A sequence is K = 10 hedges of the same size Q = f x D10 (D10 measured at the sequence start, fixed for the sequence), f in {0.1, 0.5, 1.0},
  the same side, spaced s seconds apart, s in {1, 2, 5, 10}. Each hedge is decided at t0 + k s and walks the book as it stands 0.1 s later (as T1).
- The own-consumption reductions are a VIRTUAL OFFSET inside the walk (a per-sequence ledger price -> removed size, applied to a copy of the level list); the live book view and VenueState are never changed, so the next decision's D10, survival and gap scans never read our own footprint.
  Identity check with the sequences switched ON: the fractions 0.25, 0.5, 1.0 rows of 2024-03-01 must still equal the G rows exactly.
- Own consumption variants on the same sequences: OFF (every hedge walks the market book as it stands: our earlier hedges are not in it); REFILL tau = 3.5 s (the tau of the isolated overlay default, impact_overlay_settings.json): the size our earlier hedges
  took at each price level is removed from the book at that price, and the removal decays as exp(-age/tau) from the hedge's arrival; NEVER (bound): removed sizes never come back inside the sequence. A level that left the book is not touched.
  The overlay file's own permanent fraction is 1 (the shift never recovers); REFILL is a different construct (a per-level size refill) and is labelled so.
- Cost per hedge: side-signed VWAP of the walk (on the possibly reduced book) against the UNMODIFIED touch at that hedge's decision, in bps. So the cost includes our own consumption. Per unit: mean over hedges of a sequence and by k.
- Reported: mean cost per unit by (f, s, variant), by hedge index k, with intervals (10-minute blocks of the sequence start), and the unfillable share; cost per unit against the total size K x Q / D10. Bybit March (TRAIN and SELECT apart) and the capture on Bybit and Binance.

## 1c. T5 at 100 and 500 SOL, with a time-sliced variant (t5_split.py large; capture only, Binance as the hedge venue)
- Binance March has no depth, so 100 and 500 SOL cannot be walked there; the touch-fits-the-size assumption of the March T5 is NOT used. The capture has Binance depth: the market part and any later remainder walk the real Binance book at their arrival.
  So this item has NO TRAIN/SELECT/HOLDOUT split of days. Chronological halves of the capture instead (as the factor study): the FIRST half builds the outflow samples and sigma_1s and CHOOSES; the SECOND half is the answer. The first half is not the owner's TRAIN.
- Decisions every 5 s after the 120 s warm-up (not 1 s: the walks are heavy), both hedge sides, S in {100, 500}. Arrival delay 0.045 s, taker 1.7 bps, maker rebate 0, as T5.
- Arms: M (all at market at arrival, walk the book); E (split_single_venue, theta = 0 as in T5; limit part rests at the touch, fills by the depleting prints after the queue ahead; the unfilled part is sent at market at T + 0.045 s; (T, lambda_u) from the
  T5 grid); TS-AC (N market slices over a horizon H: slice i at i H / N, sizes from the Almgren-Chriss trajectory x(t) = X sinh(kappa (H - t)) / sinh(kappa H), kappa H in {0 (equal slices), 2 (front-loaded)}, N in {2, 5, 10}, H in {5, 30} s).
  Own consumption for every arm with more than one order (E market part then the late remainder, every slice): REFILL tau = 3.5 s as 1b, plus for the slices the OFF bound (no own consumption).
- Cost per unit = side-signed (decision mid - VWAP of the walk) / mid in bps + taker fee; limit fills at the rest price (as T5). Unhedged time: the volume-weighted time to hedge (as T5), and for the slices the inventory-risk time sqrt(integral of (remaining/S)^2 dt) also.
  Risk-adjusted net = -cost - w x sqrt(time) x sigma_1s with w in {0, 0.5, 1}; sigma_1s = std of 1 s changes of the last-trade price of the first half, bps. First half chooses by the highest net at w = 1 and S = 100 within each arm (E: T, lambda_u; TS: N, H, kappa).
- Reported on the second half: M, chosen E, chosen TS-AC (REFILL and OFF) per S with intervals and the differences to M; the full grid on both halves in the JSON. Cost of E whose limit share is below the interval is NO EFFECT.

## 2. T5 at maker-fill moments (t5_split.py run --times; outputs t5c/)
- Hedge moments = the Bybit maker fill times of the PROBE (qv2_probe: a 1 SOL Bybit bid every 15 s, no hedge): TRAIN 03-01..07 (2,035 fills) and SELECT 03-08..11 (692 fills); these are SIMULATED fills (the queue model is perp-unvalidated).
  The hedge is the SELL on Binance, decided at the fill time, arriving 0.045 s later. HOLDOUT 03-13..15 has no probe fills: not run (running the probe there would be a new HOLDOUT read; offered to the owner, not done).
- Settings: the cell chosen in G on random moments (T = 1 s, lambda_u = 10 bps) is the primary; the 12-cell grid is reported at S = 10 on TRAIN and on SELECT fills to show whether the choice would differ.
- Per size 1 / 10 / 100: fill rate of the rested part (filled volume / rested volume), cost, unhedged time, net at w 0 / 0.5 / 1, E minus M with intervals; the rested-fill markout apart (+1 s, +5 s).
- Comparison on the SAME days and the SAME side (sell hedges): the random-moment result from t5b (all 1 s grid times). Adverse-move check: the Binance mid move over the 5 s before the fill (negative = fell, adverse to a hedge that sells), in terciles of the TRAIN fills; E minus M by tercile.
- Blocks: 10-minute blocks. Same decision labels (NO EFFECT when the interval includes 0).
- Clock basis (checked BEFORE any result is read): the probe fill timestamps and the Binance event exchange_ts that t5_split.py reads must share a clock. On 2024-03-01 the mean of (Binance bid at fill + 0.045 s - fill price) / fill price, read once from the
  probe_shortfall.py MAIN series basis (data/binance/book_snapshots, the series that reproduced the pinned quote-to-hedge move) and once from the t5_split.py event basis, must agree within 0.1 bps; if not, the basis of probe_shortfall MAIN is used for the moment
  and the arrival price, and the pre-fill move terciles are computed on the same basis. The result of the check is saved in t5c/clock_check.txt.

## 3. Thinner books: ADA and MNT (Bybit linear perp, April 2026)
- data/ has NOTHING for them (data/bybit/*/pair=ADAUSDT and pair=MNTUSDT are empty directories since 1 Nov; data/binance has only BTCUSDT and SOLUSDT). BUT the owner's own download is on this Mac: ~/Downloads/bybit_data/{orderbook,trades}/{ADAUSDT,MNTUSDT}
  (public Bybit linear-perp history: ob200 zips, tick-trade csv.gz; 2026-04-01..05-02 and 2026-07-01..08, from download_bybit_april2026.py). It is read in place, READ ONLY: nothing is copied into data/, nothing is downloaded. 200 levels, about 100 ms resolution
  (public archive; the earlier simulator validation found ~103 ms ADA and ~131 ms MNT medians), so it is the archive's book, not a recorder's.
- Binance ADA / MNT depth: CONFIRMED NONE locally (no file in data/, none in Downloads, none elsewhere in the project). Everything on these coins is Bybit only; the Binance side cannot be tested.
- Windows per coin: days 04-01, 04-03, 04-05, 04-07, 04-09, 04-11, 04-13, each at 00:00-02:00 and 13:00-15:00 UTC (14 windows, 28 h per coin). The first seven windows (04-01..04-07) are the earlier half and fix the volatile threshold; the later seven are where
  it is applied (a threshold not taken from the windows it is applied to). Both halves are reported.
- Run: the T1 wrapper of 1a (same fractions, predictions, flags at 2 / 3 / 5 bps with 3 ASSUMED, sequences) in an `archive` mode; extra columns saved in every T1 run from now on: the flicker and the cancel-to-trade of the consumed side at the decision.
  Reported per coin: survival (mean per sample), cancel-to-trade, cliff-flag frequency at 2 / 3 / 5 bps, T1 error (MAE and signed) per fraction, error split by volatile vs normal 10-minute blocks (volatile = realised volatility of the 1 s mid log returns above the
  75th percentile of the coin's own first-half blocks) with intervals of the volatile-minus-normal MAE difference. The same split is done for SOL (March TRAIN + SELECT with the TRAIN threshold; the capture with its own first-half threshold).
- tick: 0.0001 for both coins (checked from the archive's price increments before the run). Thinner-book behaviour of these coins is NOT evidence about SOL, and a book archive at 100 ms is a coarser feed than a recorder's.

## 4. T1 framing (G_RESULTS)
- The 0.1 s outcome is read on a 100 ms feed on Binance and a 20 ms feed (capture) / event feed (March) on Bybit: the book at the arrival is often the same state as at the decision, or the next one. The share of samples with no book update between the decision and the arrival
  (and with one, with two or more) is measured in 1a and written into G_RESULTS with the caveat that the small error is partly by construction and that T1 does not test others' reaction or sub-100 ms changes.

## 5. Perp queue validation (a plan only; owner approval needed; nothing is sent or started): written to PERP_QUEUE_VALIDATION_PLAN.md. A question to the owner: does the live perp arb bot log full order lifecycles?


## Deviations and findings recorded AFTER the rules were written (15 Nov)
- Advisor review before any number: HOLDOUT not read again (1a, 1b run on 03-01..12); ADA/MNT searched properly (found in ~/Downloads/bybit_data, see item 3); item 2 clock check done first (passed, t5c/clock_check.txt); 1b own consumption is a virtual offset (identity check with sequences on passed).
- Found while doing item 2: the G T5 fill rule counted prints at any price. All T5 in the follow-ups is PRICE-AWARE (`--price-aware`; the G default is kept in t5b/). The cell for item 2 was re-chosen on SELECT random moments under the corrected rule (T 1 s, lambda_u 5). The G T5 HOLDOUT figures were not recomputed (second HOLDOUT read).
- The view in the new March runs keeps 500 levels (G: 200), so 400% of D10 can be walked; the old columns are identical on the rows both runs have.
- ADA flags are meaningless at the 3 bps stretch limit (tick 4.1 bps): reported, rule not changed. dq_gap_min_ticks was set to 1 for the 2 / 3 / 5 bps variants of the new runs (identical on SOL, where 3 bps is 40+ ticks) and the tick rule is reported as its own column.
- ADA and MNT: 14 windows each (04-01..13 odd days, 00-02 and 13-15 UTC), first 7 windows fix the volatile threshold, the second 7 are the table (the first is also reported).
