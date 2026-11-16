# Strategy contract — cross-venue perpetual arb (`perp_perp_maker_taker_v1`)

Canonical. Written 2026-11-01. `docs/strategy_contract_maker_taker.md` is superseded by this file. Where a report disagrees with
this document or with `tests/test_edge_dispatch_acceptance.py` / `tests/test_perp_arb_legs.py`, the report is wrong.

## 0. What the code computed before this contract (STEP 0 findings, 2026-11-01)

| where | expression | sides / reference | fees | markout |
|---|---|---|---|---|
| `arb_strategy.py` taker/taker (old line 437) | `(sell_bid − buy_ask) / buy_ask × 1e4` | sell venue best bid, buy venue best ask, top of book only; ref = buy venue ask | both taker fees + safety buffer | no |
| `arb_strategy.py` maker bid (old line 519) | `(hedge_bid − max_bid) / max_bid × 1e4`, `max_bid = hedge_bid × (1 − required)` | taker venue best bid now; ref = our maker price | maker + taker + buffer | no |
| `arb_strategy.py` maker ask (added 2026-11-01) | `(min_ask − hedge_ask) / hedge_ask × 1e4` | taker venue best ask now; ref = hedge price | maker + taker + buffer | no |
| `backtester.py` realised edge (line ~928) | `(sell_vwap − buy_vwap) / buy_vwap × 1e4` on matched qty | actual fills | separate (`fees_paid`) | no |
| live `crypto_stat_arb/strategy.py` | `(sell_best_bid − buy_best_ask) / buy_best_ask × 1e4 − taker fees` | top of book; ref = buy ask | taker fees | no |
| `backtester_design_report.html` (document only) | `(ask_B − bid_A) / mid × 1e4 − fees`, "minus adverse selection" | **not in any code**; the venue orientation in the doc is reversed and no code subtracts adverse selection from P&L | | |

Depth entered the old code only through the quantity cap (`_tradeable_depth`: depth within `max_walk_bps`), never through the
price. The quantity-weighted edge over the levels a size consumes did not exist.

## 1. Product and instruments

USDT linear perpetuals on Bybit (`linear`) and Binance (USDⓈ-M), same instrument on both venues. The Jan-2022 replay data is
that product on both legs (integer contract quantities and 0.001 ticks on Binance; sub-0.01 ticks on Bybit). Fee presets are
the perpetual ones on both legs.

## 2. One edge function (`src/new_backtest/edge.py`)

* **taker/taker:** `taker_taker_edge_bps(buy_book, sell_book, size, buy_fee_bps, sell_fee_bps)` = `1e4 × (vwap_bid_sell(size)
  − vwap_ask_buy(size)) / ref − fees`, VWAP over the levels the size consumes on each book; `insufficient_depth` when either
  book cannot fill the size. **ref = the buy venue's mid at decision time** (documented in the docstring; the two directions are
  evaluated as two explicit calls, never by a sign trick).
* **maker/taker (conditional):** `maker_taker_expected_edge_bps(q, side, hedge_book_now, size, ...)` =
  `E[hedge executable price at hedge time | maker filled] − q` (in bps of ref) `− maker_fee − taker_fee − E[hedge slippage]
  − funding/holding − p_fail × loss_fail`. The expected hedge price is the hedge touch now moved by the conditional hedge-move
  estimate (`expected_hedge_move_bps_bid/ask`), which comes from the replay's own `legging_report.hedge_exec_move_bps` (quote-time hedge touch → executed hedge VWAP, conditional on a fill, by side: the touch move up to the fill plus the flight move and slippage; `hedge_move_bps` reports the touch-only part) — i.e. the hedge venue
  price move between quote time and fill time, conditional on a fill, by side) — never from assuming the current opposite quote
  is locked. `p_fail` and `loss_fail_bps` default to the hedge-failure study values (7%, 7.8 bps) and are parameters. **`p_fail` is a PLACEHOLDER,
  not a measured rate**: a replay with no injected failures cannot measure a production hedge-failure rate (the zero failures
  seen there are only the simulator's own depth misses). Every result that uses it carries `p_fail_status =
  PLACEHOLDER_NOT_MEASURED`; sensitivity at 0.02 / 0.05 / 0.07 / 0.10 is reported in `perp_mechanics_report.html`.
* Every signal logs both the **naive** edge (current opposite quote locked) and the **conditional** edge; the gate under
  `perp_perp_maker_taker_v1` uses the conditional one.
* `legacy_v0` keeps the old expressions (moved into `edge.py` as `legacy_*` functions, bit-identical) so earlier results can be
  reproduced. No module other than `edge.py` computes an edge.

## 3. Dispatch (`ArbConfig.dispatch_mode`)

| mode | what is sent on the signal | hedge | exposure it creates | used by |
|---|---|---|---|---|
| `fill_triggered` (**canonical**) | the passive leg only | sent in `_on_fill` when the maker fill is confirmed, **for the confirmed filled quantity, partial fills included** | legging: the hedge venue moves between fill and hedge (measured in `legging_report`) | every report produced to date (both HTML reports now say so; the earlier "simultaneously hits the book" sentence was a documentation error) |
| `simultaneous` | passive leg and the full-size hedge together | none later | an **orphan hedge** when the maker never fills (or fills partially): the un-matched hedge quantity is reported (`orphan_hedges`, `orphan_qty`) and unwound by the residual policy | no report; available for comparison only |

`taker_taker` always sends both legs at the same instant (independent latency draws); that is a different strategy, not a
dispatch mode of maker/taker.

Pending hedge, retries, rejects, timeouts, residual (all in `backtester.py` / `hedge_faults.py`):

* a hedge is **pending** from its send until it lands (`entry_delay` + any injected delay); a second maker fill while a hedge
  is pending sends a second hedge for the second quantity — hedges are per confirmed fill, never netted against each other;
* a hedge that **misses** (book moved beyond `max_walk_bps`) or is **rejected** (injected) is a hedge failure: `response`
  decides — `legacy`: instant flatten at the touch (charged to `unhedged_loss`); `retry`: resend up to `max_retries` with
  `retry_delay_s`; `unwind`: trade the maker fill back on the maker venue as a real order; `carry`: keep the residual as
  inventory (reported as `residual_qty`, never silently as P&L);
* a maker order that reaches `maker_max_age_s` is cancelled; a fill that lands before the cancel takes effect is hedged;
* a signal resolves when every leg has filled or missed and the maker order is no longer resting; any imbalance at that point
  is residual inventory handled as above;
* timestamps recorded per signal: maker first/last fill, hedge first send, hedge last fill, hedge quantity, the hedge venue's
  executable price at quote time and at fill time (`legging_report.rows`), together with the executed hedge VWAP, from which `legging_s`, `hedge_move_bps` (touch at quote → touch at fill) and `hedge_exec_move_bps` (touch at quote → executed VWAP) are
  reported with mean / p10 / p50 / p90.

Any code path that sends a hedge before the maker fill runs **only** under `dispatch_mode="simultaneous"`. The live engine
`trading_platform_poc/opportunity.py` now carries the same distinction explicitly: `OpportunityRequest.dispatch_mode` is
`DispatchMode.CONCURRENT` (every leg at once — the `taker_taker` contract) or `DispatchMode.FILL_TRIGGERED` (Method A: the leg
flagged `LegRequest.maker=True` is dispatched alone; the hedge legs are persisted and dispatched only after the maker leg
reaches its fill deadline or a terminal state, sized to the confirmed filled quantity, partial fills included; if the maker
never fills the hedge legs are `BLOCKED` and never transmitted; `OpportunityResult.legging` carries the maker-fill and
hedge-dispatch timestamps). A request that marks a maker leg under `CONCURRENT` is refused at construction, so Method B
cannot be run by accident. Limitation of this first version: the hedge is sent once, after the maker leg's fill window, not
after each partial fill inside the window. The live `CrossVenueSpreadStrategy` still emits taker/taker opportunities only; a
maker/taker opportunity must be built explicitly with `maker=True` and `FILL_TRIGGERED` before `maker_taker` goes live.

## 4. P&L accounting

* The cash/inventory ledger is the only P&L: `ending_equity = Σ cash + Σ base × mark`, where every venue's mark is its mid at
  the COMMON timestamp — the last time all venues had data (`valuation.py`; R11). Marking each venue at its own last mid
  (`last_mark_equity`, reported as a diagnostic, selectable with `ArbBacktestConfig.valuation="last_mark"`) overstates a hedged
  pair whenever one feed stops early and is NOT a P&L figure. `realized_pnl = gross_pnl − fees_paid − unhedged_loss −
  funding_paid` is an attribution. Markouts (`markout_summary`) and adverse-selection figures are diagnostics; they are never
  subtracted from P&L (pinned by `test_perp_arb_legs.AccountingTests`).
* Fees are charged once per execution, on the fill (`Fill.fee`), including a liquidation's fee (taker fee + liquidation fee on
  that fill). `fees_paid` is the sum of every fill's fee; `margin_report.liquidation_fees` shows the liquidation part;
  `funding_paid` is separate.
* **Equity bridge** (checked at the end of every run, tolerance `1e-6` USDT scaled by traded notional): cash rebuilt from the
  trade log (fills and fees) + funding + transfers must equal the ledger cash, and `ending_equity` must equal that cash plus
  the open position marked at ONE common timestamp, which the bridge computes itself from the recorded marks (so a
  last-mark figure fails the bridge on days such as SOL 2022-01-05). A mismatch raises `EquityBridgeError` (or warns under
  `equity_bridge="warn"`). The check is skipped and flagged when the trade log was truncated (`max_trade_log`).

## 4b. Timing model (WP3, `src/new_backtest/timing.py`, opt-in via `ArbBacktestConfig.timing`)

* Six timelines per venue, each its own seeded stream: entry (decision → venue arrival), ack_report, fill_report (match →
  the strategy learns → hedge dispatch), cancel (request → venue), cancel_report, feed (public data delay). No ack
  round-trip is charged twice: with `timing` set the passive leg's arrival comes from the entry timeline only.
* The strategy learns of a maker fill at max(receipt of the public event that revealed it, exchange time on the host clock +
  fill_report): never before the simulator itself can know.
* Clock: raw `exchange_ts` / `local_ts` are kept; the host offset is a MEASURED input (`ClockConfig`, with uncertainty and
  source tag) — the aligner never learns it from the data. Per-venue delivery delay stays in the data.
* Timer wheel: funding settlements, maker lifetimes (request and effective cancel) and in-flight arrivals fire at their own
  times between market events. Equal-time policy: `market_first` (v1: an event stamped exactly at a cancel's effective time
  is applied first) or `cancel_first` (legacy tie rule). A timer-fired cancel is never deferred.
* Dead-feed detection per venue on ABSOLUTE age: valid / stale (> max_book_age_s) / dead (> dead_feed_s); a dead venue or
  all venues stale blocks entry.
* Two books per venue: the venue-truth book (exchange time; taker arrivals execute against it, post-only is checked on it
  at arrival when the look-ahead window covers that time) and the strategy view (receive time; the only book the strategy
  reads). Counters in `timing_report`.

## 4c. Market data and fill model (WP4)

* Trades de-duplicate by native id when the record carries one, by content key otherwise (legacy). Native fields stay in
  the payload. Raw and aggregated prints are never mixed as independent volume.
* Book states: invalid (no snapshot yet) → valid → recovering (sequence gap: Binance U/u/pu, Bybit seq) → valid (next
  snapshot); stale by age. With `enforce_book_state` no signal and no taker execution uses a non-valid book. Captured depth
  per side is recorded at every snapshot.
* Absent level (`MakerConfig.unknown_queue_model="depth_aware"`): inside the captured depth → queue 0 (legitimately
  empty); outside → UNKNOWN queue (floor or calibrated lognormal) and the order is flagged `queue_unknown`. Legacy: the
  `min_queue_ahead` floor on every absent level.
* Level-creator rule (opt-in `level_creator_trade_only`): the order that created its level fills by public trades only.
* One print fills own orders once in total; the same execution published as trade and depth decrease is charged once,
  whichever arrives first (pinned by `tests/test_wp4_market_data.py`).
* The Jan-2022 SOL files carry no trade ids and no sequence numbers: on that data every id-based path is inert and the
  content-key rule applies.

## 4d. Funding, margin and ledger (WP5)

* Funding: linear (quote-settled) perps only — payment = position × contract_multiplier × mark × rate at each venue's
  settlement time; every settlement is logged (`funding_report.settlements`: venue, time, position, mark and its source,
  rate, payment). A missing mark is flagged (`mark_source=book_mid`); an inverse contract is refused. Predicted financing
  (Σ signal notional × funding_holding_bps) is reported next to the realised figure.
* Margin per venue: tiers → initial / maintenance; `mode` cross (whole account backs the position) or isolated (only the
  position's initial margin and its unrealised P&L); initial margin reserved for RESTING orders; liquidation price per venue;
  ADL exposure flagged when the venue's captured book cannot absorb the liquidation; `max_utilisation` limit. No cross-venue
  collateral; transfers have delay, fee and watermark policy.
* Ledger: fee reconciliation on every fill (schedule × notional, plus the declared liquidation fee on a liquidation) and
  per-venue cash and base identities (1e-9 scaled by the venue's traded notional) are part of the equity bridge and fail it.
* `attribution_pnl` = gross − fees − unhedged − funding is a diagnostic; `realized_pnl` is its deprecated alias. The headline
  is the ledger (cash + common-timestamp open position).

## 4e. Risk, recovery and failure handling (WP6)

* Reachable exposure: resting remainders, in-flight orders, PostOnly-pending arrivals and (opt-in) UNKNOWN orders count
  toward the per-venue cap, the net ceiling (`net_ceiling_multiple` × max_position_base, legacy 2; the perp profile uses 1) and the
  gross long / short limits. `fill_headroom_base` is a SEPARATE parameter (profile starting value 0.25 × max_position_base, the
  assistant's choice, DECISION_PENDING_CALIBRATION); what it buys is sizing room — the cap is ceiling + headroom — so the effective
  limit in the profile is 1.25 × max_position_base, and headroom 0 gives the bare 1× ceiling.
* UNKNOWN orders: the strategy never replaces them blind; the status query has its own failure probability and a bounded
  number of attempts; exhaustion raises an ALARM.
* Unwind: price-banded (`unwind_band_bps`, 0 = market; profile 15 bps) **around the TOUCH of the unwinding venue** (a sell: best bid x (1 - band), a buy: best ask x (1 + band), rounded to the tick; owner 10 Nov 2026, as the OMS does). The reference profiles (`legacy_v0`, `v1`, `v1_1`, `v1_2`, `v1_3`) keep the old backtester's band around the MID unchanged, byte for byte with `unwind_max_attempts` total sends (profile 3, independent of
  the hedge retries); exhaustion raises a persistent ALARM that
  blocks new risk (counted) until `reset_alarm()` — an operator action, never automatic. Exposure retained is reported.
* Breaker (corrected 2026-11-01: the earlier text said "six triggers, each tested" — that was true of the `CircuitBreaker` class
  in isolation only; through the backtester the breaker crashed at its first call (`len()` of a generator) and the gateway-down
  and UNKNOWN triggers were never fed, so no earlier backtest result contains breaker behaviour). Now wired and pinned through
  the backtester (`tests/test_r12_breaker.py`). Owner's starting values (DECISION_PENDING_CALIBRATION, in the profile): 3
  consecutive hedge failures; > 5% failures in the last 20 hedges (needs a full window); any failed unwind; |net| != 0 for more
  than 5 s; an order UNKNOWN for more than 2 s; gateway down (silent for `heartbeat_timeout_s`, ASSUMED 10 s). Counting rule: one
  hedge FAILURE per hedge chain (its first failed attempt) — a rejected hedge and its retries count once; only a clean
  first-attempt hedge counts as a success; unwind failures feed the separate failed-unwind trigger. UNKNOWN age starts when the
  ack timeout expires and stops at resolution. Reset is MANUAL ONLY (`reset_breaker()`). A trip blocks new signals AND cancels every
  resting maker order (implemented 2026-11-01 at the owner's request; two earlier reports had claimed it before it existed): each
  order gets a cancel request at the trip with its own cancel latency, fills inside that window are kept and hedged, an order
  that lands after the trip is cancelled at placement, and the handler runs once per trip (a manual reset re-arms it). The 5 s
  net-exposure rule is kept as specified.
  Fault injector: `reject_prob` 0 / 0.1 / 0.3 changes failed-hedge counts and cash (pinned); the stressed
  preset no longer hits a hedge with both the hedge and the by-venue reject probability (`venue_reject_applies_to_hedges`).
  Restart is a MODELLED scenario (`restart_s`, `restart_at_fills`), not the live `RestartCoordinator` path.
* Order-state parity with the specification, under the OMS02 resolver (`crypto_stat_arb.oms._resolve_order_state`): a late
  acknowledgement clears UNKNOWN (D-04), a cancel in flight is CANCEL_PENDING, still live and fillable (D-05;
  `OMS.note_cancel_requested` records it), a reject after a fill keeps the fill, re-queries the venue
  (REJECT_AFTER_FILL_REQUERIED) and records REJECT_AFTER_FILL_ALARM once per order (D-06). Names against
  `order_state_machine`: SENT = SUBMITTED, CANCEL_PENDING = CANCEL_REQUESTED, TIMED_OUT = UNKNOWN; stored state
  "CANCEL_REQUESTED" (the 1 Nov local design) reads as CANCEL_PENDING. Still open and documented:
  D-01 (the simulator cannot reject a resting order after acceptance), D-02 (no acknowledgement state in the simulator),
  D-03 (resolved only under `cancel_time_basis="exchange"`).

## 5. Strategy specification

| review item | value for this strategy |
|---|---|
| residual / spread construction | price difference of the same perpetual on two venues; no residual model |
| point-in-time universe | the single configured pair (`ArbBacktestConfig.pair`); multi-pair runs are independent pairs (`batch_backtest`) |
| hedge ratio and estimation cutoff | N/A — same instrument on both venues, hedge ratio fixed at 1 (1 contract hedges 1 contract) |
| warm-up | books must be `ready` and fresh (`max_book_age_s`), touch confirmed within `max_touch_age_s`; venue state EWMAs start at the first event |
| stationarity / regime checks | `block_on_toxic` (trade-sign and depth-collapse), `cross_venue_skew` gate, lambda regime bucket feeds the fill gate; no cointegration test (N/A, same instrument) |
| entry rule | conditional net edge ≥ `min_net_edge_bps` after fees and buffer; fill-probability / adverse / TTF gates when a temporally valid `FillModelV2` is loaded |
| exit / rebalance rule | the hedge is the exit of the maker leg; positions are per-venue (long one venue / short the other) and are not rebalanced across venues by the strategy; the margin model's transfers move equity, not position |
| portfolio constraints | `max_position_base`, per-venue imbalance cap, hard net ceiling, pending-exposure counting, margin gate (opt-in) |
| model expiry | `FillModelV2` is refitted per walk-forward fold and refused by the leakage guard when `trained_until_ts` is after the run start |

Backtest vs live code paths: the backtest signal/edge/dispatch code is `src/new_backtest/{arb_strategy,edge,backtester}.py`. The
live/paper path is `src/crypto_stat_arb/strategy.py` (`CrossVenueSpreadStrategy`, taker/taker only, top-of-book edge with the buy
ask as reference, `taker_fee` from the dated fee table) executed by `trading_platform_poc/opportunity.py` (concurrent dispatch).
They are **different code**: the live strategy has no maker/taker signal, no depth-weighted edge and no conditional edge; the
live engine does have fill-triggered dispatch (`DispatchMode.FILL_TRIGGERED`, 2026-11-01) but nothing in the live strategy
produces a maker leg yet. `crypto_stat_arb/sdk.py` maps a `MAKER_TAKER` execution mode onto the backtest only.

## 6. Known defects kept for reproducibility

`ArbBacktestConfig.delta_uses_book_size=False` is a known defect (level removals never drained a queue); it is kept only so
results published before the fix can be reproduced. The default is the fixed behaviour. A label is not a fix: results that still
rely on the legacy setting are wrong by the amount the followup report measured.

## 4f. Perp order lifecycle (2026-11-02)

* **Calibration is product-checked.** `perp_perp_maker_taker_v1` names `config/lifecycle_calibration_perp_bybit.json` in its `calibration:` block. `profiles.resolve_calibration`
  refuses a file whose `product` is not `perp` (the spot file has none and is read as spot), whose venue differs, or whose content hash no longer matches. The file's sha256, content hash,
  venue, product and networks go into `release_identity["calibration"]`. The files hold TESTNET measurements taken from the Mac; there is no mainnet order-event data.
* **`postonly_reject_model` is MANDATORY in a perp profile.** Arrival = decision + a latency drawn from the measured entry grid (REST round trip / 2, local clock only).
* **Venue message semantics** live in `new_backtest/venue_semantics_perp.py`. A PostOnly would-cross is `postonly_reject` on both venues (Bybit: Cancelled + EC_PostOnlyWillTakeLiquidity with no New before it;
  Binance: REST -5022 observed, stream EXPIRED on a GTX order not cancelled by us = spec, not observed). An unmapped status raises `ORDER_TRANSITION_UNEXPECTED`.
* **MakerExecutor has explicit REJECTED and UNKNOWN states** (`lifecycle_state`, bookkeeping only) and `venue_reject_notice` (a reject after a fill keeps the fill, one alarm). The legacy-default
  engine keeps D-01..D-03 on purpose (legacy_v0 bit-identical); the parity matrix has a `maker_perp` column in which nothing is expected to fail.
* **Injector additions:** `outage_on_fill_venue` ("hedge" | "maker") and `ack_unknown_scope` ("both" | "hedge" | "maker"); defaults reproduce the old behaviour.

## 4g. Canonical execution layer, phase 0 (2026-11-03)

* **One place decides how an order is worked.** `new_backtest/execution/` holds `ExecutionIntent` (what the strategy wants: role, side, qty, venues, `worst_price`, urgency, deadline, reason), the tactic registry (three tactics, see 4h.6) and `ExecutionLayer`. `CrossVenueArbBacktester._send_leg` requires an intent, so every order that goes through the in-flight queue has one. Every child `InFlightOrder` carries `intent_id`. **Not yet routed (named, not hidden):** the margin model's liquidation and flatten fills (`_margin_step`) and the legacy latency-free `_unwind` execute against the book directly without a child order; the latter is journaled (`note_instant`), the former is not.
* **Phase 0 changes no behaviour.** The maker leg is a `PASSIVE` intent with placement `deep` (price: the edge-required quote the strategy computes), the fill-triggered hedge an `IOC_CAPPED` intent for the filled quantity (cap rule `touch_slack`: hedge-venue touch moved by `max(arb.max_walk_bps, 5)` bps, re-taken on every attempt, computed in the layer independently of `ArbStrategy.hedge_leg` and checked against it), the price-banded unwind a `MARKET_BANDED` intent; the latency-free legacy `_unwind` is journaled, not routed. Intents draw nothing random and touch no engine counter. Gate 0 (2026-11-03): `legacy_v0` and `perp_perp_maker_taker_v1` on 2022-01-05 and 2022-01-13 (singapore, bybit_perp_mm, clean) are identical before and after: trades, fill events, fill-bucket stats and model settings byte for byte, every `summary.json` field except `wall_s`, `release_identity` and `switch_table` (those change by design: code hash, profile hash, the `execution` block, two new switch rows).
* **Rules enforced in the layer.** The intent is journaled before its first child. Every intent but unwind / flatten carries a `worst_price` and each child stays inside it (`price_cap_required`, CALIBRATED: true in both profiles, may be changed only by the cap decision; see 4h.7). A hedge intent exists only for a quantity already filled on its parent (except `dispatch_mode: simultaneous`, which hedges before the fill on purpose). One live hedge child per hedge intent (`one_hedge_child`, MANDATORY in perp, never `off`): **`count` today, not `enforce`.** A probe (out/exec_layer/one_hedge_child_probe.txt) shows the ghost outage policy plus retries puts up to four hedge children live at once; `enforce` would refuse the retry sent while the ghost is live, which is a behaviour change reserved for a gated phase.
* **Profile.** Both `legacy_v0` and `perp_perp_maker_taker_v1` carry an `execution:` block with the values that reproduce today. `pairing` must equal `arb.mode`; `maker_maker` raises until phase 3. The resolved block and its hash are in `release_identity["execution"]` and the result's `execution_report`.

## 4h. Canonical execution layer, phase 0 hardening (2026-11-03) — SPECIFICATION

Written before the code. Phase 1 (policy optimisation) does not start until every rule below has a test, and the four
mutation tests in `tests/test_execution_contract.py` fail when the rule is broken. Nothing here promotes a tactic to production.

### 4h.1 Intent lifecycle

States: `accepted, working, partially_done, done, escalated, failed, unknown`. `done` and `failed` are terminal. A transition not
in the table raises `IntentError`; nothing in the engine moves a state any other way (`ExecutionIntent.move`).

| from \ to        | working | partially_done | done | escalated | failed | unknown |
|------------------|:-:|:-:|:-:|:-:|:-:|:-:|
| accepted         | x |   |   |   | x |   |
| working          | x | x | x | x | x | x |
| partially_done   | x | x | x | x | x | x |
| escalated        |   | x | x | x | x | x |
| unknown          | x | x | x | x | x |   |
| done / failed    |   |   |   |   |   |   |

* `accepted -> working` when the first child is sent. `failed` straight from `accepted` = refused or never sent (a primary whose
  maker order was rejected before it rested, a hedge whose cap was unreachable on every attempt).
* A child that the strategy has given up on but that is still delivered later (`ghost`, an acknowledgement lost) makes the intent
  `unknown`. **An `unknown` intent is never settled and never gets a second live child**: a hedge retry waits for the outcome of
  the first (the venue is queried once the ghost's release time plus the acknowledgement delay has passed). `unknown -> working`
  when the child is resolved.
* `settle(intent)` is the only way to a terminal state: `done` if the filled quantity covers the intent, otherwise `failed`
  (a partial fill that was closed short is `failed` with `filled_qty > 0`). It is a no-op while any child is live.
* A fill after a terminal state is an **overfill**: counted, journaled, never a transition.

### 4h.2 Atomic risk reservation

* Only a `primary` intent adds exposure; hedge, unwind, flatten and rebalance intents reduce it and are exempt from the check.
* `open_intent`/`open_group` run **check and reserve as one call**: the worst-case exposure with the new legs added is compared to
  the limits, and only if it fits are the legs reserved, the intent created and journaled. A refusal changes nothing: no serial
  number, no journal line, no reservation, no counter other than `risk_refusals`. A signal with several primary legs is reserved
  as a group, all or none.
* Limits mirror `ArbStrategy._inventory_room` (per-venue cap `max_venue_imbalance_base` or `net_ceiling_multiple x max_position_base
  + fill_headroom_base`; net ceiling `max_net_base_hard` or the multiple, plus headroom), applied to the venues the intent touches
  and to the net. Worst case, nothing nets off: reserved buys count on the buy venue, reserved sells on the sell venue. The layer
  keeps its **own** reservation book; it does not read the engine's `PendingExposure`.
* A reservation is consumed by fills (`note_filled`) and released in full at the intent's terminal state (`settle`) and at
  `finalize`. `reservations_open` must be zero for every settled intent; a leak is reported and, in `enforce`, fails the run.
* `execution.risk_reservation`: `off | count | enforce` (MANDATORY, never `off` in a perp profile). `count` records what would be
  refused; `enforce` refuses and the engine drops the signal (`signals_refused_by_risk`).

### 4h.3 IOC cap semantics

* A taker child carries the intent's `worst_price` at the moment it is sent (`cap_at_send`). A buy never fills above it and a sell
  never below it. The engine checks this **again at booking time** from the fill, independently of the tactic that built the order.
* A cap may be re-taken from the then-current touch only under the `touch_slack` rule (and the banded unwind): `retarget` takes the
  touch and refuses a cap looser than `touch -/+ max(max_walk_bps, slack_bps_floor)` bps. Any other cap rule (phase 1: the break-even
  cap) is fixed when the intent is opened and `retarget` raises.
* A partial-fill remainder keeps its cap: the retry is for `intent.qty - intent.filled_qty` at most (counted as
  `retry_exceeds_remaining`; refused under `one_hedge_child: enforce`) and is re-capped by the same rule.
* A cap that no price can satisfy (phase 1: break-even unreachable) sends nothing, is counted (`hedge_cap_unreachable`), and the
  chain goes down the ladder to the price-banded unwind.

### 4h.4 One live hedge child, fill-triggered

* A hedge intent is opened only after the maker fill that creates it, for at most the filled quantity (`dispatch_mode:
  simultaneous` is the one deliberate exception, a comparison mode).
* `execution.one_hedge_child` is `enforce` in `perp_perp_maker_taker_v1` and stays `count` in `legacy_v0`, so the legacy
  behaviour is reproducible bit for bit while the canonical contract is the default for the new profile. Clean runs (no injected
  fault) are identical under both; the two differ only when a ghost order is live, which `count` lets the retry overlap.

### 4h.5 Independent reconciliation

`execution/reconcile.py` rebuilds the books from the fill journal **only** (every fill the engine books is journaled by
`_on_fill`, with the intent that sent it or an explicit kind: `instant_unwind`, `margin_liquidation`, `margin_flatten`,
`synthetic`) and compares them with the engine:

1. every fill is attributed — to an intent whose venue and side it matches, or to an explicit kind;
2. fee = qty x price x bps from the venue schedule, recomputed (plus the declared liquidation fee), equals the booked fee;
3. per-venue base and cash rebuilt from the journal equal the inventory ledger; total cash less funding equals `cash_pnl`;
4. the journal and the engine's `trade_log` hold the same fills (count, notional, row by row);
5. every taker child fill is inside the cap it was sent with;
6. no intent is filled beyond its quantity (an overfill is reported; under `enforce` it fails).

It runs at the end of every replay; a failure raises `ExecutionReconciliationError` unless `equity_bridge == "warn"`. It is a
second ledger over the same fills, so it cannot detect a wrong fill *price*, only a fill that is mis-booked, mis-fee'd, lost,
double-counted, unattributed, or booked outside its cap. The fill model itself is validated elsewhere (`probe_crosscheck`).

### 4h.6 Tactic set (decision 2026-11-03, replaces the seven-tactic registry of 4g)

Only three tactics exist. There is no speculative-IOC and no scheduled tactic, and no code path or stub for them.

| tactic | used for | notes |
|---|---|---|
| `PASSIVE` | the maker leg | PostOnly. `placement` in `{touch, improve, deep}`; one order type, fill model, lifetime and cancel/replace rule for all three |
| `IOC_CAPPED` | the hedge, and taker_taker legs | capped at the intent's `worst_price` |
| `MARKET_BANDED` | breaker flatten and unwinds only | the only tactic that may carry no `worst_price`; its band is +/- `band_bps` around the venue's TOUCH, not the mid (owner 10 Nov 2026; reference profiles keep the old mid band) |

* `touch` = best bid (buy) / best ask (sell). `improve` = one tick inside the spread when the spread is wider than one tick, else the
  touch. `deep` = the price the edge requires, or `deep_ticks` ticks behind the touch when `execution.primary.deep_ticks` is set.
  Every placement is capped by the edge price: a buy never rests above it, a sell never below it, and a price that would cross the
  book falls back to the touch. The intent's `worst_price` is the edge price, so the cap check applies to all three.
* **Today's behaviour is placement `deep` in both profiles** (confirmed from `ArbStrategy.evaluate_maker_taker`: the order rests at
  `max_bid`/`min_ask`, the price the edge requires; `legacy_v0` computes it from the naive edge — `price_rule: naive_edge` — and
  `perp_perp_maker_taker_v1` from the conditional edge — `price_rule: conditional_edge`; neither quotes at the touch, and
  `ArbConfig.maker_improve_ticks` is declared but never read). `deep` without `deep_ticks` sends the strategy's price unchanged, so
  replays are bit-identical. `touch` and `improve` re-price the maker leg in `_fire`, after the intent and its reservation exist.
* The profile `execution:` block names `tactic: PASSIVE` / `IOC_CAPPED` / `MARKET_BANDED`; any other name (including the retired
  `PASSIVE_TOUCH`, `PASSIVE_IMPROVE`, `PASSIVE_DEEP`, `IOC_SPECULATIVE`, `SCHEDULED`) is a startup error.

### 4h.7 Observation records and the public-book invariant (brief steps 6-8, 2026-11-03)

* **Observation only.** `execution/observation.py` and the `obs` dict on a maker `Fill` are written by the engine and read by nothing in it
  (a test greps the source); they draw no random number and change no state, so replays are identical with them on or off
  (`ArbBacktestConfig.record_maker_obs`, default on; `maker_observations.jsonl` is written with `output_dir`).
* **Per maker order, at placement:** relative queue position (queue ahead / level size), near-side and opposite-side size at our price,
  the touch imbalance (B - A) / (B + A) on BOTH venues, the hedge venue's touch at the decision. **At each maker fill:** the same,
  plus the fill record: cause tag (`trade`, `trade_through`, `trade_after_front`, `delta_share`, `delta_vanish`, `snapshot`,
  `opposite_cross`) and the print's own id when the record has one (Jan-2022 files carry none), volume offered by the event and
  consumed by the queue ahead and by the fill, the queue estimate before the fill, the netting credit (`dedupe_trade_depth`) used,
  the cancel model, and uncertainty flags: `queue_unknown` / `outside_captured_depth` (level absent and outside the captured depth),
  `queue_proxy` (absent level standing in `min_queue_ahead`), `ambiguous_trade_depth` (a netting credit or fill-evidence rule
  attributed volume, or the fill came from a depth decrease). **At each hedge arrival:** the hedge venue's touch at arrival and at the decision.
* **Invariant (`tests/test_public_book_invariant.py`).** A reference engine fed the same public events and no orders is the public
  reconstruction. An own MAKER fill never changes the engine's book. An own TAKER fill never does either when
  `consume_liquidity` is off. **With `consume_liquidity` on (the engine default, used by both profiles) a taker fill removes what it
  walked from the hedge venue's VIEW book until a feed update overwrites that level — a deliberate own-impact model, not a violation
  of the maker rule, and counted in `maker_observations.own_taker_book_consumption`.** Switching it off changes every replay's
  P&L slightly, so it is a decision for the user, not part of a no-behaviour-change phase.

## Switches required for real orders (owner 13 Nov)

Two tables, two jobs. `MANDATORY_CONTROLS` (`crypto_stat_arb/config.py`) lists what every non-legacy profile must have on. `REAL_ORDER_REQUIRED` (same file, checked by `check_real_order_profile`) lists the switches that are
OFF in the older reference profiles, so they cannot be mandatory for those, but that a profile used with REAL orders (live, paper against a venue, testnet drills) must have ON. A replay writes the result of the
check into its manifest (`config_notes.real_order_requirements`: `met`, or the list of what is missing). **`perp_perp_maker_taker_v1_10` is the profile named for real orders and for the testnet drill (owner 16 Nov) and meets every row.** v1_9 meets the first two rows only (it lacks `dust_carry`, so it is refused for real orders); v1_8 and earlier do not, and say so.

| Key | Required | Why |
|---|---|---|
| `oms.maker_ack_deadline` | `true` | A sent PostOnly quote is watched from the send, not only from its ack. An unanswered submit is queried after the report timeout, never resent, and after `max_queries` is TIMED_OUT with the kill switch. Without it a lost submit sat SENT and unwatched for 565 s in the 2l replay. |
| `hedge_faults.silent_carry_bound` | `below_one_lot` | A failed hedge always acts. When a hedge chain exhausts its retries the residual is unwound with MARKET_BANDED at the touch band (`unwind_band_bps`, 15) whatever its size; an unwind that cannot be done is the persistent alarm (`UNWIND_EXHAUSTED_ALARM`, no new risk until an operator reset). Only a remainder below one hedge-venue lot, which the venue cannot be sent, is carried silently, with the end-of-day warning. The key sets the residual bound (`oms.residual.max_residual_base` and `risk.max_residual_base`) to the hedge venue's lot; `position_size` (the older profiles) sets it to the position size, so a failed 0.0876 SOL hedge under a 1.0 SOL bound was carried with no action. |
| `hedge_faults.dust_carry` | `next_hedge` | Owner 16 Nov. A hedge chunk below the hedge venue's minimum quantity or notional is carried in one signed balance per venue and instrument, added to the next hedge, sent alone as soon as it reaches the minimum, never refused and left open; one age alarm after `dust_alarm_after_s` (ASSUMED 60 s). v1_10 and later. Reduce-only exemption from the minimum notional is UNVERIFIED and is item 1 of the testnet drill. |

**The failed-hedge-always-acts behaviour exists only with `silent_carry_bound: below_one_lot`** (v1_9 and later, and every profile used with real orders). With the older bound (`position_size`, the setting of v1_5 to v1_8) a hedge chain that exhausts its retries on a residual below the position size is carried with no action. So **v1_7 and v1_8 results predate the behaviour**: their failed hedges were carried, not unwound, and their P&L and residual figures must not be compared with v1_9's without saying so. The 2c rerun of 13 Nov shows the difference on one plan: the 0.0876 SOL plan whose 0.08 hedge failed three times was carried under v1_7/v1_8 and is unwound (filled in full, 0.0 bps against the maker price) under v1_9, leaving 0.0076 SOL, below the lot.

Exchange-side restart reconciliation (`OMS.reconcile_account_after_restart`, same date): after a restart the OMS asks every venue for its account (`account_snapshot(since_ns)`: orders open and closed, executions, positions),
closes `restart_reconcile_gate` first, books what happened while it was down once (cumulative, executions de-duplicated by execution id and required to add up to the venue's order state), sends the hedge a missed maker fill
triggers, and opens the gate only when every order, execution and position agrees. A venue order that is no plan's is flagged and holds new risk; it is never cancelled automatically. A venue whose account cannot be fetched stays
unreconciled. Tests: scripted venues (`tests/test_restart_resilience.py`, `ExchangeSideRestartReconciliationTests`) and, for the live adapters, hand-written recorded messages in each venue's response shape (`tests/test_perpetual_path.py`, `AccountSnapshotTests`, and one OMS end-to-end case). **Live adapters (owner 13 Nov, built):** `TestnetVenueAdapter.account_snapshot(since_ns)` over the perpetual transports in `testnet.py` (Bybit v5 linear, Binance USDⓈ-M), read-only (`ACCOUNT_SNAPSHOT` asks for the read permission, never the trade one). Orders and executions are keyed by the CLIENT order id the platform sent (Bybit `orderLinkId`, Binance `clientOrderId`; a Binance trade carries only the venue's order number, which is mapped through the order, or fetched once by that number); an order or execution with no client id is reported as `venue-order:<id>` and the OMS flags it. Limits used (venue documentation read 13 Nov): Bybit order history and execution list in windows under 7 days, cursor paging, 50 orders and 100 executions a page, execution rows of type Trade only; Bybit keeps a Cancelled, Rejected or Deactivated order with no fill only 24 hours; Binance `allOrders` and `userTrades` windows under 7 days (6 used), 1000 rows a page, 90 days of history, an order cancelled or expired with no fill is dropped after 3 days. A `since` older than 90 days, a page that never ends, an unmapped order status, a position in hedge mode a Binance order window that returns a full page (1000) and any HTTP failure raise, so the OMS keeps the gate closed. With nothing persisted to start from (no open plan, no execution) the OMS asks for the last 24 hours (`restart_default_lookback_ns`), never from the epoch. **A gap since the last persisted state longer than that lookback is not reconciled automatically** (owner 13 Nov): nothing is fetched, every venue gets the problem `restart_gap_exceeds_lookback:<gap>h><lookback>h`, the ledger records `RESTART_GAP_EXCEEDS_LOOKBACK`, the report carries `gap_hours`, and the restart gate stays closed (an operator reconciles, or raises the lookback knowingly). A venue that refuses the window (older than it keeps: 90 days at Binance, 24 hours for a Bybit order cancelled or rejected with no fill) keeps the gate closed with `account_snapshot_refused:<reason>`. NOT confirmed from the venue pages: Binance `userTrades` ordering inside a window (the pager moves forward by the last trade's time and de-duplicates by trade id) and the `positionRisk` sign convention (a wrong sign shows as `venue_position_differs`, never as a pass): both are for the testnet restart drill, which is not built yet.

**Replays run with the profile's own safety values (owner 13 Nov).** `SessionReplay(profile_safety=True)` (every v1_9 acceptance replay) takes the layer risk gate's mode from `execution.risk_reservation` (mandatory: enforce) and the circuit breaker from the profile's `breaker` block, instead of the replay's own defaults (gate in count mode, no breaker); an explicit `layer_risk_mode` or `breaker_config` still wins, and without the flag every earlier replay is unchanged. The layer route mode and the shadow reconciliation mode have no profile key (count), and the session's pre-send risk gate is off; the manifest's `safety_from_profile` says what ran. 13 Nov result: the enforce gate refused nothing in 22 runs and changed no order, fill or gate decision of any v1_9 case (stated explicitly); the breaker changes four cases (see the acceptance report findings).

**Circuit breaker: dust and flatten (owner 13 Nov).** (1) The net-exposure trigger ignores a net residual below one lot of the hedge venue (`on_net_update(..., dust_below=)`, the session passes the hedge venue's `quantity_step`): untradeable dust, still counted in the exposure (breaker summary `dust_residual_base`, the end-of-day report `breaker_dust`, an end-of-day warning), but it never starts the `net_exposure_s` timer. One lot or more held past the limit trips as before. (2) After a trip a flatten remainder below the venue's minimum quantity or notional is DUST: `BREAKER_FLATTEN_DUST` (or `UNWIND_DUST` for a flatten plan inside the OMS) is recorded ONCE with the minimums and the lot, it is never retried, the trip stays latched until the manual reset, and the dust is shown in the alarm payload and the end-of-day report. (3) A flatten skipped because the venue is unavailable is retried 1 s, 2 s, 4 s ... capped at 30 s (not on every event). (4) Logging: one `UNWIND_VENUE_SKIPPED` per state change (reason change) plus the counters `unwind_venue_skips`, `unwind_venue_skip_retries_suppressed`, `unwind_dust_plans` and the breaker's `breaker_flatten` counters; never one per event (the case that wrote 168,133 such events now writes none). An arb residual above the OMS bound but below the venue minimum is still the 5 s hold and the kill-switch alarm of the residual rule, logged once.

**Breaker trip: closed once (owner 13 Nov, found in the end-to-end rehearsal preparation).** On a trip every hedge chain with no order in flight (not sent, rejected, waiting to retry, partly filled) is escalated at once (`HEDGE_CHAINS_ESCALATED`) and unwinds ONCE through its own deterministic path; a chain with an order in flight or unknown is left to resolve (the order may have executed). The breaker flatten covers only the net that no open plan manages (`open_plan_position_by_venue`): `BREAKER_FLATTEN`, or `BREAKER_FLATTEN_DUST` below the venue minimum. Before this the flatten and a retrying chain's own unwind exited the same exposure twice (4.07 SOL of unwind volume for 2.09 SOL of exposure in 2c). A breaker flatten plan counts only its own fills in the residual totals (`_flatten_shadow`), so carried plus open residual equals the ledger net; a liquidation flatten still counts its inherited exposure in full. The alarm of a trip is the `BREAKER_TRIP` event with its alert payload, latched until the manual reset; `BREAKER_ALARM` is the additional alarm when a flatten cannot be sent.

**Production limits approved (owner 13 Nov)** as proposed in `out/integration/production_limits_proposal.md` with two changes: silent residual carry only below one hedge-venue lot (0.01 SOL) and failed hedges always unwind or alarm (not 0.10 SOL); hedges are never refused by the limiter (they queue with priority and may use the whole window), new quotes use at most 70% of each window with at least 2 requests free (`oms.rate_limits.quote_share 0.7`, `quote_min_free 2`, now read from the profile), cancels unrestricted; every value without data is ASSUMED. They are in `perp_perp_maker_taker_v1_9.yaml` (the `oms.rate_limits` block, `arb.max_venue_imbalance_base 2.0` gross per venue, and a documentation block `_production_limits_approved_13nov` that says, item by item, what is ENFORCED or CONFIG and what is NOT BUILT: new orders per second, daily loss stop, funding quiet window, clock-offset gate, margin WARNING stop, kill after 60 s of missing marks, tier table at startup, account-settings check wired into the session, the 1 s re-quote delay, the 10,000 events/s proof) and the real-orders table (`oms.rate_limits.quote_share`, `quote_min_free`). The proposal's "layer gate in count until validation" conflicts with the earlier order to run v1_9 in the profile's enforce mode: the later order stands (it refused nothing in 22 runs). The run scripts still set their own gross per venue (scenarios.py 5.0); `run_day.py` uses the profile's value unless `--gross` is given.

## Market-making quote: pairing none (owner 13 Nov, CR-MQ part 2)

A market-making quote is a PASSIVE intent with `execution.pairing: none` on the ems layer, through the existing plan model: `OMS.execute_quote` sends ONE post-only GTC child, with no hedge spec, no hedge and no unwind; fills book to inventory once; the quote is amended with `request_amend` (REPLACE, AMEND_PENDING, a fill during the pending amend counts once) and cancelled like any resting order; its plan closes when its order (the whole amend lineage) has ended (`QUOTE_FILLED`, `QUOTE_PARTIAL_THEN_ENDED`, `QUOTE_ENDED_UNFILLED`). Admission has two gates beside each other: the layer's per-pair inventory cap (`execution.quote.inventory_cap_base`: filled net base plus resting quotes plus the new quote, per side; a leg that reduces the inventory is never refused by the cap; `execution.risk_reservation` count or enforce as for arb) and the core notional / margin / position reservation for the ONE leg (`RiskEngine.reserve(..., only_side=)`: no legging-exposure term and no carried-residual term, because there is no package to leg out of). A quote's fills are inventory, not an arb residual: they are left out of `carried_residual_base`, the residual snapshot and every residual rule (bound, unwind, alarm, hold on breach), so a market-making inventory is never read as an arb residual (no HEDGE_UNWIND, no unwind order, nothing carried, also after a restart: the `QUOTE_PLAN` marker is persisted), while the alarms and holds on new risk still hold new quotes. **One OMS instance is either a quote book (pairing none) or an arb book, never both** (owner 13 Nov): `execute_quote` needs the layer's pairing none and `execute` refuses an arb package under pairing none; a process that runs both strategies runs two OMS instances. An amend's replacement order belongs to the quote's intent, so the layer's fill journal and the cap arithmetic count every fill once. Pairing none is refused for every arb profile at load (`ProfileError`), `ExecutionConfig(pairing="none")` needs `quote.inventory_cap_base`, and the `quote` key is absent from `as_dict` unless set, so every existing profile's execution hash is unchanged (checked for v1 to v1_9 and legacy_v0). There is no `oms` switch and no new plan-kind enum. Replay identity of the arb path after this change is stated in the plan entry of 13 Nov with what was actually rerun. **Session-level quote interface (owner 13 Nov, CR-MQ part 3; off unless a strategy opts in).** A strategy with the exact attribute `quote_book = True` is called by `TradingSession.on_event` (the one call site: the session replay and `LivePaperSession` both go through it) after the timers have run, the book is applied and the gate has allowed: `strategy.quotes(books, now_ns)` returns a list of actions (`strategy.py`): `PlaceQuote(quote_id, venue, side, price, qty)` -> `oms.execute_quote`; `AmendQuote(quote_id, price, qty=None)` -> `request_amend` (REPLACE with AMEND_PENDING, the fill on the old price kept; a quantity change is refused); `CancelQuote(quote_id)` -> the normal urgent cancel of the quote's whole lineage; `ExitInventory(quote_id, venue, side, qty, cap_price)` -> `oms.execute_exit`, one reduce-only UNWIND child, IOC, limited at the cap (the worst price, MARKET_BANDED in the layer's vocabulary), a quote-book plan whose fills are inventory moves and never an arb residual, never held by the stops that block new quotes. A new quote is refused with a reason when the session is flattening, the breaker is paused or tripped, the margin price is missing (`blocked:<why>`), or the rate-limit quote share is used up (`rate_limit_skipped`); the layer's per-pair inventory cap applies inside `execute_quote` (`rejected:<why>`). Callbacks, keyed by the strategy's own quote id and each optional: `on_quote_ack(quote_id, accepted, reason, ts)`, `on_quote_fill(quote_id, qty, price, fee, liquidity, ts)` (each unique execution once: they are driven by the ledger's de-duplicated FILL events, so a repeated report or a late fill on the old order during an amend is one callback; the replacement order is the same quote), `on_quote_closed(quote_id, reason, ts)` (`filled`, `cancelled`, `cancelled_after_partial_fill`, `rejected:<why>`, `blocked:<why>`, `rate_limit_skipped`, `ioc_partial_fill`, `ioc_not_filled`, `amend_refused:<why>` when no live order is left) and `on_quote_amend_refused(quote_id, reason, ts)` (an amend refused while the quote is still live: reported separately so the strategy does not read a live quote as closed). Callbacks are delivered in the order ack, fills, closed, at the next pump (every event and every trade). Not built: a quote the book gate leaves resting when a book goes invalid is not withdrawn by the session (the order lifetime watchdog and the strategy's own cancel apply); `AmendQuote` cannot change the quantity. Tests: `tests/test_oms_amend.py` `SessionQuoteBookTests` (placed, amended, partly filled, cancelled; a late fill after the cancel request booked and reported once; the inventory cap; an exit IOC; a blocked quote; an arb-like strategy without the exact opt-in is never called), and the arb suites are unchanged (the hook is absent).

Not built: the two-book eligibility alignment does not apply to a one-venue quote (the book gate still stops quoting on an invalid book), and the market-making strategy that calls it is the MM session's.

## Funding window, funding in the edge, participation limit, S0 repricing in the session runtime (owner 13-14 Nov)

**Funding in the edge.** `arb.funding_expected_holding_s` (v1_9: 28800 s = 8 h, ASSUMED, to be replaced by the measured median holding time from the stage-0 logs, owner-approved) with `arb.funding_credit floor_zero`: `funding_holding_bps` = the NET funding of the pair, the long venue's paid minus the short venue's received, per direction, from each venue's current predicted rate (the derivative stream, `TradingSession.on_derivative`); a net credit counts 0. With no rate (a replay without derivative data) the term is 0 and counted (`funding_term_unavailable`).
**Funding stop-quoting window.** `arb.funding_window_s` (v1_9: 30): no new quote within 30 s before a funding timestamp on either venue (the next funding time from the derivative stream), every resting primary quote withdrawn when the window starts (reason `funding_window`), quoting resumes when the stream shows the next settlement, or `funding_window_after_s` (ASSUMED 30 s) after the timestamp. No next-funding time = NOT EVALUABLE: counted (`funding_window_not_evaluable`), nothing blocked, labelled in the replay's config notes.
**Fees at start-up.** A session that sends real orders reads the account's maker/taker rates on both venues and refuses to start if one cannot be read or differs from the profile (`fees.verify_account_fee_rates`); a replay books the profile's fee presets and says so.
**Participation limit (E3, ASSUMED, owner to approve).** `arb.participation_max` 0.10 over `arb.participation_window_s` 60: a NEW quote's size is capped at that share of the public volume printed at its price level on the maker venue in the window (`VenueState.level_volume`); a cap below the minimum clip refuses the quote (reject `participation`), a cap that only shrinks it is counted (`participation_size_capped`). Hedges keep their depth cap and are never blocked by it. `maker.participation_cap` (v1_9 on): one budget per public print across every own order; `new_backtest.session_replay.participation_audit` checks a whole replay's own passive fills per level against the public volume printed there. Own taker depth consumption (hedge then retry sees the reduced depth) is the adapter's rule (`DeterministicVenueAdapter`, tested in `tests/test_e3_participation.py`).
**S0 repricing in the session runtime (v1_9s).** ONE controller per resting quote (`TradingSession._reprice_quotes`), on every book update after the quote-validity withdrawals: (a) validity withdraws a quote whose edge is gone; (b) otherwise the strategy's `quote_targets` (`requote_target`: the edge at the current hedge-venue VWAP for the quote's remaining size, the S1 table at `adverse_requote_horizon_s`) goes to the layer's `decide_reprice` (stickiness band, `min_interval_ms`; the 15 s lifetime stays the OMS backstop), amend first through `OMS.request_amend`, cancel-and-replace as the fallback (the OMS sends nothing in place of a cancelled quote: the replacement is a fresh signal through validity and the risk gate); (c) otherwise keep. The layer decides, the OMS transports. A request is a request: a quote counts as repriced only on the venue's confirmation (`AMEND_CONFIRMED`); until then the old order is live at the old price and its fills count; an amend is quote-tier rate-limit traffic and is skipped (never queued) with no room; hedges and cancels are never refused. v1_9 keeps S0 off; v1_9s = v1_9 + S0 on + quote_withdraw on (hysteresis 1.5 bps so the 1.0 bp band sits inside the withdrawal tolerance). The values are ASSUMED and labelled ILLUSTRATIVE_NOT_FITTED until the trader's or stage-0-measured values are approved. The backtester's own `new_backtest/requote.py` still serves the older backtester path (same rule, same target function); it is NOT yet a caller of the session controller.

## Depth quality: gap depth and flicker (owner 14 Nov; OFF by default)
`VenueStateConfig.depth_quality` turns on two APPROXIMATE per-venue, per-side fields (L2 at the feed's update rate): `depth_to_first_gap` (size before the first near-empty stretch longer than `dq_gap_stretch_bps`, and where it starts) and `flicker` (unexplained depth decrease of the best `dq_top_levels` over `dq_flicker_window_s`, divided by the volume printed against that side; the split is the depletion ledger's two-way netting rule). A gap seen with a sequence gap is FEED_FAULT, not a cliff; the end of the captured depth is NO_GAP_WITHIN_CAPTURE. All thresholds are ASSUMED. `choose_tactic` reads them only with `TacticChoiceConfig.gap_aware` (default False) and never for the arb hedge, which does not call it. The intent journal carries them with every intent and child when an evidence provider is set. On the 2026-10-29 capture neither field predicted hedge slippage change or maker markout (out/integration/dq/factor_tables.md): not kept.

Step B (owner 14 Nov) extends the same tracker: a tick is thin when absent or below `dq_thin_ratio` x the rolling median level size at that distance, a gap is `dq_gap_min_ticks` or more consecutive thin ticks or a jump above `dq_gap_stretch_bps`; per side it reports the size before the first gap, its distance, the gap count within `dq_gap_within_bps`, the persistence class, and per venue the gap asymmetry; survival (min of size now and at the next update, over size now, any decrease counted), cancel-to-trade (= flicker) and the flicker spike (z-score against a rolling baseline). A gap or spike with a sequence gap or an unhealthy feed is FEED_FAULT. APPROXIMATE, off by default, no rule reads survival or the spike yet.

## Execution cost model, follow-ups 1-5 (15 Nov 2026; report only, nothing committed; full text in out/integration/cost/G_RESULTS.md, rules FOLLOWUP_RULES.md)
Large sizes: T1 out to 400% of D10 (Bybit March 12 days, capture both venues; HOLDOUT not read again): displayed depth stays the better predictor at every size, effective depth worse (+0.003 to +0.29 bps MAE, more on 100 ms feeds); cost curve delta 0.69 over f 0.25 to 4 (0.53 for small sizes). Repeated hedging: with own consumption (per-level refill tau 3.5 s, an assumption) a 1 s spaced sequence of full-depth hedges pays 10.0 against 6.3 bps per unit, under 0.1 bps extra at 10 s spacing; thinner books and the capture are worse. T5 at 100 / 500 SOL on the capture with real Binance depth: time-slicing never pays (saves 0.005 to 0.05 bps, costs 0.3 to 0.75 bps in unhedged time at risk weights 0.5 to 1); E saves 0.17 to 0.22 bps of cost and loses at weight 1. T1 CAVEAT (owner 15 Nov): with ~100 ms books the outcome 0.1 s later is often the same or the next snapshot (97% on the Binance capture, ADA and MNT; on Bybit March it is read after about 64 updates), so the small error is partly BY CONSTRUCTION; T1 does not test other participants' reaction to our order, nor book changes below 100 ms.
CORRECTION: the G T5 fill rule counted prints at any price; fills are now price-aware (`--price-aware`), the G T5 gains are an upper bound and E's advantage at risk weight 1 disappears (fill rate of the rested part 55 to 65%, not 97%). At the probe's maker-fill moments the all-market hedge costs 2.30 bps against 1.76 at random moments; E is no better than all-market at weight 1. ADA / MNT (Bybit perp April, from ~/Downloads/bybit_data; no Binance depth exists): effective depth worse than displayed, the cliff flag is meaningless on ADA (tick 4.1 bps exceeds the 3 bps stretch limit; a rule defect, not fixed), error 2 to 4 times larger in volatile blocks on every coin. On the 100 ms feeds 97% of the 0.1 s outcomes are the next snapshot: the small T1 error is partly by construction. Perp queue validation plan: out/integration/cost/PERP_QUEUE_VALIDATION_PLAN.md (owner approval needed).

## Profile line (owner 16 Nov 2026)

* `perp_perp_maker_taker_v1_9` is **FROZEN** as the v1_9 reference (it was edited in place in five commits). Pinned by sha256 `f798ac62497da2f26b93c845b383ea5f47dd6d7409e6122c3f733d7b857be8cb` in `profiles/perp_perp_maker_taker_v1_9.freeze.json` (with v1_9s, unchanged), tested in `tests/test_audit_decision_a.py::FrozenV1_9`. A change is a new profile version, never an edit. `perp_perp_maker_taker_v1_8` stays as it is.
* `perp_perp_maker_taker_v1_10` = v1_9 + quote withdrawal ON (`arb.quote_withdraw`, hysteresis 1.5 bps ASSUMED, wider than the S0 band) + S0 repricing ON (`execution.primary.reprice`: band 1.0 bp, min interval 1000 ms, max age 15 s, amend first, queue hold; `source ILLUSTRATIVE_NOT_FITTED`) + hedge dust carry (`hedge_faults.dust_carry next_hedge`, alarm after 60 s ASSUMED) + the 1 s no-requote after a validity withdrawal (`arb.requote_delay_after_withdraw_s 1.0` ASSUMED) + the 5 s mark rule by the mark's own timestamp (`margin.mark_stamp own_timestamp`) + seeds as profile keys (`seed 7`, `hedge_faults.seed 11`, the current defaults) + the real-order amend guard flags in `decision_status`. A test lists exactly these keys. The profile named in the real-orders table and the testnet drill is v1_10. The new limits (Order 3), feed health (v1_11) and pair close (a separate switch, not in v1_10) come after.

**Hedge dust carry (owner 16 Nov; `hedge_faults.dust_carry next_hedge`, off by default).** A hedge chunk below the hedge venue's minimum quantity or notional is not sent and not unwound. It is carried in ONE signed balance per (hedge venue, instrument), shared by every plan (`HEDGE_DUST_CARRIED` once per carried chunk, with venue, signed exposure, price and plan). The next hedge on that venue is its own quantity plus the signed balance (an opposite balance reduces it; the lot rounding never loosens; what cannot be sent stays carried); the carried part taken in is journaled on the hedge intent (`dust_included`) and in `HEDGE_DUST_INCLUDED`. As soon as the balance alone reaches the minimum it is sent at once as an ordinary hedge chain (size-aware IOC cap, retries, unwind and alarm path), on the plan whose fill lifted it, or on a small dust-host plan when a price move did. The balance can never exceed one minimum order. The sub-lot remainder of a plan joins the same balance (no per-plan `HEDGE_LOT_RESIDUAL`, no persistent lot-residual alarm with the switch on). `HEDGE_DUST_ALARM` fires once when dust has been carried longer than `dust_alarm_after_s` (ASSUMED 60 s; it carries the `alert_context` fields, blocks no new risk, does not trip the breaker, and clears at zero balance). Carried dust is in the residual totals once (as `carried_dust_base`, in `total_residual_base`), not in any plan: the residual bound, the 5 s hold, the 30 s kill stage and the breaker's net-exposure trigger ignore it; anything not carried still trips. `RESIDUAL_LIMIT_BREACH` is recorded once per breach with the switch on. A restart keeps the balance (plan-store events `DUST_ADJUST`). Whether a reduce-only order is exempt from the minimum notional is UNVERIFIED and goes into the testnet drill; the dust hedge is not reduce-only and does not rely on it.

**Real-order amend guard (owner 16 Nov).** With real orders (testnet, mainnet, paper with sends) a profile that reprices by amend sends no amend until `decision_status` records BOTH `case2i_passed` and `testnet_amend_drill_passed` as true; until then every reprice is a cancel and a fresh quote through all the gates (counted `amend_guard_cancel_instead`, labelled `REAL_ORDER_AMEND_GUARD`). Withdrawal is not restricted. Replays are not restricted.

**5 s mark rule (owner 16 Nov, v1_10 onward).** `margin.mark_stamp own_timestamp`: a mark older than 5 s by its own timestamp pauses new risk; nothing is re-stamped. Boundary: 4.9 s and 5.0 s count, 5.1 s is missing. Every earlier profile keeps `restamp` (a mark lives 9-10 s), labelled LEGACY in the manifest (`config_notes.mark_stamp`). Every session manifest records the effective seeds (`config_notes.effective_seeds`).

**Testnet drill (owner 16 Nov): the profile is `perp_perp_maker_taker_v1_10`.** Items the replays cannot settle, in this order: (1) whether a Binance reduce-only order is exempt from the minimum notional (Order 1 rule 8, UNVERIFIED: the dust path does not rely on it); (2) the amend drill on the testnet (place, amend, fill during AMEND_PENDING, reject, timeout, disconnect, duplicate Filled), after which `decision_status.testnet_amend_drill_passed` may be set to true; (3) `case2i_passed` is set only after the case 2i replays on v1_10 show every amend path acting on real amends, not on emptiness (see the coverage matrix row 2i v1_10). Until both flags are true every reprice is a cancel plus a fresh quote, counted and labelled `amend_guard_cancel_instead`.

**Session limits (owner 16 Nov, Order 3; v1_10 onward; `oms.session_limits`, off in every earlier profile, byte-identical when absent).** Each limit has a profile key, a counter, one `LIMIT_STATE` event per state change (ACTIVE / CLEARED), a unit test and an injected replay case. They sit in `session.py` (A1-A5), `rate_limits.py` (A1 bucket), `time_integrity.py` (A3 source) and `testnet.py`, `sdk.py`, `paper_live.py` (A6, A8).
* **A1 order rate** (`order_rate: {per_s 2.0, burst 5}`): new maker orders and amends per venue, a token bucket on the event clock; skipped and counted, never queued. Hedges, unwinds, flattens and cancels never pass through it. It stacks on the window limiter (`oms.rate_limits`, which can queue); both apply. Under the amend guard the fresh quote of a cancel-plus-requote also takes a token.
* **A2 daily loss stop** (`daily_loss_stop_usdt_per_sol_of_clip: -5.0`): cash plus positions at the book mids (the replay's `equity_at_book_mids`; funding is not included) against the value at 00:00 UTC, or at the session start for the first evaluation (every 1 s of event time). At or below the limit (the key times the run's clip in SOL: -5 at 1 SOL, -25 at 5 SOL clip; the labelled "5.0 SOL gross per venue" run has a 1 SOL clip) it trips a latched kill switch with `keep_risk_reducing`: quotes cancelled, hedges and unwinds in flight finish, no new risk. Cleared only by a logged manual reset (`oms.recover_kill_switch`), which re-bases the daily baseline. A book that is not VALID gives no equity, so the check waits.
* **A3 clock-offset gate** (`clock_offset: {max_uncertainty_ms 5.0, max_age_s 900}`): no new quotes while the clock offset is unknown, older than `max_age_s` (ASSUMED; the probe repeats every 300 s) or its uncertainty is above 5 ms; hedges continue. The source is the venues' server time, read the way ollog does it (five probes, the lowest round trip wins, offset = server minus the local midpoint, uncertainty = half the round trip; the worse venue counts). `HostClockMonitor` is NOT a source (audit A1: its reference is the estimate itself). A session that can send real orders refuses to start without a clock source; a replay with no measured clock reports "not evaluable" (counted `clock_not_evaluable`) and is never blocked.
* **A4 margin WARNING stop** (`margin_warning: {free_margin_multiple 10.0, resume_hysteresis_s 5.0}`): a real margin WARNING (a buffer below its requirement; a WARNING that only means a missing price is the pause's business and A5's), or free margin below 10x the initial margin at the hard cap (gross per venue times the mark through the venue's tiers), stops new quotes; resumes after 5 s clear (ASSUMED).
* **A5 missing marks** (`missing_marks_kill_s: 60.0`): a margin price pause longer than 60 s trips a latched kill switch (same action as A2). This amends "the price pause never trips the kill switch" for v1_10 onward; earlier profiles are unchanged.
* **A6 tier table** (`tier_table: read_at_start`): real-order modes read the venue's risk tiers at start-up (Binance `leverageBracket`, signed; Bybit `risk-limit`, the symbol's tier ladder) and replace the ASSUMED margin tiers (`source ACCOUNT_READ`); the session refuses to start if they cannot be read. The endpoint shapes are from the public documentation and UNVERIFIED against the live endpoints (tested on hand-written recorded messages; the testnet drill confirms them). The Bybit endpoint is the symbol's ladder, not the account's selected risk limit: reading that is a drill item. Replays keep the ASSUMED tiers, labelled.
* **A7** is `arb.requote_delay_after_withdraw_s: 1.0` (Order 2).
* **A8 account check** (`verify_account: {enabled, margin_mode cross, leverage 10}`): at paper/live start-up `testnet.verify_accounts_for_start` requires one-way mode, the margin mode and the leverage of the symbol (Binance `positionRisk`, Bybit position list), and reduce-only acceptance, which is DERIVED from one-way mode and labelled UNVERIFIED (the check sends no order; the testnet drill sends one). Leverage 10 is ASSUMED (the instrument rule used in the replays). Refuses to start on any mismatch or failed read; a purely simulated session with no account does not run it and says so.
* **A9** is a measurement (`scripts/stress_feed.py`), not a switch. See the report.
All of these are REAL_ORDER_REQUIRED rows; v1_10 meets every row, v1_9 and v1_9s meet none of the new ones and are refused for real orders.

