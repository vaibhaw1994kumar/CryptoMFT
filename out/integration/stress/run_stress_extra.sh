#!/bin/bash
# A9 follow-up (the 10,000/s run FAILED): the profile of where the time goes, and the knee (the rate the path keeps up with). One heavy job at a time.
cd /Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
O=../out/integration/stress
PYTHONPATH=src python3.12 scripts/stress_feed.py --rate 10000 --seconds 2 --profile-on-fail --out $O/stress_feed_profile.json > $O/stress_feed_profile.log 2>&1
for r in 500 1000 1500 2000 3000 5000; do
  PYTHONPATH=src python3.12 scripts/stress_feed.py --rate $r --seconds 5 --out $O/stress_feed_rate_$r.json > $O/stress_feed_rate_$r.log 2>&1
done
echo done > $O/stress_extra.done
