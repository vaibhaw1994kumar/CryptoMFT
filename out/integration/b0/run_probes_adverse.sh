#!/bin/zsh
# Owner 7 Nov adverse-selection report: the EXISTING probe (out/queue_v2/qv2_probe.py, with the observation-only block_on_toxic hook) on the 7 TRAIN days, v1_2 settings (March dataset_settings applied),
# through rule off. 4 jobs at a time.
export QV2_SNAP=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
export QV2_PROBE_PROFILE=perp_perp_maker_taker_v1_2
cd /Users/vaibhav/Documents/Newproject/out/queue_v2
O=/Users/vaibhav/Documents/Newproject/out/integration/b0/probe_adverse
mkdir -p $O
run() { [ -f $O/$1.json ] || QV2_PROBE_OUT=$O nice -n 5 python3.12 -I qv2_probe.py $1 > $O/probe_$1.log 2>&1; }
run 2024-03-01 & run 2024-03-02 & run 2024-03-03 & run 2024-03-04 & wait
run 2024-03-05 & run 2024-03-06 & run 2024-03-07 & wait
date +%T > $O/done
