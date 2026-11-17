#!/bin/bash
# W7 7.D (pre-trade estimate per order): G3 v1_9 1 SOL identity with the W7 code; v1_14 on the 29 Oct day, compared with v1_13 (out/integration/w6/v113) after stripping the new key.
cd /Users/vaibhav/Documents/Newproject
W=/Users/vaibhav/Documents/Newproject/out/integration/w7
RB=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/config/regime_boundaries_day1_2026-10-29_insample.json
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_9 $W/identity_1sol_v19 --size 1 --gross 5 > $W/identity_1sol_v19.log 2>&1 &
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_14 $W/v114 --size 1 --hedge-min-notional 5 --regime-boundaries $RB > $W/v114.log 2>&1 &
wait
echo done > $W/w7.done
