#!/bin/bash
# 2i item 10, second batch: the five dense variants with the audit's reject reasons, lost timeouts and owed queries; the disconnect variant opens a 30 s outage at each amend's arrival.
cd /Users/vaibhav/Documents/Newproject/out/integration/day1
for c in 2i_v110_dense2 2i_v110_dense2_reject 2i_v110_dense2_timeout 2i_v110_dense2_dup_filled 2i_v110_dense2_disconnect; do
  python3.12 -I scenarios.py $c > ../2i_fixes/$c.log 2>&1 &
done
wait
echo done > ../2i_fixes/item10_batch2.done
