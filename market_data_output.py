from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import ipaddress
import json
import os
from pathlib import Path
import socket
import struct
import time
from typing import Any, Protocol

from .models import BookState, MarketEvent
from .models import stable_id
from .orderbook import OrderBook


UDP_SCHEMA_VERSION = "1.0.0"
OHLC_SCHEMA_VERSION = "1.0.0"
MAX_UDP_PAYLOAD_BYTES = 65_507


def _utc_iso(timestamp_ns: int) -> str:
    return datetime.fromtimestamp(
        timestamp_ns / 1_000_000_000, timezone.utc
    ).isoformat()


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class OhlcBar:
    schema_version: str
    venue: str
    instrument_id: str
    interval: str
    open_ts_ns: int
    close_ts_ns: int
    open_ts_utc: str
    close_ts_utc: str
    is_final: bool
    sample_count: int
    first_event_id: str
    last_event_id: str
    bid_open: Decimal
    bid_high: Decimal
    bid_low: Decimal
    bid_close: Decimal
    ask_open: Decimal
    ask_high: Decimal
    ask_low: Decimal
    ask_close: Decimal
    mid_open: Decimal
    mid_high: Decimal
    mid_low: Decimal
    mid_close: Decimal
    spread_open: Decimal
    spread_high: Decimal
    spread_low: Decimal
    spread_close: Decimal

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        for key, value in tuple(row.items()):
            if isinstance(value, Decimal):
                row[key] = str(value)
        return row


@dataclass(frozen=True, slots=True)
class TradeTick:
    schema_version: str
    event_id: str
    venue: str
    instrument_id: str
    symbol: str
    trade_id: str
    exchange_ts_ns: int
    source_ts_ns: int
    receive_ts_ns: int
    price: Decimal
    quantity: Decimal
    aggressor_side: str
    raw_ref: str
    adapter_version: str
    native: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["price"] = str(self.price)
        row["quantity"] = str(self.quantity)
        return row


def normalize_binance_trade(
    payload: dict[str, Any],
    receive_ts_ns: int,
    instrument_id: str,
    raw_ref: str,
) -> TradeTick:
    symbol = str(payload["s"])
    trade_id = str(payload["t"])
    exchange_ts_ns = int(payload.get("T", payload.get("E"))) * 1_000_000
    return TradeTick(
        schema_version="1.0.0",
        event_id=stable_id("trade", "BINANCE", symbol, trade_id, raw_ref),
        venue="BINANCE",
        instrument_id=instrument_id,
        symbol=symbol,
        trade_id=trade_id,
        exchange_ts_ns=exchange_ts_ns,
        source_ts_ns=exchange_ts_ns,
        receive_ts_ns=receive_ts_ns,
        price=Decimal(str(payload["p"])),
        quantity=Decimal(str(payload["q"])),
        aggressor_side="SELL" if bool(payload.get("m")) else "BUY",
        raw_ref=raw_ref,
        adapter_version="binance-trade-v1",
        native=payload,
    )


def normalize_bybit_trades(
    payload: dict[str, Any],
    receive_ts_ns: int,
    instrument_id: str,
    raw_ref: str,
) -> tuple[TradeTick, ...]:
    ticks = []
    for index, item in enumerate(payload["data"]):
        symbol = str(item["s"])
        trade_id = str(item.get("i", f"{item['T']}-{index}"))
        exchange_ts_ns = int(item["T"]) * 1_000_000
        ticks.append(TradeTick(
            schema_version="1.0.0",
            event_id=stable_id("trade", "BYBIT", symbol, trade_id, raw_ref, index),
            venue="BYBIT",
            instrument_id=instrument_id,
            symbol=symbol,
            trade_id=trade_id,
            exchange_ts_ns=exchange_ts_ns,
            source_ts_ns=exchange_ts_ns,
            receive_ts_ns=receive_ts_ns,
            price=Decimal(str(item["p"])),
            quantity=Decimal(str(item["v"])),
            aggressor_side=str(item.get("S", "")).upper(),
            raw_ref=raw_ref,
            adapter_version="bybit-public-trade-v1",
            native=item,
        ))
    return tuple(ticks)


@dataclass(slots=True)
class _MutableOhlc:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    @classmethod
    def from_value(cls, value: Decimal) -> "_MutableOhlc":
        return cls(value, value, value, value)

    def update(self, value: Decimal) -> None:
        self.high = max(self.high, value)
        self.low = min(self.low, value)
        self.close = value


@dataclass(slots=True)
class _MutableBar:
    venue: str
    instrument_id: str
    open_ts_ns: int
    close_ts_ns: int
    sample_count: int
    first_event_id: str
    last_event_id: str
    bid: _MutableOhlc
    ask: _MutableOhlc
    mid: _MutableOhlc
    spread: _MutableOhlc

    def update(
        self,
        bid: Decimal,
        ask: Decimal,
        mid: Decimal,
        spread: Decimal,
        event_id: str,
    ) -> None:
        self.bid.update(bid)
        self.ask.update(ask)
        self.mid.update(mid)
        self.spread.update(spread)
        self.sample_count += 1
        self.last_event_id = event_id


class OrderBookOhlcResampler:
    """Receive-time, per-venue OHLC derived from valid reconstructed top of book."""

    def __init__(self, interval_seconds: int = 60) -> None:
        if interval_seconds <= 0:
            raise ValueError("OHLC interval must be positive")
        self.interval_seconds = interval_seconds
        self.interval_ns = interval_seconds * 1_000_000_000
        self.interval_label = "1m" if interval_seconds == 60 else f"{interval_seconds}s"
        self._bars: dict[tuple[str, str], _MutableBar] = {}
        self.samples = 0
        self.completed_bars = 0
        self.partial_bars = 0
        self.late_samples = 0
        self.invalid_book_samples = 0
        self.ignored_event_samples = 0

    def update(
        self,
        book: OrderBook,
        event: MarketEvent,
        *,
        availability_ts_ns: int | None = None,
    ) -> tuple[OhlcBar, ...]:
        if book.state != BookState.VALID or not book.bids or not book.asks:
            self.invalid_book_samples += 1
            return ()
        if availability_ts_ns is None and book.last_event_id != event.event_id:
            self.ignored_event_samples += 1
            return ()
        timestamp_ns = (
            availability_ts_ns if availability_ts_ns is not None else event.receive_ts_ns
        )
        source_event_id = book.last_event_id or event.event_id
        bucket_ns = timestamp_ns // self.interval_ns * self.interval_ns
        key = (book.venue, book.instrument_id)
        current = self._bars.get(key)
        completed: list[OhlcBar] = []
        if current is not None and bucket_ns < current.open_ts_ns:
            self.late_samples += 1
            return ()
        if current is not None and bucket_ns > current.open_ts_ns:
            completed.append(self._freeze(current, is_final=True))
            self.completed_bars += 1
            current = None
        bid = book.best_bid
        ask = book.best_ask
        mid = (bid + ask) / Decimal("2")
        spread = ask - bid
        if current is None:
            current = _MutableBar(
                venue=book.venue,
                instrument_id=book.instrument_id,
                open_ts_ns=bucket_ns,
                close_ts_ns=bucket_ns + self.interval_ns,
                sample_count=1,
                first_event_id=source_event_id,
                last_event_id=source_event_id,
                bid=_MutableOhlc.from_value(bid),
                ask=_MutableOhlc.from_value(ask),
                mid=_MutableOhlc.from_value(mid),
                spread=_MutableOhlc.from_value(spread),
            )
            self._bars[key] = current
        else:
            current.update(bid, ask, mid, spread, source_event_id)
        self.samples += 1
        return tuple(completed)

    def finalize_until(self, timestamp_ns: int) -> tuple[OhlcBar, ...]:
        completed: list[OhlcBar] = []
        for key, bar in sorted(tuple(self._bars.items())):
            if bar.close_ts_ns <= timestamp_ns:
                completed.append(self._freeze(bar, is_final=True))
                del self._bars[key]
                self.completed_bars += 1
        return tuple(completed)

    def flush_partial(self) -> tuple[OhlcBar, ...]:
        partial = tuple(
            self._freeze(bar, is_final=False)
            for _, bar in sorted(self._bars.items())
        )
        self.partial_bars += len(partial)
        self._bars.clear()
        return partial

    def next_boundary_ns(self, now_ns: int) -> int:
        return (now_ns // self.interval_ns + 1) * self.interval_ns

    def stats_snapshot(self) -> dict[str, int | str]:
        return {
            "interval": self.interval_label,
            "samples": self.samples,
            "completed_bars": self.completed_bars,
            "partial_bars": self.partial_bars,
            "late_samples": self.late_samples,
            "invalid_book_samples": self.invalid_book_samples,
            "ignored_event_samples": self.ignored_event_samples,
            "open_bars": len(self._bars),
        }

    def _freeze(self, bar: _MutableBar, *, is_final: bool) -> OhlcBar:
        return OhlcBar(
            schema_version=OHLC_SCHEMA_VERSION,
            venue=bar.venue,
            instrument_id=bar.instrument_id,
            interval=self.interval_label,
            open_ts_ns=bar.open_ts_ns,
            close_ts_ns=bar.close_ts_ns,
            open_ts_utc=_utc_iso(bar.open_ts_ns),
            close_ts_utc=_utc_iso(bar.close_ts_ns),
            is_final=is_final,
            sample_count=bar.sample_count,
            first_event_id=bar.first_event_id,
            last_event_id=bar.last_event_id,
            bid_open=bar.bid.open,
            bid_high=bar.bid.high,
            bid_low=bar.bid.low,
            bid_close=bar.bid.close,
            ask_open=bar.ask.open,
            ask_high=bar.ask.high,
            ask_low=bar.ask.low,
            ask_close=bar.ask.close,
            mid_open=bar.mid.open,
            mid_high=bar.mid.high,
            mid_low=bar.mid.low,
            mid_close=bar.mid.close,
            spread_open=bar.spread.open,
            spread_high=bar.spread.high,
            spread_low=bar.spread.low,
            spread_close=bar.spread.close,
        )


@dataclass(frozen=True, slots=True)
class LtpOhlcBar:
    schema_version: str
    venue: str
    instrument_id: str
    interval: str
    open_ts_ns: int
    close_ts_ns: int
    open_ts_utc: str
    close_ts_utc: str
    is_final: bool
    trade_count: int
    first_trade_event_id: str
    last_trade_event_id: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    base_volume: Decimal
    quote_volume: Decimal

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        for key, value in tuple(row.items()):
            if isinstance(value, Decimal):
                row[key] = str(value)
        return row


@dataclass(slots=True)
class _MutableLtpBar:
    venue: str
    instrument_id: str
    open_ts_ns: int
    close_ts_ns: int
    trade_count: int
    first_trade_event_id: str
    last_trade_event_id: str
    price: _MutableOhlc
    base_volume: Decimal
    quote_volume: Decimal


class LtpOhlcResampler:
    """Per-venue one-minute OHLC built only from normalized last trades."""

    def __init__(self, interval_seconds: int = 60) -> None:
        if interval_seconds <= 0:
            raise ValueError("LTP OHLC interval must be positive")
        self.interval_seconds = interval_seconds
        self.interval_ns = interval_seconds * 1_000_000_000
        self.interval_label = "1m" if interval_seconds == 60 else f"{interval_seconds}s"
        self._bars: dict[tuple[str, str], _MutableLtpBar] = {}
        self.trades = 0
        self.completed_bars = 0
        self.partial_bars = 0
        self.late_trades = 0

    def update(self, tick: TradeTick) -> tuple[LtpOhlcBar, ...]:
        bucket_ns = tick.receive_ts_ns // self.interval_ns * self.interval_ns
        key = (tick.venue, tick.instrument_id)
        current = self._bars.get(key)
        completed: list[LtpOhlcBar] = []
        if current is not None and bucket_ns < current.open_ts_ns:
            self.late_trades += 1
            return ()
        if current is not None and bucket_ns > current.open_ts_ns:
            completed.append(self._freeze(current, is_final=True))
            self.completed_bars += 1
            current = None
        notional = tick.price * tick.quantity
        if current is None:
            current = _MutableLtpBar(
                venue=tick.venue,
                instrument_id=tick.instrument_id,
                open_ts_ns=bucket_ns,
                close_ts_ns=bucket_ns + self.interval_ns,
                trade_count=1,
                first_trade_event_id=tick.event_id,
                last_trade_event_id=tick.event_id,
                price=_MutableOhlc.from_value(tick.price),
                base_volume=tick.quantity,
                quote_volume=notional,
            )
            self._bars[key] = current
        else:
            current.price.update(tick.price)
            current.trade_count += 1
            current.last_trade_event_id = tick.event_id
            current.base_volume += tick.quantity
            current.quote_volume += notional
        self.trades += 1
        return tuple(completed)

    def finalize_until(self, timestamp_ns: int) -> tuple[LtpOhlcBar, ...]:
        completed = []
        for key, bar in sorted(tuple(self._bars.items())):
            if bar.close_ts_ns <= timestamp_ns:
                completed.append(self._freeze(bar, is_final=True))
                del self._bars[key]
                self.completed_bars += 1
        return tuple(completed)

    def flush_partial(self) -> tuple[LtpOhlcBar, ...]:
        partial = tuple(
            self._freeze(bar, is_final=False)
            for _, bar in sorted(self._bars.items())
        )
        self.partial_bars += len(partial)
        self._bars.clear()
        return partial

    def stats_snapshot(self) -> dict[str, int | str]:
        return {
            "interval": self.interval_label,
            "trades": self.trades,
            "completed_bars": self.completed_bars,
            "partial_bars": self.partial_bars,
            "late_trades": self.late_trades,
            "open_bars": len(self._bars),
        }

    def _freeze(self, bar: _MutableLtpBar, *, is_final: bool) -> LtpOhlcBar:
        return LtpOhlcBar(
            schema_version=OHLC_SCHEMA_VERSION,
            venue=bar.venue,
            instrument_id=bar.instrument_id,
            interval=self.interval_label,
            open_ts_ns=bar.open_ts_ns,
            close_ts_ns=bar.close_ts_ns,
            open_ts_utc=_utc_iso(bar.open_ts_ns),
            close_ts_utc=_utc_iso(bar.close_ts_ns),
            is_final=is_final,
            trade_count=bar.trade_count,
            first_trade_event_id=bar.first_trade_event_id,
            last_trade_event_id=bar.last_trade_event_id,
            open=bar.price.open,
            high=bar.price.high,
            low=bar.price.low,
            close=bar.price.close,
            base_volume=bar.base_volume,
            quote_volume=bar.quote_volume,
        )


class OhlcCsvWriter:
    FIELDNAMES = tuple(OhlcBar.__dataclass_fields__)

    def __init__(self, path: Path, *, durable: bool = True) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.durable = durable
        self._handle = path.open("x", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(self._handle, fieldnames=self.FIELDNAMES)
        self._writer.writeheader()
        self.count = 0
        self._flush()

    def _flush(self) -> None:
        self._handle.flush()
        if self.durable:
            os.fsync(self._handle.fileno())

    def append(self, bar: OhlcBar) -> None:
        self._writer.writerow(bar.to_dict())
        self.count += 1
        self._flush()

    def close(self) -> dict[str, Any]:
        self._flush()
        self._handle.close()
        return {
            "path": str(self.path),
            "rows": self.count,
            "sha256": _file_sha256(self.path),
        }


class LtpOhlcCsvWriter:
    FIELDNAMES = tuple(LtpOhlcBar.__dataclass_fields__)

    def __init__(self, path: Path, *, durable: bool = True) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.durable = durable
        self._handle = path.open("x", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(self._handle, fieldnames=self.FIELDNAMES)
        self._writer.writeheader()
        self.count = 0
        self._flush()

    def _flush(self) -> None:
        self._handle.flush()
        if self.durable:
            os.fsync(self._handle.fileno())

    def append(self, bar: LtpOhlcBar) -> None:
        self._writer.writerow(bar.to_dict())
        self.count += 1
        self._flush()

    def close(self) -> dict[str, Any]:
        self._flush()
        self._handle.close()
        return {
            "path": str(self.path),
            "rows": self.count,
            "sha256": _file_sha256(self.path),
        }


@dataclass(frozen=True, slots=True)
class UdpPublisherSettings:
    group: str = "239.255.77.77"
    port: int = 19_001
    interface: str = "0.0.0.0"
    ttl: int = 1
    depth: int = 20
    max_datagram_bytes: int = 60_000

    def validate(self) -> None:
        address = ipaddress.ip_address(self.group)
        if address.version != 4 or not address.is_multicast:
            raise ValueError("UDP group must be an IPv4 multicast address")
        if not 1 <= self.port <= 65_535:
            raise ValueError("UDP port must be between 1 and 65535")
        ipaddress.IPv4Address(self.interface)
        if not 0 <= self.ttl <= 255:
            raise ValueError("UDP multicast TTL must be between 0 and 255")
        if self.depth <= 0:
            raise ValueError("UDP book depth must be positive")
        if not 1 <= self.max_datagram_bytes <= MAX_UDP_PAYLOAD_BYTES:
            raise ValueError(f"max datagram bytes cannot exceed {MAX_UDP_PAYLOAD_BYTES}")


@dataclass(slots=True)
class UdpPublisherStats:
    datagrams_sent: int = 0
    bytes_sent: int = 0
    orderbook_messages: int = 0
    ohlc_messages: int = 0
    trade_messages: int = 0
    ltp_ohlc_messages: int = 0
    dropped_messages: int = 0
    send_errors: int = 0
    last_error: str = ""


class MarketDataPublisher(Protocol):
    def publish_orderbook(
        self, book: OrderBook, event: MarketEvent | None, published_ts_ns: int
    ) -> bool: ...

    def publish_ohlc(self, bar: OhlcBar, published_ts_ns: int) -> bool: ...

    def publish_trade(self, tick: TradeTick, published_ts_ns: int) -> bool: ...

    def publish_ltp_ohlc(self, bar: LtpOhlcBar, published_ts_ns: int) -> bool: ...

    def stats_snapshot(self) -> dict[str, Any]: ...

    def close(self) -> None: ...


class UdpMulticastPublisher:
    """Non-blocking UDP multicast publisher for reconstructed books and OHLC bars."""

    def __init__(
        self,
        settings: UdpPublisherSettings | None = None,
        *,
        socket_factory: Any = socket.socket,
    ) -> None:
        self.settings = settings or UdpPublisherSettings()
        self.settings.validate()
        self.destination = (self.settings.group, self.settings.port)
        self.socket = socket_factory(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.socket.setsockopt(
            socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("B", self.settings.ttl)
        )
        self.socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        if self.settings.interface != "0.0.0.0":
            self.socket.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_MULTICAST_IF,
                socket.inet_aton(self.settings.interface),
            )
        self.socket.setblocking(False)
        self.sequence = 0
        self.stats = UdpPublisherStats()

    def publish_orderbook(
        self, book: OrderBook, event: MarketEvent | None, published_ts_ns: int
    ) -> bool:
        bids = sorted(book.bids.items(), reverse=True)[: self.settings.depth]
        asks = sorted(book.asks.items())[: self.settings.depth]
        best_bid = bids[0][0] if bids else None
        best_ask = asks[0][0] if asks else None
        midpoint = (
            (best_bid + best_ask) / Decimal("2")
            if best_bid is not None and best_ask is not None
            else None
        )
        payload = {
            "venue": book.venue,
            "instrument_id": book.instrument_id,
            "state": book.state.value,
            "tradable": book.is_tradable,
            "invalid_reason": book.invalid_reason,
            "event_id": event.event_id if event is not None else None,
            "event_type": event.event_type.value if event is not None else "CONTROL",
            "exchange_ts_ns": event.exchange_ts_ns if event is not None else None,
            "source_ts_ns": event.source_ts_ns if event is not None else None,
            "receive_ts_ns": event.receive_ts_ns if event is not None else published_ts_ns,
            "sequence_start": event.sequence_start if event is not None else None,
            "sequence": book.sequence,
            "raw_ref": event.raw_ref if event is not None else None,
            "event_schema_version": event.schema_version if event is not None else None,
            "adapter_version": event.adapter_version if event is not None else None,
            "quality_flags": list(event.quality_flags) if event is not None else [],
            "best_bid": str(best_bid) if best_bid is not None else None,
            "best_ask": str(best_ask) if best_ask is not None else None,
            "midpoint": str(midpoint) if midpoint is not None else None,
            "bids": [[str(price), str(quantity)] for price, quantity in bids],
            "asks": [[str(price), str(quantity)] for price, quantity in asks],
            "depth": self.settings.depth,
            "book_sha256": book.snapshot_hash(),
        }
        sent = self._send("ORDERBOOK", payload, published_ts_ns)
        self.stats.orderbook_messages += int(sent)
        return sent

    def publish_ohlc(self, bar: OhlcBar, published_ts_ns: int) -> bool:
        sent = self._send("OHLC_1M", bar.to_dict(), published_ts_ns)
        self.stats.ohlc_messages += int(sent)
        return sent

    def publish_trade(self, tick: TradeTick, published_ts_ns: int) -> bool:
        sent = self._send("TRADE", tick.to_dict(), published_ts_ns)
        self.stats.trade_messages += int(sent)
        return sent

    def publish_ltp_ohlc(self, bar: LtpOhlcBar, published_ts_ns: int) -> bool:
        sent = self._send("LTP_OHLC_1M", bar.to_dict(), published_ts_ns)
        self.stats.ltp_ohlc_messages += int(sent)
        return sent

    def _send(
        self, message_type: str, payload: dict[str, Any], published_ts_ns: int
    ) -> bool:
        self.sequence += 1
        envelope = {
            "schema_version": UDP_SCHEMA_VERSION,
            "message_type": message_type,
            "publisher_sequence": self.sequence,
            "published_ts_ns": published_ts_ns,
            "payload": payload,
        }
        encoded = json.dumps(
            envelope, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > self.settings.max_datagram_bytes:
            self.stats.dropped_messages += 1
            self.stats.last_error = (
                f"{message_type} datagram is {len(encoded)} bytes; "
                f"limit is {self.settings.max_datagram_bytes}"
            )
            return False
        try:
            sent = self.socket.sendto(encoded, self.destination)
        except (BlockingIOError, OSError) as exc:
            self.stats.dropped_messages += 1
            self.stats.send_errors += 1
            self.stats.last_error = f"{type(exc).__name__}: {exc}"
            return False
        if sent != len(encoded):
            self.stats.dropped_messages += 1
            self.stats.last_error = f"partial UDP send: {sent}/{len(encoded)} bytes"
            return False
        self.stats.datagrams_sent += 1
        self.stats.bytes_sent += sent
        return True

    def stats_snapshot(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "group": self.settings.group,
            "port": self.settings.port,
            "interface": self.settings.interface,
            "ttl": self.settings.ttl,
            "depth": self.settings.depth,
            **asdict(self.stats),
        }

    def close(self) -> None:
        self.socket.close()
