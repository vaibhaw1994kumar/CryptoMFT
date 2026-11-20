#!/bin/bash
# Phase 2 F02 b, owner 20 Nov (follow-up): series ii dropped (owner: "cut the ladder to series i only"); the remaining series-i rungs run at once, alongside the three already running.
cd /Users/vaibhav/Documents/Newproject
L=/Users/vaibhav/Documents/Newproject/out/integration/phase2/f02/ladder
RB=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/config/regime_boundaries_day1_2026-10-29_insample.json
run() {
  name=$1; size=$2; cap=$3; gross=$(python3.12 -c "print(round(2*$size,1))")
  python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_17 $L/$name --size $size --gross $gross --hedge-min-notional 5 --regime-boundaries $RB \
    --set arb.close_pairs=reduce_only_quote --set arb.close_reservation=true --set arb.close_unfilled_after_s=60 \
    --set margin.venues.bybit.initial_capital=$cap --set margin.venues.binance.initial_capital=$cap > $L/$name.log 2>&1
  echo done > $L/$name.done
}
run i_20k 204.1 12000 &
run i_30k 306.1 45000 &
run i_50k 510.2 75000 &
wait
