#!/bin/bash
cd /Users/vaibhav/Documents/Newproject
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_9 out/integration/w1/identity_1sol_W1 --size 1 --gross 5 > out/integration/w1/identity_1sol_W1.log 2>&1
echo done > out/integration/w1/identity_1sol_W1.done
