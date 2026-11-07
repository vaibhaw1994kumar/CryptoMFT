#!/bin/bash
# Gate after B2.5 (funding from the core FundingEngine: funding_source switch, CoreFundingSettler, FUNDING_MARK_FALLBACK flag in the core; every earlier profile on the settler): 13 clean baselines + four short faulted runs against b1 / f0.
B=/Users/vaibhav/Documents/Newproject/out/integration/baselines
P=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
BASELINE_SKIP='@(realistic|stressed)' python3.12 -I $B/run_baselines.py b25 5 > $B/logs/orchestrator_b25.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare b1 b25 subset > $B/logs/compare_b1_b25.log 2>&1
BASELINE_ONLY=short python3.12 -I $B/run_baselines.py b25_f 4 > $B/logs/orchestrator_b25_f.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare f0 b25_f subset > $B/logs/compare_f0_b25_f.log 2>&1
date +%T > $B/logs/after_b25.done
