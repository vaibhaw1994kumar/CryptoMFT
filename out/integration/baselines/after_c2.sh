#!/bin/bash
# Identity check after the Phase C review (15 bps band, labels, unknown block, fee pricing of stream fills): March 2024-03-11 (2 h), legacy_v0, v1, v1.1 against b1, v1_2 (cell ft) and v1_4 (cell g1l) against their earlier cells. All five at once.
B=/Users/vaibhav/Documents/Newproject/out/integration/baselines
P=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
L=/Users/vaibhav/Documents/Newproject/out/integration/phase_b
BASELINE_ONLY='re:^mar/.*2024-03-11' python3.12 -I $B/run_baselines.py c2_m 3 > $B/logs/orchestrator_c2_m.log 2>&1 &
LEDGER_OUT=$L/ledger_c2 LEDGER_DAYS=2024-03-11 python3.12 -I $L/run_ledger_cells.py 2 mar ft g1l > $B/logs/orchestrator_c2_ledger.log 2>&1 &
wait
python3.12 -I $P/scripts/compare_baselines.py compare b1 c2_m subset > $B/logs/compare_b1_c2_m.log 2>&1
date +%T > $B/logs/after_c2.done
