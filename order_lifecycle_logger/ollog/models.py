from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal


def stamp() -> tuple[int, int]:
    """(wall clock ns, monotonic ns), taken back to back."""
    return time.time_ns(), time.monotonic_ns()


@dataclass(frozen=True)
class Instrument:
    market: str
    symbol: str
    base: str
    quote: str
    tick: Decimal
    step: Decimal
    min_qty: Decimal
    min_notional: Decimal
    pct_down: Decimal | None = None     # lower price band multiplier (Binance PERCENT_PRICE[_BY_SIDE]); None if the venue has none


@dataclass
class RestResult:
    ok: bool
    order_id: str | None = None
    ex_time_ms: int | None = None
    code: str = ""
    msg: str = ""
    http_status: int = 0
    t_send_wall_ns: int = 0
    t_send_mono_ns: int = 0
    t_ret_wall_ns: int = 0
    t_ret_mono_ns: int = 0


@dataclass
class UserEvent:
    """A private-stream order update, normalised across venues. kind: new | cancelled | rejected | expired | trade | other."""
    market: str
    kind: str
    symbol: str
    order_id: str
    client_id: str
    ex_event_ms: int | None
    ex_txn_ms: int | None
    status: str
    reason: str = ""
    recv_wall_ns: int = 0
    recv_mono_ns: int = 0
    raw: dict = field(default_factory=dict, repr=False)


def round_down(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def round_up(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def fmt(value: Decimal) -> str:
    """Plain decimal string, no exponent, no trailing zeros."""
    s = format(value.normalize(), "f")
    return s if "." not in s else s.rstrip("0").rstrip(".")
