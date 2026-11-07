#!/bin/bash
# Gate after B2.4 (fees from the core FeeSchedule: fee_source switch, tier registry, profile v1_4; every earlier profile on presets): 13 clean baselines + four short faulted runs against b1 / f0.
B=/Users/vaibhav/Documents/Newproject/out/integration/baselines
P=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
BASELINE_SKIP='@(realistic|stressed)' python3.12 -I $B/run_baselines.py b24 5 > $B/logs/orchestrator_b24.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare b1 b24 subset > $B/logs/compare_b1_b24.log 2>&1
BASELINE_ONLY=short python3.12 -I $B/run_baselines.py b24_f 4 > $B/logs/orchestrator_b24_f.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare f0 b24_f subset > $B/logs/compare_f0_b24_f.log 2>&1
date +%T > $B/logs/after_b24.done
