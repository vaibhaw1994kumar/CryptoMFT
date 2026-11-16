#!/bin/bash
# Order 2 gate: the full suite, compare_baselines, then the v1_9 1 SOL identity replay. One heavy job at a time.
cd /Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
PYTHONPATH=src python3.12 -m pytest tests -q -p no:cacheprovider > ../out/integration/v110/suite_order2.log 2>&1
python3.12 scripts/compare_baselines.py tests ../out/integration/v110/suite_order2.log > ../out/integration/v110/compare_baselines_order2.txt 2>&1
echo done > ../out/integration/v110/suite_order2.done
cd /Users/vaibhav/Documents/Newproject
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_9 out/integration/v110/identity_1sol --size 1 --gross 5 > out/integration/v110/identity_1sol.log 2>&1
echo done > out/integration/v110/identity_1sol.done
