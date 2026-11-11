"""Phase E (owner 9 Nov): the risk gate, ONE pre-send check every child order passes through. Pure: it reads the state it is given and answers allowed or refused with a reason code (and, for an unknown hedge, the
action to take instead). It sits AROUND the phase 0 reservation (`ReservationBook`): the caller builds `ExposureView` from the layer's reservations and the OMS, the gate adds nothing to them and writes nothing.
A send is refused unless all hold, in this order:
  1  price cap        a child has a limit price; only MARKET_BANDED may carry a worst price, and it must be within the unwind band of the mid
  2  breaker          PAUSED or TRIPPED blocks NEW risk; risk-reducing children (hedge, unwind, flatten) are always allowed past this rule but are still capped by the rest
  3  one hedge child  at most one live hedge child per hedge intent
  4  no blind replacement  a new hedge or unwind child is refused while an earlier hedge child of the same intent is UNKNOWN: the answer is the action QUERY_STATUS
  5  reachable exposure   the worst case over FILLED + RESTING + PENDING + UNKNOWN children, per side, within the hard net ceiling plus the named fill headroom, and each venue within its cap (new risk only)
  6  margin           per-venue margin utilisation within its limit (new risk only)
  7  rate limit       the scoped limiter's verdict (capacity for cancels is the limiter's business, not this gate's)
"""
from __future__ import annotations

from dataclasses import dataclass, field

RISK_REDUCING_ROLES = ("HEDGE", "UNWIND", "FLATTEN")
REASONS = ("NO_PRICE_CAP", "WORST_PRICE_NOT_ALLOWED", "BAND_VIOLATED", "BREAKER_PAUSED", "BREAKER_TRIPPED", "SECOND_HEDGE_CHILD", "UNKNOWN_HEDGE_PENDING",
           "NET_CEILING", "VENUE_CAP", "MARGIN_UTILISATION", "RATE_LIMIT")


@dataclass(frozen=True)
class GateChild:
    role: str                          # PRIMARY | HEDGE | UNWIND | FLATTEN
    tactic: str                        # PASSIVE | IOC_CAPPED | MARKET_BANDED
    venue: str
    side: str                          # buy | sell
    qty: float
    intent_id: str
    limit_price: float | None = None
    worst_price: float | None = None


@dataclass(frozen=True)
class ExposureView:
    """Signed base quantities (long positive). resting / pending / unknown map (venue, side) -> quantity of children of that kind."""
    filled: dict[str, float] = field(default_factory=dict)
    resting: dict[tuple[str, str], float] = field(default_factory=dict)
    pending: dict[tuple[str, str], float] = field(default_factory=dict)
    unknown: dict[tuple[str, str], float] = field(default_factory=dict)


@dataclass(frozen=True)
class GateLimits:
    net_ceiling: float                 # the hard net ceiling in base units (1x max_position_base)
    fill_headroom: float               # the named headroom above the ceiling that a fill in flight may use
    venue_cap: float                   # |base on one venue| may not exceed this
    band_bps: float = 15.0             # the unwind band
    margin_utilisation_cap: float | None = None


@dataclass(frozen=True)
class GateContext:
    breaker_state: str                                       # OK | PAUSED | TRIPPED
    exposure: ExposureView
    limits: GateLimits
    mid: dict[str, float] = field(default_factory=dict)
    touch: dict[str, tuple[float, float]] = field(default_factory=dict)    # venue -> (best bid, best ask); when present the band is read from the touch the child would cross (owner 11 Nov), else from the mid
    live_hedge_children: dict[str, int] = field(default_factory=dict)      # hedge intent id -> live child count
    unknown_hedge_intents: frozenset = frozenset()                        # intents with a hedge child in UNKNOWN
    margin_utilisation: dict[str, float] = field(default_factory=dict)
    rate_allowed: bool = True


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str = ""
    detail: str = ""
    action: str = ""                   # QUERY_STATUS when an unknown hedge must be resolved first


def reachable_exposure(child_side: str, child_qty: float, venue: str, view: ExposureView) -> tuple[float, float, float]:
    """(worst-case net long, worst-case net short, worst-case |base on the venue|) with the child added: every resting, pending and unknown child fills, on the worst side."""
    net = sum(view.filled.values())
    add = lambda side: sum(q for kind in (view.resting, view.pending, view.unknown) for (v, s), q in kind.items() if s == side) + (child_qty if child_side == side else 0.0)
    long_net, short_net = net + add("buy"), net - add("sell")
    base_v = view.filled.get(venue, 0.0)
    venue_add = lambda side: sum(q for kind in (view.resting, view.pending, view.unknown) for (v, s), q in kind.items() if v == venue and s == side) + (child_qty if child_side == side else 0.0)
    return long_net, short_net, max(abs(base_v + venue_add("buy")), abs(base_v - venue_add("sell")))


def evaluate(child: GateChild, ctx: GateContext) -> GateDecision:
    reducing = child.role in RISK_REDUCING_ROLES
    lim = ctx.limits
    # 1 price cap
    if child.tactic == "MARKET_BANDED":
        if child.worst_price is None:
            return GateDecision(False, "NO_PRICE_CAP", "a MARKET_BANDED child needs its worst price")
        ref = ctx.touch[child.venue][1 if child.side == "buy" else 0] if child.venue in ctx.touch else ctx.mid.get(child.venue)     # the touch the child crosses, else the mid
        mid = ref
        if mid is not None:
            off = (child.worst_price - mid) / mid * 1e4 * (1 if child.side == "buy" else -1)       # bps beyond the reference against us
            if off > lim.band_bps + 1e-9:
                return GateDecision(False, "BAND_VIOLATED", f"worst price {child.worst_price} is {off:.2f} bps from the mid, the band is {lim.band_bps}")
    else:
        if child.worst_price is not None:
            return GateDecision(False, "WORST_PRICE_NOT_ALLOWED", "only MARKET_BANDED may carry a worst price")
        if child.limit_price is None:
            return GateDecision(False, "NO_PRICE_CAP", "the child has no limit price")
    # 2 breaker
    if not reducing and ctx.breaker_state == "PAUSED":
        return GateDecision(False, "BREAKER_PAUSED", "new risk is blocked while the breaker is paused")
    if not reducing and ctx.breaker_state == "TRIPPED":
        return GateDecision(False, "BREAKER_TRIPPED", "new risk is blocked until a manual reset")
    # 3 one live hedge child per hedge intent
    if child.role == "HEDGE" and ctx.live_hedge_children.get(child.intent_id, 0) >= 1:
        return GateDecision(False, "SECOND_HEDGE_CHILD", f"intent {child.intent_id} already has a live hedge child")
    # 4 no blind replacement of an unknown hedge
    if child.role in ("HEDGE", "UNWIND") and child.intent_id in ctx.unknown_hedge_intents:
        return GateDecision(False, "UNKNOWN_HEDGE_PENDING", f"a hedge child of intent {child.intent_id} is UNKNOWN: query its status before sending another", action="QUERY_STATUS")
    # 5 reachable exposure (new risk only: a reducing child is allowed even at the ceiling)
    if not reducing:
        long_net, short_net, venue_base = reachable_exposure(child.side, child.qty, child.venue, ctx.exposure)
        ceiling = lim.net_ceiling + lim.fill_headroom
        if max(abs(long_net), abs(short_net)) > ceiling + 1e-9:
            return GateDecision(False, "NET_CEILING", f"worst-case net {max(abs(long_net), abs(short_net)):.6g} exceeds {ceiling:.6g} (ceiling {lim.net_ceiling} + headroom {lim.fill_headroom})")
        if venue_base > lim.venue_cap + 1e-9:
            return GateDecision(False, "VENUE_CAP", f"worst-case base on {child.venue} {venue_base:.6g} exceeds {lim.venue_cap}")
        # 6 margin
        cap = lim.margin_utilisation_cap
        if cap is not None and ctx.margin_utilisation.get(child.venue, 0.0) > cap + 1e-12:
            return GateDecision(False, "MARGIN_UTILISATION", f"margin utilisation on {child.venue} is {ctx.margin_utilisation[child.venue]:.3f}, the limit {cap}")
    # 7 rate limit
    if not ctx.rate_allowed:
        return GateDecision(False, "RATE_LIMIT", "the scoped rate limiter refuses")
    return GateDecision(True)
