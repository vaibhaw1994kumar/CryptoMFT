#!/bin/bash
# Phase B0 gate: 13 clean baselines + the four short faulted runs on the live tree, against b1 / f0.
B=/Users/vaibhav/Documents/Newproject/out/integration/baselines
P=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
BASELINE_SKIP='@(realistic|stressed)' python3.12 -I $B/run_baselines.py b0g 3 > $B/logs/orchestrator_b0g.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare b1 b0g subset > $B/logs/compare_b1_b0g.log 2>&1
BASELINE_ONLY=short python3.12 -I $B/run_baselines.py b0g_f 2 > $B/logs/orchestrator_b0g_f.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare f0 b0g_f subset > $B/logs/compare_f0_b0g_f.log 2>&1
date +%T > $B/logs/after_b0.done
