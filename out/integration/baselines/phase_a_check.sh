#!/bin/bash
# Phase A per-move check: scenario file, parity tests, the affected tests, then the March 2024-03-08 slices (3 profiles) against the b1 baseline.
# usage: phase_a_check.sh LABEL [affected pytest paths ...]       SOLO=1 adds the solo v1.1 2024-03-11 run (speed reference 378 s)
B=/Users/vaibhav/Documents/Newproject/out/integration/baselines
P=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
L=$1; shift
cd $P && PYTHONPATH=src python3.12 -m pytest tests/test_integration_scenarios.py tests/test_order_state_parity.py tests/test_maker_executor_engine_parity.py "$@" -q -p no:cacheprovider --no-header 2>&1 | tail -4
BASELINE_ONLY="re:^mar/.*2024-03-08" python3.12 -I $B/run_baselines.py $L 3 > $B/logs/orchestrator_$L.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare b1 $L subset
if [ -n "$FAULT" ]; then
  # four faulted v1.1 runs (stressed seeds 1-3, realistic seed 2) cut to the first 7,500 s of 2022-01-13, against the same runs on the pre-Phase-A tree (label f0)
  BASELINE_ONLY=short python3.12 -I $B/run_baselines.py ${L}_f 2 > $B/logs/orchestrator_${L}_f.log 2>&1
  python3.12 -I $P/scripts/compare_baselines.py compare f0 ${L}_f subset
fi
if [ -n "$SOLO" ]; then
  BASELINE_ONLY="re:^mar/perp_perp_maker_taker_v1_1/2024-03-11" python3.12 -I $B/run_baselines.py ${L}_solo 1 > $B/logs/orchestrator_${L}_solo.log 2>&1
  python3.12 -c "import json;print('solo v1.1 03-11 wall_s', round(json.load(open('$B/runs/${L}_solo/mar/base/perp_perp_maker_taker_v1_1/2024-03-11/summary.json'))['wall_s']), '(reference 378)')"
  python3.12 -I $P/scripts/compare_baselines.py compare b1 ${L}_solo subset
fi
