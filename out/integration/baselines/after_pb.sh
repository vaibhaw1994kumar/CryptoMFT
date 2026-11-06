#!/bin/bash
# Phase B gate: 13 clean baselines + the four short faulted runs on the live tree, against b1 / f0 (v1.1 and legacy_v0 must stay byte-identical).
B=/Users/vaibhav/Documents/Newproject/out/integration/baselines
P=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
BASELINE_SKIP='@(realistic|stressed)' python3.12 -I $B/run_baselines.py pbg 3 > $B/logs/orchestrator_pbg.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare b1 pbg subset > $B/logs/compare_b1_pbg.log 2>&1
BASELINE_ONLY=short python3.12 -I $B/run_baselines.py pbg_f 2 > $B/logs/orchestrator_pbg_f.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare f0 pbg_f subset > $B/logs/compare_f0_pbg_f.log 2>&1
date +%T > $B/logs/after_pb.done
