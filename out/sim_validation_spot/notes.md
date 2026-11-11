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

### Open for the owner (before any order replay)
1. Accept T4 (a harness tie rule) and T5 (the archive is a coarser view) as explained, or ask for more.
2. Choose the restart window to exclude on 04-23.
3. The step-0 simulator predates Phase D, S1 and S2. Steps 1–3 would run on this worktree's newer core. Re-run T4 here first?

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
