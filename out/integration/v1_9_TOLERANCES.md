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
