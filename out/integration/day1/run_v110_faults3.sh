#!/bin/bash
# Order 2: the case 2i fault variants on v1_10 and the size-25 case again, after the PARTIAL-state fix. One heavy job at a time.
cd /Users/vaibhav/Documents/Newproject/out/integration/day1
for c in 2i_v110_reject 2i_v110_timeout 2i_v110_disconnect 2i_v110_dup_filled 2n_size25_v110; do
  python3.12 -I scenarios.py $c > runs_$c.log 2>&1; echo done3 > runs_$c.done3
done
echo all > runs_2i_faults3.done
