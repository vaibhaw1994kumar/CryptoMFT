# SOLUSDT size ladder, series i (Phase 2 F02 b)

EXPLORATORY: SOLUSDT size ladder, series i (child = parent, capped at the 25,000 USDT clip cap), 29 Oct 2026 capture (REJECTED / CRITICAL: mechanics only), v1_17, pair close on, singapore latency (ASSUMED, pre-migration), the profile's fees; capital per venue = max(1000, 15 x IM(2 x parent)) at the ASSUMED tiers

| Rung | Parent SOL | Child SOL | Verdict | Maker fills (buy/sell) | Pair net bps p10 / p50 | Hedge cost bps p50 / p90 | Fully hedged | Unhedged s p50 / p99 | Bursts (longest) | Peak net SOL | Peak notional / IM per venue | Max orders 1 s | Closing signals | Bound / failed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| i_01k | 10.2 | 10.2 | PASS | 24 (15/9) | 1.4553 / 3.3768 | 0.0 / 2.0786 | 0.9167 | 0.0386 / 0.0494 | 17 (9) | 10.2 | BINANCE: 1983 / 39.66, BYBIT: 1981 / 39.62 | 3 | 19 | - |
| i_05k | 51.0 | 51.0 | PASS | 80 (67/13) | 1.4553 / 3.4731 | -0.0 / 0.0 | 0.9375 | 0.0398 / 0.0646 | 68 (20) | 51.0 | BINANCE: 9925 / 198.49, BYBIT: 9915 / 198.31 | 5 | 45 | - |
| i_10k | 102.0 | 102.0 | PASS | 111 (100/11) | -4.7651 / 3.4731 | -0.0 / 7.2254 | 0.9459 | 0.0393 / 0.0646 | 97 (29) | 102.0 | BINANCE: 19846 / 396.91, BYBIT: 19829 / 396.59 | 5 | 73 | - |
| i_20k | 204.1 | 204.1 | PASS | 176 (165/11) | 1.4082 / 5.5248 | -2.0553 / 1.0267 | 0.9489 | 0.0398 / 0.0646 | 158 (29) | 109.0 | BINANCE: 39755 / 795.09, BYBIT: 39738 / 794.77 | 5 | 71 | - |
| i_30k | 306.1 | 255.1 | PASS | 239 (228/11) | 1.4082 / 3.4718 | -1.0125 / 1.0267 | 0.9456 | 0.0394 / 0.064 | 216 (29) | 109.0 | BINANCE: 59592 / 2979.58, BYBIT: 59555 / 2977.74 | 5 | 72 | - |
| i_50k | 510.2 | 255.1 | PASS | 306 (295/11) | 1.4381 / 3.4667 | -0.0 / 1.0267 | 0.9477 | 0.0395 / 0.064 | 279 (40) | 139.5 | BINANCE: 99346 / 4967.31, BYBIT: 99295 / 4964.76 | 5 | 72 | - |
