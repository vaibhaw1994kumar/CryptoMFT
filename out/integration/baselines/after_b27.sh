#!/bin/bash
# Gate after B2.7 (ledger from the core: ledger_source switch, strict_bridge, reconcile + economics gate, MARK_MISSING, funding schedule, liquidation booking; every earlier profile on the backtester books): 13 clean baselines + four short faulted runs against b1 / f0.
B=/Users/vaibhav/Documents/Newproject/out/integration/baselines
P=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
BASELINE_SKIP='@(realistic|stressed)' python3.12 -I $B/run_baselines.py b27 5 > $B/logs/orchestrator_b27.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare b1 b27 subset > $B/logs/compare_b1_b27.log 2>&1
BASELINE_ONLY=short python3.12 -I $B/run_baselines.py b27_f 4 > $B/logs/orchestrator_b27_f.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare f0 b27_f subset > $B/logs/compare_f0_b27_f.log 2>&1
date +%T > $B/logs/after_b27.done
