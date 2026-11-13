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

    def quote_breach(self, cap: float, legs: list[tuple[str, str, float]], net_base: float) -> str:
        """'' if the quote legs fit under the per-pair inventory cap, else "inventory_cap". Pure. The inventory the quotes could build is the filled net base plus every resting reservation plus the new legs, on
        each side: a buy may not take the pair above +cap, a sell not below -cap. A leg that reduces the inventory is never refused by the cap (it moves the pair towards flat)."""
        buys, sells = self.totals()
        add_b = sum(q for _, side, q in legs if side == "buy")
        add_s = sum(q for _, side, q in legs if side == "sell")
        if add_b > 0 and net_base + sum(buys.values()) + add_b > cap + EPS and add_b > max(0.0, -net_base) + EPS:
            return "inventory_cap"
        if add_s > 0 and net_base - sum(sells.values()) - add_s < -cap - EPS and add_s > max(0.0, net_base) + EPS:
            return "inventory_cap"
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


# ── flatten on a breaker trip (Phase E, owner 9 Nov) ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RestingChild:
    order_id: str
    venue: str
    tactic: str = "PASSIVE"


@dataclass(frozen=True)
class FlattenStep:
    kind: str                      # "CANCEL" | "FLATTEN" | "ALARM"
    reason: str
    venue: str = ""
    side: str = ""                 # FLATTEN: "buy" | "sell"
    qty: float = 0.0
    tactic: str = ""               # FLATTEN: always MARKET_BANDED
    worst_price: float | None = None
    order_id: str = ""             # CANCEL


def plan_flatten_on_trip(resting: list[RestingChild], base: dict[str, float], mid: dict[str, float], *, band_bps: float, attempt: int, retry_budget: int,
                         available: dict[str, bool] | None = None, mode: str = "net", touch: dict[str, tuple[float, float]] | None = None) -> list[FlattenStep]:
    """What the execution layer does when the breaker TRIPS. Pure; the caller executes the steps (cancels with their real latency, the flatten as a MARKET_BANDED intent inside the unwind band) and calls this again
    after each attempt with `attempt` raised. In order: (1) a CANCEL for every resting PASSIVE child (no new risk may rest); (2) a FLATTEN of the residual exposure, MARKET_BANDED with a worst price `band_bps` from the
    venue's mid: mode "net" (default) sells or buys only the NET base, on a venue that holds a position of the net's sign (closing both legs would cost two taker fees and is not needed to remove the exposure),
    mode "all" closes every venue's base; (3) once `attempt` has reached `retry_budget` with exposure still open, no more sends: a persistent ALARM step on every call.
    `touch` (owner 11 Nov): per venue (best bid, best ask). When given, the worst price is the band from the TOUCH the order would cross (a sell: the bid, a buy: the ask), the unwind rule of the core
    (a unwind band around the touch); without it the band is from the mid, as before.
    `base`: signed base quantity per venue (long positive). `available`: a venue whose gateway is down or whose book is not valid cannot be flattened on; another venue holding the sign is used, else an ALARM."""
    if mode not in ("net", "all"):
        raise ValueError("mode must be net or all")
    if retry_budget < 1 or attempt < 0:
        raise ValueError("retry_budget must be >= 1 and attempt >= 0")
    steps = [FlattenStep("CANCEL", "TRIP_CANCEL_RESTING", venue=c.venue, order_id=c.order_id) for c in resting if c.tactic == "PASSIVE"]
    up = lambda v: (available or {}).get(v, True)
    targets: list[tuple[str, float]] = []                       # (venue, signed quantity to close)
    if mode == "all":
        targets = [(v, q) for v, q in sorted(base.items()) if abs(q) > EPS]
    else:
        net = sum(base.values())
        if abs(net) > EPS:
            holders = sorted((v for v, q in base.items() if q * net > 0 and up(v)), key=lambda v: (-abs(base[v]), v))
            if holders:
                targets = [(holders[0], net)]
            else:
                steps.append(FlattenStep("ALARM", "NO_AVAILABLE_VENUE_TO_FLATTEN_ON", qty=abs(net)))
                return steps
    if not targets:
        return steps
    if attempt >= retry_budget:
        steps.append(FlattenStep("ALARM", "FLATTEN_RETRY_BUDGET_EXHAUSTED", qty=sum(abs(q) for _, q in targets)))
        return steps
    for venue, qty in targets:
        if not up(venue):
            steps.append(FlattenStep("ALARM", "VENUE_DOWN_CANNOT_FLATTEN", venue=venue, qty=abs(qty)))
            continue
        sell = qty > 0
        m = (touch[venue][0] if sell else touch[venue][1]) if touch and venue in touch else mid[venue]
        steps.append(FlattenStep("FLATTEN", "TRIP_FLATTEN", venue=venue, side="sell" if sell else "buy", qty=abs(qty), tactic="MARKET_BANDED",
                                 worst_price=m * (1.0 - band_bps / 1e4) if sell else m * (1.0 + band_bps / 1e4)))
    return steps
