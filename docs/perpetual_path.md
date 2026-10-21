# Perpetual futures path (Binance USDⓈ-M, Bybit linear)

This is the path for the "simple cross-venue perpetual futures strategy" in
*Platform Requirements v1 (15 Oct 2026)*: the same session, OMS, risk and
ledger as spot, with perpetual market data, futures testnet execution and
realised funding.

## Config profile: `config/perp_research.json`

* **Instruments:** `BTC-USDT-PERP` on BINANCE and BYBIT (`BTCUSDT`):
  - product `PERP`, linear, multiplier 1, settled in USDT;
  - 8h funding interval, funding rate bounds ±3%;
  - minimum notional 100 (Binance) / 5 (Bybit).
* **Fees:** dated rules keyed to `BTC-USDT-PERP`: Binance 2 / 5 bps,
  Bybit 2 / 5.5 bps. **ASSUMED**: verify the account tiers.
* **Margin:**
  - `risk.margin`: cross mode, USDT collateral;
  - risk tiers and the 1.25% liquidation fee are **ASSUMED placeholders**:
    replace them with the account's leverage brackets / risk limits.
* **Inventory:** unconstrained. Pre-funded spot inventory doesn't apply to
  perpetuals.
* **Funding:**
  - `mark_fallback: exchange_mark`: settlements without a record mark use
    the venue's mark from the derivatives stream, observed at most
    `max_mark_age_ms` before the settlement;
  - `rates` / `rates_path` come from the funding-history command below.

## Market data

`live-paper` / `live-orderbook` with `--binance-market linear --bybit-market linear`.
A perpetual config on spot books is refused at start-up, and the
derivatives stream is switched on automatically.

| Venue | Channel | Fields used |
|---|---|---|
| Binance | `wss://fstream.binance.com/ws/<symbol>@markPrice@1s` | `p` mark, `i` index, `r` current funding rate, `T` next funding time, `E` event time |
| Bybit | `tickers.<symbol>` on `wss://stream.bybit.com/v5/public/linear` | `markPrice`, `indexPrice`, `fundingRate`, `nextFundingTime`, `fundingIntervalHour` |

* **Bybit deltas.** Bybit sends a snapshot and then deltas in which an
  omitted field is *unchanged*. `BybitTickerMerger` keeps the merged state
  and is reset on reconnect, so a delta before a snapshot is rejected.
* **Recording.** Each ticker becomes a canonical `DerivativeTicker` (schema,
  adapter version, raw reference, arrival ordinal, quality flags). It is
  recorded to `part-*/derivatives.jsonl` (only in parts that have one), with
  a `DERIVATIVE` trace row. Spot sessions are byte-identical.
* **Where the values go.** The streamed funding rate is a *prediction*:
  `FundingEngine.predict` only, never realised settlement. The streamed mark
  is the settlement mark when the rate record has none.
* **Replay.** `backtest --derivatives <derivatives.jsonl>` replays recorded
  tickers in receive order, each before the first market event at or after
  it.

## Realised funding history

```bash
PYTHONPATH=src python3 -m crypto_stat_arb.cli funding-history --venue BINANCE --symbol BTCUSDT \
  --instrument-id BTC-USDT-PERP --start 2026-10-01T00:00:00Z --end 2026-10-21T00:00:00Z \
  --output data/funding/binance_btcusdt.jsonl
```

* **Binance** `GET /fapi/v1/fundingRate`: ascending, up to 1000 rows,
  includes `markPrice`.
* **Bybit** `GET /v5/market/funding/history`: up to 200 rows and **no mark
  price**. The mark is the open of the 1-minute candle from
  `GET /v5/market/mark-price-kline` at the settlement (`mark_source`).
* Records are `settlement_history: true` and `source: OBSERVED`, and pass
  the funding engine's validation. Only the public hosts `fapi.binance.com`
  and `api.bybit.com` are accepted.
* **Processing-time offsets.** Binance stamps `fundingTime` with when the
  settlement was processed, 0–7 ms after the 8h boundary in the October
  2026 data. The engine assigns a record within `funding.schedule_tolerance_ms`
  (default 1000) to that scheduled settlement, and keeps the venue time in
  the event (`venue_settlement_ts_ns`). Without this, every Binance
  settlement would show as a missing record plus an off-schedule one.
* **Downloaded data (2026-09-22 → 2026-10-22):**
  `data/funding/binance_btcusdt_20260922_20261022.jsonl` and
  `data/funding/bybit_btcusdt_20260922_20261022.jsonl`.
  - 90 settlements per venue; no gaps, none rejected, every record with a
    mark.
  - Venue marks agree within 0.6 bps (median).
  - Mean rate 0.0032% (Binance) / 0.0034% (Bybit) per 8h.
  - Over the month, a 0.01 BTC long paid 2.41 USDT on Binance and a 0.01 BTC
    short received 2.56 USDT on Bybit (net +0.15 USDT for the hedged pair).
  - To use them, set `funding.rates_path` to one file per run, or merge
    them.

## Futures testnet execution (`testnet.py`)

The transport is chosen per venue from the instruments (`markets_for(cfg)`):
a PERP instrument gets the futures transport.

| | Binance USDⓈ-M futures testnet | Bybit v5 testnet, `category=linear` |
|---|---|---|
| Host | `testnet.binancefuture.com` (`/fapi/v1/order`) | `api-testnet.bybit.com` |
| Post-only / reduce-only | `timeInForce=GTX` / `reduceOnly=true` | `PostOnly` / `reduceOnly`, `positionIdx=0` |
| Maker/taker and venue fee per fill | `GET /fapi/v1/userTrades` (`maker`, `commission`, `commissionAsset`) | `GET /v5/execution/list` (`isMaker`, `execFee`, `feeCurrency`) |
| Account check before any order | `GET /fapi/v1/positionSide/dual`: must be one-way | `GET /v5/account/info` margin mode must match `risk.margin`; `/v5/position/list` must be one-way |

* **Account settings are read, never changed.** A mismatch stops the
  adapter from being built.
* **Binance per-symbol margin type** (cross/isolated) is *not* verified
  yet. The endpoint's response fields weren't confirmed in the current
  docs, so the result says `NOT_VERIFIED`.
* **Fill labels.** An order fill is labelled MAKER only if every fill was
  maker, otherwise TAKER. The venue commission is kept in the callback's
  native payload for audit.

## What this does not prove yet

* **No lifecycle evidence yet.** The futures transports are tested against
  recorded/fake responses only. Until they have run against the real
  testnets with your testnet keys, they are not lifecycle evidence (the
  requirements make the same point about spot adapters).
* **No recorded perp data.** Recording means running a public-feed capture
  with linear markets for a period you choose.
* **Assumed parameters.** Fee tiers, risk tiers, liquidation fee and
  collateral rules in `perp_research.json` are assumptions until checked
  against the account.
