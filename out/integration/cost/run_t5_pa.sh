#!/bin/bash
# FOLLOWUP_RULES.md item 2 (price-aware fills): outflow ratios from the TRAIN prints, random-moment run 03-01..12 (t5e: SELECT chooses the cell), fill-moment run (t5d).
cd /Users/vaibhav/Documents/Newproject
D=out/integration/cost
P="python3.12 -I $D/t5_split.py --price-aware"
$P --out t5c ratios 2024-03-01 2024-03-02 2024-03-03 2024-03-04 2024-03-05 2024-03-06 2024-03-07 > $D/t5c/log_ratios_pa.txt 2>&1
$P --out t5d --times $D/t5d/times.json run 2024-03-01 2024-03-02 2024-03-03 2024-03-04 2024-03-05 2024-03-06 2024-03-07 2024-03-08 2024-03-09 2024-03-10 2024-03-11 > $D/t5d/log_run.txt 2>&1
$P --out t5e run 2024-03-01 2024-03-02 2024-03-03 2024-03-04 2024-03-05 2024-03-06 2024-03-07 > $D/t5e/log_a.txt 2>&1
$P --out t5e run 2024-03-08 2024-03-09 2024-03-10 2024-03-11 2024-03-12 > $D/t5e/log_b.txt 2>&1
echo done > $D/t5_pa.done
