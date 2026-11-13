#!/bin/bash
cd /Users/vaibhav/Documents/Newproject
E=out/integration/edge_vwap
while [ ! -f $E/before_1sol.done ]; do sleep 5; done
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_9 $E/after_1sol --src $PWD/$E/snap_final/crypto_MFT_arb_platform/src > $E/after_1sol.log 2>&1
echo done > $E/after_1sol.done
