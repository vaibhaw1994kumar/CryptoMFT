from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
import json
from typing import Any
from uuid import uuid5, NAMESPACE_URL


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class BookState(StrEnum):
    SYNCING = "SYNCING"
    VALID = "VALID"
    STALE = "STALE"
    GAP_DETECTED = "GAP_DETECTED"
    CHECKSUM_FAILED = "CHECKSUM_FAILED"
    PRICE_ANOMALY = "PRICE_ANOMALY"
    INVALID_DATA = "INVALID_DATA"


class EventType(StrEnum):
    BOOK_SNAPSHOT = "BOOK_SNAPSHOT"
    BOOK_DELTA = "BOOK_DELTA"
    CONTROL = "CONTROL"


class PlanState(StrEnum):
    CREATED = "CREATED"
    RISK_RESERVED = "RISK_RESERVED"
    DISPATCHED = "DISPATCHED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    TIMED_OUT = "TIMED_OUT"
    HEDGE_UNWIND = "HEDGE_UNWIND"
    RECONCILED = "RECONCILED"
    CLOSED = "CLOSED"


class OrderState(StrEnum):
    CREATED = "CREATED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


def d(value: str | int | float | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def stable_id(kind: str, *parts: object) -> str:
    return str(uuid5(NAMESPACE_URL, "|".join([kind, *map(str, parts)])))


@dataclass(frozen=True, slots=True)
class Level:
    price: Decimal
    quantity: Decimal

    @classmethod
    def from_pair(cls, pair: list[str] | tuple[str, str] | dict[str, str]) -> "Level":
        if isinstance(pair, dict):
            return cls(d(pair["price"]), d(pair["quantity"]))
        return cls(d(pair[0]), d(pair[1]))


@dataclass(frozen=True, slots=True)
class MarketEvent:
    event_id: str
    event_type: EventType
    venue: str
    instrument_id: str
    exchange_ts_ns: int
    receive_ts_ns: int
    sequence: int
    source_ts_ns: int = 0
    sequence_start: int = 0
    raw_ref: str = ""
    schema_version: str = "1.2.0"
    adapter_version: str = ""
    checksum: str = ""
    bids: tuple[Level, ...] = ()
    asks: tuple[Level, ...] = ()
    quality_flags: tuple[str, ...] = ()
    native: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "MarketEvent":
        return cls(
            event_id=row.get("event_id") or stable_id(
                "event", row["venue"], row["instrument_id"], row["sequence"]
            ),
            event_type=EventType(row["event_type"]),
            venue=row["venue"],
            instrument_id=row["instrument_id"],
            exchange_ts_ns=int(row["exchange_ts_ns"]),
            receive_ts_ns=int(row["receive_ts_ns"]),
            sequence=int(row["sequence"]),
            source_ts_ns=int(row.get("source_ts_ns", row["exchange_ts_ns"])),
            sequence_start=int(row.get("sequence_start", row["sequence"])),
            raw_ref=row.get("raw_ref", ""),
            schema_version=row.get("schema_version", "1.2.0"),
            adapter_version=row.get("adapter_version") or "legacy-jsonl-v1",
            checksum=str(row.get("checksum") or ""),
            bids=tuple(Level.from_pair(x) for x in row.get("bids", [])),
            asks=tuple(Level.from_pair(x) for x in row.get("asks", [])),
            quality_flags=tuple(row.get("quality_flags", [])),
            native=row.get("native", {}),
        )


@dataclass(frozen=True, slots=True)
class Instrument:
    instrument_id: str
    venue: str
    symbol: str
    base: str
    quote: str
    tick_size: Decimal
    quantity_step: Decimal
    min_quantity: Decimal
    min_notional: Decimal
    taker_fee_bps: Decimal
    status: str = "TRADING"
    product_type: str = "SPOT"
    leverage: Decimal = Decimal("1")
    maker_fee_bps: Decimal | None = None
    effective_from_ns: int = 0


@dataclass(frozen=True, slots=True)
class Opportunity:
    opportunity_id: str
    strategy_id: str
    instrument_id: str
    observed_ts_ns: int
    buy_venue: str
    sell_venue: str
    buy_price: Decimal
    sell_price: Decimal
    quantity: Decimal
    gross_edge_bps: Decimal
    estimated_net_edge_bps: Decimal
    source_event_ids: tuple[str, str]
    eligibility_fingerprint: str = ""


@dataclass(slots=True)
class ChildOrder:
    order_id: str
    parent_id: str
    opportunity_id: str
    venue: str
    instrument_id: str
    side: Side
    quantity: Decimal
    limit_price: Decimal
    state: OrderState = OrderState.CREATED
    filled_quantity: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")
    fee: Decimal = Decimal("0")
    reject_reason: str | None = None
    state_updates: list[dict[str, Any]] = field(default_factory=list)
    order_role: str = "PRIMARY"
    reduce_only: bool = False
    unwind_attempt: int = 0
    order_type: str = "LIMIT"
    time_in_force: str = "IOC"
    post_only: bool = False


@dataclass(slots=True)
class ParentPlan:
    parent_id: str
    opportunity: Opportunity
    children: list[ChildOrder]
    state: PlanState = PlanState.CREATED
    residual_base: Decimal = Decimal("0")
    gross_pnl: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    unwind_cost: Decimal = Decimal("0")
    net_pnl: Decimal = Decimal("0")
    terminal_outcome: str = ""
    transitions: list[dict[str, Any]] = field(default_factory=list)
    residual_since_ts_ns: int | None = None
    unwind_attempts: int = 0

    def transition(self, state: PlanState, ts_ns: int, reason: str = "") -> None:
        self.state = state
        self.transitions.append({"ts_ns": ts_ns, "state": state.value, "reason": reason})


def json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, (MarketEvent, Instrument, Opportunity, ChildOrder, ParentPlan, Level)):
        return asdict(value)
    raise TypeError(f"Cannot serialize {type(value)!r}")


def canonical_json(value: Any) -> str:
    return json.dumps(value, default=json_default, sort_keys=True, separators=(",", ":"))


def object_hash(value: Any) -> str:
    return sha256(canonical_json(value).encode()).hexdigest()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
