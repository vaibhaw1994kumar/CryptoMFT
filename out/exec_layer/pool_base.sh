#!/bin/bash
# Gate-0 baseline: 4 pre-refactor replays (legacy_v0, v1 x 2022-01-05, 2022-01-13), singapore latency, bybit_perp_mm, clean, seed 1.
# 2 at a time (the other session's band.sh uses 2 cores). Only this script's own PIDs.
X=/Users/vaibhav/Documents/Newproject/out/exec_layer
LABEL=${1:-base}; SNAP=${2:-$X/snap_base}
export EXEC_SNAP=$SNAP EXEC_OUT=$X/$LABEL
cd $X; mkdir -p logs
T13=2022-01-05,2022-01-06,2022-01-07,2022-01-08,2022-01-09,2022-01-10
run() { local tag=$1; shift; nice -n 5 python3.12 exec_run.py "$@" > logs/${LABEL}_$tag.log 2>&1; echo "$(date +%T) rc=$? $LABEL $tag" >> logs/${LABEL}_pool.log; }
echo "$(date +%T) start $LABEL snap=$SNAP" >> logs/${LABEL}_pool.log
( run leg_0105 legacy_v0 2022-01-05 --allow-rejected-data "exploratory: partial Binance coverage"; run v1_0105 perp_perp_maker_taker_v1 2022-01-05 --allow-rejected-data "exploratory: partial Binance coverage" ) &
( run leg_0113 legacy_v0 2022-01-13 --allow-rejected-data "exploratory: partial Binance coverage"; run v1_0113 perp_perp_maker_taker_v1 2022-01-13 --train $T13 --allow-rejected-data "exploratory: partial Binance coverage" ) &
wait
echo "$(date +%T) done $LABEL" >> logs/${LABEL}_pool.log; touch logs/${LABEL}.done
