#!/bin/bash
# Gate after B2.6 (margin from the core RiskEngine: margin_source switch, CoreMarginModel; every earlier profile on margin_model): 13 clean baselines + four short faulted runs against b1 / f0.
B=/Users/vaibhav/Documents/Newproject/out/integration/baselines
P=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
BASELINE_SKIP='@(realistic|stressed)' python3.12 -I $B/run_baselines.py b26 5 > $B/logs/orchestrator_b26.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare b1 b26 subset > $B/logs/compare_b1_b26.log 2>&1
BASELINE_ONLY=short python3.12 -I $B/run_baselines.py b26_f 4 > $B/logs/orchestrator_b26_f.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare f0 b26_f subset > $B/logs/compare_f0_b26_f.log 2>&1
date +%T > $B/logs/after_b26.done
