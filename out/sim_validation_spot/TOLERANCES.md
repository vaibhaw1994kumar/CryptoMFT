# Spot simulator validation: tolerances (written 2026-11-10 04:37:05 IST, BEFORE any converted number, rebuild result or order row was looked at)

Scope: ADAUSDT and MNTUSDT Bybit spot, 2026-04-19..2026-05-02. Worktree at tag phaseC-gate (outer 2013aa4, core 1c123ad). EXPLORATORY.
Fixed here so that none of them can be moved after the numbers exist. A threshold that is missed is reported as missed, not re-tuned.

## Step 0: data
T0  Raw files: sha256 of every raw file used equals the entry in mm_wt data_incoming/bybit_spot_2026-04/raw_sha256.txt or raw_sha256_may.txt. A mismatch = STOP. A file with no reference hash is listed as NO_REFERENCE_HASH (not a failure).
T1  Conversion: the converter finishes; trade rows written = csv rows read; no unreadable record. Anything else = STOP for that coin-day.
T2  Sequence check (the converter's own): PASS = 0 sequence gaps; REVIEW = 1..5 gaps in the day (each listed with its size); FAIL = more than 5. Repeats (u equal to the previous u) are listed, not gated.
T3  Rebuild vs snapshot (the converter's own: rebuilt book just before each snapshot against the snapshot): touch match >= 99.0 % of snapshots per day (the threshold already used in new_backtest/data_manifest.py). Top-10 match is reported, not gated.
T4  Core harness (scripts/queue_v2_rebuild_acceptance.py, simulation/queue_v2/rebuild.py): touch match >= 99.0 % per day. It was built for the SOLUSDT perp data root; if it cannot read the converted spot pair=/date= layout the result is "NOT RUN" with the reason, never a pass.
T5  Collector comparison (data/bybit-spot, our recorder, 04-19..04-25 only, READ ONLY). The recorder stamps OUR receive time; the archive has Bybit's publish time (ts) and matching time (cts). For each collector book snapshot or event the archive touch is read at (collector time - lag) for lag in {0, 50, 100, 250, 500, 1000} ms and the lag with the best match is reported per day together with all the others.
    Compared only where both feeds exist (the recorder's own outages are not counted against the archive). PASS = at the best lag, touch PRICE equal in >= 99 % of compared moments and touch SIZE equal in >= 95 %; coverage reported. Trades are reported, not gated.

## Step 1-2: order replay (set now, applied later)
Placement: real createdTime plus the profile's entry latency (singapore preset). Cancel: real cancel time. Cancel models: proportional (headline), behind_first, ahead_first (the two bounds).
Stale book = the last book message is older than 1.0 s at placement; fresh otherwise.
Queue position buckets (simulated queue ahead, quote currency): inside the touch (0), up to 1,000, 1,000 to 10,000, above 10,000.
2a  False fill = real CANCELLED order with a simulated fill before the real cancel time. Wilson 95 % CI. PASS = point estimate <= 5 % AND upper bound <= 10 %. FAIL = lower bound > 10 %. Otherwise INCONCLUSIVE. The verdict must be the same under all three cancel models or it is reported as model-dependent.
2b  Real FILLED orders: first-fill time hit within 1 s of the real first fill, premature (simulated earlier by more than 1 s), late (later by more than 1 s), unfilled. Compared with the earlier 81 % proportional result (location is pinned in the report): consistent if the hit rate is within 5 percentage points of it. Premature rate <= 10 %.
2c  PostOnly: real REJECTED vs accepted against simulated reject at arrival. Sensitivity (real rejects the simulation also rejects) >= 70 %; false-reject rate on real accepted orders <= 2 %. The count of real rejects (about 38) is small and the interval is reported.
## Step 3: FillModelV2 (set now, applied later)
Fit 04-19..05-01, test 05-02 and per day; IPCW as in fill_model_v1. Counts as an improvement only if Brier on the test day is at least 5 % lower than the base-rate Brier AND it is no worse than the base rate on at least 70 % of test days AND the calibration slope is within 0.8 to 1.2. Otherwise it is reported as no improvement. Output tag MEASURED_SPOT_2026; not approved as a quote filter.

## Not established (stated in advance)
Spot not perp; two coins; one heavy cancel day (05-02); the archive has no receive time of ours (feed delay is assumed from the profile); the cancel-model bounds are bounds, not measurements; the phaseC-gate simulator predates Phase D, S1 and S2 changes.

## ADDENDUM 1 (2026-11-10 04:37:52 IST): alignment key for T5 (the thresholds are NOT changed)
Written after reading the FIELDS of one collector record (no comparison result existed): each collector record carries exchange_ts (Bybit's own matching time, the archive's cts) as well as local_ts (our receive time), and the first record of 04-19 MNT shows local_ts - exchange_ts of about 1.9 s.
T5 therefore aligns on exchange_ts = cts (same clock, no lag search). The lag search over {0, 50, 100, 250, 500, 1000} ms of the original T5 is kept as a secondary table, applied to the ARCHIVE side (cts), to show how sensitive the match is to time alignment.
Original TOLERANCES.md sha256 before this addendum: 749a53fff4fe8f66668f6a5aea55b73113dda1f1f1e026b2f111f3ffeb85a8eb. The PASS thresholds (touch price >= 99 %, touch size >= 95 %) are unchanged.

## ADDENDUM 2 (2026-11-10 05:00:09 IST): post-hoc diagnostics for T5, added AFTER the first six ADA results were seen
The first ADA results (04-19..04-24) were below both T5 gates (touch price 96.6 to 98.3 % against 99 %, touch size 65 to 72 % against 95 %); a lag that moves the archive EARLIER made the match worse. The gates and the verdict rule are NOT changed: a day below them is FAIL.
Added for explanation only (labelled post-hoc in the output): archive read LATER by 0.05, 0.1, 0.25, 0.5 s, and the share of collector moments whose touch price matches at ANY of all lags. Evidence read from the converter's own stats: archive book updates of ADA 04-19 come every 103 ms at the median (MNT 131 ms) while the recorder's events are 42 ms apart (p10 20 ms), so the archive is a coarser sampling of the same book. That is a hypothesis for the mismatch, not a result.

## ADDENDUM 3 (2026-11-12 03:28:22 IST): owner decisions after step 0 (Track C). The thresholds are NOT changed.
This file was copied unchanged from simval_wt/Newproject/out/sim_validation_spot/TOLERANCES.md (sha256 50924c3140e4dd1e37f466b03cc62a7bf6d171c24711af8f0b00478f83711bfb) before this addendum.
- T4 ACCEPTED as explained: the 2 failing days (ADA 05-02, MNT 04-30) are a tie rule of the harness. Each archive day ends with the next day's 00:00:01
  snapshot, stamped with the same exchange time and update id as the last deltas, so it is the book AFTER them. Condition set by the owner (11 Nov): T4 must
  pass with updates first at equal timestamps. Result (11 Nov, worktree core): event_order=delta_first 28/28 PASS; legacy 26/28 as in step 0.
  Re-run 12 Nov on the current core (outer c468a9a, core f7b7cc3), cut short at the owner's request: 16 of 28 coin-days (all 14 ADA, MNT 04-19, 04-20,
  04-30; 33 runs over both orders) are identical to 11 Nov in every variant and metric, including both legacy failures (ADA 05-02, MNT 04-30); the code
  T4 uses (event_loader, rebuild harness, order book) is unchanged by the merge.
  delta_first is used for the spot archive in the validation run config only (no trading profile changed).
- T5 ACCEPTED as "the archive is a coarser view of the same book" (ob200 updates about every 100-130 ms, the recorder's every 42 ms). The recorder capture of
  04-19..04-25 was later lost (live capture, cannot be downloaded again); owner 12 Nov: all 14 days use the public archive, labelled "public ob200 archive, ~100 ms resolution".
- Exclusion (feed restart, 2026-04-23): orders placed 06:00-06:20 UTC (owner, latest; was 06:04-06:20 on 11 Nov), and any order resting across the resync
  (ADA 06:08:58.321, MNT 06:09:08.302 UTC). No order of ours was placed between 05:00 and 07:30 UTC that day, so the exclusion removes nothing.
