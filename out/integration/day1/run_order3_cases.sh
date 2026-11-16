#!/bin/bash
# Order 3: the limit cases on v1_10 (INJECTED conditions, labelled), one heavy job at a time. EXPLORATORY.
cd /Users/vaibhav/Documents/Newproject/out/integration/day1
for c in 3x_baseline_v110 3a_order_rate_v110 3b_loss_stop_v110 3c_clock_bad_v110 3c_clock_ok_v110 3c_clock_none_v110 3d_margin_warning_v110 3e_missing_marks_v110; do
  python3.12 -I scenarios.py $c > runs_$c.log 2>&1; echo done > runs_$c.done
done
echo all > runs_order3_cases.done
