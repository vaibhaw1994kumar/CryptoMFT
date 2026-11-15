#!/bin/bash
# FOLLOWUP_RULES.md 1c: T5 at 100 / 500 SOL on the capture (Binance hedge venue, real depth), price-aware fills. First pass builds the outflow samples from the first half; second pass runs the arms.
cd /Users/vaibhav/Documents/Newproject
D=out/integration/cost
python3.12 -I $D/t5_split.py --out t5c largeprep > $D/t5c/log_largeprep.txt 2>&1
python3.12 -I $D/t5_split.py --out t5c large > $D/t5c/log_large.txt 2>&1
echo done > $D/t5_large.done
