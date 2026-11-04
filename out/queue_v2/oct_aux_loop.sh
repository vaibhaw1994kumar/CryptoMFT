#!/bin/bash
# runs oct_day_features.py for each Oct day whose proportional run is complete, one at a time, only while fewer than 4 replay jobs are running (guardrail: max 4 parallel jobs)
X=/Users/vaibhav/Documents/Newproject/out/queue_v2
cd $X
while true; do
  todo=0
  for d in 2024-10-{01..15}; do
    [ -f oct_aux/proportional/$d.json ] && continue
    todo=1
    [ -f maker_only/oct/proportional/$d/summary.json ] || continue
    n=$(ps -axo stat,command | grep -E "qv2_run.py|qv2_maker_only.py|oct_day_features.py" | grep -v grep | grep -vc "^T")
    [ "$n" -lt 4 ] || continue
    python3.12 oct_day_features.py $d proportional >> logs/oct_aux.log 2>&1
  done
  [ $todo -eq 0 ] && break
  sleep 30
done
touch logs/oct_aux.done
