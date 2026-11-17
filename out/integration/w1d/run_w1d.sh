#!/bin/bash
# Queue item (d): v1_11 with the owner's feed-health decisions. Cases 4x / 4a / 4c with the profile's own values; full days: v1_9 identity (G3), v1_10 reference and v1_11 with the
# block removed (G4), v1_11 as decided (withdrawals at 500 ms silence, quotes surviving), v1_12 (= v1_11 + incident block; its G4 against v1_11).
cd /Users/vaibhav/Documents/Newproject/out/integration/day1
W=/Users/vaibhav/Documents/Newproject/out/integration/w1d
RB=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/config/regime_boundaries_day1_2026-10-29_insample.json
for c in 4x_feed_health_v111_decided 4a_hedge_feed_lost_v111_decided 4c_feed_delay_v111_decided; do python3.12 -I scenarios.py $c > $W/$c.log 2>&1 & done
cd /Users/vaibhav/Documents/Newproject
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_9 $W/identity_1sol_v19 --size 1 --gross 5 > $W/identity_1sol_v19.log 2>&1 &
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_10 $W/v110_ref --size 1 --hedge-min-notional 5 --regime-boundaries $RB > $W/v110_ref.log 2>&1 &
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_11 $W/v111_off --size 1 --hedge-min-notional 5 --set feed_health=null --regime-boundaries $RB > $W/v111_off.log 2>&1 &
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_11 $W/v111_decided --size 1 --hedge-min-notional 5 --regime-boundaries $RB > $W/v111_decided.log 2>&1 &
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_12 $W/v112_decided --size 1 --hedge-min-notional 5 --regime-boundaries $RB > $W/v112_decided.log 2>&1 &
wait
echo done > $W/w1d.done
