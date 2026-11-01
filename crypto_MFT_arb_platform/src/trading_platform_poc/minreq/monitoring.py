"""Operational monitoring: metric registry, thresholds and alert ownership.

Section 8 asks for a named metric list with thresholds, and an alert matrix
where every alert has a severity and an owner. Both are data here so the
runbook, the dashboard and the acceptance report read from one definition.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


class ThresholdDirection(str, Enum):
    ABOVE = "above"
    BELOW = "below"


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    name: str
    description: str
    unit: str
    source: str
    warn: Decimal | None
    critical: Decimal | None
    direction: ThresholdDirection
    owner: str

    def evaluate(self, value: Decimal) -> str:
        def breached(limit: Decimal | None) -> bool:
            if limit is None:
                return False
            if self.direction is ThresholdDirection.ABOVE:
                return value > limit
            return value < limit

        if breached(self.critical):
            return "critical"
        if breached(self.warn):
            return "warning"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "unit": self.unit,
            "source": self.source,
            "warn": None if self.warn is None else str(self.warn),
            "critical": None if self.critical is None else str(self.critical),
            "direction": self.direction.value,
            "owner": self.owner,
        }


METRICS: tuple[MetricDefinition, ...] = (
    MetricDefinition(
        name="feed_gap_count",
        description="Sequence gaps detected per venue and instrument.",
        unit="count",
        source="quality monitor",
        warn=Decimal(1),
        critical=Decimal(5),
        direction=ThresholdDirection.ABOVE,
        owner="platform engineering",
    ),
    MetricDefinition(
        name="feed_staleness_ns",
        description="Time since the last accepted market-data update.",
        unit="nanoseconds",
        source="quality monitor",
        warn=Decimal(1_000_000_000),
        critical=Decimal(5_000_000_000),
        direction=ThresholdDirection.ABOVE,
        owner="platform engineering",
    ),
    MetricDefinition(
        name="reconnect_count",
        description="Feed reconnects and snapshot resyncs in the run.",
        unit="count",
        source="ingestion",
        warn=Decimal(3),
        critical=Decimal(10),
        direction=ThresholdDirection.ABOVE,
        owner="platform engineering",
    ),
    MetricDefinition(
        name="order_reject_rate",
        description="Rejected child orders divided by dispatched child orders.",
        unit="ratio",
        source="ledger",
        warn=Decimal("0.05"),
        critical=Decimal("0.20"),
        direction=ThresholdDirection.ABOVE,
        owner="execution owner",
    ),
    MetricDefinition(
        name="fill_rate",
        description="Filled child orders divided by dispatched child orders.",
        unit="ratio",
        source="ledger",
        warn=Decimal("0.60"),
        critical=Decimal("0.30"),
        direction=ThresholdDirection.BELOW,
        owner="execution owner",
    ),
    MetricDefinition(
        name="residual_notional",
        description="Largest one-sided residual exposure held by a plan.",
        unit="quote currency",
        source="router",
        warn=Decimal(1_000),
        critical=Decimal(2_500),
        direction=ThresholdDirection.ABOVE,
        owner="risk/operations",
    ),
    MetricDefinition(
        name="residual_duration_ns",
        description="Longest time a plan held a one-sided residual.",
        unit="nanoseconds",
        source="router",
        warn=Decimal(2_000_000_000),
        critical=Decimal(5_000_000_000),
        direction=ThresholdDirection.ABOVE,
        owner="risk/operations",
    ),
    MetricDefinition(
        name="ack_latency_p99_ns",
        description="Ninety-ninth percentile submit-to-acknowledge latency.",
        unit="nanoseconds",
        source="latency model",
        warn=Decimal(250_000_000),
        critical=Decimal(1_000_000_000),
        direction=ThresholdDirection.ABOVE,
        owner="execution owner",
    ),
    MetricDefinition(
        name="risk_limit_utilization",
        description="Gross exposure divided by the gross exposure limit.",
        unit="ratio",
        source="risk engine",
        warn=Decimal("0.80"),
        critical=Decimal("0.95"),
        direction=ThresholdDirection.ABOVE,
        owner="risk/operations",
    ),
    MetricDefinition(
        name="reconciliation_breaks",
        description="Open differences between local and venue state.",
        unit="count",
        source="reconciliation",
        warn=Decimal(1),
        critical=Decimal(1),
        direction=ThresholdDirection.ABOVE,
        owner="risk/operations",
    ),
    MetricDefinition(
        name="missed_opportunities",
        description="Signals that never became a parent plan.",
        unit="count",
        source="router",
        warn=Decimal(50),
        critical=Decimal(200),
        direction=ThresholdDirection.ABOVE,
        owner="strategy owner",
    ),
    MetricDefinition(
        name="net_pnl",
        description="Realised net profit after fees, slippage and unwind cost.",
        unit="quote currency",
        source="tca",
        warn=Decimal("-500"),
        critical=Decimal("-2500"),
        direction=ThresholdDirection.BELOW,
        owner="strategy owner",
    ),
)

METRICS_BY_NAME: dict[str, MetricDefinition] = {
    metric.name: metric for metric in METRICS
}


ALERT_MATRIX: tuple[dict[str, str], ...] = (
    {
        "alert": "feed_integrity_degraded",
        "trigger": "feed_gap_count or feed_staleness_ns critical",
        "severity": "critical",
        "owner": "platform engineering",
        "response": "Suspend the venue, force a snapshot resync, and hold new "
        "opportunities until the book is valid again.",
    },
    {
        "alert": "venue_disconnected",
        "trigger": "reconnect_count critical or no data for the staleness limit",
        "severity": "critical",
        "owner": "platform engineering",
        "response": "Cancel open orders on the venue and run reconciliation "
        "before resuming.",
    },
    {
        "alert": "execution_quality_degraded",
        "trigger": "order_reject_rate or fill_rate warning",
        "severity": "high",
        "owner": "execution owner",
        "response": "Review route scoring inputs and venue rate limits.",
    },
    {
        "alert": "residual_exposure_open",
        "trigger": "residual_notional or residual_duration_ns critical",
        "severity": "critical",
        "owner": "risk/operations",
        "response": "Force the unwind policy and escalate if the unwind cannot "
        "complete.",
    },
    {
        "alert": "risk_limit_near_breach",
        "trigger": "risk_limit_utilization warning",
        "severity": "high",
        "owner": "risk/operations",
        "response": "Reduce target size or stop new entries.",
    },
    {
        "alert": "reconciliation_break",
        "trigger": "reconciliation_breaks above zero",
        "severity": "critical",
        "owner": "risk/operations",
        "response": "Freeze the strategy, resolve the break, and only then "
        "clear the kill switch.",
    },
    {
        "alert": "daily_loss_breach",
        "trigger": "net_pnl critical",
        "severity": "critical",
        "owner": "strategy owner",
        "response": "Trip the kill switch and allow risk-reducing flow only.",
    },
)


@dataclass(frozen=True, slots=True)
class MetricSample:
    name: str
    value: Decimal
    status: str
    owner: str
    labels: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": str(self.value),
            "status": self.status,
            "owner": self.owner,
            "labels": self.labels,
        }


class MetricRegistry:
    """Collects named metrics and grades them against declared thresholds."""

    def __init__(self) -> None:
        self._samples: list[MetricSample] = []

    def observe(
        self,
        name: str,
        value: Decimal | int | float,
        labels: Mapping[str, str] | None = None,
    ) -> MetricSample:
        definition = METRICS_BY_NAME.get(name)
        decimal_value = Decimal(str(value))
        if definition is None:
            sample = MetricSample(
                name=name,
                value=decimal_value,
                status="unmonitored",
                owner="unassigned",
                labels=dict(labels or {}),
            )
        else:
            sample = MetricSample(
                name=name,
                value=decimal_value,
                status=definition.evaluate(decimal_value),
                owner=definition.owner,
                labels=dict(labels or {}),
            )
        self._samples.append(sample)
        return sample

    @property
    def samples(self) -> tuple[MetricSample, ...]:
        return tuple(self._samples)

    def alerts(self) -> list[dict[str, Any]]:
        raised: list[dict[str, Any]] = []
        for sample in self._samples:
            if sample.status in {"warning", "critical"}:
                definition = METRICS_BY_NAME.get(sample.name)
                raised.append(
                    {
                        "metric": sample.name,
                        "value": str(sample.value),
                        "status": sample.status,
                        "owner": sample.owner,
                        "labels": sample.labels,
                        "description": (
                            "" if definition is None else definition.description
                        ),
                    }
                )
        return raised

    def report(self) -> dict[str, Any]:
        return {
            "samples": [sample.to_dict() for sample in self._samples],
            "alerts": self.alerts(),
            "worst_status": self.worst_status(),
        }

    def worst_status(self) -> str:
        ranking = {"ok": 0, "unmonitored": 0, "warning": 1, "critical": 2}
        worst = "ok"
        for sample in self._samples:
            if ranking.get(sample.status, 0) > ranking[worst]:
                worst = sample.status
        return worst


def monitoring_document() -> dict[str, Any]:
    """Section 10 deliverable: metric list and alert matrix with owners."""

    return {
        "metrics": [metric.to_dict() for metric in METRICS],
        "alerts": [dict(row) for row in ALERT_MATRIX],
    }


def write_monitoring_document(path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(monitoring_document(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return target
