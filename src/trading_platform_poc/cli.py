from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from trading_platform_poc.applications import (
    run_paper,
    run_record,
    run_replay,
    run_tardis_import,
)
from trading_platform_poc.binance_cli import (
    configure_binance_parser,
    run_binance_command,
)
from trading_platform_poc.book_display import run_book_display
from trading_platform_poc.config import AppConfig
from trading_platform_poc.datasets import list_datasets
from trading_platform_poc.hftbacktest_integration import (
    export_hftbacktest_npz,
    hftbacktest_health,
)
from trading_platform_poc.hftbacktest_market_making import (
    GridMarketMakingConfig,
    HftBacktestAssetConfig,
    HftLatencyModel,
    HftOrderScenario,
    HftQueueModel,
    HftTardisSnapshotMode,
    SyntheticHftConfig,
    convert_binance_futures_raw,
    convert_tardis_csv,
    create_hft_last_snapshot,
    generate_feed_order_latency,
    generate_synthetic_hft_dataset,
    run_grid_market_making_backtest,
    run_hft_order_lifecycle,
)
from trading_platform_poc.controls_demo import (
    CONTROL_SCENARIOS,
    run_controls_demo,
)
from trading_platform_poc.logging import configure_logging
from trading_platform_poc.library_integrations import library_integrations
from trading_platform_poc.minreq.cli import (
    MR_COMMANDS,
    configure_minreq_parser,
    run_minreq_command,
)
from trading_platform_poc import runtime
from trading_platform_poc.ems import (
    DispatchPolicy,
    ExecutionCoordinator,
    ExecutionLeg,
    ScheduleState,
    VenueQuote,
    build_guarded_adapters,
    instrument_mappings_from_config,
)
from trading_platform_poc.exchange_adapter import ExchangeAdapter
from trading_platform_poc.models import Side, OrderType
from trading_platform_poc.opportunity import (
    LegRequest,
    MarketQuote,
    OpportunityCoordinator,
    OpportunityRequest,
    OpportunityTimeouts,
    ResidualPolicy,
)
from trading_platform_poc.opportunity_store import OpportunityStore
from trading_platform_poc.simulated_venue import (
    FillStep,
    SimulatedVenue,
    VenueScript,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="HFT platform SDK and command-line toolkit"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="path to the YAML configuration file",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    record = subparsers.add_parser("record", help="record Binance market data")
    record.add_argument(
        "--duration-seconds",
        type=float,
        default=30,
        help="stop recording after this many seconds",
    )
    record.add_argument(
        "--max-events",
        type=int,
        help="stop after this many normalized market events",
    )
    replay = subparsers.add_parser("replay", help="replay a recorded dataset")
    replay.add_argument("--data", type=Path, required=True)
    replay.add_argument(
        "--speed",
        default="max",
        help="max, real-time/normal, or a positive multiplier such as 10x",
    )
    tardis_import = subparsers.add_parser(
        "tardis-import",
        help="download and normalize Binance data through tardis-dev",
    )
    tardis_import.add_argument("--from-date", required=True)
    tardis_import.add_argument("--to-date", required=True)
    tardis_import.add_argument(
        "--filter",
        dest="channels",
        action="append",
        default=None,
        help="Tardis channel filter; repeat for multiple channels",
    )
    tardis_import.add_argument(
        "--api-key",
        default=None,
        help="Tardis API key (defaults to TARDIS_API_KEY, then anonymous)",
    )
    tardis_import.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(".tardis-cache"),
    )
    tardis_import.add_argument("--output", type=Path)
    tardis_import.add_argument("--max-events", type=int)
    hft_export = subparsers.add_parser(
        "hft-export",
        help="export normalized events to HftBacktest NPZ data",
    )
    hft_export.add_argument("--data", type=Path, required=True)
    hft_export.add_argument("--output", type=Path, required=True)
    subparsers.add_parser(
        "hft-health",
        help="verify the installed HftBacktest Python integration",
    )
    hft_synthetic = subparsers.add_parser(
        "hft-synthetic",
        help="generate synthetic HftBacktest depth and trade data",
    )
    hft_synthetic.add_argument(
        "--output-dir",
        type=Path,
        # Under data/, which is already gitignored and excluded from the
        # sdist. The previous default wrote generated NPZs straight into the
        # working tree, where they showed up as untracked files and could be
        # packaged by a build that followed a demo run.
        default=Path("data/synthetic_hft_data"),
    )
    hft_synthetic.add_argument("--duration-seconds", type=int, default=300)
    hft_synthetic.add_argument("--events-per-second", type=int, default=20)
    hft_synthetic.add_argument("--mid-price", type=float, default=60_000)
    hft_synthetic.add_argument("--tick-size", type=float, default=0.1)
    hft_synthetic.add_argument("--lot-size", type=float, default=0.001)
    hft_synthetic.add_argument("--levels", type=int, default=20)
    hft_synthetic.add_argument(
        "--volatility-bps-per-sqrt-second",
        type=float,
        default=0.35,
    )
    hft_synthetic.add_argument(
        "--trade-probability",
        type=float,
        default=0.30,
    )
    hft_synthetic.add_argument("--seed", type=int, default=42)
    hft_latency = subparsers.add_parser(
        "hft-latency",
        help="derive interpolated order latency from feed latency",
    )
    hft_latency.add_argument("--data", type=Path, required=True)
    hft_latency.add_argument("--output", type=Path, required=True)
    hft_latency.add_argument("--entry-multiplier", type=float, default=4)
    hft_latency.add_argument("--response-multiplier", type=float, default=3)
    hft_binance = subparsers.add_parser(
        "hft-convert-binance",
        help="convert a Binance Futures collector gzip feed to HftBacktest NPZ",
    )
    hft_binance.add_argument("--input", type=Path, required=True)
    hft_binance.add_argument("--output", type=Path, required=True)
    hft_binance.add_argument(
        "--regular-stream",
        action="store_true",
        help="input records are regular streams rather than combined streams",
    )
    hft_binance.add_argument(
        "--additional-streams",
        choices=("", "m", "t", "mt"),
        default="",
        help="include mark-price (m), book-ticker (t), or both (mt)",
    )
    hft_binance.add_argument(
        "--buffer-size",
        type=int,
        default=100_000_000,
    )
    hft_tardis = subparsers.add_parser(
        "hft-convert-tardis",
        help="convert Tardis trade/depth CSV files to HftBacktest NPZ",
    )
    hft_tardis.add_argument(
        "--trade",
        type=Path,
        action="append",
        required=True,
        help="trade CSV file; repeat for multiple files",
    )
    hft_tardis.add_argument(
        "--depth",
        type=Path,
        action="append",
        required=True,
        help="incremental-book CSV file; repeat for multiple files",
    )
    hft_tardis.add_argument("--output", type=Path, required=True)
    hft_tardis.add_argument(
        "--snapshot-mode",
        choices=tuple(item.value for item in HftTardisSnapshotMode),
        default=HftTardisSnapshotMode.PROCESS.value,
    )
    hft_tardis.add_argument(
        "--buffer-size",
        type=int,
        default=100_000_000,
    )
    hft_tardis.add_argument(
        "--snapshot-buffer-size",
        type=int,
        default=1_000_000,
    )
    hft_snapshot = subparsers.add_parser(
        "hft-snapshot",
        help="build a last/EOD depth snapshot from one or more NPZ days",
    )
    hft_snapshot.add_argument(
        "--data",
        type=Path,
        action="append",
        required=True,
        help="chronological HftBacktest NPZ file; repeat for multiple days",
    )
    hft_snapshot.add_argument("--tick-size", type=float, required=True)
    hft_snapshot.add_argument("--lot-size", type=float, required=True)
    hft_snapshot.add_argument("--output", type=Path, required=True)
    hft_snapshot.add_argument("--initial-snapshot", type=Path)
    hft_grid = subparsers.add_parser(
        "hft-grid",
        help="run the notebook-compatible HftBacktest grid market maker",
    )
    hft_grid.add_argument(
        "--data",
        type=Path,
        action="append",
        required=True,
        help="chronological data NPZ; repeat for multiple days",
    )
    hft_grid.add_argument("--snapshot", type=Path, required=True)
    hft_grid.add_argument("--tick-size", type=float, default=0.1)
    hft_grid.add_argument("--lot-size", type=float, default=0.001)
    hft_grid.add_argument("--roi-lower-bound", type=float, default=50_000)
    hft_grid.add_argument("--roi-upper-bound", type=float, default=70_000)
    hft_grid.add_argument("--grid-levels", type=int, default=20)
    hft_grid.add_argument("--max-position", type=float, default=5)
    hft_grid.add_argument("--order-quantity", type=float, default=0.1)
    hft_grid.add_argument("--inventory-skew", type=float, default=0)
    hft_grid.add_argument("--grid-interval", type=float)
    hft_grid.add_argument("--half-spread", type=float)
    hft_grid.add_argument("--inventory-skew-price", type=float)
    hft_grid.add_argument("--normalize-inventory", action="store_true")
    hft_grid.add_argument(
        "--latency",
        type=Path,
        action="append",
        default=[],
        help="interpolated latency NPZ; repeat for multiple days",
    )
    hft_grid.add_argument(
        "--queue-model",
        choices=tuple(item.value for item in HftQueueModel),
        default=HftQueueModel.POWER_PROBABILITY.value,
    )
    hft_grid.add_argument("--recorder-output", type=Path)
    hft_grid.add_argument("--statistics-resample")
    hft_grid.add_argument(
        "--no-statistics",
        action="store_true",
        help="skip LinearAssetRecord statistics for short smoke datasets",
    )
    hft_order = subparsers.add_parser(
        "hft-order-demo",
        help="run an HftBacktest order lifecycle scenario",
    )
    hft_order.add_argument("--data", type=Path, required=True)
    hft_order.add_argument("--snapshot", type=Path, required=True)
    hft_order.add_argument(
        "--scenario",
        choices=tuple(item.value for item in HftOrderScenario),
        required=True,
    )
    subparsers.add_parser(
        "libraries",
        help="show active and optional library integrations",
    )
    subparsers.add_parser(
        "datasets",
        help="list locally recorded and imported datasets",
    )
    paper = subparsers.add_parser(
        "paper", help="run the live paper-trading pipeline"
    )
    paper.add_argument(
        "--duration-seconds",
        type=float,
        default=30,
        help="stop paper trading after this many seconds",
    )
    paper.add_argument(
        "--max-events",
        type=int,
        help="stop after this many live market events",
    )
    paper.add_argument(
        "--status-port",
        type=int,
        help="serve /status and /metrics on this port during paper mode",
    )
    paper.add_argument(
        "--kill-after-seconds",
        type=float,
        help="activate the paper-trading kill switch after this duration",
    )
    book = subparsers.add_parser(
        "book",
        help="display the live level-2 order book in the terminal",
    )
    book.add_argument(
        "--symbol",
        help="symbol to display (defaults to the configured symbol)",
    )
    book.add_argument(
        "--depth",
        type=int,
        default=10,
        help="number of price levels to show on each side",
    )
    book.add_argument(
        "--duration-seconds",
        type=float,
        default=300,
        help="stop after this many seconds (default: 5 minutes)",
    )
    book.add_argument(
        "--refresh-ms",
        type=float,
        default=500,
        help="minimum interval between redraws",
    )
    book.add_argument(
        "--json",
        action="store_true",
        help="emit one JSON object per refresh instead of a table",
    )
    subparsers.add_parser(
        "ems-inspect",
        help="inspect guarded EMS venue and dispatch configuration",
    )
    subparsers.add_parser(
        "ems-demo",
        help="preview native SOR and TWAP scheduling without submitting orders",
    )
    opportunity = subparsers.add_parser(
        "opportunity-demo",
        help=(
            "run the two-venue concurrent-leg state machine offline against "
            "simulated venues"
        ),
    )
    opportunity.add_argument(
        "--scenario",
        choices=("filled", "partial", "reject", "all"),
        default="all",
        help="which lifecycle scenario to demonstrate",
    )
    opportunity.add_argument(
        "--store",
        type=Path,
        help="persist the opportunity ledger to this SQLite file",
    )
    controls = subparsers.add_parser(
        "controls-demo",
        help=(
            "demonstrate venue rate limits, the global throttle, stale-feed "
            "handling, the kill switch and atomic capacity reservation"
        ),
    )
    controls.add_argument(
        "--scenario",
        choices=("all", *CONTROL_SCENARIOS),
        default="all",
        help="which execution control to demonstrate",
    )
    configure_binance_parser(subparsers)
    configure_minreq_parser(subparsers)
    return parser


async def run(args: argparse.Namespace) -> None:
    config = _load_config(args.config)
    if args.command in MR_COMMANDS:
        run_minreq_command(args, config)
        return
    if args.command == "record":
        if args.duration_seconds <= 0:
            raise ValueError("--duration-seconds must be positive")
        if args.max_events is not None and args.max_events <= 0:
            raise ValueError("--max-events must be positive")
        await run_record(config, args.duration_seconds, args.max_events)
    elif args.command == "replay":
        await run_replay(config, args.data, args.speed)
    elif args.command == "tardis-import":
        if args.max_events is not None and args.max_events <= 0:
            raise ValueError("--max-events must be positive")
        await run_tardis_import(
            config,
            from_date=args.from_date,
            to_date=args.to_date,
            channels=args.channels or ["depth", "trade"],
            api_key=args.api_key or os.environ.get("TARDIS_API_KEY", ""),
            cache_directory=args.cache_dir,
            output_directory=args.output,
            max_events=args.max_events,
        )
    elif args.command == "hft-export":
        count = export_hftbacktest_npz(args.data, args.output)
        print(json.dumps({"event_count": count, "output": str(args.output)}))
    elif args.command == "hft-health":
        print(json.dumps(hftbacktest_health(), sort_keys=True))
    elif args.command == "hft-synthetic":
        dataset = generate_synthetic_hft_dataset(
            args.output_dir,
            SyntheticHftConfig(
                duration_seconds=args.duration_seconds,
                events_per_second=args.events_per_second,
                mid_price=args.mid_price,
                tick_size=args.tick_size,
                lot_size=args.lot_size,
                levels=args.levels,
                volatility_bps_per_sqrt_second=(
                    args.volatility_bps_per_sqrt_second
                ),
                trade_probability=args.trade_probability,
                seed=args.seed,
            ),
        )
        print(
            json.dumps(
                {
                    "data_path": str(dataset.data_path),
                    "initial_snapshot_path": str(
                        dataset.initial_snapshot_path
                    ),
                    "preview_path": str(dataset.preview_path),
                    "event_count": dataset.event_count,
                    "depth_event_count": dataset.depth_event_count,
                    "trade_event_count": dataset.trade_event_count,
                    "minimum_latency_ns": dataset.minimum_latency_ns,
                    "mean_latency_ns": dataset.mean_latency_ns,
                    "maximum_latency_ns": dataset.maximum_latency_ns,
                },
                indent=2,
                sort_keys=True,
            )
        )
    elif args.command == "hft-latency":
        latency = generate_feed_order_latency(
            args.data,
            args.output,
            entry_multiplier=args.entry_multiplier,
            response_multiplier=args.response_multiplier,
        )
        print(
            json.dumps(
                {"output": str(args.output), "sample_count": len(latency)}
            )
        )
    elif args.command == "hft-convert-binance":
        data = convert_binance_futures_raw(
            args.input,
            output_path=args.output,
            combined_stream=not args.regular_stream,
            additional_streams=args.additional_streams,
            buffer_size=args.buffer_size,
        )
        print(json.dumps({"output": str(args.output), "event_count": len(data)}))
    elif args.command == "hft-convert-tardis":
        data = convert_tardis_csv(
            args.trade,
            args.depth,
            output_path=args.output,
            buffer_size=args.buffer_size,
            snapshot_buffer_size=args.snapshot_buffer_size,
            snapshot_mode=HftTardisSnapshotMode(args.snapshot_mode),
        )
        print(json.dumps({"output": str(args.output), "event_count": len(data)}))
    elif args.command == "hft-snapshot":
        snapshot = create_hft_last_snapshot(
            args.data,
            tick_size=args.tick_size,
            lot_size=args.lot_size,
            output_path=args.output,
            initial_snapshot_path=args.initial_snapshot,
        )
        print(
            json.dumps(
                {"output": str(args.output), "level_count": len(snapshot)}
            )
        )
    elif args.command == "hft-grid":
        result = run_grid_market_making_backtest(
            args.data,
            args.snapshot,
            asset_config=HftBacktestAssetConfig(
                tick_size=args.tick_size,
                lot_size=args.lot_size,
                latency_model=(
                    HftLatencyModel.INTERPOLATED
                    if args.latency
                    else HftLatencyModel.CONSTANT
                ),
                order_latency_paths=tuple(args.latency),
                queue_model=HftQueueModel(args.queue_model),
                roi_lower_bound=args.roi_lower_bound,
                roi_upper_bound=args.roi_upper_bound,
            ),
            strategy_config=GridMarketMakingConfig(
                grid_levels=args.grid_levels,
                max_position=args.max_position,
                order_quantity=args.order_quantity,
                inventory_skew=args.inventory_skew,
                grid_interval=args.grid_interval,
                half_spread=args.half_spread,
                inventory_skew_price=args.inventory_skew_price,
                normalize_inventory=args.normalize_inventory,
                recorder_output_path=args.recorder_output,
                statistics_resample=args.statistics_resample,
                generate_statistics=not args.no_statistics,
            ),
        )
        print(
            json.dumps(
                result.to_dict(),
                indent=2,
                default=str,
                allow_nan=False,
            )
        )
    elif args.command == "hft-order-demo":
        result = run_hft_order_lifecycle(
            args.data,
            args.snapshot,
            HftOrderScenario(args.scenario),
        )
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    elif args.command == "libraries":
        print(
            json.dumps(
                [
                    integration.to_dict()
                    for integration in library_integrations()
                ],
                indent=2,
                sort_keys=True,
            )
        )
    elif args.command == "datasets":
        print(
            json.dumps(
                list_datasets(config.storage.data_directory),
                indent=2,
                sort_keys=True,
            )
        )
    elif args.command == "paper":
        if args.duration_seconds <= 0:
            raise ValueError("--duration-seconds must be positive")
        if args.max_events is not None and args.max_events <= 0:
            raise ValueError("--max-events must be positive")
        if args.kill_after_seconds is not None and args.kill_after_seconds <= 0:
            raise ValueError("--kill-after-seconds must be positive")
        await run_paper(
            config,
            args.duration_seconds,
            args.max_events,
            args.status_port,
            args.kill_after_seconds,
        )
    elif args.command == "book":
        if args.duration_seconds <= 0:
            raise ValueError("--duration-seconds must be positive")
        if args.depth <= 0:
            raise ValueError("--depth must be positive")
        if args.refresh_ms <= 0:
            raise ValueError("--refresh-ms must be positive")
        await run_book_display(
            config,
            symbol=args.symbol,
            depth=args.depth,
            duration_seconds=args.duration_seconds,
            refresh_seconds=args.refresh_ms / 1000.0,
            as_json=args.json,
        )
    elif args.command == "binance-testnet":
        await run_binance_command(args, config)
    elif args.command == "ems-inspect":
        print(json.dumps(_ems_inspection(config), indent=2, sort_keys=True))
    elif args.command == "ems-demo":
        print(
            json.dumps(
                await _ems_demo(config),
                indent=2,
                sort_keys=True,
            )
        )
    elif args.command == "opportunity-demo":
        print(
            json.dumps(
                await run_opportunity_demo(
                    scenario=args.scenario,
                    store_path=args.store,
                ),
                indent=2,
                sort_keys=True,
            )
        )
    elif args.command == "controls-demo":
        print(
            json.dumps(
                run_controls_demo(scenario=args.scenario),
                indent=2,
                sort_keys=True,
            )
        )
    else:
        raise ValueError(f"unsupported command: {args.command}")


def _load_config(path: Path) -> AppConfig:
    if path.is_file():
        return AppConfig.from_yaml(path)
    if path == Path("config.yaml"):
        return AppConfig()
    raise FileNotFoundError(f"configuration file does not exist: {path}")


def _ems_inspection(config: AppConfig) -> dict[str, object]:
    return {
        "dispatch_policy": config.ems.dispatch_policy,
        "enabled_venues": list(config.ems.enabled_venues),
        "latency_penalty_bps_per_ms": str(
            config.ems.latency_penalty_bps_per_ms
        ),
        "max_reserved_notional": str(config.ems.max_reserved_notional),
        "live_trading_enabled": False,
        "schedule_lifecycle": {
            "states": [state.value for state in ScheduleState],
            "reconciliation": [
                "reconcile_schedule",
                "cancel_schedule",
                "complete_schedule_recovery",
            ],
        },
        "plan_recovery": [
            "reconcile_plan",
            "complete_plan_recovery",
            "resolve_plan_recovery",
        ],
        "instruments": {
            canonical: {
                "compatibility_group": instrument.compatibility_group,
                "canonical_quantity_unit": (
                    instrument.canonical_quantity_unit
                ),
                "venues": {
                    venue: {
                        "symbol": mapping.symbol,
                        "quantity_unit": mapping.quantity_unit,
                        "contract_size_quote": (
                            str(mapping.contract_size_quote)
                            if mapping.contract_size_quote is not None
                            else None
                        ),
                    }
                    for venue, mapping in instrument.venues.items()
                },
            }
            for canonical, instrument in config.ems.instruments.items()
        },
        "venues": {
            "binance": {
                "environment": config.binance_execution.environment,
                "rest_base_url": config.binance_execution.rest_base_url,
            },
            "okx": {
                "environment": "demo",
                "rest_base_url": config.okx_demo_execution.rest_base_url,
            },
            "deribit": {
                "environment": "test",
                "rest_base_url": config.deribit_test_execution.rest_base_url,
            },
        },
    }


async def _ems_demo(
    config: AppConfig,
    adapters: dict[str, ExchangeAdapter] | None = None,
) -> dict[str, object]:
    if adapters is None:
        adapters = build_guarded_adapters(config, load_credentials=False)
    coordinator = ExecutionCoordinator(
        adapters,
        max_reserved_notional=config.ems.max_reserved_notional,
        latency_penalty_bps_per_ms=(
            config.ems.latency_penalty_bps_per_ms
        ),
        dispatch_policy=DispatchPolicy(config.ems.dispatch_policy),
        instrument_mappings=instrument_mappings_from_config(config),
    )
    try:
        quotes = {
            "binance": ("99990", "100000", "1.0", "1.0", "1.0", "800"),
            "okx": ("99985", "100005", "1.0", "1.0", "0.8", "500"),
            "deribit": ("99980", "100010", "1.0", "1.0", "0.5", "300"),
        }
        instrument = config.ems.instruments.get(config.symbol.upper())
        if instrument is None:
            raise ValueError(
                f"EMS demo symbol has no canonical mapping: {config.symbol}"
            )
        demo_venues = [
            venue
            for venue in instrument.venues
            if venue in config.ems.enabled_venues
        ]
        if not demo_venues:
            raise ValueError(
                f"no enabled compatible demo venue for {config.symbol}"
            )
        for venue in demo_venues:
            bid, ask, bid_size, ask_size, fee_bps, latency_us = quotes[venue]
            await coordinator.update_quote(
                VenueQuote(
                    venue=venue,
                    symbol=config.symbol,
                    bid=Decimal(bid),
                    ask=Decimal(ask),
                    bid_size=Decimal(bid_size),
                    ask_size=Decimal(ask_size),
                    fee_bps=Decimal(fee_bps),
                    latency_us=Decimal(latency_us),
                )
            )
        route = await coordinator.preview_route(
            config.symbol,
            Side.BUY,
            Decimal("0.5"),
        )
        schedule = await coordinator.schedule_twap(
            "cli-demo",
            ExecutionLeg(
                symbol=config.symbol,
                side=Side.BUY,
                quantity=Decimal("0.5"),
                reference_price=Decimal("100000"),
            ),
            start_ns=1_000_000_000,
            end_ns=4_000_000_000,
            slice_count=4,
        )
        return {
            "mode": "inspection_only",
            "orders_submitted": 0,
            "route": [
                {key: str(value) for key, value in item.items()} for item in route
            ],
            "twap": [
                {"timestamp_ns": timestamp_ns, "quantity": str(quantity)}
                for timestamp_ns, quantity in schedule.slices
            ],
            "twap_state": coordinator.schedule_result("cli-demo").state.value,
        }
    finally:
        await coordinator.close()


_DEMO_SCENARIOS: dict[str, dict[str, VenueScript]] = {
    "filled": {
        "binance": VenueScript(
            fills=(FillStep(Decimal("1"), Decimal("100000")),),
        ),
        "okx": VenueScript(
            fills=(FillStep(Decimal("1"), Decimal("100040")),),
        ),
    },
    "partial": {
        "binance": VenueScript(
            fills=(FillStep(Decimal("0.4"), Decimal("100000")),),
        ),
        "okx": VenueScript(
            fills=(FillStep(Decimal("1"), Decimal("100040")),),
        ),
    },
    "reject": {
        "binance": VenueScript(reject_message="Filter failure: MIN_NOTIONAL"),
        "okx": VenueScript(
            fills=(FillStep(Decimal("1"), Decimal("100040")),),
        ),
    },
}


async def run_opportunity_demo(
    *,
    scenario: str = "all",
    store_path: Path | None = None,
) -> dict[str, object]:
    """Drive the two-venue state machine against offline simulated venues.

    No network, credentials, or native extensions are involved, so the
    demo behaves identically on Windows, macOS, and Linux.
    """

    names = (
        tuple(_DEMO_SCENARIOS)
        if scenario == "all"
        else (scenario,)
    )
    runs: list[dict[str, object]] = []
    for name in names:
        runs.append(
            await _run_opportunity_scenario(
                name,
                store_path=store_path,
            )
        )
    return {
        "mode": "simulated_venues",
        "scenarios": runs,
    }


async def _run_opportunity_scenario(
    name: str,
    *,
    store_path: Path | None,
) -> dict[str, object]:
    scripts = _DEMO_SCENARIOS[name]
    adapters: dict[str, ExchangeAdapter] = {
        venue: SimulatedVenue(venue, script=script)
        for venue, script in scripts.items()
    }
    path = (
        ":memory:"
        if store_path is None
        else str(store_path.with_name(f"{store_path.stem}-{name}.db"))
    )
    with OpportunityStore(path) as store:
        coordinator = OpportunityCoordinator(
            adapters,
            store,
            timeouts=OpportunityTimeouts(
                ack_timeout_ms=250,
                first_fill_timeout_ms=500,
                complete_fill_timeout_ms=1500,
            ),
            residual_policy=ResidualPolicy(
                max_residual_ratio=Decimal("0.05"),
                max_residual_duration_ms=1000,
                max_hedge_delay_ms=500,
                unwind_venue_priority=("okx", "binance"),
            ),
        )
        for venue in adapters:
            coordinator.update_quote(
                MarketQuote(
                    venue=venue,
                    symbol="BTCUSDT",
                    bid_price=Decimal("99990"),
                    ask_price=Decimal("100010"),
                    bid_quantity=Decimal("5"),
                    ask_quantity=Decimal("5"),
                    fee_bps=Decimal("1"),
                )
            )
        request = OpportunityRequest(
            strategy_id="two-venue-basis",
            strategy_version="v1.0.0",
            signal_id=f"demo-{name}",
            canonical_symbol="BTCUSDT",
            legs=(
                LegRequest(
                    venue="binance",
                    symbol="BTCUSDT",
                    side=Side.BUY,
                    quantity=Decimal("1"),
                    reference_price=Decimal("100000"),
                    order_type=OrderType.LIMIT,
                    limit_price=Decimal("100010"),
                ),
                LegRequest(
                    venue="okx",
                    symbol="BTCUSDT",
                    side=Side.SELL,
                    quantity=Decimal("1"),
                    reference_price=Decimal("100040"),
                    order_type=OrderType.LIMIT,
                    limit_price=Decimal("100030"),
                ),
            ),
            expected_edge=Decimal("40"),
        )
        result = await coordinator.execute(request)
        # Replaying an identical signal must be a no-op, not a new order.
        replay = await coordinator.execute(request)
        payload = result.to_dict()
        payload["scenario"] = name
        payload["replay_is_idempotent"] = (
            replay.opportunity_id == result.opportunity_id
            and replay.state is result.state
        )
        payload["event_log"] = [
            event.event_type
            for event in store.load_events(result.opportunity_id)
        ]
        payload["venue_positions"] = {
            venue: {
                symbol: format(quantity, "f")
                for symbol, quantity in adapter.positions().items()
            }
            for venue, adapter in adapters.items()
            if isinstance(adapter, SimulatedVenue)
        }
        return payload


def main(argv: Sequence[str] | None = None) -> None:
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        runtime.run(run(args))
    except (FileNotFoundError, ValueError, RuntimeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
