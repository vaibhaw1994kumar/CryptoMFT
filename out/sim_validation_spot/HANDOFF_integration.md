# Track C (sim-validation) → integration session: handoff

Written 2026-11-12 by session 064a8b6d (Track C). EXPLORATORY.
- Nothing is pushed. All commits are local on the track branches.
- This session made no edits to the main tree and wrote nothing to `data/`.

## 1. What to merge

| Repo | Branch | Head | Action |
|---|---|---|---|
| outer | `sim-validation` | see `git log -1 sim-validation` (this file's commit) | merge into `integration-2026-11-01` |
| core | `sim-validation-core` | `f7b7cc3` | **nothing to merge**: identical to `consolidate-new-backtest` |

**Already in integration, don't merge again:**
- `93d795b`: `ccb3ed8`, `cd85775`, `15d3dcd` (edge guard, spot labels, notes).
- `fe5ea27`: `80e28a6`, `093ca45` (`event_order="delta_first"`, T4 notes).
- core `1c11951`: `4d65ef7` (`cross_gross_bps`).

**Merge base:** `sim-validation` already contains `integration-2026-11-01` up to `d88a4be`; that merge commit is `c468a9a`. Integration has moved since (`fe5ea27`, `84ac38a`).
- `git merge-tree integration-2026-11-01 sim-validation` reports **no conflicts** (checked at `84ac38a`).
- The two branches have two merge bases, so a three-dot diff mis-attributes `event_loader.py` and `test_queue_v2_invariants.py`. Both are already in integration through `fe5ea27`.

**Commits still to merge (outer, oldest first):**

| Commit | What |
|---|---|
| `2cbd9ea` | `backtester.py`: `maker_only` scheduled orders take an optional 5th item `cancel_ts` (cancel at exactly that exchange time, no latency added); each scheduled order's outcome in `analysis["orders"]`. Opt-in: inert unless `analysis_mode` is set; 4-item orders unchanged. +3 tests in `test_maker_order_lifetime.py` |
| `98a2238` | new package `src/new_backtest/validation/`: `spot_order_replay.py` (step-1 runner), `evaluate.py` (step-2 scoring) |
| `edaf768` | `validation/fill_model_fit.py` (step 3: FillModelV2 on real outcomes); `tests/test_spot_validation.py` |
| `00ab7e6`, `eeb0733` | step 1–3 aggregates, March tie check, notes |
| `19450ae` | `validation/perp_taker_check.py` (step 5) and its outputs |
| `f00918b` | scoring with the definitions pre-registered in `TOLERANCES.md`; calibration slope |
| `869df79` | `validation/build_report.py`, `out/sim_validation_spot/report.html` |
| `122cd17` | 2c cause (amended orders), post hoc sensitivity, step-3 explanation |
| `0742985` | owner answers (`TOLERANCES.md` addendum 3, window 06:00–06:20), current-core re-run, fill rate and markouts, multi-coin FillModelV2 tolerances DRAFT |
| (this file) | handoff |

**Code touched outside `validation/`:** only `backtester.py` (`2cbd9ea`, opt-in).
- No profile, no core module and no default changed.
- `legacy_v0` is unaffected; the scheduled-order path is analysis-only.

## 2. Tests (worktree, `PYTHONPATH=src:tests python3.12 -m pytest`)
- **Full suite without `platform_kit`, on the merged branch (`0742985`):** **2,565 passed, 0 failed**, 28 skipped, 11 xfailed, 93 subtests passed (12 min 53 s)
- **Before the merge (`19450ae`):** 2,485 passed, 0 failed, 23 skipped, 11 xfailed.
- **`tests/platform_kit` (11 Nov, before the merge):** the 93 names of the Phase-0 failing list fail as before.
  - 2 more fail only in a worktree, because gitignored files are missing (`config.yaml`, `examples/custom_strategy.py`).
  - Not re-run after the merge.
- **New or extended tests:**
  - `test_spot_validation.py` (13): exclusions, false-fill and timing classes, PostOnly confusion, Wilson interval, pre-registered buckets and verdict rules, calibration slope, probable amendment, real labels only;
  - `test_maker_order_lifetime.py::ScheduledCancelTests` (3).

## 3. Results (owner brief "spot simulator validation", steps 0–5)
- **Market and data:** Bybit spot ADAUSDT and MNTUSDT, 2026-04-19..05-02, our 1,976 real PostOnly orders, books from the public ob200 archive.
- **Run setup:** `maker_only` on the old loop, profile `spot_spot_maker_taker_v1`, latency preset singapore; `event_order=delta_first`, `trust_local_ts=False` (validation run config only).
- **Full report:** `out/sim_validation_spot/report.html`, also a private artifact the owner holds (https://claude.ai/artifact/KCUw6fnfMqz4pwUF2HKP7w).

| Check (pre-registered rule, `TOLERANCES.md` 10 Nov) | Result | Verdict |
|---|---|---|
| T4 rebuild check | `delta_first` 28/28; legacy 26/28 (a tie rule of the harness, accepted by the owner) | accepted |
| T5 archive vs recorder | 14/14 fail; the archive is a coarser view (accepted by the owner) | accepted |
| 2a false fills on real cancels | 0.46% proportional (0.35% / 0.81% for the bounds) | PASS |
| 2b first-fill timing | hit 83.9% vs 80.5% earlier, premature 4.3% (an upper bound: our fills are in the tape) | PASS |
| 2c PostOnly | 37/38 real rejects caught; 52/1,936 false rejects = 2.7% (limit 2%) | FAIL, cause below |
| 3 FillModelV2 on real outcomes | Brier −18%, 11/13 days not worse; calibration slope 2.11 | NO IMPROVEMENT |
| 5 perp taker, 8 orders | 8/8 exact price and quantity (tests the touch only) | no gross error |

- **Fill rate and markouts** (owner 12 Nov, proportional):
  - fill rate over the accepted orders: real 55.4%, sim 53.0%;
  - markouts at 5 s and 30 s: sim within 0.1 bps of real.
  - The difference comes entirely from the 123 orders whose simulated fill is more than 1 s from the real one.
- **2c cause: amended orders.**
  - Our MM amends live quotes (`FAST_MM/bybit_order_manager.py` `amend_order`), and Bybit's history keeps the final price with the original create time.
  - The account export has no amend flag or history.
  - Post hoc, without the 58 probable amendments: 2c 0/1,878 false rejects; 2b hit 88.2% (outside the two-sided band).
- **Re-run on the current core** (`c468a9a` / `f7b7cc3`, cut short by the owner): T4 on 16 coin-days, 59 of the 84 replays (4,848 orders) and step 5 are identical field by field.

## 4. Rules and decisions the integration session should keep
- **`event_order="delta_first"`** (non-default) is right for the Bybit **spot archive** only. Each day's 00:00:01 snapshot holds its same-timestamp updates.
  - March 2024 SOLUSDT keeps `legacy`: ties on 03-01 (rows come after the snapshot), 03-02 and 03-04 (rows inside it). The arb's replayed book is correct under legacy; `delta_first` would be wrong on 03-01.
  - No arb profile was changed.
- **Labels for any fill model come from real orders only,** never from the simulator.
- **Account order data stays on this Mac.**
  - Per-order rows are in `out/sim_validation_spot/local_only/` (git-ignored by its own `.gitignore`) and in the session scratchpad. They carry a running number, never order ids.
  - Do not `git add -f` that folder.
- **Old-loop deviation (recorded):** the order replay uses the old loop's `maker_only` mode, the only engine path with scheduled orders. Porting scheduled orders into the session runtime would close this.

## 5. Data facts found (for CLAUDE.md / the data owner)
- **`data/bybit-spot/` (recorder capture 04-19..04-25) was lost** (12 Nov).
  - The owner rebuilt the folder from the public archive with `Newproject/scripts/rebuild_bybit_spot_from_archive.py`. That is **not recorder data** (200 levels, no gaps, full trade tape).
  - Suggest renaming it, e.g. `bybit-spot-from-archive/`.
  - That script duplicates `scripts/mm/bybit_spot_archive.py` (mm-integration) and sits in the old tree; one of them should go.
- **`data/bybit/`:** every coin folder except SOLUSDT is empty. The 22-coin collector data used by the fill-evidence study (30 Oct) seems gone too.
- **Converted spot archive:** the data steps 1–3 read lives only in `simval_wt/Newproject/out/sim_validation_spot/converted/` (626 MB, not in git). The perp 04-29 conversion and the per-order runs are in this session's scratchpad (temporary). Aggregates are committed.

## 6. Open items (owner)
1. **2c / amended orders:** a clean check needs the MM's successful-amend log, which is not kept today. Logging successful amends (order id, time, old and new price) in `FAST_MM` would close it.
2. **FillModelV2 multi-coin study:** `out/sim_validation_spot/fillmodel_multicoin/TOLERANCES.md` is a DRAFT, not frozen.
   - Scope: 8 coins, 91 coin-days, about 17,100 orders.
   - Owner, 12 Nov: "later stage". It needs a download OK; disk is tight (about 16 GB free).
3. **Rename `data/bybit-spot/`** (section 5).
4. **Steps 1–3 of the brief are done;** nothing of Track C is running.

## 7. Reproduce
From `crypto_MFT_arb_platform/`, with `PYTHONPATH=src`:
- step 1, one coin-day and cancel model per process:
  `python3.12 -m new_backtest.validation.spot_order_replay --orders <bybit_30d dir> --data-root <root with bybit/ -> converted spot> --pair ADAUSDT --day 2026-04-26 --cancel-model proportional --out <runs>`
- steps 2–3:
  `python3.12 -m new_backtest.validation.evaluate --runs <runs> --orders <dir> --data-root <root> --out step2_evaluation.json [--order-csv <local file>]`, then `python3.12 -m new_backtest.validation.fill_model_fit --runs <runs> --out step3_fill_model.json`
- step 5: `python3.12 -m new_backtest.validation.perp_taker_check ...`
- report: `python3.12 -m new_backtest.validation.build_report --root out/sim_validation_spot`
