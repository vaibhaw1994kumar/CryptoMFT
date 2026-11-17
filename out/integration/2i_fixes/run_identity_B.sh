#!/bin/bash
# 2i fixes, items 1-8: the v1_9 1 SOL identity replay on snapB (all items), and snapA (items 1-3, before item 6) again AT THE SAME TIME, so the two wall times are comparable (item 6 timing).
cd /Users/vaibhav/Documents/Newproject
R=/Users/vaibhav/Documents/Newproject/out/integration/2i_fixes
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_9 $R/identity_B --size 1 --gross 5 --src $R/snapB/crypto_MFT_arb_platform/src > $R/identity_B.log 2>&1 &
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_9 $R/identity_A2 --size 1 --gross 5 --src $R/snapA/crypto_MFT_arb_platform/src > $R/identity_A2.log 2>&1 &
wait
echo done > $R/identity_B.done
