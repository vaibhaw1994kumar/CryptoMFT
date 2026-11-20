#!/bin/bash
# Phase 2 order, "G1 for Phase 1": the four extra Phase 1 failures run alone, serially, three times each (pass or fail per run);
# stop if any fails alone; otherwise the full suite serially once against the 93 baseline.
W=/Users/vaibhav/Documents/Newproject/out/integration/phase2
cd /Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
T1='tests/platform_kit/test_minreq_execution.py::test_performance_limits_are_enforced_on_the_target_platform_only[Darwin-True]'
T2='tests/platform_kit/test_minreq_execution.py::test_performance_limits_are_enforced_on_the_target_platform_only[Linux-False]'
T3='tests/platform_kit/test_minreq_execution.py::test_performance_limits_are_enforced_on_the_target_platform_only[Windows-True]'
T4='tests/test_live_feed_a9.py::ArrivalStamp::test_a_processor_that_keeps_up_changes_nothing'
: > $W/g1_alone.tsv
FAILED=0
for T in "$T1" "$T2" "$T3" "$T4"; do
  for RUN in 1 2 3; do
    PYTHONPATH=src python3.12 -m pytest "$T" -q -p no:cacheprovider > $W/g1_alone_last.log 2>&1
    RC=$?
    R=$([ $RC -eq 0 ] && echo PASS || echo FAIL)
    [ $RC -ne 0 ] && FAILED=1 && cp $W/g1_alone_last.log "$W/g1_alone_fail_$(echo $T | tr '/:[]' '____')_run$RUN.log"
    printf "%s\t%s\t%s\t%s\n" "$T" "$RUN" "$R" "$(tail -1 $W/g1_alone_last.log)" >> $W/g1_alone.tsv
  done
done
if [ $FAILED -ne 0 ]; then echo "STOP: a test failed alone" > $W/g1_phase1.done; exit 1; fi
START=$(date +%s)
PYTHONPATH=src python3.12 -m pytest tests -q -p no:cacheprovider -rfE --continue-on-collection-errors > $W/g1_suite_serial.log 2>&1
END=$(date +%s)
python3.12 scripts/compare_baselines.py tests $W/g1_suite_serial.log > $W/g1_compare_baselines_serial.txt 2>&1
echo "serial suite wall_s $((END-START))" > $W/g1_phase1.done
