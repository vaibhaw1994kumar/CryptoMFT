# v1_9 tolerances: pass/fail rules, written BEFORE any v1_9 replay is evaluated

Written 12 Nov 2026 (owner order, EMS note section 6). Nothing on v1_9 is read, compared or reported until this file and
`production_limits_proposal.md` both exist. A v1_9 replay started earlier on the same day (day-1, 1 SOL) was stopped
unevaluated and is discarded: the replays below carry the lifecycle report, the stage traces and the processing-time setting
that it lacked. A difference above a tolerance is a FINDING to classify (rounding, BUG_BACKTEST, BUG_CORE, BY_DESIGN, DATA),
never something to tune away. Every P&L figure is EXPLORATORY and carries profile, size, day, fees and "mark = mid, no funding".

Profiles: v1_9 = v1_7 + `execution.hedge.cap: size_aware` and nothing else. v1_8 = v1_7 + `arb.quote_withdraw`. The two are
compared with v1_7, not with each other. Quote withdrawal stays out of v1_9 (one change at a time).

## A. Identity (byte level)

| # | Check | Pass | Fail |
|---|---|---|---|
| A1 | March 2024-03-11, v1_5, v1_6 (g), v1_7, new code against the last frozen snapshot, every switch off | `session_orders`, `session_fills`, `session_signals`, `session_intent_journal`, `session_intent_fills`, `session_pair_economics(.json/.jsonl)`, `maker_observations`, `order_times` byte-identical. `session_manifest` / `summary` differ only in `release_identity.*` and `wall_s` | any other byte |
| A2 | Day-1 (2026-10-29), v1_7 on the new code against `runs/v17_c` | same file list, same rule | any other byte |
| A3 | Day-1, v1_9 at 1 SOL against v1_7 at 1 SOL | Every difference traces to a `HEDGE_CAP_SIZE_AWARE` event with `touch_covers = false`. Where `touch_covers = true` on every hedge, the two runs are byte-identical apart from the new file `session_decision_events.jsonl` and `release_identity.*`. Every hedge where the touch does NOT cover the size is LISTED (time, size, touch, deepest level reached, cap, fallback or not) | a difference with no such event; a `touch_covers = true` hedge with a cap different from touch_slack |
| A4 | v1_8 against v1_7 (day-1, 1 SOL) | Without a `QUOTE_WITHDRAWN` event the order, fill and signal files are identical; every difference starts at a withdrawal | a difference before the first withdrawal |
| A5 | Processing time: `processing_ms` empty, against the run without the setting | byte-identical (the manifest carries no `processing_ms` key) | any difference |
| A6 | E3 participation cap off, against the run without the field | byte-identical; `participation` appears in the manifest only if a fill would have exceeded its print | a new key without a binding print |
| A7 | Amend off (default), v1_5 to v1_9 | byte-identical on the A1 file list | any difference |

## B. Mechanics replays (day-1 capture, one at a time, each with its own frozen snapshot)

Every replay is a FULL independent replay of the capture. No run reuses another run's fills (owner 12 Nov).

| # | Run | Pass (all must hold) |
|---|---|---|
| B1 | v1_9 at 1 SOL (clip 1, gross 5) | Net position never above the ceiling plus headroom (1.25 SOL) at any fill; per-venue |position| never above 5; every order price on the venue tick; flat net at the end or an alarm on record (never a silent residual); each maker fill booked once (one `FILL` ledger event per fill id) and hedged once; zero `ORDER_TRANSITION_UNEXPECTED` |
| B2 | v1_9 at 5 SOL (clip 5, gross 25, E3 cap ON) | B1 with limits scaled (net ceiling 6.25, gross 25); participation invariant: for every public print, the own fills it caused sum to at most the print volume (zero violations; the cases the cap clipped are listed with their prints) |
| B3 | v1_9 at 25 SOL (clip 25, gross 125, E3 cap ON) | as B2, limits scaled (net ceiling 31.25, gross 125); if the margin engine breaches, the run must stop quoting through the margin rule, never trade on |
| B4 | v1_9 at 1 SOL with `processing_ms` {strategy: 1, risk: 0} then {strategy: 5, risk: 5} (ASSUMED sensitivity values; replaced by the D7d measured p50 / p99 when they are extracted) | Every order is sent later by exactly the sum (tested at unit level); `session_pair_economics.hedge_send_delay_s` equals the setting for every hedge; everything else may change only through timing. Report: fills, hedge fill delay, unhedged interval, markouts, versus B1 |
| B5 | v1_9 stress at the p99 and p99.9 delays (testnet-measured, labelled) | After the sampled mode exists (section F). Report stale-quote exposure time, cancel-to-fill races, hedge delay. No pass rule beyond B1 safety |

No profit rule. A positive or negative P&L is never a pass or a fail here.

## C. EMS coverage cases (each is a test and writes a stage trace)

Each case passes when its test passes AND the evidence file exists (`out/integration/ems_cases/<case>.jsonl`, one line per
stage with a timestamp: market event received, decision, request sent, exchange-effective, fills in between, notification
received, hedge sent) AND the coverage matrix row links to it.

1. Quote validity: Binance moves against a resting quote between timer ticks, so the quote is withdrawn; a fill that lands before the cancel takes effect is booked once and hedged once.
2. Hedge cap: never loosened by tick rounding (property test at every SOL tick); `touch` and `size_vwap` bases; `size_aware`: touch covers, size spans levels, absolute limit binds, retry recomputes, view book (never the truth book), fallback to touch_slack with a label.
3. Duplicate Bybit "Filled" in a cancel/fill race: booked once, residual hedged once.
4. Rate-limit pressure: new quotes refused at 75%, hedge and cancel still sent. PINNED FINDING: a hedge is refused at 90% and ends as a visible alarm (owner decision pending).
5. Alerts (breaker trip and alarm, unwind exhausted, hedge alarm, kill switch, margin pause) carry the four fields.
6. Restart against the venue: missed execution booked once, hedged once, no new maker risk until reconciled; second restart books nothing.
7. Amend (switch on in the test only): fill during amend pending; accepted request confirms nothing; reject then query then revalidate; timeout then late success; cancel during amend; lineage; duplicate Filled across the replacement; fallback never two live orders. Scenario case i (fill during amend pending) is added to the EMS coverage replays through the S0 test profile.
8. Recovery after a feed gap or a stale book (section E, new): quoting resumes only after a valid book, resync and reconciliation; no order is priced from a stale book.
9. Late-success reconciliation (section E, new): client order id, exchange order id, execution reports and venue position all agree before anything new is sent.

## D. Lifecycle report by regime (added to every v1_9 replay output, next to the statistics block)

Definitions (fixed here, before any number exists):

* **Fill rate** = maker fills / maker quotes placed. Quotes placed = every maker order that reached SENT, including orders that
  never filled and orders withdrawn or cancelled. A partially filled quote counts once in the denominator and once in the numerator.
* **Cancel-to-fill races**: three counts. (a) fills booked while the order was CANCEL_PENDING; (b) a cancel requested while the
  order was still SENT; (c) fills reported after the order's cancel was confirmed (late fills).
* **Message load**: orders sent, cancels sent, amends sent per minute: p50, p99, max over the minutes of the run.
* **Residual exposure duration**: the unhedged interval per maker fill (fill notification to hedge fill) and the oldest exposure age
  seen (max over the run and at each alert).
* **Hedge completion**: share of hedge chains that end RECONCILED without unwind; chains with retries; chains unwound; chains FAILED.
* **Adverse tails**: markout p5 and p95 of the maker fills at 1 s, 5 s, 30 s, bps of the mid.
* **Regimes**, each a partition of the run (the parts add up to the whole, checked): (1) volatility tercile, (2) spread bucket
  (tercile), (3) liquidity bucket (depth at the touch, tercile), (4) UTC hour. The tercile boundaries are computed ONLY on the
  training days (March 2024-03-01..07) and stored in `config/regime_boundaries_train_0301_0307.json` with the script hash; a day
  is bucketed by those boundaries, never its own. Volatility = absolute mid change over 1 s (bps); spread = touch spread (bps);
  liquidity = bid plus ask size at the touch of the maker venue.

Pass: every table sums to the run totals (fill counts, quote counts, race counts); the denominators are exact integers; a bucket
with fewer than 30 quotes is printed as "n < 30" and never as a rate.

## E. New tests the owner added

* E1. Recovery after a feed gap or stale book: inject a 6 s gap (dead feed); no order is priced from the stale book; quoting resumes only after a valid book, resync and reconciliation. Pass: no `PRIMARY` order sent between the gap and the resume.
* E2. Late success: the venue executes while the OMS has lost the answer; reconciliation by client order id, exchange order id, execution reports and venue position; all four agree or nothing new is sent. Pass: zero new orders until agreement; one fill booked.

## F. Tail latency (finding and plan)

FINDING (12 Nov): the session path draws every timeline from a lognormal given by the preset's mean and jitter, with the 35 ms Binance floors: light tails (singapore: Binance entry 40 ms, jitter 8 ms). It does not sample the measured tails. The
lifecycle calibration files (`config/lifecycle_calibration_perp_*.json`, TESTNET, labelled) hold empirical quantile grids. Plan: a
sampled mode (inverse-CDF over the grid) per timeline, off by default; B5 runs at the p99 and p99.9 of the grid. Until it exists B5 is not run.

## G. Not claimed

Queue priority and fill-probability claims wait for the simulator-validation step (controlled live evidence). The timer-only versus
event-driven comparison is PARKED and, when run, is a full independent replay per policy on the same capture.

---

# Part 2: the EMS acceptance replays (owner order 12 Nov, "v1_9 EMS acceptance replays", all sections of the 09 Nov note)

Written before any further run. Runs already made under Part 1 (v1_9 at 1 SOL, v1_9 with 5+5 ms processing time, v1_7 identity, all on 2026-10-29) stay as recorded and are re-checked against these rules; no other run starts until the owner approves this file and `production_limits_proposal.md`. One job at a time.

## H. Determinism and seeds

* Every replay uses the profile's fixed seeds (timing seed 11 and the queue seeds in the profile); no run draws from the clock or the OS.
* PASS rule H1: one scenario of the matrix (the 2-hour window of case 2a) is run twice from the same frozen snapshot; every output file except `release_identity.*` and `wall_s` is byte-identical. FAIL: any other byte. Until H1 passes, no other result of this part is reported.

## I. Data available (stated now, so no row is reported as covered that cannot be)

* 2026-10-29 SOLUSDT capture, both venues, full day: available (scenario windows are 2-hour cuts of it, 20:00-21:00 included).
* March 2024: the local slices hold 2-hour windows of 03-01, 03-08 and 03-11 only (Bybit full book, Binance best bid/ask). The clean replay "March 03-11" is therefore the 2-hour window, labelled as such. The seven training days are not on this machine.
* 2022-01-05 (and 01-13): to be confirmed on disk before its replay is listed; if absent it is "not covered: data".

## J. Clean-replay rules (part 1 of the order), v1_9, one run per dataset

| # | Output | Pass | Fail |
|---|---|---|---|
| J1 | **EMS path coverage table** (`session_ems_path_coverage.json`): count of every path that occurred, from the ledger and the layer journal: intent states incl. escalated and unknown; maker placed / clipped by hedge depth / withdrawn on event / expired at 15 s / cancel while submitted; hedge IOC full, partial, zero; retry; query before replace; unwind; flatten (MARKET_BANDED at the touch band); gate pause, resume, trip; margin pause, resume; lot residual; rate-limit deferral; duplicate updates suppressed; late fills; participation cap binding; hedge cap source (touch_slack vs size_aware) | every listed path appears with an integer count, 0 included; a path with count 0 is listed "not exercised in this run" (never omitted); counts reconcile with the ledger event counts of the same names | a missing path name; a count that differs from the ledger |
| J2 | **Tactic and placement of every order** | every maker quote PASSIVE with placement deep; every hedge IOC_CAPPED; MARKET_BANDED only on unwind and flatten orders; `tactic_choice` shown as refused for the arb pairing in the manifest; the dynamic venue choice appears as a report-only column | any order with another tactic; a hedge sent as MARKET_BANDED |
| J3 | **Per-order checks** | no order price off the venue tick; every limit equals the grid rounding that never loosens (buy down, sell up) of its unrounded value | one violation |
| J4 | **Lifecycle metrics by regime** (v1_9_TOLERANCES.md section D, extended): volatility tercile, spread bucket, liquidity tercile, UTC hour; fill rate, races, message load, residual duration and oldest age, hedge completion, adverse tails, plus MATCHED ENTRY SPREAD and LIFECYCLE NET P&L per quote, with the statistics block | partitions add up; cells under 30 quotes print counts only; boundaries are the training-set ones where the training days exist, else labelled in-sample (the 29 Oct run) or partial (March slice) | a partition that does not add up; a rate in a cell under 30 |

## K. Scenario rules (part 2 of the order; each 2-hour window of 2026-10-29; each writes a stage trace)

Each scenario passes only if its assertions hold AND its stage trace exists (`out/integration/ems_cases/<case>.jsonl`, with market event received, decision, request sent, exchange-effective, fills in between, notification received, hedge sent) AND the coverage matrix row links it.

| Case | Assertion (pass) |
|---|---|
| 2a Binance move before the timer tick (quote_withdraw on) | `QUOTE_WITHDRAWN` at the move, before the 15 s expiry; the old quote can still fill during the effective-action delay (a fill in that window, if the capture produces one, is booked once) |
| 2b Fill during cancel pending (longer cancel latency); 2b2 fill after cancel confirmed | booked once; residual hedged once; the hedge target adjusted to the booked fill |
| 2c IOC partial and zero fill | only the true residual is retried (retry quantity = exposure less hedged); exposure age and the escalation are in the trace |
| 2d Timeout then late success (ghost 3 s and 30 s) | no duplicate order, no duplicate hedge; reconciliation agrees on client order id, exchange order id, execution reports and venue position before anything new is sent |
| 2e Feed gap / stale (20:00-21:00 plus an injected outage) | entries stop; exposure follows the defined path; quoting resumes only after a valid book, resync and reconciliation; zero orders priced from a stale book |
| 2f Rate-limit pressure (lowered limit) | cancels and hedges keep capacity; the quote population falls; the hedge-above-80% finding is reported as is |
| 2g Restart with open orders (resting quote and a hedge in flight) | recovered state reconciled against the venue's orders, executions and positions before quoting resumes; nothing booked twice |
| 2h New-level vs queued fills | the fill record carries the field (level created by us / queued behind others); counts reported; agreement with real fills is a Track C result, not claimed here |
| 2i Amend paths (S0 test profile v1_9s, ASSUMED values) | fill during amend pending; accepted request confirms nothing; reject then reason inspected, query, re-check; timeout then no second order; cancel during amend; fallback never two live orders; late fill on the old order reduces the replacement |
| 2j Duplicate Filled in a cancel/fill race (injected) | booked once, hedged once |
| 2k Breaker | pause (flat, gateway silent) resumes by itself; trip (exposure plus failure) cancels quotes, flattens with MARKET_BANDED at the touch band, stays tripped until a logged manual reset; alarm payloads carry the four fields |
| 2l Maker-venue (Bybit) outage with quotes resting | new exposure paused; open orders reconciled on reconnect |
| 2m Latency | one run with processing latency on both paths; one at p99 and one at p99.9 delays (testnet-measured, labelled); report stale-quote exposure time, races, hedge delay |
| 2n Size | size_aware cap and participation cap at 1, 5 and 25 SOL: first-attempt fill, slippage against the decision touch, partials, unwinds |

## L. What exists today and what does not (honest status at the time of writing, so "not covered" is decided now, not afterwards)

* BUILT and tested at unit or OMS level, with a stage trace: 2a (quote validity), 2b (partly), 2d (late success, unit), 2e (feed gap, synthetic stream), 2f (rate limit), 2g (restart, OMS level), 2i (amend, OMS level), 2j, plus the alert payloads of 2k.
* BUILT but not yet replayed on the capture window: all of the above; the replay-level versions are the work of this part.
* BUILT AND REPLAYED since (13 Nov): 2l (maker-venue outage: order entry lost, fill reports held to the reconnect; needs `oms.maker_ack_deadline`, off by default; finding: without it a lost quote sat SENT 565 s), 2c (thinned hedge-venue book: zero, partial, exhausted, thin touch; finding of 13 Nov: a hedge that FAILED under the 1.0 SOL residual bound raised no alarm and was carried, now changed by decision 2), 2m at fixed testnet p99 and p99.9 delays, 2h (`queue_class` on the maker_fill observation rows; 2 maker fills only).
* OWNER DECISIONS 13 Nov (applied to v1_9 itself; every v1_9 replay was rerun after them, so no row below rests on the earlier v1_9):
  1. `oms.maker_ack_deadline` is ON in v1_9 (profile key `oms.maker_ack_deadline`, core `oms.timeouts.maker_ack_deadline`) and is a REQUIRED switch for any profile used with real orders (`crypto_stat_arb.config.REAL_ORDER_REQUIRED`, `docs/strategy_contract.md`).
  2. A failed hedge always acts. When a hedge chain exhausts its retries the residual is unwound with MARKET_BANDED at the touch band whatever its size; a failed unwind is the persistent alarm. Only a remainder below one hedge-venue lot (0.01 SOL on Binance) may be carried silently, with the end-of-day warning. v1_9 sets the silent-carry bound to one lot (`hedge_faults.silent_carry_bound: below_one_lot`; was the position size, 1.0 SOL in the replay; the stage-0 proposal of 0.10 is withdrawn). Also in the required table.
  3. 2g at replay level is NOT built (reason accepted). Built instead: exchange-side restart reconciliation, `OMS.reconcile_account_after_restart` (open and closed orders, executions and positions fetched from each venue, missed fills booked once, the gate closed until everything agrees); scripted-venue tests only. A testnet restart drill comes later.
  4. 2i is covered by the MM replays after CR-MQ part 2 (amend on real paths). Recorded; not claimed.
  5. 2c extra: only the Binance touch thinned (deeper levels intact), the hedge larger than the touch, so the size_aware cap spans levels (case `2c_thin_top`; the cap, the fill and the slippage are in its matrix row).
  6. 2f passes with the rate-limit reserve (0 hedges refused, quote attempts fall; the matrix row states both numbers). At p99.9 of about 390 testnet samples the value is effectively the sample maximum (2m rows say so).
* NOT BUILT: a sampled (mixture) tail latency for 2m; a restart inside a replay for 2g (the replay driver holds about 30 references to one session object; a faithful restart rebuilds the session from the plan store and re-subscribes the books; reason accepted 13 Nov); the S0 requote strategy and profile v1_9s for 2i (the MM replays after CR-MQ part 2 cover amend).
* A case that cannot be built without owner input is listed in the matrix as "not covered" with the reason.

## M. The coverage matrix

One file, `out/integration/ems_cases/coverage_matrix.md`: path or case, covering layer (OMS, execution layer, router, simulator, risk, session), count, PASS / FAIL against this file, evidence file. A path with no evidence is "not covered: <reason>". Scenario P&L is mechanics only; no profit claim anywhere.

