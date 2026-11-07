#!/bin/bash
# Identity check after renaming new_backtest/core_shadow.py to core_accounting.py: March 2024-03-11 (2 h), legacy_v0, v1, v1.1 against b1, v1_2 (cell ft) and v1_4 (cell g1l) against their earlier cells. All five at once.
B=/Users/vaibhav/Documents/Newproject/out/integration/baselines
P=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
L=/Users/vaibhav/Documents/Newproject/out/integration/phase_b
BASELINE_ONLY='re:^mar/.*2024-03-11' python3.12 -I $B/run_baselines.py b2r_m 3 > $B/logs/orchestrator_b2r_m.log 2>&1 &
LEDGER_OUT=$L/ledger_b2r LEDGER_DAYS=2024-03-11 python3.12 -I $L/run_ledger_cells.py 2 mar ft g1l > $B/logs/orchestrator_b2r_ledger.log 2>&1 &
wait
python3.12 -I $P/scripts/compare_baselines.py compare b1 b2r_m subset > $B/logs/compare_b1_b2r_m.log 2>&1
date +%T > $B/logs/after_b2r.done
