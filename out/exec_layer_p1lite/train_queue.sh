#!/bin/zsh
# legacy_v0 TRAIN-day replays (03-02..03-07; 03-01 already running), one worker per invocation, days taken from a shared list.
cd /Users/vaibhav/Documents/Newproject/out/exec_layer_p1lite
while true; do
  d=$(python3 -c "
import fcntl,sys
f=open('train_todo.txt','r+'); fcntl.flock(f,fcntl.LOCK_EX); L=[l for l in f.read().split() if l]
print(L[0] if L else ''); f.seek(0); f.truncate(); f.write(' '.join(L[1:]))")
  [ -z "$d" ] && break
  /usr/bin/time -l python3.12 -I p1_run.py legacy_v0 $d --group train_legacy --cell legacy_v0 > logs/train_legacy_$d.log 2>&1
done
