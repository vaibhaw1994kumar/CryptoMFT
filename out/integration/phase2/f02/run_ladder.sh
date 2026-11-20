#!/bin/bash
# Phase 2 F02 b (owner 20 Nov): the SOLUSDT size ladder on the full 29 Oct 2026 day, v1_17, pair close ON (exits exercised), 3 runs at a time. EXPLORATORY (the capture is REJECTED / CRITICAL: mechanics only).
# Parents 1k / 5k / 10k / 20k / 30k / 50k USDT at 98 USDT per SOL. Series i: child = parent (capped at the 25,000 USDT clip cap, unchanged). Series ii: child held at 1k (10.2 SOL) while the parent rises.
# Capacity follows the rung: arb.max_position_base (parent, net ceiling 1.25x), gross per venue 2 x parent. Capital per venue = max(1000, 15 x IM(2 x parent notional)) at the ASSUMED tiers: capital, not a risk limit.
# Risk limits stay at their v1_17 values (order rate, hedge chain, dust bound, margin multiple, missing-mark kill, clock gate, participation 10%); the daily loss stop keeps its per-SOL-of-clip rule.
cd /Users/vaibhav/Documents/Newproject
W=/Users/vaibhav/Documents/Newproject/out/integration/phase2
L=$W/f02/ladder
RB=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/config/regime_boundaries_day1_2026-10-29_insample.json
until [ -f $W/g3_p2.done ] && [ -f $W/f06_close_on_v117.done ]; do sleep 20; done
run() {
  name=$1; size=$2; child=$3; cap=$4
  gross=$(python3.12 -c "print(round(2*$size,1))")
  extra=""; [ "$child" != "-" ] && extra="--set arb.max_clip_base=$child"
  python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_17 $L/$name --size $size --gross $gross --hedge-min-notional 5 --regime-boundaries $RB \
    --set arb.close_pairs=reduce_only_quote --set arb.close_reservation=true --set arb.close_unfilled_after_s=60 \
    --set margin.venues.bybit.initial_capital=$cap --set margin.venues.binance.initial_capital=$cap $extra > $L/$name.log 2>&1
  echo done > $L/$name.done
}
export -f run; export L RB
cat <<'LIST' | xargs -P 3 -L 1 bash -c 'run $0 $1 $2 $3'
i_01k 10.2 - 1000
i_05k 51.0 - 3000
i_10k 102.0 - 6000
i_20k 204.1 - 12000
i_30k 306.1 - 45000
i_50k 510.2 - 75000
ii_05k 51.0 10.2 3000
ii_10k 102.0 10.2 6000
ii_20k 204.1 10.2 12000
ii_30k 306.1 10.2 45000
ii_50k 510.2 10.2 75000
LIST
echo all > $L/all.done
