# FillModelV2 multi-coin study: tolerances (DRAFT, written 2026-11-12 BEFORE any data for this study was downloaded, converted or looked at)

Status: DRAFT for the owner. It is frozen (sha256 recorded in notes.md) when the owner approves it, and before the first download.
Owner, 12 Nov: "FillModelV2 we will see later stage": nothing here runs until the owner says so.
A threshold that is missed is reported as missed, not re-tuned. EXPLORATORY.

## Why this study
FillModelV2 failed on ADA/MNT 19 April – 2 May (TOLERANCES.md, addendum 3; report step 3). The calibration slope was 2.11 on 2 May, a test day with about 2 real fills and a different quoting regime.
This study asks whether the model is calibrated when it is fitted and tested on many coins and days with enough real fills.

## Scope (proposal)
- **Coins:** the 8 with the most of our real PostOnly spot orders in `bybit_30d.zip`: SOLUSDT, SUIUSDT, LTCUSDT, DOGEUSDT, ETHUSDT, OPUSDT, XRPUSDT, FILUSDT.
- **Days:** 2026-04-19..05-02. A coin-day counts only if it has at least 30 of our PostOnly orders: 91 coin-days, about 17,100 orders and about 13,000 real fills (counted from the order file only; no market data seen).
- **Data:** Bybit public spot archive (ob200 book zips and trade csv), the same source and converter as ADA/MNT (`bybit_spot_archive.py`).
- **Disk:** about 16 GB is free. ADA/MNT took 9–16 MB per coin-day, so the six altcoins are estimated at 1–3 GB raw.
  - SOL and ETH are unknown. One day each is downloaded and measured first, and the rest only if the total stays under 8 GB raw + converted.
- **ADA and MNT** (already converted) may be added as two more coins; that is decided now: **yes**, same day rule.

## Data checks (per coin-day, before any order is replayed)
- **D0** raw sha256 recorded. There are no reference hashes, so each is NO_REFERENCE_HASH (not a failure).
- **D1** the converter finishes; trade rows = csv rows.
- **D2** sequence gaps: 0 = PASS; 1–5 = REVIEW (listed); more than 5 = the coin-day is EXCLUDED.
- **D3** core rebuild check (`queue_v2_rebuild_acceptance.py`, `event_order=delta_first`): touch price ≥ 99% per day, or the coin-day is EXCLUDED (listed).
- **D4** a feed restart (update id resets to 1) excludes orders placed from 10 min before to 10 min after it, and orders resting across it.

## Replay (features only)
- **Path:** as in steps 1–3: `maker_only`, profile `spot_spot_maker_taker_v1`, latency preset singapore, `event_order=delta_first`, `trust_local_ts=False`, cancel model proportional.
- **Timing:** real create time + entry latency; cancelled at the real cancel time.
- **Use:** only the placement features are used. Labels are never taken from the simulator.

## Labels, censoring, exclusions
- **Labels:** REAL outcomes only. "Filled within h" = the real first execution within h seconds of the real create time.
- **Censoring:** censored = really cancelled before h without a fill. IPCW as in `fill_model_v1`.
- **`price_moved_through`:** computed from the archive, because the engine's flag was false on every row in steps 1–3. True when a trade printed at a price better than ours (below a bid, above an ask) within h after creation. The informative-censoring check is reported with it.
- **Excluded before any number:**
  - **probable amendments:** accepted orders whose recorded price crosses the archive book at the create time (fill-evidence rule e);
  - orders resting across the end of their coin-day (censored, counted);
  - real PostOnly rejects (no label).

## Splits
- **Gate horizon:** 5 s; 1 s and 15 s are reported.
- **S1, chronological:** for each test day D from 04-22 on, fit on all coins' days before D, test on D (all coins).
- **S2, leave-one-coin-out:** fit on the other coins, all days; test on the held-out coin.
- **Eligible fold:** at least 100 uncensored test rows AND at least 20 real fills at 5 s. Smaller folds are listed as "too small", not scored.

## Pass rule
The model counts as an IMPROVEMENT only if ALL of these hold, separately under S1 and under S2:
1. Pooled test Brier (all eligible folds) is at least 5% below the base-rate Brier, where the base rate is the IPCW fill rate of each fold's fit set.
2. Not worse than the base rate on at least 70% of eligible folds.
3. Pooled calibration slope (weighted logistic recalibration on logit p) is within 0.8–1.2.
4. Calibration in the large: |mean predicted − IPCW observed| ≤ 0.03, pooled.

Otherwise: NO IMPROVEMENT, with the failed conditions named. The output tag is MEASURED_SPOT_2026_MULTICOIN; not approved as a quote filter.

## Secondary (reported, not part of the verdict)
- Steps 2a, 2b and 2c (false fills, first-fill timing, PostOnly) on the new coins, under the rules of the 10 Nov TOLERANCES.md, with the amendment exclusion above.
- Per-coin results.

## Not established (stated in advance)
- Spot, not perp.
- Our own quoting only: the model learns our cancel behaviour as well as the market.
- 14 days in one season.
- The archive has no receive time of ours.
- No feature describes how we quoted (cancel speed, amend rate), so a regime like 2 May may stay hard.
