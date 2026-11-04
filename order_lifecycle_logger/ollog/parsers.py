"""Pure parsing of venue payloads: instrument lists and private-stream order updates. No I/O."""
from __future__ import annotations

from decimal import Decimal

from .models import Instrument, UserEvent


def _d(x, default="0") -> Decimal:
    return Decimal(str(x)) if x not in (None, "") else Decimal(default)


def _i(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


# ----- instruments ------------------------------------------------------------------------------------------------

def parse_bybit_instruments(market: str, payload: dict) -> dict[str, Instrument]:
    out: dict[str, Instrument] = {}
    for r in payload.get("result", {}).get("list", []):
        if r.get("status") != "Trading":
            continue
        if market == "bybit_linear" and (r.get("contractType") != "LinearPerpetual" or r.get("quoteCoin") != "USDT"):
            continue
        lot, price = r.get("lotSizeFilter", {}), r.get("priceFilter", {})
        step = _d(lot.get("basePrecision") if market == "bybit_spot" else lot.get("qtyStep"))
        notional = _d(lot.get("minOrderAmt") if market == "bybit_spot" else lot.get("minNotionalValue"))
        if step <= 0 or _d(price.get("tickSize")) <= 0:
            continue
        out[r["symbol"]] = Instrument(market, r["symbol"], r.get("baseCoin", ""), r.get("quoteCoin", ""),
                                      _d(price["tickSize"]), step, _d(lot.get("minOrderQty")), notional)
    return out


def parse_binance_instruments(market: str, payload: dict) -> dict[str, Instrument]:
    out: dict[str, Instrument] = {}
    for r in payload.get("symbols", []):
        if r.get("status") != "TRADING" or r.get("quoteAsset") != "USDT":
            continue
        if market == "binance_spot" and "LIMIT_MAKER" not in r.get("orderTypes", []):
            continue
        if market == "binance_futures" and r.get("contractType") != "PERPETUAL":
            continue
        f = {x["filterType"]: x for x in r.get("filters", [])}
        notional = f.get("NOTIONAL", f.get("MIN_NOTIONAL", {}))
        min_notional = _d(notional.get("minNotional", notional.get("notional")))
        pct = f.get("PERCENT_PRICE_BY_SIDE", {}).get("bidMultiplierDown") or f.get("PERCENT_PRICE", {}).get("multiplierDown")
        tick, step = _d(f.get("PRICE_FILTER", {}).get("tickSize")), _d(f.get("LOT_SIZE", {}).get("stepSize"))
        if tick <= 0 or step <= 0:
            continue
        out[r["symbol"]] = Instrument(market, r["symbol"], r.get("baseAsset", ""), r.get("quoteAsset", ""), tick, step,
                                      _d(f.get("LOT_SIZE", {}).get("minQty")), min_notional, _d(pct) if pct else None)
    return out


# ----- private stream ---------------------------------------------------------------------------------------------

_BYBIT_KIND = {"New": "new", "Cancelled": "cancelled", "PartiallyFilledCanceled": "cancelled", "Rejected": "rejected",
               "Filled": "trade", "PartiallyFilled": "trade", "Deactivated": "cancelled"}


def parse_bybit_ws(market: str, msg: dict) -> list[UserEvent]:
    """Bybit v5 private `order` topic. Top-level creationTime is the push time; data[].updatedTime the order's last change."""
    if not str(msg.get("topic", "")).startswith("order"):
        return []
    out = []
    for d in msg.get("data", []):
        cat = d.get("category")
        if cat and cat != ("spot" if market == "bybit_spot" else "linear"):
            continue
        status = d.get("orderStatus", "")
        out.append(UserEvent(market, _BYBIT_KIND.get(status, "other"), d.get("symbol", ""), str(d.get("orderId", "")),
                             str(d.get("orderLinkId", "")), _i(d.get("updatedTime")), _i(msg.get("creationTime")), status,
                             str(d.get("rejectReason") or ""), raw=d))
    return out


def _unwrap(msg: dict) -> dict:
    for key in ("data", "event"):
        if isinstance(msg.get(key), dict) and "e" in msg[key]:
            return msg[key]
    return msg


_BINANCE_EXEC = {"NEW": "new", "CANCELED": "cancelled", "REJECTED": "rejected", "EXPIRED": "expired", "TRADE": "trade"}


def parse_binance_spot_ws(market: str, msg: dict) -> list[UserEvent]:
    m = _unwrap(msg)
    if m.get("e") != "executionReport":
        return []
    cid = m.get("c", "")
    if m.get("x") == "CANCELED" and m.get("C") not in (None, "", "null"):
        cid = m["C"]            # on a cancel `c` is a fresh id for the cancel request; `C` is the original client order id
    return [UserEvent(market, _BINANCE_EXEC.get(m.get("x", ""), "other"), m.get("s", ""), str(m.get("i", "")),
                      str(cid), _i(m.get("E")), _i(m.get("T")), m.get("X", ""),
                      "" if m.get("r") in (None, "NONE") else str(m.get("r")), raw=m)]


def parse_binance_futures_ws(market: str, msg: dict) -> list[UserEvent]:
    m = _unwrap(msg)
    if m.get("e") != "ORDER_TRADE_UPDATE":
        return []
    o = m.get("o", {})
    return [UserEvent(market, _BINANCE_EXEC.get(o.get("x", ""), "other"), o.get("s", ""), str(o.get("i", "")),
                      str(o.get("c", "")), _i(m.get("E")), _i(m.get("T")), o.get("X", ""), "", raw=m)]
