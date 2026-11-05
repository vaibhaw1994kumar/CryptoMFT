"""Atomic risk reservation (docs/strategy_contract.md 4h.2).

The execution layer keeps its own book of exposure that primary intents have been promised but the inventory does not hold yet.
`try_reserve` checks the worst case with the new legs added and, only if it fits, records them: one call, no engine event between
the check and the reservation, no state change on a refusal. The check mirrors ArbStrategy._inventory_room (per-venue cap and net
ceiling) but reads the layer's reservations, not the engine's PendingExposure, so it is a second, independent gate.
"""
from __future__ import annotations

from dataclasses import dataclass, field

EPS = 1e-9


@dataclass(frozen=True)
class RiskLimits:
    venue_cap: float            # |base on one venue| may not exceed this
    net_ceiling: float          # |net base across venues| may not exceed this


@dataclass
class Reservation:
    venue: str
    side: str                   # "buy" | "sell"
    remaining: float


@dataclass
class ReservationBook:
    held: dict[str, Reservation] = field(default_factory=dict)         # intent id -> reservation
    released: int = 0                                                  # reservations returned to zero (terminal states)

    def totals(self) -> tuple[dict[str, float], dict[str, float]]:
        buys: dict[str, float] = {}
        sells: dict[str, float] = {}
        for r in self.held.values():
            book = buys if r.side == "buy" else sells
            book[r.venue] = book.get(r.venue, 0.0) + r.remaining
        return buys, sells

    def headroom_breach(self, limits: RiskLimits, legs: list[tuple[str, str, float]], base: dict[str, float]) -> str:
        """'' if the legs fit, else the name of the first limit they break. Pure: reads, never writes."""
        buys, sells = self.totals()
        add_b: dict[str, float] = {}
        add_s: dict[str, float] = {}
        for venue, side, qty in legs:
            (add_b if side == "buy" else add_s)[venue] = (add_b if side == "buy" else add_s).get(venue, 0.0) + qty
        touched = {v for v, _, _ in legs}
        for v in touched:
            hi = base.get(v, 0.0) + buys.get(v, 0.0) + add_b.get(v, 0.0)
            lo = base.get(v, 0.0) - sells.get(v, 0.0) - add_s.get(v, 0.0)
            if add_b.get(v, 0.0) > 0 and hi > limits.venue_cap + EPS:
                return f"venue_cap:{v}"
            if add_s.get(v, 0.0) > 0 and lo < -limits.venue_cap - EPS:
                return f"venue_cap:{v}"
        net = sum(base.values())
        net_hi = net + sum(buys.values()) + sum(add_b.values())
        net_lo = net - sum(sells.values()) - sum(add_s.values())
        if max(abs(net_hi), abs(net_lo)) > limits.net_ceiling + EPS and (add_b or add_s):
            # only a breach the new legs make or worsen counts: an already-breached book may still take a risk-reducing clip
            before = max(abs(net + sum(buys.values())), abs(net - sum(sells.values())))
            if max(abs(net_hi), abs(net_lo)) > before + EPS:
                return "net_ceiling"
        return ""

    def reserve(self, intent_id: str, venue: str, side: str, qty: float) -> None:
        self.held[intent_id] = Reservation(venue, side, qty)

    def consume(self, intent_id: str, qty: float) -> None:
        r = self.held.get(intent_id)
        if r is not None:
            r.remaining = max(0.0, r.remaining - qty)

    def release(self, intent_id: str) -> float:
        r = self.held.pop(intent_id, None)
        if r is None:
            return 0.0
        self.released += 1
        return r.remaining

    def open_total(self) -> float:
        return sum(r.remaining for r in self.held.values())
