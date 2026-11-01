# Strategy contract: cross-venue perpetual arb (maker on one perp, taker on the other)

Status: canonical. Written 2026-11-01 to resolve the conflict between two reports (one said the hedge is sent alongside the
maker order, one said it is sent when the maker fill is confirmed). This document and `tests/test_perp_arb_legs.py` are the
reference; a report that disagrees with them is wrong.

## 1. Product

Both legs are USDT linear perpetuals: Bybit `linear` and Binance USDⓈ-M. The Jan-2022 replay data is that product on both
venues (Binance records carry integer contract quantities and 0.001 ticks, which is the futures contract, not spot; Bybit
prices are on sub-0.01 ticks, not its spot tick). Fees are the perpetual presets on both legs (`bybit_perp_*`,
`binance_perp_*`); a spot preset on either leg is a configuration error.

## 2. Leg sequencing

| mode | legs sent on the signal | hedge | exposure created |
|---|---|---|---|
| `taker_taker` | both taker legs, same instant, independent latency draws | none (both legs are the trade) | leg risk: one side misses |
| `maker_taker` | the passive leg only | a taker on the other venue, sent **when the passive fill is confirmed** (`_on_fill`) | legging risk: price moves on the hedge venue between the fill and the hedge |

`maker_taker` is **Method A: fill-triggered hedge**. Nothing is ever sent to the hedge venue while the maker order is unfilled.
Method B (hedge first, maker may never fill) is not a mode of this system; it is a different strategy with the opposite failure
(a naked position when the maker order does not fill) and is not to be built under the `maker_taker` name.

Consequences that follow from A and are modelled explicitly:

* The hedge pays the taker fee and whatever the hedge venue's book has moved to by the time the order lands (`hedge_leg`,
  entry latency, `max_walk_bps` limit). A hedge that cannot be built or misses is a hedge failure, handled by
  `hedge_faults.py` (retry / unwind / carry) and reported as such, never silently as P&L.
* A partial maker fill is hedged for the filled quantity; the remainder keeps resting until filled, cancelled by
  `maker_max_age_s`, or the run ends.
* Inventory limits count the resting maker quantity as potential exposure when `count_pending_exposure` is on.

## 3. Passive direction

The passive leg may be a bid (buy on the maker venue, hedge = sell on the taker venue) or, with
`ArbConfig.maker_passive_side="both"`, an ask (sell on the maker venue, hedge = buy on the taker venue). Default is `"bid"`,
which is the behaviour of every result produced before 2026-11-01. On a perpetual both directions are equally available; the
`"both"` setting is opt-in so that earlier numbers stay reproducible.

## 4. Fill model and its calibration

* The fill engine (queue position, proportional cancel model, fill-on-vanish and the opt-in evidence rules) was **validated on
  Bybit spot real orders** (the April–May 2026 export and the fill-evidence study). Its rules are product-agnostic but that is
  where the evidence is.
* `FillModelV2` (the fill-probability gate) is **fitted inside each walk-forward fold on the engine's own simulated fills** over
  the Jan-2022 perpetual replay. It is therefore a perp-data model, but of simulated fills, not of real ones.
* `min_queue_ahead=25` came from present-level depths in that same Jan-2022 perp replay.
* Real perpetual orders are produced by `ollog fillprobe` (testnet now, mainnet when launched) and converted to the calibration
  format by `ollog calibrate`; `scripts/fit_fill_model_from_probes.py` fits `FillModelV2` on them. A model fitted on testnet
  orders is a plumbing check (testnet queues are not mainnet queues); the gate should use a mainnet-only fit.

## 5. What is modelled and what is opt-in

| concern | state | switch |
|---|---|---|
| funding | settled from rate history; mark = book mid when the history has no mark price (all Jan-2022 rows) | `funding_rate_dir` |
| margin, liquidation | opt-in model (tiers, cross margin, liquidation at market with fee, flatten the other venue) | `ArbBacktestConfig.margin` |
| cross-venue equity bridge | opt-in: routes with delay/fee/minimum, watermark rebalancing; signals are refused while a venue cannot post initial margin | `margin.transfers` |
| hedge failures | opt-in injection | `hedge_faults` |
| PostOnly rejection | opt-in exchange-time check | `postonly_reject_model` |

## 6. Live engine conformance

`trading_platform_poc/opportunity.py` dispatches all legs of an opportunity concurrently (its `POST_ONLY` is a reject reason,
not a leg type). That is the `taker_taker` contract. It does **not** implement Method A: a maker/taker opportunity sent through
it today would hedge immediately (Method B). Until the live engine has a "rest the passive leg, hedge on its fill" path with the
same failure handling as the backtester, `maker_taker` is a backtest-only mode and must not be enabled live.
