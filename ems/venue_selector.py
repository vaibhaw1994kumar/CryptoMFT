"""Phase E (owner 9 Nov): the venue selector. Pure functions, no clock, no book, no order is touched; costs are in basis points and every rejected alternative is returned with its cost (to be logged by the caller).
Two decisions:
  assign_rest_hedge   which venue RESTS the passive leg and which HEDGES it, for one direction. Mode "fixed" (the default, today's rule: Bybit rests, Binance hedges) never changes the assignment and only REPORTS what
                      "dynamic" would have chosen and the cost difference; "dynamic" takes the cheaper one. Expected cost of an assignment per filled unit =
                      maker fee (negative = rebate) of the resting venue + taker fee of the hedge venue + expected hedge slippage + adverse selection at the resting venue's fill + miss rate x the loss of a missed hedge.
                      The fill probability is reported beside it, not folded in (a low fill probability must not make an assignment look cheap).
  split_taker         how a taker order (taker_taker legs, unwinds) is split across venues: by the marginal cost of the next unit (price moved by the taker fee), within each venue's notional limit.
A venue whose gateway is down or whose book is not valid (`available` False) is never chosen, in either mode: with a fixed assignment that touches an unavailable venue the answer is `chosen=None` with the reason.
Nothing here is wired into a strategy; there is no switch to set."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RestHedgeVenue:
    """What the assignment needs to know about one venue, both as a resting venue and as a hedge venue."""
    venue: str
    available: bool                  # gateway up AND book valid
    maker_bps: float                 # fee on a passive fill; negative = rebate
    taker_bps: float
    hedge_slippage_bps: float        # expected slippage when this venue hedges
    adverse_bps: float               # expected adverse selection after a passive fill here (from training days)
    miss_rate: float                 # share of hedges here that are missed (rejected, unknown)
    miss_loss_bps: float             # loss of a missed hedge
    fill_probability: float = float("nan")      # at the chosen placement; reported, not used in the cost


@dataclass(frozen=True)
class AssignmentCost:
    rest: str
    hedge: str
    cost_bps: float
    parts: dict = field(default_factory=dict)
    fill_probability: float = float("nan")


@dataclass(frozen=True)
class AssignmentDecision:
    mode: str                                    # "fixed" | "dynamic"
    chosen: AssignmentCost | None                # None: no assignment possible (the reason below)
    reason: str                                  # "FIXED", "DYNAMIC_CHEAPEST", "VENUE_UNAVAILABLE", "NO_AVAILABLE_PAIR"
    alternatives: tuple[AssignmentCost, ...]     # every other assignment considered, with its cost
    dynamic_choice: AssignmentCost | None        # what dynamic would take (equal to `chosen` in dynamic mode)
    cost_difference_bps: float                   # chosen cost minus the dynamic choice's cost (0 in dynamic mode; > 0 = fixed costs more)


def assignment_cost(rest: RestHedgeVenue, hedge: RestHedgeVenue) -> AssignmentCost:
    parts = {"maker_bps": rest.maker_bps, "hedge_taker_bps": hedge.taker_bps, "hedge_slippage_bps": hedge.hedge_slippage_bps, "adverse_bps": rest.adverse_bps, "miss_cost_bps": hedge.miss_rate * hedge.miss_loss_bps}
    return AssignmentCost(rest.venue, hedge.venue, sum(parts.values()), parts, rest.fill_probability)


def assign_rest_hedge(venues: list[RestHedgeVenue], *, mode: str = "fixed", fixed: tuple[str, str] = ("BYBIT", "BINANCE")) -> AssignmentDecision:
    """`fixed` = (resting venue, hedge venue)."""
    if mode not in ("fixed", "dynamic"):
        raise ValueError(f"mode must be fixed or dynamic, not {mode!r}")
    if len(venues) != 2 or len({v.venue for v in venues}) != 2:
        raise ValueError("two distinct venues are needed")
    a, b = venues
    every = [assignment_cost(a, b), assignment_cost(b, a)]
    usable = [c for c in every if next(v for v in venues if v.venue == c.rest).available and next(v for v in venues if v.venue == c.hedge).available]
    dynamic = min(usable, key=lambda c: (c.cost_bps, c.rest)) if usable else None
    if mode == "dynamic":
        if dynamic is None:
            return AssignmentDecision(mode, None, "NO_AVAILABLE_PAIR", tuple(every), None, 0.0)
        return AssignmentDecision(mode, dynamic, "DYNAMIC_CHEAPEST", tuple(c for c in every if c is not dynamic), dynamic, 0.0)
    want = next((c for c in every if (c.rest, c.hedge) == fixed), None)
    if want is None:
        raise ValueError(f"the fixed assignment {fixed} names a venue that is not among {[v.venue for v in venues]}")
    if want not in usable:
        return AssignmentDecision(mode, None, "VENUE_UNAVAILABLE", tuple(c for c in every if c is not want), dynamic, float("nan"))
    return AssignmentDecision(mode, want, "FIXED", tuple(c for c in every if c is not want), dynamic, want.cost_bps - dynamic.cost_bps)


@dataclass(frozen=True)
class TakerVenue:
    venue: str
    available: bool
    taker_bps: float
    levels: tuple[tuple[float, float], ...]       # (price, quantity) from the touch outward on the side the order consumes
    max_notional: float = float("inf")            # the venue's notional (and margin) limit for this order


@dataclass(frozen=True)
class SplitResult:
    side: str
    quantity: float
    allocation: dict                              # venue -> quantity
    unfilled: float
    average_effective_price: float                # price moved by the taker fee, volume weighted over the whole fill
    alternatives: dict                            # venue -> (average effective price, quantity) if that venue alone took as much as it could


def split_taker(side: str, quantity: float, venues: list[TakerVenue]) -> SplitResult:
    """Take the next unit from the venue where it is cheapest (price moved by the taker fee: up for a buy, down for a sell), respecting each venue's notional limit and skipping unavailable venues."""
    if side not in ("buy", "sell"):
        raise ValueError("side must be buy or sell")
    sign = 1.0 if side == "buy" else -1.0
    fee = lambda v: v.taker_bps / 1e4
    eff = lambda v, price: price * (1.0 + sign * fee(v))
    pool = [(eff(v, p), p, q, v) for v in venues if v.available for p, q in v.levels if q > 0]
    pool.sort(key=lambda x: (sign * x[0], x[3].venue))
    left = quantity
    got: dict[str, float] = {}
    notional: dict[str, float] = {}
    paid = 0.0
    for e, p, q, v in pool:
        if left <= 1e-12:
            break
        room = (v.max_notional - notional.get(v.venue, 0.0)) / p
        take = min(q, left, max(room, 0.0))
        if take <= 1e-12:
            continue
        got[v.venue] = got.get(v.venue, 0.0) + take
        notional[v.venue] = notional.get(v.venue, 0.0) + take * p
        paid += take * e
        left -= take
    filled = quantity - left
    alt = {}
    for v in venues:                                    # what each venue alone would have done, within its own notional limit
        if not v.available:
            continue
        rest, spent, used = quantity, 0.0, 0.0
        for p, q in v.levels:
            take = min(q, rest, max((v.max_notional - used) / p, 0.0))
            if take <= 1e-12:
                continue
            spent += take * eff(v, p)
            used += take * p
            rest -= take
            if rest <= 1e-12:
                break
        taken = quantity - rest
        alt[v.venue] = (spent / taken if taken > 1e-12 else float("nan"), taken)
    return SplitResult(side, quantity, got, left, paid / filled if filled > 1e-12 else float("nan"), alt)
