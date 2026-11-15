#!/bin/bash
# FOLLOWUP_RULES.md 1a, 1b, 3: the T1 queue, two jobs at a time (8 GB RAM). Capture first (longest), then March TRAIN + SELECT (03-01..12; HOLDOUT NOT READ), then ADA / MNT archive windows.
cd /Users/vaibhav/Documents/Newproject
D=out/integration/cost
xargs -P 2 -I CMD bash -c 'CMD' < $D/followup_t1_jobs.txt
echo done > $D/t1_followup.done
