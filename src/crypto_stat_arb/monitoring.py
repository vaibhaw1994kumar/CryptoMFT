from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from enum import StrEnum
import json
import os
from pathlib import Path
import re
from typing import Any

from .alerting import AlertOwnership
from .metrics import plan_total


class MonitorHealth(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class MonitoringThresholds:
    max_data_age_ns: int = 5_000_000_000
    min_partition_completeness: float = 1.0
    max_gaps: int = 0
    max_invalid_books: int = 0
    max_schema_mismatches: int = 0
    min_replay_events_per_second: float = 1.0
    max_reconnects: int = 5
    max_stale_disconnects: int = 0
    max_persistence_lag_ns: int = 5_000_000_000
    max_order_rejects: int = 0
    max_open_residual_base: float = 0.002
    max_dispatch_latency_ns: int = 10_000_000
    max_rate_limit_utilization: float = 0.90
    max_unhealthy_connectors: int = 0
    max_active_kill_switches: int = 0


@dataclass(frozen=True, slots=True)
class MonitoringMetric:
    name: str
    value: int | float
    unit: str
    comparator: str
    threshold: int | float
    health_on_breach: MonitorHealth
    breached: bool
    scope: str = "PIPELINE"

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["health_on_breach"] = self.health_on_breach.value
        return row


class PipelineMonitor:
    """One operational envelope for replay, orchestration and live feeds."""

    def __init__(
        self, thresholds: MonitoringThresholds,
        alert_ownership: AlertOwnership | None = None,
    ):
        self.thresholds = thresholds
        self.alert_ownership = alert_ownership or AlertOwnership()

    @staticmethod
    def _metric(
        name: str, value: int | float, unit: str, comparator: str,
        threshold: int | float, health: MonitorHealth, scope: str = "PIPELINE",
    ) -> MonitoringMetric:
        breached = value > threshold if comparator == "<=" else value < threshold
        return MonitoringMetric(
            name, value, unit, comparator, threshold, health, breached, scope
        )

    def build(
        self, *, mode: str, generated_ts_ns: int, correlation_id: str,
        job: dict[str, Any], data: dict[str, Any],
        partitions: dict[str, Any], books: dict[str, Any],
        versions: dict[str, Any], performance: dict[str, Any],
        live: dict[str, Any], strategy_execution: dict[str, Any] | None = None,
        tca: dict[str, Any] | None = None,
        structured_logs: dict[str, Any] | None = None,
        restart_resilience: dict[str, Any] | None = None,
        forced_alert_types: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        invalid_books = sum(
            str(row.get("state", "")) != "VALID" for row in books.values()
        )
        gaps = sum(int(row.get("gaps", 0)) for row in books.values())
        data_age = max(
            (int(row.get("age_ns", 0)) for row in books.values()), default=0
        )
        metrics = [
            self._metric("data_age_ns", data_age, "nanoseconds", "<=",
                         self.thresholds.max_data_age_ns, MonitorHealth.CRITICAL),
            self._metric(
                "partition_completeness", float(partitions.get("completeness", 0)),
                "ratio", ">=", self.thresholds.min_partition_completeness,
                MonitorHealth.CRITICAL,
            ),
            self._metric("sequence_gaps", gaps, "count", "<=",
                         self.thresholds.max_gaps, MonitorHealth.CRITICAL),
            self._metric("invalid_books", invalid_books, "count", "<=",
                         self.thresholds.max_invalid_books, MonitorHealth.CRITICAL),
            self._metric(
                "schema_mismatches", int(versions.get("schema_mismatches", 0)),
                "count", "<=", self.thresholds.max_schema_mismatches,
                MonitorHealth.CRITICAL,
            ),
            self._metric(
                "replay_events_per_second",
                float(performance.get("replay_events_per_second", 0)),
                "events_per_second", ">=",
                self.thresholds.min_replay_events_per_second,
                MonitorHealth.DEGRADED,
            ),
            self._metric("reconnects", int(live.get("reconnects", 0)), "count", "<=",
                         self.thresholds.max_reconnects, MonitorHealth.DEGRADED),
            self._metric(
                "stale_disconnects", int(live.get("stale_disconnects", 0)),
                "count", "<=", self.thresholds.max_stale_disconnects,
                MonitorHealth.CRITICAL,
            ),
            self._metric(
                "persistence_lag_ns", int(live.get("persistence_lag_ns", 0)),
                "nanoseconds", "<=", self.thresholds.max_persistence_lag_ns,
                MonitorHealth.DEGRADED,
            ),
        ]
        if mode != "REPLAY":
            metrics = [m for m in metrics if m.name != "replay_events_per_second"]
        strategy_execution = strategy_execution or {}
        tca = tca or {}
        structured_logs = structured_logs or {}
        restart_resilience = restart_resilience or {}
        if restart_resilience:
            session = restart_resilience.get("session", {})
            resilience_connectors = restart_resilience.get("connectors", [])
            owner_valid = bool(session.get("owner_id")) and generated_ts_ns <= int(
                session.get("lease_expires_ts_ns") or -1
            )
            resume_ready = (
                session.get("state") == "READY"
                and not bool(session.get("reconciliation_required", 1))
            )
            disconnected = sum(
                row.get("state") != "CONNECTED" for row in resilience_connectors
            )
            metrics.extend([
                self._metric(
                    "active_order_owner", int(owner_valid), "boolean", ">=", 1,
                    MonitorHealth.CRITICAL, "RESTART_RESILIENCE",
                ),
                self._metric(
                    "restart_resume_ready", int(resume_ready), "boolean", ">=", 1,
                    MonitorHealth.CRITICAL, "RESTART_RESILIENCE",
                ),
                self._metric(
                    "restart_disconnected_connectors", disconnected, "count", "<=", 0,
                    MonitorHealth.CRITICAL, "RESTART_RESILIENCE",
                ),
            ])
        if strategy_execution:
            orders = strategy_execution.get("orders", {})
            residual = strategy_execution.get("residual", {})
            latency = strategy_execution.get("latency", {})
            rate_limits = strategy_execution.get("rate_limits", {})
            connectors = strategy_execution.get("connectors", {})
            kill_switch = strategy_execution.get("kill_switch", {})
            metrics.extend([
                self._metric(
                    "order_rejects", int(orders.get("rejects", 0)), "count", "<=",
                    self.thresholds.max_order_rejects, MonitorHealth.CRITICAL,
                    "STRATEGY_EXECUTION",
                ),
                self._metric(
                    "open_residual_base",
                    float(residual.get("open_residual_base", 0)), "base_quantity", "<=",
                    self.thresholds.max_open_residual_base, MonitorHealth.CRITICAL,
                    "STRATEGY_EXECUTION",
                ),
                self._metric(
                    "total_residual_base",
                    float(residual.get("total_residual_base", 0)), "base_quantity", "<=",
                    self.thresholds.max_open_residual_base, MonitorHealth.CRITICAL,
                    "STRATEGY_EXECUTION",
                ),
                self._metric(
                    "max_dispatch_latency_ns",
                    int(latency.get("max_dispatch_duration_ns", 0)), "nanoseconds", "<=",
                    self.thresholds.max_dispatch_latency_ns, MonitorHealth.DEGRADED,
                    "STRATEGY_EXECUTION",
                ),
                self._metric(
                    "rate_limit_utilization",
                    float(rate_limits.get("max_utilization", 0)), "ratio", "<=",
                    self.thresholds.max_rate_limit_utilization, MonitorHealth.DEGRADED,
                    "STRATEGY_EXECUTION",
                ),
                self._metric(
                    "unhealthy_connectors",
                    int(connectors.get("unhealthy", 0)), "count", "<=",
                    self.thresholds.max_unhealthy_connectors, MonitorHealth.CRITICAL,
                    "STRATEGY_EXECUTION",
                ),
                self._metric(
                    "active_kill_switches",
                    int(kill_switch.get("state") == "ACTIVE"), "count", "<=",
                    self.thresholds.max_active_kill_switches, MonitorHealth.CRITICAL,
                    "STRATEGY_EXECUTION",
                ),
            ])
        alerts = [
            self.alert_ownership.owned_alert({
                "code": metric.name.upper(),
                "health": metric.health_on_breach.value,
                "observed": metric.value,
                "comparator": metric.comparator,
                "threshold": metric.threshold,
                "scope": metric.scope,
            })
            for metric in metrics if metric.breached
        ]
        alerts.extend(self.alert_ownership.explicit_alerts(
            mode=mode, job=job, strategy_execution=strategy_execution,
            forced_alert_types=forced_alert_types,
        ))
        health = MonitorHealth.HEALTHY
        if any(x["health"] == MonitorHealth.CRITICAL.value for x in alerts):
            health = MonitorHealth.CRITICAL
        elif alerts:
            health = MonitorHealth.DEGRADED
        return {
            "schema_version": "1.0.0",
            "generated_ts_ns": generated_ts_ns,
            "correlation_id": correlation_id,
            "mode": mode,
            "health": health.value,
            "job": job,
            "data": data,
            "partitions": partitions,
            "books": books,
            "versions": versions,
            "performance": performance,
            "live": live,
            "strategy_execution": strategy_execution,
            "tca": tca,
            "structured_logs": structured_logs,
            "restart_resilience": restart_resilience,
            "alert_ownership": self.alert_ownership.matrix(),
            "thresholds": asdict(self.thresholds),
            "metrics": [metric.to_dict() for metric in metrics],
            "alerts": alerts,
        }

    @staticmethod
    def strategy_execution_snapshot(
        oms: Any, *, account_state: dict[str, Any],
        margin_status: dict[str, Any], kill_switch_status: dict[str, Any],
        session_mode: str = "PAPER_SIMULATION",
    ) -> dict[str, Any]:
        """Aggregate the correlated paper/backtest OMS state into monitoring."""
        plans = list(oms.plans.values())
        children = [order for plan in plans for order in plan.children]
        edges = [plan.opportunity.estimated_net_edge_bps for plan in plans]
        state_counts: dict[str, int] = {}
        for order in children:
            state_counts[order.state.value] = state_counts.get(order.state.value, 0) + 1

        route_decisions = [
            row for row in oms.router.trace if row.get("record_type") == "ROUTE_DECISION"
        ]
        routes_by_venue: dict[str, int] = {}
        for row in route_decisions:
            venue = row.get("selected_venue")
            if venue:
                routes_by_venue[str(venue)] = routes_by_venue.get(str(venue), 0) + 1

        dispatch = oms.dispatch_stats_snapshot()
        callback_latencies = [
            item.first_callback_observed_ts_ns - item.dispatch_started_ts_ns
            for item in oms.dispatch_measurements
            if item.first_callback_observed_ts_ns is not None
        ]
        rate_limit_state = oms.router.rate_limit_snapshot()
        utilization_by_venue: dict[str, float] = {}
        for venue, row in rate_limit_state["venues"].items():
            capacity = int(row["capacity"])
            utilization_by_venue[venue] = (
                int(row["used"]) / capacity if capacity else 1.0
            )
        connectors = {
            venue: {
                "account_enabled": status.account_enabled,
                "venue_enabled": status.venue_enabled,
                "healthy": status.account_enabled and status.venue_enabled,
                "supported_order_types": sorted(x.value for x in status.supported_order_types),
                "supports_post_only": status.supports_post_only,
                "supports_reduce_only": status.supports_reduce_only,
            }
            for venue, status in sorted(oms.router.route_status.items())
        }
        residual = oms.residual_risk_snapshot()
        return {
            "schema_version": "1.0.0",
            "session_mode": session_mode,
            "opportunities": {
                "count": len(plans),
                "expected_net_edge_bps": {
                    "minimum": str(min(edges)) if edges else "0",
                    "maximum": str(max(edges)) if edges else "0",
                    "average": str(
                        sum(edges, Decimal("0")) / len(edges) if edges else Decimal("0")
                    ),
                },
            },
            "routing": {
                "route_decisions": len(route_decisions),
                "route_rejections": sum(row.get("selected_venue") is None for row in route_decisions),
                "route_overrides": sum(row.get("route_override") is not None for row in route_decisions),
                "routes_by_venue": dict(sorted(routes_by_venue.items())),
            },
            "orders": {
                "total": len(children),
                "states": dict(sorted(state_counts.items())),
                "fills": state_counts.get("FILLED", 0),
                "rejects": state_counts.get("REJECTED", 0),
                "cancels": state_counts.get("CANCELLED", 0),
            },
            "residual": residual,
            "latency": {
                **dispatch,
                "min_first_callback_latency_ns": min(callback_latencies) if callback_latencies else 0,
                "max_first_callback_latency_ns": max(callback_latencies) if callback_latencies else 0,
            },
            "pnl": {
                "gross": str(plan_total(plans, "gross_pnl")),
                "fees": str(plan_total(plans, "fees")),
                "net": str(plan_total(plans, "net_pnl")),
            },
            "rate_limits": {
                **rate_limit_state,
                "utilization_by_venue": utilization_by_venue,
                "max_utilization": max(utilization_by_venue.values(), default=0.0),
            },
            "connectors": {
                "unhealthy": sum(not row["healthy"] for row in connectors.values()),
                "venues": connectors,
            },
            "account": account_state,
            "margin": margin_status,
            "kill_switch": kill_switch_status,
        }

    @staticmethod
    def write(report: dict[str, Any], json_path: Path, prometheus_path: Path) -> None:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = json_path.with_suffix(json_path.suffix + ".tmp")
        temporary.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, json_path)
        lines = [
            "# HELP crypto_pipeline_monitor_health Pipeline health by level.",
            "# TYPE crypto_pipeline_monitor_health gauge",
        ]
        for level in MonitorHealth:
            lines.append(
                f'crypto_pipeline_monitor_health{{level="{level.value}"}} '
                f'{1 if report["health"] == level.value else 0}'
            )
        for metric in report["metrics"]:
            name = re.sub(r"[^a-zA-Z0-9_]", "_", metric["name"])
            lines.extend([
                f"# TYPE crypto_pipeline_{name} gauge",
                f'crypto_pipeline_{name}{{mode="{report["mode"]}"}} {metric["value"]}',
            ])
        lines.extend([
            "# TYPE crypto_pipeline_alerts gauge",
            f'crypto_pipeline_alerts{{mode="{report["mode"]}"}} {len(report["alerts"])}',
        ])
        alert_counts: dict[tuple[str, str, str], int] = {}
        for alert in report["alerts"]:
            key = (alert["alert_type"], alert["owner"], alert["severity"])
            alert_counts[key] = alert_counts.get(key, 0) + 1
        for (alert_type, owner, severity), count in sorted(alert_counts.items()):
            lines.append(
                "crypto_pipeline_alerts_by_owner"
                f'{{alert_type="{alert_type}",owner="{owner}",severity="{severity}"}} {count}'
            )
        strategy = report.get("strategy_execution", {})
        if strategy:
            opportunities = strategy["opportunities"]
            routing = strategy["routing"]
            orders = strategy["orders"]
            pnl = strategy["pnl"]
            observed = {
                "opportunity_count": opportunities["count"],
                "expected_net_edge_bps_average": opportunities["expected_net_edge_bps"]["average"],
                "route_decisions": routing["route_decisions"],
                "route_rejections": routing["route_rejections"],
                "route_overrides": routing["route_overrides"],
                "order_count": orders["total"],
                "order_fills": orders["fills"],
                "order_cancels": orders["cancels"],
                "gross_pnl": pnl["gross"],
                "fees": pnl["fees"],
                "net_pnl": pnl["net"],
                "position_count": len(strategy["account"].get("positions", {})),
            }
            for name, value in observed.items():
                lines.extend([
                    f"# TYPE crypto_strategy_{name} gauge",
                    f'crypto_strategy_{name}{{mode="{report["mode"]}"}} {value}',
                ])
            for state, count in orders["states"].items():
                lines.append(
                    'crypto_strategy_orders_by_state'
                    f'{{mode="{report["mode"]}",state="{state}"}} {count}'
                )
        tca = report.get("tca", {})
        if tca:
            summary = tca["summary"]
            for name in (
                "opportunities_detected", "opportunities_executed",
                "missed_opportunities", "fills", "rejects", "cancels",
                "unwind_orders", "open_residual_base", "weighted_slippage_bps",
                "fees", "net_pnl",
            ):
                lines.extend([
                    f"# TYPE crypto_tca_{name} gauge",
                    f'crypto_tca_{name}{{scope="{tca["scope"]}"}} {summary[name]}',
                ])
        structured_logs = report.get("structured_logs", {})
        if structured_logs:
            log_metrics = {
                "records": structured_logs.get("records", 0),
                "errors": structured_logs.get("levels", {}).get("ERROR", 0),
                "warnings": structured_logs.get("levels", {}).get("WARNING", 0),
                "failed_opportunities": structured_logs.get("failed_opportunities", 0),
            }
            for name, value in log_metrics.items():
                lines.extend([
                    f"# TYPE crypto_structured_logs_{name} gauge",
                    f'crypto_structured_logs_{name}{{mode="{report["mode"]}"}} {value}',
                ])
        temporary_prom = prometheus_path.with_suffix(prometheus_path.suffix + ".tmp")
        temporary_prom.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temporary_prom, prometheus_path)
