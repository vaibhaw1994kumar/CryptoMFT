# hft-platform-kit SDK

`hft-platform-kit` is an installable Python SDK plus CLI for embedding the PoC
market-data, replay, strategy, risk, OMS, paper-execution, and reporting
components in another codebase.

## Installation

From a built wheel:

```bash
uv build --wheel --clear
pip install dist/hft_platform_kit-*.whl
```

The wheel is platform-tagged because it contains the native
`hft_platform_kit._ems` extension.

From PyPI after publication:

```bash
pip install hft-platform-kit
```

## Basic Imports

```python
from hft_platform_kit import (
    AppConfig,
    BinanceOrderBook,
    ReplaySource,
    RiskEngine,
    TradingEngine,
    create_engine,
    replay_dataset,
)
```

## Embed the Default Engine

```python
from hft_platform_kit import AppConfig, create_engine

config = AppConfig()
engine = create_engine(config)
```

## Replay Programmatically

```python
import asyncio

from hft_platform_kit import replay_dataset


async def main() -> None:
    engine = await replay_dataset(
        "data/my-recording",
        speed="max",
    )
    print(engine.book.checksum())
    print(engine.portfolio.snapshot(engine.book.mid_price))


asyncio.run(main())
```

## Supply a Custom Strategy

Custom strategies implement the `Strategy` protocol:

```python
from hft_platform_kit import (
    BinanceOrderBook,
    Order,
    OrderIntent,
)


class MyStrategy:
    strategy_id = "my_strategy"
    strategy_version = "1.0.0"

    def evaluate(
        self,
        book: BinanceOrderBook,
        timestamp_ns: int,
        open_orders: tuple[Order, ...],
    ) -> tuple[OrderIntent, ...]:
        return ()

    def should_cancel(self, book: BinanceOrderBook) -> bool:
        return False
```

Inject it:

```python
from hft_platform_kit import create_engine

engine = create_engine(strategy=MyStrategy())
```

## Replace Components

`TradingEngine` accepts injectable implementations for:

- Order book
- Strategy
- Risk engine
- OMS
- Execution venue
- Portfolio

```python
engine = TradingEngine(
    config,
    strategy=my_strategy,
    risk=my_risk_engine,
    exchange=my_execution_venue,
)
```

Execution adapters can implement the `ExecutionVenue` protocol. Market-data
adapters can implement the `MarketDataSource` protocol.

## HftBacktest Market Making

The notebook-compatible HftBacktest layer is a reusable SDK rather than a
sequence of stateful cells. Paths, latency, queue/exchange models, ROI bounds,
fees, grid parameters, and inventory skew are explicit configurations.
Official HftBacktest raw-data preparation and multi-asset grid tutorial
profiles are included rather than requiring notebook-only glue.

```python
from hft_platform_kit import (
    GridMarketMakingConfig,
    HftBacktestAssetConfig,
    SyntheticHftConfig,
    generate_synthetic_hft_dataset,
    run_grid_market_making_backtest,
)

dataset = generate_synthetic_hft_dataset(
    "data/synthetic_hft_data",
    SyntheticHftConfig(duration_seconds=300, seed=42),
)
result = run_grid_market_making_backtest(
    dataset.data_path,
    dataset.initial_snapshot_path,
    asset_config=HftBacktestAssetConfig(
        tick_size=0.1,
        lot_size=0.001,
        roi_lower_bound=50_000,
        roi_upper_bound=70_000,
    ),
    strategy_config=GridMarketMakingConfig(
        grid_levels=20,
        max_position=5,
        grid_interval_ticks=10,
        half_spread_ticks=20,
        order_quantity=0.1,
        inventory_skew=10,
    ),
)
print(result.summary)
# The original tutorial's plot remains available.
# result.statistics.plot()
```

Raw Binance Futures collector feeds are converted with HftBacktest's own
timestamp correction and row reordering. Tardis inputs are separated into
trade and depth lists so trade files are always passed first.

```python
from hft_platform_kit import (
    HftTardisSnapshotMode,
    convert_binance_futures_raw,
    convert_tardis_csv,
    create_hft_last_snapshot,
)

convert_binance_futures_raw(
    "usdm/btcusdt_20240808.gz",
    output_path="usdm/btcusdt_20240808.npz",
)
convert_tardis_csv(
    ["trades.csv.gz"],
    ["incremental_book_L2.csv.gz"],
    output_path="tardis/btcusdt_20240808.npz",
    snapshot_mode=HftTardisSnapshotMode.IGNORE_START_OF_DAY,
)
create_hft_last_snapshot(
    ["usdm/btcusdt_20240808.npz"],
    tick_size=0.1,
    lot_size=0.001,
    output_path="usdm/btcusdt_20240808_eod.npz",
)
```

The official multi-asset tutorial profile is also reusable:

```python
from pathlib import Path

from hft_platform_kit import (
    MultiAssetGridConfig,
    run_multi_asset_grid_backtests,
)

portfolio = run_multi_asset_grid_backtests(
    [
        MultiAssetGridConfig(
            asset_name="BTCUSDT",
            data_paths=tuple(
                sorted(Path("data").glob("btcusdt_202307*.npz"))
            ),
            initial_snapshot_path=Path("data/btcusdt_20230630_eod.npz"),
            mid_price=30_500,
            tick_size=0.1,
            lot_size=0.001,
            order_latency_paths=tuple(
                sorted(Path("latency").glob("live_latency_202307*.npz"))
            ),
        ),
    ],
    output_directory="stats",
)
print(portfolio.portfolio_equity)
```

The exported APIs cover the notebook and official tutorial surfaces:

| Capability | SDK API |
| --- | --- |
| Binance Futures raw gzip conversion, timestamp correction/reordering, direct NPZ output | `convert_binance_futures_raw` |
| Tardis trade-before-depth conversion, buffers, and snapshot policy | `convert_tardis_csv`, `HftTardisSnapshotMode` |
| Synthetic L2/trade feed and initial snapshot | `generate_synthetic_hft_dataset` |
| Data validation and NPZ loading | `validate_hft_event_data`, `load_hft_event_data` |
| Single/multi-day EOD snapshot and prior-snapshot chaining | `create_hft_last_snapshot` |
| Constant/multi-file interpolated latency, queue, exchange, fees, ROI | `HftBacktestAssetConfig`, `build_hftbacktest_asset` |
| BBO and N-level depth | `sample_hft_order_book` |
| Top/0.5%/1% depth imbalance | `calculate_hft_order_book_imbalance` |
| Public trade capture | `sample_hft_market_trades` |
| 10-second and rolling buy/sell VWAP | `calculate_hft_rolling_vwap` |
| Feed-derived order latency | `generate_feed_order_latency` |
| Pending, acknowledged, cancel, market, and GTX states | `run_hft_order_lifecycle` |
| Tick- or price-based grid, absolute/normalized inventory skew | `GridMarketMakingConfig`, `run_grid_market_making_backtest` |
| `log_prob_queue_model2`, notional sizing, per-asset runs, portfolio equity | `MultiAssetGridConfig`, `run_multi_asset_grid_backtests` |
| Recorder NPZ, performance table, and tutorial plot object | `HftBacktestRunResult.records`, `.summary`, `.statistics` |

For an executable end-to-end example, see
[`examples/hft_market_making.py`](examples/hft_market_making.py).

## Data Quality

The default engine includes a `DataQualityValidator` covering timestamp
ordering and skew, delayed data, price and quantity bounds, duplicate trades,
abnormal price movements, minimum book depth, maximum spread, and deterministic
local checksum consistency.

```python
from hft_platform_kit import (
    AnomalyCode,
    DataQualityConfig,
    DataQualityValidator,
)


validator = DataQualityValidator(DataQualityConfig())
engine = create_engine(data_quality=validator)

# Poll from the thread-safe local alert queue.
anomaly = validator.alerts.get_nowait()
print(anomaly.to_dict())
```

Blocking events are prevented from reaching the strategy. Recording mode keeps
accepted native messages in `raw.jsonl`; rejected native and canonical events
plus their anomaly reasons are written to `quarantine.jsonl`. Accepted
canonical events remain in `events.jsonl` and `events.parquet`.

### Feed integrity checks

Message-level checks catch corruption the per-field bounds cannot see: a book
that is crossed or locked, a side whose levels are not monotonically ordered, a
repeated price level, ingestion falling behind, and a feed that has simply gone
quiet.

```python
import time

from hft_platform_kit import AnomalyCode, DataQualityConfig, DataQualityValidator

validator = DataQualityValidator(
    DataQualityConfig(
        max_processing_latency_ms=50,   # receive -> process budget
        max_heartbeat_interval_ms=5000, # tolerated silence per symbol
        reject_crossed_books=True,      # a crossed book is never tradable
        reject_processing_latency=False,# slow, but the data is still valid
    )
)

# Heartbeats are recorded automatically for every validated event, and can be
# polled on a timer so silence is detectable when no message arrives at all.
for anomaly in validator.check_heartbeats(time.time_ns()):
    print(anomaly.code, anomaly.symbol, anomaly.details["silence_ns"])
```

| Code | Detects | Blocking by default |
| --- | --- | --- |
| `CROSSED_BOOK` | Best bid >= best ask in a snapshot | Yes |
| `BOOK_LEVEL_ORDERING` | Bids not descending, asks not ascending | Yes |
| `DUPLICATE_BOOK_LEVEL` | A side repeats a price | Yes |
| `EXCESSIVE_PROCESSING_LATENCY` | Receive-to-process exceeded budget | No |
| `FEED_HEARTBEAT_TIMEOUT` | No messages within the interval | Yes |

Ordering and the crossed-market test apply to **snapshots** only — a delta
legitimately carries removals and one-sided updates, so enforcing monotonicity
on deltas would produce false positives. Duplicate prices are contradictory in
both, and are always rejected.

## Two-Venue Concurrent-Leg Execution

`OpportunityCoordinator` executes a two-leg strategy across two venues
concurrently, backed by a SQLite write-ahead ledger so a crash cannot produce a
duplicate order. It imports nothing native, so it runs where `pyarrow` and
`hftbacktest` cannot.

```python
import asyncio
from decimal import Decimal

from hft_platform_kit import (
    LegRequest, MarketQuote, OpportunityCoordinator, OpportunityRequest,
    OpportunityStore, OpportunityTimeouts, ResidualPolicy, Side,
)


async def main() -> None:
    with OpportunityStore("ledger.db") as store:
        coordinator = OpportunityCoordinator(
            {"binance": binance_adapter, "okx": okx_adapter},
            store,
            timeouts=OpportunityTimeouts(
                ack_timeout_ms=100,
                first_fill_timeout_ms=500,
                complete_fill_timeout_ms=2000,
            ),
            residual_policy=ResidualPolicy(
                max_residual_ratio=Decimal("0.05"),
                max_residual_duration_ms=2000,
                unwind_venue_priority=("okx", "binance"),
            ),
        )
        coordinator.update_quote(
            MarketQuote(
                venue="okx", symbol="BTCUSDT",
                bid_price=Decimal("99990"), ask_price=Decimal("100010"),
                fee_bps=Decimal("1"),
            )
        )

        result = await coordinator.execute(
            OpportunityRequest(
                strategy_id="two-venue-basis",
                strategy_version="v1.0.0",
                signal_id="signal-42",       # identity: replay is a no-op
                canonical_symbol="BTCUSDT",
                legs=(
                    LegRequest("binance", "BTCUSDT", Side.BUY,
                               Decimal("1"), Decimal("100000")),
                    LegRequest("okx", "BTCUSDT", Side.SELL,
                               Decimal("1"), Decimal("100040")),
                ),
                expected_edge=Decimal("40"),
            )
        )
        print(result.state, result.reconciliation.balanced)

        # After a restart, resume everything still open.
        await coordinator.recover()


asyncio.run(main())
```

Key guarantees:

- **Idempotent.** `opportunity_id`, client order IDs, and idempotency keys are
  pure functions of the signal. Re-executing the same signal returns the stored
  result without contacting a venue.
- **Restart safe.** `recover()` reloads non-terminal opportunities, recomputes
  the deterministic client order IDs, and asks each venue whether the order
  already exists before considering a resubmission.
- **Deterministically unwound.** Venue selection ranks on
  `(priority, all-in cost, venue name)`, and every rejected candidate is
  persisted alongside the chosen plan.
- **Reconciled four ways.** Exchange, local, risk, and ledger positions must
  agree before an opportunity closes. The risk view is accumulated
  independently as fills arrive, not recomputed from the leg table, so it
  can actually disagree.
- **Honest about fees.** `reconciliation.fees_by_currency` reports fees per
  currency; the scalar `fees` carries only the `quote_currency` total.
  Venues that charge in the base asset are flagged as a discrepancy rather
  than folded into a quote-denominated PnL.

Two errors are worth catching explicitly. `SignalHashMismatchError` means a
stored opportunity with the same ID describes a different set of legs —
resuming it would execute a plan you did not ask for. `LegIdentityError`
means something tried to rewrite a leg's venue, symbol, side, or target
quantity after it was written.

`SimulatedVenue` provides an offline, scriptable exchange implementing the
`ExchangeAdapter` protocol, so the whole lifecycle can be exercised in tests
and demos with no credentials:

```bash
hft-platform-kit opportunity-demo --scenario all
```

See [TWO_VENUE_EXECUTION.md](TWO_VENUE_EXECUTION.md) for the state diagram,
timeout ladder, and reject classification table.

## Execution controls

Every outbound message is checked against venue rate limits, the firm-wide
throttle, market-data staleness, and the kill switch, in a fixed precedence
order. `ExecutionControlPlane.evaluate()` returns a `ControlDecision`
carrying the action, the binding control, its level in the hierarchy, the
reason, and a retry hint.

```python
from hft_platform_kit import (
    ExecutionControlPlane,
    FeedMonitor,
    MessageBudget,
    MessageRequest,
    OrderStage,
    Priority,
)

plane = ExecutionControlPlane(
    venue_budgets={
        "binance": MessageBudget(name="binance", limit=100, window_ms=1000),
    },
    global_budget=MessageBudget(name="firm", limit=20_000, window_ms=1000),
    feed_monitor=FeedMonitor(stale_after_ms=100),
)
plane.feed_monitor.record_tick("binance")

decision = plane.evaluate(
    MessageRequest(
        message_id="ord-1",
        strategy_id="spread-capture",
        venue="binance",
        stage=OrderStage.NEW,
        priority=Priority.MARKET_MAKING,
    )
)
if decision.transmits:
    ...  # send it
elif decision.synchronous_rejection:
    ...  # nothing was sent; tell the caller now
```

`ReservationLedger.reserve()` takes message capacity, notional, margin,
position limits, and hedge capacity for every leg atomically, releasing
everything if any component fails.

A reserved message is *drawn down* at transmission rather than charged
again, so an opportunity spends exactly what it reserved. The reservation
is matched against the requesting strategy and instrument: it authorises
that opportunity's own traffic, not anything that quotes its id. Pass
`require_reservation=True` to refuse any message that holds none.

A spread reserves one instrument tier per leg:

```python
from decimal import Decimal
from hft_platform_kit import ReservationRequest

ledger.reserve(
    ReservationRequest(
        opportunity_id="OPP-1",
        strategy_id="spread-capture",
        venue_messages={"binance": 3, "okx": 3},
        instrument="BTCUSDT",
        venue_instruments={"okx": "ETHUSDT"},
        hedge_messages=2,
        notional=Decimal("1200000"),
    )
)
```

Beyond the firm and venue budgets, the plane accepts optional
`pm_budgets`, `strategy_budgets`, `instrument_budgets`, and
`max_messages_per_order`, so the full published hierarchy is enforceable.
Each is opt-in; an unconfigured tier never binds.

Scheduling classes have utilisation ceilings so priority allocates
capacity rather than only reordering a queue. Defaults are exported as
`PRIORITY_CEILING_PCT` and overridable with `priority_ceilings=`, which
the plane forwards to its reservation ledger. Risk-reducing classes have
no ceiling.

A ceiling is applied at admission, on drain, and on `ledger.reserve` —
reserving is the one call that can draw a whole budget at once, so a
ceiling checked only at admission is one a caller can evade by reserving
first. Set `ReservationRequest.priority` to select the ceiling; it
defaults to `MARKET_MAKING`, which has none. A request that would take a
tier past its ceiling raises `CapacityReservationError` and reserves
nothing, on any tier.

The four decision tables the evaluator consults are exported as data, so
a caller can reproduce any transition without reading the evaluator:

| Export | Contents |
| --- | --- |
| `CONTROL_MATRIX` | `{OrderStage: {ControlKind: ControlAction \| None}}` — the required behaviour for all 40 combinations |
| `CONTROL_PRECEDENCE` | `((ControlKind, ControlLevel), ...)` in evaluation order, kill switch first |
| `EXEMPT_PRIORITIES` | The priorities a capacity control may never block |
| `RISK_REDUCING_STAGES` | The order states that only ever reduce exposure |

```python
from hft_platform_kit import CONTROL_MATRIX, CONTROL_PRECEDENCE, OrderStage

for control, level in CONTROL_PRECEDENCE:
    action = CONTROL_MATRIX[OrderStage.NEW][control]
    print(control.value, level.name, action.value if action else "n/a")
```

```bash
hft-platform-kit controls-demo --scenario all
```

See [EXECUTION_CONTROLS.md](EXECUTION_CONTROLS.md) for the full
control-to-order-state matrix, the precedence hierarchy, and the
utilization bands.

## Binance Spot Testnet Execution

```python
import asyncio

from hft_platform_kit import (
    BinanceExecutionService,
    BinanceSpotTestnetGateway,
    OrderManagementSystem,
)


async def main() -> None:
    oms = OrderManagementSystem()
    async with BinanceSpotTestnetGateway() as gateway:
        await gateway.ping()
        account = await gateway.account()
        open_orders = await gateway.open_orders("BTCUSDT")
        report = await gateway.reconcile(oms, "BTCUSDT")
        print(account, open_orders, report)


asyncio.run(main())
```

Credentials are read from:

```text
BINANCE_TESTNET_API_KEY
BINANCE_TESTNET_API_SECRET
```

`BinanceExecutionService` submits risk-approved OMS orders and applies Binance
statuses back into the OMS lifecycle. Supported order operations are:

- Market
- Limit/GTC
- Post-only through `LIMIT_MAKER`
- Cancel
- Query
- Account and open-order snapshots
- Recent-trade retrieval
- Local-versus-exchange reconciliation

Binance documents `-1006 UNEXPECTED_RESP` and `-1007 TIMEOUT` as unknown
execution status in its
[official Spot API error codes](https://developers.binance.com/docs/binance-spot-api-docs/errors).
The gateway treats either code as ambiguous regardless of HTTP status and
never blindly retries the submit or cancel. Instead, it queries with the
deterministic `client_order_id` and applies a found live, filled, or canceled
status. If the query returns `-2013` or is itself transiently ambiguous,
`BinanceAmbiguousOrderError` is raised: unresolved submissions remain
`SUBMITTED`, while unresolved cancels become `RECONCILIATION_REQUIRED`. Both
states remain open in the OMS so risk controls continue to reserve exposure.

## Native EMS and Multi-Exchange Execution

The public SDK exports the Python coordinator and guarded adapters:

```python
from hft_platform_kit import (
    BinanceTestnetAdapter,
    DeribitTestAdapter,
    DispatchPolicy,
    ExecutionCoordinator,
    ExecutionLeg,
    ExecutionPlanRequest,
    OKXDemoAdapter,
    VenueQuote,
)
```

The coordinator wraps `hft_platform_kit._ems`, a C++20/pybind11 extension that
provides a thread-affine event loop, atomic multi-leg risk reservations,
fee/latency-aware SOR, TWAP/VWAP schedules, and guarded recovery states.
`ExecutionCoordinator` binds these objects to one Python asyncio loop.

Scheduled execution has an aggregate lifecycle in addition to per-slice
results:

```python
schedule = await coordinator.schedule_twap(...)
await coordinator.run_due(now_ns)

status = coordinator.schedule_result(schedule.schedule_id)
status = await coordinator.reconcile_schedule(schedule.schedule_id)
status = await coordinator.cancel_schedule(schedule.schedule_id)
status = await coordinator.complete_schedule_recovery(
    schedule.schedule_id,
    unwind_completed=False,
    recovery_orders=(),
)
```

`ScheduleResult` exposes `state`, `state_history`, every planned route/order,
slice results, cumulative canonical filled quantity, remaining quantity,
residual canonical exposure, reconciliation errors, and reservation release
status. Accepted, open,
partially-filled, ambiguous, and cancel-requested orders retain the aggregate
risk reservation. Cancellation always finishes with an order query; a cancel
acknowledgement alone is not terminal. The reservation releases only after
every route is confirmed terminal with either no fill or full requested
completion. A terminal partial fill enters `ScheduleState.UNWIND_REQUIRED` and retains risk
until `complete_schedule_recovery(..., unwind_completed=True,
recovery_orders=...)` verifies distinct filled hedge/unwind orders and reduces
the residual exposure to zero. A bare operator boolean cannot release risk.
External resolution uses `resolve_schedule_recovery`, with a reason,
external evidence, and terminal evidence for every submitted route that
query/cancel reconciliation could not confirm. If those terminal records
establish a partial fill, verified `recovery_orders` are also required:

```python
from trading_platform_poc.ems import (
    RouteOrderStatus,
    ScheduleRecoveryOrderEvidence,
)

status = await coordinator.resolve_schedule_recovery(
    schedule.schedule_id,
    reason="venue support confirmed cancellation",
    external_evidence="support ticket OPS-41",
    order_evidence=(
        ScheduleRecoveryOrderEvidence(
            venue="binance",
            exchange_order_id="12345",
            status=RouteOrderStatus.CANCELED,
            reconciliation_evidence="venue support confirmed terminal state",
        ),
    ),
)
```

Multi-leg plans retain their atomic reservation while any original route is
live, ambiguous, or unreconciled, or while the native plan remains in
`HEDGE_REQUIRED`, `UNWIND_REQUIRED`, or `RECOVERY_REQUIRED`. Recovery requires
reconciled fill evidence for the hedge/unwind orders:

```python
from decimal import Decimal

from hft_platform_kit import PlanRecoveryOrderEvidence, Side

result = await coordinator.complete_plan_recovery(
    plan_id,
    recovery_orders=(
        PlanRecoveryOrderEvidence(
            recovery_id="unwind-btc-1",
            canonical_symbol="BTCUSDT",
            side=Side.SELL,
            filled_quantity=Decimal("1"),
            venue="binance",
            exchange_order_id="12345",
            reconciliation_evidence="query_order confirmed FILLED",
        ),
    ),
)
```

Every recovery order must be filled and reconciled through its configured
venue adapter, and the signed canonical exposure must be zero for every symbol
before risk is released. For recovery
performed outside the process, `resolve_plan_recovery(plan_id, reason=...,
external_evidence=...)` is the explicit operator override. It still refuses
release while an original route is live, ambiguous, or unreconciled. Both APIs
are idempotent and retain recovery actions, evidence, and the terminal
`CLOSED` plan state. Original and recovery fills are coordinator-wide unique by venue plus exchange
order ID, falling back to venue plus client order ID when no exchange ID
exists. Re-labeling an original fill as an opposite-side hedge, reusing a fill
across plans or schedules, or supplying conflicting evidence is rejected and
never reduces residual exposure.

`hft-platform-kit ems-demo` previews SOR and TWAP output with synthetic quotes.
It is inspection-only and submits zero orders. Signed Binance Testnet, OKX
Demo, and Deribit Test operations require environment-provided credentials;
public time/instrument endpoints do not.

See [MULTI_EXCHANGE.md](MULTI_EXCHANGE.md) for endpoint restrictions and
[HFT_VERIFICATION_MATRIX.md](HFT_VERIFICATION_MATRIX.md) for test evidence.

## CLI

The package installs both commands:

```bash
hft-platform-kit --help
trading-platform-poc --help
```

The second command is retained for backward compatibility. Both are also
runnable as modules (`python -m hft_platform_kit.cli`).

Command results are printed to **stdout** as JSON; structured logs go to
**stderr**. That separation means results can be piped directly:

```bash
hft-platform-kit opportunity-demo --scenario filled 2>/dev/null \
  | jq '.scenarios[0].reconciliation.balanced'
```

## Stability

Version `0.x` is an alpha API. Public names exported from
`hft_platform_kit` are the supported SDK surface. Modules under
`trading_platform_poc` are retained for compatibility but should be considered
internal implementation details for new integrations.
