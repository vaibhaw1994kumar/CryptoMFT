#!/bin/bash
# E3 size sweep: two workers (8 GB machine), v1_9 with the participation limit and cap, 2026-10-29 capture, gross 5 x size.
cd /Users/vaibhav/Documents/Newproject
E=out/integration/e3_sweep
run() { n=$1; python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_9 $E/size_$n --size $n --gross $((n*5)) --src $PWD/$E/snap/crypto_MFT_arb_platform/src > $E/size_$n.log 2>&1; echo done > $E/size_$n.done; }
( run 1; run 25 ) &
( run 5; run 100 ) &
wait
echo all > $E/all.done
