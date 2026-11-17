#!/bin/bash
# A9 rerun after the two fixes (recorder bounded flush, arrival stamps). One heavy job at a time. Pass criterion unchanged.
cd /Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
O=../out/integration/stress/after_fix
mkdir -p $O
PYTHONPATH=src python3.12 scripts/stress_feed.py --rate 10000 --seconds 5 --profile-on-fail --out $O/stress_feed_report.json > $O/stress_feed.log 2>&1
for r in 2000 3000 4000 5000 7000; do
  PYTHONPATH=src python3.12 scripts/stress_feed.py --rate $r --seconds 5 --out $O/stress_feed_rate_$r.json > $O/stress_feed_rate_$r.log 2>&1
done
PYTHONPATH=src python3.12 scripts/stress_feed.py --rate 10000 --seconds 5 --arrival-stamp off --out $O/stress_feed_arrival_off.json > $O/stress_feed_arrival_off.log 2>&1
echo done > $O/a9_rerun.done
