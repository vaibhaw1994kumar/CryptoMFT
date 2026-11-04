#!/bin/bash
# Bybit Oct-2024 rebuild acceptance, every day with trades (Bybit has real snapshots; Binance has none usable, see oct_binance_book_events_vs_trades.json)
cd /Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
run() { nice -n 6 python3.12 scripts/queue_v2_rebuild_acceptance.py --data-root ../data --venue bybit --pair SOLUSDT --day $1 --event-order legacy --out-dir ../out/queue_v2/rebuild_acceptance >> ../out/queue_v2/logs/acc_oct_bybit.log 2>&1; }
( for d in 01 03 05 07 09 11 13 15; do run 2024-10-$d; done ) &
( for d in 02 04 06 08 10 12 14; do run 2024-10-$d; done ) &
wait; touch ../out/queue_v2/logs/acc_oct.done
