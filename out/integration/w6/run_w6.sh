#!/bin/bash
# W6 notify-only: G3 v1_9 1 SOL identity; G4 v1_13 (dislocation monitor on, notify only) against v1_12 on the 29 Oct day.
cd /Users/vaibhav/Documents/Newproject
W=/Users/vaibhav/Documents/Newproject/out/integration/w6
RB=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/config/regime_boundaries_day1_2026-10-29_insample.json
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_9 $W/identity_1sol_v19 --size 1 --gross 5 > $W/identity_1sol_v19.log 2>&1 &
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_12 $W/v112 --size 1 --hedge-min-notional 5 --regime-boundaries $RB > $W/v112.log 2>&1 &
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_13 $W/v113 --size 1 --hedge-min-notional 5 --regime-boundaries $RB > $W/v113.log 2>&1 &
wait
echo done > $W/w6.done
