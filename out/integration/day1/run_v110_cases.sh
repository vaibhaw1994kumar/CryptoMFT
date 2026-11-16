#!/bin/bash
# Order 2 case runs on v1_10 (owner 16 Nov), one heavy job at a time. EXPLORATORY.
cd /Users/vaibhav/Documents/Newproject/out/integration/day1
for c in 2i_v110_dense 2i_v110_reject 2i_v110_timeout 2i_v110_disconnect 2i_v110_dup_filled 2a_v110 2b_v110 2f_v110 2m_processing_v110 2m_p99_v110 2m_p999_v110 2n_size1_v110 2n_size5_v110 2n_size25_v110; do
  python3.12 -I scenarios.py $c > runs_$c.log 2>&1; echo done > runs_$c.done
done
echo all > runs_v110_all.done
