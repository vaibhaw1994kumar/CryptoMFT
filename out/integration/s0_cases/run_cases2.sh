#!/bin/bash
cd /Users/vaibhav/Documents/Newproject
S=out/integration/s0_cases
for c in 2i_s0 2i_s0_B 2i_control_v19_B; do
  python3.12 -I out/integration/day1/scenarios.py $c --src $PWD/$S/snap2/crypto_MFT_arb_platform/src > $S/${c}_r2.log 2>&1
  echo done > $S/${c}_r2.done
done
echo all > $S/all2.done
