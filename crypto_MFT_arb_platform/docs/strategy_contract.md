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
* Unwind: price-banded (`unwind_band_bps`, 0 = market; profile 15 bps) with `unwind_max_attempts` total sends (profile 3, independent of
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
| `MARKET_BANDED` | breaker flatten and unwinds only | the only tactic that may carry no `worst_price` |

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
