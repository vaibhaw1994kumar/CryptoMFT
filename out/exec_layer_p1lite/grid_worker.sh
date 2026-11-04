#!/bin/zsh
# Generic Phase 1-lite worker: takes one line from the shared queue file ($1), runs p1_run.py with that line's arguments.
# Each queue line: "<day> <group> <cell> <extra p1_run args...>"; profile is v1_1 (owner decision 2026-11-04, evening; replaces v1), TRAIN = 03-01..03-07.
cd /Users/vaibhav/Documents/Newproject/out/exec_layer_p1lite
Q=$1
TRAIN=2024-03-01,2024-03-02,2024-03-03,2024-03-04,2024-03-05,2024-03-06,2024-03-07
while true; do
  line=$(python3 -c "
import fcntl,sys
f=open('$Q','r+'); fcntl.flock(f,fcntl.LOCK_EX); L=[l for l in f.read().splitlines() if l.strip()]
print(L[0] if L else ''); f.seek(0); f.truncate(); f.write('\n'.join(L[1:]) + ('\n' if L[1:] else ''))")
  [ -z "$line" ] && break
  args=(${=line})
  d=${args[1]}; g=${args[2]}; c=${args[3]}
  [ -f runs/$g/$c/$d/summary.json ] && continue
  /usr/bin/time -l python3.12 -I p1_run.py perp_perp_maker_taker_v1_1 $d --group $g --cell $c --train $TRAIN ${args[4,-1]} > logs/${g}_${c}_$d.log 2>&1
done
