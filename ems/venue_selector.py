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
import bisect
import math
import random


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


# ── order split: how much to rest and how much to cross (Cont and Kukanov, "Optimal order placement in limit order markets", arXiv 1210.1625; owner 14 Nov, step E; pure, nothing is wired to a strategy) ─────────────
@dataclass(frozen=True)
class SplitVenue:
    """One venue's inputs to the split, all in basis points per unit of size except the queue and the outflow samples.
    queue_ahead      Q_k: size ahead of a limit order at its price
    outflow          F_k: samples of the volume that leaves the level over the horizon T (trades and cancels that reach our price), from the queue model or from replays; sorted or not
    half_spread_bps  h_k      fee_bps f_k (taker fee)      rebate_bps r_k (a maker rebate; a maker fee is negative)      adverse_bps AS_k (expected adverse move per FILLED limit unit, from markouts)
    impact_bps       theta_k: the cost per unit of finishing an unfilled remainder at market later (from the impact model at that size); it adds to the underfill penalty
    max_market_qty   M_k <= the effective depth before the first gap and within the price cap (B and C level 1); max_limit_qty an optional cap on the resting size"""
    venue: str
    available: bool
    queue_ahead: float
    outflow: tuple
    half_spread_bps: float
    fee_bps: float
    rebate_bps: float
    adverse_bps: float = 0.0
    impact_bps: float = 0.0
    max_market_qty: float = float("inf")
    max_limit_qty: float = float("inf")


@dataclass(frozen=True)
class OrderSplit:
    quantity: float
    market: dict                                  # venue -> market size now
    limit: dict                                   # venue -> resting size
    expected_cost_bps_per_unit: float | None      # expected cost of the chosen split over the outflow samples (None for the closed form without a sample set)
    method: str                                   # "closed_form" | "stochastic_approximation" | "table" | "none"
    p: float | None = None                        # the closed form's quantile level
    status: str = "OK"                            # OK | NO_VENUE | INFEASIBLE_MARKET_CAP (the market caps cannot take what the limits do not)
    inputs: dict = field(default_factory=dict)


def _quantile(sorted_samples, p: float) -> float:
    """Empirical quantile with linear interpolation; p outside [0, 1] is clipped."""
    n = len(sorted_samples)
    if n == 0:
        raise ValueError("an empty outflow sample set has no quantile")
    p = min(1.0, max(0.0, p))
    x = p * (n - 1)
    lo = int(math.floor(x))
    hi = min(lo + 1, n - 1)
    return sorted_samples[lo] + (sorted_samples[hi] - sorted_samples[lo]) * (x - lo)


def split_single_venue(quantity: float, v: SplitVenue, lambda_u_bps: float, *, lambda_o_bps: float = 0.0) -> OrderSplit:
    """The closed form for one venue. With S the size, M + L = S, the expected cost per unit is d cost / dL = -(h + f) - (h + r - AS) (1 - F) + (lambda_u + theta) F, F the CDF of the filled part (outflow - Q)+;
    setting it to zero gives p = F = (2h + f + r') / (lambda_u + theta + h + r'), r' = r - AS (the rebate net of adverse selection), and L* = F^-1(p) - Q, M* = S - L*, both clipped to [0, S]. With AS 0 and theta 0
    this is (2h + f + r) / (lambda_u + h + r). The overfill penalty lambda_o is carried for the interface: with M + L = S nothing can be overfilled, so it never changes the answer."""
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if not v.available:
        return OrderSplit(quantity, {}, {}, None, "none", None, "NO_VENUE")
    r_net = v.rebate_bps - v.adverse_bps
    denom = lambda_u_bps + v.impact_bps + v.half_spread_bps + r_net
    num = 2.0 * v.half_spread_bps + v.fee_bps + r_net
    if denom <= 0:
        raise ValueError("lambda_u + theta + h + r' must be positive")
    p = num / denom
    xs = sorted(v.outflow)
    if p <= 0.0:                                          # resting costs more than crossing at every fill probability: everything at market
        limit = 0.0
    elif p >= 1.0:                                        # resting beats crossing even when it fills: everything rests
        limit = quantity
    else:
        limit = min(max(_quantile(xs, p) - v.queue_ahead, 0.0), quantity, v.max_limit_qty)
    market = quantity - limit
    status = "OK"
    if market > v.max_market_qty + 1e-12:                 # the market part must fit the cap (the depth before the first gap, within the price cap); the rest goes back to the limit
        market = v.max_market_qty
        limit = quantity - market
        status = "MARKET_CAPPED"
    return OrderSplit(quantity, {v.venue: market} if market > 1e-12 else {}, {v.venue: limit} if limit > 1e-12 else {}, None, "closed_form", p, status,
                      {"lambda_u_bps": lambda_u_bps, "queue_ahead": v.queue_ahead, "samples": len(xs)})


def expected_split_cost(quantity: float, venues, market: dict, limit: dict, lambda_u_bps: float, lambda_o_bps: float = 0.0, *, samples: int | None = None, seed: int = 0) -> float:
    """Expected cost per unit (bps of the order's price) of a split over the venues' outflow samples (the first `samples` of each, paired by index; all of them by default): market pays (h + f) per unit,
    each FILLED limit unit gains (h + r - AS), each unfilled limit unit pays lambda_u + theta_k, anything executed beyond the quantity pays lambda_o."""
    vs = [v for v in venues if v.available]
    n = min(len(v.outflow) for v in vs) if samples is None else samples
    total = 0.0
    for i in range(n):
        executed, cost = 0.0, 0.0
        for v in vs:
            m, l = market.get(v.venue, 0.0), limit.get(v.venue, 0.0)
            filled = min(l, max(v.outflow[i] - v.queue_ahead, 0.0))
            executed += m + filled
            cost += (v.half_spread_bps + v.fee_bps) * m - (v.half_spread_bps + v.rebate_bps - v.adverse_bps) * filled + (lambda_u_bps + v.impact_bps) * (l - filled)
        cost += lambda_o_bps * max(executed - quantity, 0.0)
        total += cost
    return total / (max(n, 1) * quantity)


def _project(y, upper, total):
    """Euclidean projection of y onto {0 <= x_i <= upper_i, sum x = total} by bisection on the shift; None when the caps cannot reach `total`."""
    if sum(upper) < total - 1e-9:
        return None
    lo, hi = min(y) - max(upper) - total, max(y) + 1.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if sum(min(max(yi - mid, 0.0), ui) for yi, ui in zip(y, upper)) > total:
            lo = mid
        else:
            hi = mid
    tau = 0.5 * (lo + hi)
    return [min(max(yi - tau, 0.0), ui) for yi, ui in zip(y, upper)]


def split_multi_venue(quantity: float, venues, lambda_u_bps: float, *, lambda_o_bps: float = 0.0, epochs: int = 5, seed: int = 0, burn_in: float = 0.1) -> OrderSplit:
    """Several venues: stochastic approximation over the outflow samples (Robbins-Monro steps a / sqrt(n), the iterates averaged after a burn-in: the averaged estimate) under M_k + L_k summing to the quantity,
    M_k <= max_market_qty and L_k <= max_limit_qty (Euclidean projection onto the capped simplex). The per-sample gradient: a market unit on k costs h_k + f_k; one more resting unit on k gains (h_k + r_k - AS_k)
    when the sample fills the whole resting size and costs lambda_u + theta_k when it does not. Deterministic for a given seed."""
    vs = [v for v in venues if v.available]
    if not vs:
        return OrderSplit(quantity, {}, {}, None, "none", None, "NO_VENUE")
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    K = len(vs)
    upper = [v.max_market_qty if v.max_market_qty < float("inf") else quantity for v in vs] + [v.max_limit_qty if v.max_limit_qty < float("inf") else quantity for v in vs]
    upper = [min(u, quantity) for u in upper]
    start = _project([0.0] * K + [quantity / K] * K, upper, quantity)
    if start is None:
        return OrderSplit(quantity, {}, {}, None, "none", None, "INFEASIBLE_MARKET_CAP")
    x = list(start)
    rng = random.Random(seed)
    n_samples = min(len(v.outflow) for v in vs)
    scale = max(abs(v.half_spread_bps) + abs(v.fee_bps) + abs(v.rebate_bps) + abs(v.adverse_bps) + abs(v.impact_bps) for v in vs) + lambda_u_bps + lambda_o_bps + 1e-9
    total_steps = epochs * n_samples
    avg, count = [0.0] * (2 * K), 0
    for step in range(1, total_steps + 1):
        i = rng.randrange(n_samples)
        filled = [min(x[K + k], max(vs[k].outflow[i] - vs[k].queue_ahead, 0.0)) for k in range(K)]
        g = [0.0] * (2 * K)
        for k, v in enumerate(vs):
            g[k] = v.half_spread_bps + v.fee_bps                                                        # a market unit: crosses, executes for sure
            if x[K + k] <= max(v.outflow[i] - v.queue_ahead, 0.0):                                      # this sample fills the whole resting size: one more resting unit fills too and gains (h + r - AS)
                g[K + k] = -(v.half_spread_bps + v.rebate_bps - v.adverse_bps)
            else:                                                                                       # it fills only part: one more resting unit stays unfilled and pays the underfill penalty
                g[K + k] = lambda_u_bps + v.impact_bps
        a = quantity / (scale * math.sqrt(step))
        x = _project([xi - a * gi for xi, gi in zip(x, g)], upper, quantity) or x
        if step > burn_in * total_steps:
            count += 1
            avg = [av + (xi - av) / count for av, xi in zip(avg, x)]
    final = _project(avg, upper, quantity) or avg
    market = {vs[k].venue: final[k] for k in range(K) if final[k] > 1e-9}
    limit = {vs[k].venue: final[K + k] for k in range(K) if final[K + k] > 1e-9}
    cost = expected_split_cost(quantity, vs, market, limit, lambda_u_bps, lambda_o_bps)
    return OrderSplit(quantity, market, limit, cost, "stochastic_approximation", None, "OK", {"lambda_u_bps": lambda_u_bps, "samples": n_samples, "steps": total_steps, "seed": seed})


class SplitTable:
    """The lookup table by queue size and recent volume (single venue). The closed form's limit size is L = max(V x q_p - Q, 0) where q_p is the p-quantile of the outflow measured in units of the recent volume V
    (a normalised sample set), so for fixed costs the table is a 2-D grid over (Q, V), read by bilinear interpolation and clipped to [0, S] at the call. Built once per set of costs; no sampling at decision time."""

    def __init__(self, p: float, queues, volumes, grid) -> None:
        self.p, self.queues, self.volumes, self.grid = p, tuple(queues), tuple(volumes), grid

    @classmethod
    def build(cls, normalised_outflow, v: SplitVenue, lambda_u_bps: float, queues, volumes) -> "SplitTable":
        base = split_single_venue(1.0, SplitVenue(v.venue, True, 0.0, tuple(normalised_outflow), v.half_spread_bps, v.fee_bps, v.rebate_bps, v.adverse_bps, v.impact_bps), lambda_u_bps)
        p = base.p
        xs = sorted(normalised_outflow)
        q = _quantile(xs, p) if 0.0 < p < 1.0 else (0.0 if p <= 0 else float("inf"))
        grid = [[max(vol * q - Q, 0.0) if q != float("inf") else float("inf") for vol in volumes] for Q in queues]
        return cls(p, queues, volumes, grid)

    def limit_size(self, queue_ahead: float, recent_volume: float, quantity: float) -> float:
        def locate(axis, x):
            j = min(max(bisect.bisect_right(axis, x) - 1, 0), len(axis) - 2)
            span = axis[j + 1] - axis[j]
            return j, min(max((x - axis[j]) / span, 0.0), 1.0) if span > 0 else 0.0
        i, a = locate(self.queues, queue_ahead)
        j, b = locate(self.volumes, recent_volume)
        g = self.grid
        val = (1 - a) * ((1 - b) * g[i][j] + b * g[i][j + 1]) + a * ((1 - b) * g[i + 1][j] + b * g[i + 1][j + 1])
        return min(max(val, 0.0), quantity)


@dataclass
class SplitConfig:
    """Step E/F settings for the order split evidence. Present (not None) in a profile = the evidence is computed and journaled with every decision; it never sends anything. EVERY number is ASSUMED until measured.
    horizon_s            T: how long a resting part is given to fill before the remainder is finished at market
    lambda_u_bps         underfill penalty per unit of unfilled size: the expected cost of staying unhedged for T (ASSUMED: one standard deviation of the move over T, sigma_1s x sqrt(T), from step D)
    lambda_o_bps         overfill penalty (inert while market + limit = quantity)
    adverse_bps          adverse selection per filled limit unit (from markouts)
    volume_window_s      the recent traded volume that scales the outflow samples (must be the depth-quality flicker window)
    outflow_ratio_file   JSON list: samples of (depleting aggressor volume over the next T) / (the same side's volume over the previous volume_window_s), from public prints
    min_recent_volume    base units: the scale never falls below this"""
    horizon_s: float = 2.0
    lambda_u_bps: float = 2.6
    lambda_o_bps: float = 0.0
    adverse_bps: float = 0.0
    volume_window_s: float = 10.0
    outflow_ratio_file: str = ""
    min_recent_volume: float = 1.0
    label: str = "ASSUMED"

    def __post_init__(self) -> None:
        for name in ("horizon_s", "lambda_u_bps", "lambda_o_bps", "adverse_bps", "volume_window_s", "min_recent_volume"):
            v = getattr(self, name)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or math.isnan(v) or v < 0:
                raise ValueError(f"split.{name} must be a number >= 0, not {v!r}")
        if self.horizon_s <= 0 or self.volume_window_s <= 0 or self.min_recent_volume <= 0:
            raise ValueError("split.horizon_s, split.volume_window_s and split.min_recent_volume must be positive")
