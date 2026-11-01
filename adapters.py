from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .models import EventType, MarketEvent, stable_id


SymbolResolver = Callable[[str, str], str]


def normalize_binance_depth(
    payload: dict[str, Any],
    receive_ts_ns: int,
    resolve: SymbolResolver,
    *,
    raw_ref: str = "",
    adapter_version: str = "binance-depth-v1",
    symbol_override: str | None = None,
) -> MarketEvent:
    symbol = symbol_override or payload["s"]
    sequence = int(payload.get("lastUpdateId", payload.get("u")))
    snapshot = "lastUpdateId" in payload
    exchange_ms = int(payload.get("E", payload.get("T", receive_ts_ns // 1_000_000)))
    source_ms = int(payload.get("T", exchange_ms))
    quality_flags = []
    if receive_ts_ns < exchange_ms * 1_000_000:
        quality_flags.append("NEGATIVE_RECEIVE_LATENCY")
    return MarketEvent.from_dict({
        "event_id": stable_id(
            "canonical", raw_ref or "BINANCE", symbol, sequence, receive_ts_ns
        ),
        "event_type": EventType.BOOK_SNAPSHOT if snapshot else EventType.BOOK_DELTA,
        "venue": "BINANCE",
        "instrument_id": resolve("BINANCE", symbol),
        "exchange_ts_ns": exchange_ms * 1_000_000,
        "source_ts_ns": source_ms * 1_000_000,
        "receive_ts_ns": receive_ts_ns,
        "sequence": sequence,
        "sequence_start": int(payload.get("U", sequence)),
        "raw_ref": raw_ref,
        "schema_version": "1.2.0",
        "adapter_version": adapter_version,
        "checksum": payload.get("checksum", payload.get("cs", "")),
        "bids": payload.get("bids", payload.get("b", [])),
        "asks": payload.get("asks", payload.get("a", [])),
        "quality_flags": quality_flags,
        "native": payload,
    })


def normalize_bybit_depth(
    payload: dict[str, Any],
    receive_ts_ns: int,
    resolve: SymbolResolver,
    *,
    raw_ref: str = "",
    adapter_version: str = "bybit-depth-v1",
) -> MarketEvent:
    data = payload["data"]
    symbol = data.get("s") or payload["topic"].rsplit(".", 1)[-1]
    sequence = int(data["u"])
    event_type = payload.get("type", "delta")
    exchange_ms = int(payload.get("ts", payload.get("cts", receive_ts_ns // 1_000_000)))
    source_ms = int(payload.get("cts", exchange_ms))
    quality_flags = []
    if receive_ts_ns < exchange_ms * 1_000_000:
        quality_flags.append("NEGATIVE_RECEIVE_LATENCY")
    return MarketEvent.from_dict({
        "event_id": stable_id(
            "canonical", raw_ref or "BYBIT", symbol, sequence, receive_ts_ns
        ),
        "event_type": EventType.BOOK_SNAPSHOT if event_type == "snapshot" else EventType.BOOK_DELTA,
        "venue": "BYBIT",
        "instrument_id": resolve("BYBIT", symbol),
        "exchange_ts_ns": exchange_ms * 1_000_000,
        "source_ts_ns": source_ms * 1_000_000,
        "receive_ts_ns": receive_ts_ns,
        "sequence": sequence,
        "sequence_start": sequence,
        "raw_ref": raw_ref,
        "schema_version": "1.2.0",
        "adapter_version": adapter_version,
        "checksum": payload.get("checksum", payload.get("cs", data.get("checksum", data.get("cs", "")))),
        "bids": data.get("b", []),
        "asks": data.get("a", []),
        "quality_flags": quality_flags,
        "native": payload,
    })
