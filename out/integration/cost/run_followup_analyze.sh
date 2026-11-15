#!/bin/bash
# FOLLOWUP_RULES.md: the analyses after the runs. HOLDOUT (03-13..15) is NOT read.
cd /Users/vaibhav/Documents/Newproject
D=out/integration/cost
A="python3.12 -I $D/t1_analyze.py --extra"
F="python3.12 -I $D/t1_fit.py --fracs 0.25,0.5,1.0,2.0,4.0"
TR=""; for d in 01 02 03 04 05 06 07; do TR="$TR march_2024-03-$d"; done
SE=""; for d in 08 09 10 11 12; do SE="$SE march_2024-03-$d"; done
TRC=$(echo $TR | tr ' ' ',')
$A --dir t1b --vol-from $TRC TRAIN $TR > $D/t1b/analysis_TRAIN.txt 2>&1
$A --dir t1b --vol-from $TRC SELECT $SE > $D/t1b/analysis_SELECT.txt 2>&1
$A --dir t1b --vol-half CAPTURE_BYBIT capture_bybit > $D/t1b/analysis_CAPTURE_BYBIT.txt 2>&1
$A --dir t1b --vol-half CAPTURE_BINANCE capture_binance > $D/t1b/analysis_CAPTURE_BINANCE.txt 2>&1
$F --dir t1b TRAINSELECT_bybit $TR $SE
$F --dir t1b CAPTURE_bybit capture_bybit
$F --dir t1b CAPTURE_binance capture_binance
for C in ADAUSDT MNTUSDT; do
  FIRST=""; SECOND=""
  for d in 01 03 05 07; do for h in 00 13; do FIRST="$FIRST archive_${C}_2026-04-${d}_h${h}"; done; done
  for d in 09 11 13; do for h in 00 13; do SECOND="$SECOND archive_${C}_2026-04-${d}_h${h}"; done; done
  FC=$(echo $FIRST | tr ' ' ',')
  $A --dir t1c --vol-from $FC ${C}_FIRST $FIRST > $D/t1c/analysis_${C}_FIRST.txt 2>&1
  $A --dir t1c --vol-from $FC ${C}_SECOND $SECOND > $D/t1c/analysis_${C}_SECOND.txt 2>&1
  $F --dir t1c ${C}_ALL $FIRST $SECOND
done
echo done > $D/t1_analysis.done
