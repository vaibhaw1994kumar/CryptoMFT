# Discarded v1.0 (moved 2026-11-05)

Nothing here is used by current runs. Moved, not deleted; to restore, move an item back to its original path.
Do not quote results from this folder.

| Original path (from Newproject/) | Now at | Files | Why discarded |
|---|---|---|---|
| `crypto_MFT_arb_platform/scripts/run_sol_wf.py` | `discarded/v1.0/scripts/run_sol_wf.py` | 1 | Retired walk-forward: fake Oct-2024 Binance book, colocated_both, circular fill gates |
| `crypto_MFT_arb_platform/scripts/run_sol_wf_vip0.py` | `discarded/v1.0/scripts/run_sol_wf_vip0.py` | 1 | Fee variant of the same retired walk-forward |
| `out/sol_wf_backtest` | `discarded/v1.0/out/sol_wf_backtest` | 15 | Results of the retired walk-forward: invalid |
| `out/sol_wf_backtest_vip0` | `discarded/v1.0/out/sol_wf_backtest_vip0` | 0 | Results of the retired fee variant: invalid |
| `out/queue_v2/runs_mar2024_SUPERSEDED_pre_fix` | `discarded/v1.0/out/queue_v2/runs_mar2024_SUPERSEDED_pre_fix` | 18 | March v1.1 runs stopped before the five step-9 fixes |
| `out/queue_v2/build_report_part2.py.bak_quarantined` | `discarded/v1.0/out/queue_v2/build_report_part2.py.bak_quarantined` | 1 | Quarantined backup of a report builder |
| `out/queue_v2/oct_sim_pool.sh` | `discarded/v1.0/out/queue_v2_oct_harness/oct_sim_pool.sh` | 1 | October maker harness: own fill rules, skipped PostOnly check; outputs unused |

Still in `out/queue_v2/`, not moved (other scripts refer to them, including the running ladder's code path): `oct_bybit_maker_sim.py`, `oct_sim/`, `maker_only/`. Move them after the ladder finishes and the referring scripts are checked.
`_empty_leftover` is an empty folder created by mistake during the move; safe to delete.
Also updated: `tests/test_perp_arb_legs.py` now reads run_sol_wf.py from this folder (pre-change copy in `crypto_MFT_arb_platform/_backups/discard_v1_0_20261105_0055/`).
