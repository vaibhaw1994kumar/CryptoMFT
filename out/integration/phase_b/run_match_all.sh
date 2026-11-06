#!/bin/bash
# Phase B trade/depth matching study: TRAIN days only, 00:00-06:00 UTC of each day; read-only; two at a time.
cd /Users/vaibhav/Documents/Newproject/out/integration/phase_b
export MATCH_OUT=${MATCH_OUT:-match}
mkdir -p $MATCH_OUT/logs
jobs_list=()
for d in 2024-03-01 2024-03-02 2024-03-03 2024-03-04 2024-03-05 2024-03-06 2024-03-07; do jobs_list+=("mar2024 bybit $d"); done
for d in 2022-01-05 2022-01-06 2022-01-07 2022-01-08 2022-01-09 2022-01-10; do jobs_list+=("jan2022 bybit $d"); jobs_list+=("jan2022 binance $d"); done
printf '%s\n' "${jobs_list[@]}" | xargs -P 2 -L 1 bash -c 'nice -n 8 python3.12 -I trade_depth_match.py $0 $1 $2 6 >> $MATCH_OUT/logs/$0_$1_$2.log 2>&1'
date +%T > $MATCH_OUT/done
