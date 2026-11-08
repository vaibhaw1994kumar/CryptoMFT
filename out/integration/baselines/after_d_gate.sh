#!/bin/bash
# Phase D gate: identity on March 03-11 for the five profiles (after_d1.sh), then the 13 clean baselines against b1 and the four short faulted runs against f0 on the live tree (as after_final.sh). Usage: after_d_gate.sh TAG
B=/Users/vaibhav/Documents/Newproject/out/integration/baselines
P=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
TAG=${1:-pdg}
bash $B/after_d1.sh ${TAG}_i
BASELINE_SKIP='@(realistic|stressed)' python3.12 -I $B/run_baselines.py $TAG 3 > $B/logs/orchestrator_$TAG.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare b1 $TAG subset > $B/logs/compare_b1_$TAG.log 2>&1
BASELINE_ONLY=short python3.12 -I $B/run_baselines.py ${TAG}_f 2 > $B/logs/orchestrator_${TAG}_f.log 2>&1
python3.12 -I $P/scripts/compare_baselines.py compare f0 ${TAG}_f subset > $B/logs/compare_f0_${TAG}_f.log 2>&1
date +%T > $B/logs/after_$TAG.done
