#!/bin/bash
# Same final gate as after_final.sh, with more jobs at once (the first orchestrator ran 3 of 8 cores). The three January 01-13 jobs it had started keep running and are skipped here.
B=/Users/vaibhav/Documents/Newproject/out/integration/baselines
P=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
BASELINE_SKIP='@(realistic|stressed)|^jan/(legacy_v0|perp_perp_maker_taker_v1|perp_perp_maker_taker_v1_1)/2022-01-13$' python3.12 -I $B/run_baselines.py pbf 5 > $B/logs/orchestrator_pbf2.log 2>&1
for i in $(seq 1 600); do [ -f $B/runs/pbf/jan/legacy_v0/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/summary.json ] && [ -f $B/runs/pbf/jan/perp_perp_maker_taker_v1/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/summary.json ] && [ -f $B/runs/pbf/jan/perp_perp_maker_taker_v1_1/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/summary.json ] && break; sleep 10; done
python3.12 -I $P/scripts/compare_baselines.py compare b1 pbf subset > $B/logs/compare_b1_pbf.log 2>&1
BASELINE_ONLY=short python3.12 -I $B/run_baselines.py pbf_f 4 > $B/logs/orchestrator_pbf_f.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare f0 pbf_f subset > $B/logs/compare_f0_pbf_f.log 2>&1
date +%T > $B/logs/after_final.done
