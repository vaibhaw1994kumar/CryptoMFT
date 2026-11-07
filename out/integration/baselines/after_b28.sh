#!/bin/bash
# Gate after B2.8 (reference profiles labelled, working profiles refused on old accounting modules at startup): 13 clean baselines + four short faulted runs against b1 / f0.
B=/Users/vaibhav/Documents/Newproject/out/integration/baselines
P=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
BASELINE_SKIP='@(realistic|stressed)' python3.12 -I $B/run_baselines.py b28 5 > $B/logs/orchestrator_b28.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare b1 b28 subset > $B/logs/compare_b1_b28.log 2>&1
BASELINE_ONLY=short python3.12 -I $B/run_baselines.py b28_f 4 > $B/logs/orchestrator_b28_f.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare f0 b28_f subset > $B/logs/compare_f0_b28_f.log 2>&1
date +%T > $B/logs/after_b28.done
