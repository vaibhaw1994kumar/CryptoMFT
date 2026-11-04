#!/bin/zsh
# Start the SELECT grid once all 7 TRAIN summaries exist: 3 workers (8 GB Mac, ~1.7 GB peak per replay: a 4th would swap).
cd /Users/vaibhav/Documents/Newproject/out/exec_layer_p1lite
until [ $(ls runs/train_legacy/legacy_v0/*/summary.json 2>/dev/null | wc -l) -ge 7 ]; do
  ps -Ao command | grep -q "[p]1_run.py legacy_v0" || { echo "training workers gone without 7 summaries"; exit 1; }
  sleep 120
done
date; echo "train complete; starting select workers"
for i in 1 2 3; do nohup ./grid_worker.sh select_queue.txt > /dev/null 2>&1 & sleep 5; done
