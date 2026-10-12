"""Live instrument status/rules channel (MR-03).

Polls the public Binance and Bybit instrument endpoints and appends a new
effective-dated ``InstrumentMaster`` version whenever the trading status or a
trading rule (tick, lot step, minimum quantity/notional) changes. History is
never rewritten, so a replay after the change uses the rules that applied at
each event's time. No credentials are involved.
"""

from __future__ import annotations

from decimal import Decimal
import json
import ssl
import time
from typing import Any, Callable
from urllib.parse import urlencode, urlparse
from urllib.request import urlopen

from .instruments import InstrumentMaster


BINANCE_SPOT_PUBLIC_URL = "https://api.binance.com"
BYBIT_PUBLIC_URL = "https://api.bybit.com"
RULE_FIELDS = ("tick_size", "quantity_step", "min_quantity", "min_notional")


def public_json_get(url: str, timeout: float = 15.0) -> dict[str, Any]:
    with urlopen(url, timeout=timeout, context=ssl.create_default_context()) as response:
        return json.loads(response.read().decode("utf-8"))


def _retrying_get(
    http_get: Callable[[str], dict[str, Any]], url: str, *,
    attempts: int = 3, backoff_seconds: float = 1.5,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Retry a transient failure (timeout, DNS blip) before giving up.

    A single flaky poll should not be counted as a real status-poller
    failure: exchange REST endpoints are stable, and the failures observed
    in practice are short-lived local network hiccups (connection timeouts,
    occasional DNS resolution errors), not the exchange being down.
    """
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return http_get(url)
        except Exception as exc:  # noqa: BLE001 - genuinely want to retry any transient failure
            last_error = exc
            if attempt + 1 < attempts:
                sleep(backoff_seconds * (attempt + 1))
    assert last_error is not None
    raise last_error


class InstrumentStatusPoller:
    def __init__(
        self, instruments: InstrumentMaster, symbols: dict[str, tuple[str, str]], *,
        http_get: Callable[[str], dict[str, Any]] = public_json_get,
        binance_base_url: str = BINANCE_SPOT_PUBLIC_URL,
        bybit_base_url: str = BYBIT_PUBLIC_URL, bybit_category: str = "spot",
        apply_rule_changes: bool = True,
    ) -> None:
        for url in (binance_base_url, bybit_base_url):
            if urlparse(url).scheme != "https":
                raise ValueError("instrument status endpoints must use https")
        self.instruments = instruments
        self.symbols = dict(symbols)
        self.http_get = http_get
        self.binance_base_url = binance_base_url.rstrip("/")
        self.bybit_base_url = bybit_base_url.rstrip("/")
        self.bybit_category = bybit_category
        self.apply_rule_changes = apply_rule_changes

    def fetch(self) -> dict[str, dict[str, Any]]:
        fetched = {}
        for venue, (instrument_id, symbol) in sorted(self.symbols.items()):
            if venue == "BINANCE":
                data = _retrying_get(
                    self.http_get,
                    f"{self.binance_base_url}/api/v3/exchangeInfo?"
                    + urlencode({"symbol": symbol}),
                )
                row = data["symbols"][0]
                filters = {item["filterType"]: item for item in row.get("filters", [])}
                notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
                fetched[venue] = {
                    "instrument_id": instrument_id,
                    "native_status": row["status"],
                    "status": "TRADING" if row["status"] == "TRADING" else row["status"],
                    "tick_size": filters.get("PRICE_FILTER", {}).get("tickSize"),
                    "quantity_step": filters.get("LOT_SIZE", {}).get("stepSize"),
                    "min_quantity": filters.get("LOT_SIZE", {}).get("minQty"),
                    "min_notional": notional.get("minNotional"),
                }
            elif venue == "BYBIT":
                data = _retrying_get(
                    self.http_get,
                    f"{self.bybit_base_url}/v5/market/instruments-info?"
                    + urlencode({"category": self.bybit_category, "symbol": symbol}),
                )
                if data.get("retCode") != 0 or not data.get("result", {}).get("list"):
                    raise ValueError(f"bybit instruments-info failed: {data.get('retMsg')}")
                row = data["result"]["list"][0]
                lot = row.get("lotSizeFilter", {})
                fetched[venue] = {
                    "instrument_id": instrument_id,
                    "native_status": row["status"],
                    "status": "TRADING" if row["status"] == "Trading" else row["status"].upper(),
                    "tick_size": row.get("priceFilter", {}).get("tickSize"),
                    "quantity_step": lot.get("basePrecision") or lot.get("qtyStep"),
                    "min_quantity": lot.get("minOrderQty"),
                    "min_notional": lot.get("minOrderAmt") or lot.get("minNotionalValue"),
                }
            else:
                raise ValueError(f"no public instrument endpoint for {venue}")
        return fetched

    def apply(self, fetched: dict[str, dict[str, Any]], now_ns: int) -> list[dict[str, Any]]:
        changes = []
        for venue, row in sorted(fetched.items()):
            rules = {
                field: Decimal(str(row[field]))
                for field in RULE_FIELDS
                if self.apply_rule_changes and row.get(field) not in (None, "")
            }
            updated = self.instruments.apply_status(
                venue, row["instrument_id"], effective_from_ns=now_ns,
                source=f"{venue}_PUBLIC_REST", status=row["status"], **rules,
            )
            if updated is not None:
                changes.append(self.instruments.status_events[-1])
        return changes

    def poll(self, now_ns: int) -> list[dict[str, Any]]:
        return self.apply(self.fetch(), now_ns)
