"""Derivatives market data: mark price, index price and current funding (DAT02).

One canonical event, ``DerivativeTicker``, carries what a perpetual's
economics need from the venue, alongside books and trades:

* **Binance USDⓈ-M** mark-price stream ``<symbol>@markPrice@1s``
  (``e="markPriceUpdate"``: ``p`` mark, ``i`` index, ``P`` estimated settle
  price, ``r`` current funding rate, ``T`` next funding time, ``E`` event time).
* **Bybit v5 linear** ``tickers.<symbol>``: a snapshot followed by deltas in
  which **an omitted field is unchanged, not zero**. ``BybitTickerMerger``
  keeps the merged state per symbol.

The funding rate here is the venue's *current/predicted* rate for the next
settlement: it feeds ``FundingEngine.predict`` only, never realised
settlement. The mark price at a settlement can serve as that settlement's
mark (``mark_fallback="exchange_mark"``), labelled as the venue mark.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal
from typing import Any

from .models import stable_id

DERIVATIVE_SCHEMA_VERSION = "1.0.0"


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class DerivativeTicker:
    event_id: str
    venue: str
    instrument_id: str
    symbol: str
    exchange_ts_ns: int
    source_ts_ns: int
    receive_ts_ns: int
    mark_price: Decimal | None
    index_price: Decimal | None
    funding_rate: Decimal | None            # current / predicted rate for the next settlement
    next_funding_ts_ns: int | None
    funding_interval_hours: Decimal | None = None
    sequence: int = 0
    arrival_ordinal: int = 0
    raw_ref: str = ""
    schema_version: str = DERIVATIVE_SCHEMA_VERSION
    adapter_version: str = ""
    event_type: str = "DERIVATIVE_TICKER"
    quality_flags: tuple[str, ...] = ()
    native: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        for key in ("mark_price", "index_price", "funding_rate", "funding_interval_hours"):
            row[key] = None if row[key] is None else str(row[key])
        row["quality_flags"] = list(self.quality_flags)
        return row

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "DerivativeTicker":
        return cls(
            event_id=str(row["event_id"]), venue=str(row["venue"]), instrument_id=str(row["instrument_id"]),
            symbol=str(row.get("symbol", "")), exchange_ts_ns=int(row["exchange_ts_ns"]),
            source_ts_ns=int(row.get("source_ts_ns", row["exchange_ts_ns"])),
            receive_ts_ns=int(row["receive_ts_ns"]),
            mark_price=_decimal(row.get("mark_price")), index_price=_decimal(row.get("index_price")),
            funding_rate=_decimal(row.get("funding_rate")),
            next_funding_ts_ns=None if row.get("next_funding_ts_ns") in (None, "") else int(row["next_funding_ts_ns"]),
            funding_interval_hours=_decimal(row.get("funding_interval_hours")),
            sequence=int(row.get("sequence", 0)), arrival_ordinal=int(row.get("arrival_ordinal", 0)),
            raw_ref=str(row.get("raw_ref", "")), schema_version=str(row.get("schema_version", DERIVATIVE_SCHEMA_VERSION)),
            adapter_version=str(row.get("adapter_version", "")),
            quality_flags=tuple(row.get("quality_flags", ())), native=dict(row.get("native") or {}),
        )


def validate_derivative_ticker(ticker: DerivativeTicker, *, require_raw_ref: bool = True) -> None:
    missing = [name for name in ("event_id", "venue", "instrument_id", "schema_version", "adapter_version")
               if not getattr(ticker, name)]
    if require_raw_ref and not ticker.raw_ref:
        missing.append("raw_ref")
    if missing:
        raise ValueError(f"derivative ticker missing required fields: {', '.join(missing)}")
    if min(ticker.exchange_ts_ns, ticker.receive_ts_ns) < 0:
        raise ValueError("derivative ticker timestamps must be non-negative")
    for name in ("mark_price", "index_price"):
        value = getattr(ticker, name)
        if value is not None and not (value.is_finite() and value > 0):
            raise ValueError(f"derivative ticker {name} must be positive")
    if ticker.funding_rate is not None and not ticker.funding_rate.is_finite():
        raise ValueError("derivative ticker funding_rate must be finite")
    if ticker.mark_price is None and ticker.funding_rate is None:
        raise ValueError("derivative ticker carries neither a mark price nor a funding rate")


def normalize_binance_mark_price(
    payload: dict[str, Any], receive_ts_ns: int, instrument_id: str, raw_ref: str,
) -> DerivativeTicker:
    if payload.get("e") != "markPriceUpdate":
        raise ValueError("not a Binance markPriceUpdate frame")
    symbol = str(payload["s"])
    exchange_ts_ns = int(payload["E"]) * 1_000_000
    next_funding = int(payload.get("T") or 0)
    return DerivativeTicker(
        event_id=stable_id("derivative", "BINANCE", symbol, exchange_ts_ns, raw_ref),
        venue="BINANCE", instrument_id=instrument_id, symbol=symbol,
        exchange_ts_ns=exchange_ts_ns, source_ts_ns=exchange_ts_ns, receive_ts_ns=receive_ts_ns,
        mark_price=_decimal(payload.get("p")), index_price=_decimal(payload.get("i")),
        # An empty rate / zero next time means "not a perpetual" (delivery contract).
        funding_rate=_decimal(payload.get("r")),
        next_funding_ts_ns=next_funding * 1_000_000 if next_funding else None,
        raw_ref=raw_ref, adapter_version="binance-mark-price-v1", native=payload,
    )


@dataclass(slots=True)
class BybitTickerMerger:
    """Bybit ``tickers`` snapshot + deltas: an omitted field keeps its last value."""

    state: dict[str, dict[str, Any]] = field(default_factory=dict)
    MERGED = ("markPrice", "indexPrice", "fundingRate", "nextFundingTime", "fundingIntervalHour")

    def normalize(
        self, payload: dict[str, Any], receive_ts_ns: int, instrument_id: str, raw_ref: str,
    ) -> DerivativeTicker:
        topic = str(payload.get("topic", ""))
        if not topic.startswith("tickers."):
            raise ValueError("not a Bybit tickers frame")
        kind = payload.get("type")
        data = payload.get("data") or {}
        symbol = str(data.get("symbol") or topic.split(".", 1)[1])
        if kind == "snapshot":
            merged = {key: data.get(key) for key in self.MERGED}
        elif kind == "delta":
            if symbol not in self.state:
                raise ValueError(f"Bybit ticker delta before a snapshot for {symbol}")
            merged = dict(self.state[symbol])
            merged.update({key: data[key] for key in self.MERGED if key in data})
        else:
            raise ValueError(f"unknown Bybit ticker message type {kind!r}")
        self.state[symbol] = merged
        exchange_ts_ns = int(payload["ts"]) * 1_000_000
        next_funding = int(merged.get("nextFundingTime") or 0)
        return DerivativeTicker(
            event_id=stable_id("derivative", "BYBIT", symbol, exchange_ts_ns, raw_ref),
            venue="BYBIT", instrument_id=instrument_id, symbol=symbol,
            exchange_ts_ns=exchange_ts_ns, source_ts_ns=exchange_ts_ns, receive_ts_ns=receive_ts_ns,
            mark_price=_decimal(merged.get("markPrice")), index_price=_decimal(merged.get("indexPrice")),
            funding_rate=_decimal(merged.get("fundingRate")),
            next_funding_ts_ns=next_funding * 1_000_000 if next_funding else None,
            funding_interval_hours=_decimal(merged.get("fundingIntervalHour")),
            sequence=int(payload.get("cs") or 0), raw_ref=raw_ref,
            adapter_version="bybit-linear-ticker-v1",
            quality_flags=("MERGED_DELTA",) if kind == "delta" else (),
            native=payload,
        )

    def reset(self, symbol: str | None = None) -> None:
        """A reconnect invalidates merged state: wait for a new snapshot."""
        if symbol is None:
            self.state.clear()
        else:
            self.state.pop(symbol, None)


def with_arrival(ticker: DerivativeTicker, ordinal: int) -> DerivativeTicker:
    return replace(ticker, arrival_ordinal=ordinal)
