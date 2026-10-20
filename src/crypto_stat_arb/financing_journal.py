"""Durable financing journal: funding and borrow survive an OMS restart (audit P1).

Trade fills are rebuilt from the plan store on restart; financing is not part
of any plan. This journal lives in the same SQLite WAL database as the plan
store and holds:

* every FUNDING / BORROW_* event and their REVERSALs, in booking order;
* fills that belong to no plan, with their economic type: venue buy-ins
  (BORROW_FORCED_CLOSE) and simulated venue liquidations (LIQUIDATION);
* a checkpoint per engine for state the ledger cannot tell (session start,
  data flags, settlements processed while flat, last prices).

On restart the journal is replayed into the rebuilt ledger (legacy cash and
equity included) without triggering the engines. Each engine then restores
its checkpoint and re-derives what was booked, and at which revision, from
the ledger itself. So a crash between writing an event and its checkpoint
cannot leave the two disagreeing: a settlement that is re-run produces the
same idempotency key and payload and is ignored, never booked twice.
"""

from __future__ import annotations

from dataclasses import asdict
import json
import sqlite3
from typing import TYPE_CHECKING, Any

from .economics import EconomicEvent, EconomicEventType, event_from_dict
from .models import ChildOrder, json_default

if TYPE_CHECKING:
    from .ledger import Ledger
    from .plan_store import ParentPlanStore

FINANCING_TYPES = {
    EconomicEventType.FUNDING, EconomicEventType.BORROW_OPEN, EconomicEventType.BORROW_ACCRUAL,
    EconomicEventType.BORROW_REPAY, EconomicEventType.BORROW_RECALL,
}


class FinancingJournal:
    def __init__(self, store: "ParentPlanStore") -> None:
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS financing_events (
                ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                event_key TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS financing_state (
                engine TEXT PRIMARY KEY,
                state_json TEXT NOT NULL
            );
        """)
        self.connection.commit()
        self.replayed = 0

    # -- writing -----------------------------------------------------------------------
    def append_event(self, event: EconomicEvent) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO financing_events(kind, event_key, payload_json) VALUES (?, ?, ?)",
                ("event", event.idempotency_key, json.dumps(event.to_dict(), sort_keys=True)),
            )

    def append_forced_fill(self, order: ChildOrder, ts_ns: int, economic_type: EconomicEventType) -> None:
        """A fill outside every plan (borrow buy-in, venue liquidation), with its economic type."""
        payload = json.dumps(
            {"order": asdict(order), "ts_ns": ts_ns, "economic_type": economic_type.value},
            default=json_default, sort_keys=True,
        )
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO financing_events(kind, event_key, payload_json) VALUES (?, ?, ?)",
                ("forced_fill", f"forced-fill|{order.order_id}|{order.filled_quantity}", payload),
            )

    def save_state(self, engine: str, state: dict[str, Any]) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO financing_state(engine, state_json) VALUES (?, ?) "
                "ON CONFLICT(engine) DO UPDATE SET state_json=excluded.state_json",
                (engine, json.dumps(state, default=json_default, sort_keys=True)),
            )

    def load_state(self, engine: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT state_json FROM financing_state WHERE engine = ?", (engine,)
        ).fetchone()
        return None if row is None else json.loads(row[0])

    # -- replay ------------------------------------------------------------------------
    def replay(self, ledger: "Ledger") -> int:
        """Re-apply every journaled financing record to a freshly rebuilt ledger."""
        from .plan_store import restore_child

        rows = self.connection.execute(
            "SELECT kind, payload_json FROM financing_events ORDER BY ordinal"
        ).fetchall()
        listeners, ledger.trade_fill_listeners = ledger.trade_fill_listeners, []
        journal, ledger.journal = ledger.journal, None
        try:
            for kind, payload in rows:
                row = json.loads(payload)
                if kind == "forced_fill":
                    ledger.apply_fill(
                        restore_child(row["order"]), int(row["ts_ns"]),
                        # Rows written before the type was stored are borrow buy-ins.
                        economic_type=EconomicEventType(
                            row.get("economic_type", EconomicEventType.BORROW_FORCED_CLOSE.value)
                        ),
                    )
                else:
                    ledger.replay_financing(event_from_dict(row))
        finally:
            ledger.trade_fill_listeners = listeners
            ledger.journal = journal
        self.replayed = len(rows)
        return len(rows)
