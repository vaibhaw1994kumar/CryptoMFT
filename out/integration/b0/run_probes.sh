#!/bin/zsh
# B0.2 / queue evidence: the EXISTING probe (out/queue_v2/qv2_probe.py, unchanged logic) on the LIVE tree. 2024-03-01 is the identity check against the stored frozen-snapshot result.
export QV2_SNAP=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
cd /Users/vaibhav/Documents/Newproject/out/queue_v2
run() { QV2_PROBE_OUT=/Users/vaibhav/Documents/Newproject/out/integration/b0/probe/$1 nice -n 5 python3.12 -I qv2_probe.py $2 > /Users/vaibhav/Documents/Newproject/out/integration/b0/probe_$2.log 2>&1; }
run train_check 2024-03-01 &
run select 2024-03-08 &
wait
run select 2024-03-09 &
run select 2024-03-10 &
wait
run select 2024-03-11
date +%T
