#!/bin/bash
# Final gate of the Phase B second pass (ledger, override split, stamps, cancel-model clamp): 13 clean baselines + the four short faulted runs on the live tree, against b1 / f0.
B=/Users/vaibhav/Documents/Newproject/out/integration/baselines
P=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
BASELINE_SKIP='@(realistic|stressed)' python3.12 -I $B/run_baselines.py pbf 3 > $B/logs/orchestrator_pbf.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare b1 pbf subset > $B/logs/compare_b1_pbf.log 2>&1
BASELINE_ONLY=short python3.12 -I $B/run_baselines.py pbf_f 2 > $B/logs/orchestrator_pbf_f.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare f0 pbf_f subset > $B/logs/compare_f0_pbf_f.log 2>&1
date +%T > $B/logs/after_final.done
