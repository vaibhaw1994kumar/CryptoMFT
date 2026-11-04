#!/bin/bash
# Phase-1 capture replays: perp_perp_maker_taker_v1, singapore, clean, seed 1, hedge capture ON. mm fees on 01-05..01-10 and 01-13, vip0 on 01-05 and 01-13.
# 2 at a time. Training = legacy_v0 clean replays of strictly earlier days (out/e2e_perp, read-only), as in the e2e runs.
X=/Users/vaibhav/Documents/Newproject/out/exec_layer; LABEL=${1:-cap}; SNAP=${2:-$X/snap_cap}
export EXEC_SNAP=$SNAP EXEC_OUT=$X/$LABEL EXEC_CAPTURE=1
cd $X; mkdir -p logs
WHY="exploratory: partial Binance coverage"
tr() { local d=$1; local out=""; for x in 2022-01-05 2022-01-06 2022-01-07 2022-01-08 2022-01-09 2022-01-10; do [[ "$x" < "$d" ]] && out="$out,$x"; done; echo "${out#,}"; }
run() { local fee=$1 day=$2; local t=$(tr $day); local a=(perp_perp_maker_taker_v1 $day --fee $fee --allow-rejected-data "$WHY"); [ -n "$t" ] && a+=(--train "$t");
  nice -n 5 python3.12 exec_run.py "${a[@]}" > logs/${LABEL}_${fee}_$day.log 2>&1; echo "$(date +%T) rc=$? $LABEL $fee $day" >> logs/${LABEL}_pool.log; }
echo "$(date +%T) start $LABEL snap=$SNAP" >> logs/${LABEL}_pool.log
( run bybit_perp_mm 2022-01-13; run bybit_perp_mm 2022-01-06; run bybit_perp_mm 2022-01-08; run bybit_perp_mm 2022-01-10; run bybit_perp_vip0 2022-01-13 ) &
( run bybit_perp_mm 2022-01-05; run bybit_perp_mm 2022-01-07; run bybit_perp_mm 2022-01-09; run bybit_perp_vip0 2022-01-05 ) &
wait
echo "$(date +%T) done $LABEL" >> logs/${LABEL}_pool.log; touch logs/$LABEL.done
