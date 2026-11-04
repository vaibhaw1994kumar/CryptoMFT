#!/bin/bash
# Resumable remainder of the capture replays (same snapshot snap_cap, same settings as pool_capture.sh). Skips a run whose hedge_capture exists.
X=/Users/vaibhav/Documents/Newproject/out/exec_layer; LABEL=cap; SNAP=$X/snap_cap
export EXEC_SNAP=$SNAP EXEC_OUT=$X/$LABEL EXEC_CAPTURE=1
cd $X; WHY="exploratory: partial Binance coverage"
tr() { local d=$1; local out=""; for x in 2022-01-05 2022-01-06 2022-01-07 2022-01-08 2022-01-09 2022-01-10; do [[ "$x" < "$d" ]] && out="$out,$x"; done; echo "${out#,}"; }
run() { local fee=$1 day=$2; [ -f $X/$LABEL/perp_perp_maker_taker_v1/singapore_${fee}_clean_s1/$day/hedge_capture.json.gz ] && return
  local t=$(tr $day); local a=(perp_perp_maker_taker_v1 $day --fee $fee --allow-rejected-data "$WHY"); [ -n "$t" ] && a+=(--train "$t")
  nice -n 5 python3.12 exec_run.py "${a[@]}" > logs/${LABEL}_${fee}_$day.log 2>&1; echo "$(date +%T) rc=$? $LABEL $fee $day" >> logs/${LABEL}_pool.log; }
echo "$(date +%T) restart $LABEL (resumable)" >> logs/${LABEL}_pool.log
( run bybit_perp_mm 2022-01-06; run bybit_perp_vip0 2022-01-13 ) &
( run bybit_perp_mm 2022-01-08; run bybit_perp_vip0 2022-01-05 ) &
( run bybit_perp_mm 2022-01-09; run bybit_perp_mm 2022-01-10 ) &
wait
echo "$(date +%T) done $LABEL" >> logs/${LABEL}_pool.log; touch logs/$LABEL.done
