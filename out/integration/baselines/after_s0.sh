#!/bin/bash
# Gate after S0 (quote repricing: policy, requote_target, Requoter, v1_3 profile, removed unused ArbConfig fields): 13 clean baselines + the four short faulted runs on the live tree, against b1 / f0.
B=/Users/vaibhav/Documents/Newproject/out/integration/baselines
P=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
BASELINE_SKIP='@(realistic|stressed)' python3.12 -I $B/run_baselines.py s0 5 > $B/logs/orchestrator_s0.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare b1 s0 subset > $B/logs/compare_b1_s0.log 2>&1
BASELINE_ONLY=short python3.12 -I $B/run_baselines.py s0_f 4 > $B/logs/orchestrator_s0_f.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare f0 s0_f subset > $B/logs/compare_f0_s0_f.log 2>&1
date +%T > $B/logs/after_s0.done
