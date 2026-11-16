#!/bin/bash
cd /Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
PYTHONPATH=src python3.12 -m pytest tests -q -p no:cacheprovider --continue-on-collection-errors > ../out/integration/v110/suite_order2.log 2>&1
python3.12 scripts/compare_baselines.py tests ../out/integration/v110/suite_order2.log > ../out/integration/v110/compare_baselines_order2.txt 2>&1
echo done > ../out/integration/v110/suite_order2b.done
