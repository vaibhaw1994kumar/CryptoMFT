#!/bin/bash
# Gate for C: the full suite vs the 93 baseline, then the v1_9 1 SOL identity replay.
cd /Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
PYTHONPATH=src python3.12 -m pytest tests -q -p no:cacheprovider --continue-on-collection-errors > ../out/integration/order3/suite_C.log 2>&1
python3.12 scripts/compare_baselines.py tests ../out/integration/order3/suite_C.log > ../out/integration/order3/compare_baselines_C.txt 2>&1
echo done > ../out/integration/order3/suite_C.done
cd /Users/vaibhav/Documents/Newproject
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_9 out/integration/order3/identity_1sol_C --size 1 --gross 5 > out/integration/order3/identity_1sol_C.log 2>&1
echo done > out/integration/order3/identity_1sol_C.done
