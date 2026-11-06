#!/bin/zsh
# Step 8 re-check (owner 7 Nov): the EXISTING probe (out/queue_v2/qv2_probe.py) on the 00:00-02:00 UTC slice of the 7 TRAIN days 03-01..03-07, v1_2 settings, live tree,
# through-print rule off (the profile as it stands) and on (fill_through_prints + through_print_volume_limited by labelled override). 4 jobs at a time. EXPLORATORY.
export QV2_SNAP=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
export QV2_PROBE_PROFILE=perp_perp_maker_taker_v1_2
cd /Users/vaibhav/Documents/Newproject/out/queue_v2
O=/Users/vaibhav/Documents/Newproject/out/integration/b0/probe_through
mkdir -p $O/off $O/on
job() {   # cell day
  local cell=$1 day=$2
  [ -f $O/$cell/$day.json ] && return 0
  if [ $cell = on ]; then export QV2_PROBE_MAKER='{"fill_through_prints": true, "through_print_volume_limited": true}'; else unset QV2_PROBE_MAKER; fi
  QV2_PROBE_OUT=$O/$cell nice -n 5 python3.12 -I qv2_probe.py $day > $O/probe_${cell}_$day.log 2>&1
}
typeset -a pids
for day in 2024-03-01 2024-03-02 2024-03-03 2024-03-04 2024-03-05 2024-03-06 2024-03-07; do
  for cell in off on; do
    while [ ${#${(M)pids:#*}} -ge 4 ]; do
      pids=(${(@)pids:#}); alive=(); for p in $pids; do kill -0 $p 2>/dev/null && alive+=($p); done; pids=($alive); [ ${#pids} -ge 4 ] && sleep 5
    done
    job $cell $day & pids+=($!)
  done
done
wait
date +%T > $O/done
