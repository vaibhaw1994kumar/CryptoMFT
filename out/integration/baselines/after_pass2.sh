#!/bin/bash
# Gate after the second-pass decisions of 7 Nov (cohort clear_ahead, one print budget, dataset_settings, placement_book): 13 clean baselines + the four short faulted runs on the live tree, against b1 / f0.
B=/Users/vaibhav/Documents/Newproject/out/integration/baselines
P=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
BASELINE_SKIP='@(realistic|stressed)' python3.12 -I $B/run_baselines.py pp2 5 > $B/logs/orchestrator_pp2.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare b1 pp2 subset > $B/logs/compare_b1_pp2.log 2>&1
BASELINE_ONLY=short python3.12 -I $B/run_baselines.py pp2_f 4 > $B/logs/orchestrator_pp2_f.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare f0 pp2_f subset > $B/logs/compare_f0_pp2_f.log 2>&1
date +%T > $B/logs/after_pass2.done
