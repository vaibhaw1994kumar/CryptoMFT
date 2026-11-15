#!/bin/bash
# Step C large-size replays: v1_9, 2026-10-29 capture, participation limit OFF (labelled), gross 5 x size, overlay off vs on (ASSUMED phi 0.25, tau 30 s, fitted peak curve). Frozen snapshot snapC.
cd /Users/vaibhav/Documents/Newproject
D=out/integration/cost
SRC=$PWD/$D/snapC/crypto_MFT_arb_platform/src
run() { name=$1; shift; python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_9 $D/$name "$@" --src $SRC > $D/$name.log 2>&1; echo done > $D/$name.done; }
( run identityC_1sol --size 1 --gross 5; run impact_1000_off --size 1000 --gross 5000 --participation-max 0; run impact_1000_on --size 1000 --gross 5000 --participation-max 0 --impact $D/impact_overlay_settings.json; run impact_5000_off --size 5000 --gross 25000 --participation-max 0 ) &
( run impact_100_off --size 100 --gross 500 --participation-max 0; run impact_100_on --size 100 --gross 500 --participation-max 0 --impact $D/impact_overlay_settings.json; run impact_5000_on --size 5000 --gross 25000 --participation-max 0 --impact $D/impact_overlay_settings.json ) &
wait
echo all > $D/impact_sweep.done
