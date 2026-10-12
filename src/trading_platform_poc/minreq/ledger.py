"""Append-only event ledger, derived state and reconciliation (MR-11).

Orders, fills, positions and balances are never written directly. They are
derived from an immutable event log keyed by the correlation identifiers, so a
restart rebuilds exactly the same state and a reconciliation break is always
traceable back to the event that caused it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_events (
    sequence        INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL UNIQUE,
    timestamp_ns    INTEGER NOT NULL,
    event_type      TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    plan_id         TEXT,
    child_order_id  TEXT,
    client_order_id TEXT,
    venue           TEXT,
    instrument_id   TEXT,
    source          TEXT NOT NULL,
    payload         TEXT NOT NULL,
    digest          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_run ON ledger_events(run_id);
CREATE INDEX IF NOT EXISTS idx_ledger_plan ON ledger_events(plan_id);
CREATE INDEX IF NOT EXISTS idx_ledger_child ON ledger_events(child_order_id);
CREATE INDEX IF NOT EXISTS idx_ledger_type ON ledger_events(event_type);

CREATE TABLE IF NOT EXISTS ledger_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# timed_out is deliberately not terminal: it means the venue never confirmed
# the order, so it must still surface in reconciliation as an open question.
TERMINAL_STATES = {"filled", "cancelled", "rejected", "closed"}


class LedgerError(RuntimeError):
    """Raised when an append-only invariant would be violated."""


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    event_id: str
    timestamp_ns: int
    event_type: str
    run_id: str
    payload: dict[str, Any]
    plan_id: str | None = None
    child_order_id: str | None = None
    client_order_id: str | None = None
    venue: str | None = None
    instrument_id: str | None = None
    source: str = "internal"
    sequence: int = 0

    @property
    def digest(self) -> str:
        document = json.dumps(
            {
                "event_id": self.event_id,
                "timestamp_ns": self.timestamp_ns,
                "event_type": self.event_type,
                "run_id": self.run_id,
                "plan_id": self.plan_id,
                "child_order_id": self.child_order_id,
                "client_order_id": self.client_order_id,
                "venue": self.venue,
                "instrument_id": self.instrument_id,
                "source": self.source,
                "payload": self.payload,
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(document.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "event_id": self.event_id,
            "timestamp_ns": self.timestamp_ns,
            "event_type": self.event_type,
            "run_id": self.run_id,
            "plan_id": self.plan_id,
            "child_order_id": self.child_order_id,
            "client_order_id": self.client_order_id,
            "venue": self.venue,
            "instrument_id": self.instrument_id,
            "source": self.source,
            "payload": self.payload,
        }


@dataclass(slots=True)
class DerivedOrder:
    child_order_id: str
    client_order_id: str
    plan_id: str
    venue: str
    instrument_id: str
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal | None
    state: str
    filled_quantity: Decimal = Decimal(0)
    average_price: Decimal | None = None
    fees: Decimal = Decimal(0)
    venue_order_id: str | None = None
    created_ns: int = 0
    updated_ns: int = 0
    reject_reason: str = ""

    @property
    def leaves_quantity(self) -> Decimal:
        return max(Decimal(0), self.quantity - self.filled_quantity)

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "child_order_id": self.child_order_id,
            "client_order_id": self.client_order_id,
            "plan_id": self.plan_id,
            "venue": self.venue,
            "instrument_id": self.instrument_id,
            "side": self.side,
            "order_type": self.order_type,
            "quantity": str(self.quantity),
            "price": None if self.price is None else str(self.price),
            "state": self.state,
            "filled_quantity": str(self.filled_quantity),
            "leaves_quantity": str(self.leaves_quantity),
            "average_price": (
                None if self.average_price is None else str(self.average_price)
            ),
            "fees": str(self.fees),
            "venue_order_id": self.venue_order_id,
            "created_ns": self.created_ns,
            "updated_ns": self.updated_ns,
            "reject_reason": self.reject_reason,
        }


@dataclass(slots=True)
class DerivedPosition:
    instrument_id: str
    venue: str
    quantity: Decimal = Decimal(0)
    cost_basis: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)

    @property
    def average_price(self) -> Decimal | None:
        if self.quantity == 0:
            return None
        return self.cost_basis / self.quantity

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "venue": self.venue,
            "quantity": str(self.quantity),
            "cost_basis": str(self.cost_basis),
            "average_price": (
                None if self.average_price is None else str(self.average_price)
            ),
            "realized_pnl": str(self.realized_pnl),
            "fees": str(self.fees),
        }


@dataclass(slots=True)
class DerivedState:
    orders: dict[str, DerivedOrder] = field(default_factory=dict)
    positions: dict[tuple[str, str], DerivedPosition] = field(default_factory=dict)
    balances: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    plans: dict[str, dict[str, Any]] = field(default_factory=dict)
    fills: list[dict[str, Any]] = field(default_factory=list)
    realized_pnl: Decimal = Decimal(0)
    total_fees: Decimal = Decimal(0)
    event_count: int = 0

    def open_orders(self) -> list[DerivedOrder]:
        return [order for order in self.orders.values() if not order.terminal]

    def position(self, instrument_id: str, venue: str) -> DerivedPosition:
        key = (instrument_id, venue)
        if key not in self.positions:
            self.positions[key] = DerivedPosition(instrument_id, venue)
        return self.positions[key]

    def net_position(self, instrument_id: str) -> Decimal:
        return sum(
            (
                position.quantity
                for (instrument, _), position in self.positions.items()
                if instrument == instrument_id
            ),
            Decimal(0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_count": self.event_count,
            "realized_pnl": str(self.realized_pnl),
            "total_fees": str(self.total_fees),
            "orders": {
                key: order.to_dict() for key, order in sorted(self.orders.items())
            },
            "positions": [
                position.to_dict()
                for _, position in sorted(self.positions.items())
            ],
            "balances": {
                f"{venue}:{currency}": str(amount)
                for (venue, currency), amount in sorted(self.balances.items())
            },
            "plans": {key: value for key, value in sorted(self.plans.items())},
            "fill_count": len(self.fills),
        }

    def state_hash(self) -> str:
        document = json.dumps(self.to_dict(), sort_keys=True, default=str)
        return hashlib.sha256(document.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ReconciliationBreak:
    scope: str
    key: str
    local_value: str
    venue_value: str
    difference: str
    severity: str
    cause: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "key": self.key,
            "local_value": self.local_value,
            "venue_value": self.venue_value,
            "difference": self.difference,
            "severity": self.severity,
            "cause": self.cause,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    timestamp_ns: int
    run_id: str
    checked_orders: int
    checked_positions: int
    checked_balances: int
    breaks: tuple[ReconciliationBreak, ...]

    @property
    def clean(self) -> bool:
        return not self.breaks

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp_ns": self.timestamp_ns,
            "run_id": self.run_id,
            "checked_orders": self.checked_orders,
            "checked_positions": self.checked_positions,
            "checked_balances": self.checked_balances,
            "clean": self.clean,
            "breaks": [item.to_dict() for item in self.breaks],
        }


class EventLedger:
    """Append-only SQLite ledger. Updates and deletes are not supported."""

    def __init__(self, path: str | Path, *, run_id: str) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path))
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.executescript(SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> EventLedger:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def set_meta(self, key: str, value: str) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO ledger_meta(key, value) VALUES(?, ?)",
            (key, value),
        )
        self._connection.commit()

    def meta(self, key: str) -> str | None:
        row = self._connection.execute(
            "SELECT value FROM ledger_meta WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row["value"])

    def append(self, event: LedgerEvent) -> bool:
        """Append one event. Returns False when the event was already stored."""

        existing = self._connection.execute(
            "SELECT digest FROM ledger_events WHERE event_id = ?",
            (event.event_id,),
        ).fetchone()
        if existing is not None:
            if str(existing["digest"]) != event.digest:
                raise LedgerError(
                    f"event {event.event_id} already exists with a different digest"
                )
            return False
        self._connection.execute(
            """
            INSERT INTO ledger_events(
                event_id, timestamp_ns, event_type, run_id, plan_id,
                child_order_id, client_order_id, venue, instrument_id,
                source, payload, digest
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.timestamp_ns,
                event.event_type,
                event.run_id,
                event.plan_id,
                event.child_order_id,
                event.client_order_id,
                event.venue,
                event.instrument_id,
                event.source,
                json.dumps(event.payload, sort_keys=True, default=str),
                event.digest,
            ),
        )
        self._connection.commit()
        return True

    def append_many(self, events: Iterable[LedgerEvent]) -> int:
        return sum(1 for event in events if self.append(event))

    def events(
        self, *, run_id: str | None = None, event_type: str | None = None
    ) -> Iterator[LedgerEvent]:
        query = "SELECT * FROM ledger_events"
        clauses: list[str] = []
        params: list[Any] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY sequence ASC"
        for row in self._connection.execute(query, params):
            yield LedgerEvent(
                event_id=str(row["event_id"]),
                timestamp_ns=int(row["timestamp_ns"]),
                event_type=str(row["event_type"]),
                run_id=str(row["run_id"]),
                payload=json.loads(row["payload"]),
                plan_id=row["plan_id"],
                child_order_id=row["child_order_id"],
                client_order_id=row["client_order_id"],
                venue=row["venue"],
                instrument_id=row["instrument_id"],
                source=str(row["source"]),
                sequence=int(row["sequence"]),
            )

    def count(self, *, run_id: str | None = None) -> int:
        if run_id is None:
            row = self._connection.execute(
                "SELECT COUNT(*) AS total FROM ledger_events"
            ).fetchone()
        else:
            row = self._connection.execute(
                "SELECT COUNT(*) AS total FROM ledger_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row["total"])

    def chain_digest(self, *, run_id: str | None = None) -> str:
        digest = hashlib.sha256()
        for event in self.events(run_id=run_id):
            digest.update(event.digest.encode("utf-8"))
        return digest.hexdigest()

    def derive(self, *, run_id: str | None = None) -> DerivedState:
        return derive_state(self.events(run_id=run_id))

    def reconcile(
        self,
        venue_state: Mapping[str, Any],
        timestamp_ns: int,
        *,
        run_id: str | None = None,
        quantity_tolerance: Decimal = Decimal("0"),
        balance_tolerance: Decimal = Decimal("0.00000001"),
    ) -> ReconciliationReport:
        return reconcile(
            self.derive(run_id=run_id),
            venue_state,
            timestamp_ns,
            run_id=run_id or self.run_id,
            quantity_tolerance=quantity_tolerance,
            balance_tolerance=balance_tolerance,
        )


def derive_state(events: Iterable[LedgerEvent]) -> DerivedState:
    """Fold the immutable event log into current orders, positions and cash."""

    state = DerivedState()
    for event in events:
        state.event_count += 1
        payload = event.payload
        kind = event.event_type
        if kind == "plan_created":
            state.plans[str(event.plan_id)] = {
                "plan_id": event.plan_id,
                "state": "created",
                "created_ns": event.timestamp_ns,
                "legs": payload.get("legs", []),
                "expected_edge": payload.get("expected_edge"),
                "feature_id": payload.get("feature_id"),
                "risk_decision_id": payload.get("risk_decision_id"),
            }
        elif kind == "plan_state":
            plan = state.plans.setdefault(str(event.plan_id), {})
            plan["state"] = payload.get("state")
            plan["updated_ns"] = event.timestamp_ns
            if payload.get("reason"):
                plan["reason"] = payload["reason"]
        elif kind == "order_created":
            state.orders[str(event.child_order_id)] = DerivedOrder(
                child_order_id=str(event.child_order_id),
                client_order_id=str(event.client_order_id),
                plan_id=str(event.plan_id),
                venue=str(event.venue),
                instrument_id=str(event.instrument_id),
                side=str(payload["side"]),
                order_type=str(payload.get("order_type", "limit")),
                quantity=Decimal(str(payload["quantity"])),
                price=(
                    None
                    if payload.get("price") is None
                    else Decimal(str(payload["price"]))
                ),
                state="created",
                created_ns=event.timestamp_ns,
                updated_ns=event.timestamp_ns,
            )
        elif kind == "order_state":
            order = state.orders.get(str(event.child_order_id))
            if order is not None:
                order.state = str(payload["state"])
                order.updated_ns = event.timestamp_ns
                if payload.get("venue_order_id"):
                    order.venue_order_id = str(payload["venue_order_id"])
                if payload.get("reason"):
                    order.reject_reason = str(payload["reason"])
        elif kind == "fill":
            order = state.orders.get(str(event.child_order_id))
            quantity = Decimal(str(payload["quantity"]))
            price = Decimal(str(payload["price"]))
            fee = Decimal(str(payload.get("fee", "0")))
            venue = str(event.venue)
            instrument_id = str(event.instrument_id)
            side = str(payload["side"])
            if order is not None:
                previous_notional = order.filled_quantity * (
                    order.average_price or Decimal(0)
                )
                order.filled_quantity += quantity
                order.average_price = (
                    previous_notional + quantity * price
                ) / order.filled_quantity
                order.fees += fee
                order.updated_ns = event.timestamp_ns
                order.state = (
                    "filled"
                    if order.filled_quantity >= order.quantity
                    else "partially_filled"
                )
            position = state.position(instrument_id, venue)
            signed = quantity if side == "buy" else -quantity
            realized = _apply_position(position, signed, price)
            position.fees += fee
            position.realized_pnl += realized
            state.realized_pnl += realized - fee
            state.total_fees += fee
            base, quote = _split_currencies(instrument_id, payload)
            state.balances[(venue, base)] = (
                state.balances.get((venue, base), Decimal(0)) + signed
            )
            state.balances[(venue, quote)] = (
                state.balances.get((venue, quote), Decimal(0))
                - signed * price
                - fee
            )
            state.fills.append(
                {
                    "timestamp_ns": event.timestamp_ns,
                    "plan_id": event.plan_id,
                    "child_order_id": event.child_order_id,
                    "venue": venue,
                    "instrument_id": instrument_id,
                    "side": side,
                    "quantity": str(quantity),
                    "price": str(price),
                    "fee": str(fee),
                    "liquidity": payload.get("liquidity", "taker"),
                    "source": event.source,
                }
            )
        elif kind == "funding":
            venue = str(event.venue)
            currency = str(payload.get("currency", "USDT"))
            amount = Decimal(str(payload["amount"]))
            state.balances[(venue, currency)] = (
                state.balances.get((venue, currency), Decimal(0)) + amount
            )
            state.realized_pnl += amount
        elif kind == "balance_adjustment":
            venue = str(event.venue)
            currency = str(payload["currency"])
            amount = Decimal(str(payload["amount"]))
            state.balances[(venue, currency)] = (
                state.balances.get((venue, currency), Decimal(0)) + amount
            )
    return state


def _apply_position(
    position: DerivedPosition, signed_quantity: Decimal, price: Decimal
) -> Decimal:
    """Average-cost accounting that realises PnL only on reducing trades."""

    realized = Decimal(0)
    current = position.quantity
    if current == 0 or (current > 0) == (signed_quantity > 0):
        position.quantity = current + signed_quantity
        position.cost_basis += signed_quantity * price
        return realized

    average = position.cost_basis / current
    closing = min(abs(signed_quantity), abs(current))
    direction = Decimal(1) if current > 0 else Decimal(-1)
    realized = (price - average) * closing * direction
    position.quantity = current + signed_quantity
    remaining = abs(signed_quantity) - closing
    if position.quantity == 0:
        position.cost_basis = Decimal(0)
    elif remaining > 0:
        position.cost_basis = position.quantity * price
    else:
        position.cost_basis = average * position.quantity
    return realized


def _split_currencies(
    instrument_id: str, payload: Mapping[str, Any]
) -> tuple[str, str]:
    base = payload.get("base_currency")
    quote = payload.get("quote_currency")
    if base and quote:
        return str(base), str(quote)
    symbol = instrument_id.split(".")[-1]
    if "-" in symbol:
        left, _, right = symbol.partition("-")
        return left, right
    return symbol, "USDT"


def reconcile(
    state: DerivedState,
    venue_state: Mapping[str, Any],
    timestamp_ns: int,
    *,
    run_id: str,
    quantity_tolerance: Decimal = Decimal("0"),
    balance_tolerance: Decimal = Decimal("0.00000001"),
) -> ReconciliationReport:
    """Compare derived local state with a venue snapshot and classify breaks."""

    breaks: list[ReconciliationBreak] = []

    venue_orders = {
        str(key): value for key, value in venue_state.get("orders", {}).items()
    }
    local_open = {order.client_order_id: order for order in state.open_orders()}
    for client_order_id, order in sorted(local_open.items()):
        remote = venue_orders.get(client_order_id)
        if remote is None:
            breaks.append(
                ReconciliationBreak(
                    scope="order",
                    key=client_order_id,
                    local_value=order.state,
                    venue_value="missing",
                    difference=str(order.leaves_quantity),
                    severity="high",
                    cause="order_unknown_at_venue",
                )
            )
            continue
        remote_state = str(remote.get("state", "unknown"))
        if remote_state in TERMINAL_STATES and not order.terminal:
            breaks.append(
                ReconciliationBreak(
                    scope="order",
                    key=client_order_id,
                    local_value=order.state,
                    venue_value=remote_state,
                    difference=str(order.leaves_quantity),
                    severity="medium",
                    cause="missed_terminal_update",
                )
            )
        remote_filled = Decimal(str(remote.get("filled_quantity", "0")))
        if abs(remote_filled - order.filled_quantity) > quantity_tolerance:
            breaks.append(
                ReconciliationBreak(
                    scope="order_fill",
                    key=client_order_id,
                    local_value=str(order.filled_quantity),
                    venue_value=str(remote_filled),
                    difference=str(remote_filled - order.filled_quantity),
                    severity="high",
                    cause="fill_quantity_mismatch",
                )
            )
    for client_order_id in sorted(set(venue_orders) - set(local_open)):
        remote = venue_orders[client_order_id]
        if str(remote.get("state", "unknown")) in TERMINAL_STATES:
            continue
        breaks.append(
            ReconciliationBreak(
                scope="order",
                key=client_order_id,
                local_value="missing",
                venue_value=str(remote.get("state", "unknown")),
                difference=str(remote.get("leaves_quantity", "0")),
                severity="critical",
                cause="orphan_order_at_venue",
            )
        )

    venue_positions = venue_state.get("positions", {})
    for key, remote_quantity in sorted(venue_positions.items()):
        instrument_id, _, venue = str(key).partition("@")
        local = state.positions.get((instrument_id, venue))
        local_quantity = Decimal(0) if local is None else local.quantity
        remote_value = Decimal(str(remote_quantity))
        if abs(remote_value - local_quantity) > quantity_tolerance:
            breaks.append(
                ReconciliationBreak(
                    scope="position",
                    key=str(key),
                    local_value=str(local_quantity),
                    venue_value=str(remote_value),
                    difference=str(remote_value - local_quantity),
                    severity="critical",
                    cause="position_mismatch",
                )
            )
    for (instrument_id, venue), position in sorted(state.positions.items()):
        key = f"{instrument_id}@{venue}"
        if key in venue_positions or position.quantity == 0:
            continue
        breaks.append(
            ReconciliationBreak(
                scope="position",
                key=key,
                local_value=str(position.quantity),
                venue_value="0",
                difference=str(-position.quantity),
                severity="critical",
                cause="position_missing_at_venue",
            )
        )

    venue_balances = venue_state.get("balances", {})
    for key, remote_balance in sorted(venue_balances.items()):
        venue, _, currency = str(key).partition(":")
        local_balance = state.balances.get((venue, currency), Decimal(0))
        remote_value = Decimal(str(remote_balance))
        if abs(remote_value - local_balance) > balance_tolerance:
            breaks.append(
                ReconciliationBreak(
                    scope="balance",
                    key=str(key),
                    local_value=str(local_balance),
                    venue_value=str(remote_value),
                    difference=str(remote_value - local_balance),
                    severity="high",
                    cause="balance_mismatch",
                )
            )

    return ReconciliationReport(
        timestamp_ns=timestamp_ns,
        run_id=run_id,
        checked_orders=len(set(local_open) | set(venue_orders)),
        checked_positions=len(
            set(venue_positions) | {f"{i}@{v}" for i, v in state.positions}
        ),
        checked_balances=len(venue_balances),
        breaks=tuple(breaks),
    )
