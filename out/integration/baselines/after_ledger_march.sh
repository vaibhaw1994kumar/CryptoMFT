#!/bin/bash
# Re-run of the six March gate baselines after the runner stopped listing a switch that is off in summary.maker_config (the first pbl March run differed only in that key).
B=/Users/vaibhav/Documents/Newproject/out/integration/baselines
P=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
while [ ! -f $B/logs/after_ledger.done ]; do sleep 10; done
BASELINE_ONLY=mar python3.12 -I $B/run_baselines.py pbl_m 3 > $B/logs/orchestrator_pbl_m.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare b1 pbl_m subset > $B/logs/compare_b1_pbl_m.log 2>&1
date +%T > $B/logs/after_ledger_march.done
