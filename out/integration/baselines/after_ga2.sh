#!/bin/bash
# Phase A gate, second part (owner approved 5 Nov: the five full-day faulted runs are not repeated at the gate; the short faulted checks cover those paths).
B=/Users/vaibhav/Documents/Newproject/out/integration/baselines
P=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
R=$B/runs/ga/jan
until [ -f $R/legacy_v0/singapore_bybit_perp_mm_clean_s1_base/2022-01-05/summary.json ] && [ -f $R/perp_perp_maker_taker_v1/singapore_bybit_perp_mm_clean_s1_base/2022-01-05/summary.json ] && [ -f $R/perp_perp_maker_taker_v1_1/singapore_bybit_perp_mm_clean_s1_base/2022-01-05/summary.json ]; do sleep 10; done
BASELINE_SKIP='@(realistic|stressed)' python3.12 -I $B/run_baselines.py ga 3 > $B/logs/orchestrator_ga2.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare b1 ga subset > $B/logs/compare_b1_ga.log 2>&1
echo finished > $B/logs/after_ga.done
