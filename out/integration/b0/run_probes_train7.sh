#!/bin/zsh
# B0 follow-up item 3: the EXISTING probe (out/queue_v2/qv2_probe.py, unchanged) on the 00:00-02:00 UTC slice of each of the 7 TRAIN days (03-01..03-07), live tree. Two at a time.
export QV2_SNAP=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
cd /Users/vaibhav/Documents/Newproject/out/queue_v2
O=/Users/vaibhav/Documents/Newproject/out/integration/b0/probe_train7
mkdir -p $O
run() { QV2_PROBE_OUT=$O nice -n 5 python3.12 -I qv2_probe.py $1 > $O/probe_$1.log 2>&1; }
run 2024-03-01 & run 2024-03-02 & wait
run 2024-03-03 & run 2024-03-04 & wait
run 2024-03-05 & run 2024-03-06 & wait
run 2024-03-07
date +%T > $O/done
