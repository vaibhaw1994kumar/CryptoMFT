"""Durable state for multi-venue opportunity execution.

Every state transition is written here *before* the corresponding
external action is taken, so a process that dies mid-flight can always
recover what it was about to do. The store is backed by SQLite in WAL
mode, which ships with CPython on every supported platform and needs no
native wheels.

Two uniqueness constraints do the heavy lifting for duplicate
suppression:

``legs(venue, client_order_id)``
    The same client order id can never be attached to two different
    legs, so a retry that reuses the id can never become a second order.

``fills(venue, trade_id)``
    A trade reported twice (for example by a poll and a later
    reconciliation) is counted once.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping


_SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunities (
    opportunity_id    TEXT PRIMARY KEY,
    strategy_id       TEXT NOT NULL,
    strategy_version  TEXT NOT NULL,
    signal_hash       TEXT NOT NULL,
    canonical_symbol  TEXT NOT NULL,
    target_quantity   TEXT NOT NULL,
    expected_edge     TEXT NOT NULL,
    reservation_id    TEXT,
    reserved_notional TEXT NOT NULL DEFAULT '0',
    state             TEXT NOT NULL,
    terminal          INTEGER NOT NULL DEFAULT 0,
    created_ns        INTEGER NOT NULL,
    updated_ns        INTEGER NOT NULL,
    metadata          TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS legs (
    opportunity_id    TEXT NOT NULL,
    leg_index         INTEGER NOT NULL,
    venue             TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,
    order_type        TEXT NOT NULL,
    target_quantity   TEXT NOT NULL,
    limit_price       TEXT,
    reference_price   TEXT,
    client_order_id   TEXT NOT NULL,
    idempotency_key   TEXT NOT NULL,
    exchange_order_id TEXT,
    filled_quantity   TEXT NOT NULL DEFAULT '0',
    average_price     TEXT,
    state             TEXT NOT NULL,
    reject_reason     TEXT,
    reject_action     TEXT,
    attempt           INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    submitted_ns      INTEGER,
    acknowledged_ns   INTEGER,
    updated_ns        INTEGER NOT NULL,
    PRIMARY KEY (opportunity_id, leg_index),
    UNIQUE (venue, client_order_id)
);

CREATE TABLE IF NOT EXISTS fills (
    fill_row_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id    TEXT NOT NULL,
    leg_index         INTEGER NOT NULL,
    venue             TEXT NOT NULL,
    trade_id          TEXT NOT NULL,
    exchange_order_id TEXT NOT NULL,
    side              TEXT NOT NULL,
    price             TEXT NOT NULL,
    quantity          TEXT NOT NULL,
    fee               TEXT NOT NULL DEFAULT '0',
    fee_currency      TEXT,
    timestamp_ms      INTEGER NOT NULL,
    UNIQUE (venue, trade_id)
);

CREATE TABLE IF NOT EXISTS opportunity_events (
    sequence       INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    payload        TEXT NOT NULL,
    timestamp_ns   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_opportunity
    ON opportunity_events (opportunity_id, sequence);
CREATE INDEX IF NOT EXISTS idx_fills_opportunity
    ON fills (opportunity_id, leg_index);
CREATE INDEX IF NOT EXISTS idx_opportunities_terminal
    ON opportunities (terminal, created_ns);
"""

# Columns introduced after the first release, applied to existing
# databases on open. Order matters only in that it is applied top down.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("legs", "reference_price", "TEXT"),
)


class OpportunityStoreError(RuntimeError):
    """Raised when persisted state is inconsistent or unusable."""


class DuplicateClientOrderIdError(OpportunityStoreError):
    """Raised when a client order id is already bound to another leg."""


class LegIdentityError(OpportunityStoreError):
    """Raised when an immutable leg identity column would change.

    Venue, symbol, side, and target quantity define what a leg *is*. An
    upsert that silently ignored a change to them would book an order
    against the wrong venue or the wrong direction.
    """


def _decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


@dataclass(frozen=True, slots=True)
class OpportunityRecord:
    opportunity_id: str
    strategy_id: str
    strategy_version: str
    signal_hash: str
    canonical_symbol: str
    target_quantity: Decimal
    expected_edge: Decimal
    state: str
    created_ns: int
    updated_ns: int
    terminal: bool = False
    reservation_id: str | None = None
    reserved_notional: Decimal = Decimal(0)
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LegRecord:
    opportunity_id: str
    leg_index: int
    venue: str
    symbol: str
    side: str
    order_type: str
    target_quantity: Decimal
    client_order_id: str
    idempotency_key: str
    state: str
    updated_ns: int
    limit_price: Decimal | None = None
    reference_price: Decimal | None = None
    exchange_order_id: str | None = None
    filled_quantity: Decimal = Decimal(0)
    average_price: Decimal | None = None
    reject_reason: str | None = None
    reject_action: str | None = None
    attempt: int = 0
    last_error: str | None = None
    submitted_ns: int | None = None
    acknowledged_ns: int | None = None


@dataclass(frozen=True, slots=True)
class FillRecord:
    opportunity_id: str
    leg_index: int
    venue: str
    trade_id: str
    exchange_order_id: str
    side: str
    price: Decimal
    quantity: Decimal
    timestamp_ms: int
    fee: Decimal = Decimal(0)
    fee_currency: str | None = None


@dataclass(frozen=True, slots=True)
class OpportunityEvent:
    sequence: int
    opportunity_id: str
    event_type: str
    payload: Mapping[str, Any]
    timestamp_ns: int


class OpportunityStore:
    """SQLite-backed write-ahead store for opportunity execution."""

    def __init__(self, path: Path | str = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            parent = Path(self.path).expanduser().resolve().parent
            parent.mkdir(parents=True, exist_ok=True)
            self.path = str(Path(self.path).expanduser().resolve())
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            if self.path != ":memory:":
                self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.executescript(_SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Add columns that later versions introduced.

        ``CREATE TABLE IF NOT EXISTS`` is a no-op against an existing
        database, so a new column would never appear and every read and
        write against a ledger written by an older build would fail —
        precisely when it matters, during restart recovery.
        """

        for table, column, definition in _ADDED_COLUMNS:
            existing = {
                row["name"]
                for row in self._connection.execute(
                    f"PRAGMA table_info({table})"
                )
            }
            if column not in existing:
                self._connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> OpportunityStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def save_opportunity(self, record: OpportunityRecord) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO opportunities (
                    opportunity_id, strategy_id, strategy_version,
                    signal_hash, canonical_symbol, target_quantity,
                    expected_edge, reservation_id, reserved_notional,
                    state, terminal, created_ns, updated_ns, metadata
                ) VALUES (
                    :opportunity_id, :strategy_id, :strategy_version,
                    :signal_hash, :canonical_symbol, :target_quantity,
                    :expected_edge, :reservation_id, :reserved_notional,
                    :state, :terminal, :created_ns, :updated_ns, :metadata
                )
                ON CONFLICT (opportunity_id) DO UPDATE SET
                    reservation_id = excluded.reservation_id,
                    reserved_notional = excluded.reserved_notional,
                    state = excluded.state,
                    terminal = excluded.terminal,
                    updated_ns = excluded.updated_ns,
                    metadata = excluded.metadata
                """,
                {
                    "opportunity_id": record.opportunity_id,
                    "strategy_id": record.strategy_id,
                    "strategy_version": record.strategy_version,
                    "signal_hash": record.signal_hash,
                    "canonical_symbol": record.canonical_symbol,
                    "target_quantity": _text(record.target_quantity),
                    "expected_edge": _text(record.expected_edge),
                    "reservation_id": record.reservation_id,
                    "reserved_notional": _text(record.reserved_notional),
                    "state": record.state,
                    "terminal": int(record.terminal),
                    "created_ns": record.created_ns,
                    "updated_ns": record.updated_ns,
                    "metadata": json.dumps(
                        dict(record.metadata),
                        sort_keys=True,
                    ),
                },
            )

    def save_leg(self, record: LegRecord) -> None:
        with self._lock:
            owner = self._connection.execute(
                """
                SELECT opportunity_id, leg_index FROM legs
                WHERE venue = ? AND client_order_id = ?
                """,
                (record.venue, record.client_order_id),
            ).fetchone()
            if owner is not None and (
                owner["opportunity_id"] != record.opportunity_id
                or owner["leg_index"] != record.leg_index
            ):
                raise DuplicateClientOrderIdError(
                    f"client order id {record.client_order_id} on "
                    f"{record.venue} already belongs to "
                    f"{owner['opportunity_id']}[{owner['leg_index']}]"
                )
            self._reject_identity_change(record)
            self._connection.execute(
                """
                INSERT INTO legs (
                    opportunity_id, leg_index, venue, symbol, side,
                    order_type, target_quantity, limit_price,
                    reference_price,
                    client_order_id, idempotency_key, exchange_order_id,
                    filled_quantity, average_price, state, reject_reason,
                    reject_action, attempt, last_error, submitted_ns,
                    acknowledged_ns, updated_ns
                ) VALUES (
                    :opportunity_id, :leg_index, :venue, :symbol, :side,
                    :order_type, :target_quantity, :limit_price,
                    :reference_price,
                    :client_order_id, :idempotency_key, :exchange_order_id,
                    :filled_quantity, :average_price, :state,
                    :reject_reason, :reject_action, :attempt, :last_error,
                    :submitted_ns, :acknowledged_ns, :updated_ns
                )
                ON CONFLICT (opportunity_id, leg_index) DO UPDATE SET
                    limit_price = excluded.limit_price,
                    -- Immutable once known, but a row migrated from an
                    -- older schema starts NULL and must be backfilled.
                    reference_price = COALESCE(
                        legs.reference_price, excluded.reference_price
                    ),
                    exchange_order_id = excluded.exchange_order_id,
                    filled_quantity = excluded.filled_quantity,
                    average_price = excluded.average_price,
                    state = excluded.state,
                    reject_reason = excluded.reject_reason,
                    reject_action = excluded.reject_action,
                    attempt = excluded.attempt,
                    last_error = excluded.last_error,
                    submitted_ns = excluded.submitted_ns,
                    acknowledged_ns = excluded.acknowledged_ns,
                    updated_ns = excluded.updated_ns
                """,
                {
                    "opportunity_id": record.opportunity_id,
                    "leg_index": record.leg_index,
                    "venue": record.venue,
                    "symbol": record.symbol,
                    "side": record.side,
                    "order_type": record.order_type,
                    "target_quantity": _text(record.target_quantity),
                    "limit_price": _text(record.limit_price),
                    "reference_price": _text(record.reference_price),
                    "client_order_id": record.client_order_id,
                    "idempotency_key": record.idempotency_key,
                    "exchange_order_id": record.exchange_order_id,
                    "filled_quantity": _text(record.filled_quantity),
                    "average_price": _text(record.average_price),
                    "state": record.state,
                    "reject_reason": record.reject_reason,
                    "reject_action": record.reject_action,
                    "attempt": record.attempt,
                    "last_error": record.last_error,
                    "submitted_ns": record.submitted_ns,
                    "acknowledged_ns": record.acknowledged_ns,
                    "updated_ns": record.updated_ns,
                },
            )

    def _reject_identity_change(self, record: LegRecord) -> None:
        existing = self._connection.execute(
            """
            SELECT venue, symbol, side, target_quantity FROM legs
            WHERE opportunity_id = ? AND leg_index = ?
            """,
            (record.opportunity_id, record.leg_index),
        ).fetchone()
        if existing is None:
            return
        incoming = (
            record.venue,
            record.symbol,
            record.side,
            _text(record.target_quantity),
        )
        stored = (
            existing["venue"],
            existing["symbol"],
            existing["side"],
            existing["target_quantity"],
        )
        if incoming != stored:
            raise LegIdentityError(
                f"{record.opportunity_id}[{record.leg_index}] is "
                f"{stored} and cannot be rewritten as {incoming}"
            )

    def record_fill(self, record: FillRecord) -> bool:
        """Persist a fill. Returns ``False`` when already recorded."""

        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO fills (
                    opportunity_id, leg_index, venue, trade_id,
                    exchange_order_id, side, price, quantity, fee,
                    fee_currency, timestamp_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.opportunity_id,
                    record.leg_index,
                    record.venue,
                    record.trade_id,
                    record.exchange_order_id,
                    record.side,
                    _text(record.price),
                    _text(record.quantity),
                    _text(record.fee),
                    record.fee_currency,
                    record.timestamp_ms,
                ),
            )
            return cursor.rowcount > 0

    def append_event(
        self,
        opportunity_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        timestamp_ns: int,
    ) -> int:
        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT INTO opportunity_events (
                    opportunity_id, event_type, payload, timestamp_ns
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    opportunity_id,
                    event_type,
                    json.dumps(dict(payload), sort_keys=True, default=str),
                    timestamp_ns,
                ),
            )
            return int(cursor.lastrowid or 0)

    def load_opportunity(
        self,
        opportunity_id: str,
    ) -> OpportunityRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM opportunities WHERE opportunity_id = ?",
                (opportunity_id,),
            ).fetchone()
        return None if row is None else _opportunity_from_row(row)

    def load_legs(self, opportunity_id: str) -> tuple[LegRecord, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM legs WHERE opportunity_id = ?
                ORDER BY leg_index
                """,
                (opportunity_id,),
            ).fetchall()
        return tuple(_leg_from_row(row) for row in rows)

    def load_fills(self, opportunity_id: str) -> tuple[FillRecord, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM fills WHERE opportunity_id = ?
                ORDER BY fill_row_id
                """,
                (opportunity_id,),
            ).fetchall()
        return tuple(_fill_from_row(row) for row in rows)

    def load_events(
        self,
        opportunity_id: str,
    ) -> tuple[OpportunityEvent, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM opportunity_events
                WHERE opportunity_id = ?
                ORDER BY sequence
                """,
                (opportunity_id,),
            ).fetchall()
        return tuple(
            OpportunityEvent(
                sequence=int(row["sequence"]),
                opportunity_id=row["opportunity_id"],
                event_type=row["event_type"],
                payload=json.loads(row["payload"]),
                timestamp_ns=int(row["timestamp_ns"]),
            )
            for row in rows
        )

    def open_opportunity_ids(self) -> tuple[str, ...]:
        """Non-terminal opportunities, oldest first."""

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT opportunity_id FROM opportunities
                WHERE terminal = 0
                ORDER BY created_ns, opportunity_id
                """
            ).fetchall()
        return tuple(row["opportunity_id"] for row in rows)

    def leg_for_client_order_id(
        self,
        venue: str,
        client_order_id: str,
    ) -> LegRecord | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM legs
                WHERE venue = ? AND client_order_id = ?
                """,
                (venue, client_order_id),
            ).fetchone()
        return None if row is None else _leg_from_row(row)


def _opportunity_from_row(row: sqlite3.Row) -> OpportunityRecord:
    return OpportunityRecord(
        opportunity_id=row["opportunity_id"],
        strategy_id=row["strategy_id"],
        strategy_version=row["strategy_version"],
        signal_hash=row["signal_hash"],
        canonical_symbol=row["canonical_symbol"],
        target_quantity=Decimal(row["target_quantity"]),
        expected_edge=Decimal(row["expected_edge"]),
        state=row["state"],
        created_ns=int(row["created_ns"]),
        updated_ns=int(row["updated_ns"]),
        terminal=bool(row["terminal"]),
        reservation_id=row["reservation_id"],
        reserved_notional=Decimal(row["reserved_notional"]),
        metadata=json.loads(row["metadata"]),
    )


def _leg_from_row(row: sqlite3.Row) -> LegRecord:
    return LegRecord(
        opportunity_id=row["opportunity_id"],
        leg_index=int(row["leg_index"]),
        venue=row["venue"],
        symbol=row["symbol"],
        side=row["side"],
        order_type=row["order_type"],
        target_quantity=Decimal(row["target_quantity"]),
        client_order_id=row["client_order_id"],
        idempotency_key=row["idempotency_key"],
        state=row["state"],
        updated_ns=int(row["updated_ns"]),
        limit_price=_decimal(row["limit_price"]),
        reference_price=_decimal(row["reference_price"]),
        exchange_order_id=row["exchange_order_id"],
        filled_quantity=Decimal(row["filled_quantity"]),
        average_price=_decimal(row["average_price"]),
        reject_reason=row["reject_reason"],
        reject_action=row["reject_action"],
        attempt=int(row["attempt"]),
        last_error=row["last_error"],
        submitted_ns=row["submitted_ns"],
        acknowledged_ns=row["acknowledged_ns"],
    )


def _fill_from_row(row: sqlite3.Row) -> FillRecord:
    return FillRecord(
        opportunity_id=row["opportunity_id"],
        leg_index=int(row["leg_index"]),
        venue=row["venue"],
        trade_id=row["trade_id"],
        exchange_order_id=row["exchange_order_id"],
        side=row["side"],
        price=Decimal(row["price"]),
        quantity=Decimal(row["quantity"]),
        timestamp_ms=int(row["timestamp_ms"]),
        fee=Decimal(row["fee"]),
        fee_currency=row["fee_currency"],
    )


__all__ = [
    "DuplicateClientOrderIdError",
    "FillRecord",
    "LegRecord",
    "OpportunityEvent",
    "OpportunityRecord",
    "OpportunityStore",
    "OpportunityStoreError",
    "replace",
]
