#!/bin/bash
# HOLDOUT 03-13..15, read ONCE (owner 14 Nov): T1 on Bybit and T5 on Binance, with the settings fixed in G_RULES.md and t5b/chosen.json (T 1 s, lambda_u 10 bps, chosen on SELECT).
cd /Users/vaibhav/Documents/Newproject
D=out/integration/cost
python3.12 -I $D/t1_hedge_cost.py march 2024-03-13 > $D/t1/log_h13.txt 2>&1 &
python3.12 -I $D/t1_hedge_cost.py march 2024-03-14 > $D/t1/log_h14.txt 2>&1 &
python3.12 -I $D/t1_hedge_cost.py march 2024-03-15 > $D/t1/log_h15.txt 2>&1 &
python3.12 -I $D/t5_split.py run 2024-03-13 2024-03-14 2024-03-15 > $D/t5b/log_h.txt 2>&1 &
wait
echo done > $D/holdout.done
