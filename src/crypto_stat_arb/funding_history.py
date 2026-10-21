"""Realised funding history from the venues' public endpoints (ECO02 input).

* **Binance USDⓈ-M** ``GET /fapi/v1/fundingRate``: ascending, up to 1000 rows
  per call, with ``fundingRate``, ``fundingTime`` and ``markPrice``. Paged
  forward by moving ``startTime`` past the last ``fundingTime``.
* **Bybit v5** ``GET /v5/market/funding/history?category=linear``: up to 200
  rows per call with ``fundingRate`` and ``fundingRateTimestamp`` and **no
  mark price**. The order is not documented, so rows are sorted here; pages
  move ``endTime`` before the oldest row. The mark at each settlement comes
  from ``GET /v5/market/mark-price-kline`` (1-minute candle opening at the
  settlement; labelled in ``mark_source``). Without it a settlement would be
  ``FUNDING_MARK_MISSING``.

Every record is a final settled rate: ``settlement_history: true``,
``source: OBSERVED``. Records still pass the funding engine's validation
(configured instrument, rate bounds). Only public, read-only market-data hosts
are accepted, and the HTTP call is injectable: nothing here runs against a
live endpoint unless the caller passes a real HTTP function.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode, urlparse

HttpGet = Callable[[str], tuple[int, str]]

BINANCE_FAPI = "https://fapi.binance.com"
BYBIT_API = "https://api.bybit.com"
PUBLIC_HOSTS = frozenset({"fapi.binance.com", "api.bybit.com"})


def _get_json(http: HttpGet, base: str, path: str, params: dict[str, Any]) -> Any:
    url = f"{base}{path}?{urlencode(params)}"
    if urlparse(url).hostname not in PUBLIC_HOSTS:
        raise ValueError(f"refusing non-public funding-history host {url}")
    status, text = http(url)
    if status != 200:
        raise ConnectionError(f"{path} HTTP {status}: {text[:200]}")
    return json.loads(text)


def to_ms(value: str | int) -> int:
    if isinstance(value, int) or str(value).isdigit():
        return int(value)
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def _record(venue: str, instrument_id: str, ts_ms: int, rate: str, mark: str | None, origin: str,
            mark_source: str) -> dict[str, Any]:
    return {
        "venue": venue, "instrument_id": instrument_id, "funding_ts_ms": ts_ms,
        "rate": str(Decimal(rate)), "mark_price": None if mark in (None, "") else str(Decimal(mark)),
        "source": "OBSERVED", "settlement_history": True, "origin": origin,
        "mark_source": mark_source if mark not in (None, "") else "",
    }


def fetch_binance(symbol: str, instrument_id: str, start_ms: int, end_ms: int, *, http: HttpGet,
                  limit: int = 1000, max_pages: int = 10_000) -> list[dict[str, Any]]:
    records: dict[int, dict[str, Any]] = {}
    cursor = start_ms
    for _ in range(max_pages):
        rows = _get_json(http, BINANCE_FAPI, "/fapi/v1/fundingRate",
                         {"symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": limit})
        if not rows:
            break
        for row in rows:
            ts = int(row["fundingTime"])
            if start_ms <= ts <= end_ms:
                records[ts] = _record("BINANCE", instrument_id, ts, row["fundingRate"], row.get("markPrice"),
                                      "binance /fapi/v1/fundingRate", "binance funding history markPrice")
        last = max(int(row["fundingTime"]) for row in rows)
        if len(rows) < limit or last >= end_ms:
            break
        cursor = last + 1
    return [records[ts] for ts in sorted(records)]


def fetch_bybit(symbol: str, instrument_id: str, start_ms: int, end_ms: int, *, http: HttpGet,
                limit: int = 200, with_marks: bool = True, max_pages: int = 10_000) -> list[dict[str, Any]]:
    rates: dict[int, str] = {}
    cursor_end = end_ms
    for _ in range(max_pages):
        payload = _get_json(http, BYBIT_API, "/v5/market/funding/history", {
            "category": "linear", "symbol": symbol, "startTime": start_ms, "endTime": cursor_end, "limit": limit,
        })
        if payload.get("retCode") != 0:
            raise ConnectionError(f"bybit funding history: {payload.get('retMsg')}")
        rows = payload.get("result", {}).get("list", [])
        if not rows:
            break
        stamps = [int(row["fundingRateTimestamp"]) for row in rows]
        for row, ts in zip(rows, stamps):
            if start_ms <= ts <= end_ms:
                rates[ts] = str(row["fundingRate"])
        oldest = min(stamps)
        if len(rows) < limit or oldest <= start_ms:
            break
        cursor_end = oldest - 1
    records = []
    for ts in sorted(rates):
        mark = _bybit_mark_at(symbol, ts, http) if with_marks else None
        records.append(_record("BYBIT", instrument_id, ts, rates[ts], mark, "bybit /v5/market/funding/history",
                               "bybit mark-price kline open (1m) at settlement"))
    return records


def _bybit_mark_at(symbol: str, ts_ms: int, http: HttpGet) -> str | None:
    payload = _get_json(http, BYBIT_API, "/v5/market/mark-price-kline", {
        "category": "linear", "symbol": symbol, "interval": "1", "start": ts_ms, "end": ts_ms + 59_999, "limit": 1,
    })
    if payload.get("retCode") != 0:
        return None
    for candle in payload.get("result", {}).get("list", []):
        if int(candle[0]) == ts_ms:                 # the candle that opens at the settlement
            return str(candle[1])
    return None


def write_jsonl(records: list[dict[str, Any]], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    return path


def urllib_get(url: str, timeout: float = 15.0) -> tuple[int, str]:
    """Plain public HTTPS GET (default trust store)."""
    from .testnet import urllib_http

    return urllib_http("GET", url, {}, None, timeout)
