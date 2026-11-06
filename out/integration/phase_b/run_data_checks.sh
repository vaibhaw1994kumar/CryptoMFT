#!/bin/zsh
# Phase B data checks over 01-05, 01-13 (full days) and the four March slices, both venues. Three at a time.
cd /Users/vaibhav/Documents/Newproject/out/integration/phase_b
jobs_list=(
 "jan2022 bybit 2022-01-13" "jan2022 binance 2022-01-13" "jan2022 bybit 2022-01-05" "jan2022 binance 2022-01-05"
 "mar2024 bybit 2024-03-08" "mar2024 bybit 2024-03-09" "mar2024 bybit 2024-03-10" "mar2024 bybit 2024-03-11"
 "mar2024 binance 2024-03-08" "mar2024 binance 2024-03-09" "mar2024 binance 2024-03-10" "mar2024 binance 2024-03-11" )
i=0
for j in $jobs_list; do
  nice -n 5 python3.12 -I run_data_checks.py ${=j} > data_checks/log_${j// /_}.txt 2>&1 &
  i=$((i+1)); if (( i % 3 == 0 )); then wait; fi
done
wait
date +%T > data_checks/done
