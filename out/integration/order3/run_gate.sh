#!/bin/bash
# Order 3 gate (A1-A8): the full suite, compare_baselines, the v1_9 1 SOL identity replay, then the A9 stress proof. One heavy job at a time.
cd /Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
PYTHONPATH=src python3.12 -m pytest tests -q -p no:cacheprovider --continue-on-collection-errors > ../out/integration/order3/suite.log 2>&1
python3.12 scripts/compare_baselines.py tests ../out/integration/order3/suite.log > ../out/integration/order3/compare_baselines.txt 2>&1
echo done > ../out/integration/order3/suite.done
cd /Users/vaibhav/Documents/Newproject
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_9 out/integration/order3/identity_1sol --size 1 --gross 5 > out/integration/order3/identity_1sol.log 2>&1
echo done > out/integration/order3/identity_1sol.done
cd crypto_MFT_arb_platform
PYTHONPATH=src python3.12 scripts/stress_feed.py --rate 10000 --seconds 5 --profile-on-fail --out ../out/integration/stress/stress_feed_report.json > ../out/integration/stress/stress_feed.log 2>&1
echo done > ../out/integration/stress/stress_feed.done
echo all > ../out/integration/order3/gate.done
