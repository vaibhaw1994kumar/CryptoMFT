#!/bin/bash
B=/Users/vaibhav/Documents/Newproject/out/integration/baselines
P=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
python3.12 -I $B/run_baselines.py ga 3 > $B/logs/orchestrator_ga.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare b1 ga > $B/logs/compare_b1_ga.log 2>&1
echo finished > $B/logs/after_ga.done
