#!/bin/bash
# 2i item 10: the denser arb 2i case on v1_10 (pair-close switch on for the case, labelled) and its fault variants, 2-hour window A, in parallel.
cd /Users/vaibhav/Documents/Newproject/out/integration/day1
for c in 2i_v110_dense2 2i_v110_dense2_reject 2i_v110_dense2_timeout 2i_v110_dense2_dup_filled; do
  python3.12 -I scenarios.py $c > ../2i_fixes/$c.log 2>&1 &
done
wait
echo done > ../2i_fixes/item10_batch1.done
