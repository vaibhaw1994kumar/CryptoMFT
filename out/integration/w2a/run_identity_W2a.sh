#!/bin/bash
cd /Users/vaibhav/Documents/Newproject
mkdir -p out/integration/w2a
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_9 out/integration/w2a/identity_1sol_W2a --size 1 --gross 5 > out/integration/w2a/identity_1sol_W2a.log 2>&1
echo done > out/integration/w2a/identity_1sol_W2a.done
