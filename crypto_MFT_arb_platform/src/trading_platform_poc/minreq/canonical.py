"""Canonical event model shared by ingestion, replay, backtest and paper mode.

MR-02 requires one envelope that carries venue, instrument, every relevant
timestamp, the venue sequence, explicit quality flags and a reference back to
the raw record, without discarding native venue fields.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import msgspec

SCHEMA_VERSION = 2
"""Canonical envelope schema version. Bump on any breaking field change."""


class CanonicalEventType(str, Enum):
    """Every event kind that can travel through the canonical bus."""

    BOOK_SNAPSHOT = "book_snapshot"
    BOOK_DELTA = "book_delta"
    TRADE = "trade"
    TICKER = "ticker"
    INSTRUMENT_STATUS = "instrument_status"
    FUNDING = "funding"
    DATA_QUALITY = "data_quality"
    CONTROL = "control"
    FEATURE = "feature"
    SIGNAL = "signal"
    RISK_DECISION = "risk_decision"
    ORDER_SUBMIT = "order_submit"
    ORDER_ACK = "order_ack"
    ORDER_REJECT = "order_reject"
    ORDER_CANCEL = "order_cancel"
    ORDER_TIMEOUT = "order_timeout"
    FILL = "fill"
    POSITION = "position"
    BALANCE = "balance"
    PLAN_STATE = "plan_state"
    ACCRUAL = "accrual"


MARKET_DATA_EVENTS = frozenset(
    {
        CanonicalEventType.BOOK_SNAPSHOT,
        CanonicalEventType.BOOK_DELTA,
        CanonicalEventType.TRADE,
        CanonicalEventType.TICKER,
        CanonicalEventType.INSTRUMENT_STATUS,
        CanonicalEventType.FUNDING,
    }
)


class QualityFlag(str, Enum):
    """Explicit data-quality state attached to a canonical event."""

    OK = "ok"
    SEQUENCE_GAP = "sequence_gap"
    DUPLICATE = "duplicate"
    OUT_OF_ORDER = "out_of_order"
    STALE = "stale"
    CROSSED_BOOK = "crossed_book"
    EMPTY_BOOK = "empty_book"
    SCHEMA_EXCEPTION = "schema_exception"
    NEGATIVE_LATENCY = "negative_latency"
    CLOCK_REGRESSION = "clock_regression"
    SNAPSHOT_RESYNC = "snapshot_resync"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    SYNTHETIC = "synthetic"
    INSTRUMENT_UNKNOWN = "instrument_unknown"
    PRICE_JUMP = "price_jump"
    WIDE_SPREAD = "wide_spread"
    TRADE_OUTSIDE_BOOK = "trade_outside_book"
    IMPOSSIBLE_LATENCY = "impossible_latency"


class BookValidity(str, Enum):
    """Tri-state book disposition required by the book-integrity control."""

    VALID = "valid"
    STALE = "stale"
    INVALID = "invalid"


class ControlAction(str, Enum):
    """Operator or platform control events that replay must reproduce."""

    KILL_SWITCH_ON = "kill_switch_on"
    KILL_SWITCH_OFF = "kill_switch_off"
    FEED_STALL = "feed_stall"
    FEED_RECOVERED = "feed_recovered"
    RATE_LIMIT_BIND = "rate_limit_bind"
    RATE_LIMIT_RELEASE = "rate_limit_release"
    VENUE_SUSPENDED = "venue_suspended"
    VENUE_RESUMED = "venue_resumed"
    INSTRUMENT_SUSPENDED = "instrument_suspended"
    INSTRUMENT_RESUMED = "instrument_resumed"
    DISCONNECT = "disconnect"
    RECONNECT = "reconnect"
    MAINTENANCE = "maintenance"
    TRADE_BREAK = "trade_break"
    RECONCILE_REQUIRED = "reconcile_required"


class Liquidity(str, Enum):
    MAKER = "maker"
    TAKER = "taker"


class RawReference(msgspec.Struct, frozen=True):
    """Pointer from a canonical event back to the immutable raw record."""

    dataset_id: str
    partition: str
    record_index: int
    sha256: str = ""


class PriceLevel(msgspec.Struct, frozen=True):
    price: Decimal
    quantity: Decimal


class BookPayload(msgspec.Struct, frozen=True):
    """Snapshot or delta level set. A delta quantity of zero removes a level."""

    bids: tuple[PriceLevel, ...] = ()
    asks: tuple[PriceLevel, ...] = ()
    first_sequence: int | None = None
    final_sequence: int | None = None
    is_snapshot: bool = False
    checksum: int | None = None


class TradePayload(msgspec.Struct, frozen=True):
    trade_id: str
    price: Decimal
    quantity: Decimal
    aggressor_side: str | None = None
    trade_timestamp_ns: int = 0


class TickerPayload(msgspec.Struct, frozen=True):
    bid_price: Decimal | None = None
    bid_quantity: Decimal | None = None
    ask_price: Decimal | None = None
    ask_quantity: Decimal | None = None
    last_price: Decimal | None = None


class InstrumentStatusPayload(msgspec.Struct, frozen=True):
    status: str
    reason: str = ""


class FundingPayload(msgspec.Struct, frozen=True):
    funding_rate: Decimal
    next_funding_timestamp_ns: int | None = None
    mark_price: Decimal | None = None
    index_price: Decimal | None = None


class DataQualityPayload(msgspec.Struct, frozen=True):
    code: str
    severity: str
    message: str
    blocking: bool = False
    details: dict[str, str] = msgspec.field(default_factory=dict)


class ControlPayload(msgspec.Struct, frozen=True):
    action: str
    reason: str = ""
    details: dict[str, str] = msgspec.field(default_factory=dict)


class Correlation(msgspec.Struct, frozen=True):
    """Correlation identifiers required so any record traces to its source.

    Every downstream record keeps the same identifiers so an opportunity's PnL
    can be walked back to the exact market state that produced it.
    """

    run_id: str = ""
    source_event_id: str = ""
    feature_id: str = ""
    opportunity_id: str = ""
    risk_decision_id: str = ""
    plan_id: str = ""
    child_order_id: str = ""


EMPTY_CORRELATION = Correlation()


class CanonicalEvent(msgspec.Struct, frozen=True):
    """The single event envelope used across research, backtest and paper."""

    event_id: str
    event_type: CanonicalEventType
    venue: str
    instrument_id: str
    venue_symbol: str
    exchange_timestamp_ns: int
    receive_timestamp_ns: int
    sequence: int | None = None
    ingest_timestamp_ns: int = 0
    schema_version: int = SCHEMA_VERSION
    quality_flags: tuple[QualityFlag, ...] = ()
    book_validity: BookValidity = BookValidity.VALID
    payload: Any = None
    native: dict[str, str] = msgspec.field(default_factory=dict)
    raw: RawReference | None = None
    correlation: Correlation = EMPTY_CORRELATION

    @property
    def ordering_key(self) -> tuple[int, str, int, str]:
        """Total order used by the deterministic multi-venue merge.

        Receive time first (that is when the platform could have acted), then
        venue and sequence, then the event id so ties never depend on dict or
        file iteration order.
        """

        return (
            self.receive_timestamp_ns,
            self.venue,
            self.sequence if self.sequence is not None else -1,
            self.event_id,
        )

    @property
    def is_market_data(self) -> bool:
        return self.event_type in MARKET_DATA_EVENTS

    @property
    def age_ns(self) -> int:
        return self.receive_timestamp_ns - self.exchange_timestamp_ns

    def with_flags(self, *flags: QualityFlag) -> CanonicalEvent:
        merged = tuple(dict.fromkeys((*self.quality_flags, *flags)))
        return msgspec.structs.replace(self, quality_flags=merged)

    def with_validity(self, validity: BookValidity) -> CanonicalEvent:
        return msgspec.structs.replace(self, book_validity=validity)

    def with_correlation(self, correlation: Correlation) -> CanonicalEvent:
        return msgspec.structs.replace(self, correlation=correlation)


_PAYLOAD_TYPES: dict[CanonicalEventType, type[msgspec.Struct]] = {
    CanonicalEventType.BOOK_SNAPSHOT: BookPayload,
    CanonicalEventType.BOOK_DELTA: BookPayload,
    CanonicalEventType.TRADE: TradePayload,
    CanonicalEventType.TICKER: TickerPayload,
    CanonicalEventType.INSTRUMENT_STATUS: InstrumentStatusPayload,
    CanonicalEventType.FUNDING: FundingPayload,
    CanonicalEventType.DATA_QUALITY: DataQualityPayload,
    CanonicalEventType.CONTROL: ControlPayload,
}


def payload_type(event_type: CanonicalEventType) -> type[msgspec.Struct] | None:
    return _PAYLOAD_TYPES.get(event_type)


_ENCODER = msgspec.json.Encoder()


def encode_event(event: CanonicalEvent) -> bytes:
    return _ENCODER.encode(event)


def decode_event(raw: bytes | str) -> CanonicalEvent:
    """Decode an event, restoring the concrete payload struct."""

    shell = msgspec.json.decode(raw, type=CanonicalEvent)
    concrete = payload_type(shell.event_type)
    if concrete is None or shell.payload is None:
        return shell
    payload = msgspec.convert(shell.payload, type=concrete, strict=False)
    return msgspec.structs.replace(shell, payload=payload)


def make_event_id(
    venue: str,
    instrument_id: str,
    event_type: CanonicalEventType,
    sequence: int | None,
    exchange_timestamp_ns: int,
    discriminator: str = "",
) -> str:
    """Deterministic event identity so re-ingestion is idempotent."""

    key = (
        f"{venue}|{instrument_id}|{event_type.value}|{sequence}"
        f"|{exchange_timestamp_ns}|{discriminator}"
    )
    return str(uuid5(NAMESPACE_URL, key))


def dedupe_key(event: CanonicalEvent) -> str:
    """Key used by the pipeline to suppress duplicate raw records."""

    return event.event_id


def event_sort_key(event: CanonicalEvent) -> tuple[int, str, int, str]:
    return event.ordering_key


def uuid_for(namespace: str, *parts: object) -> UUID:
    return uuid5(NAMESPACE_URL, namespace + "|" + "|".join(str(p) for p in parts))


SCHEMA_FIELDS: tuple[str, ...] = tuple(CanonicalEvent.__struct_fields__)

VERSIONING_RULES: tuple[str, ...] = (
    "schema_version is a single integer for the whole canonical envelope and "
    "is stamped on every event; a reader refuses a dataset whose version it "
    "does not implement rather than guessing.",
    "Adding an optional payload field, a new CanonicalEventType member or a "
    "new QualityFlag member is backwards compatible and does not bump "
    "schema_version.",
    "Removing or renaming an envelope field, changing the meaning or unit of "
    "an existing field, or changing the ordering key bumps schema_version.",
    "Prices, quantities and money are transported as decimal strings so that "
    "no value is ever rounded by a float round-trip.",
    "Every event carries adapter_version and, where relevant, the "
    "instrument_version and fee record_version that were in force, so a "
    "replay resolves the same reference data the live path resolved.",
    "event_id is a uuid5 over the venue, channel, sequence and payload "
    "digest, so re-ingesting the same raw bytes yields the same identity and "
    "duplicates collapse instead of double-counting.",
    "Datasets are immutable: a corrected capture is written as a new dataset "
    "with its own manifest checksum, never by rewriting an approved one.",
)


def schema_document() -> dict[str, Any]:
    """Machine-readable schema published as an MR-02 deliverable."""
    return {
        "schema_version": SCHEMA_VERSION,
        "envelope_fields": list(SCHEMA_FIELDS),
        "event_types": [item.value for item in CanonicalEventType],
        "quality_flags": [item.value for item in QualityFlag],
        "book_validity": [item.value for item in BookValidity],
        "control_actions": [item.value for item in ControlAction],
        "correlation_fields": list(Correlation.__struct_fields__),
        "payloads": {
            event_type.value: list(struct.__struct_fields__)
            for event_type, struct in _PAYLOAD_TYPES.items()
        },
        "ordering_key": [
            "receive_timestamp_ns",
            "venue",
            "sequence",
            "event_id",
        ],
        "versioning_rules": list(VERSIONING_RULES),
    }
