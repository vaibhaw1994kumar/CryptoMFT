"""Order price and size selection from the testnet's own filters."""
from __future__ import annotations

import re
from decimal import Decimal

from .models import Instrument, round_down, round_up


class NoOrder(RuntimeError):
    """The symbol cannot be quoted safely right now (empty book, band cannot be honoured, size above the cap)."""


# Bybit spot 170193. The testnet books can sit above the exchange's own reference, so a buy below the bid is still refused.
_BUY_CAP = re.compile(r"buy order price cannot be higher than\s*([0-9]*\.?[0-9]+)", re.IGNORECASE)


def parse_buy_price_cap(code: str, msg: str) -> Decimal | None:
    """The buy-price limit a 170193 rejection reports, or None for any other reply."""
    if str(code) != "170193":
        return None
    m = _BUY_CAP.search(msg or "")
    return Decimal(m.group(1)) if m and Decimal(m.group(1)) > 0 else None


_POST_ONLY_WORDS = ("postonly", "post only", "post-only", "take liquidity", "immediately match", "could not be executed as maker")


def is_post_only_reject(market: str, code: str, msg: str, reason: str = "", kind: str = "") -> bool:
    """True only when the venue refused the order BECAUSE it would have taken liquidity. A price-limit, minimum-size or
    permission rejection of a probe is not that, and must not be reported as a post-only reject."""
    text = f"{msg} {reason}".lower()
    if any(w in text for w in _POST_ONLY_WORDS) or str(code) == "-5022":
        return True
    return market == "binance_futures" and kind == "expired"       # a GTX order that would take is EXPIRED on the stream


def passive_buy_price(inst: Instrument, bid: Decimal, dist_bps: Decimal, ref: Decimal | None = None,
                      cap: Decimal | None = None) -> Decimal:
    """A buy below the best bid, kept inside the venue's lower price band when one applies and never above `cap`."""
    if bid <= 0:
        raise NoOrder("no bid")
    price = bid * (1 - dist_bps / Decimal(10000))
    if cap is not None:
        price = min(price, cap)
    if inst.pct_down is not None and ref and ref > 0:
        floor = ref * (inst.pct_down + Decimal("0.02"))     # 2 points of margin inside the band
        price = max(price, floor)
    price = round_down(price, inst.tick)
    if price <= 0 or price >= bid:
        raise NoOrder(f"cannot place below the bid inside the band (bid {bid}, price {price})")
    return price


def crossing_buy_price(inst: Instrument, ask: Decimal) -> Decimal:
    """A post-only buy at the best ask: it would take liquidity, so a post-only order is rejected and never fills."""
    if ask <= 0:
        raise NoOrder("no ask")
    return round_up(ask, inst.tick)


def min_quantity(inst: Instrument, price: Decimal, max_notional: Decimal, safety: Decimal = Decimal("1.05")) -> Decimal:
    qty = max(inst.min_qty, round_up(inst.min_notional * safety / price, inst.step))
    qty = round_up(qty, inst.step)
    if qty * price > max_notional:
        raise NoOrder(f"minimum size is {qty * price:.2f} quote, above the cap {max_notional}")
    return qty
