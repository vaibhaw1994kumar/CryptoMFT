from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import Path
import time
from typing import Any

from .models import canonical_json, stable_id, utc_now_iso


class HealthLevel(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class QualityThresholds:
    min_events: int = 1
    max_gaps: int = 0
    max_stale_intervals: int = 0
    max_crossed_books: int = 0
    max_duplicates: int = 0
    max_out_of_order: int = 0
    max_schema_exceptions: int = 0
    max_rejected_partitions: int = 0
    max_negative_latency: int = 0
    max_impossible_latency: int = 0
    max_unknown_instrument_rules: int = 0
    max_persistence_lag_ns: int = 5_000_000_000
    max_unpersisted_bytes: int = 0
    max_price_jumps: int = 0
    max_wide_spreads: int = 0
    max_invalid_levels: int = 0
    max_invalid_trades: int = 0
    max_silent_feeds: int = 0


@dataclass(frozen=True, slots=True)
class QualitySnapshot:
    events: int = 0
    gaps: int = 0
    stale_intervals: int = 0
    crossed_books: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    schema_exceptions: int = 0
    rejected_records: int = 0
    rejected_partitions: int = 0
    negative_latency: int = 0
    impossible_latency: int = 0
    unknown_instrument_rules: int = 0
    persistence_lag_ns: int = 0
    unpersisted_bytes: int = 0
    price_jumps: int = 0
    wide_spreads: int = 0
    invalid_levels: int = 0
    invalid_trades: int = 0
    silent_feeds: int = 0
    corrected_negative_latency: int = 0


@dataclass(frozen=True, slots=True)
class HealthFlag:
    code: str
    level: HealthLevel
    metric: str
    observed: int
    threshold: int
    scope: str
    message: str


class QualityGate:
    def __init__(
        self,
        thresholds: QualityThresholds,
        *,
        clock_ns=time.time_ns,
    ):
        self.thresholds = thresholds
        self.clock_ns = clock_ns
        self.active_flags: tuple[HealthFlag, ...] = ()
        self.health = HealthLevel.HEALTHY
        self.defects: list[dict[str, Any]] = []
        self._defect_keys: set[tuple[str, str, str]] = set()
        self._previous_defect_hash = "0" * 64

    def evaluate(
        self,
        snapshot: QualitySnapshot,
        *,
        scope: str,
        correlation_id: str,
    ) -> tuple[HealthFlag, ...]:
        flags: list[HealthFlag] = []
        if snapshot.events < self.thresholds.min_events:
            flags.append(self._flag(
                "INSUFFICIENT_EVENTS", HealthLevel.CRITICAL, "events",
                snapshot.events, self.thresholds.min_events, scope,
                "event count is below the approval minimum",
            ))
        checks = (
            ("SEQUENCE_GAPS", HealthLevel.CRITICAL, "gaps", snapshot.gaps, self.thresholds.max_gaps),
            ("STALE_INTERVALS", HealthLevel.CRITICAL, "stale_intervals", snapshot.stale_intervals, self.thresholds.max_stale_intervals),
            ("CROSSED_BOOKS", HealthLevel.CRITICAL, "crossed_books", snapshot.crossed_books, self.thresholds.max_crossed_books),
            ("DUPLICATE_EVENTS", HealthLevel.DEGRADED, "duplicates", snapshot.duplicates, self.thresholds.max_duplicates),
            ("OUT_OF_ORDER_EVENTS", HealthLevel.CRITICAL, "out_of_order", snapshot.out_of_order, self.thresholds.max_out_of_order),
            ("SCHEMA_EXCEPTIONS", HealthLevel.CRITICAL, "schema_exceptions", snapshot.schema_exceptions, self.thresholds.max_schema_exceptions),
            ("REJECTED_PARTITIONS", HealthLevel.CRITICAL, "rejected_partitions", snapshot.rejected_partitions, self.thresholds.max_rejected_partitions),
            ("NEGATIVE_LATENCY", HealthLevel.CRITICAL, "negative_latency", snapshot.negative_latency, self.thresholds.max_negative_latency),
            ("IMPOSSIBLE_LATENCY", HealthLevel.CRITICAL, "impossible_latency", snapshot.impossible_latency, self.thresholds.max_impossible_latency),
            ("UNKNOWN_INSTRUMENT_RULES", HealthLevel.CRITICAL, "unknown_instrument_rules", snapshot.unknown_instrument_rules, self.thresholds.max_unknown_instrument_rules),
            ("PERSISTENCE_LAG", HealthLevel.DEGRADED, "persistence_lag_ns", snapshot.persistence_lag_ns, self.thresholds.max_persistence_lag_ns),
            ("UNPERSISTED_BYTES", HealthLevel.DEGRADED, "unpersisted_bytes", snapshot.unpersisted_bytes, self.thresholds.max_unpersisted_bytes),
            ("PRICE_JUMPS", HealthLevel.CRITICAL, "price_jumps", snapshot.price_jumps, self.thresholds.max_price_jumps),
            ("WIDE_SPREADS", HealthLevel.DEGRADED, "wide_spreads", snapshot.wide_spreads, self.thresholds.max_wide_spreads),
            ("INVALID_BOOK_LEVELS", HealthLevel.CRITICAL, "invalid_levels", snapshot.invalid_levels, self.thresholds.max_invalid_levels),
            ("INVALID_TRADES", HealthLevel.DEGRADED, "invalid_trades", snapshot.invalid_trades, self.thresholds.max_invalid_trades),
            ("SILENT_FEEDS", HealthLevel.CRITICAL, "silent_feeds", snapshot.silent_feeds, self.thresholds.max_silent_feeds),
        )
        for code, level, metric, observed, threshold in checks:
            if observed > threshold:
                flags.append(self._flag(
                    code, level, metric, observed, threshold, scope,
                    f"{metric} exceeded configured threshold",
                ))
        self.active_flags = tuple(flags)
        if any(flag.level == HealthLevel.CRITICAL for flag in flags):
            self.health = HealthLevel.CRITICAL
        elif flags:
            self.health = HealthLevel.DEGRADED
        else:
            self.health = HealthLevel.HEALTHY
        for flag in flags:
            self._record_defect(flag, correlation_id)
        return self.active_flags

    @staticmethod
    def _flag(
        code: str,
        level: HealthLevel,
        metric: str,
        observed: int,
        threshold: int,
        scope: str,
        message: str,
    ) -> HealthFlag:
        return HealthFlag(code, level, metric, observed, threshold, scope, message)

    def _record_defect(self, flag: HealthFlag, correlation_id: str) -> None:
        key = (flag.code, flag.scope, correlation_id)
        if key in self._defect_keys:
            return
        self._defect_keys.add(key)
        unsigned = {
            "defect_id": stable_id("quality-defect", *key),
            "detected_ts_ns": self.clock_ns(),
            "correlation_id": correlation_id,
            **asdict(flag),
            "level": flag.level.value,
            "previous_defect_hash": self._previous_defect_hash,
        }
        defect_hash = sha256(canonical_json(unsigned).encode()).hexdigest()
        defect = {**unsigned, "defect_hash": defect_hash}
        self.defects.append(defect)
        self._previous_defect_hash = defect_hash

    def build_report(
        self,
        snapshot: QualitySnapshot,
        *,
        scope: str,
        correlation_id: str,
        limitations: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        self.evaluate(snapshot, scope=scope, correlation_id=correlation_id)
        report = {
            "schema_version": "1.0.0",
            "created_at": utc_now_iso(),
            "scope": scope,
            "correlation_id": correlation_id,
            "decision": "APPROVED" if not self.active_flags else "REJECTED",
            "health": self.health.value,
            "counts": asdict(snapshot),
            "thresholds": asdict(self.thresholds),
            "active_flags": [
                {**asdict(flag), "level": flag.level.value} for flag in self.active_flags
            ],
            "limitations": list(limitations),
            "defect_count": len(self.defects),
            "terminal_defect_hash": self._previous_defect_hash,
        }
        report["approval_attestation_sha256"] = sha256(canonical_json(report).encode()).hexdigest()
        return report

    def write_artifacts(
        self,
        report_path: Path,
        defect_path: Path,
        snapshot: QualitySnapshot,
        *,
        scope: str,
        correlation_id: str,
        limitations: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        report = self.build_report(
            snapshot,
            scope=scope,
            correlation_id=correlation_id,
            limitations=limitations,
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        defect_path.parent.mkdir(parents=True, exist_ok=True)
        with defect_path.open("w", encoding="utf-8") as sink:
            for defect in self.defects:
                sink.write(json.dumps(defect, sort_keys=True) + "\n")
        report["defect_log_sha256"] = sha256(defect_path.read_bytes()).hexdigest()
        report["approval_attestation_sha256"] = sha256(
            canonical_json({k: v for k, v in report.items() if k != "approval_attestation_sha256"}).encode()
        ).hexdigest()
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        return report
