#!/bin/zsh
# Bybit rebuild acceptance, one day at a time (serial), March 1-15 2024.
cd /Users/vaibhav/Documents/Newproject/out/exec_layer_p1lite
for d in 02 03 04 05 06 07 08 09 10 11 12 13 14 15; do
  python3.12 -I snap/scripts/queue_v2_rebuild_acceptance.py --data-root /Users/vaibhav/Documents/Newproject/data --venue bybit --day 2024-03-$d --out-dir step0/rebuild_acceptance >> logs/acceptance.log 2>&1
done
echo DONE >> logs/acceptance.log
