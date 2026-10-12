# Full Feature Demonstration Guide

A complete, runnable walkthrough of every capability in `hft-platform-kit`:
market data capture, deterministic replay, backtest export, historical import,
live paper trading with observability, the native C++ EMS (smart order routing
and TWAP scheduling), guarded exchange execution, and the embeddable Python SDK.

Every command and code block below was executed against this build. Sample
outputs are real, abbreviated only where noted with `...`.

**Contents**

1. [Setup](#1-setup)
2. [Health and integration inventory](#2-health-and-integration-inventory)
3. [Record live market data](#3-record-live-market-data)
4. [Inspect datasets and provenance](#4-inspect-datasets-and-provenance)
5. [Deterministic replay](#5-deterministic-replay)
6. [Export to HftBacktest](#6-export-to-hftbacktest)
7. [Historical import via Tardis](#7-historical-import-via-tardis)
8. [Live paper trading with observability](#8-live-paper-trading-with-observability)
9. [Reports and artifacts](#9-reports-and-artifacts)
10. [Multi-exchange EMS configuration](#10-multi-exchange-ems-configuration)
11. [Native C++ SOR and TWAP](#11-native-c-sor-and-twap)
12. [Guarded Binance Spot Testnet execution](#12-guarded-binance-spot-testnet-execution)
13. [Embed the Python SDK](#13-embed-the-python-sdk)
14. [Plug in a custom strategy](#14-plug-in-a-custom-strategy)
15. [Safety and resilience behaviours](#15-safety-and-resilience-behaviours)
16. [Suggested demo agendas](#suggested-demo-agendas)

> For the minimum-requirements stat-arb platform (`mr-*` commands: multi-venue
> capture, data-quality gating, deterministic backtest, TCA, reconciliation and
> the AT-01…AT-14 acceptance suite), see the dedicated
> [MR_DEMO_GUIDE.md](MR_DEMO_GUIDE.md).

---

## 1. Setup

### Install the published package

```bash
pip install hft-platform-kit
```

Two equivalent console scripts are installed: `hft-platform-kit` and
`trading-platform-poc`.

| Platform | Python | Notes |
| --- | --- | --- |
| Linux x86_64 / aarch64 | 3.12, 3.13 | manylinux (glibc) wheels |
| macOS arm64 | 3.12, 3.13 | Apple Silicon only — Intel Macs are unsupported |
| Windows x64 | 3.12, 3.13 | `win_amd64`; no VC++ redistributable needed |

No compiler is required — the native EMS extension ships prebuilt.

### Or run from a source checkout

```bash
git clone https://github.com/ankurarrah/hft-platform-kit.git
cd hft-platform-kit
uv sync
uv run hft-platform-kit --help
```

### Create a demo workspace

Datasets and reports are written relative to the working directory, so run the
demo from a dedicated folder:

```bash
mkdir -p ~/hft-demo && cd ~/hft-demo
```

### Confirm the full command surface

```bash
hft-platform-kit --help
```

```text
{record,replay,tardis-import,hft-export,hft-health,libraries,datasets,
 paper,ems-inspect,ems-demo,opportunity-demo,binance-testnet}
```

**Talking point.** One binary covers the whole lifecycle: capture, replay,
export, live paper trading, and guarded venue execution.

---

## 2. Health and integration inventory

Prove the native and third-party stack is wired up before demonstrating anything
else.

```bash
hft-platform-kit hft-health
```

```json
{"asset_type": "BacktestAsset",
 "event_dtype": ["ev","exch_ts","local_ts","px","qty","order_id","ival","fval"],
 "version": "2.4.4"}
```

This round-trips a real `BacktestAsset` through the installed HftBacktest
build — it is a live check, not a version string lookup.

```bash
hft-platform-kit libraries
```

```text
 - tardis-dev          | tardis_dev         | active=True
 - hftbacktest         | hftbacktest        | active=True
 - pyarrow             | pyarrow            | active=True
 - FastAPI             | fastapi            | active=True
 - Prometheus client   | prometheus_client  | active=True
 - uvloop              | uvloop             | active=True
 - level2-order-book   | level2_order_book  | active=False
 - OrderBook-rs        | orderbook_rs       | active=False
```

**Talking point.** Each entry reports its real import status and the role it
plays. The two inactive rows are evaluated alternatives that were deliberately
not adopted — the inventory documents that decision rather than hiding it.

On Windows the loop row reports `winloop` instead of `uvloop`; the platform
selects the accelerated loop automatically and falls back to the standard
library loop if neither is importable.

---

## 3. Record live market data

Capture a live Binance depth and trade stream. No credentials are needed —
these are public market data streams.

```bash
hft-platform-kit record --duration-seconds 12
```

```json
{"run_id": "09eb7002-...", "event_count": 358,
 "output_directory": "data/09eb7002-1844-4de0-99a8-9662b7a4f0ce",
 "event": "record_mode_completed"}
```

Stop on an event budget instead of a clock:

```bash
hft-platform-kit record --max-events 5000
```

**Talking point.** Every event carries both an exchange timestamp and a local
receive timestamp. That dual clock is what makes latency analysis and
deterministic replay possible later.

### Watch the live L2 order book in the terminal

Recording writes to disk; `book` renders the maintained level-2 book live.
It also needs no credentials. The display runs for **five minutes by
default** and redraws in place:

```bash
hft-platform-kit book --symbol BTCUSDT --depth 10 2>/dev/null
```

```
BTCUSDT  level-2 order book
elapsed    8.0s   remaining  292.0s   events 78
--------------------------------------------------------------
         bid qty           bid | ask           ask qty
         3.35643     62,957.51 | 62,957.52     3.24706
          0.1467     62,957.50 | 62,957.53     0.12794
--------------------------------------------------------------
mid 62,957.515   spread 0.01 (0.0016 bps)
levels 1,012 x 1,006   feed HEALTHY   book fresh
```

Structured logs go to stderr by design, so `2>/dev/null` leaves a clean
table. Use `--duration-seconds` for a shorter run, `--refresh-ms` to change
the redraw rate, and `--json` to emit one machine-readable state object per
refresh instead of a table.

**Talking point.** The book is built by connecting the websocket **first**,
then fetching the REST snapshot, then discarding buffered updates the
snapshot already covers. Doing it the intuitive way round — snapshot first —
silently drops every update that arrives during the REST call, and the book
never recovers. On a sequence gap this resynchronises rather than drifting,
and `feed HEALTHY` / `book fresh` come from the same data-quality state
machine that blocks quoting on a stale feed in §16.

---

## 4. Inspect datasets and provenance

```bash
hft-platform-kit datasets
```

```text
  09eb7002  BTCUSDT  358 events
  91334fc9  BTCUSDT  200 events
```

Look at what a capture actually produced:

```bash
ls data/09eb7002-1844-4de0-99a8-9662b7a4f0ce/
```

```text
dataset_manifest.json   events.jsonl   events.parquet
quarantine.jsonl        raw.jsonl
```

| File | Purpose |
| --- | --- |
| `raw.jsonl` | Untouched exchange payloads — the audit trail |
| `events.jsonl` | Normalized, schema-versioned events |
| `events.parquet` | Columnar form for analytics |
| `quarantine.jsonl` | Events rejected by data-quality validation |
| `dataset_manifest.json` | Counts, schema version, config hash, SHA-256 checksums |

```bash
python -c "import json;print(list(json.load(open('data/09eb7002-.../dataset_manifest.json')).keys()))"
```

```text
['checksums', 'config_hash', 'dataset_id', 'event_count', 'event_counts',
 'files', 'quarantine_count', 'quarantine_counts', 'schema_version', 'symbol']
```

**Talking point.** The manifest hashes every file *and* the configuration that
produced it. A dataset is reproducible and tamper-evident. JSONL is written with
explicit LF endings so a capture on Windows hashes identically to one on Linux.

---

## 5. Deterministic replay

Replay the capture through the same engine that runs live:

```bash
hft-platform-kit replay --data data/09eb7002-1844-4de0-99a8-9662b7a4f0ce
```

The completion record includes `book_checksum` and `result_hash`.

### Prove determinism

Run it twice and compare:

```bash
for i in 1 2; do
  hft-platform-kit replay --data data/09eb7002-... 2>&1 | tail -1 \
    | python -c "import json,sys;d=json.load(sys.stdin);print(d['result_hash'])"
done
```

```text
e55325c3cc80f3ee8905ba04b7c95fe9551e1f04e1dd05c0bc5c3c4ac7110f44
e55325c3cc80f3ee8905ba04b7c95fe9551e1f04e1dd05c0bc5c3c4ac7110f44
```

Identical. Now change the pacing and show the result is *still* identical:

```bash
hft-platform-kit replay --data data/09eb7002-... --speed max
hft-platform-kit replay --data data/09eb7002-... --speed 10x
```

```text
speed=max    events 358  hash 78c799557bf8
speed=10x    events 358  hash 78c799557bf8
```

`--speed` accepts `max`, `real-time`, or a multiplier such as `10x`.

**Talking point.** This is the headline property. Wall-clock pacing changes how
fast the replay runs but never changes the outcome, because the engine is driven
by a simulated clock bound to recorded timestamps. Strategy changes can be
attributed to the strategy, not to timing noise.

---

## 6. Export to HftBacktest

Convert a normalized dataset into the HftBacktest NPZ event format:

```bash
hft-platform-kit hft-export \
  --data data/09eb7002-1844-4de0-99a8-9662b7a4f0ce \
  --output export.npz
```

```json
{"event_count": 8164, "output": "export.npz"}
```

The event count exceeds the dataset event count because each book snapshot and
delta expands into one row per price level, which is what HftBacktest consumes.

**Talking point.** Captured data flows directly into an established backtesting
engine — no bespoke conversion scripts, and the same data backs both the
platform's own replay and third-party backtests.

---

## 7. Historical import via Tardis

Pull exchange-native historical data and normalize it into the same dataset
layout. The first day of each month is available anonymously:

```bash
hft-platform-kit tardis-import \
  --from-date 2025-01-01 --to-date 2025-01-02 --max-events 200
```

```json
{"output_directory": "data/91334fc9-...", "event_count": 200,
 "event": "tardis_import_completed"}
```

For other ranges, supply a key with `--api-key` or the `TARDIS_API_KEY`
environment variable.

Imported data is indistinguishable downstream:

```bash
hft-platform-kit hft-export --data data/91334fc9-... --output tardis.npz
```

```json
{"event_count": 859, "output": "tardis.npz"}
```

**Talking point.** Live capture and historical import converge on one schema, so
replay, export, and analytics work identically on both.

---

## 8. Live paper trading with observability

The full pipeline: live feed → order book → data-quality validation → strategy →
risk → OMS → simulated exchange → portfolio.

```bash
hft-platform-kit paper --duration-seconds 45 --status-port 8077
```

### Query the status endpoint while it runs

```bash
curl -s http://127.0.0.1:8077/status
```

```json
{"mode":"paper","run_id":"b2131690-...","symbol":"BTCUSDT",
 "feed_state":"healthy","event_count":763,"order_count":3,"fill_count":1,
 "market_data_delay_ns":0,"processing_lag_ns":90000,
 "message":"market data healthy","anomaly_count":2,
 "blocking_anomaly_count":2,"reconciliation_required_order_count":0,
 "orders_by_venue":{"unassigned":3}}
```

### Scrape Prometheus metrics

```bash
curl -s http://127.0.0.1:8077/metrics
```

```text
# HELP trading_poc_events_total Processed market events
# TYPE trading_poc_events_total counter
trading_poc_events_total 763.0
# HELP trading_poc_orders Current total order count
trading_poc_orders 3.0
# HELP trading_poc_fills Current total fill count
trading_poc_fills 1.0
# HELP trading_poc_feed_healthy Whether the market-data feed is healthy
trading_poc_feed_healthy 1.0
```

### Other run controls

```bash
hft-platform-kit paper --max-events 5000            # stop on event budget
hft-platform-kit paper --kill-after-seconds 30      # exercise the kill switch
```

**Talking point.** Operational surfaces are built in, not bolted on. The
`/metrics` endpoint drops straight into Prometheus and Grafana, and
`feed_healthy` plus `reconciliation_required_order_count` are the two signals an
operator would alert on.

---

## 9. Reports and artifacts

Every run writes a self-contained report directory:

```bash
ls reports/b2131690-754c-497a-9749-0611b3558d3d/
```

```text
anomalies.jsonl   fills.csv        opportunities.csv
order_updates.csv orders.csv       pnl_summary.json   run_manifest.json
```

| Artifact | Contents |
| --- | --- |
| `orders.csv` / `order_updates.csv` | Full order lifecycle transitions |
| `fills.csv` | Every simulated fill with fees |
| `opportunities.csv` | Signals the strategy identified |
| `anomalies.jsonl` | Data-quality findings with severity and cause |
| `pnl_summary.json` | Realized/unrealized PnL, fees, exposure peaks |
| `run_manifest.json` | Run inputs, config hash, and result hashes |

The run summary includes a portfolio breakdown:

```json
"portfolio": {"position": "0.00079600",
  "average_entry_price": "62736.666...", "realized_pnl": "0.00133064...",
  "unrealized_pnl": "0.01607787...", "fees": "0.0099878103980000",
  "net_pnl": "0.00742070960199999", "maximum_absolute_position": "0.00119400",
  "maximum_notional_exposure": "74.9043960000000000"}
```

**Talking point.** All monetary values are exact decimals, never floats. Fees,
maximum absolute position, and peak notional exposure are tracked explicitly for
risk review.

---

## 10. Multi-exchange EMS configuration

Show the guarded venue topology without touching any venue:

```bash
hft-platform-kit ems-inspect
```

```json
{"dispatch_policy": "sequential",
 "enabled_venues": ["binance", "okx", "deribit"],
 "instruments": {"BTC-PERPETUAL": {
     "canonical_quantity_unit": "base",
     "compatibility_group": "btc-usd-inverse-perpetual",
     "venues": { ... }}}}
```

**Talking point.** Instruments are mapped canonically across venues, including
each venue's quantity denomination. Binance spot sizes are base-denominated
while a Deribit inverse perpetual is USD-denominated — the mapping makes that
explicit so a single logical order can be split correctly across venues.

---

## 11. Native C++ SOR and TWAP

### Through the CLI

```bash
hft-platform-kit ems-demo
```

```json
{"mode": "inspection_only", "orders_submitted": 0,
 "route": [{"venue": "binance", "quantity": "0.5",
            "reference_price": "100000.0", "expected_fee": "5.0",
            "score": "100010.0"}],
 "twap": [{"quantity": "0.12500000", "timestamp_ns": 1000000000},
          {"quantity": "0.12500000", "timestamp_ns": 2000000000},
          {"quantity": "0.12500000", "timestamp_ns": 3000000000},
          {"quantity": "0.12500000", "timestamp_ns": 4000000000}],
 "twap_state": "scheduled"}
```

Note `"mode": "inspection_only"` and `"orders_submitted": 0` — the routing and
scheduling logic runs in full, but the submission path is disabled.

### Drive the native extension directly

```python
# native_demo.py
import hft_platform_kit._ems as ems

def quote(venue, bid, ask, fee_bps, latency_us):
    q = ems.VenueQuote()
    q.venue, q.symbol = venue, "BTC-PERPETUAL"
    q.bid, q.ask = bid, ask
    q.bid_size = q.ask_size = 1.0
    q.fee_bps, q.latency_us, q.healthy = fee_bps, latency_us, True
    return q

quotes = [
    quote("binance", 99990.0, 100000.0, 1.0, 800),
    quote("okx",     99985.0, 100005.0, 0.8, 500),
    quote("deribit", 99980.0, 100010.0, 0.5, 300),
]

for s in ems.SmartOrderRouter().route(ems.Side.BUY, 1.5, quotes):
    print(f"{s.venue:<8} qty={s.quantity:<5} px={s.reference_price:<10} "
          f"fee={s.expected_fee:<7} score={s.score}")

for s in ems.Scheduler.twap(0, 4_000_000_000, 1.0, 4):
    print(f"t={s.timestamp_ns:<12} qty={s.quantity}")
```

```text
binance  qty=1.0   px=100000.0   fee=10.0    score=100010.0
okx      qty=0.5   px=100005.0   fee=4.0002  score=100013.0004
t=0            qty=0.25
t=1333333333   qty=0.25
t=2666666666   qty=0.25
t=4000000000   qty=0.25
```

**Talking point.** Requesting 1.5 units against 1.0 of top-of-book liquidity
makes the router *spill* onto the next venue. The score blends price, fee, and
latency, so the cheapest headline price does not automatically win. This is
compiled C++ (`_ems`) exposed through pybind11.

Available native types:

```text
Event, ExecutionManagementSystem, Leg, MultiLegPlan, OrderType, PlanState,
RiskReservationBook, RouteSlice, ScheduledSlice, Scheduler, Side, SingleLoop,
SmartOrderRouter, VenueQuote
```

---

## 12. Two-venue concurrent-leg execution

This is the only demo that runs the **complete** execution lifecycle —
concurrent dispatch, partial fills, rejects, timeouts, deterministic unwind,
and reconciliation — with no credentials, no network, and no native
extensions. It works identically on Windows, macOS, and Linux.

Design detail lives in [TWO_VENUE_EXECUTION.md](TWO_VENUE_EXECUTION.md).

### Happy path: both legs land

```bash
hft-platform-kit opportunity-demo --scenario filled
```

```json
{"scenario": "filled", "state": "closed", "residual_quantity": "0",
 "duplicate_free": true, "replay_is_idempotent": true, "unwinds": [],
 "transitions": ["created", "risk_reserved", "orders_created", "dispatched",
                 "acknowledged", "fully_filled", "reconciling", "closed"],
 "reconciliation": {"balanced": true, "net_position": "0",
                    "open_order_count": 0, "risk_released": true,
                    "realized_pnl": "19.996", "fees": "20.004",
                    "exchange_positions": {"binance": "1", "okx": "-1"},
                    "local_positions":    {"binance": "1", "okx": "-1"},
                    "risk_positions":     {"binance": "1", "okx": "-1"}}}
```

**Talking point.** Three independently derived position views agree, and the
expected $40 edge nets to $19.996 after $20.004 of fees. The demo executes the
same signal twice; `"replay_is_idempotent": true` proves the second call
contacted no venue.

### Partial fill: residual is hedged deterministically

```bash
hft-platform-kit opportunity-demo --scenario partial
```

```json
{"scenario": "partial", "state": "closed",
 "transitions": ["created", "risk_reserved", "orders_created", "dispatched",
                 "acknowledged", "timeout", "partially_filled",
                 "hedge_required", "unwinding", "reconciling", "closed"],
 "unwinds": [{"venue": "okx", "side": "buy", "quantity": "0.6",
              "reason": "residual_exposure",
              "candidates": [{"venue": "okx",     "cost": "100020.0010"},
                             {"venue": "binance", "cost": "100020.0010"}]}],
 "reconciliation": {"net_position": "0.0", "balanced": true,
                    "hedge_cost": "60024.0", "slippage": "24.0"}}
```

**Talking point.** One leg filled 0.4 against the other's 1.0, leaving a 0.6
residual that breached the 5% policy. Note the `candidates` list: both venues
priced identically, so the tie was broken by configured priority — the
decision is reproducible, not arbitrary, and the losing candidate is persisted
for audit.

### Reject: classified, actioned, never retried

```bash
hft-platform-kit opportunity-demo --scenario reject
```

```json
{"scenario": "reject",
 "legs": [{"venue": "binance", "state": "rejected", "attempts": 1,
           "reject_reason": "invalid_order",
           "reject_action": "manual_investigation"},
          {"venue": "okx", "state": "filled", "filled_quantity": "1"}],
 "unwinds": [{"venue": "okx", "side": "buy", "quantity": "1"}]}
```

**Talking point.** A `MIN_NOTIONAL` filter failure is classified as an invalid
order, so it is attempted exactly once — retrying it would only burn rate
limit. The opposing leg's exposure is unwound automatically.

### Inspect the durable audit trail

```bash
hft-platform-kit opportunity-demo --scenario filled --store ledger.db
```

```python
from hft_platform_kit import OpportunityStore

with OpportunityStore("ledger-filled.db") as store:
    opportunity_id = "OPP-B05B22CA9D51AF5D"
    print(store.load_opportunity(opportunity_id).state)   # closed
    print(store.open_opportunity_ids())                   # ()
    for event in store.load_events(opportunity_id):
        print(event.sequence, event.event_type)
```

```text
1 opportunity_created     9  state_transition
2 risk_reserved          10  leg_fill_progress
3 state_transition       11  leg_fill_progress
4 state_transition       12  state_transition
5 leg_submitting         13  state_transition
6 leg_acknowledged       14  risk_released
7 leg_submitting         15  state_transition
8 leg_acknowledged       16  reconciliation
```

**Talking point.** `leg_submitting` is persisted *before* `leg_acknowledged` —
intent is durable before the venue is contacted. If the process dies between
those two events, restart recovery recomputes the same deterministic client
order ID, asks the venue whether it exists, and resumes without duplicating.

---

## 13. Guarded Binance Spot Testnet execution

All execution is pinned to Binance **Spot Testnet**. Production endpoints are
rejected by configuration.

### No credentials required

```bash
hft-platform-kit binance-testnet ping      # connectivity + clock skew
hft-platform-kit binance-testnet rules     # tick size, lot size, min notional
hft-platform-kit binance-testnet ticker    # current public price
```

`ping` reports a `time_offset_ms`, which matters because signed requests are
rejected if the local clock drifts.

### Demonstrate the credential guard

```bash
hft-platform-kit binance-testnet account
```

```text
error: Binance Testnet credentials are missing.
Set BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET.
```

**Talking point.** It fails closed with a precise remediation message rather
than attempting an unauthenticated call.

### With Testnet credentials

```bash
export BINANCE_TESTNET_API_KEY=...
export BINANCE_TESTNET_API_SECRET=...

hft-platform-kit binance-testnet account
```

Dry-run an order through risk and the OMS without sending it to the venue:

```bash
hft-platform-kit binance-testnet submit \
  --side buy --type limit --quantity 0.001 --price 20000 \
  --daily-net-pnl 0 --test-only
```

Submit for real on Testnet — note the explicit confirmation token:

```bash
hft-platform-kit binance-testnet submit \
  --side buy --type limit --quantity 0.001 --price 20000 \
  --daily-net-pnl 0 --confirm TESTNET
```

Then inspect and unwind it:

```bash
hft-platform-kit binance-testnet open-orders
hft-platform-kit binance-testnet query  --client-order-id <id>
hft-platform-kit binance-testnet cancel --client-order-id <id> --confirm TESTNET
hft-platform-kit binance-testnet reconcile
```

Amend a resting order in place. This is a single exchange `cancelReplace`
call in `STOP_ON_FAILURE` mode, not a cancel followed by a fresh submit, so
a failed cancel can never leave the replacement resting *alongside* the
original:

```bash
hft-platform-kit binance-testnet modify \
  --cancel-client-order-id <id> \
  --side buy --type limit --quantity 0.001 --price 21000 \
  --daily-net-pnl 0 --confirm TESTNET
```

Only resting orders can be modified; a filled or cancelled order is refused
with a reason. A replace does not consume open-order capacity, because it
withdraws one order as it adds one. There is no separate *delete* verb —
removing a resting order is `cancel`.

Dispatch several legs together. Every leg is validated, risk-checked and
balance-checked **before any of them is transmitted**, so a single bad leg
means nothing reaches the venue at all:

```bash
hft-platform-kit binance-testnet multi-leg \
  --leg BTCUSDT:BUY:LIMIT:0.002:20000 \
  --leg ETHUSDT:SELL:LIMIT:0.05:5000 \
  --daily-net-pnl 0 --confirm TESTNET
```

Leg syntax is `SYMBOL:SIDE:TYPE:QUANTITY[:PRICE]`, minimum two legs. The
balance check sums the requirement across *all* legs rather than testing each
in isolation. Admitted legs are then dispatched concurrently, and a failure
on one is reported without discarding the other's outcome. Atomicity applies
to **admission only** — once both orders are live, either can still fill
alone, which is what the hedging and unwind logic exists to handle.

| Flag | Why it exists |
| --- | --- |
| `--type {market,limit,post-only}` | Explicit order type; no implicit default |
| `--daily-net-pnl` | Required input to the daily-loss risk check |
| `--test-only` | Runs risk and OMS validation, sends nothing |
| `--confirm TESTNET` | Mandatory typed token for any state-changing call |
| `--leg SYMBOL:SIDE:TYPE:QTY[:PRICE]` | One repeatable leg of a multi-leg trade |

`submit` runs the order through risk checks and the OMS before it reaches the
venue. `reconcile` compares local OMS state against venue state and reports any
order needing attention — the same counter exposed as
`reconciliation_required_order_count` on `/status`.

**Talking point.** Both `submit` and `cancel` refuse to act without
`--confirm TESTNET`, and `--daily-net-pnl` is mandatory rather than defaulted,
so the daily-loss limit can never be silently bypassed. `modify` and
`multi-leg` enforce the same two rules.

---

## 14. Embed the Python SDK

The platform is a library first. Replay a dataset inside your own process:

```python
# sdk_demo.py
import asyncio
from hft_platform_kit import replay_dataset

async def main():
    engine = await replay_dataset("data/09eb7002-...", speed="max")
    print("events processed :", engine.event_count)
    print("orders           :", len(engine.oms.orders))
    print("realized pnl     :", engine.portfolio.net_realized_pnl)
    print("portfolio        :", engine.portfolio.snapshot(engine.book.mid_price))

asyncio.run(main())
```

```text
events processed : 358
orders           : 3
realized pnl     : -0.00249705020300
portfolio        : {'position': '-0.000397', 'average_entry_price': '62897.99',
                    'net_pnl': '-0.00249903520300', ...}
```

`create_engine()` builds an engine with injectable components:

```python
from hft_platform_kit import create_engine, AppConfig

engine = create_engine(
    AppConfig(),
    strategy=my_strategy,   # also: risk, oms, exchange, portfolio, data_quality
)
```

The package ships `py.typed`, so `AppConfig`, `Order`, `Fill`, and the rest are
fully typed for your editor and type checker. See `SDK_API.md` for the
full surface.

**Talking point.** Every subsystem — risk, OMS, exchange, portfolio,
data quality — is replaceable through constructor injection, so the platform can
be adopted piecemeal rather than all at once.

---

## 15. Plug in a custom strategy

Implement the `Strategy` protocol — no inheritance required:

```python
# strategy_demo.py
import asyncio
from decimal import Decimal
from uuid import uuid4
from hft_platform_kit import OrderIntent, OrderType, Side, replay_dataset

class WideQuoteStrategy:
    strategy_id = "demo_wide_quote"
    strategy_version = "1.0.0"

    def __init__(self):
        self.evaluations = 0

    def evaluate(self, book, timestamp_ns, open_orders):
        self.evaluations += 1
        if open_orders or book.best_bid is None:
            return ()
        return (OrderIntent(
            intent_id=uuid4(),
            strategy_id=self.strategy_id,
            symbol="BTCUSDT",
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("0.001"),
            price=book.best_bid * Decimal("0.999"),
            created_timestamp_ns=timestamp_ns,
        ),)

    def should_cancel(self, book):
        return False

async def main():
    strategy = WideQuoteStrategy()
    engine = await replay_dataset("data/09eb7002-...", strategy=strategy)
    print("evaluations:", strategy.evaluations)
    print("orders     :", len(engine.oms.orders))

asyncio.run(main())
```

```text
evaluations: 111
orders     : 1
```

**Talking point.** A custom strategy is a plain class satisfying a protocol. It
inherits deterministic replay, risk checks, OMS lifecycle management, and
reporting for free.

---

## 16. Safety and resilience behaviours

Worth showing explicitly — these are the properties that matter in production.

### Data-quality validation is live

During any run, look for `market_data_anomaly` records:

```json
{"code": "out_of_order_book_update", "severity": "warning",
 "message": "duplicate or stale order-book update was dropped",
 "blocking": true, "details": {"book_last_update_id": "98095132875",
 "delta_first_update_id": "98095132718"}, "event": "market_data_anomaly"}
```

```json
{"code": "stale_market_data", "severity": "error",
 "message": "market event exceeded the configured receive delay",
 "details": {"delay_ns": "2046644000"}}
```

Stale and out-of-order updates are detected, classified, quarantined, and
counted — never silently applied to the book.

### Execution is guarded by default

- `ems-demo` reports `"mode": "inspection_only"` and `"orders_submitted": 0`.
- Execution is pinned to Binance Spot Testnet; production URLs are refused.
- Missing credentials fail closed with actionable messages.

### Native dependencies degrade gracefully

`pyarrow`, `numpy`, and `hftbacktest` load on first use, not at import. If a
Windows Application Control policy blocks a native DLL, the SDK still imports and
the pure-Python surfaces keep working:

```python
import hft_platform_kit          # succeeds even if pyarrow's DLL is blocked
from hft_platform_kit import AppConfig, BinanceOrderBook, RiskEngine
```

A blocked optional feature raises a targeted error naming the cause:

```text
OptionalDependencyError: Parquet support requires the optional dependency
'pyarrow', which could not be imported (DLL load failed while importing lib...).
... a Windows Application Control policy (WDAC/AppLocker/Smart App Control) is
most likely blocking pyarrow's native library ... or use the JSONL dataset
paths, which need no native dependency.
```

### The test suite is hermetic

```bash
uv run pytest -q
```

```text
747 passed, 1 skipped
```

Outbound network access is blocked for every test except the explicitly
credential-gated venue smoke tests, so results never depend on the runner's
geography. The single skip is the credential-gated Binance Testnet signed test.

---

## Suggested demo agendas

### 10-minute executive walkthrough

| Min | Step | Message |
| --- | --- | --- |
| 0–1 | §2 `libraries`, `hft-health` | The stack is real and verifiable |
| 1–3 | §3 `record` | Live market data capture |
| 3–5 | §5 replay twice | Identical `result_hash` — determinism |
| 5–8 | §8 `paper` + `/status` | Full pipeline with live observability |
| 8–10 | §9 reports | Auditable, exact-decimal artifacts |

### 30-minute technical deep dive

Add §4 (manifest and checksums), §6–§7 (backtest export and historical import),
§10–§11 (EMS mapping and native SOR spill behaviour), §12 (two-venue
concurrent-leg lifecycle), §13 (guarded execution and credential failure),
§14–§15 (SDK embedding and custom strategy), and §16 (data-quality anomalies
and graceful degradation).

### Offline / no-network demo

Pre-record a dataset, then run §4, §5, §6, §9, §10, §11, §12, §14, §15, and the
test suite in §16. Only §3, §7, §8, and §13 require connectivity. §12 needs no
network, credentials, or native extensions at all.

---

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `replay dataset does not exist` | Use the full path under `data/`; run `hft-platform-kit datasets` to list IDs |
| Port already in use | Choose another `--status-port` |
| No fills in a short run | Spread capture needs opportunities; extend `--duration-seconds` |
| `to_date must be later than from_date` | Tardis ranges are half-open — use `--from-date 2025-01-01 --to-date 2025-01-02` |
| Tardis range rejected | Only the first day of each month is anonymous; set `TARDIS_API_KEY` |
| `credentials are missing` | Expected without keys; export `BINANCE_TESTNET_API_KEY` and `BINANCE_TESTNET_API_SECRET` |
| `DLL load failed` on Windows | An Application Control policy is blocking a native dependency; see §16 |

## Related documents

- `README.md` — installation and platform support
- `SDK_API.md` — full SDK reference
- `MULTI_EXCHANGE.md` — venue mapping and denomination rules
- `CLIENT_DEMO_RUNBOOK.md` — scripted client-call runbook
- `HFT_VERIFICATION_MATRIX.md` — what has been verified, and known limits
- `TWO_VENUE_EXECUTION.md` — concurrent-leg state machine, idempotency, and unwind
