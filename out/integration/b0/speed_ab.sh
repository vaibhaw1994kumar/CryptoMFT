#!/bin/bash
# B0 follow-up item 2: same tree with and without the queue_updates counter, solo, v1.1 on the 2024-03-11 2-hour slice. Order with, without, with, without.
# Trees are scratch copies (src, profiles, config); "without" has the three @_count_queue_updates decorators removed from simulation/fill_engine.py.
S=/private/tmp/claude-501/-Users-vaibhav-Documents-Newproject-FAST-MM/9fa278c5-4c41-4529-9543-aae9f7bf14d4/scratchpad/ab
B=/Users/vaibhav/Documents/Newproject/out/integration/baselines
for rep in 1 2; do
  for t in with without; do
    s=$(date +%s)
    BASELINE_SNAP=$S/$t BASELINE_ONLY='re:mar/perp_perp_maker_taker_v1_1/2024-03-11' python3.12 -I $B/run_baselines.py spd_${t}_$rep 1 > $B/logs/orchestrator_spd_${t}_$rep.log 2>&1
    echo "$t rep$rep wall_total $(( $(date +%s) - s )) s" >> $B/logs/speed_ab.log
  done
done
date +%T > $B/logs/speed_ab.done
