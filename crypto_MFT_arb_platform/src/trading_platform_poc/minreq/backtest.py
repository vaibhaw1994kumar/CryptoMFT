"""End-to-end orchestration for backtest and paper trading (MR-08, MR-14).

The same object graph serves both modes: replay feeds the feature engine, the
strategy expresses intent, risk reserves capacity, the router chooses venues
and dispatches, the matcher fills against replayed depth, and every state
change lands in the append-only ledger. Only the event source and the matching
mode differ between a backtest and a paper run.

:class:`TradingSession` is that object graph. It consumes canonical events in
receive-time order and advances router time before each one, so orders in
flight land against the book as it is when they arrive. The backtest drives it
from :class:`MultiVenueReplay`; ``trading_platform_poc.minreq.live`` drives it
from live public feeds.
"""

from __future__ import annotations

import json
import time
import tracemalloc
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from new_backtest.latency_model import MarkoutTracker
from trading_platform_poc.minreq import DOCUMENT_VERSION, LAYER_VERSION
from trading_platform_poc.minreq.canonical import (
    CanonicalEvent,
    CanonicalEventType,
    ControlAction,
    QualityFlag,
    SCHEMA_VERSION,
)
from trading_platform_poc.minreq.features import (
    FEATURE_SET_VERSION,
    CrossVenueFeatureEngine,
    FeatureConfig,
    feature_set_hash,
)
from trading_platform_poc.minreq.fees import CostModel, FeeBook, default_fee_book
from trading_platform_poc.minreq.latency import (
    default_latency_model,
    latency_model_from_preset,
)
from trading_platform_poc.minreq.ledger import EventLedger
from trading_platform_poc.minreq.manifest import RunManifest, build_identity
from trading_platform_poc.minreq.matching import (
    ExecutionMode,
    MatchingConfig,
    PaperMatchingEngine,
)
from trading_platform_poc.minreq.monitoring import MetricRegistry
from trading_platform_poc.minreq.pipeline import (
    Pipeline,
    instrument_master_version,
)
from trading_platform_poc.minreq.quality import QualityReport, QualityThresholds
from trading_platform_poc.minreq.replay import ControlSchedule, MultiVenueReplay
from trading_platform_poc.minreq.riskcontrols import (
    AutoKillPolicy,
    KillSwitch,
    KillSwitchSupervisor,
    RiskEngine,
    RiskLimits,
)
from trading_platform_poc.minreq.router import (
    OrderRouter,
    RouterConfig,
)
from trading_platform_poc.minreq.securitymaster import (
    SecurityMaster,
    TradingStatus,
    default_security_master,
)
from trading_platform_poc.minreq.statarb import (
    STRATEGY_VERSION,
    CrossVenueStatArbStrategy,
    StatArbConfig,
)
from trading_platform_poc.minreq.synthetic import SyntheticConfig, generate
from trading_platform_poc.minreq.tca import TCAReport, build_tca_report

UNTRADABLE_TRADE_FLAGS = frozenset(
    {QualityFlag.SCHEMA_EXCEPTION, QualityFlag.TRADE_OUTSIDE_BOOK}
)


@dataclass(slots=True)
class BacktestConfig:
    run_id: str
    economic_group: str = "BTC/USDT.SPOT"
    mode: str = "backtest"
    seed: int = 20260817
    venue_instruments: dict[str, str] = field(
        default_factory=lambda: {
            "binance": "BTC-USDT.BINANCE.SPOT",
            "okx": "BTC-USDT.OKX.SPOT",
        }
    )
    feature: FeatureConfig | None = None
    strategy: StatArbConfig | None = None
    risk: RiskLimits = field(default_factory=RiskLimits)
    router: RouterConfig = field(default_factory=RouterConfig)
    matching: MatchingConfig = field(default_factory=MatchingConfig)
    stale_after_ns: int = 2_000_000_000
    controls: tuple[ControlSchedule, ...] = ()
    snapshot_every: int = 1
    warmup_events: int = 0
    latency_preset: str | None = None
    auto_kill: AutoKillPolicy = field(default_factory=AutoKillPolicy)
    reconcile_every_ns: int = 60_000_000_000
    drain_horizon_ns: int = 60_000_000_000

    def __post_init__(self) -> None:
        if self.feature is None:
            self.feature = FeatureConfig(economic_group=self.economic_group)
        if self.strategy is None:
            self.strategy = StatArbConfig(economic_group=self.economic_group)

    def to_dict(self) -> dict[str, Any]:
        assert self.feature is not None and self.strategy is not None
        return {
            "run_id": self.run_id,
            "economic_group": self.economic_group,
            "mode": self.mode,
            "seed": self.seed,
            "venue_instruments": dict(sorted(self.venue_instruments.items())),
            "feature": self.feature.to_dict(),
            "strategy": self.strategy.to_dict(),
            "risk": self.risk.to_dict(),
            "router": self.router.to_dict(),
            "matching": self.matching.to_dict(),
            "stale_after_ns": self.stale_after_ns,
            "controls": [control.to_dict() for control in self.controls],
            "snapshot_every": self.snapshot_every,
            "latency_preset": self.latency_preset,
            "auto_kill": self.auto_kill.to_dict(),
            "reconcile_every_ns": self.reconcile_every_ns,
            "drain_horizon_ns": self.drain_horizon_ns,
        }


@dataclass(slots=True)
class BacktestResult:
    config: BacktestConfig
    manifest: RunManifest
    tca: TCAReport
    replay_summary: dict[str, Any]
    monitoring: dict[str, Any]
    reconciliation: dict[str, Any]
    strategy_summary: dict[str, Any]
    router_summary: dict[str, Any]
    risk_summary: dict[str, Any]
    ledger_path: Path
    state_hash: str
    benchmark: dict[str, Any]
    output_dir: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.config.run_id,
            "mode": self.config.mode,
            "manifest": self.manifest.to_dict(),
            "replay": self.replay_summary,
            "strategy": self.strategy_summary,
            "router": self.router_summary,
            "risk": self.risk_summary,
            "monitoring": self.monitoring,
            "reconciliation": self.reconciliation,
            "tca_totals": self.tca.totals,
            "state_hash": self.state_hash,
            "benchmark": self.benchmark,
            "ledger_path": str(self.ledger_path),
            "output_dir": str(self.output_dir),
        }


def assert_execution_mode(
    config: BacktestConfig, matcher: Any | None = None
) -> ExecutionMode:
    """Refuse a run whose execution adapter does not match its mode.

    A backtest may only simulate. A paper run labels its fills
    ``internal_paper`` or routes to a real ``venue_testnet`` adapter, and in
    the latter case the adapter that was injected must actually be one.
    """

    configured = config.matching.mode
    if config.mode == "backtest" and configured is not ExecutionMode.INTERNAL_SIMULATION:
        raise ValueError(
            "backtest runs must use internal_simulation execution, "
            f"not {configured.value}"
        )
    if config.mode == "paper" and configured is ExecutionMode.INTERNAL_SIMULATION:
        raise ValueError(
            "paper runs label fills internal_paper or venue_testnet, "
            "never internal_simulation"
        )
    if configured is ExecutionMode.VENUE_TESTNET:
        if matcher is None or getattr(matcher, "mode", None) is not ExecutionMode.VENUE_TESTNET:
            raise ValueError(
                "execution_mode venue_testnet requires an active venue testnet "
                "adapter; none was provided"
            )
    elif matcher is not None and getattr(matcher, "mode", None) is not configured:
        raise ValueError(
            f"execution adapter mode {getattr(matcher, 'mode', None)} does not "
            f"match configured mode {configured.value}"
        )
    return configured


class TradingSession:
    """The wired stack, driven one canonical event at a time."""

    def __init__(
        self,
        config: BacktestConfig,
        output_dir: str | Path,
        *,
        security_master: SecurityMaster | None = None,
        fee_book: FeeBook | None = None,
        dataset_info: Mapping[str, Any] | None = None,
        matcher: Any | None = None,
        matcher_factory: Any | None = None,
    ) -> None:
        assert config.feature is not None
        assert config.strategy is not None
        if matcher is None and matcher_factory is None:
            # Fail before any file is written when a venue adapter is required.
            assert_execution_mode(config, None)
        self.config = config
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.security_master = security_master or default_security_master()
        self.fee_book = fee_book or default_fee_book()
        self.dataset_info = dict(dataset_info or {})
        self.started_ns = time.time_ns()

        venues = tuple(config.venue_instruments)
        self.cost_model = CostModel(self.fee_book)
        self.latency = (
            latency_model_from_preset(config.latency_preset, venues, seed=config.seed)
            if config.latency_preset
            else default_latency_model(venues, seed=config.seed)
        )
        self.feature_engine = CrossVenueFeatureEngine(
            config.feature,
            config.venue_instruments,
            stale_after_ns=config.stale_after_ns,
        )
        self.kill_switch = KillSwitch()
        self.risk_engine = RiskEngine(
            config.risk, self.security_master, kill_switch=self.kill_switch
        )
        self.supervisor = KillSwitchSupervisor(self.risk_engine, config.auto_kill)
        if matcher is None and matcher_factory is not None:
            matcher = matcher_factory(
                books=self.feature_engine.books,
                latency=self.latency,
                config=config.matching,
            )
        self.matcher = matcher or PaperMatchingEngine(
            self.feature_engine.books, self.latency, config=config.matching
        )
        # The adapter that will actually execute must match the configured
        # mode: a venue_testnet run with a simulator is refused here.
        self.execution_mode = assert_execution_mode(config, matcher)
        self.ledger_path = self.output_dir / "ledger.sqlite"
        if self.ledger_path.exists():
            self.ledger_path.unlink()
        self.ledger = EventLedger(self.ledger_path, run_id=config.run_id)
        self.ledger.set_meta("document_version", DOCUMENT_VERSION)
        self.ledger.set_meta("layer_version", LAYER_VERSION)
        self.ledger.set_meta("mode", config.mode)
        self.ledger.set_meta("execution_mode", self.execution_mode.value)

        self.router = OrderRouter(
            run_id=config.run_id,
            security_master=self.security_master,
            cost_model=self.cost_model,
            risk_engine=self.risk_engine,
            matcher=self.matcher,
            ledger=self.ledger,
            books=self.feature_engine.books,
            latency=self.latency,
            config=config.router,
        )
        self.strategy = CrossVenueStatArbStrategy(
            config.strategy,
            config.venue_instruments,
            reference_venue=self.feature_engine.reference_venue,
        )
        self.metrics = MetricRegistry()
        self.markouts = MarkoutTracker()
        self._markout_seen = 0
        self.processed = 0
        self.book_events = 0
        self.signal_events = 0
        self.plans_submitted = 0
        self.quality_counts: dict[str, int] = {}
        self.last_event_ns = 0
        self.feature_seconds = 0.0
        self.strategy_seconds = 0.0
        self.execution_seconds = 0.0
        self.kill_actions: list[dict[str, Any]] = []
        self._flattened = False
        self._last_reconcile_ns = 0
        self._last_breaks = 0
        self.reconciliations = 0
        self._closed = False
        tracemalloc.start()
        self._wall_start = time.perf_counter()

    # --------------------------------------------------------------- events
    def process(self, event: CanonicalEvent) -> None:
        self.processed += 1
        timestamp = event.receive_timestamp_ns
        self.last_event_ns = max(self.last_event_ns, timestamp)
        # Everything in flight up to this instant lands before the event is
        # applied, so an arrival sees the book the venue had at that moment.
        self.advance(timestamp)

        if event.event_type is CanonicalEventType.CONTROL:
            self._apply_control(event)
            return
        if event.event_type is CanonicalEventType.DATA_QUALITY:
            code = str(getattr(event.payload, "code", "unknown"))
            self.quality_counts[code] = self.quality_counts.get(code, 0) + 1
            return
        if event.event_type is CanonicalEventType.INSTRUMENT_STATUS:
            self._apply_instrument_status(event)
            return
        if event.event_type is CanonicalEventType.TRADE:
            if not UNTRADABLE_TRADE_FLAGS.intersection(event.quality_flags):
                self._apply_trade(event)
            return
        if event.event_type not in {
            CanonicalEventType.BOOK_SNAPSHOT,
            CanonicalEventType.BOOK_DELTA,
        }:
            return

        mark = time.perf_counter()
        self.feature_engine.observe(event)
        if event.venue in self.feature_engine.books:
            self.matcher.on_book_update(event.venue)
            book = self.feature_engine.books[event.venue]
            self.risk_engine.mark(book.instrument_id, event.venue, book.mid_price)
        self.book_events += 1
        if self.book_events % max(1, self.config.snapshot_every):
            self.feature_seconds += time.perf_counter() - mark
            return
        snapshot = self.feature_engine.snapshot(timestamp)
        self.feature_seconds += time.perf_counter() - mark
        mark = time.perf_counter()
        decision = self.strategy.evaluate(snapshot)
        self.strategy_seconds += time.perf_counter() - mark
        if decision.intent is None:
            return
        self.signal_events += 1
        mark = time.perf_counter()
        result = self.router.submit_opportunity(decision.intent)
        self.execution_seconds += time.perf_counter() - mark
        if result.accepted:
            self.plans_submitted += 1
        else:
            # A refused intent changed nothing on the venues, so the strategy
            # must not keep believing it holds the position it asked for.
            self._sync_strategy(busy=bool(self.router.open_plans()))

    def advance(self, now_ns: int) -> None:
        """Move execution time forward and run every time-driven control."""

        self.router.advance(now_ns)
        now = self.router.now_ns
        self._update_markouts(now)

        feed_ages = {
            venue: now - book.last_update_ns
            for venue, book in self.feature_engine.books.items()
            if book.initialized
        }
        fired = self.supervisor.evaluate(
            now,
            feed_ages_ns=feed_ages,
            venue_reject_streaks=self.router.venue_reject_streak,
            open_residual_breaches=self.router.open_residual_breaches(),
            unresolved_orders=self.router.unresolved_orders,
            reconciliation_breaks=self._last_breaks,
        )
        for record in fired:
            if record["engaged_kill_switch"]:
                cancels = self.router.cancel_all(now, f"auto_kill:{record['trigger']}")
                self.kill_actions.append({**record, "cancel_requests": cancels})
                self._flattened = False

        open_plans = self.router.open_plans()
        if self.kill_switch.active and not self._flattened and not open_plans:
            opened = self.router.flatten_positions(
                now, self.kill_switch.reason or "kill_switch"
            )
            self._flattened = True
            self.kill_actions.append(
                {"timestamp_ns": now, "action": "flatten", "plans_opened": opened}
            )
            open_plans = self.router.open_plans()

        if (
            now - self._last_reconcile_ns >= self.config.reconcile_every_ns
            and self.router.quiescent()
        ):
            self._reconcile(now)

        self._sync_strategy(busy=bool(open_plans))

    def _sync_strategy(self, *, busy: bool) -> None:
        positions = {
            venue: self.risk_engine.position(instrument, venue)
            for venue, instrument in self.config.venue_instruments.items()
        }
        self.strategy.observe_positions(positions, busy=busy)

    def _reconcile(self, timestamp_ns: int) -> Any:
        report = self.ledger.reconcile(
            self.matcher.venue_snapshot(), timestamp_ns, run_id=self.config.run_id
        )
        self._last_breaks = len(report.breaks)
        self._last_reconcile_ns = timestamp_ns
        self.reconciliations += 1
        return report

    def _mids(self) -> dict[str, float]:
        return {
            venue: float(book.mid_price)
            for venue, book in self.feature_engine.books.items()
            if book.mid_price is not None
        }

    def _update_markouts(self, now_ns: int, *, finalize: bool = False) -> None:
        entries = self.router.execution_quality
        for item in entries[self._markout_seen :]:
            self.markouts.record_fill(
                (item["execution_ns"] or item["report_ns"]) / 1e9,
                item["venue"],
                item["side"],
                float(item["fill_price"]),
                float(item["quantity"]),
            )
        self._markout_seen = len(entries)
        self.markouts.resolve(now_ns / 1e9, self._mids(), finalize=finalize)

    # ------------------------------------------------------------- controls
    def _apply_control(self, event: CanonicalEvent) -> None:
        action = str(getattr(event.payload, "action", ""))
        venue = event.venue or ""
        timestamp = event.receive_timestamp_ns
        if action in {
            ControlAction.DISCONNECT.value,
            ControlAction.VENUE_SUSPENDED.value,
            ControlAction.FEED_STALL.value,
        }:
            self.feature_engine.suspend(venue)
            self.risk_engine.suspend_venue(venue)
            self.matcher.set_venue_available(venue, False)
            self.supervisor.venue_down(venue, timestamp)
            self.router.cancel_all(timestamp, f"venue_{action}")
        elif action in {
            ControlAction.RECONNECT.value,
            ControlAction.VENUE_RESUMED.value,
            ControlAction.FEED_RECOVERED.value,
        }:
            self.feature_engine.resume(venue)
            self.risk_engine.resume_venue(venue)
            self.matcher.set_venue_available(venue, True)
            self.supervisor.venue_up(venue)
        elif action == ControlAction.KILL_SWITCH_ON.value:
            self.risk_engine.kill_switch.activate(
                timestamp,
                str(getattr(event.payload, "reason", "") or "control"),
                "control",
            )
            self._flattened = False
            self.router.cancel_all(timestamp, "kill_switch")
        elif action == ControlAction.KILL_SWITCH_OFF.value:
            # Clearing requires a clean reconciliation, not an assertion of one.
            clean = self.router.quiescent() and self._reconcile(timestamp).clean
            if clean:
                self.risk_engine.kill_switch.clear(timestamp, "control", reconciled=True)
                self.supervisor.reset()
                self._flattened = False
            else:
                self.risk_engine.kill_switch.history.append(
                    {
                        "action": "clear_refused",
                        "timestamp_ns": timestamp,
                        "actor": "control",
                        "reason": "reconciliation_not_clean",
                    }
                )

    def _apply_instrument_status(self, event: CanonicalEvent) -> None:
        raw = str(getattr(event.payload, "status", "trading")).lower()
        try:
            status = TradingStatus(raw)
        except ValueError:
            return
        current = self.risk_engine.security_master.resolve(
            event.instrument_id, event.receive_timestamp_ns
        )
        if current.status is status:
            return
        updated = self.risk_engine.security_master.with_status(
            event.instrument_id,
            status,
            effective_from_ns=event.receive_timestamp_ns,
        )
        self.risk_engine.security_master = updated
        self.router.security_master = updated

    def _apply_trade(self, event: CanonicalEvent) -> None:
        payload = event.payload
        price = getattr(payload, "price", None)
        quantity = getattr(payload, "quantity", None)
        if price is None or quantity is None:
            return
        fills = self.matcher.on_trade(
            event.venue,
            event.instrument_id,
            Decimal(str(price)),
            Decimal(str(quantity)),
            str(getattr(payload, "aggressor_side", None) or "buy"),
            event.receive_timestamp_ns,
        )
        if fills:
            self.router.on_maker_fills(fills)

    # --------------------------------------------------------------- finish
    def finish(
        self,
        *,
        quality_report: QualityReport,
        replay_signature: str,
        inputs: Mapping[str, Any],
    ) -> BacktestResult:
        self.router.drain(horizon_ns=self.config.drain_horizon_ns)
        end_ns = max(self.router.now_ns, self.last_event_ns)
        self._update_markouts(end_ns, finalize=True)

        wall_elapsed = time.perf_counter() - self._wall_start
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        reconciliation = self._reconcile(end_ns or self.started_ns)
        derived = self.ledger.derive(run_id=self.config.run_id)
        tca = build_tca_report(
            run_id=self.config.run_id,
            generated_ns=self.started_ns,
            ledger=self.ledger,
            plans=self.router.plans,
            missed_opportunities=self.router.missed_opportunities,
            rejects=self.matcher.rejects,
            latency=self.latency.report(),
        )
        self._observe_metrics(
            quality_report=quality_report,
            tca=tca,
            reconciliation_breaks=len(reconciliation.breaks),
        )

        benchmark = {
            "events_processed": self.processed,
            "book_events": self.book_events,
            "wall_seconds": round(wall_elapsed, 6),
            "events_per_second": (
                round(self.processed / wall_elapsed, 2) if wall_elapsed > 0 else 0
            ),
            "feature_seconds": round(self.feature_seconds, 6),
            "strategy_seconds": round(self.strategy_seconds, 6),
            "execution_seconds": round(self.execution_seconds, 6),
            "child_orders": int(tca.totals.get("orders", "0")),
            "peak_memory_bytes": peak,
            "current_memory_bytes": current,
            "ledger_events": self.ledger.count(run_id=self.config.run_id),
            "ledger_bytes": (
                self.ledger_path.stat().st_size if self.ledger_path.exists() else 0
            ),
        }

        router_summary = self.router.summary()
        results = {
            "replay_signature": replay_signature,
            "quality": quality_report.to_dict(),
            "quality_event_counts": dict(sorted(self.quality_counts.items())),
            "strategy": self.strategy.summary(),
            "router": router_summary,
            "tca_totals": tca.totals,
            "ledger_chain_digest": self.ledger.chain_digest(run_id=self.config.run_id),
            "derived_state_hash": derived.state_hash(),
            "reconciliation": reconciliation.to_dict(),
            "risk_binding_events": self.risk_engine.binding_events,
            "auto_kill": self.supervisor.to_dict(),
            "kill_actions": self.kill_actions,
            "markouts": self.markouts.summary(),
            "reconciliations": self.reconciliations,
            "monitoring": self.metrics.report(),
        }

        manifest = RunManifest(
            run_id=self.config.run_id,
            mode=self.config.mode,
            started_ns=self.started_ns,
            seed=self.config.seed,
            build=build_identity(),
            config=self.config.to_dict(),
            component_versions={
                "document": DOCUMENT_VERSION,
                "layer": LAYER_VERSION,
                "canonical_schema": str(SCHEMA_VERSION),
                "feature_set": FEATURE_SET_VERSION,
                "feature_set_hash": feature_set_hash(),
                "strategy": STRATEGY_VERSION,
                "fee_book": self.fee_book.version,
                "instrument_master": instrument_master_version(
                    self.security_master
                ),
                "latency_seed": str(self.latency.seed),
                "execution_mode": self.execution_mode.value,
            },
            dataset=self.dataset_info,
            inputs=dict(inputs),
            results=results,
            finished_ns=time.time_ns(),
        )

        manifest.write(self.output_dir / "run_manifest.json")
        tca.write_json(self.output_dir / "tca.json")
        tca.write_csv(self.output_dir / "tca.csv")
        tca.write_markdown(self.output_dir / "tca.md")
        (self.output_dir / "route_log.json").write_text(
            json.dumps(self.router.route_log, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        (self.output_dir / "execution_quality.json").write_text(
            json.dumps(
                {
                    "summary": router_summary,
                    "markouts": self.markouts.summary(),
                    "fills": self.router.execution_quality,
                    "residual_breaches": self.router.residual_breaches,
                    "unresolved_orders": self.router.unresolved_orders,
                },
                indent=2,
                sort_keys=True,
                default=str,
            ),
            encoding="utf-8",
        )
        (self.output_dir / "benchmark.json").write_text(
            json.dumps(benchmark, indent=2, sort_keys=True), encoding="utf-8"
        )

        result = BacktestResult(
            config=self.config,
            manifest=manifest,
            tca=tca,
            replay_summary={
                "events_processed": self.processed,
                "book_events": self.book_events,
                "signals": self.signal_events,
                "plans_submitted": self.plans_submitted,
                "quality": quality_report.to_dict(),
            },
            monitoring=self.metrics.report(),
            reconciliation=reconciliation.to_dict(),
            strategy_summary=self.strategy.summary(),
            router_summary=router_summary,
            risk_summary={
                "utilization": self.risk_engine.utilization(),
                "binding_events": self.risk_engine.binding_events,
                "kill_switch": self.kill_switch.to_dict(),
                "auto_kill": self.supervisor.to_dict(),
            },
            ledger_path=self.ledger_path,
            state_hash=derived.state_hash(),
            benchmark=benchmark,
            output_dir=self.output_dir,
        )
        self.ledger.close()
        self._closed = True
        return result

    def _observe_metrics(
        self,
        *,
        quality_report: Any,
        tca: TCAReport,
        reconciliation_breaks: int,
    ) -> None:
        metrics = self.metrics
        counters = quality_report.to_dict()
        metrics.observe(
            "feed_gap_count", counters.get("by_code", {}).get("sequence_gap", 0)
        )
        metrics.observe(
            "reconnect_count", self.quality_counts.get("snapshot_resync", 0)
        )
        orders = int(tca.totals.get("orders", "0"))
        fills = int(tca.totals.get("fills", "0"))
        rejects = int(tca.totals.get("rejects", "0"))
        metrics.observe(
            "order_reject_rate", Decimal(rejects) / Decimal(orders or 1)
        )
        metrics.observe("fill_rate", Decimal(fills) / Decimal(orders or 1))
        residual = max(
            (item.residual_quantity for item in tca.opportunities),
            default=Decimal(0),
        )
        metrics.observe("residual_notional", residual)
        metrics.observe(
            "residual_duration_ns",
            max(
                (item.residual_duration_ns for item in tca.opportunities),
                default=0,
            ),
        )
        stages = self.latency.report().get("stages", {})
        ack_p99 = max(
            (
                int(value.get("p99_ns", 0))
                for key, value in stages.items()
                if key.endswith("submit_ack") or key.endswith("entry")
            ),
            default=0,
        )
        metrics.observe("ack_latency_p99_ns", ack_p99)
        limit = self.risk_engine.limits.max_gross_notional
        metrics.observe(
            "risk_limit_utilization",
            (self.risk_engine.gross_notional / limit) if limit else Decimal(0),
        )
        metrics.observe("reconciliation_breaks", reconciliation_breaks)
        metrics.observe(
            "missed_opportunities", len(self.router.missed_opportunities)
        )
        metrics.observe("net_pnl", Decimal(tca.totals.get("net_pnl", "0")))


class BacktestRunner:
    """Wires the whole stack and runs one deterministic pass over a dataset."""

    def __init__(
        self,
        config: BacktestConfig,
        streams: Mapping[str, Sequence[CanonicalEvent]],
        output_dir: str | Path,
        *,
        security_master: SecurityMaster | None = None,
        fee_book: FeeBook | None = None,
        dataset_info: Mapping[str, Any] | None = None,
    ) -> None:
        self.config = config
        self.streams = {venue: list(events) for venue, events in streams.items()}
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.security_master = security_master or default_security_master()
        self.fee_book = fee_book or default_fee_book()
        self.dataset_info = dict(dataset_info or {})

    def run(self) -> BacktestResult:
        session = TradingSession(
            self.config,
            self.output_dir,
            security_master=self.security_master,
            fee_book=self.fee_book,
            dataset_info=self.dataset_info,
        )
        replay = MultiVenueReplay(
            {venue: list(events) for venue, events in self.streams.items()},
            controls=self.config.controls,
            stale_after_ns=self.config.stale_after_ns,
            thresholds=QualityThresholds(),
            run_id=self.config.run_id,
        )
        for event in replay.events():
            session.process(event)
        return session.finish(
            quality_report=replay.monitor.report(),
            replay_signature=replay_signature_of(self.streams),
            inputs={
                "venues": sorted(self.streams),
                "event_counts": {
                    venue: len(events)
                    for venue, events in sorted(self.streams.items())
                },
            },
        )


def replay_signature_of(
    streams: Mapping[str, Sequence[CanonicalEvent]]
) -> str:
    import hashlib

    digest = hashlib.sha256()
    for venue in sorted(streams):
        digest.update(venue.encode("utf-8"))
        for event in streams[venue]:
            digest.update(event.event_id.encode("utf-8"))
    return digest.hexdigest()


def build_dataset(
    dataset_root: str | Path,
    *,
    synthetic: SyntheticConfig | None = None,
    security_master: SecurityMaster | None = None,
) -> tuple[Pipeline, dict[str, Any]]:
    """Generate, capture and normalize a hermetic multi-venue dataset."""

    master = security_master or default_security_master()
    pipeline = Pipeline(Path(dataset_root), master)
    records = generate(synthetic)
    pipeline.ingest(records)
    manifest = pipeline.run()
    return pipeline, manifest


def run_backtest(
    config: BacktestConfig,
    output_dir: str | Path,
    *,
    dataset_root: str | Path | None = None,
    synthetic: SyntheticConfig | None = None,
    security_master: SecurityMaster | None = None,
    fee_book: FeeBook | None = None,
) -> BacktestResult:
    """Convenience entry point used by the CLI and the acceptance suite."""

    master = security_master or default_security_master()
    output = Path(output_dir)
    root = Path(dataset_root) if dataset_root is not None else output / "dataset"
    manifest_path = root / "dataset_manifest.json"
    if manifest_path.exists():
        pipeline = Pipeline(root, master)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        pipeline, manifest = build_dataset(
            root, synthetic=synthetic, security_master=master
        )
    streams = pipeline.venue_streams()
    runner = BacktestRunner(
        config,
        streams,
        output,
        security_master=master,
        fee_book=fee_book,
        dataset_info=manifest,
    )
    return runner.run()
