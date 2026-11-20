#!/bin/bash
# Owner order 20 Nov, item 9 (REPORT ONLY, no profile change): margin.missing_price_action pause (the profile path's default) against breach (MarginConfig's own default), same 29 Oct replay.
# One profile per group of identical margin settings: v1_5..v1_9 (v1_9), v1_10..v1_16 (v1_15), v1_17. The pause runs of v1_9 and v1_15 already exist with the same arguments
# (phase2/g3_p2, phase1/g4_v115, made from committed code); this runs the breach side of each and both sides of v1_17. Launched from committed code (core 306aa08, outer 3322ed4 or later).
cd /Users/vaibhav/Documents/Newproject
W=/Users/vaibhav/Documents/Newproject/out/integration/n_order/item9
RB=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/config/regime_boundaries_day1_2026-10-29_insample.json
until [ -f out/integration/phase2/f02/ladder/i_20k.done ] && [ -f out/integration/phase2/f02/ladder/i_30k.done ] && [ -f out/integration/phase2/f02/ladder/i_50k.done ]; do sleep 30; done
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_9 $W/v19_breach --size 1 --gross 5 --set margin.missing_price_action=breach > $W/v19_breach.log 2>&1 &
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_15 $W/v115_breach --size 1 --hedge-min-notional 5 --regime-boundaries $RB --set margin.missing_price_action=breach > $W/v115_breach.log 2>&1 &
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_17 $W/v117_pause --size 1 --hedge-min-notional 5 --regime-boundaries $RB > $W/v117_pause.log 2>&1 &
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_17 $W/v117_breach --size 1 --hedge-min-notional 5 --regime-boundaries $RB --set margin.missing_price_action=breach > $W/v117_breach.log 2>&1 &
wait
echo done > $W/item9.done
