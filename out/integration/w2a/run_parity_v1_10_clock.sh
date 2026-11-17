#!/bin/bash
# W2a: the same offline paper-vs-replay check on v1_10 with the paper side's clock gate fed an INJECTED measurement (an offline run has no server-time probe).
cd /Users/vaibhav/Documents/Newproject/out/integration
python3.12 paper_live/offline_strategy_check.py paper_live/run2/d7d-run2 60 w2a/offline_parity_run2_v1_10_clock.json perp_perp_maker_taker_v1_10 gate_first w2a/config_v1_10.json inject_clock > w2a/offline_parity_run2_v1_10_clock.out 2>&1
echo done > w2a/offline_parity_run2_v1_10_clock.done
