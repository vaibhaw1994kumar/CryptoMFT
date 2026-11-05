#!/bin/bash
# waits for the b1 pool, writes its manifest, then re-runs the three March slices as b2 (determinism) and compares
B=/Users/vaibhav/Documents/Newproject/out/integration/baselines
until grep -q "baselines done b1" $B/logs/orchestrator_b1.log; do sleep 20; done
python3.12 -I $B/compare_baselines.py manifest b1 > $B/logs/manifest_b1.log 2>&1
BASELINE_ONLY=mar python3.12 -I $B/run_baselines.py b2 3 > $B/logs/orchestrator_b2.log 2>&1
python3.12 -I $B/compare_baselines.py manifest b2 > $B/logs/manifest_b2.log 2>&1
python3.12 -I $B/compare_baselines.py compare b1 b2 subset > $B/logs/compare_b1_b2.log 2>&1
echo finished > $B/logs/after_b1.done
