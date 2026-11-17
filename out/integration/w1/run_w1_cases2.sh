#!/bin/bash
cd /Users/vaibhav/Documents/Newproject/out/integration/day1
for c in 4a_hedge_feed_lost_actions_v111 4b_hedge_feed_lost_observe_v111 3e_missing_marks_v110; do
  ( python3.12 -I scenarios.py $c > runs_$c.log 2>&1; echo done2 > runs_$c.done2 ) &
done
wait
echo all > ../w1/w1_cases2.done
