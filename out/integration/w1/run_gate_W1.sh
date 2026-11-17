#!/bin/bash
# Gate for W1: the full suite vs the 93 baseline, then the v1_9 1 SOL identity replay.
cd /Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
PYTHONPATH=src python3.12 -m pytest tests -q -p no:cacheprovider --continue-on-collection-errors > ../out/integration/w1/suite_W1.log 2>&1
python3.12 scripts/compare_baselines.py tests ../out/integration/w1/suite_W1.log > ../out/integration/w1/compare_baselines_W1.txt 2>&1
echo done > ../out/integration/w1/suite_W1.done
