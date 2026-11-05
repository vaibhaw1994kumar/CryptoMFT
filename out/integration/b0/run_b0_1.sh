#!/bin/zsh
cd /Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
for v in bybit binance; do for d in 2022-01-05 2022-01-13; do
  PYTHONPATH=src python3.12 scripts/queue_v2_rebuild_acceptance.py --data-root /Users/vaibhav/Documents/Newproject/data --venue $v --pair SOLUSDT --day $d --event-order legacy --out-dir ../out/integration/b0/rebuild_acceptance
done; done
date +%T
