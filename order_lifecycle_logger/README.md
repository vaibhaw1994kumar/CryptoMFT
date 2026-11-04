# ollog: testnet order-lifecycle latency logger

Testnet only. Hosts are hard-coded (`ollog/endpoints.py`) and every REST call and WebSocket connect is checked against them.

Per market (bybit_spot, bybit_linear, binance_spot, binance_futures) it places a minimum-size post-only BUY below the best bid,
waits for the private-stream "New", holds, cancels, waits for the private-stream "Cancelled", and records wall-clock and
monotonic stamps for: client send, REST return (+ exchange time), stream New receipt (+ exchange event/transaction time),
cancel send, cancel REST return, stream Cancelled receipt (+ exchange time). A server-time probe (best of 5, every 5 min)
gives the clock offset and its uncertainty; exchange-referenced figures carry that uncertainty.

    python3.12 -m ollog discover --check-ws      # public data only, no credentials
    python3.12 -m ollog run --markets bybit_spot --coins 5 --max-orders 8
    python3.12 -m pytest tests -q                # offline, no network

Output: `latency_logs/<market>_<utc>/{meta.json, events.jsonl, orders.jsonl, summary.json}`. events/orders keep order ids and
are for local use; summary.json has no ids. Everything is labelled `testnet-measured`: it is not mainnet, not a calibration
input for the realistic preset, and it measures no fill-versus-cancel races (buy-only, one live order, below the touch).

## Bybit buy-price limit (170193)

Testnet books can be stale, so a buy below the bid can still be above the exchange's own limit ("Buy order price cannot be
higher than X"). The first such rejection is recorded as `rejected_price_limit`, the cap is remembered for that symbol, and
later orders are priced at or under it (record field `price_cap_applied`). That order spends one slot of the order cap, is not
counted as an error, and is left out of the latency figures. A rejection that does not report a lower cap counts as a normal
error. Orders under a learned cap sit further below the touch than `--dist-bps`; that does not matter for lifecycle latency.

The venue's reference price moves, so a learned cap goes stale (DOGE drifted about 0.2% over 60 orders and was refused 9 times).
Orders are therefore priced `--cap-margin-bps` (default 100) under the cap the venue reported.

## Reject probes (`--reject-probe-every N`)

Every Nth order is a post-only buy at the best ask, which would take liquidity and must be refused. The probe symbol rotates
on its own counter (not the normal-order cycle), and only symbols that already completed a clean lifecycle in this run, with a
bid and an ask on the book (and the ask under any learned buy-price limit), are probed. A probe counts as a post-only reject
only when the venue says so (Bybit `EC_PostOnlyWillTakeLiquidity`, Binance spot "would immediately match", Binance futures
`-5022` or a GTX `EXPIRED`). Any other refusal (price limit, minimum order value, permissions) is recorded as
`probe_rejected_other`, is not an error, and removes that symbol from probing; after 3 of them probing stops for the market.
`summary.json` reports probes apart (`reject_probes`) and keeps them out of the lifecycle latency figures.

## Binance spot order events

The REST listenKey endpoints (`/api/v3/userDataStream`) answer HTTP 410 on the spot testnet. Spot now subscribes on the WebSocket
API (`wss://ws-api.testnet.binance.vision/ws-api/v3`, `userDataStream.subscribe.signature`, HMAC key, signature checked against the
documented example) and reads events wrapped as `{subscriptionId, event}`. A spot CANCELED event carries a fresh id in `c` and the
original client order id in `C`; the parser matches on `C`. Futures still uses the listenKey stream.

## Fill probe: capture + at-touch orders + replay (perpetual markets)

`python3.12 -m ollog fillprobe --coins-list ETHUSDT,XRPUSDT --max-orders 120` runs, in one process, (1) a public order-book and trade
capture for Bybit linear and Binance USD-M futures written in the backtester's own layout (`capture/<venue>/book_snapshots|book_events|trades/
pair=<PAIR>/date=<day>/*.jsonl.gz`, absolute level sizes, plus `_raw/` frames), and (2) minimum-size post-only orders that JOIN the best bid or
ask (alternating sides per symbol, net-position cap, reduce-only flatten of the traded symbols at the end), each held 15-45 s (configurable)
or until filled, then cancelled. A fill is data (quantity, exchange time, price); a cancel refused because the order filled is recorded.

Sequencing: Bybit `u` continuity (a gap rebuilds from a fresh snapshot); Binance REST snapshot + `U`/`u`/`pu` sync. Every 30 s the
reconstructed top 20 is compared with a REST depth snapshot aligned on the venue's update id (Bybit `seq`, Binance `lastUpdateId`); when the
snapshot falls inside a batch, a level matches if it equals the size before OR after the batch, and levels the batch touched are ambiguous
(counted separately), so `levels_unexplained` is a real reconstruction error count. Results: `recon_checks.jsonl`.

Replay: `python3.12 -m ollog replay --run fillprobe_runs/<run> --src <dir containing new_backtest>` injects every real order at its exchange
created time (zero entry latency), cancels it at the real end (full fill or the exchange's cancel event), and scores four configs
(`off`, `vanish_clears_queue`, `confirm_delta_20ms`, both) with the fill-evidence study's definitions: hit / premature / late / unfilled
within +-1 s of the real first fill, and `false_fill` for orders that really did not fill. Use a snapshot copy of `src/` for `--src` when
other work edits the live tree. Testnet results are a plumbing and event-ordering check, not fill-model evidence: testnet queues are not
mainnet queues.

Mainnet (real orders, real money): `--network mainnet` is gated twice. `--capture-only` needs no credentials and sends no orders. Orders
need `--confirm-mainnet I-ACCEPT-REAL-ORDERS`, `OLLOG_MAINNET=1`, mainnet credential variables (BYBIT_MAINNET_KEY/SECRET,
BINANCE_FUT_MAINNET_KEY/SECRET; never the testnet names), the perpetual markets only, symbols BTCUSDT/SOLUSDT only, at most 200 orders and
150 quote per order and per net position (see `ollog/mainnet.py`). Mainnet uses its own host allow-list; the testnet guard is unchanged and
refuses mainnet hosts. Binance USD-M mainnet serves depth on `/public/` and aggTrade on `/market/`, so the capture opens two connections.
