#!/bin/bash
cd /Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
PYTHONPATH=src python3.12 -m pytest tests -q -p no:cacheprovider --continue-on-collection-errors > ../out/integration/order3/suite_B2.log 2>&1
python3.12 scripts/compare_baselines.py tests ../out/integration/order3/suite_B2.log > ../out/integration/order3/compare_baselines_B2.txt 2>&1
echo done > ../out/integration/order3/suite_B2.done
