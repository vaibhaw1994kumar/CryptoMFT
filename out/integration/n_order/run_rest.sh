#!/bin/bash
# N-order (owner 20 Nov) rest, after the network break: two runs at a time at most (coordinator). 1) dense 2i BEFORE (snapshot core 776c2a9 + outer a415bbd, profiles/config copied in) and v1_9 breach;
# 2) v1_15 breach and v1_17 breach; then the serial G1 suite alone. Committed code only (core 306aa08, outer 44a74e8).
cd /Users/vaibhav/Documents/Newproject
N=/Users/vaibhav/Documents/Newproject/out/integration/n_order
RB=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/config/regime_boundaries_day1_2026-10-29_insample.json
( cd out/integration/day1 && [ ! -d runs/scn_2i_v110_dense2 ] && python3.12 -I scenarios.py 2i_v110_dense2 --src $N/snap_before/src > $N/dense2_before.log 2>&1 && mv runs/scn_2i_v110_dense2 $N/dense2_before ) &
python3.12 -I $N/item9/run_mpa.py breach perp_perp_maker_taker_v1_9 $N/item9/v19_breach --size 1 --gross 5 > $N/item9/v19_breach.log 2>&1 &
wait
python3.12 -I $N/item9/run_mpa.py breach perp_perp_maker_taker_v1_15 $N/item9/v115_breach --size 1 --hedge-min-notional 5 --regime-boundaries $RB > $N/item9/v115_breach.log 2>&1 &
python3.12 -I $N/item9/run_mpa.py breach perp_perp_maker_taker_v1_17 $N/item9/v117_breach --size 1 --hedge-min-notional 5 --regime-boundaries $RB > $N/item9/v117_breach.log 2>&1 &
wait
echo done > $N/rest_runs.done
cd crypto_MFT_arb_platform
PYTHONPATH=src python3.12 -m pytest tests -q -p no:cacheprovider -rfE --continue-on-collection-errors > $N/g1_serial.log 2>&1
python3.12 scripts/compare_baselines.py tests $N/g1_serial.log > $N/g1_compare.txt 2>&1
echo done > $N/rest.done
