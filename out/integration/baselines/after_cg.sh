#!/bin/bash
# Phase C gate: 13 clean baselines (6 at a time) + four short faulted runs against b1 / f0. Byte identity only; no speed figure is taken from it.
B=/Users/vaibhav/Documents/Newproject/out/integration/baselines
P=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
BASELINE_SKIP='@(realistic|stressed)' python3.12 -I $B/run_baselines.py cg 6 > $B/logs/orchestrator_cg.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare b1 cg subset > $B/logs/compare_b1_cg.log 2>&1
BASELINE_ONLY=short python3.12 -I $B/run_baselines.py cg_f 4 > $B/logs/orchestrator_cg_f.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare f0 cg_f subset > $B/logs/compare_f0_cg_f.log 2>&1
date +%T > $B/logs/after_cg.done
