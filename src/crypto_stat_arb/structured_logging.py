from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from enum import StrEnum
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Iterator

from .models import MarketEvent, ParentPlan, json_default, stable_id



@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    """Commit (or roll back) and *close*: ``with sqlite3.connect(...)`` alone leaves the
    connection open, which keeps the file locked on Windows."""
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()

class StructuredLogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class StructuredLogRecord:
    log_id: str
    ts_ns: int
    level: StructuredLogLevel
    component: str
    event_type: str
    message: str
    correlation_id: str
    host: str = ""
    venue: str = ""
    instrument_id: str = ""
    source_event_id: str = ""
    source_event_ids: tuple[str, ...] = ()
    raw_ref: str = ""
    feature_state_id: str = ""
    opportunity_id: str = ""
    risk_decision_id: str = ""
    reservation_id: str = ""
    parent_plan_id: str = ""
    child_order_id: str = ""
    request_id: str = ""
    route_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "1.0.0"

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["level"] = self.level.value
        row["source_event_ids"] = list(self.source_event_ids)
        return row


class StructuredLogStore:
    """JSONL plus indexed SQLite store shared by connectors and paper hosts."""

    _COLUMNS = (
        "log_id", "ts_ns", "level", "component", "event_type", "message",
        "correlation_id", "host", "venue", "instrument_id", "source_event_id",
        "source_event_ids", "raw_ref", "feature_state_id", "opportunity_id",
        "risk_decision_id", "reservation_id", "parent_plan_id",
        "child_order_id", "request_id", "route_id", "payload", "schema_version",
    )

    def __init__(self, jsonl_path: Path, database_path: Path) -> None:
        self.jsonl_path = jsonl_path
        self.database_path = database_path
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _initialize(self) -> None:
        with _connect(self.database_path) as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS structured_logs (
                    log_id TEXT PRIMARY KEY,
                    ts_ns INTEGER NOT NULL,
                    level TEXT NOT NULL,
                    component TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    host TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    source_event_ids TEXT NOT NULL,
                    raw_ref TEXT NOT NULL,
                    feature_state_id TEXT NOT NULL,
                    opportunity_id TEXT NOT NULL,
                    risk_decision_id TEXT NOT NULL,
                    reservation_id TEXT NOT NULL,
                    parent_plan_id TEXT NOT NULL,
                    child_order_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    schema_version TEXT NOT NULL
                )
            """)
            for column in (
                "ts_ns", "level", "component", "event_type", "correlation_id",
                "venue", "source_event_id", "feature_state_id", "opportunity_id",
                "risk_decision_id", "parent_plan_id", "child_order_id", "request_id",
            ):
                connection.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_logs_{column} "
                    f"ON structured_logs({column})"
                )

    @staticmethod
    def _encoded(record: StructuredLogRecord) -> tuple[Any, ...]:
        row = record.to_dict()
        row["source_event_ids"] = json.dumps(row["source_event_ids"], separators=(",", ":"))
        row["payload"] = json.dumps(
            row["payload"], default=json_default, sort_keys=True, separators=(",", ":")
        )
        return tuple(row[column] for column in StructuredLogStore._COLUMNS)

    def append(self, record: StructuredLogRecord) -> bool:
        row = record.to_dict()
        serialized = json.dumps(
            row, default=json_default, sort_keys=True, separators=(",", ":")
        )
        with _connect(self.database_path) as connection:
            inserted = connection.execute(
                f"INSERT OR IGNORE INTO structured_logs ({','.join(self._COLUMNS)}) "
                f"VALUES ({','.join('?' for _ in self._COLUMNS)})",
                self._encoded(record),
            ).rowcount
        if not inserted:
            return False
        with self.jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(serialized + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return True

    def replace(self, records: Iterable[StructuredLogRecord]) -> dict[str, Any]:
        ordered = [
            row for _, row in sorted(
                enumerate(records), key=lambda pair: (pair[1].ts_ns, pair[0])
            )
        ]
        with _connect(self.database_path) as connection:
            connection.execute("DELETE FROM structured_logs")
            connection.executemany(
                f"INSERT INTO structured_logs ({','.join(self._COLUMNS)}) "
                f"VALUES ({','.join('?' for _ in self._COLUMNS)})",
                [self._encoded(record) for record in ordered],
            )
        temporary = self.jsonl_path.with_suffix(self.jsonl_path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            for record in ordered:
                handle.write(json.dumps(
                    record.to_dict(), default=json_default, sort_keys=True,
                    separators=(",", ":"),
                ) + "\n")
        os.replace(temporary, self.jsonl_path)
        return self.stats()

    def stats(self) -> dict[str, Any]:
        with _connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            total = connection.execute("SELECT COUNT(*) AS count FROM structured_logs").fetchone()[0]
            levels = {
                row["level"]: row["count"] for row in connection.execute(
                    "SELECT level, COUNT(*) AS count FROM structured_logs GROUP BY level"
                )
            }
            components = {
                row["component"]: row["count"] for row in connection.execute(
                    "SELECT component, COUNT(*) AS count FROM structured_logs GROUP BY component"
                )
            }
            failed = connection.execute(
                "SELECT COUNT(DISTINCT opportunity_id) FROM structured_logs "
                "WHERE opportunity_id <> '' AND event_type = 'OPPORTUNITY_OUTCOME' "
                "AND level = 'ERROR'"
            ).fetchone()[0]
        return {
            "schema_version": "1.0.0",
            "records": total,
            "levels": dict(sorted(levels.items())),
            "components": dict(sorted(components.items())),
            "failed_opportunities": failed,
            "jsonl": self.jsonl_path.name,
            "database": self.database_path.name,
            "searchable_fields": [
                "correlation_id", "component", "level", "event_type", "venue",
                "source_event_id", "feature_state_id", "opportunity_id",
                "risk_decision_id", "parent_plan_id", "child_order_id", "request_id",
            ],
        }

    def query(
        self, *, correlation_id: str | None = None,
        opportunity_id: str | None = None, parent_plan_id: str | None = None,
        child_order_id: str | None = None, source_event_id: str | None = None,
        feature_state_id: str | None = None, risk_decision_id: str | None = None,
        request_id: str | None = None,
        component: str | None = None, level: str | None = None,
        event_type: str | None = None, venue: str | None = None,
        text: str | None = None, limit: int = 1000,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 10_000:
            raise ValueError("limit must be between 1 and 10000")
        filters = {
            "correlation_id": correlation_id, "opportunity_id": opportunity_id,
            "parent_plan_id": parent_plan_id, "child_order_id": child_order_id,
            "source_event_id": source_event_id, "component": component,
            "feature_state_id": feature_state_id,
            "risk_decision_id": risk_decision_id, "request_id": request_id,
            "level": level, "event_type": event_type, "venue": venue,
        }
        clauses = []
        values: list[Any] = []
        for column, value in filters.items():
            if value is not None:
                clauses.append(f"{column} = ?")
                values.append(value)
        if text is not None:
            clauses.append("(message LIKE ? OR payload LIKE ?)")
            values.extend([f"%{text}%", f"%{text}%"])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with _connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"SELECT * FROM structured_logs{where} ORDER BY ts_ns, rowid LIMIT ?",
                [*values, limit],
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["source_event_ids"] = json.loads(item["source_event_ids"])
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    def trace_opportunity(self, opportunity_id: str) -> list[dict[str, Any]]:
        records = self.query(opportunity_id=opportunity_id, limit=10_000)
        source_ids = sorted({
            event_id for record in records for event_id in record["source_event_ids"]
        })
        seen = {record["log_id"] for record in records}
        for source_event_id in source_ids:
            for record in self.query(source_event_id=source_event_id, limit=10_000):
                if record["log_id"] not in seen:
                    records.append(record)
                    seen.add(record["log_id"])
        return sorted(records, key=lambda row: row["ts_ns"])

    def write_failed_opportunity_traces(
        self, plans: Iterable[ParentPlan], path: Path
    ) -> int:
        failed = sorted(
            (plan for plan in plans if plan.terminal_outcome != "FILLED"),
            key=lambda plan: plan.opportunity.opportunity_id,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            for plan in failed:
                records = self.trace_opportunity(plan.opportunity.opportunity_id)
                handle.write(json.dumps({
                    "schema_version": "1.0.0",
                    "opportunity_id": plan.opportunity.opportunity_id,
                    "parent_plan_id": plan.parent_id,
                    "terminal_outcome": plan.terminal_outcome or "OPEN",
                    "record_count": len(records),
                    "records": records,
                }, sort_keys=True, separators=(",", ":")) + "\n")
        os.replace(temporary, path)
        return len(failed)

    @staticmethod
    def connector_record(
        *, session_id: str, ts_ns: int, event_type: str, level: StructuredLogLevel,
        message: str, venue: str = "", host: str = "", payload: dict[str, Any] | None = None,
        instrument_id: str = "", source_event_id: str = "", raw_ref: str = "",
    ) -> StructuredLogRecord:
        payload = payload or {}
        return StructuredLogRecord(
            log_id=stable_id(
                "structured-log", session_id, "CONNECTOR", event_type, ts_ns,
                venue, host, message,
            ),
            ts_ns=ts_ns, level=level, component="CONNECTOR",
            event_type=event_type, message=message, correlation_id=session_id,
            host=host, venue=venue, payload=payload,
            instrument_id=instrument_id, source_event_id=source_event_id,
            source_event_ids=((source_event_id,) if source_event_id else ()),
            raw_ref=raw_ref,
        )


class BacktestLogBuilder:
    """Projects canonical, strategy, risk, OMS and adapter records into one trace."""

    @staticmethod
    def _level(event_type: str, payload: dict[str, Any]) -> StructuredLogLevel:
        if event_type == "ROUTE_DECISION" and payload.get("selected_venue") is None:
            return StructuredLogLevel.ERROR
        value = " ".join((event_type, str(payload.get("state", "")),
                          str(payload.get("decision", ""))))
        if any(token in value for token in ("REJECT", "FAILED", "BREACH", "TIMED_OUT")):
            return StructuredLogLevel.ERROR
        if any(token in value for token in ("CANCEL", "PARTIAL", "STALE", "GAP")):
            return StructuredLogLevel.WARNING
        return StructuredLogLevel.INFO

    @classmethod
    def build(
        cls, *, correlation_id: str, events: Iterable[MarketEvent], ledger: Any,
        router: Any, plans: Iterable[ParentPlan], host: str = "paper-host",
    ) -> list[StructuredLogRecord]:
        event_rows = list(events)
        plan_rows = list(plans)
        event_map = {event.event_id: event for event in event_rows}
        opportunity_map = {
            plan.opportunity.opportunity_id: plan for plan in plan_rows
        }
        feature_ids = {
            opportunity_id: stable_id(
                "feature-state", opportunity_id,
                *plan.opportunity.source_event_ids,
                plan.opportunity.eligibility_fingerprint,
            )
            for opportunity_id, plan in opportunity_map.items()
        }
        risk_ids: dict[str, str] = {}
        reservation_ids: dict[str, str] = {}
        for event in ledger.events:
            opportunity_id = str(event.get("opportunity_id", ""))
            if event.get("event_type") == "PRE_TRADE_VALIDATION":
                risk_ids[opportunity_id] = str(
                    event.get("validation", {}).get("validation_id", "")
                )
            if event.get("event_type") == "PACKAGE_RISK_RESERVED":
                reservation_ids[opportunity_id] = str(
                    event.get("reservation", {}).get("reservation_id", "")
                )

        records: list[StructuredLogRecord] = []
        for event in event_rows:
            records.append(StructuredLogRecord(
                log_id=stable_id("structured-log", correlation_id, "SOURCE_EVENT", event.event_id),
                ts_ns=event.receive_ts_ns, level=StructuredLogLevel.INFO,
                component="MARKET_DATA", event_type="SOURCE_EVENT",
                message="canonical source event available",
                correlation_id=correlation_id, host=host, venue=event.venue,
                instrument_id=event.instrument_id, source_event_id=event.event_id,
                source_event_ids=(event.event_id,), raw_ref=event.raw_ref,
                payload={
                    "exchange_ts_ns": event.exchange_ts_ns,
                    "receive_ts_ns": event.receive_ts_ns,
                    "sequence": event.sequence,
                    "quality_flags": list(event.quality_flags),
                },
            ))
        for plan in plan_rows:
            opportunity = plan.opportunity
            feature_id = feature_ids[opportunity.opportunity_id]
            raw_refs = {
                event_id: event_map[event_id].raw_ref
                for event_id in opportunity.source_event_ids
                if event_id in event_map and event_map[event_id].raw_ref
            }
            records.append(StructuredLogRecord(
                log_id=stable_id("structured-log", correlation_id, "FEATURE_STATE", feature_id),
                ts_ns=opportunity.observed_ts_ns, level=StructuredLogLevel.INFO,
                component="STRATEGY", event_type="FEATURE_STATE",
                message="cross-venue feature state evaluated",
                correlation_id=correlation_id, host=host,
                instrument_id=opportunity.instrument_id,
                source_event_ids=opportunity.source_event_ids,
                feature_state_id=feature_id,
                opportunity_id=opportunity.opportunity_id,
                parent_plan_id=plan.parent_id,
                payload={
                    "buy_venue": opportunity.buy_venue,
                    "sell_venue": opportunity.sell_venue,
                    "buy_price": opportunity.buy_price,
                    "sell_price": opportunity.sell_price,
                    "gross_edge_bps": opportunity.gross_edge_bps,
                    "estimated_net_edge_bps": opportunity.estimated_net_edge_bps,
                    "eligibility_fingerprint": opportunity.eligibility_fingerprint,
                    "raw_refs_by_source_event": raw_refs,
                },
            ))
            for child in plan.children:
                records.append(StructuredLogRecord(
                    log_id=stable_id(
                        "structured-log", correlation_id, "CHILD_ORDER_CREATED",
                        child.order_id,
                    ),
                    ts_ns=opportunity.observed_ts_ns,
                    level=StructuredLogLevel.INFO, component="PAPER_HOST",
                    event_type="CHILD_ORDER_CREATED",
                    message="child order created",
                    correlation_id=correlation_id, host=host,
                    venue=child.venue, instrument_id=child.instrument_id,
                    source_event_ids=opportunity.source_event_ids,
                    feature_state_id=feature_id,
                    opportunity_id=opportunity.opportunity_id,
                    risk_decision_id=risk_ids.get(opportunity.opportunity_id, ""),
                    reservation_id=reservation_ids.get(opportunity.opportunity_id, ""),
                    parent_plan_id=plan.parent_id,
                    child_order_id=child.order_id,
                    payload={
                        "side": child.side.value, "quantity": child.quantity,
                        "limit_price": child.limit_price,
                        "order_type": child.order_type,
                        "order_role": child.order_role,
                    },
                ))

        for event in ledger.events:
            event_type = str(event["event_type"])
            nested_opportunity = event.get("opportunity", {})
            opportunity_id = str(
                event.get("opportunity_id", nested_opportunity.get("opportunity_id", ""))
            )
            plan = opportunity_map.get(opportunity_id)
            source_ids = plan.opportunity.source_event_ids if plan else ()
            payload = dict(event)
            records.append(StructuredLogRecord(
                log_id=str(event["ledger_event_id"]), ts_ns=int(event["ts_ns"]),
                level=cls._level(event_type, event), component="PAPER_HOST",
                event_type=event_type, message=event_type.replace("_", " ").lower(),
                correlation_id=correlation_id, host=host,
                venue=str(event.get("venue", "")),
                instrument_id=str(event.get(
                    "instrument_id", nested_opportunity.get("instrument_id", "")
                )),
                source_event_ids=source_ids,
                feature_state_id=feature_ids.get(opportunity_id, ""),
                opportunity_id=opportunity_id,
                risk_decision_id=risk_ids.get(opportunity_id, ""),
                reservation_id=reservation_ids.get(opportunity_id, ""),
                parent_plan_id=str(event.get("parent_id", "")),
                child_order_id=str(event.get("order_id", "")),
                request_id=str(event.get("request_id", "")), payload=payload,
            ))

        for ordinal, row in enumerate(router.trace):
            request = row.get("request", {})
            opportunity_id = str(
                request.get("parent_opportunity_id", row.get("parent_opportunity_id", ""))
            )
            if not opportunity_id and row.get("route_id"):
                matching = next((
                    plan for plan in plan_rows if any(
                        child.order_id == request.get("order_id") for child in plan.children
                    )
                ), None)
                opportunity_id = matching.opportunity.opportunity_id if matching else ""
            plan = opportunity_map.get(opportunity_id)
            venue = str(request.get("venue", row.get("selected_venue") or row.get("venue", "")))
            event_type = str(row.get("record_type", "CONNECTOR_EVENT"))
            ts_ns = int(row.get("ts_ns", request.get("created_ts_ns", 0)))
            records.append(StructuredLogRecord(
                log_id=stable_id(
                    "structured-log", correlation_id, "EXECUTION_CONNECTOR",
                    ordinal, event_type, row.get("route_id", ""),
                    request.get("request_id", ""),
                ),
                ts_ns=ts_ns, level=cls._level(event_type, row),
                component="EXECUTION_CONNECTOR", event_type=event_type,
                message=event_type.replace("_", " ").lower(),
                correlation_id=correlation_id,
                host=f"paper-adapter:{venue}" if venue else "paper-adapter",
                venue=venue,
                instrument_id=str(request.get("instrument_id", row.get("instrument_id", ""))),
                source_event_ids=(plan.opportunity.source_event_ids if plan else ()),
                feature_state_id=feature_ids.get(opportunity_id, ""),
                opportunity_id=opportunity_id,
                risk_decision_id=risk_ids.get(opportunity_id, ""),
                reservation_id=reservation_ids.get(opportunity_id, ""),
                parent_plan_id=str(request.get("parent_order_id", row.get("parent_order_id", ""))),
                child_order_id=str(request.get("order_id", row.get("order_id", ""))),
                request_id=str(request.get("request_id", "")),
                route_id=str(row.get("route_id", "")), payload=dict(row),
            ))

        for plan in plan_rows:
            opportunity_id = plan.opportunity.opportunity_id
            failed = plan.terminal_outcome != "FILLED"
            records.append(StructuredLogRecord(
                log_id=stable_id("structured-log", correlation_id, "OPPORTUNITY_OUTCOME", opportunity_id),
                ts_ns=int(plan.transitions[-1]["ts_ns"] if plan.transitions else plan.opportunity.observed_ts_ns),
                level=(StructuredLogLevel.ERROR if failed else StructuredLogLevel.INFO),
                component="PAPER_HOST", event_type="OPPORTUNITY_OUTCOME",
                message=("opportunity failed" if failed else "opportunity completed"),
                correlation_id=correlation_id, host=host,
                instrument_id=plan.opportunity.instrument_id,
                source_event_ids=plan.opportunity.source_event_ids,
                feature_state_id=feature_ids[opportunity_id],
                opportunity_id=opportunity_id,
                risk_decision_id=risk_ids.get(opportunity_id, ""),
                reservation_id=reservation_ids.get(opportunity_id, ""),
                parent_plan_id=plan.parent_id,
                payload={
                    "state": plan.state.value,
                    "terminal_outcome": plan.terminal_outcome or "OPEN",
                    "reason": plan.transitions[-1].get("reason", "") if plan.transitions else "",
                    "child_order_ids": [child.order_id for child in plan.children],
                    "residual_base": plan.residual_base,
                    "net_pnl": plan.net_pnl,
                },
            ))
        return records
