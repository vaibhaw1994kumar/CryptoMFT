#!/bin/bash
# 2022-01-05 funding report (owner 8 Nov): January cells fs (v1.1 + message_atomic_book, funding by the settler) and jl (same, funding + margin + ledger from the core), full day, no hedge-move file.
L=/Users/vaibhav/Documents/Newproject/out/integration/phase_b
B=/Users/vaibhav/Documents/Newproject/out/integration/baselines
LEDGER_OUT=$L/ledger_fund0105 LEDGER_JAN_DAY=2022-01-05 python3.12 -I $L/run_ledger_cells.py 2 jan fs jl > $B/logs/orchestrator_b2f.log 2>&1
date +%T > $B/logs/after_b2f.done
