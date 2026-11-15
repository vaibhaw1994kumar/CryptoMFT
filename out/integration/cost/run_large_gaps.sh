#!/bin/bash
# LARGE_GAPS_RULES.md steps 3, 4c, 5, 6: one heavy job at a time. Waits for the identity gate first. HOLDOUT (03-13..15) is NOT read.
cd /Users/vaibhav/Documents/Newproject
D=out/integration/cost
until [ -f $D/identityL_1sol.done ]; do sleep 10; done
P="python3.12 -I $D/t5_split.py"
DAYS="2024-03-01 2024-03-02 2024-03-03 2024-03-04 2024-03-05 2024-03-06 2024-03-07 2024-03-08 2024-03-09 2024-03-10 2024-03-11"
ALLD="$DAYS 2024-03-12"
$P worst --out t5w > $D/t5w/log_worst.txt 2>&1; echo worst_done > $D/t5w.done
# step 3: lambda_u from CVaR, random moments (pa fills), then the probe's fill moments
$P --price-aware --out t5p --lam-file $D/t5w/lambdas.json run $ALLD > $D/t5p/log_run.txt 2>&1
$P --price-aware --out t5q --lam-file $D/t5w/lambdas.json --times $D/t5c/times.json run $DAYS > $D/t5q/log_run.txt 2>&1
# step 5: the pessimistic floor, same cells
$P --price-aware --pessimistic --out t5r --lam-file $D/t5w/lambdas.json run $ALLD > $D/t5r/log_run.txt 2>&1
$P --price-aware --pessimistic --out t5s --lam-file $D/t5w/lambdas.json --times $D/t5c/times.json run $DAYS > $D/t5s/log_run.txt 2>&1
echo march_done > $D/t5_march.done
# steps 4c and 5 at 100 / 500 / 1000 SOL on the capture
$P large2 --out t5l2 > $D/t5l2/log_large2.txt 2>&1
echo large2_done > $D/t5l2.done
# step 6: T1 splits (flow, after a fill) with the extra analyses
bash $D/run_followup_analyze.sh > $D/t1_analysis_lg.log 2>&1
echo all_done > $D/large_gaps_all.done
