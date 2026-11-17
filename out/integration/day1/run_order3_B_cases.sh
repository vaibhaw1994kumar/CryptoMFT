#!/bin/bash
cd /Users/vaibhav/Documents/Newproject/out/integration/day1
for c in 3g_close_pairs_on_v110; do
  python3.12 -I scenarios.py $c > runs_$c.log 2>&1; echo done > runs_$c.done
done
echo all > runs_order3_B_cases.done
