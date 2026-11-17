#!/bin/bash
# one rehearsal run: run_one.sh NAME PROFILE [run_day flags...]
cd /Users/vaibhav/Documents/Newproject
D=out/integration/rehearsal_20261117
N=$1; P=$2; shift 2
python3.12 -I out/integration/day1/run_day.py $P $D/$N "$@" > $D/$N.log 2>&1; echo done > $D/$N.done
