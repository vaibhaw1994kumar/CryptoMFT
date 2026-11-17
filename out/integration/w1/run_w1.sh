#!/bin/bash
# W1 replays (EXPLORATORY): the four injected cases on v1_11, and two full 29 Oct days: v1_11 as it is (observation) and v1_11 with the feed_health block removed (the G4 off-identity run against v1_10).
cd /Users/vaibhav/Documents/Newproject/out/integration/day1
for c in 4x_feed_health_v111 4a_hedge_feed_lost_actions_v111 4b_hedge_feed_lost_observe_v111 4c_feed_delay_v111; do
  ( python3.12 -I scenarios.py $c > runs_$c.log 2>&1; echo done > runs_$c.done ) &
done
cd /Users/vaibhav/Documents/Newproject
( python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_11 out/integration/w1/v111_observe --size 1 --hedge-min-notional 5 --regime-boundaries /Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/config/regime_boundaries_day1_2026-10-29_insample.json > out/integration/w1/v111_observe.log 2>&1; echo done > out/integration/w1/v111_observe.done ) &
( python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_11 out/integration/w1/v111_off --size 1 --hedge-min-notional 5 --set feed_health=null --regime-boundaries /Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/config/regime_boundaries_day1_2026-10-29_insample.json > out/integration/w1/v111_off.log 2>&1; echo done > out/integration/w1/v111_off.done ) &
wait
echo all > out/integration/w1/w1_runs.done
