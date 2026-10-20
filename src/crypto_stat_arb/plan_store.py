from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from typing import Any

from .models import (
    ChildOrder,
    Opportunity,
    OrderState,
    ParentPlan,
    PlanState,
    Side,
    json_default,
)


PLAN_STORE_SCHEMA_VERSION = "1.2.0"
TERMINAL_PLAN_STATES = (PlanState.CLOSED.value,)


@dataclass(frozen=True, slots=True)
class VenueOrderSnapshot:
    order_id: str
    venue: str
    instrument_id: str
    state: OrderState
    filled_quantity: Decimal
    average_price: Decimal
    fee: Decimal = Decimal("0")
    venue_order_id: str = ""
    observed_ts_ns: int = 0


def _plan_json(plan: ParentPlan) -> str:
    return json.dumps(
        asdict(plan), default=json_default, sort_keys=True, separators=(",", ":")
    )


def restore_child(child: dict[str, Any]) -> ChildOrder:
    """A ``ChildOrder`` from its JSON form (plan snapshots, financing journal)."""
    return ChildOrder(
        order_id=child["order_id"],
        parent_id=child["parent_id"],
        opportunity_id=child["opportunity_id"],
        venue=child["venue"],
        instrument_id=child["instrument_id"],
        side=Side(child["side"]),
        quantity=Decimal(child["quantity"]),
        limit_price=Decimal(child["limit_price"]),
        state=OrderState(child["state"]),
        filled_quantity=Decimal(child["filled_quantity"]),
        average_price=Decimal(child["average_price"]),
        fee=Decimal(child["fee"]),
        reject_reason=child.get("reject_reason"),
        state_updates=list(child.get("state_updates", [])),
        order_role=child.get("order_role", "PRIMARY"),
        reduce_only=bool(child.get("reduce_only", False)),
        unwind_attempt=int(child.get("unwind_attempt", 0)),
        order_type=str(child.get("order_type", "LIMIT")),
        time_in_force=str(child.get("time_in_force", "IOC")),
        post_only=bool(child.get("post_only", False)),
        fee_asset=str(child.get("fee_asset", "")),
        fee_quantity=Decimal(str(child.get("fee_quantity", "0"))),
        fee_rate_bps=(
            Decimal(str(child["fee_rate_bps"])) if child.get("fee_rate_bps") is not None else None
        ),
        liquidity=str(child.get("liquidity", "")),
        fee_source=str(child.get("fee_source", "")),
    )


def _restore_plan(payload: str) -> ParentPlan:
    row = json.loads(payload)
    opportunity_row = row["opportunity"]
    opportunity = Opportunity(
        opportunity_id=opportunity_row["opportunity_id"],
        strategy_id=opportunity_row["strategy_id"],
        instrument_id=opportunity_row["instrument_id"],
        observed_ts_ns=int(opportunity_row["observed_ts_ns"]),
        buy_venue=opportunity_row["buy_venue"],
        sell_venue=opportunity_row["sell_venue"],
        buy_price=Decimal(opportunity_row["buy_price"]),
        sell_price=Decimal(opportunity_row["sell_price"]),
        quantity=Decimal(opportunity_row["quantity"]),
        gross_edge_bps=Decimal(opportunity_row["gross_edge_bps"]),
        estimated_net_edge_bps=Decimal(opportunity_row["estimated_net_edge_bps"]),
        source_event_ids=tuple(opportunity_row["source_event_ids"]),
        eligibility_fingerprint=opportunity_row.get("eligibility_fingerprint", ""),
    )
    children = [restore_child(child) for child in row["children"]]
    return ParentPlan(
        parent_id=row["parent_id"],
        opportunity=opportunity,
        children=children,
        state=PlanState(row["state"]),
        residual_base=Decimal(row["residual_base"]),
        gross_pnl=Decimal(row["gross_pnl"]),
        fees=Decimal(row["fees"]),
        unwind_cost=Decimal(row["unwind_cost"]),
        net_pnl=Decimal(row["net_pnl"]),
        terminal_outcome=row.get("terminal_outcome", ""),
        transitions=list(row.get("transitions", [])),
        residual_since_ts_ns=(
            int(row["residual_since_ts_ns"])
            if row.get("residual_since_ts_ns") is not None else None
        ),
        unwind_attempts=int(row.get("unwind_attempts", 0)),
    )


class ParentPlanStore:
    """SQLite WAL store for atomic parent snapshots and append-only plan events."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS parent_plans (
                parent_id TEXT PRIMARY KEY,
                opportunity_id TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL,
                version INTEGER NOT NULL,
                updated_ts_ns INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS plan_events (
                event_ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                ts_ns INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE(parent_id, version)
            );
        """)
        self.connection.commit()

    def save(
        self,
        plan: ParentPlan,
        ts_ns: int,
        event_type: str,
        **payload: Any,
    ) -> int:
        snapshot = _plan_json(plan)
        payload_json = json.dumps(
            payload, default=json_default, sort_keys=True, separators=(",", ":")
        )
        with self.connection:
            current = self.connection.execute(
                "SELECT version FROM parent_plans WHERE parent_id = ?",
                (plan.parent_id,),
            ).fetchone()
            version = (int(current[0]) if current else 0) + 1
            self.connection.execute("""
                INSERT INTO parent_plans(
                    parent_id, opportunity_id, state, version, updated_ts_ns,
                    snapshot_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(parent_id) DO UPDATE SET
                    state=excluded.state,
                    version=excluded.version,
                    updated_ts_ns=excluded.updated_ts_ns,
                    snapshot_json=excluded.snapshot_json
            """, (
                plan.parent_id,
                plan.opportunity.opportunity_id,
                plan.state.value,
                version,
                ts_ns,
                snapshot,
            ))
            self.connection.execute("""
                INSERT INTO plan_events(
                    parent_id, version, event_type, ts_ns, payload_json
                ) VALUES (?, ?, ?, ?, ?)
            """, (plan.parent_id, version, event_type, ts_ns, payload_json))
        return version

    def get(self, parent_id: str) -> ParentPlan | None:
        row = self.connection.execute(
            "SELECT snapshot_json FROM parent_plans WHERE parent_id = ?",
            (parent_id,),
        ).fetchone()
        return _restore_plan(row[0]) if row else None

    def load_nonterminal(self) -> dict[str, ParentPlan]:
        placeholders = ",".join("?" for _ in TERMINAL_PLAN_STATES)
        rows = self.connection.execute(f"""
            SELECT parent_id, snapshot_json
            FROM parent_plans
            WHERE state NOT IN ({placeholders})
            ORDER BY parent_id
        """, TERMINAL_PLAN_STATES).fetchall()
        return {parent_id: _restore_plan(snapshot) for parent_id, snapshot in rows}

    def load_all(self) -> dict[str, ParentPlan]:
        """Load the latest durable snapshot for complete account reconstruction."""
        rows = self.connection.execute("""
            SELECT parent_id, snapshot_json
            FROM parent_plans
            ORDER BY parent_id
        """).fetchall()
        return {parent_id: _restore_plan(snapshot) for parent_id, snapshot in rows}

    def events(self, parent_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute("""
            SELECT version, event_type, ts_ns, payload_json
            FROM plan_events WHERE parent_id = ? ORDER BY version
        """, (parent_id,)).fetchall()
        return [
            {
                "version": version,
                "event_type": event_type,
                "ts_ns": ts_ns,
                "payload": json.loads(payload),
            }
            for version, event_type, ts_ns, payload in rows
        ]

    def stats_snapshot(self) -> dict[str, int | str]:
        total = self.connection.execute(
            "SELECT COUNT(*) FROM parent_plans"
        ).fetchone()[0]
        placeholders = ",".join("?" for _ in TERMINAL_PLAN_STATES)
        active = self.connection.execute(f"""
            SELECT COUNT(*) FROM parent_plans WHERE state NOT IN ({placeholders})
        """, TERMINAL_PLAN_STATES).fetchone()[0]
        events = self.connection.execute(
            "SELECT COUNT(*) FROM plan_events"
        ).fetchone()[0]
        return {
            "schema_version": PLAN_STORE_SCHEMA_VERSION,
            "plans": int(total),
            "nonterminal_plans": int(active),
            "plan_events": int(events),
        }

    def close(self) -> None:
        self.connection.close()
