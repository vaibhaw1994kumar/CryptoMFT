#!/bin/bash
# owner 18 Nov, item 2: v1_15 (post-only amend to a crossing price ends the order). G3 v1_9 identity; G4 v1_15 vs v1_14 on the 29 Oct day; the dense 2i case and the full-day rehearsal (pair-close on, as the
# 17 Nov rehearsal rows with amends) on v1_14 and v1_15, with the case-2i amend audit.
cd /Users/vaibhav/Documents/Newproject
W=/Users/vaibhav/Documents/Newproject/out/integration/v115
RB=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/config/regime_boundaries_day1_2026-10-29_insample.json
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_9 $W/identity_1sol_v19 --size 1 --gross 5 > $W/identity_1sol_v19.log 2>&1 &
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_15 $W/v115 --size 1 --hedge-min-notional 5 --regime-boundaries $RB > $W/v115.log 2>&1 &
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_14 $W/rehearsal_v114cp --size 1 --hedge-min-notional 5 --set arb.close_pairs=reduce_only_quote --regime-boundaries $RB --s0-audit > $W/rehearsal_v114cp.log 2>&1 &
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_15 $W/rehearsal_v115cp --size 1 --hedge-min-notional 5 --set arb.close_pairs=reduce_only_quote --regime-boundaries $RB --s0-audit > $W/rehearsal_v115cp.log 2>&1 &
cd out/integration/day1
python3.12 -I scenarios.py 2i_v114_dense2 > $W/2i_v114_dense2.log 2>&1 &
python3.12 -I scenarios.py 2i_v115_dense2 > $W/2i_v115_dense2.log 2>&1 &
wait
echo done > $W/v115.done
