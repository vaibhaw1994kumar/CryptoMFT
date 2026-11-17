#!/bin/bash
# W2b gates: (1) v1_9 1 SOL identity replay; (2) v1_12 against v1_11 (the incident block is never read by a replay): both full 29 Oct days, in parallel.
cd /Users/vaibhav/Documents/Newproject
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_9 out/integration/w2b/identity_1sol_W2b --size 1 --gross 5 > out/integration/w2b/identity_1sol_W2b.log 2>&1 &
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_12 out/integration/w2b/v112_1sol --size 1 --gross 5 > out/integration/w2b/v112_1sol.log 2>&1 &
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_11 out/integration/w2b/v111_1sol --size 1 --gross 5 > out/integration/w2b/v111_1sol.log 2>&1 &
wait
echo done > out/integration/w2b/gate_W2b.done
