#!/bin/bash
# Phase 0 hardening fault evidence: conditional scenarios on 2022-01-13 with the final snapshot, v1 profile as shipped (enforce) and a
# legacy `count` control for the two ghost-relevant scenarios. 2 at a time (8 GB RAM).
X=/Users/vaibhav/Documents/Newproject/out/exec_layer; SNAP=$X/snap_p0h; OUT=$X/fault_p0h; D=2022-01-13
DATA=/Users/vaibhav/Documents/Newproject/data
cd $X; mkdir -p logs $OUT
run() { local tag=$1 name=$2 mode=$3; EXEC_TAG=$tag EXEC_MODE=$mode nice -n 5 python3.12 $SNAP/scripts/perp_fault_run.py scenario $name $D --seed 0 --size 1 --out-root $OUT --data-root $DATA > logs/fault_${name}_${tag}.log 2>&1; echo "$(date +%T) rc=$? $name $tag" >> logs/fault_pool.log; }
echo "$(date +%T) start" >> logs/fault_pool.log
( run enforce outage_binance_2s '{}'; run enforce ghost_hedge '{}'; run enforce hedge_reject_fast_move '{}' ) &
( run count outage_binance_2s '{"one_hedge_child":"count","risk_reservation":"count"}'; run count ghost_hedge '{"one_hedge_child":"count","risk_reservation":"count"}' ) &
wait
echo "$(date +%T) done" >> logs/fault_pool.log; touch logs/fault.done
