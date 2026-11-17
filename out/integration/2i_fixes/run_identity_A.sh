#!/bin/bash
# 2i fixes, items 1-3: the v1_9 1 SOL identity replay on a frozen snapshot of the code (snapA).
cd /Users/vaibhav/Documents/Newproject
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_9 out/integration/2i_fixes/identity_A --size 1 --gross 5 --src /Users/vaibhav/Documents/Newproject/out/integration/2i_fixes/snapA/crypto_MFT_arb_platform/src > out/integration/2i_fixes/identity_A.log 2>&1
echo done > out/integration/2i_fixes/identity_A.done
