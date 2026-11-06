#!/bin/bash
# Phase B (depletion ledger / override split / stamps) gate: 13 clean baselines + the four short faulted runs on the live tree, against b1 / f0 (v1.1, v1 and legacy_v0 must stay byte-identical).
B=/Users/vaibhav/Documents/Newproject/out/integration/baselines
P=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
BASELINE_SKIP='@(realistic|stressed)' python3.12 -I $B/run_baselines.py pbl 3 > $B/logs/orchestrator_pbl.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare b1 pbl subset > $B/logs/compare_b1_pbl.log 2>&1
BASELINE_ONLY=short python3.12 -I $B/run_baselines.py pbl_f 2 > $B/logs/orchestrator_pbl_f.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare f0 pbl_f subset > $B/logs/compare_f0_pbl_f.log 2>&1
date +%T > $B/logs/after_ledger.done
