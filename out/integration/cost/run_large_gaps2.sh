#!/bin/bash
# LARGE_GAPS_RULES.md step 4b: repeated hedges with the core overlay variants (MODELLED), sequences only (no 1 s T1 decisions): capture both venues, March 03-01 and 03-08 (TRAIN / SELECT; HOLDOUT NOT READ), ADA and MNT one window each. One job at a time.
cd /Users/vaibhav/Documents/Newproject
D=out/integration/cost
bash $D/run_followup_analyze.sh > $D/t1_analysis_lg.log 2>&1   # step 6 splits (the first chain died with the machine restart)
mkdir -p $D/t1ov
python3.12 -I $D/t5_split.py large2 --out t5l2 > $D/t5l2/log_large2.txt 2>&1
echo large2_done > $D/t5l2.done
T="python3.12 -I $D/t1_hedge_cost.py --fracs 1.0 --max-levels 500 --seq --seq-ov --no-decisions --out t1ov"
$T capture > $D/t1ov/log_capture.txt 2>&1
$T march 2024-03-01 > $D/t1ov/log_march_01.txt 2>&1
$T march 2024-03-08 > $D/t1ov/log_march_08.txt 2>&1
$T archive ADAUSDT 2026-04-01 0 > $D/t1ov/log_ada.txt 2>&1
$T archive MNTUSDT 2026-04-01 0 > $D/t1ov/log_mnt.txt 2>&1
A="python3.12 -I $D/t1_analyze.py --dir t1ov --seq-only"
$A TRAIN march_2024-03-01 > $D/t1ov/analysis_TRAIN.txt 2>&1
$A SELECT march_2024-03-08 > $D/t1ov/analysis_SELECT.txt 2>&1
$A CAPTURE_BYBIT capture_bybit > $D/t1ov/analysis_CAPTURE_BYBIT.txt 2>&1
$A CAPTURE_BINANCE capture_binance > $D/t1ov/analysis_CAPTURE_BINANCE.txt 2>&1
$A ADA archive_ADAUSDT_2026-04-01_h00 > $D/t1ov/analysis_ADA.txt 2>&1
$A MNT archive_MNTUSDT_2026-04-01_h00 > $D/t1ov/analysis_MNT.txt 2>&1
echo done > $D/t1ov.done
