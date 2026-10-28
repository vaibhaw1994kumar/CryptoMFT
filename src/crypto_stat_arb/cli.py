from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
import signal
import sys
import time

from .runner import enforce_data_quality, run_backtest
from .benchmark import run_benchmark
from .calibration import calibrate_latency, compare_runs
from .economics_gate import economics_gate
from .config import load_config
from .instrument_status import InstrumentStatusPoller
from .paper_live import LivePaperSession, perpetual_feed_settings
from .maker_sim import maker_simulator_from_config, simulate_recorded_session
from .session_audit import audit_session
from .cycle_report import build_cycle_report
from .cycle_reports import build_cycle_reports
from .session import ExecutionMode
from .pipeline import normalize_raw_file
from .contract_master import PublicHttpClient, dump_contract_master
from .raw_capture import capture_envelopes, verify_raw_file
from .orchestration import PartitionSpec, PipelineOrchestrator
from .alignment import align_canonical_files
from .live_feed import (
    LiveFeedSettings,
    LiveOrderBookService,
    default_live_session_id,
    verify_wire_file,
)
from .structured_logging import StructuredLogStore
from .sdk import AdvancedBacktestSpec, BacktestAPI, RuntimeMode
from .release import (
    ArtifactInput, ArtifactKind, PaperReleaseRegistry, PaperReleaseSpec,
)


def _live_settings(args: argparse.Namespace) -> LiveFeedSettings:
    return LiveFeedSettings(
        venues=tuple(args.venue or ("BINANCE", "BYBIT")),
        binance_market=args.binance_market,
        bybit_market=args.bybit_market,
        binance_ws_base=args.binance_ws_base,
        binance_rest_base=args.binance_rest_base,
        bybit_ws_url=args.bybit_ws_url,
        bybit_depth=args.bybit_depth,
        http_timeout_seconds=args.http_timeout_seconds,
        initial_sync_timeout_seconds=args.initial_sync_timeout_seconds,
        reconnect_min_seconds=args.reconnect_min_seconds,
        reconnect_max_seconds=args.reconnect_max_seconds,
        rotate_seconds=args.rotate_seconds,
        rotate_bytes=args.rotate_bytes,
        durable=not args.no_fsync,
        max_runtime_seconds=args.max_runtime_seconds,
        max_events=args.max_events,
        ohlc_interval_seconds=60,
        trade_streams_enabled=not args.no_trades,
        trade_stale_seconds=args.trade_stale_seconds,
        udp_enabled=not args.no_udp,
        udp_group=args.udp_group,
        udp_port=args.udp_port,
        udp_interface=args.udp_interface,
        udp_ttl=args.udp_ttl,
        udp_depth=args.udp_depth,
        udp_max_datagram_bytes=args.udp_max_datagram_bytes,
        monitor_interval_seconds=args.monitor_interval_seconds,
        rest_reconcile_seconds=args.rest_reconcile_seconds,
    )


async def _run_with_signals(service, runner) -> dict:
    loop = asyncio.get_running_loop()
    registered = []
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, service.request_stop)
            registered.append(sig)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        return await runner()
    finally:
        for sig in registered:
            loop.remove_signal_handler(sig)


def _persistence_failure(result: dict) -> str:
    """The recording failure a live run stopped on ("" if none): it must not exit 0."""
    feed = result.get("live_feed", result)
    return (feed.get("persistence_failure")
            or (feed.get("service") or {}).get("persistence_failure") or "")


def _print_live_result(result: dict) -> None:
    print(json.dumps(result, indent=2, default=str))
    failure = _persistence_failure(result)
    if failure:
        print(f"recording failed; the session stopped fail-closed: {failure}", file=sys.stderr)
        raise SystemExit(2)


async def _run_live_paper(args: argparse.Namespace) -> dict:
    if args.execution_mode == "venue_testnet":
        raise SystemExit(
            "VENUE_TESTNET needs a CredentialBroker backed by your secret manager: "
            "build adapters with crypto_stat_arb.testnet.build_testnet_adapters and "
            "run LivePaperSession(..., execution_mode=ExecutionMode.VENUE_TESTNET) "
            "from Python. Credentials are never accepted on the command line."
        )
    settings = _live_settings(args)
    paper = LivePaperSession(
        args.config, args.output_dir,
        args.session_id or default_live_session_id(), settings,
        execution_mode=ExecutionMode.INTERNAL_PAPER,
        status_poll_seconds=max(args.status_poll_seconds, 1.0),
        flatten_lead_seconds=args.flatten_lead_seconds,
    )
    if args.maker_dry_run:
        paper.cfg.strategies.setdefault("maker_simulation", {})["enabled"] = True
        paper.maker_simulator = maker_simulator_from_config(paper.cfg)
    # Each instrument is polled on its own product's endpoint (spot or linear perpetual).
    if args.status_poll_seconds > 0:
        paper.status_poller = InstrumentStatusPoller(
            paper.cfg.instruments,
            {
                venue: (
                    paper.cfg.instrument_id,
                    paper.cfg.instruments.get(venue, paper.cfg.instrument_id).symbol,
                )
                for venue in settings.venues
            },
            apply_rule_changes=not args.status_only,
        )
    return await _run_with_signals(paper.service, paper.run)


async def _run_live_orderbook(args: argparse.Namespace) -> dict:
    # A perpetual config also records mark/index/funding (derivatives.jsonl).
    settings = perpetual_feed_settings(load_config(args.config)[0], _live_settings(args))
    service = LiveOrderBookService(
        args.config,
        args.output_dir,
        args.session_id or default_live_session_id(),
        settings,
    )
    return await _run_with_signals(service, service.run)


def _add_live_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("data/live"))
    parser.add_argument("--session-id")
    parser.add_argument(
        "--venue", action="append", choices=("BINANCE", "BYBIT"),
        help="repeat to select venues; defaults to both",
    )
    parser.add_argument("--binance-market", choices=("spot", "linear"), default="spot")
    parser.add_argument("--bybit-market", choices=("spot", "linear"), default="spot")
    parser.add_argument("--binance-ws-base")
    parser.add_argument("--binance-rest-base")
    parser.add_argument("--bybit-ws-url")
    parser.add_argument("--bybit-depth", type=int, default=50)
    parser.add_argument(
        "--rest-reconcile-seconds", type=float,
        help="cross-check each valid book against a REST snapshot this often (off by default); "
             "a best bid/ask disagreement forces a resync",
    )
    parser.add_argument("--http-timeout-seconds", type=float, default=10.0)
    parser.add_argument("--initial-sync-timeout-seconds", type=float, default=15.0)
    parser.add_argument("--reconnect-min-seconds", type=float, default=1.0)
    parser.add_argument("--reconnect-max-seconds", type=float, default=30.0)
    parser.add_argument("--rotate-seconds", type=float, default=3600.0)
    parser.add_argument("--rotate-bytes", type=int, default=256 * 1024 * 1024)
    parser.add_argument("--max-runtime-seconds", type=float)
    parser.add_argument("--max-events", type=int)
    parser.add_argument("--trade-stale-seconds", type=float, default=30.0)
    parser.add_argument(
        "--no-trades", action="store_true",
        help="disable public trade/LTP capture and LTP OHLC generation",
    )
    parser.add_argument("--udp-group", default="239.255.77.77")
    parser.add_argument("--udp-port", type=int, default=19001)
    parser.add_argument("--udp-interface", default="0.0.0.0")
    parser.add_argument("--udp-ttl", type=int, default=1)
    parser.add_argument("--udp-depth", type=int, default=20)
    parser.add_argument("--udp-max-datagram-bytes", type=int, default=60000)
    parser.add_argument("--monitor-interval-seconds", type=float, default=1.0)
    parser.add_argument("--no-udp", action="store_true")
    parser.add_argument("--no-fsync", action="store_true")


def main() -> None:
    parser = argparse.ArgumentParser(description="Crypto stat-arb platform")
    sub = parser.add_subparsers(dest="command", required=True)
    backtest = sub.add_parser("backtest")
    backtest.add_argument("--config", type=Path, required=True)
    backtest.add_argument("--events", type=Path, required=True)
    backtest.add_argument("--output", type=Path, default=Path("runs/latest"))
    backtest.add_argument(
        "--comparison-report", type=Path, action="append", default=[],
        help="a compare-tardis report for this data; summarised in dataset_manifest.json",
    )
    backtest.add_argument(
        "--allow-rejected-data", metavar="JUSTIFICATION",
        help="exit 0 even when the data-quality gate REJECTS the dataset; the written "
             "justification is recorded in the run (data_quality_override.json)",
    )
    backtest.add_argument(
        "--instrument-status-log", type=Path,
        help="a live-paper session's paper_timestamps.jsonl: replay its recorded "
             "effective-dated instrument/rule changes",
    )
    backtest.add_argument(
        "--derivatives", type=Path,
        help="a recorded derivatives.jsonl (mark/index/funding) replayed with the events",
    )
    backtest.add_argument(
        "--event-timing-log", type=Path,
        help="a live-paper session's paper_timestamps.jsonl: replay each event at its "
             "recorded live processing time instead of an idealized zero-latency clock",
    )
    sdk_backtest = sub.add_parser(
        "sdk-backtest",
        help="run latency-, queue-, and depth-aware cross-venue historical replay",
    )
    sdk_backtest.add_argument("--pair", required=True)
    sdk_backtest.add_argument("--data-root", type=Path, required=True)
    sdk_backtest.add_argument("--start", required=True)
    sdk_backtest.add_argument("--end", required=True)
    sdk_backtest.add_argument(
        "--mode", choices=("taker_taker", "maker_taker"), default="taker_taker"
    )
    sdk_backtest.add_argument(
        "--latency-preset",
        choices=("singapore", "tokyo", "neutral", "colocated_both"),
        default="singapore",
    )
    sdk_backtest.add_argument("--min-edge-bps", type=float, default=1.0)
    sdk_backtest.add_argument("--max-book-age-ms", type=float, default=500.0)
    sdk_backtest.add_argument("--max-touch-age-ms", type=float, default=300.0)
    sdk_backtest.add_argument("--max-walk-bps", type=float, default=2.0)
    sdk_backtest.add_argument("--max-clip-notional", type=float, default=25_000.0)
    sdk_backtest.add_argument("--max-position-base", type=float, default=0.0)
    sdk_backtest.add_argument("--max-events", type=int)
    sdk_backtest.add_argument("--seed", type=int, default=7)
    sdk_backtest.add_argument("--output", type=Path, required=True)
    sdk_backtest.add_argument("--no-deltas", action="store_true")
    sdk_backtest.add_argument("--no-toxic-gate", action="store_true")
    normalize = sub.add_parser("normalize")
    normalize.add_argument("--config", type=Path, required=True)
    normalize.add_argument("--raw", type=Path, required=True)
    normalize.add_argument("--output", type=Path, required=True)
    contract_master = sub.add_parser("contract-master")
    contract_master.add_argument(
        "--output-dir", type=Path, default=Path("data/contract_master")
    )
    contract_master.add_argument("--include-spot", action="store_true")
    contract_master.add_argument("--allow-partial", action="store_true")
    contract_master.add_argument("--timeout-seconds", type=float, default=30)
    contract_master.add_argument("--retries", type=int, default=3)
    raw_capture = sub.add_parser("raw-capture")
    raw_capture.add_argument("--input", type=Path, required=True)
    raw_capture.add_argument("--output-dir", type=Path, default=Path("data/raw"))
    raw_capture.add_argument("--session-id", required=True)
    verify_raw = sub.add_parser("verify-raw")
    verify_raw.add_argument("--raw", type=Path, required=True)
    verify_wire = sub.add_parser("verify-wire")
    verify_wire.add_argument("--wire", type=Path, required=True)
    pipeline_run = sub.add_parser("pipeline-run")
    pipeline_run.add_argument("--config", type=Path, required=True)
    pipeline_run.add_argument("--state-db", type=Path, default=Path("data/pipeline/state.sqlite"))
    pipeline_run.add_argument("--job-id", required=True)
    pipeline_run.add_argument("--partition-id", required=True)
    pipeline_run.add_argument("--raw", type=Path, required=True)
    pipeline_run.add_argument("--output", type=Path, required=True)
    pipeline_run.add_argument("--retries", type=int, default=3)
    pipeline_run.add_argument("--continuous", action="store_true")
    pipeline_run.add_argument("--max-records", type=int)
    pipeline_run.add_argument("--poll-seconds", type=float, default=1.0)
    pipeline_run.add_argument("--max-polls", type=int)
    align_streams = sub.add_parser("align-streams")
    align_streams.add_argument("--config", type=Path, required=True)
    align_streams.add_argument(
        "--input", action="append", required=True, metavar="VENUE=PATH"
    )
    align_streams.add_argument("--output", type=Path, required=True)
    live = sub.add_parser(
        "live-orderbook",
        help="continuously capture and reconstruct public Binance/Bybit order books",
    )
    _add_live_arguments(live)
    live.add_argument(
        "--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO"
    )
    paper = sub.add_parser(
        "live-paper",
        help="forward-test the strategy on live public books with simulated or testnet execution",
    )
    _add_live_arguments(paper)
    paper.add_argument(
        "--execution-mode", choices=("internal_paper", "venue_testnet"),
        default="internal_paper",
    )
    paper.add_argument(
        "--status-poll-seconds", type=float, default=60.0,
        help="poll public instrument status/rules; 0 disables",
    )
    paper.add_argument(
        "--maker-dry-run", action="store_true",
        help="run the sim_engine queue-aware maker model on live books and trades",
    )
    paper.add_argument("--flatten-lead-seconds", type=float, default=20.0)
    paper.add_argument(
        "--status-only", action="store_true",
        help="apply status changes only, not tick/lot/minimum rule changes",
    )
    paper.add_argument(
        "--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO"
    )
    calibrate = sub.add_parser(
        "calibrate-latency", help="fit latency distributions from a live paper/testnet run"
    )
    calibrate.add_argument("--run-dir", type=Path, required=True, nargs="+",
                           help="one or more paper/testnet run dirs (samples are pooled)")
    calibrate.add_argument("--output", type=Path, required=True)
    calibrate.add_argument("--seed", type=int, default=0)
    calibrate.add_argument("--min-samples", type=int, default=30)
    calibrate.add_argument("--preset")
    compare = sub.add_parser(
        "compare-runs", help="compare a replay of the same period against a forward run"
    )
    compare.add_argument("--replay", type=Path, required=True)
    compare.add_argument("--paper", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    compare.add_argument("--min-opportunities", type=int, default=30)
    history = sub.add_parser(
        "funding-history",
        help="download realised funding (public endpoints) into a rate-records JSONL",
    )
    history.add_argument("--venue", choices=("BINANCE", "BYBIT"), required=True)
    history.add_argument("--symbol", required=True, help="venue symbol, e.g. BTCUSDT")
    history.add_argument("--instrument-id", required=True, help="platform instrument id, e.g. BTC-USDT-PERP")
    history.add_argument("--start", required=True, help="ISO time or epoch ms")
    history.add_argument("--end", required=True, help="ISO time or epoch ms")
    history.add_argument("--output", type=Path, required=True)
    history.add_argument("--no-marks", action="store_true", help="Bybit: skip the mark-price kline lookups")
    gate = sub.add_parser(
        "economics-gate",
        help="fail unless a run's economics reconcile (exit 1 on FAIL)",
    )
    gate.add_argument("--run-dir", type=Path, required=True)
    gate.add_argument(
        "--allow-incomplete", action="append", default=[], metavar="REASON=JUSTIFICATION",
        help="accept an INCOMPLETE reason (MISSING_PRICE, FEE_ASSET_UNPRICED, FEE_RULE_FALLBACK, FUNDING_DATA, "
             "BORROW_DATA, TRANSFER_DATA, DATA_QUALITY) with a written justification; listed in the result",
    )
    caps = sub.add_parser("capabilities", help="venue/product/adapter capability matrix (REF05)")
    caps.add_argument("--venue", action="append", choices=("BINANCE", "BYBIT"))
    tardis = sub.add_parser(
        "compare-tardis",
        help="offline: compare a recorded capture's books against a Tardis incremental_book_L2 CSV",
    )
    tardis.add_argument("--capture", type=Path, required=True, help="live session directory (part-*/)")
    tardis.add_argument("--venue", choices=("BINANCE", "BYBIT"), required=True)
    tardis.add_argument("--tardis", type=Path, required=True, help="incremental_book_L2 .csv or .csv.gz")
    tardis.add_argument("--symbol", help="Tardis symbol filter, e.g. BTCUSDT")
    tardis.add_argument("--interval-ms", type=int, default=1000)
    tardis.add_argument("--depth", type=int, default=20)
    tardis.add_argument("--output", type=Path, required=True)
    benchmark = sub.add_parser("benchmark", help="MR-14 replay and paper performance baseline")
    benchmark.add_argument("--config", type=Path, required=True)
    benchmark.add_argument("--output", type=Path, required=True)
    benchmark.add_argument("--events", type=Path)
    benchmark.add_argument("--synthetic-count", type=int, default=20_000)
    analyze = sub.add_parser(
        "analyze-session",
        help="audit a live paper session: storage, books, replay, backtest, simulation, dry run",
    )
    analyze.add_argument("--config", type=Path, required=True)
    analyze.add_argument("--session-dir", type=Path, required=True)
    analyze.add_argument("--output", type=Path, required=True)
    analyze.add_argument("--stderr-log", type=Path)
    analyze.add_argument("--no-simulation", action="store_true")
    analyze.add_argument("--min-opportunities", type=int, default=30)
    cycle_report = sub.add_parser(
        "cycle-report", help="consolidated audit report across every watcher cycle"
    )
    cycle_report.add_argument("--root", type=Path, default=Path("runs/paper_watch"))
    cycle_reports = sub.add_parser(
        "cycle-reports",
        help="downloadable per-cycle HTML/PDF/CSV reports with the MR-01..MR-14 assessment",
    )
    cycle_reports.add_argument("--root", type=Path, default=Path("runs/paper_watch"))
    cycle_reports.add_argument("--no-pdf", action="store_true")
    simulate = sub.add_parser(
        "simulate-maker", help="replay stored books and trades through sim_engine"
    )
    simulate.add_argument("--config", type=Path, required=True)
    simulate.add_argument("--session-dir", type=Path, required=True)
    simulate.add_argument("--output", type=Path, required=True)
    status = sub.add_parser(
        "instrument-status", help="poll public instrument status/rules once"
    )
    status.add_argument("--config", type=Path, required=True)
    status.add_argument("--venue", action="append", choices=("BINANCE", "BYBIT"))
    log_query = sub.add_parser(
        "log-query", help="query indexed connector and paper-host structured logs"
    )
    log_query.add_argument("--db", type=Path, required=True)
    log_query.add_argument("--correlation-id")
    log_query.add_argument("--source-event-id")
    log_query.add_argument("--feature-state-id")
    log_query.add_argument("--opportunity-id")
    log_query.add_argument("--risk-decision-id")
    log_query.add_argument("--parent-plan-id")
    log_query.add_argument("--child-order-id")
    log_query.add_argument("--request-id")
    log_query.add_argument("--component")
    log_query.add_argument("--level", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    log_query.add_argument("--event-type")
    log_query.add_argument("--venue")
    log_query.add_argument("--text")
    log_query.add_argument("--limit", type=int, default=1000)
    release = sub.add_parser(
        "paper-release", help="create, approve, deploy, verify or roll back a paper release"
    )
    release_sub = release.add_subparsers(dest="release_command", required=True)
    release_create = release_sub.add_parser("create")
    release_create.add_argument("--registry", type=Path, required=True)
    release_create.add_argument("--strategy-id", required=True)
    release_create.add_argument("--build-version", required=True)
    release_create.add_argument("--environment", required=True)
    release_create.add_argument(
        "--artifact", action="append", required=True,
        metavar="KIND:NAME:VERSION=PATH",
    )
    for command in ("approve", "deploy", "verify"):
        item = release_sub.add_parser(command)
        item.add_argument("--registry", type=Path, required=True)
        item.add_argument("--release-id", required=True)
        if command in {"approve", "deploy"}:
            item.add_argument("--owner", required=True)
    release_rollback = release_sub.add_parser("rollback")
    release_rollback.add_argument("--registry", type=Path, required=True)
    release_rollback.add_argument("--release-id", required=True)
    release_rollback.add_argument("--owner", required=True)
    release_rollback.add_argument("--reason", required=True)
    release_reproduce = release_sub.add_parser("reproduce")
    release_reproduce.add_argument("--registry", type=Path, required=True)
    release_reproduce.add_argument("--release-id", required=True)
    release_reproduce.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "backtest":
        summary = run_backtest(
            args.config, args.events, args.output,
            instrument_status_log=args.instrument_status_log,
            event_timing_log=args.event_timing_log,
            derivatives_log=args.derivatives,
            comparison_reports=args.comparison_report,
        )
        rejected = enforce_data_quality(summary, args.output, args.allow_rejected_data)
        print(json.dumps(summary, indent=2))
        if rejected:
            print(rejected, file=sys.stderr)
            raise SystemExit(1)
    elif args.command == "sdk-backtest":
        result = BacktestAPI(RuntimeMode.BACKTEST).run(AdvancedBacktestSpec(
            pair=args.pair,
            data_root=args.data_root,
            start_date=args.start,
            end_date=args.end,
            execution_mode=args.mode,
            latency_preset=args.latency_preset,
            min_net_edge_bps=args.min_edge_bps,
            max_book_age_ms=args.max_book_age_ms,
            max_touch_age_ms=args.max_touch_age_ms,
            max_walk_bps=args.max_walk_bps,
            max_clip_notional=args.max_clip_notional,
            max_position_base=args.max_position_base,
            max_events=args.max_events,
            seed=args.seed,
            output_dir=args.output,
            include_deltas=not args.no_deltas,
            block_on_toxic=not args.no_toxic_gate,
        ))
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    elif args.command == "normalize":
        print(json.dumps(normalize_raw_file(args.config, args.raw, args.output), indent=2))
    elif args.command == "contract-master":
        print(json.dumps(dump_contract_master(
            args.output_dir,
            include_spot=args.include_spot,
            allow_partial=args.allow_partial,
            client=PublicHttpClient(args.timeout_seconds, args.retries),
        ), indent=2))
    elif args.command == "raw-capture":
        with args.input.open(encoding="utf-8") as handle:
            envelopes = [json.loads(line) for line in handle if line.strip()]
        print(json.dumps(capture_envelopes(
            envelopes, args.output_dir, args.session_id
        ), indent=2))
    elif args.command == "verify-raw":
        print(json.dumps(verify_raw_file(args.raw), indent=2))
    elif args.command == "verify-wire":
        print(json.dumps(verify_wire_file(args.wire), indent=2))
    elif args.command == "pipeline-run":
        spec = PartitionSpec(args.partition_id, args.raw, args.output)
        with PipelineOrchestrator.for_canonical_pipeline(args.state_db, args.config) as pipeline:
            if args.max_records is not None:
                result = pipeline.run_partition(
                    args.job_id, spec, max_records=args.max_records,
                    complete_on_eof=not args.continuous,
                    job_mode="CONTINUOUS" if args.continuous else "BATCH",
                )
            elif args.continuous:
                result = pipeline.run_continuous(
                    args.job_id,
                    spec,
                    poll_interval_seconds=args.poll_seconds,
                    max_polls=args.max_polls,
                )
            else:
                result = pipeline.run_with_retry(args.job_id, spec, max_retries=args.retries)
        print(json.dumps(result, indent=2))
    elif args.command == "align-streams":
        venue_files = {}
        for value in args.input:
            try:
                venue, path = value.split("=", 1)
            except ValueError as exc:
                raise SystemExit("--input must use VENUE=PATH") from exc
            venue = venue.upper()
            if venue in venue_files:
                raise SystemExit(f"duplicate --input venue: {venue}")
            venue_files[venue] = Path(path)
        print(json.dumps(align_canonical_files(
            args.config, venue_files, args.output
        ), indent=2))
    elif args.command == "live-orderbook":
        logging.basicConfig(
            level=getattr(logging, args.log_level),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        _print_live_result(asyncio.run(_run_live_orderbook(args)))
    elif args.command == "live-paper":
        logging.basicConfig(
            level=getattr(logging, args.log_level),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        _print_live_result(asyncio.run(_run_live_paper(args)))
    elif args.command == "calibrate-latency":
        print(json.dumps(calibrate_latency(
            args.run_dir, args.output, seed=args.seed,
            min_samples=args.min_samples, preset=args.preset,
        ), indent=2))
    elif args.command == "compare-runs":
        print(json.dumps(compare_runs(
            args.replay, args.paper, args.output,
            min_opportunities=args.min_opportunities,
        ), indent=2))
    elif args.command == "funding-history":
        from .funding_history import fetch_binance, fetch_bybit, to_ms, urllib_get, write_jsonl

        start, end = to_ms(args.start), to_ms(args.end)
        if args.venue == "BINANCE":
            records = fetch_binance(args.symbol, args.instrument_id, start, end, http=urllib_get)
        else:
            records = fetch_bybit(args.symbol, args.instrument_id, start, end, http=urllib_get,
                                  with_marks=not args.no_marks)
        write_jsonl(records, args.output)
        print(json.dumps({"venue": args.venue, "records": len(records),
                          "without_mark": sum(1 for r in records if not r["mark_price"]),
                          "output": str(args.output)}, indent=2))
    elif args.command == "capabilities":
        from .capabilities import report as capability_report

        print(json.dumps(capability_report(tuple(args.venue) if args.venue else None), indent=2))
    elif args.command == "compare-tardis":
        from .tardis_compare import compare_with_tardis

        report = compare_with_tardis(args.capture, args.venue, args.tardis, symbol=args.symbol,
                                     interval_ns=args.interval_ms * 1_000_000, depth=args.depth)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps({k: report[k] for k in ("venue", "samples", "compared_samples", "mismatch_share",
                                                 "event_counts")}, indent=2))
    elif args.command == "economics-gate":
        allow = {}
        for item in args.allow_incomplete:
            reason, _, why = item.partition("=")
            allow[reason.strip().upper()] = why.strip()
        result = economics_gate(args.run_dir, allow_incomplete=allow)
        print(json.dumps(result, indent=2))
        if result["decision"] != "PASS":
            raise SystemExit(1)
    elif args.command == "benchmark":
        print(json.dumps(run_benchmark(
            args.config, args.output, events_path=args.events,
            synthetic_count=args.synthetic_count,
        ), indent=2))
    elif args.command == "analyze-session":
        audit = audit_session(
            args.config, args.session_dir, args.output, stderr_log=args.stderr_log,
            run_simulation=not args.no_simulation,
            min_opportunities=args.min_opportunities,
        )
        print(json.dumps({
            "overall_status": audit["overall_status"],
            "components": {
                name: row["status"] for name, row in audit["components"].items()
            },
            "issues": len(audit["issues"]),
            "report": str(args.output / "session_audit.json"),
        }, indent=2))
        if args.output.resolve().parent.name == "audits":
            # Watcher layout: refresh the consolidated report for every cycle.
            build_cycle_report(args.output.resolve().parent.parent)
            build_cycle_reports(args.output.resolve().parent.parent)
    elif args.command == "cycle-reports":
        build_cycle_report(args.root)
        result = build_cycle_reports(args.root, pdf=not args.no_pdf)
        print(json.dumps({key: result[key] for key in (
            "cycles", "audited", "output", "bundle", "requirements_rollup",
        )}, indent=2))
    elif args.command == "cycle-report":
        report = build_cycle_report(args.root)
        print(json.dumps({
            "totals": report["totals"],
            "report": str(args.root / "AUDIT_REPORT.md"),
        }, indent=2, default=str))
    elif args.command == "simulate-maker":
        parts = sorted(args.session_dir.glob("part-*"))
        print(json.dumps(simulate_recorded_session(
            args.config, [part / "canonical.jsonl" for part in parts],
            [part / "trades.jsonl" for part in parts], args.output,
        ), indent=2))
    elif args.command == "instrument-status":
        cfg, _ = load_config(args.config)
        poller = InstrumentStatusPoller(cfg.instruments, {
            venue: (cfg.instrument_id, cfg.instruments.get(venue, cfg.instrument_id).symbol)
            for venue in (args.venue or cfg.venues)
        })
        fetched = poller.fetch()
        changes = poller.apply(fetched, time.time_ns())
        print(json.dumps({"fetched": fetched, "changes": changes}, indent=2, default=str))
    elif args.command == "log-query":
        if not args.db.exists():
            raise SystemExit(f"structured log database does not exist: {args.db}")
        store = StructuredLogStore(
            args.db.with_suffix(".jsonl"), args.db
        )
        if args.limit < 1 or args.limit > 10_000:
            raise SystemExit("--limit must be between 1 and 10000")
        opportunity_trace_only = args.opportunity_id and not any((
            args.correlation_id, args.source_event_id, args.feature_state_id,
            args.risk_decision_id, args.parent_plan_id, args.child_order_id,
            args.request_id, args.component, args.level, args.event_type,
            args.venue, args.text,
        ))
        if opportunity_trace_only:
            records = store.trace_opportunity(args.opportunity_id)[:args.limit]
        else:
            records = store.query(
                correlation_id=args.correlation_id,
                source_event_id=args.source_event_id,
                feature_state_id=args.feature_state_id,
                opportunity_id=args.opportunity_id,
                risk_decision_id=args.risk_decision_id,
                parent_plan_id=args.parent_plan_id,
                child_order_id=args.child_order_id,
                request_id=args.request_id,
                component=args.component, level=args.level,
                event_type=args.event_type, venue=args.venue,
                text=args.text, limit=args.limit,
            )
        print(json.dumps({"count": len(records), "records": records}, indent=2))
    elif args.command == "paper-release":
        registry = PaperReleaseRegistry(args.registry)
        if args.release_command == "create":
            artifacts = []
            for value in args.artifact:
                try:
                    descriptor, raw_path = value.split("=", 1)
                    raw_kind, name, version = descriptor.split(":", 2)
                    artifacts.append(ArtifactInput(
                        ArtifactKind(raw_kind.upper()), name, version, Path(raw_path)
                    ))
                except (ValueError, TypeError) as exc:
                    raise SystemExit(
                        "--artifact must use KIND:NAME:VERSION=PATH"
                    ) from exc
            result = registry.create(PaperReleaseSpec(
                strategy_id=args.strategy_id,
                build_version=args.build_version,
                environment=args.environment,
                artifacts=tuple(artifacts),
            ))
        elif args.release_command == "approve":
            result = registry.approve(args.release_id, args.owner)
        elif args.release_command == "deploy":
            result = registry.deploy(args.release_id, args.owner)
        elif args.release_command == "rollback":
            result = registry.rollback(args.release_id, args.owner, args.reason)
        elif args.release_command == "verify":
            result = registry.verify(args.release_id)
        else:
            result = registry.reproduce(args.release_id, args.output_dir)
        print(json.dumps(result, indent=2))



if __name__ == "__main__":
    main()
