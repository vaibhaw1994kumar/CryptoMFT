from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Iterable


class AlertType(StrEnum):
    FAILED_INGESTION = "FAILED_INGESTION"
    INVALID_BOOK = "INVALID_BOOK"
    REPLAY_FAILURE = "REPLAY_FAILURE"
    RISK_BREACH = "RISK_BREACH"
    PIPELINE_THRESHOLD = "PIPELINE_THRESHOLD"
    EXECUTION_THRESHOLD = "EXECUTION_THRESHOLD"


class AlertSeverity(StrEnum):
    WARNING = "WARNING"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class AlertPolicy:
    alert_type: AlertType
    owner: str
    severity: AlertSeverity
    response_target_minutes: int
    response_expectation: str
    coverage: str = "RESEARCH_HOURS"

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["alert_type"] = self.alert_type.value
        row["severity"] = self.severity.value
        return row


DEFAULT_ALERT_POLICIES = (
    AlertPolicy(
        AlertType.FAILED_INGESTION, "market-data-owner", AlertSeverity.CRITICAL, 30,
        "Stop dependent paper strategies, preserve the failed partition and restart or backfill after diagnosis.",
    ),
    AlertPolicy(
        AlertType.INVALID_BOOK, "market-data-owner", AlertSeverity.CRITICAL, 15,
        "Confirm automatic strategy gating, cancel ordinary orders on the venue and force a clean snapshot resync.",
    ),
    AlertPolicy(
        AlertType.REPLAY_FAILURE, "research-infrastructure-owner", AlertSeverity.HIGH, 240,
        "Quarantine the run, retain its checkpoint and raw lineage, then resume only after a deterministic replay check.",
    ),
    AlertPolicy(
        AlertType.RISK_BREACH, "paper-risk-owner", AlertSeverity.CRITICAL, 15,
        "Confirm the kill state, cancel new-risk orders, permit only approved risk reduction and reconcile before resume.",
    ),
    AlertPolicy(
        AlertType.PIPELINE_THRESHOLD, "research-infrastructure-owner", AlertSeverity.WARNING, 240,
        "Inspect the breached pipeline threshold and record disposition before the next paper session.",
    ),
    AlertPolicy(
        AlertType.EXECUTION_THRESHOLD, "paper-trading-owner", AlertSeverity.HIGH, 30,
        "Inspect the execution threshold, residual and connector state; suspend new risk if safety is uncertain.",
    ),
)


class AlertOwnership:
    """Classify operational signals and attach accountable response policy."""

    _INVALID_BOOK_CODES = {
        "INVALID_BOOKS", "SEQUENCE_GAPS", "DATA_AGE_NS", "STALE_DISCONNECTS",
    }
    _RISK_BREACH_CODES = {
        "ACTIVE_KILL_SWITCHES", "OPEN_RESIDUAL_BASE",
    }

    def __init__(self, policies: Iterable[AlertPolicy] = DEFAULT_ALERT_POLICIES):
        self._policies = {row.alert_type: row for row in policies}
        missing = set(AlertType) - set(self._policies)
        if missing:
            raise ValueError(f"missing alert ownership policies: {sorted(x.value for x in missing)}")

    def matrix(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "coverage_statement": (
                "Research-hours coverage; 24/7 staffing is not required. Every paper alert "
                "has a named owner and response target."
            ),
            "policies": [
                self._policies[key].to_dict() for key in AlertType
            ],
        }

    @staticmethod
    def _classify(code: str, scope: str) -> AlertType:
        if code in AlertOwnership._INVALID_BOOK_CODES:
            return AlertType.INVALID_BOOK
        if code in AlertOwnership._RISK_BREACH_CODES:
            return AlertType.RISK_BREACH
        if scope == "STRATEGY_EXECUTION":
            return AlertType.EXECUTION_THRESHOLD
        return AlertType.PIPELINE_THRESHOLD

    def owned_alert(
        self, row: dict[str, Any], *, alert_type: AlertType | None = None,
        forced: bool = False,
    ) -> dict[str, Any]:
        resolved_type = alert_type or self._classify(
            str(row["code"]), str(row.get("scope", "PIPELINE"))
        )
        policy = self._policies[resolved_type]
        return {
            **row,
            "alert_type": resolved_type.value,
            "owner": policy.owner,
            "severity": policy.severity.value,
            "response_target_minutes": policy.response_target_minutes,
            "response_expectation": policy.response_expectation,
            "coverage": policy.coverage,
            "forced_test": forced,
        }

    def explicit_alerts(
        self, *, mode: str, job: dict[str, Any],
        strategy_execution: dict[str, Any], forced_alert_types: Iterable[str],
    ) -> list[dict[str, Any]]:
        rows: list[tuple[AlertType, bool, str]] = []
        if str(job.get("state", "")).upper() == "FAILED":
            incident = (
                AlertType.REPLAY_FAILURE if mode == "REPLAY"
                else AlertType.FAILED_INGESTION
            )
            rows.append((incident, False, str(job.get("error", "job failed"))))
        kill_state = str(strategy_execution.get("kill_switch", {}).get("state", ""))
        if kill_state == "ACTIVE":
            rows.append((AlertType.RISK_BREACH, False, "risk control is active"))
        for raw_type in forced_alert_types:
            try:
                incident = AlertType(str(raw_type))
            except ValueError as exc:
                raise ValueError(f"unknown forced alert type: {raw_type}") from exc
            rows.append((incident, True, "forced alert control test"))

        result = []
        seen: set[tuple[AlertType, bool]] = set()
        for incident, forced, detail in rows:
            if (incident, forced) in seen:
                continue
            seen.add((incident, forced))
            result.append(self.owned_alert({
                "code": incident.value,
                "health": "CRITICAL",
                "observed": 1,
                "comparator": "<=",
                "threshold": 0,
                "scope": "CONTROL_TEST" if forced else "INCIDENT",
                "detail": detail,
            }, alert_type=incident, forced=forced))
        return result
