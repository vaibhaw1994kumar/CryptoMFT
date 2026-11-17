#!/bin/bash
# Gate for B: the full suite, compare_baselines, then the v1_9 1 SOL identity replay. One heavy job at a time.
cd /Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
PYTHONPATH=src python3.12 -m pytest tests -q -p no:cacheprovider --continue-on-collection-errors > ../out/integration/order3/suite_B.log 2>&1
python3.12 scripts/compare_baselines.py tests ../out/integration/order3/suite_B.log > ../out/integration/order3/compare_baselines_B.txt 2>&1
echo done > ../out/integration/order3/suite_B.done
cd /Users/vaibhav/Documents/Newproject
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_9 out/integration/order3/identity_1sol_B --size 1 --gross 5 > out/integration/order3/identity_1sol_B.log 2>&1
echo done > out/integration/order3/identity_1sol_B.done
