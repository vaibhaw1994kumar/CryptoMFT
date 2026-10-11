from __future__ import annotations

import csv
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import shutil
import time
from typing import Any, Callable, Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SCHEMA_VERSION = "1.0.0"

COLUMNS = [
    "schema_version",
    "dump_ts_utc",
    "exchange",
    "market_category",
    "native_symbol",
    "instrument_id",
    "base_asset",
    "quote_asset",
    "settle_asset",
    "margin_asset",
    "contract_type",
    "underlying",
    "option_type",
    "strike_price",
    "expiry_ts_ms",
    "launch_ts_ms",
    "status",
    "is_trading",
    "price_tick_size",
    "quantity_step",
    "min_quantity",
    "max_quantity",
    "min_notional",
    "contract_multiplier",
    "quantity_unit",
    "is_linear",
    "is_inverse",
    "price_precision",
    "quantity_precision",
    "maker_fee_rate",
    "taker_fee_rate",
    "maker_rebate_rate",
    "delivery_fee_rate",
    "fee_currency",
    "fee_tier",
    "fee_scope",
    "funding_applicable",
    "funding_interval_minutes",
    "current_funding_rate",
    "funding_rate_cap",
    "funding_rate_floor",
    "next_funding_ts_ms",
    "funding_asof_ts_ms",
    "funding_scope",
    "borrow_applicable",
    "borrow_rate",
    "borrow_rate_interval",
    "borrow_scope",
    "margin_model",
    "initial_margin_rate",
    "maintenance_margin_rate",
    "min_leverage",
    "max_leverage",
    "leverage_step",
    "margin_scope",
    "source_endpoint",
    "native_record_sha256",
]

BINANCE_ENDPOINTS = {
    "usd_m_futures": "https://fapi.binance.com/fapi/v1/exchangeInfo",
    "coin_m_futures": "https://dapi.binance.com/dapi/v1/exchangeInfo",
    "options": "https://eapi.binance.com/eapi/v1/exchangeInfo",
    "spot": "https://api.binance.com/api/v3/exchangeInfo",
}

BYBIT_ENDPOINT = "https://api.bybit.com/v5/market/instruments-info"
BYBIT_TICKERS_ENDPOINT = "https://api.bybit.com/v5/market/tickers"

BINANCE_FUNDING_INFO_ENDPOINTS = {
    "usd_m_futures": "https://fapi.binance.com/fapi/v1/fundingInfo",
    "coin_m_futures": "https://dapi.binance.com/dapi/v1/fundingInfo",
}

BINANCE_PREMIUM_INDEX_ENDPOINTS = {
    "usd_m_futures": "https://fapi.binance.com/fapi/v1/premiumIndex",
    "coin_m_futures": "https://dapi.binance.com/dapi/v1/premiumIndex",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stable_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(raw.encode("utf-8")).hexdigest()


def _filter(filters: Iterable[dict[str, Any]], name: str) -> dict[str, Any]:
    return next((item for item in filters if item.get("filterType") == name), {})


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _rebate_from_maker_rate(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return ""
    return str(abs(rate)) if rate < 0 else "0"


def _base_row(
    *, dump_ts: str, exchange: str, category: str, symbol: str,
    source_endpoint: str, native: dict[str, Any]
) -> dict[str, Any]:
    return {
        key: "" for key in COLUMNS
    } | {
        "schema_version": SCHEMA_VERSION,
        "dump_ts_utc": dump_ts,
        "exchange": exchange,
        "market_category": category,
        "native_symbol": symbol,
        "instrument_id": f"{exchange}:{category}:{symbol}",
        "source_endpoint": source_endpoint,
        "native_record_sha256": _stable_hash(native),
    }


def normalize_binance_futures(
    payload: dict[str, Any],
    category: str,
    endpoint: str,
    dump_ts: str,
    funding_info: dict[str, dict[str, Any]] | None = None,
    premium_index: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    inverse = category == "coin_m_futures"
    for item in payload.get("symbols", []):
        price = _filter(item.get("filters", []), "PRICE_FILTER")
        lot = _filter(item.get("filters", []), "LOT_SIZE")
        notional = (
            _filter(item.get("filters", []), "MIN_NOTIONAL")
            or _filter(item.get("filters", []), "NOTIONAL")
        )
        symbol = item["symbol"]
        status = item.get("status", "")
        funding_rule = (funding_info or {}).get(symbol, {})
        funding_state = (premium_index or {}).get(symbol, {})
        is_perpetual = item.get("contractType") == "PERPETUAL"
        row = _base_row(
            dump_ts=dump_ts, exchange="BINANCE", category=category,
            symbol=symbol, source_endpoint=endpoint, native=item,
        )
        row.update({
            "base_asset": item.get("baseAsset", ""),
            "quote_asset": item.get("quoteAsset", ""),
            "settle_asset": item.get("marginAsset", ""),
            "margin_asset": item.get("marginAsset", ""),
            "contract_type": item.get("contractType", ""),
            "underlying": item.get("pair", item.get("underlying", "")),
            "expiry_ts_ms": item.get("deliveryDate", ""),
            "launch_ts_ms": item.get("onboardDate", ""),
            "status": status,
            "is_trading": _bool(status == "TRADING"),
            "price_tick_size": price.get("tickSize", ""),
            "quantity_step": lot.get("stepSize", ""),
            "min_quantity": lot.get("minQty", ""),
            "max_quantity": lot.get("maxQty", ""),
            "min_notional": notional.get("notional", notional.get("minNotional", "")),
            "contract_multiplier": item.get("contractSize", "1" if not inverse else ""),
            "quantity_unit": "CONTRACTS" if inverse else "BASE_ASSET",
            "is_linear": _bool(not inverse),
            "is_inverse": _bool(inverse),
            "price_precision": item.get("pricePrecision", ""),
            "quantity_precision": item.get("quantityPrecision", ""),
            "fee_currency": item.get("marginAsset", ""),
            "fee_tier": "ACCOUNT",
            "fee_scope": "ACCOUNT_SPECIFIC_AUTH_REQUIRED",
            "funding_applicable": _bool(is_perpetual),
            "funding_interval_minutes": (
                int(funding_rule["fundingIntervalHours"]) * 60
                if funding_rule.get("fundingIntervalHours") not in (None, "")
                else (480 if is_perpetual else "")
            ),
            "current_funding_rate": funding_state.get("lastFundingRate", ""),
            "funding_rate_cap": funding_rule.get("adjustedFundingRateCap", ""),
            "funding_rate_floor": funding_rule.get("adjustedFundingRateFloor", ""),
            "next_funding_ts_ms": funding_state.get("nextFundingTime", ""),
            "funding_asof_ts_ms": funding_state.get("time", ""),
            "funding_scope": "PUBLIC_DYNAMIC" if is_perpetual else "NOT_APPLICABLE",
            "borrow_applicable": "false",
            "borrow_scope": "NOT_APPLICABLE_TO_DERIVATIVE",
            "margin_model": "RISK_TIERED",
            "margin_scope": "ACCOUNT_SPECIFIC_AUTH_REQUIRED",
        })
        rows.append(row)
    return rows


def normalize_binance_options(
    payload: dict[str, Any], endpoint: str, dump_ts: str
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in payload.get("optionSymbols", []):
        filters = item.get("filters", [])
        price = _filter(filters, "PRICE_FILTER")
        lot = _filter(filters, "LOT_SIZE")
        symbol = item["symbol"]
        status = item.get("status", "TRADING")
        row = _base_row(
            dump_ts=dump_ts, exchange="BINANCE", category="options",
            symbol=symbol, source_endpoint=endpoint, native=item,
        )
        row.update({
            "base_asset": item.get("underlying", item.get("underlyingAsset", "")),
            "quote_asset": item.get("quoteAsset", "USDT"),
            "settle_asset": item.get("settleAsset", item.get("quoteAsset", "USDT")),
            "margin_asset": item.get("settleAsset", item.get("quoteAsset", "USDT")),
            "contract_type": item.get("contractType", "EUROPEAN_OPTION"),
            "underlying": item.get("underlying", item.get("underlyingAsset", "")),
            "option_type": item.get("side", item.get("optionType", "")),
            "strike_price": item.get("strikePrice", ""),
            "expiry_ts_ms": item.get("expiryDate", ""),
            "launch_ts_ms": item.get("onboardDate", ""),
            "status": status,
            "is_trading": _bool(status in {"TRADING", "1"}),
            "price_tick_size": price.get("tickSize", item.get("priceScale", "")),
            "quantity_step": lot.get("stepSize", item.get("unit", "")),
            "min_quantity": lot.get("minQty", item.get("minQty", "")),
            "max_quantity": lot.get("maxQty", item.get("maxQty", "")),
            "contract_multiplier": item.get("unit", ""),
            "quantity_unit": "CONTRACTS",
            "is_linear": "true",
            "is_inverse": "false",
            "maker_fee_rate": item.get("makerFeeRate", ""),
            "taker_fee_rate": item.get("takerFeeRate", ""),
            "maker_rebate_rate": _rebate_from_maker_rate(item.get("makerFeeRate")),
            "fee_currency": item.get("settleAsset", item.get("quoteAsset", "USDT")),
            "fee_tier": "PUBLIC_CONTRACT",
            "fee_scope": "PUBLIC_CONTRACT",
            "funding_applicable": "false",
            "funding_scope": "NOT_APPLICABLE",
            "borrow_applicable": "false",
            "borrow_scope": "NOT_APPLICABLE_TO_DERIVATIVE",
            "margin_model": "OPTION_PREMIUM_MARGIN",
            "initial_margin_rate": item.get("initialMargin", item.get("minInitialMargin", "")),
            "maintenance_margin_rate": item.get(
                "maintenanceMargin", item.get("minMaintenanceMargin", "")
            ),
            "margin_scope": "PUBLIC_CONTRACT",
        })
        rows.append(row)
    return rows


def normalize_binance_spot(
    payload: dict[str, Any], endpoint: str, dump_ts: str
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in payload.get("symbols", []):
        price = _filter(item.get("filters", []), "PRICE_FILTER")
        lot = _filter(item.get("filters", []), "LOT_SIZE")
        notional = (
            _filter(item.get("filters", []), "NOTIONAL")
            or _filter(item.get("filters", []), "MIN_NOTIONAL")
        )
        symbol = item["symbol"]
        status = item.get("status", "")
        row = _base_row(
            dump_ts=dump_ts, exchange="BINANCE", category="spot",
            symbol=symbol, source_endpoint=endpoint, native=item,
        )
        row.update({
            "base_asset": item.get("baseAsset", ""),
            "quote_asset": item.get("quoteAsset", ""),
            "settle_asset": item.get("quoteAsset", ""),
            "contract_type": "SPOT",
            "status": status,
            "is_trading": _bool(status == "TRADING"),
            "price_tick_size": price.get("tickSize", ""),
            "quantity_step": lot.get("stepSize", ""),
            "min_quantity": lot.get("minQty", ""),
            "max_quantity": lot.get("maxQty", ""),
            "min_notional": notional.get("minNotional", ""),
            "contract_multiplier": "1",
            "quantity_unit": "BASE_ASSET",
            "is_linear": "true",
            "is_inverse": "false",
            "price_precision": item.get("quoteAssetPrecision", ""),
            "quantity_precision": item.get("baseAssetPrecision", ""),
            "fee_currency": item.get("quoteAsset", ""),
            "fee_tier": "ACCOUNT",
            "fee_scope": "ACCOUNT_SPECIFIC_AUTH_REQUIRED",
            "funding_applicable": "false",
            "funding_scope": "NOT_APPLICABLE",
            "borrow_applicable": _bool(item.get("isMarginTradingAllowed", False)),
            "borrow_scope": "ASSET_AND_ACCOUNT_SPECIFIC_AUTH_REQUIRED",
            "margin_model": "SPOT_OR_MARGIN",
            "margin_scope": "ACCOUNT_SPECIFIC_AUTH_REQUIRED",
        })
        rows.append(row)
    return rows


def normalize_bybit(
    items: Iterable[dict[str, Any]],
    category: str,
    endpoint: str,
    dump_ts: str,
    tickers: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        price = item.get("priceFilter", {})
        lot = item.get("lotSizeFilter", {})
        symbol = item["symbol"]
        status = item.get("status", "")
        inverse = category == "inverse"
        option = category == "option"
        spot = category == "spot"
        ticker = (tickers or {}).get(symbol, {})
        leverage = item.get("leverageFilter", {})
        is_perpetual = "Perpetual" in item.get("contractType", "")
        pre_listing = item.get("preListingInfo") or {}
        auction_fee = pre_listing.get("continuousTradingFeeRate") or {}
        maker_fee = item.get("makerFeeRate", auction_fee.get("makerFeeRate", ""))
        taker_fee = item.get("takerFeeRate", auction_fee.get("takerFeeRate", ""))
        row = _base_row(
            dump_ts=dump_ts, exchange="BYBIT", category=category,
            symbol=symbol, source_endpoint=endpoint, native=item,
        )
        strike = item.get("strike", item.get("strikePrice", ""))
        if option and not strike:
            parts = symbol.split("-")
            strike = parts[-2] if len(parts) >= 4 else ""
        row.update({
            "base_asset": item.get("baseCoin", ""),
            "quote_asset": item.get("quoteCoin", ""),
            "settle_asset": item.get("settleCoin", item.get("quoteCoin", "")),
            "margin_asset": item.get("settleCoin", ""),
            "contract_type": item.get("contractType", "OPTION" if option else ""),
            "underlying": item.get("baseCoin", ""),
            "option_type": item.get("optionsType", item.get("optionType", "")),
            "strike_price": strike,
            "expiry_ts_ms": item.get("deliveryTime", ""),
            "launch_ts_ms": item.get("launchTime", ""),
            "status": status,
            "is_trading": _bool(status == "Trading"),
            "price_tick_size": price.get("tickSize", ""),
            "quantity_step": lot.get("qtyStep", ""),
            "min_quantity": lot.get("minOrderQty", ""),
            "max_quantity": lot.get("maxOrderQty", ""),
            "min_notional": lot.get("minNotionalValue", ""),
            "contract_multiplier": item.get("contractSize", ""),
            "quantity_unit": "CONTRACTS" if inverse or option else "BASE_ASSET",
            "is_linear": _bool(category in {"linear", "spot", "option"}),
            "is_inverse": _bool(inverse),
            "price_precision": item.get("priceScale", ""),
            "quantity_precision": "",
            "maker_fee_rate": maker_fee,
            "taker_fee_rate": taker_fee,
            "maker_rebate_rate": _rebate_from_maker_rate(maker_fee),
            "delivery_fee_rate": item.get("deliveryFeeRate", ""),
            "fee_currency": item.get("settleCoin", item.get("quoteCoin", "")),
            "fee_tier": "PUBLIC_PRELISTING" if auction_fee else "ACCOUNT",
            "fee_scope": (
                "PUBLIC_PRELISTING" if auction_fee
                else "ACCOUNT_SPECIFIC_AUTH_REQUIRED"
            ),
            "funding_applicable": _bool(is_perpetual),
            "funding_interval_minutes": item.get("fundingInterval", ""),
            "current_funding_rate": ticker.get("fundingRate", ""),
            "funding_rate_cap": item.get("upperFundingRate", ""),
            "funding_rate_floor": item.get("lowerFundingRate", ""),
            "next_funding_ts_ms": ticker.get("nextFundingTime", ""),
            "funding_asof_ts_ms": ticker.get("timestamp", ""),
            "funding_scope": "PUBLIC_DYNAMIC" if is_perpetual else "NOT_APPLICABLE",
            "borrow_applicable": _bool(spot and item.get("marginTrading") not in {None, "none", "None"}),
            "borrow_scope": (
                "ASSET_AND_ACCOUNT_SPECIFIC_AUTH_REQUIRED" if spot
                else "NOT_APPLICABLE_TO_DERIVATIVE"
            ),
            "margin_model": "SPOT_OR_MARGIN" if spot else (
                "OPTION_PREMIUM_MARGIN" if option else "RISK_TIERED"
            ),
            "min_leverage": leverage.get("minLeverage", ""),
            "max_leverage": leverage.get("maxLeverage", ""),
            "leverage_step": leverage.get("leverageStep", ""),
            "margin_scope": (
                "ACCOUNT_SPECIFIC_AUTH_REQUIRED" if spot or option
                else "PUBLIC_MAX_LEVERAGE_RISK_TIERS_SEPARATE"
            ),
        })
        rows.append(row)
    return rows


class PublicHttpClient:
    def __init__(self, timeout_seconds: float = 30, retries: int = 3):
        self.timeout_seconds = timeout_seconds
        self.retries = retries

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        target = f"{url}?{urlencode(params)}" if params else url
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                request = Request(target, headers={"User-Agent": "crypto-stat-arb-contract-master/1.0"})
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    return json.loads(response.read().decode("utf-8"))
            except Exception as exc:  # network errors vary by operating system
                last_error = exc
                if attempt + 1 < self.retries:
                    time.sleep(2 ** attempt)
        raise RuntimeError(f"GET failed after {self.retries} attempts: {target}: {last_error}")


def _fetch_bybit_category(
    client: PublicHttpClient, category: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    items: list[dict[str, Any]] = []
    raw_pages: list[dict[str, Any]] = []
    cursor = ""
    seen_cursors: set[str] = set()
    while True:
        params: dict[str, Any] = {"category": category}
        if category != "spot":
            params["limit"] = 1000
            if cursor:
                params["cursor"] = cursor
        payload = client.get_json(BYBIT_ENDPOINT, params)
        if payload.get("retCode") != 0:
            raise RuntimeError(f"Bybit {category}: {payload.get('retCode')} {payload.get('retMsg')}")
        raw_pages.append(payload)
        result = payload.get("result", {})
        items.extend(result.get("list", []))
        cursor = result.get("nextPageCursor", "")
        if category == "spot" or not cursor:
            break
        if cursor in seen_cursors:
            raise RuntimeError(f"Bybit {category}: repeated pagination cursor")
        seen_cursors.add(cursor)
    return items, raw_pages


def _by_symbol(payload: Any) -> dict[str, dict[str, Any]]:
    records = payload if isinstance(payload, list) else [payload]
    return {
        str(item["symbol"]): item
        for item in records
        if isinstance(item, dict) and item.get("symbol")
    }


def _fetch_bybit_tickers(
    client: PublicHttpClient, category: str
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    payload = client.get_json(BYBIT_TICKERS_ENDPOINT, {"category": category})
    if payload.get("retCode") != 0:
        raise RuntimeError(
            f"Bybit tickers {category}: {payload.get('retCode')} {payload.get('retMsg')}"
        )
    result = payload.get("result", {})
    return _by_symbol(result.get("list", [])), payload


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def dump_contract_master(
    output_dir: Path,
    *,
    include_spot: bool = False,
    allow_partial: bool = False,
    client: PublicHttpClient | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> dict[str, Any]:
    client = client or PublicHttpClient()
    timestamp = now()
    dump_ts = timestamp.isoformat()
    dump_id = timestamp.strftime("%Y%m%dT%H%M%SZ")
    raw_dir = output_dir / "raw" / dump_id
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    counts: dict[str, int] = {}

    binance_categories = ["usd_m_futures", "coin_m_futures", "options"]
    if include_spot:
        binance_categories.append("spot")
    for category in binance_categories:
        endpoint = BINANCE_ENDPOINTS[category]
        try:
            payload = client.get_json(endpoint)
            _write_json(raw_dir / f"binance_{category}.json", payload)
            if category in {"usd_m_futures", "coin_m_futures"}:
                funding_endpoint = BINANCE_FUNDING_INFO_ENDPOINTS[category]
                premium_endpoint = BINANCE_PREMIUM_INDEX_ENDPOINTS[category]
                funding_payload = client.get_json(funding_endpoint)
                premium_payload = client.get_json(premium_endpoint)
                _write_json(raw_dir / f"binance_{category}_funding_info.json", funding_payload)
                _write_json(raw_dir / f"binance_{category}_premium_index.json", premium_payload)
                normalized = normalize_binance_futures(
                    payload,
                    category,
                    endpoint,
                    dump_ts,
                    funding_info=_by_symbol(funding_payload),
                    premium_index=_by_symbol(premium_payload),
                )
            elif category == "options":
                normalized = normalize_binance_options(payload, endpoint, dump_ts)
            else:
                normalized = normalize_binance_spot(payload, endpoint, dump_ts)
            rows.extend(normalized)
            counts[f"BINANCE:{category}"] = len(normalized)
        except Exception as exc:
            errors.append({"source": f"BINANCE:{category}", "error": str(exc)})

    bybit_categories = ["linear", "inverse", "option"]
    if include_spot:
        bybit_categories.append("spot")
    for category in bybit_categories:
        try:
            items, raw_pages = _fetch_bybit_category(client, category)
            _write_json(raw_dir / f"bybit_{category}.json", raw_pages)
            tickers: dict[str, dict[str, Any]] = {}
            if category in {"linear", "inverse"}:
                tickers, ticker_payload = _fetch_bybit_tickers(client, category)
                _write_json(raw_dir / f"bybit_{category}_tickers.json", ticker_payload)
            normalized = normalize_bybit(
                items, category, BYBIT_ENDPOINT, dump_ts, tickers=tickers
            )
            rows.extend(normalized)
            counts[f"BYBIT:{category}"] = len(normalized)
        except Exception as exc:
            errors.append({"source": f"BYBIT:{category}", "error": str(exc)})

    if errors and not allow_partial:
        _write_json(output_dir / f"contract_master_{dump_id}.failed.json", {
            "dump_ts_utc": dump_ts, "errors": errors, "counts": counts,
        })
        raise RuntimeError(f"contract master incomplete; no CSV published: {errors}")
    if not rows:
        raise RuntimeError("no instrument records returned; no CSV published")

    rows.sort(key=lambda row: (
        row["exchange"], row["market_category"], row["native_symbol"]
    ))
    csv_path = output_dir / f"contract_master_binance_bybit_{dump_id}.csv"
    _write_csv(csv_path, rows)
    latest_path = output_dir / "contract_master_binance_bybit_latest.csv"
    shutil.copy2(csv_path, latest_path)
    csv_sha256 = sha256(csv_path.read_bytes()).hexdigest()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "dump_ts_utc": dump_ts,
        "include_spot": include_spot,
        "allow_partial": allow_partial,
        "row_count": len(rows),
        "counts": counts,
        "errors": errors,
        "csv": str(csv_path),
        "latest_csv": str(latest_path),
        "csv_sha256": csv_sha256,
        "raw_directory": str(raw_dir),
    }
    _write_json(output_dir / f"contract_master_{dump_id}.manifest.json", manifest)
    return manifest
