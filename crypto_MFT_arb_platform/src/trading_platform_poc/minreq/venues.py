"""Venue adapters: raw capture envelopes and raw-to-canonical normalization.

MR-01 requires at least two venues with L2 depth, trades and instrument
metadata. Binance, OKX and Bybit are implemented here. Normalization is a pure
function of the raw frame so AT-02 can be proven offline from stored records.
"""

from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, AsyncIterator, Callable, Iterable, Sequence

from trading_platform_poc.minreq.canonical import (
    BookPayload,
    CanonicalEvent,
    CanonicalEventType,
    InstrumentStatusPayload,
    PriceLevel,
    QualityFlag,
    RawReference,
    SCHEMA_VERSION,
    TradePayload,
    make_event_id,
)
from trading_platform_poc.minreq.securitymaster import SecurityMaster

ADAPTER_VERSION = "1.0.0"
SYNTHETIC_HOST = "synthetic"
"""Host stamped on raw records produced by the deterministic generator."""
MS_NS = 1_000_000


class NormalizationError(ValueError):
    """Raised when a raw frame cannot be mapped without losing information."""


@dataclass(frozen=True, slots=True)
class RawRecord:
    """Immutable raw capture record. Nothing about the frame is discarded."""

    venue: str
    channel: str
    host: str
    receive_timestamp_ns: int
    sequence: int | None
    adapter_version: str
    schema_version: int
    message: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "channel": self.channel,
            "host": self.host,
            "receive_timestamp_ns": self.receive_timestamp_ns,
            "sequence": self.sequence,
            "adapter_version": self.adapter_version,
            "schema_version": self.schema_version,
            "message": self.message,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_dict(cls, document: dict[str, Any]) -> RawRecord:
        return cls(
            venue=str(document["venue"]),
            channel=str(document["channel"]),
            host=str(document.get("host", "")),
            receive_timestamp_ns=int(document["receive_timestamp_ns"]),
            sequence=(
                None if document.get("sequence") is None else int(document["sequence"])
            ),
            adapter_version=str(document.get("adapter_version", ADAPTER_VERSION)),
            schema_version=int(document.get("schema_version", SCHEMA_VERSION)),
            message=dict(document["message"]),
        )


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError) as error:
        raise NormalizationError(f"non-numeric field: {value!r}") from error


def _levels(rows: Iterable[Sequence[Any]]) -> tuple[PriceLevel, ...]:
    return tuple(
        PriceLevel(price=_decimal(row[0]), quantity=_decimal(row[1])) for row in rows
    )


class VenueNormalizer:
    """Base class mapping one venue's raw frames onto canonical events."""

    venue: str = ""
    channels: tuple[str, ...] = ()

    def __init__(self, security_master: SecurityMaster) -> None:
        self._security_master = security_master

    @property
    def security_master(self) -> SecurityMaster:
        return self._security_master

    def instrument_id(self, venue_symbol: str) -> str:
        resolved = self._security_master.instrument_id_for(self.venue, venue_symbol)
        return resolved or f"{venue_symbol.upper()}.{self.venue.upper()}.UNKNOWN"

    def normalize(
        self, record: RawRecord, raw: RawReference | None = None
    ) -> tuple[CanonicalEvent, ...]:
        events = self._normalize(record, raw)
        if record.host == SYNTHETIC_HOST:
            events = tuple(event.with_flags(QualityFlag.SYNTHETIC) for event in events)
        return events

    def _normalize(
        self, record: RawRecord, raw: RawReference | None = None
    ) -> tuple[CanonicalEvent, ...]:
        raise NotImplementedError

    def _event(
        self,
        *,
        event_type: CanonicalEventType,
        venue_symbol: str,
        exchange_timestamp_ns: int,
        receive_timestamp_ns: int,
        sequence: int | None,
        payload: Any,
        native: dict[str, str],
        raw: RawReference | None,
        discriminator: str = "",
    ) -> CanonicalEvent:
        instrument_id = self.instrument_id(venue_symbol)
        flags: tuple[QualityFlag, ...] = ()
        if instrument_id.endswith(".UNKNOWN"):
            flags = (QualityFlag.INSTRUMENT_UNKNOWN,)
        if receive_timestamp_ns < exchange_timestamp_ns:
            flags = (*flags, QualityFlag.NEGATIVE_LATENCY)
        return CanonicalEvent(
            event_id=make_event_id(
                self.venue,
                instrument_id,
                event_type,
                sequence,
                exchange_timestamp_ns,
                discriminator,
            ),
            event_type=event_type,
            venue=self.venue,
            instrument_id=instrument_id,
            venue_symbol=venue_symbol.upper(),
            exchange_timestamp_ns=exchange_timestamp_ns,
            receive_timestamp_ns=receive_timestamp_ns,
            sequence=sequence,
            ingest_timestamp_ns=receive_timestamp_ns,
            quality_flags=flags,
            payload=payload,
            native=native,
            raw=raw,
        )


class BinanceNormalizer(VenueNormalizer):
    venue = "binance"
    channels = ("depth", "trade", "depth_snapshot", "instrument")

    def _normalize(
        self, record: RawRecord, raw: RawReference | None = None
    ) -> tuple[CanonicalEvent, ...]:
        message = record.message
        data = message.get("data", message)
        receive_ns = record.receive_timestamp_ns

        if record.channel == "depth_snapshot" or "lastUpdateId" in data:
            symbol = str(data.get("symbol") or message.get("symbol") or "").upper()
            if not symbol:
                raise NormalizationError("binance snapshot is missing its symbol")
            sequence = int(data["lastUpdateId"])
            payload = BookPayload(
                bids=_levels(data.get("bids", ())),
                asks=_levels(data.get("asks", ())),
                first_sequence=sequence,
                final_sequence=sequence,
                is_snapshot=True,
            )
            return (
                self._event(
                    event_type=CanonicalEventType.BOOK_SNAPSHOT,
                    venue_symbol=symbol,
                    exchange_timestamp_ns=int(data.get("E", receive_ns // MS_NS))
                    * MS_NS
                    if "E" in data
                    else receive_ns,
                    receive_timestamp_ns=receive_ns,
                    sequence=sequence,
                    payload=payload,
                    native={"lastUpdateId": str(sequence)},
                    raw=raw,
                ),
            )

        event_name = data.get("e")
        if event_name == "depthUpdate":
            payload = BookPayload(
                bids=_levels(data.get("b", ())),
                asks=_levels(data.get("a", ())),
                first_sequence=int(data["U"]),
                final_sequence=int(data["u"]),
                is_snapshot=False,
            )
            return (
                self._event(
                    event_type=CanonicalEventType.BOOK_DELTA,
                    venue_symbol=str(data["s"]),
                    exchange_timestamp_ns=int(data["E"]) * MS_NS,
                    receive_timestamp_ns=receive_ns,
                    sequence=int(data["u"]),
                    payload=payload,
                    native={"U": str(data["U"]), "u": str(data["u"])},
                    raw=raw,
                ),
            )
        if event_name == "trade":
            payload = TradePayload(
                trade_id=str(data["t"]),
                price=_decimal(data["p"]),
                quantity=_decimal(data["q"]),
                aggressor_side="sell" if bool(data["m"]) else "buy",
                trade_timestamp_ns=int(data["T"]) * MS_NS,
            )
            return (
                self._event(
                    event_type=CanonicalEventType.TRADE,
                    venue_symbol=str(data["s"]),
                    exchange_timestamp_ns=int(data["E"]) * MS_NS,
                    receive_timestamp_ns=receive_ns,
                    sequence=int(data["t"]),
                    payload=payload,
                    native={"m": str(bool(data["m"])).lower(), "t": str(data["t"])},
                    raw=raw,
                ),
            )
        if record.channel == "instrument":
            symbol = str(data["symbol"]).upper()
            return (
                self._event(
                    event_type=CanonicalEventType.INSTRUMENT_STATUS,
                    venue_symbol=symbol,
                    exchange_timestamp_ns=int(
                        data.get("timestamp_ns", receive_ns)
                    ),
                    receive_timestamp_ns=receive_ns,
                    sequence=None,
                    payload=InstrumentStatusPayload(
                        status=str(data["status"]).lower(),
                        reason=str(data.get("reason", "")),
                    ),
                    native={"status": str(data["status"])},
                    raw=raw,
                    discriminator=str(data["status"]),
                ),
            )
        return ()


class OkxNormalizer(VenueNormalizer):
    venue = "okx"
    channels = ("books", "trades", "instruments")

    def _normalize(
        self, record: RawRecord, raw: RawReference | None = None
    ) -> tuple[CanonicalEvent, ...]:
        message = record.message
        arg = message.get("arg", {})
        channel = str(arg.get("channel", record.channel))
        rows = message.get("data", [])
        if not isinstance(rows, list):
            raise NormalizationError("okx data must be a list")
        receive_ns = record.receive_timestamp_ns
        events: list[CanonicalEvent] = []

        if channel in {"books", "books5", "books-l2-tbt"}:
            is_snapshot = str(message.get("action", "snapshot")) == "snapshot"
            symbol = str(arg.get("instId", ""))
            for row in rows:
                sequence = int(row.get("seqId", row.get("ts", 0)))
                # OKX publishes the *previous* sequence; the canonical field is
                # the first sequence this delta covers, so it is prevSeqId + 1.
                payload = BookPayload(
                    bids=_levels(row.get("bids", ())),
                    asks=_levels(row.get("asks", ())),
                    first_sequence=(
                        None
                        if row.get("prevSeqId") is None
                        else int(row["prevSeqId"]) + 1
                    ),
                    final_sequence=sequence,
                    is_snapshot=is_snapshot,
                    # OKX's public books channel currently sends checksum 0,
                    # meaning none is published; only a non-zero value is a
                    # CRC32 that can be verified.
                    checksum=(int(row["checksum"]) or None)
                    if row.get("checksum") not in (None, "")
                    else None,
                )
                events.append(
                    self._event(
                        event_type=(
                            CanonicalEventType.BOOK_SNAPSHOT
                            if is_snapshot
                            else CanonicalEventType.BOOK_DELTA
                        ),
                        venue_symbol=symbol,
                        exchange_timestamp_ns=int(row["ts"]) * MS_NS,
                        receive_timestamp_ns=receive_ns,
                        sequence=sequence,
                        payload=payload,
                        native={
                            "action": str(message.get("action", "snapshot")),
                            "seqId": str(row.get("seqId", "")),
                            "prevSeqId": str(row.get("prevSeqId", "")),
                            "checksum": str(row.get("checksum", "")),
                        },
                        raw=raw,
                    )
                )
            return tuple(events)

        if channel == "trades":
            for row in rows:
                payload = TradePayload(
                    trade_id=str(row["tradeId"]),
                    price=_decimal(row["px"]),
                    quantity=_decimal(row["sz"]),
                    aggressor_side=str(row.get("side", "")).lower() or None,
                    trade_timestamp_ns=int(row["ts"]) * MS_NS,
                )
                events.append(
                    self._event(
                        event_type=CanonicalEventType.TRADE,
                        venue_symbol=str(row.get("instId", arg.get("instId", ""))),
                        exchange_timestamp_ns=int(row["ts"]) * MS_NS,
                        receive_timestamp_ns=receive_ns,
                        sequence=_safe_int(row["tradeId"]),
                        payload=payload,
                        native={
                            "side": str(row.get("side", "")),
                            "tradeId": str(row["tradeId"]),
                        },
                        raw=raw,
                        discriminator=str(row["tradeId"]),
                    )
                )
            return tuple(events)

        if channel == "instruments":
            for row in rows:
                events.append(
                    self._event(
                        event_type=CanonicalEventType.INSTRUMENT_STATUS,
                        venue_symbol=str(row["instId"]),
                        exchange_timestamp_ns=int(row.get("ts", receive_ns // MS_NS))
                        * MS_NS,
                        receive_timestamp_ns=receive_ns,
                        sequence=None,
                        payload=InstrumentStatusPayload(
                            status=_okx_status(str(row.get("state", "live"))),
                            reason=str(row.get("state", "")),
                        ),
                        native={"state": str(row.get("state", ""))},
                        raw=raw,
                        discriminator=str(row.get("state", "")),
                    )
                )
            return tuple(events)
        return ()


class BybitNormalizer(VenueNormalizer):
    venue = "bybit"
    channels = ("orderbook", "publicTrade", "instrument")

    def _normalize(
        self, record: RawRecord, raw: RawReference | None = None
    ) -> tuple[CanonicalEvent, ...]:
        message = record.message
        topic = str(message.get("topic", ""))
        receive_ns = record.receive_timestamp_ns
        frame_type = str(message.get("type", "snapshot"))
        events: list[CanonicalEvent] = []

        if topic.startswith("orderbook"):
            row = message.get("data", {})
            is_snapshot = frame_type == "snapshot"
            sequence = int(row.get("u", message.get("ts", 0)))
            payload = BookPayload(
                bids=_levels(row.get("b", ())),
                asks=_levels(row.get("a", ())),
                first_sequence=sequence,
                final_sequence=sequence,
                is_snapshot=is_snapshot,
            )
            events.append(
                self._event(
                    event_type=(
                        CanonicalEventType.BOOK_SNAPSHOT
                        if is_snapshot
                        else CanonicalEventType.BOOK_DELTA
                    ),
                    venue_symbol=str(row["s"]),
                    exchange_timestamp_ns=int(message["ts"]) * MS_NS,
                    receive_timestamp_ns=receive_ns,
                    sequence=sequence,
                    payload=payload,
                    native={
                        "type": frame_type,
                        "u": str(row.get("u", "")),
                        "seq": str(row.get("seq", "")),
                    },
                    raw=raw,
                )
            )
            return tuple(events)

        if topic.startswith("publicTrade"):
            for row in message.get("data", []):
                payload = TradePayload(
                    trade_id=str(row["i"]),
                    price=_decimal(row["p"]),
                    quantity=_decimal(row["v"]),
                    aggressor_side=str(row.get("S", "")).lower() or None,
                    trade_timestamp_ns=int(row["T"]) * MS_NS,
                )
                events.append(
                    self._event(
                        event_type=CanonicalEventType.TRADE,
                        venue_symbol=str(row["s"]),
                        exchange_timestamp_ns=int(row["T"]) * MS_NS,
                        receive_timestamp_ns=receive_ns,
                        sequence=_safe_int(row["i"]),
                        payload=payload,
                        native={"S": str(row.get("S", "")), "i": str(row["i"])},
                        raw=raw,
                        discriminator=str(row["i"]),
                    )
                )
            return tuple(events)

        if topic.startswith("instrument") or record.channel == "instrument":
            row = message.get("data", {})
            return (
                self._event(
                    event_type=CanonicalEventType.INSTRUMENT_STATUS,
                    venue_symbol=str(row["s"]),
                    exchange_timestamp_ns=int(message.get("ts", receive_ns // MS_NS))
                    * MS_NS,
                    receive_timestamp_ns=receive_ns,
                    sequence=None,
                    payload=InstrumentStatusPayload(
                        status=_bybit_status(str(row.get("status", "Trading"))),
                        reason=str(row.get("status", "")),
                    ),
                    native={"status": str(row.get("status", ""))},
                    raw=raw,
                    discriminator=str(row.get("status", "")),
                ),
            )
        return ()


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _okx_status(state: str) -> str:
    return {
        "live": "trading",
        "suspend": "suspended",
        "preopen": "pre_open",
        "test": "pre_open",
        "expired": "expired",
    }.get(state.lower(), "suspended")


def _bybit_status(status: str) -> str:
    return {
        "trading": "trading",
        "pretrading": "pre_open",
        "settling": "maintenance",
        "delivering": "maintenance",
        "closed": "delisted",
    }.get(status.lower(), "suspended")


NORMALIZERS: dict[str, type[VenueNormalizer]] = {
    "binance": BinanceNormalizer,
    "okx": OkxNormalizer,
    "bybit": BybitNormalizer,
}


def build_normalizer(venue: str, security_master: SecurityMaster) -> VenueNormalizer:
    try:
        factory = NORMALIZERS[venue.lower()]
    except KeyError as error:
        raise NormalizationError(f"no normalizer for venue {venue}") from error
    return factory(security_master)


def supported_venues() -> tuple[str, ...]:
    return tuple(sorted(NORMALIZERS))


@dataclass(frozen=True, slots=True)
class VenueEndpoint:
    """Public endpoints and channel names for one venue."""

    venue: str
    websocket_url: str
    rest_base_url: str
    depth_channel: str
    trade_channel: str
    instrument_channel: str
    order_types: tuple[str, ...] = ("market", "limit", "post_only")
    time_in_force: tuple[str, ...] = ("gtc", "ioc", "fok")
    maker_supported: bool = True
    depth_levels: int = 50
    max_orders_per_second: int = 20
    supports_cancel_replace: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "websocket_url": self.websocket_url,
            "rest_base_url": self.rest_base_url,
            "channels": {
                "depth": self.depth_channel,
                "trades": self.trade_channel,
                "instruments": self.instrument_channel,
            },
            "order_types": list(self.order_types),
            "time_in_force": list(self.time_in_force),
            "maker_supported": self.maker_supported,
            "depth_levels": self.depth_levels,
            "max_orders_per_second": self.max_orders_per_second,
            "supports_cancel_replace": self.supports_cancel_replace,
        }


VENUE_ENDPOINTS: dict[str, VenueEndpoint] = {
    "binance": VenueEndpoint(
        venue="binance",
        websocket_url="wss://stream.binance.com:9443/stream",
        rest_base_url="https://api.binance.com",
        depth_channel="{symbol_lower}@depth@100ms",
        trade_channel="{symbol_lower}@trade",
        instrument_channel="/api/v3/exchangeInfo",
    ),
    "okx": VenueEndpoint(
        venue="okx",
        websocket_url="wss://ws.okx.com:8443/ws/v5/public",
        rest_base_url="https://www.okx.com",
        depth_channel="books",
        trade_channel="trades",
        instrument_channel="/api/v5/public/instruments",
    ),
    "bybit": VenueEndpoint(
        venue="bybit",
        websocket_url="wss://stream.bybit.com/v5/public/spot",
        rest_base_url="https://api.bybit.com",
        depth_channel="orderbook.50.{symbol_upper}",
        trade_channel="publicTrade.{symbol_upper}",
        instrument_channel="/v5/market/instruments-info",
    ),
}


def venue_capability_matrix() -> list[dict[str, Any]]:
    """Section 10 deliverable: venue, channel and order-capability matrix."""

    return [
        {
            **VENUE_ENDPOINTS[venue].to_dict(),
            "normalizer": NORMALIZERS[venue].__name__,
            "canonical_channels": list(NORMALIZERS[venue].channels),
            "adapter_version": ADAPTER_VERSION,
        }
        for venue in supported_venues()
    ]


class PublicFeed:
    """Live public WebSocket capture for one venue and one symbol.

    Yields :class:`RawRecord` frames so the raw payload is preserved before any
    normalization happens, as MR-04 raw preservation requires.
    """

    def __init__(
        self,
        venue: str,
        symbol: str,
        *,
        endpoint: VenueEndpoint | None = None,
        clock_ns: Callable[[], int] | None = None,
        host: str | None = None,
    ) -> None:
        self.venue = venue.lower()
        self.symbol = symbol.upper()
        self.endpoint = endpoint or VENUE_ENDPOINTS[self.venue]
        self._clock_ns = clock_ns or _monotonic_wall_ns
        self._host = host or socket.gethostname()

    @property
    def stream_url(self) -> str:
        if self.venue == "binance":
            streams = "/".join(
                (
                    self.endpoint.depth_channel.format(
                        symbol_lower=self.symbol.lower()
                    ),
                    self.endpoint.trade_channel.format(
                        symbol_lower=self.symbol.lower()
                    ),
                )
            )
            return f"{self.endpoint.websocket_url}?streams={streams}"
        return self.endpoint.websocket_url

    def subscribe_frame(self) -> dict[str, Any] | None:
        if self.venue == "okx":
            return {
                "op": "subscribe",
                "args": [
                    {"channel": "books", "instId": _okx_symbol(self.symbol)},
                    {"channel": "trades", "instId": _okx_symbol(self.symbol)},
                ],
            }
        if self.venue == "bybit":
            return {
                "op": "subscribe",
                "args": [
                    self.endpoint.depth_channel.format(symbol_upper=self.symbol),
                    self.endpoint.trade_channel.format(symbol_upper=self.symbol),
                ],
            }
        return None

    def channel_for(self, message: dict[str, Any]) -> str:
        if self.venue == "binance":
            stream = str(message.get("stream", ""))
            return "trade" if "@trade" in stream else "depth"
        if self.venue == "okx":
            return str(message.get("arg", {}).get("channel", "books"))
        topic = str(message.get("topic", ""))
        return topic.split(".")[0] or "orderbook"

    def wrap(self, message: dict[str, Any]) -> RawRecord:
        return RawRecord(
            venue=self.venue,
            channel=self.channel_for(message),
            host=self._host,
            receive_timestamp_ns=self._clock_ns(),
            sequence=None,
            adapter_version=ADAPTER_VERSION,
            schema_version=SCHEMA_VERSION,
            message=message,
        )

    async def snapshot(self) -> RawRecord | None:
        """REST depth snapshot used to seed incremental streams."""

        import aiohttp

        if self.venue != "binance":
            return None
        url = f"{self.endpoint.rest_base_url}/api/v3/depth"
        params = {"symbol": self.symbol, "limit": str(self.endpoint.depth_levels)}
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                payload = await response.json()
        payload["symbol"] = self.symbol
        return RawRecord(
            venue=self.venue,
            channel="depth_snapshot",
            host=self._host,
            receive_timestamp_ns=self._clock_ns(),
            sequence=int(payload["lastUpdateId"]),
            adapter_version=ADAPTER_VERSION,
            schema_version=SCHEMA_VERSION,
            message=payload,
        )

    async def stream(
        self, *, max_records: int | None = None, duration_seconds: float | None = None
    ) -> AsyncIterator[RawRecord]:
        import aiohttp

        deadline = (
            None
            if duration_seconds is None
            else asyncio.get_running_loop().time() + duration_seconds
        )
        produced = 0
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=45)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(self.stream_url, heartbeat=20) as socket_:
                frame = self.subscribe_frame()
                if frame is not None:
                    await socket_.send_json(frame)
                async for message in socket_:
                    if message.type is not aiohttp.WSMsgType.TEXT:
                        continue
                    try:
                        payload = message.json()
                    except ValueError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if payload.get("event") in {"subscribe", "error"}:
                        continue
                    if payload.get("op") in {"subscribe", "pong"}:
                        continue
                    if payload.get("success") is not None and "topic" not in payload:
                        continue
                    yield self.wrap(payload)
                    produced += 1
                    if max_records is not None and produced >= max_records:
                        return
                    if (
                        deadline is not None
                        and asyncio.get_running_loop().time() >= deadline
                    ):
                        return


def _okx_symbol(symbol: str) -> str:
    if "-" in symbol:
        return symbol.upper()
    for quote in ("USDT", "USDC", "USD"):
        if symbol.upper().endswith(quote):
            return f"{symbol.upper()[: -len(quote)]}-{quote}"
    return symbol.upper()


def venue_symbol_for(venue: str, canonical_symbol: str) -> str:
    if venue.lower() == "okx":
        return _okx_symbol(canonical_symbol)
    return canonical_symbol.upper()


def _monotonic_wall_ns() -> int:
    import time

    return time.time_ns()
