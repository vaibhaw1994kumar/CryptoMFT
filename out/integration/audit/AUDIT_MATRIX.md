# Audit matrix, Phase 0 (deliverable A) - 16 Nov 2026

Read-only audit of the working copy (outer 3e0f8bb, core 0436b3a). Six area files hold the full blocks (Exists, Wired into, Effective config, Tests and evidence, Exact gap, Implementation, Acceptance test, Dependencies, Completion evidence); this file is the index with status and the exact gap per requirement ID. Statuses: VERIFIED_COMPLETE needs recorded end-to-end evidence; passing unit tests alone count as UNVERIFIED.

Result: 95 rows (each requirement ID of the order; W13 as its 20 sub-rows) - {'PARTIAL': 82, 'MISSING': 11, 'UNVERIFIED': 2}. VERIFIED_COMPLETE: 0. Every ID of the brief came back exactly once (checked by script; W13-A/B/C come back as table rows inside A5's W13 block).

## A1 Feed health and clock  ([A1_feed_health.md](A1_feed_health.md))

| ID | Requirement | Status | Exact gap |
|---|---|---|---|
| 1.1 | Report-only config audit of every feed-health component per profile | PARTIAL | (a) no per-profile feed-health config report or release-identity entry; (b) `config.yaml` and the testnet example are unloadable and unread; (c) FEED_DELAYED is on only in three JSON configs that no recorded live or replay run used (the D7d |
| 0.2a | Feed-health pieces, specific checks | PARTIAL | no run exercised an enabled FEED_DELAYED; no boundary test (equal to the limit; limit exactly at max); recovery from FEED_DELAYED is the first non-delayed event, no hysteresis; `cancelled_orders` 0 in the only live evidence, so cancel-on-ga |
| 0.2b | Recorder timestamp fields | PARTIAL | (1) the core canonical event and `RawFrame` have NO monotonic receive stamp and no offset estimate or uncertainty; ollog has the monotonic stamp but not in the raw lines (only `[recv_wall_ns, text]`); (2) `capture_notes.jsonl` rows carry on |
| 1.2 | core feed_health.py per venue x symbol x stream | PARTIAL | no per venue x symbol x stream rollup, no burst or run detector, no queue/loop-lag measurement, no time-of-day baseline, no versioned baseline file. |
| 1.3 | States HEALTHY / DEGRADED / UNSAFE (+OWN_LAG) per feed | PARTIAL | no HEALTHY/DEGRADED/UNSAFE/OWN_LAG per feed; no thresholds table; no hysteresis (FEED_DELAYED clears on the first non-delayed event, C:time_integrity.py:216-221; HostClockMonitor state is the LAST sample only: run2 had `max_abs_drift_ppm` 1 |
| 1.4 | Propagation of every transition to strategy layer and alerts; default actions off; no stal | PARTIAL |  |
| 1.5 | Timestamp model: which timestamps exist where | PARTIAL | as the right-hand column; the OMS and ledger use a single "ts_ns" of unstated clock. |
| 1.6 | Clock: correct_host_offset validated then enabled; HostClockMonitor; NTP; UNSYNCED -> DEGR | PARTIAL | (a) no independent reference sampling in the core; (b) no "validated then enabled" record; (c) DEGRADED from UNSYNCED not implemented; (d) FEED_DELAYED threshold not derived from a delay distribution (core stores only max corrected delay an |
| 1.7 | Historical acceptance (April ADA/MNT backlog bursts, 04-23 06:09 restart, 29 Oct and D7d s | PARTIAL | no recorded feed-health verdict on any of these periods; April has no gap/burst episode list; the 04-23 restart is not detected by code (a sequence reset `u == 1` is handled by Bybit book logic but no event is raised); silence timer latenes |
| 1.8 | Wiring: replay preflight, recorder health manifest, live/paper -> book gate + notification | PARTIAL | (1) the recorder manifest has integrity but no health (rates, gaps, delay stats, silence, reconnects per file); (2) replay results carry gate intervals but no feed-health verdicts or preflight excluded windows; (3) no notification and no in |

## A2 Capture, determinism, parity  ([A2_capture_determinism_parity.md](A2_capture_determinism_parity.md))

| ID | Requirement | Status | Exact gap |
|---|---|---|---|
| 0.2c | Replay engine determinism scope | PARTIAL | - No automated "replay twice and diff" tool for the session runtime: H1 and the identity runs were compared by hand/ad hoc scripts; `compare_baselines.py` knows only the backtester-loop file set (`FILES`, `run_dirs` globs `jan/ short/ mar/` |
| 0.2d | Intent journal, reject trace, choose_tactic reason codes | PARTIAL | - The safety/limits files above exist only when the caller is `day1/run_day.py`; `write_result` (`session_replay.py:973`) does not write them, and whether `out/queue_v2/qv2_march_run.py` (the March path) writes them was not checked. |
| 2.1 | Rolling buffers in live/paper (data and events listed in the order) | PARTIAL | - The platform recorder writes EVERYTHING to disk; it has no bounded window that is saved on a trigger and no pre-trigger/post-trigger logic (grep for ring buffer / incident / flight recorder in `P/`, `N/`, `docs/`, ollog and FAST_MM: only  |
| 2.2 | Incident triggers and durable windows (>=90 s before, >=5 s after, integrity manifest) | MISSING | everything: trigger registry, pre/post window, durable cut (copy of wire + canonical + trace + ledger rows for the interval), manifest (rule, symbol, window, frame counts, hashes, completeness), and the "overload / recorder restart / disk p |
| 2.3 | Every capture opens in the session replay; diagnostics on its window | PARTIAL | no window capture; a window with a sequence discontinuity at the cut (starts mid-stream with no snapshot) has no rule: the book needs a seed snapshot at the window start; `canonical.jsonl` rows carry no seeded state. Diagnostics (A5's list) |
| 2.4 | Replay interlock (never sends orders or writes live outputs; record requested vs actual sp | PARTIAL | it is safe by construction, not by an explicit interlock: no replay-mode flag that refuses credentials/adapters/network at start; nothing stops `write_result(result, out_dir)` (`session_replay.py:973`, `mkdir(exist_ok)`) from pointing at a  |
| 2.5 | Determinism definition, replay-twice hash compare, documented non-determinism | PARTIAL | - Determinism is defined in the order text but only partly in the repo: files cover orders, fills, signals, journal, pair economics, markouts, maker observations; not "book states" (no per-event or periodic book hash stream in a replay; `sn |
| 12.1 | Separate timing categories; compare causal intervals | PARTIAL | queueing/inference time as its own category: none (no inference in the arb path; router queue wait exists only as `held_ns` for rate-limited requests, `execution.py:1431`); strategy duration is a wall-clock sample distribution, not per-deci |
| 12.2 | Paper vs replay parity | PARTIAL |  |
| 12.3 | Testnet: all functionalities on Binance + Bybit testnet, small orders; record testnet limi | PARTIAL | no recorded run of the ARB BOT (maker + taker hedge + cancel + amend + unwind + breaker + reconcile) on both testnets through `TradingSession(VENUE_TESTNET)`; the 15 Nov hour used the probe, not the bot. Not exercised on testnet per recorde |
| 12.4 | Test order: unit/property -> integration -> historical -> incident replay -> paper/testnet | PARTIAL | no document that fixes the order and the pass criteria per stage; no property tests with a generator; the 92 known failing tests and 1 collection error are tracked, not fixed; no incident replay from real captures (2.2 missing); no shadow s |
| Q3 | Representative incidents: preserve 90 s before and after, replay, compare books, decisions | MISSING | no real incident preserved with 90 s before/after because there is no capture (2.2); replays of injected scenarios compare nothing against a live outcome; no automated comparison of books / decisions / orders / fills / health transitions /  |
| Q5 | Cross-component timing report: events ordered within documented clock uncertainty across r | PARTIAL | no documented uncertainty figure per clock (recorder host vs exchange vs VM vs Mac), no report that merges events from recorder, strategy, OMS, replay and diagnostics onto one axis with +- uncertainty and marks pairs whose order is ambiguou |

## A3 Book shape, resilience, conditions  ([A3_book_shape_resilience.md](A3_book_shape_resilience.md))

| ID | Requirement | Status | Exact gap |
|---|---|---|---|
| 0.2e | depth-quality fields (core) | PARTIAL | (1) not wired to any decision path or to live/paper; (2) all thresholds ASSUMED and the fields carry no demonstrated predictive value (T1 HOLDOUT: effective depth is not better than displayed, +0.0013 bps MAE; cliff flag 0.03% of orders, ca |
| 0.2f | FAST_MM regime_gate book_hole / depth_collapse / sweep_risk (MM-side) | UNVERIFIED | detectors untested; `FAST_MM/regime_gate.py` (1438 lines) and `FAST_MM/Fast_MM_Codebase/regime_gate.py` (1337 lines) differ and the codebase copy has no `book_features.py`, so "which copy is live" is UNVERIFIED; calibration is on Bybit spot |
| 3.1 | audit FAST_MM detectors and the core gap flag; repair rather than duplicate | PARTIAL | no side-by-side of the two sets on one data set; the ADA tick defect; two "depth_collapse" numbers with different definitions; core has no sweep-size rule. |
| 3.2 | taker book-risk estimate `book_risk(venue, side, size)` (execution side, advisory) | PARTIAL | no `book_risk()`; no flags; no typical/p95 extra cost; no sweep-in-progress input; no concentration; one number for "safe size" not exposed; nothing advisory reaches the hedge schedule. |
| 3.3 | "safe enough" measurable per instrument/venue | PARTIAL | no stated criterion such as "p95 surprise below X bps at size Q for instrument I on venue V"; no table per instrument x venue (Binance ADA depth does not exist locally and Binance has no MNTUSDT perp, `LARGE_GAPS_RESULTS.md:212`); no live/p |
| 3.4 | tests for book-shape detection | PARTIAL | the last three above; thinning and spike tested with stubs; no regression fixture from a real book (for example the ADA all-jump book). |
| 3.5 | evaluation target: surprise, TRAIN/SELECT/HOLDOUT once, pass criterion | PARTIAL | (1) the March HOLDOUT 03-13..15 has already been read at least twice (S3 gate score 10 Nov on two day blocks; G T1 14 Nov), so "HOLDOUT once" cannot be honoured again for March; G_RESULTS says "HOLDOUT not read again" after that; (2) the cr |
| 4.1 | resilience per venue/instrument after public aggressive runs | PARTIAL | MISSING: spread recovery, depth recovery (top-of-book and per-level size back to pre-run), replenishment versus cancellation (needs add/cancel split; the depletion ledger and `_dq_book` have the decrease side, not the add side), time and nu |
| 4.2 | resilience as an input to execution (trade again now or wait) | PARTIAL | tau is one assumed number for both venues and all instruments (the "3.5" is the isolated Binance fit, whose other fits give 0.5 to 200 s); no measured per-venue resilience state; no rule that decides "trade again now or wait" from resilienc |
| 5.1 | MarketState optional fields (assess VPIN first; no proxy called VPIN) | PARTIAL | add optional fields (None = unknown, as the existing ones) for top-1/top-5 imbalance, microprice deviation, spread, short_vol_bps and recent move, resilience state, depth_collapse, feed delay; a production builder from a book view and `Venu |
| M5 | FAST_MM book_hole / depth_collapse / sweep_risk vs core detectors on ADA/MNT (status and d | MISSING | never run on the same windows; core ADA flag defect blocks a fair comparison until repaired. |

## A4 Dislocation and cost  ([A4_dislocation_cost.md](A4_dislocation_cost.md))

| ID | Requirement | Status | Exact gap |
|---|---|---|---|
| 0.2g | CrossVenueMonitor (core `cross_venue.py`) | UNVERIFIED |  |
| 0.2h | Existing markouts (1/5/30 s), cost breakdowns, ledger P&L, pair economics | PARTIAL | 1. Pair join is fragile: the hedge intent is matched to a maker fill by `created_ts` within 1e-4 s, not by an id (`metrics.py:503`). Two fills of one quote in one tick could mis-pair. |
| 6.1 | Explanation of gap types (record only what the repo already says) | PARTIAL | no document gives the five-class explanation the owner asks for; Makarov-Schoar content is a statistics wrapper only; capital controls and transfer frictions are not linked to gaps; hour 20 of 29 Oct is unexplained; no stress-event catalogu |
| 6.2 | Extend/wrap CrossVenueMonitor | PARTIAL | the owner list versus what exists. Present: best bid/ask gap (via A/B in the wrapper only), mid gap (monitor), timestamp stale protection (delayed flag, stall), PARTIAL baseline (p50/p99 of the last 10,000 samples; `mid_gap.json` per hour). |
| 6.3 | Dislocation assessment record per episode | MISSING | the whole record (prices, timestamps, executable depth, costs on all legs, risk constraints, net edge, persistence, venue safety, confidence, class, decision, reason), the strategy-layer notification, and the rule "no automatic trading". Ve |
| 6.4 | Independent reference price (Q4) and the existing 5 s last-good-mark rule | PARTIAL |  |
| 6.5 | Acceptance: false-gap and genuine-dislocation examples, episode counts, controlled replay | MISSING | no classifier, so no episode counts by class and duration for 29 Oct or March; no check that no DATA episode is classed TRADEABLE; no controlled dislocation replay on the session runtime. March constraints: Bybit full book, Binance best bid |
| 7.1 | Separation of costs and one accounting convention | PARTIAL | the shortfall split and the opportunity part are not produced by any run (post-hoc wrappers in `out/`); `economics.reconcile` is not run on the session replay, so replay P&L does not reconcile to the economic ledger, only to the shadow ledg |
| 7.2 | Pre-trade estimate per order, persisted at decision time | PARTIAL |  |
| 7.3 | Post-trade measurement | PARTIAL | arb hedge slippage against the decision benchmark is effectively unmeasured on real fills (hedge impact 0 by construction in the probe; March Binance has only best bid/ask); the shortfall split reads the touch from the capture, not from the |
| 7.4 | Benchmarks on every fill and order | PARTIAL | no arrival price in the sense of the mid at order arrival at the venue (the closest is the touch at hedge arrival in `shortfall_split`, computed after the run); no interval VWAP and no interval average (grep for `interval_vwap`, `interval a |
| 7.5 | Cost dictionary in the core metrics dictionary | PARTIAL | the terms listed as "not in the dictionary" above; the markout definition conflict; docs drift (`metric_dictionary.md` vs `metrics.DICTIONARY`; 1/5 s vs 1/5/30 s); no FIX alignment. |
| Q4 | Controlled dislocation replay (and the independent reference price) | MISSING | a labelled fixture replay on the session runtime in which one venue's mid is displaced for a controlled time (a) with healthy feeds (genuine), (b) with a stalled or delayed feed (false), and assertions on the monitor class, the reference pr |

## A5 Routing, speed, diagnostics  ([A5_routing_speed_diagnostics.md](A5_routing_speed_diagnostics.md))

| ID | Requirement | Status | Exact gap |
|---|---|---|---|
| 0.2i | venue-selection code and Phase E status | PARTIAL | (1) `set_routing` (`layer.py:164`) feeds `assign_rest_hedge` an all-zero cost venue pair and discards `alternatives` and `dynamic_choice`: the one wired call ignores fees, spread, impact and adverse selection, i.e. exactly the T2 warning. ( |
| 8.1 | Phase E status and the routing evidence kept today | PARTIAL | a normal route decision (chosen venue equal to the order venue) is not journaled, only counted; no candidate list, no costs, no reason string stored for the normal case; no versions or snapshot refs. |
| 8.2 | venue selector for routable orders: inputs | PARTIAL | no routable-order caller (unwinds are `OWN_VENUE` in `decide_route`; MM quotes use pairing none; nothing chooses among several venues); inputs for latency, reject/miss rate, throttle headroom, health, inventory are not assembled into one st |
| 8.3 | alternatives compared per decision; duplicate-exposure prevention | PARTIAL | no per-decision journal of all five alternatives with their expected costs; fallback is explicitly not supported (`route_rejected` never retries elsewhere); "wait" has no arb path. |
| 8.4 | routing evidence: immutable record per decision | PARTIAL |  |
| 9.1 | Almgren-Chriss schedule as a hedge/unwind mode | PARTIAL | no core `hedge_mode` (fast / N slices over T / wait); trajectory function lives only inside a script (CLAUDE.md: no logic in `out/`); the study's costs are a PROXY (own-consumption refill 3.5 s ASSUMED, overlay MODELLED). |
| 9.2 | compare schedules on the same historical conditions | PARTIAL | one capture day (29 Oct 2026); no HOLDOUT read for the large sizes; paper/testnet runs do not compare schedules; no comparison harness in `src/`. |
| 10.1 | existing outputs: markouts 1/5/30 s, cost breakdowns, ledger P&L, pair economics | PARTIAL | completeness is only reported as counts per horizon (`excluded_counts`) in the decomposed path; the pooled `markout_summary` path has no exclusion counter for mid <= 0 (`resolve` just `continue`s). `pair_economics` reads only rows with reas |
| 10.2 | fill-time context snapshot per fill | PARTIAL | no per-fill snapshot row. VPIN is MISSING in the core (the `vpin` field exists, `simulation/models.py:69`, but `to_market_state` never fills it; `microprice_deviation_bps`, `microprice_drift_bps`, `trade_burst_flag`, `liquidity_evaporation` |
| 10.3 | favourable vs adverse: the MM "+2 captured / -4 adverse" definition | PARTIAL | (1) CONFLICT between definitions: window (placement-to-fill, FAST_MM, vs fill-to-horizon, core), sign (+ = adverse in FAST_MM, + = favourable in core), and denominator (m0 here vs fill price in the 09 Nov note). (2) The "+2 / -4" definition |
| 10.4 | one synchronized chart per session/day | MISSING | everything. Data needed for a synchronized chart exists as files: mid/spread (the feed), signals, orders, fills, markouts, exposure steps (`metrics.exposure_steps` :681), breaker/safety events (`session_safety_events.jsonl`). |
| 10.5 | timing distributions per session | PARTIAL | no single section per session with the nine distributions and the timestamp pair for each; stages 1, 2, 3, 9 are not measured in any run; stage 8 definition mixes send and decision; replay timings are the configured model, so a replay repor |
| 10.6 | cost section (pre-trade vs realised) and latency/operational health section | PARTIAL | no report section that puts pre-trade estimate and realised side by side per run; the shortfall split is not called by a run (`metrics.py:561`) and needs a touch lookup (read from the capture, not from the replay's truth book: label in `sho |
| 10.7 | self-check page Q1-Q5 for sampled fills | MISSING | page, sampler (random fill sample with seed), and the Q-to-data mapping. |
| 10.8 | generated automatically; arb and MM; asset-agnostic | PARTIAL |  |
| Q1 | pre-trade estimate vs realised, four parts, last 30 days | PARTIAL | no real recorded order in the last 30 days has the pre-trade fields (believed edge, hedge price at quote) needed, so the real-orders comparison is MISSING (not back-filled). The 24-signal sample is January 2022 + March 2024 replay fills (22 |
| Q2 | immutable decision record (same as 8.4) | PARTIAL |  |
| W13-A1 | Expected total cost | PARTIAL | impact and adverse not summed into one pre-trade number stored with the order |
| W13-A2 | Venue(s) | PARTIAL | dynamic and routable orders not wired (0.2i, 8.2) |
| W13-A3 | Size and speed | PARTIAL | no size/speed decision with evidence in session; AC not in core (9.1) |
| W13-A4 | Book deep and reliable enough | PARTIAL | `depth_quality` off in all profiles; reliability is APPROXIMATE (label in the evidence) |
| W13-A5 | Edge after costs and risk | PARTIAL | no per-decision edge-after-risk figure stored |
| W13-A6 | Evidence and assumptions | PARTIAL | labels only in opt-in evidence; no versions |
| W13-B1 | Feed healthy/current | PARTIAL | status of the blocker not verified by A5 (A1 owns) |
| W13-B2 | Book thinning/recovering | PARTIAL | observation only; no consumer in arb decisions |
| W13-B3 | Toxicity rising | PARTIAL | VPIN missing in core; no toxicity-triggered action in arb session |
| W13-B4 | Dislocation persisting | PARTIAL | A4 owns |
| W13-B5 | Order acked/partial/rejected/unknown | PARTIAL | unknown-order resolution partially evidenced in replay faults (E2 gate not verified by A5) |
| W13-B6 | Hedge exposure growing | PARTIAL | breaker and journal exist; no live display |
| W13-B7 | Maintain/amend/cancel/reduce/pause per policy | PARTIAL | reduce is only via breaker flatten/unwind; policy is not one table; `choose_tactic` not used for arb |
| W13-C1 | What happened, timestamped | PARTIAL | stamps in different clocks and files; no single timeline (10.4, 10.5) |
| W13-C2 | Cost vs the right benchmark | PARTIAL | not called by a run; benchmark (decision mid vs quote vs arrival) not fixed by the owner |
| W13-C3 | Conditions at fill | PARTIAL | no per-fill context row; VPIN missing (10.2) |
| W13-C4 | Favourable or adverse | PARTIAL | definition conflict with FAST_MM; small n; CI (10.3) |
| W13-C5 | Hedge correct and prompt | PARTIAL | decision-vs-send split missing (10.5) |
| W13-C6 | Ledger complete | PARTIAL | reconciliation mode default is `count`, not enforce (`session_replay.py` kwarg `reconcile_mode: "count"`); real-account reconcile is A2/A4 |
| W13-C7 | Reproducible and explainable | PARTIAL | explainability = 8.4 gap (no candidates and costs) |

## A6 Datasets, gates, MM package  ([A6_datasets_gates_mm.md](A6_datasets_gates_mm.md))

| ID | Requirement | Status | Exact gap |
|---|---|---|---|
| 0.3a | April ADA/MNT data (Bybit perp and spot, April 2026) | PARTIAL | (a) the label "April recorder perp data" does not match any file: recorder = spot, perp = public archive; (b) no data/ copy, no manifest in `data/manifests/` for either; (c) no perp sha256 list; (d) recorder capture lost; (e) the data lives |
| 0.3b | 2026-04-23 about 06:09 UTC feed restart | PARTIAL | UNVERIFIED whether the PERP ob200 files for 04-23 restart at the same time (no output of `bybit_perp_archive.py` on this Mac; I did not scan the 157 MB perp file, per the read rule). The MM forward test and the cost study on ADA/MNT use oth |
| 0.3c | 2026-10-29 SOLUSDT perp capture (Bybit + Binance), D7d silences | PARTIAL | (a) 5 of 8 per-part file types missing locally (VM only; the VM was not checked); (b) catalog says REJECTED/CRITICAL yet the data is used for replays without a recorded owner decision to waive that flag (the replays run with `--allow-reject |
| 0.3d | Bybit March 2024 SOLUSDT | PARTIAL | (a) the data manifest covers only 03-08..03-15, so the TRAIN days (03-01..03-07, used for S1/S3 and hedge-move fits) are not manifest-gated; (b) Bybit `book_snapshots` 03-24 is an empty file; Bybit book_events stop at 03-15 (so 03-16..03-31 |
| 0.3e | "1 July ADA b'" run (MM side) | PARTIAL | (a) no frozen b' reference artefact or hash list (no `LOCK_*.json` for b'; `LOCK_c1/c2/c3` cover the arb goldens only); (b) the b' bridge run is superseded by the session route and not byte-identical to it, so the "identical" rule needs a n |
| 0.4 | Gate definitions | PARTIAL | (1) "c1 goldens" and "14 profile hashes" are not defined in any working-copy file; they exist only in MM-session lock files, one of which (LOCK_c1) is missing here and the other (baseline_hashes.json) is a list of 14 input files dated 8 Nov |
| M1 | MM recorder on core raw_capture + feed_health (per-message timestamps) | MISSING | a recorder for Bybit spot/linear that writes through `RawCaptureWriter` with a receive stamp and a monotonic stamp per frame, sequence fields kept, reconnect events recorded, and a quality catalog like the arb's; retire the MM copy (CLAUDE. |
| M2 | MM profile actions on health events (off by default) | MISSING | the whole set, plus the health event vocabulary on the core side (A1). |
| M3 | rerun 1 July ADA b' with preflight on, attribute differences | PARTIAL | no run pair (preflight off vs on) and no attribution table; the reference b' is not pinned (0.3e); preflight on the SPOT ob200 archive may flag the 04-23 style restarts (0.3b) and `rpi` prints; effect on a July day is unknown. |
| M4 | MM implementation shortfall and diagnostics on the 6 July days | PARTIAL | shortfall definition (A5 owns the MM "+2 captured / -4 adverse" definition check), the run, and the 6-day tables; the markout caveat of the 100 ms archive (T1 caveat, 97% of the 0.1 s outcomes are the next snapshot) applies. |
| M6 | MM paper-vs-replay parity | MISSING | a paper MM session on the public feed (recorded with M1's recorder) and an offline replay of the same recording, compared order by order (orders, fills, positions, cash). |
