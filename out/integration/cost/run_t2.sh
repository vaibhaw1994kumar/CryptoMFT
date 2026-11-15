#!/bin/bash
cd /Users/vaibhav/Documents/Newproject
D=out/integration/cost
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_9 $D/t2_binance_maker_100 --size 100 --gross 500 --participation-max 0 --maker-venue binance --src $PWD/$D/snapF/crypto_MFT_arb_platform/src > $D/t2_binance_maker_100.log 2>&1
echo done > $D/t2_binance_maker_100.done
