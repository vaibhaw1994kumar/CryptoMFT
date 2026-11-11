# Track C notes (owner 11 Nov 2026): spot validation step 0, single edge formula, spot labels

Session 064a8b6d. EXPLORATORY. No push, no replays, no writes to `data/`, no VM, no downloads, no keys, no order data opened.

## Worktree

| Repo | Path | Branch | Base |
|---|---|---|---|
| Outer | `/Users/vaibhav/Documents/wt_sim-validation` | `sim-validation` | `integration-2026-11-01` @ 3345167 |
| Core | `/Users/vaibhav/Documents/wt_sim-validation/crypto_MFT_arb_platform/src/crypto_stat_arb` | `sim-validation-core` | `consolidate-new-backtest` @ bcf6e98 |

- `crypto_stat_arb.__file__` resolves to the worktree (checked with `PYTHONPATH=src:tests`).
- `crypto_MFT_arb_platform/data` is a symlink to the main tree's `data/`. It is read only and is not tracked (`data/` is ignored).
- A first worktree (`simvalidation_wt`, both branches `sim-validation`) was made before the naming brief arrived. It was removed, and both branches were deleted, before anything was committed.

## (1) Spot validation, step 0: DONE EARLIER, reused, STOPPED for review

Step 0 was already run on 10 Nov by session 05e290d4, in its own worktree: `/Users/vaibhav/Documents/simval_wt/Newproject`, detached at tag phaseC-gate (outer 2013aa4, core 1c123ad).
- It was not re-run. The conversion peaks at 3 GB per job, and the inputs (raw archive and converter) are unchanged.
- The full report is `simval_wt/Newproject/out/sim_validation_spot/step0_report.html`. The tolerances, written before any number was seen, are in `TOLERANCES.md` (sha256 `50924c3140e4dd1e…`, two dated addenda).
- Steps 1–5 have not started. The order file `~/Downloads/bybit_30d.zip` was never opened.

### Inputs and conversion
- **Input:** ADAUSDT and MNTUSDT Bybit spot, 2026-04-19..05-02, from `~/Downloads/bybit_data/spot/{orderbook,trades}`.
- **Converter:** the MM session's `scripts/mm/bybit_spot_archive.py` (mm-integration 394976f), run in place.
- **orjson shim:** `orjson` is not installed, so a stdlib shim stood in. The converted content should equal the cloud conversion; the bytes, and so the output sha256, may differ.

### Verdicts against the tolerances

| Tolerance | Verdict | Evidence |
|---|---|---|
| T0 raw sha256 | PASS | 48/56 files match the MM session's lists, 0 differ, 8 have no reference hash (05-01, 05-02, both coins) |
| T1 conversion | PASS | 28/28 coin-days exit 0; converted trade rows = raw csv rows on 28/28 |
| T2 sequence | PASS | 0 gaps in 28 coin-days (8,469,960 messages) |
| T3 rebuild vs the archive's own snapshots | PASS | touch price 100% on every comparison, but only 1 snapshot per day (04-23: 2) |
| T4 core harness (`queue_v2_rebuild_acceptance.py`) | **FAIL 2/28** | ADA 05-02 and MNT 04-30 at 0% (one-snapshot days); the other 26 are 100% on all six variants |
| T5 vs our recorder, `data/bybit-spot` 04-19..25 | **FAIL 14/14** | touch price 89.5–99.1% (gate 99%), touch size 65–95% (gate 95%), at lag 0 on the exchange clock |

### T4 cause
A tie rule, not missing data:
- The day-end snapshot has the same update id and the same cts as the last delta.
- The core loader applies a snapshot first on equal timestamps, so it compares the book without that delta.
- The converter's own update-id-order check matches those same snapshots at 100%.

### T5 per day (lag 0, gated)

| Day | ADA price % | ADA size % | MNT price % | MNT size % |
|---|---|---|---|---|
| 04-19 | 97.61 | 71.82 | 89.52 | 80.70 |
| 04-20 | 96.59 | 70.50 | 94.61 | 84.18 |
| 04-21 | 97.00 | 69.91 | 97.70 | 90.25 |
| 04-22 | 97.82 | 70.59 | 97.04 | 86.42 |
| 04-23 | 97.93 | 65.03 | 97.43 | 85.08 |
| 04-24 | 98.33 | 70.32 | 97.96 | 93.70 |
| 04-25 | 99.14 | 84.76 | 97.84 | 94.74 |

What the post-hoc checks (addendum 2) show:
- Reading the archive about 50 ms later is the best alignment on every day.
- With any lag allowed, 99.1–99.7% (ADA) and 94.1–98.8% (MNT) of recorder moments find a matching touch price.

A hypothesis, not a proven cause: the archive (ob200) updates every 103 ms (ADA) or 131 ms (MNT) at the median, against 42 ms for the recorder. The recorder holds 100% of its prints in the archive, but only 22–62% of the archive's prints.

### Findings that matter for steps 1–3
- **Feed restart on 2026-04-23 at about 06:09 UTC, both coins.** The update id restarts at 1, and the book differs after it. Flag or exclude orders placed in the minutes before it.
- **The recorder is a weak reference for fills.** Use the archive for trades.
- **T3 and T4 rest on one snapshot per day.** That makes them agreement checks only. The sequence check is the stronger evidence.

### Owner decisions (11 Nov 2026)
1. **T4:** accept the tie explanation if T4 passes with "updates first, then the snapshot" at equal timestamps. Result below: it does, 28/28.
2. **T5:** accepted as "the archive is coarser".
   - Use the recorder's books where they exist (04-19..04-25), and the archive for 04-26..05-02.
   - Label results from the archive days "public ob200 archive, ~100 ms resolution".
3. **04-23 restart:** exclude 06:04:00–06:20:00 UTC, plus any order still resting when the book resynced.
   - Resync times from the data (update id resets to 1):
     - ADA: last old update 06:08:52.756, resync snapshot 06:08:58.321;
     - MNT: last old update 06:09:03.423, resync snapshot 06:09:08.302.
   - Both are inside the window.

### T4 re-run on this worktree's core (owner 11 Nov)
Code: outer `15d3dcd`, core `4d65ef7`, `crypto_stat_arb` and `new_backtest` loaded from the worktree.
- **Inputs:** the same converted files as step 0 (`simval_wt/.../converted`), read only.
- **How:** one coin-day per process, serially. Results in `rebuild_acceptance_wt/`, tables in `rebuild_acceptance_wt/t4_comparison.md`.

**(a) `legacy` order (snapshot first on ties): identical to step 0 on all 28 coin-days.**
- Every variant and every metric match; 26 PASS and 2 FAIL, as before (ADA 05-02 and MNT 04-30 at 0%).
- The newer core (Phase D, S1, S2) changed nothing here.
- Correction to step 0's wording: "the other 26 are 100% on all six variants" holds for touch **price** only, which is what T4 gates. Under `legacy`, touch size was 0% on 12 days (2 of them the failing days) and 50% on 1. Top-10 was 40–100% on the passing days, and only 8 of 28 coin-days were 100% on all three metrics.

**(b) New, non-default `event_order="delta_first"` (deltas, then the snapshot, then trades, on equal exchange timestamps): T4 PASS 28/28.**
- **Every day has the tie.** Each day's file ends with the next day's 00:00:01 snapshot, which carries the same exchange timestamp and the same update id `u` as the last delta rows (2 to 40 rows on the four days inspected). So that snapshot is the book after those deltas.
- **Touch price:** 100% on all 28 coin-days, in all six variants. ADA 05-02 and MNT 04-30 go from 0% to 100%.
- **Touch size and top-10:** 100% on 26 of 28 coin-days, up from 8 of 28 under `legacy`.
- **The remaining mismatch is only on 04-23, both coins** (size 50%, top-10 85% ADA and 80% MNT). It is the restart snapshot itself, 06:08:58.321 for ADA and 06:09:08.302 for MNT: the pre-restart book against the post-resync book. That is inside the excluded window.
- **No metric is lower than under `legacy` on any day.**
- **Legacy unchanged:** after the loader change, a `legacy` re-run of ADA 05-02 is identical to the earlier one.
- **Tests after the loader change:** `tests` without `platform_kit`: 2474 passed (2472 + the 2 new), 0 failed, 23 skipped, 11 xfailed.

**Code change (outer, `new_backtest`, default unchanged):**
- `event_loader.iter_venue_events` accepts `event_order="delta_first"`. The `legacy` branch makes the same decisions as before.
- `scripts/queue_v2_rebuild_acceptance.py` accepts it and writes `<venue>_<day>_delta_first.json`, so legacy outputs are not overwritten.
- A comment in `backtester.py`.
- Two tests in `tests/test_queue_v2_invariants.py::EventOrderAndDedupe`:
  - `delta_first` is delta → snapshot → trade;
  - a mixed stream where `legacy` and `delta_first` differ only at snapshot/delta ties.

**Reach:** the session runtime reads through `CrossVenueFeed` → `iter_venue_events` with the config's `event_order`, so steps 1–3 can use `delta_first` by setting it.
- Nothing sets it today. No profile was changed.
- The core's own readers (`maker_sim`, `session_audit`, `tardis_compare`) were not changed.

### Still open for the owner
- A go for step 1. It opens the account order file `~/Downloads/bybit_30d.zip` (aggregates only in reports).
- Whether steps 1–3 run with `event_order="delta_first"` for the spot archive days.

## (2) Single edge formula: DONE

### Finding
| Module | Edge formula? | Action |
|---|---|---|
| core `strategy.py` (`CrossVenueSpreadStrategy`) | yes: direction choice and `gross_bps` (lines 47, 53) | routed through `strategies/edge.py` |
| core `mean_reversion.py` | yes: `gross_bps` (line 262), not named in the brief | routed, because the guard caught it |
| `trading_platform_poc/opportunity.py` | **no** | unchanged; it only carries `expected_edge` from its caller |
| `new_backtest/edge.py` | stub that re-exports the core | unchanged |

### Change (core)
- `strategies/edge.py`: new `cross_gross_bps(buy_px, sell_px) = (sell - buy) / buy * 10_000`. `legacy_taker_taker_edge_bps` now calls it.
  - The factor is the integer `10_000`, so a float result is bit-identical to `* 10_000.0` and a Decimal result to `* Decimal("10000")`.
  - The net in each strategy keeps its own fee subtraction order, so Decimal results do not move.
- `strategy.py` (direction key and gross) and `mean_reversion.py` (gross) call `cross_gross_bps`.

### Test (outer)
New file `tests/test_single_edge_formula.py`:
- **The scan:** reads `crypto_stat_arb`, `new_backtest` and `trading_platform_poc`. It skips stale copies (`crypto_stat_arb/src`, the nested `crypto_MFT_arb_platform`, `advanced_backtest`), frozen release copies (`runs/`) and tests. It fails on any assignment to an edge, gross or net name from `(a - b) / c * 1e4` outside `edge.py`. "hedge" does not count as "edge".
- **Allowed:** one line, `backtester.py`'s realised `gross_bps` from fill VWAPs.
- **Red before the fix:** it flagged exactly `strategy.py:53` and `mean_reversion.py:262`. Green after.
- **Bit-identity:** a second test checks `cross_gross_bps` is bit-identical to both old expressions on 20,000 random float and Decimal pairs.

## (3) Spot labels: DONE

- `profiles/spot_spot_maker_taker_v1.yaml` and `spot_perp_basis_v1.yaml`:
  - the description now ends "; old backtester only, mechanics";
  - new `decision_status: {scope: OLD_BACKTESTER_ONLY_MECHANICS}`.
- **Config hash unchanged:** sha256 of every field of the applied config, before and after (ADAUSDT 04-20). `spot_spot_maker_taker_v1` is `8b82ed34286b91eb` both times; `spot_perp_basis_v1` is `aef5c85fbe042469` both times.
- **File sha256 changes, as expected:** `98e9d91b…` → `45a4d11b…` and `08042fe5…` → `1de39a19…`. `decision_status` is outside the config body and goes into release_identity.

## Tests
Run from `crypto_MFT_arb_platform/` with `PYTHONPATH=src:tests python3.12 -m pytest`, one job at a time, on the worktree with the data symlink.
- **`tests` without `platform_kit`:** 2472 passed, 0 failed, 23 skipped, 11 xfailed (14 min 24 s).
- **`tests/platform_kit`** (`--continue-on-collection-errors`): 94 failed, 807 passed, 1 skipped, 1 error.
  - **Against the Phase 0 list** (`out/integration/baselines/failing_tests_phase0.txt`, 93 names, all in `platform_kit`): all 93 fail here too, and nothing on the list passes.
  - **2 failures caused only by worktree data paths:**
    - `test_config.py::test_loads_project_configuration` needs `config.yaml`;
    - `test_public_sdk.py::test_custom_strategy_example_executes` needs `examples/custom_strategy.py`.
    - Both files exist in the main tree's `crypto_MFT_arb_platform/`, but are gitignored, so a worktree does not have them.
  - No failure is caused by the Track C change.
- **Focused, after the change:** `test_single_edge_formula` (3), `test_edge_dispatch_acceptance`, `platform_kit/test_opportunity_fill_triggered`, `test_fee_paths`, `test_alignment`, `test_paper_queue_sim`, `test_order_state_parity`, `test_moved_code_guards` and `test_import_direction` all pass, as do the core `tests/test_fee_paths` and `test_alignment`.
- **Before the data symlink:** `test_alignment::test_alignment_pipeline_is_reproducible` failed only because `data/samples/two_venue_books.jsonl` was missing; it passes with the symlink.

## Commits (local, track branches only, not pushed)
| Repo, branch | Commit | What |
|---|---|---|
| core `sim-validation-core` | `4d65ef7` | item 2: `cross_gross_bps`; `strategy.py` and `mean_reversion.py` routed through it |
| outer `sim-validation` | `ccb3ed8` | item 2: `tests/test_single_edge_formula.py` |
| outer `sim-validation` | `cd85775` | item 3: the spot profile labels |
| outer `sim-validation` | (this file) | the notes |

Merge order: core `4d65ef7` first, because the guard test imports `edge.cross_gross_bps`.

### Commits for the T4 re-run (local, `sim-validation`, not pushed)
See `git log sim-validation`: the `delta_first` loader option with its tests, then these notes and the `rebuild_acceptance_wt/` results.

## Item 3 (owner 11 Nov): snapshot/update ties in the March 2024 Bybit SOLUSDT files and the recorder captures (read only, no code change)

Method: `march_tie_check/bybit_SOLUSDT_ties.jsonl`, written by a read-only scratch script. For each day 2024-03-01..15 of `Newproject/data/bybit` SOLUSDT, it finds every book-update row whose exchange timestamp equals a snapshot's. For each such row it records:
- its event type;
- whether the snapshot already holds it (same size at that price, or for a delete, the price absent);
- whether its price is inside the snapshot's top 10 levels.

`legacy` and `delta_first` differ only in where these rows go relative to the snapshot, so a day without tied rows cannot change any book.

| Day | Snapshots | Tied rows at the day-start snapshot | Tied rows at the day-end snapshot (next day 00:00:01) |
|---|---|---|---|
| 03-01 | 2 | 14 (12 update, 2 delete): **0** already in the snapshot, 1 in the top 10 | 0 |
| 03-02 | 2 | 0 | 24 (22 update, 2 delete): **all 24** already in the snapshot, 3 in the top 10 |
| 03-04 | 2 | 0 | 326 (283 update, 43 delete): **all 326** already in the snapshot, 19 in the top 10 |
| 03-03, 03-05..03-15 | 2 (03-05: 3, one mid-day) | 0 | 0 |

**What it means**
- **12 of 15 days have no tie;** `legacy` and `delta_first` give identical books there.
- **03-02 and 03-04, day end:** like the spot archive, the last rows of the day are already in the 00:00:01 snapshot. A rebuild check that compares at that snapshot under `legacy` compares before those rows, so its top-10 comparison would differ (3 and 19 levels). Under `delta_first` it matches.
- **03-01, day start:** the opposite. The 14 rows come after the snapshot. `delta_first` would apply them before the snapshot, the snapshot would wipe them, and the book would be wrong at those 14 levels (1 in the top 10) until they next change.
- **The arb's own replay is not affected under `legacy`** (the profiles' setting). Every tied row is an absolute update or a delete, with no decremental `cancel`. So re-applying rows already in the snapshot leaves the book unchanged, and the book after every tie is correct under `legacy`. `delta_first` would be wrong on 03-01.
- **No arb profile change.** The ordering has to be chosen per dataset: `delta_first` for the Bybit spot archive (validation only), `legacy` for March 2024.

**Recorder captures (`data/live_sessions/solusdt/2026-10-29`):** the reader (`session_replay.iter_recorded_events`) does not merge streams by timestamp. It replays the recorder's own arrival order (`trace.jsonl`), so `event_order` does not apply and there is no day-boundary tie rule. It is one continuous capture.

**Spot recorder data (`data/bybit-spot`) was lost:** the 04-19..04-25 capture is no longer on this Mac. The folder now holds a rebuild from the public archive (200 levels, no gaps, the full trade tape; against the original's 50 levels, 59 gaps over 60 s on ADA 04-19 and about 22% of the prints). A live capture cannot be downloaded again. The owner decided on 12 Nov to use the public archive for 04-19..04-25 too. This replaces the 11 Nov "recorder books up to 25 April" decision. All 14 days are labelled "public ob200 archive, ~100 ms resolution".

## Steps 1–3 (owner go 11 Nov; 04-19..04-25 from the archive, owner 12 Nov)

**EXPLORATORY.**
- **Market and data:** Bybit spot, ADAUSDT and MNTUSDT, 2026-04-19..05-02 (14 days, both coins), from the public ob200 archive (~100 ms resolution).
- **Orders:** our real PostOnly limit orders, 1,976 of them.
- **Profile:** `spot_spot_maker_taker_v1` (old backtester only, mechanics), `latency_preset` singapore, run in `maker_only` mode on the old loop. That is a known deviation from the session-runtime rule: it is the only engine path that takes scheduled orders.
- **Run config overrides:**
  - `event_order=delta_first`, because each archive day ends with a 00:00:01 snapshot that already holds its same-timestamp updates (T4 28/28 vs 26/28);
  - `trust_local_ts=False` (the archive's `local_ts` is Bybit's send stamp);
  - `maker_max_age_s=0`, `spot_requires_inventory=False`, risk limits lifted.
- **Timing:** each order is released at its real create time plus the profile's sampled entry latency, and cancelled at exactly its real cancel time.
- **Engine runs:** 84 (14 coin-days × 3 cancel models), all exit 0, no order skipped. Code: outer `2cbd9ea`, `98a2238`, `edaf768`.
- **Exclusions and censoring:**
  - The 04-23 exclusions caught nothing: no order was created between 05:00 and 07:30 UTC that day.
  - 2 orders resting across midnight are censored.
- **Files:** aggregates are in `steps_1_3/step2_evaluation.json` and `steps_1_3/step3_fill_model.json`. Per-order rows stay in the session scratchpad (no order ids).

**Caveat on hit rates:** the public tape contains our own real fills. A really-filled order always has its print in the data, and a cancelled one never does. Hit rates on filled orders are therefore an upper bound. The false-fill rate on cancelled orders is the fairer test.

### Step 2 results (scored 1,974)
| | proportional (headline) | behind_first | ahead_first |
|---|---|---|---|
| a. False fills on real CANCELLED (n = 864) | **4 = 0.46%** [0.18, 1.18] | 3 = 0.35% [0.12, 1.02] | 7 = 0.81% [0.39, 1.66] |
| — ADA / MNT | 0/721 / 4/143 (2.8%) | 0/721 / 3/143 | 0/721 / 7/143 (4.9%) |
| b. Real FILLED (n = 1,071): hit within 1 s | **83.9%** [81.5, 85.9] | 72.8% | 78.7% |
| — premature / late / unfilled / simulated PostOnly reject | 4.3 / 7.2 / 0.0 / 4.7% | 1.9 / 20.4 / 0.2 / 4.7% | 11.2 / 5.4 / 0.0 / 4.7% |

**a. False fills**
- By day, all false fills are on 05-01 (1/25) and 05-02 (3/839, proportional).
- By book: fresh 4/702, stale 0/162.
- By queue ahead at placement: ≥1000× the order size 0/572; 100–1000× 3/286; 10–100× 1/4.
- About 99% of the cancels are on 05-02 (839 of 864).

**b. First-fill timing (proportional)**
- By coin: ADA 88% hit, MNT 80%.
- By week: 04-19..04-25 86%, 04-26..05-02 79%.
- By book: fresh 84%, stale 83%.
- Orders placed inside the spread (n = 20): hit 70%, premature 30%. That is the weakest group.
- Reference: the fill-evidence study had an 80.5% hit (proportional, 22 coins, collector data).
- Proportional sits between the two bounds, as expected: behind_first is late 20%, ahead_first premature 11%.

**c. PostOnly reject at arrival (the same in every model)**
- The simulator rejects 37 of the 38 real PostOnly rejects.
- It also rejects 52 of the 1,936 really accepted orders (2.7%).

**PartiallyFilledCanceled (1 order, ADA 04-29):** simulated fill under proportional and ahead_first, none under behind_first.

### Step 3: FillModelV2 on real outcomes (tag MEASURED_SPOT_2026; not approved as a quote filter)
- **Inputs:** features are the engine's placement features from the proportional runs; labels and censoring are the REAL outcomes (`fill_model_fit.py`); IPCW as in fill_model_v1.
- **Folds:** chronological, each fitted on all earlier days.
- **Horizon 5 s:** the model's Brier score is below the base rate's on 11 of 13 test days. It is worse on 04-23 (+0.019, n = 37) and 04-26 (+0.046, n = 34).
- **Headline fold: fit 04-19..05-01 (991 orders), test 05-02 (895 orders, 640 censored at 5 s).**

| Horizon | Base rate (fit days) | Observed on 05-02 | Brier model / base rate | Difference [95%] |
|---|---|---|---|---|
| 1 s | 6.9% | 0.0% | 0.0021 / 0.0047 | −0.0026 [−0.0030, −0.0022] |
| 5 s | 20.0% | 0.7% | 0.0361 / 0.0440 | −0.0079 [−0.0109, −0.0050] |
| 15 s | 41.9% | 3.1% | 0.152 / 0.181 | −0.028 [−0.038, −0.019] |

**What step 3 shows**
- **05-02 is a different quoting regime,** as flagged. Its 5 s fill rate is 0.7%, against 20% on the fit days.
- **The model is not calibrated there.** It beats the base rate only by predicting lower. It still predicts about 8–26% in reliability bins where 0–2% is observed.
- **The censoring check could not run.** The engine's `price_moved_through` is false on every row, so the IPCW assumption (non-informative cancels) is untested here.

### What this does NOT establish
- Spot, not perp.
- Two coins.
- About 97% of the cancels are on one day (05-02), and that day is a different regime.
- All books are the public archive: the recorder capture was lost, so there is no second book source.
- Hit rates are an upper bound, because our own fills are in the tape.
