"""Command-line surface for the minimum-requirements platform layer.

Every requirement in the document has to be demonstrable by an operator, not
just by a test, so each stage of the pipeline is reachable from its own
``mr-*`` subcommand and every command prints a JSON document.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_platform_poc.config import AppConfig
from trading_platform_poc.minreq import DOCUMENT_VERSION, LAYER_VERSION
from trading_platform_poc.minreq.acceptance import run_acceptance, write_deliverables
from trading_platform_poc.minreq.backtest import (
    BacktestConfig,
    build_dataset,
    run_backtest,
)
from trading_platform_poc.minreq.benchmark import run_benchmark
from trading_platform_poc.minreq.canonical import Liquidity, schema_document
from trading_platform_poc.minreq.features import FeatureConfig, feature_catalogue
from trading_platform_poc.minreq.fees import default_fee_book
from trading_platform_poc.minreq.ingest import record_live
from trading_platform_poc.minreq.latency import latency_preset_names
from trading_platform_poc.minreq.ledger import EventLedger
from trading_platform_poc.minreq.matching import ExecutionMode, MatchingConfig
from trading_platform_poc.minreq.monitoring import monitoring_document
from trading_platform_poc.minreq.pipeline import (
    list_datasets,
    load_manifest,
    verify_manifest,
)
from trading_platform_poc.minreq.riskcontrols import RiskLimits, risk_limit_matrix
from trading_platform_poc.minreq.router import ORDER_STATE_MACHINE_DOC
from trading_platform_poc.minreq.securitymaster import default_security_master
from trading_platform_poc.minreq.statarb import StatArbConfig
from trading_platform_poc.minreq.synthetic import FaultInjection, SyntheticConfig
from trading_platform_poc.minreq.venues import venue_capability_matrix

MR_COMMANDS = (
    "mr-instruments",
    "mr-fees",
    "mr-schema",
    "mr-generate",
    "mr-record",
    "mr-datasets",
    "mr-verify",
    "mr-quality",
    "mr-backtest",
    "mr-paper",
    "mr-reconcile",
    "mr-tca",
    "mr-benchmark",
    "mr-acceptance",
    "mr-deliverables",
)


def _emit(document: Any) -> None:
    print(json.dumps(document, indent=2, sort_keys=True, default=str))


def _synthetic_from(args: argparse.Namespace, config: AppConfig) -> SyntheticConfig:
    return SyntheticConfig(
        seed=args.seed if args.seed is not None else config.minreq.seed,
        steps=args.steps if args.steps is not None else config.minreq.steps,
        venues=tuple(args.venues) if args.venues else tuple(config.minreq.venues),
        faults=FaultInjection(
            sequence_gap_at=getattr(args, "sequence_gap_at", None),
            stale_gap_at=getattr(args, "stale_gap_at", None),
            crossed_book_at=getattr(args, "crossed_book_at", None),
            duplicate_at=getattr(args, "duplicate_at", None),
        ),
    )


def resolve_execution_mode(run_mode: str, configured: str) -> ExecutionMode:
    """Map the command and ``minreq.execution_mode`` onto one execution mode.

    Backtests always simulate. Paper runs simulate as ``internal_paper`` unless
    ``venue_testnet`` is configured, in which case a venue adapter is required
    at startup and every fill carries the testnet label.
    """

    if run_mode == "backtest":
        if configured == ExecutionMode.VENUE_TESTNET.value:
            raise ValueError(
                "execution_mode venue_testnet applies to mr-paper only; "
                "a backtest cannot route to a venue"
            )
        return ExecutionMode.INTERNAL_SIMULATION
    if configured == ExecutionMode.VENUE_TESTNET.value:
        return ExecutionMode.VENUE_TESTNET
    return ExecutionMode.INTERNAL_PAPER


def _backtest_config(
    args: argparse.Namespace, config: AppConfig, *, mode: str
) -> BacktestConfig:
    settings = config.minreq
    venues = tuple(args.venues) if args.venues else tuple(settings.venues)
    base = settings.economic_group.split(".")[0].replace("/", "-")
    product = settings.economic_group.split(".")[-1]
    venue_instruments = {
        venue: f"{base}.{venue.upper()}.{product}" for venue in venues
    }
    quantity = (
        Decimal(args.quantity) if args.quantity else settings.target_quantity
    )
    return BacktestConfig(
        run_id=args.run_id,
        economic_group=settings.economic_group,
        mode=mode,
        seed=args.seed if args.seed is not None else settings.seed,
        venue_instruments=venue_instruments,
        feature=FeatureConfig(
            economic_group=settings.economic_group, min_samples=args.min_samples
        ),
        strategy=StatArbConfig(
            economic_group=settings.economic_group,
            entry_z=Decimal(args.entry_z) if args.entry_z else settings.entry_z,
            exit_z=Decimal(args.exit_z) if args.exit_z else settings.exit_z,
            target_quantity=quantity,
            min_samples=args.min_samples,
            cooldown_ns=args.cooldown_ns,
        ),
        risk=RiskLimits(
            max_gross_notional=settings.max_gross_notional,
            max_order_notional=settings.max_order_notional,
        ),
        matching=MatchingConfig(
            mode=resolve_execution_mode(
                mode, getattr(args, "execution_mode", None) or settings.execution_mode
            )
        ),
        latency_preset=getattr(args, "latency_preset", None) or settings.latency_preset,
    )


def configure_minreq_parser(subparsers: Any) -> None:
    """Register the ``mr-*`` commands on an existing subparser group."""

    def dataset_argument(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--dataset",
            type=Path,
            required=True,
            help="dataset root produced by mr-generate",
        )

    def workload_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--seed", type=int, help="deterministic generator seed")
        parser.add_argument("--steps", type=int, help="number of book updates")
        parser.add_argument(
            "--venues",
            nargs="+",
            help="venues to capture, at least two",
        )

    def strategy_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--entry-z", help="entry threshold in sigmas")
        parser.add_argument("--exit-z", help="exit threshold in sigmas")
        parser.add_argument("--quantity", help="target base quantity per leg")
        parser.add_argument("--min-samples", type=int, default=10)
        parser.add_argument("--cooldown-ns", type=int, default=0)

    subparsers.add_parser(
        "mr-instruments",
        help="print the effective-dated security master (MR-03)",
    ).add_argument(
        "--as-of-ns",
        type=int,
        default=0,
        help="resolve the master at this epoch nanosecond timestamp",
    )

    fees = subparsers.add_parser(
        "mr-fees", help="print the effective-dated fee schedule (MR-09)"
    )
    fees.add_argument("--as-of-ns", type=int, default=0)
    fees.add_argument("--venue", default="binance")

    subparsers.add_parser(
        "mr-schema",
        help="print canonical schemas, versions and the venue capability matrix",
    )

    generate = subparsers.add_parser(
        "mr-generate",
        help="capture and normalize a deterministic multi-venue dataset (MR-01/02/04)",
    )
    generate.add_argument("--output", type=Path, required=True)
    workload_arguments(generate)
    generate.add_argument("--sequence-gap-at", type=int)
    generate.add_argument("--stale-gap-at", type=int)
    generate.add_argument("--crossed-book-at", type=int)
    generate.add_argument("--duplicate-at", type=int)

    record = subparsers.add_parser(
        "mr-record",
        help="capture live public feeds into a dataset with raw, canonical, "
        "quality and manifest (MR-01/MR-04)",
    )
    record.add_argument("--output", type=Path, required=True)
    record.add_argument("--venues", nargs="+", help="venues to capture")
    record.add_argument("--symbol", help="canonical symbol, for example BTCUSDT")
    record.add_argument("--duration", type=float, help="seconds to capture")
    record.add_argument("--max-records", type=int, help="stop after N raw records")

    datasets = subparsers.add_parser(
        "mr-datasets", help="list captured datasets and their approval state"
    )
    datasets.add_argument("--root", type=Path, required=True)

    verify = subparsers.add_parser(
        "mr-verify", help="re-hash a dataset and check the manifest (MR-04)"
    )
    dataset_argument(verify)

    quality = subparsers.add_parser(
        "mr-quality", help="print the data-quality report for a dataset (MR-04)"
    )
    dataset_argument(quality)

    backtest = subparsers.add_parser(
        "mr-backtest",
        help="run the deterministic stat-arb backtest end to end (MR-05..MR-13)",
    )
    backtest.add_argument("--output", type=Path, required=True)
    backtest.add_argument("--dataset", type=Path)
    backtest.add_argument("--run-id", default="mr-backtest")
    backtest.add_argument(
        "--latency-preset",
        choices=latency_preset_names(),
        help="new_backtest co-location latency preset",
    )
    workload_arguments(backtest)
    strategy_arguments(backtest)

    paper = subparsers.add_parser(
        "mr-paper",
        help="run the same stack in paper mode on synthetic or live public feeds, "
        "with internal or venue-testnet execution (MR-07)",
    )
    paper.add_argument("--output", type=Path, required=True)
    paper.add_argument("--dataset", type=Path)
    paper.add_argument("--run-id", default="mr-paper")
    paper.add_argument(
        "--feed",
        choices=("synthetic", "live"),
        help="market data source (default: minreq.feed)",
    )
    paper.add_argument("--symbol", help="canonical symbol for live feeds")
    paper.add_argument("--duration", type=float, help="seconds of live trading")
    paper.add_argument("--max-records", type=int, help="stop after N live raw records")
    paper.add_argument(
        "--record",
        type=Path,
        help="persist the live raw frames as a replayable dataset",
    )
    paper.add_argument(
        "--execution-mode",
        choices=("internal_paper", "venue_testnet"),
        help="internal paper matcher or real venue testnet (default: minreq.execution_mode)",
    )
    paper.add_argument(
        "--latency-preset",
        choices=latency_preset_names(),
        help="new_backtest co-location latency preset",
    )
    workload_arguments(paper)
    strategy_arguments(paper)

    reconcile = subparsers.add_parser(
        "mr-reconcile", help="rebuild state from the ledger and reconcile (MR-11)"
    )
    reconcile.add_argument("--ledger", type=Path, required=True)
    reconcile.add_argument("--run-id", required=True)

    tca = subparsers.add_parser(
        "mr-tca", help="print the TCA report written by a run (MR-12)"
    )
    tca.add_argument("--run-dir", type=Path, required=True)

    benchmark = subparsers.add_parser(
        "mr-benchmark", help="measure throughput, overhead and memory (MR-14)"
    )
    benchmark.add_argument("--output", type=Path, required=True)
    benchmark.add_argument("--steps", type=int, default=600)
    benchmark.add_argument("--seed", type=int, default=20260817)

    acceptance = subparsers.add_parser(
        "mr-acceptance", help="run acceptance tests AT-01 to AT-14 (section 9)"
    )
    acceptance.add_argument("--output", type=Path, required=True)
    acceptance.add_argument(
        "--only",
        nargs="+",
        help="run a subset, for example AT-06 AT-09",
    )

    deliverables = subparsers.add_parser(
        "mr-deliverables", help="write the section 10 data deliverables"
    )
    deliverables.add_argument("--output", type=Path, required=True)


def run_minreq_command(args: argparse.Namespace, config: AppConfig) -> None:
    """Execute one ``mr-*`` command. Raises ValueError on bad input."""

    command = args.command
    if command == "mr-instruments":
        master = default_security_master()
        _emit(
            {
                "as_of_ns": args.as_of_ns,
                "economic_groups": list(master.economic_groups),
                "instruments": [
                    record.to_dict()
                    for record in master.snapshot(args.as_of_ns)
                ],
            }
        )
        return

    if command == "mr-fees":
        book = default_fee_book()
        master = default_security_master()
        resolved = []
        for record in master.snapshot(args.as_of_ns):
            if args.venue not in {"*", record.venue}:
                continue
            for liquidity in (Liquidity.MAKER, Liquidity.TAKER):
                fee = book.resolve(record, liquidity, args.as_of_ns)
                resolved.append(
                    {
                        "instrument_id": record.instrument_id,
                        "liquidity": liquidity.value,
                        **fee.to_dict(),
                    }
                )
        _emit(
            {
                "fee_book_version": book.version,
                "as_of_ns": args.as_of_ns,
                "resolved": resolved,
                "schedule": book.to_documents(),
            }
        )
        return

    if command == "mr-schema":
        _emit(
            {
                "document_version": DOCUMENT_VERSION,
                "layer_version": LAYER_VERSION,
                "canonical_schema": schema_document(),
                "venue_capability_matrix": venue_capability_matrix(),
                "feature_catalogue": feature_catalogue(),
                "risk_limit_matrix": risk_limit_matrix(),
                "order_state_machine": ORDER_STATE_MACHINE_DOC,
                "monitoring": monitoring_document(),
            }
        )
        return

    if command == "mr-generate":
        synthetic = _synthetic_from(args, config)
        if len(synthetic.venues) < 2:
            raise ValueError("at least two venues are required")
        _, manifest = build_dataset(args.output, synthetic=synthetic)
        _emit(
            {
                "dataset": str(args.output),
                "approved": manifest["approved"],
                "checksum": manifest["checksum"],
                "venues": manifest["venues"],
                "instruments": manifest["instruments"],
                "raw_records": manifest["raw_records"],
                "canonical_events": manifest["canonical_events"],
                "quality_failures": manifest["quality"]["failures"],
            }
        )
        return

    if command == "mr-record":
        venues = tuple(args.venues) if args.venues else tuple(config.minreq.venues)
        outcome = asyncio.run(
            record_live(
                args.output,
                venues=venues,
                symbol=args.symbol or config.minreq.live_symbol,
                duration_seconds=(
                    args.duration
                    if args.duration is not None
                    else config.minreq.live_duration_seconds
                ),
                max_records=args.max_records,
            )
        )
        manifest = outcome["manifest"]
        _emit(
            {
                "dataset": str(args.output),
                "approved": manifest.get("approved"),
                "checksum": manifest.get("checksum"),
                "venues": manifest.get("venues"),
                "raw_records": manifest.get("raw_records"),
                "canonical_events": manifest.get("canonical_events"),
                "quality_failures": manifest.get("quality", {}).get("failures"),
                "quality_codes": manifest.get("quality", {}).get("by_code"),
                "capture": outcome["capture"],
            }
        )
        return

    if command == "mr-datasets":
        _emit(list_datasets(args.root))
        return

    if command == "mr-verify":
        _emit(verify_manifest(args.dataset))
        return

    if command == "mr-quality":
        manifest = load_manifest(args.dataset)
        _emit(
            {
                "dataset_id": manifest["dataset_id"],
                "approved": manifest["approved"],
                "quality": manifest["quality"],
            }
        )
        return

    if command in {"mr-backtest", "mr-paper"}:
        mode = "backtest" if command == "mr-backtest" else "paper"
        backtest_config = _backtest_config(args, config, mode=mode)
        feed = (getattr(args, "feed", None) or config.minreq.feed) if mode == "paper" else "synthetic"
        testnet = backtest_config.matching.mode is ExecutionMode.VENUE_TESTNET
        if feed == "live":
            from trading_platform_poc.minreq.live import run_live_paper

            matcher_factory = None
            if testnet:
                from trading_platform_poc.minreq.testnet import (
                    build_testnet_gateways,
                    venue_testnet_matcher_factory,
                )

                venues = tuple(backtest_config.venue_instruments)
                matcher_factory = venue_testnet_matcher_factory(
                    build_testnet_gateways(config, venues),
                    venues,
                    security_master=default_security_master(),
                )
            result = asyncio.run(
                run_live_paper(
                    backtest_config,
                    args.output,
                    symbol=args.symbol or config.minreq.live_symbol,
                    duration_seconds=(
                        args.duration
                        if args.duration is not None
                        else config.minreq.live_duration_seconds
                    ),
                    max_records=args.max_records,
                    record_dataset=args.record,
                    matcher_factory=matcher_factory,
                )
            )
        else:
            if testnet:
                raise ValueError(
                    "venue_testnet routes real orders and needs live market data; "
                    "run mr-paper --feed live"
                )
            result = run_backtest(
                backtest_config,
                args.output,
                dataset_root=args.dataset,
                synthetic=_synthetic_from(args, config),
            )
        _emit(
            {
                "run_id": backtest_config.run_id,
                "mode": mode,
                "feed": feed,
                "execution_mode": backtest_config.matching.mode.value,
                "latency_preset": backtest_config.latency_preset,
                "output": str(result.output_dir),
                "manifest_identity": result.manifest.identity(),
                "result_hash": result.manifest.result_hash,
                "state_hash": result.state_hash,
                "tca_totals": result.tca.totals,
                "router": result.router_summary,
                "reconciliation": result.reconciliation,
                "monitoring_status": result.monitoring["worst_status"],
                "benchmark": result.benchmark,
            }
        )
        return

    if command == "mr-reconcile":
        if not Path(args.ledger).is_file():
            raise FileNotFoundError(f"no ledger at {args.ledger}")
        ledger = EventLedger(args.ledger, run_id=args.run_id)
        try:
            derived = ledger.derive(run_id=args.run_id)
            _emit(
                {
                    "run_id": args.run_id,
                    "ledger": str(args.ledger),
                    "chain_digest": ledger.chain_digest(run_id=args.run_id),
                    "state_hash": derived.state_hash(),
                    "events": ledger.count(run_id=args.run_id),
                    "derived": derived.to_dict(),
                }
            )
        finally:
            ledger.close()
        return

    if command == "mr-tca":
        report = Path(args.run_dir) / "tca.json"
        if not report.is_file():
            raise FileNotFoundError(f"no TCA report at {report}")
        _emit(json.loads(report.read_text(encoding="utf-8")))
        return

    if command == "mr-benchmark":
        report = run_benchmark(args.output, steps=args.steps, seed=args.seed)
        _emit(report.to_dict())
        return

    if command == "mr-acceptance":
        report = run_acceptance(args.output, only=args.only)
        _emit(report.summary())
        return

    if command == "mr-deliverables":
        _emit(write_deliverables(args.output))
        return

    raise ValueError(f"unknown command: {command}")
