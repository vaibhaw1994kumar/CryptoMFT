#!/bin/bash
# W2a: the offline paper-vs-replay check (both wirings on the SAME recording, no network, no orders) on v1_10, 60 minutes of the 8 Nov d7d-run2 recording.
cd /Users/vaibhav/Documents/Newproject/out/integration
python3.12 paper_live/offline_strategy_check.py paper_live/run2/d7d-run2 60 w2a/offline_parity_run2_v1_10.json perp_perp_maker_taker_v1_10 gate_first w2a/config_v1_10.json > w2a/offline_parity_run2_v1_10.out 2>&1
echo done > w2a/offline_parity_run2_v1_10.done
