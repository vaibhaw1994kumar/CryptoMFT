#!/bin/bash
cd /Users/vaibhav/Documents/Newproject
S=out/integration/s0_cases
for c in 2i_control_v19 2i_s0 2i_s0_reject 2i_s0_timeout 2i_s0_disconnect 2i_s0_dup_filled; do
  python3.12 -I out/integration/day1/scenarios.py $c --src $PWD/$S/snap/crypto_MFT_arb_platform/src > $S/$c.log 2>&1
  echo done > $S/$c.done
done
echo all > $S/all.done
