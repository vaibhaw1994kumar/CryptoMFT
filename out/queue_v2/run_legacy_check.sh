#!/bin/bash
# legacy_v0 default-behaviour identity check on the queue-v2 snapshot: 2022-01-05 and 2022-01-13 vs the Phase 0 gate runs (out/exec_layer/p0i).
X=/Users/vaibhav/Documents/Newproject/out/queue_v2
REASON="exploratory: Binance trade tape truncated at ~17 h on 2022-01-05 and 2022-01-13 (queue_v2 manifest); Bybit maker fills use the Bybit tape"
for d in 2022-01-05 2022-01-13; do
  ( EXEC_SNAP=$X/snap_qv2g EXEC_OUT=$X/runs_final4 EXEC_TAG=legacy_check QV2_MAKER='{}' QV2_CFG='{}' nice -n 5 python3.12 $X/qv2_run.py legacy_v0 $d --allow-rejected-data "$REASON" > $X/logs/run_legacy_check_$d.log 2>&1; echo "rc $? legacy $d" >> $X/logs/legacy_check.log ) &
done
( EXEC_SNAP=$X/snap_qv2g EXEC_OUT=$X/runs_final4 EXEC_TAG=v1_check QV2_MAKER='{"opposite_cross_fills": false}' QV2_CFG='{}' nice -n 5 python3.12 $X/qv2_run.py perp_perp_maker_taker_v1 2022-01-05 --allow-rejected-data "$REASON" > $X/logs/run_v1_check_2022-01-05.log 2>&1; echo "rc $? v1 2022-01-05" >> $X/logs/legacy_check.log ) &
wait; touch $X/logs/legacy_check.done
