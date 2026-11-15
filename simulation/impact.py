"""Price impact of our own aggressive orders: ONE interface, three levels (owner 14 Nov, execution cost model step C; core, pure Python, every level OFF by default).

  level 1  DisplayedDepthImpact: the exact VWAP cost of a size on the EFFECTIVE depth (displayed size x survival). No fit. The effective depth is what choose_tactic's `executable_depth_within_cap` (CR-LQ) reads.
  level 2  CurveImpact: a fitted curve for the PEAK impact of an order of size Q, in basis points, in two forms
             (a) depth:   cost_bps = a + b x (Q / D_k) ^ delta         D_k = depth within k bps of the touch at the decision
             (b) volume:  cost_bps = Y x sigma_daily_bps x (Q / V_daily) ^ delta
  level 3  ImpactOverlay: after each aggressive order of ours the later prices of its venue move by sign x peak x (phi + (1 - phi) x g(age)), phi = permanent fraction, g = the transient: option A, an exponential
           refill (Obizhaeva-Wang), g(u) = exp(-u / tau); option B, a propagator kernel, g(u) = (1 + u / tau) ^ -beta. The OTHER venue moves by the cross-impact coefficient x our venue's shift. Overlapping orders add.
           The overlay only COMPUTES the shift (exactly); the replay applies it (new_backtest.session_replay) and the queue adapter's taker walk reads the shifted book.
  level 1 also holds the FILL RANGE (owner 15 Nov, CR-LQ, optional): the size a market-style order can expect to get inside its cap at survival x 0.8 / 1.0 / min(1, x 1.2), less our own recent take that has not refilled (take x exp(-age / tau)); `fill_range_totals` is the one implementation, `choose_tactic` calls it.
Every number is a setting (ImpactConfig), fitted on public archives and labelled PROXY, or ASSUMED. With ImpactConfig.level 0 (the default) nothing here is built or called."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

KERNELS = ("exp", "power")
FORMS = ("depth", "volume")


@dataclass
class ImpactConfig:
    """level 0 = off (the default; a profile without `impact:` has none). Level 3 builds the overlay, which takes its peak from the level-2 curve given by `form`."""
    level: int = 0
    form: str = "depth"                     # level-2 form that gives the peak of an order
    # form (a): cost_bps = a + b * (Q / D_k) ** delta
    a_bps: float = 0.0
    b_bps: float = 0.0
    delta: float = 0.5
    depth_k_bps: float = 5.0                # k of D_k (depth within k bps of the touch, on the side the order consumes)
    # form (b): cost_bps = Y * sigma_daily_bps * (Q / V_daily) ** delta_volume
    y: float = 0.0
    sigma_daily_bps: float = 0.0
    daily_volume: float = 0.0               # base units
    delta_volume: float = 0.5
    # the path after the order
    permanent_fraction: float = 0.0         # phi
    kernel: str = "exp"                     # "exp" (option A) | "power" (option B)
    tau_s: float = 10.0
    beta: float = 0.5
    cross_bybit_to_binance: float = 0.0     # the other venue's shift per unit of ours (bps per bps), by direction
    cross_binance_to_bybit: float = 0.0
    own_refill_tau_s: float = 3.5           # step 2 (owner 15 Nov): the refill constant of our own takes under own_impact "refill" (a take comes back as 1 - exp(-t / tau)); ASSUMED (the isolated-run fit of tau is 3.5 s, not identified well); read only by that mode
    label: str = "ASSUMED"                  # where the numbers come from: ASSUMED | PROXY (fitted on public archives; say which in the profile)

    def __post_init__(self) -> None:
        if self.level not in (0, 1, 2, 3):
            raise ValueError(f"impact.level must be 0, 1, 2 or 3, not {self.level!r}")
        if self.form not in FORMS or self.kernel not in KERNELS:
            raise ValueError(f"impact.form must be one of {FORMS} and impact.kernel one of {KERNELS}")
        for name in ("a_bps", "b_bps", "delta", "depth_k_bps", "y", "sigma_daily_bps", "daily_volume", "delta_volume", "permanent_fraction", "tau_s", "beta", "cross_bybit_to_binance", "cross_binance_to_bybit", "own_refill_tau_s"):
            v = getattr(self, name)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or math.isnan(v) or v < 0:
                raise ValueError(f"impact.{name} must be a number >= 0, not {v!r}")
        if not 0.0 <= self.permanent_fraction <= 1.0:
            raise ValueError("impact.permanent_fraction must be in [0, 1]")
        if self.tau_s <= 0 or self.depth_k_bps <= 0 or self.own_refill_tau_s <= 0:
            raise ValueError("impact.tau_s, impact.depth_k_bps and impact.own_refill_tau_s must be positive")


@dataclass(frozen=True)
class FillRange:
    """Size available inside the cap at the three survival cases (base units): `low` (survival x low multiplier: the PESSIMISTIC case), `expected` (survival as measured) and `high` (survival x high multiplier, capped at 1)."""
    low: float
    expected: float
    high: float

    def expected_fill(self, qty: float) -> float:
        return min(qty, self.expected)

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.low, self.expected, self.high)


def own_unrefilled(takes, now: float, tau_s: float | None) -> float:
    """The part of our own recent aggressive takes still missing from the book at `now`: sum(qty x exp(-(now - ts) / tau)) (the part refilled is 1 - exp(-t / tau)). `takes` are (ts, qty) pairs; a take in the future of `now` is ignored. With tau None or <= 0 no refill is assumed: the whole take counts as missing."""
    total = 0.0
    for ts, qty in takes:
        age = now - ts
        if age < 0:
            continue
        total += qty if not tau_s or tau_s <= 0 else qty * math.exp(-age / tau_s)
    return total


def fill_range_totals(displayed_in_cap: float, survival: float, own_unrefilled_qty: float = 0.0, low: float = 0.8, high: float = 1.2) -> FillRange:
    """available(m) = max(0, displayed_in_cap x min(1, survival x m) - own_unrefilled_qty) at m = low, 1.0, high. Exact, no fitted parameter beyond the two multipliers (ASSUMED 0.8 and 1.2). The sum of displayed x survival over the levels inside the cap is displayed_in_cap x survival, so only the total is needed."""
    for name, v in (("displayed_in_cap", displayed_in_cap), ("own_unrefilled_qty", own_unrefilled_qty)):
        if not isinstance(v, (int, float)) or isinstance(v, bool) or math.isnan(v) or v < 0:
            raise ValueError(f"{name} must be a number >= 0, not {v!r}")
    if not isinstance(survival, (int, float)) or isinstance(survival, bool) or not 0.0 <= survival <= 1.0:
        raise ValueError("survival must be in [0, 1]")
    if not 0.0 < low <= 1.0 <= high:
        raise ValueError("fill range multipliers need 0 < low <= 1 <= high")
    avail = lambda m: max(0.0, displayed_in_cap * min(1.0, survival * m) - own_unrefilled_qty)
    return FillRange(avail(low), avail(1.0), avail(high))


class OwnTakeLedger:
    """Our own aggressive takes by (book side, price), and what is still missing from the book later (owner 15 Nov, step 2: own consumption with refill). A take of `qty` at `ts` is missing qty x exp(-(now - ts) / tau) at `now` (the part refilled is 1 - exp(-t / tau)),
    summed over the takes of a price. ONE implementation: the float OrderBook (own_impact "refill") and the queue adapter's taker walk both use it, and `own_unrefilled` / `fill_range_totals` carry the same arithmetic.
    side is the side of the BOOK consumed: "ask" when we buy, "bid" when we sell. Queries move forward in time (a replay): an entry whose missing part fell below 1e-9 is dropped; a query in the past of an earlier one is answered without dropping."""

    def __init__(self, tau_s: float) -> None:
        if not isinstance(tau_s, (int, float)) or isinstance(tau_s, bool) or math.isnan(tau_s) or tau_s <= 0:
            raise ValueError(f"tau_s must be a number > 0, not {tau_s!r}")
        self.tau_s = float(tau_s)
        self._takes: dict[str, dict[float, list[tuple[float, float]]]] = {"ask": {}, "bid": {}}
        self._latest = -math.inf
        self.recorded = 0

    def record(self, side: str, price: float, qty: float, ts: float) -> None:
        if side not in self._takes:
            raise ValueError(f"side must be 'ask' or 'bid', not {side!r}")
        if qty <= 0:
            return
        self._takes[side].setdefault(float(price), []).append((float(ts), float(qty)))
        self._latest = max(self._latest, float(ts))
        self.recorded += 1

    def missing_by_price(self, side: str, now: float) -> dict[float, float]:
        """{price: size still missing at `now`} for the side of the book; a take in the future of `now` is ignored."""
        out: dict[float, float] = {}
        book = self._takes[side]
        forward = now >= self._latest
        for price in list(book):
            keep, total = [], 0.0
            for ts, qty in book[price]:
                age = now - ts
                if age < 0:
                    keep.append((ts, qty))
                    continue
                rem = qty * math.exp(-age / self.tau_s)
                total += rem
                if rem > 1e-9 or not forward:
                    keep.append((ts, qty))
            if keep:
                book[price] = keep
            else:
                del book[price]
            if total > 1e-9:
                out[price] = total
        return out

    def missing(self, side: str, price: float, now: float) -> float:
        return self.missing_by_price(side, now).get(float(price), 0.0)


class ImpactModel:
    """The interface: the peak impact in bps of an aggressive order of `qty`. Levels 1 and 2 implement it."""
    level = 0

    def cost_bps(self, qty: float, **state) -> float | None:
        raise NotImplementedError


class DisplayedDepthImpact(ImpactModel):
    """Level 1. `levels` are (price, size) from the touch outward on the side the order consumes; `survival` in [0, 1] scales every displayed size (use the MEAN of the survival measure, not its median, which is 1.0 on
    most books). Exact: no fitted parameter."""
    level = 1

    @staticmethod
    def effective_levels(levels, survival: float = 1.0) -> list[tuple[float, float]]:
        if not 0.0 <= survival <= 1.0:
            raise ValueError("survival must be in [0, 1]")
        return [(p, s * survival) for p, s in levels]

    def vwap(self, levels, qty: float, survival: float = 1.0) -> float | None:
        """VWAP of `qty` walked on the effective depth, None when the effective depth cannot fill it."""
        if qty <= 0:
            raise ValueError("qty must be positive")
        need, notional = qty, 0.0
        for price, size in self.effective_levels(levels, survival):
            take = min(size, need)
            notional += take * price
            need -= take
            if need <= 1e-12:
                return notional / qty
        return None

    def vwap_cost_bps(self, levels, qty: float, survival: float = 1.0) -> float | None:
        """(VWAP - touch) / touch in bps, positive = worse for the order whichever side it consumes (the touch is the first level)."""
        if not levels:
            return None
        v = self.vwap(levels, qty, survival)
        return None if v is None else abs(v - levels[0][0]) / levels[0][0] * 1e4

    def effective_depth_within_cap(self, levels, cap_price: float, buying: bool, survival: float = 1.0) -> float:
        """Effective size resting at prices no worse than the cap (<= cap for a buy, >= cap for a sell): the `executable_depth_within_cap` of a MarketState."""
        total = 0.0
        for price, size in self.effective_levels(levels, survival):
            if (buying and price > cap_price) or (not buying and price < cap_price):
                break
            total += size
        return total

    def fill_range(self, levels, cap_price: float, buying: bool, survival: float, own_takes=(), now: float = 0.0, tau_s: float | None = None, low: float = 0.8, high: float = 1.2) -> FillRange:
        """The fill range of an order capped at `cap_price` on the DISPLAYED depth `levels`: see fill_range_totals; the own take still missing is own_unrefilled(own_takes, now, tau_s)."""
        displayed = self.effective_depth_within_cap(levels, cap_price, buying, survival=1.0)
        return fill_range_totals(displayed, survival, own_unrefilled(own_takes, now, tau_s), low, high)

    def cost_bps(self, qty: float, *, levels=(), survival: float = 1.0, **_) -> float | None:
        return self.vwap_cost_bps(levels, qty, survival)


class CurveImpact(ImpactModel):
    """Level 2: the fitted peak curve of ImpactConfig.form. None when an input the form needs is missing or not positive."""
    level = 2

    def __init__(self, cfg: ImpactConfig) -> None:
        self.cfg = cfg

    def cost_bps(self, qty: float, *, depth_k: float | None = None, daily_volume: float | None = None, sigma_daily_bps: float | None = None, **_) -> float | None:
        c = self.cfg
        if qty <= 0:
            raise ValueError("qty must be positive")
        if c.form == "depth":
            if not depth_k or depth_k <= 0:
                return None
            return c.a_bps + c.b_bps * (qty / depth_k) ** c.delta
        vol = daily_volume or c.daily_volume
        sig = sigma_daily_bps or c.sigma_daily_bps
        if not vol or vol <= 0 or not sig or sig <= 0:
            return None
        return c.y * sig * (qty / vol) ** c.delta_volume

    def size_for_peak_bps(self, target_bps: float, *, depth_k: float | None = None, daily_volume: float | None = None, sigma_daily_bps: float | None = None) -> float | None:
        """The order size whose predicted peak equals `target_bps` (invert the form); None when it cannot be inverted (a zero slope or a missing input)."""
        c = self.cfg
        if c.form == "depth":
            if not depth_k or c.b_bps <= 0 or target_bps <= c.a_bps:
                return None
            return depth_k * ((target_bps - c.a_bps) / c.b_bps) ** (1.0 / c.delta)
        vol = daily_volume or c.daily_volume
        sig = sigma_daily_bps or c.sigma_daily_bps
        if not vol or not sig or c.y <= 0:
            return None
        return vol * (target_bps / (c.y * sig)) ** (1.0 / c.delta_volume)


@dataclass
class _Order:
    venue: str
    ts: float
    sign: int
    peak_bps: float


class ImpactOverlay:
    """Level 3: the shift of every later price, in bps, by venue and time. Pure arithmetic over the recorded orders; it never reads a book.
      own(v, t)   = sum over our orders i on v with ts_i < t of sign_i x peak_i x (phi + (1 - phi) x g(t - ts_i))
      shift(v, t) = own(v, t) + cross(w -> v) x own(w, t)          w the other venue
    A buy has sign +1 (prices rise), a sell -1. An order's own path starts strictly AFTER its time (the order itself fills at the price it found). Orders add.
    Speed: while queries only move forward in time and lie after every recorded order (the replay), the permanent part is a running sum and an order whose transient fell below 1e-9 bps is dropped from the live list;
    any other query is answered exactly from all the orders."""

    def __init__(self, cfg: ImpactConfig, ticks: dict[str, float], model: ImpactModel | None = None) -> None:
        self.cfg = cfg
        self.ticks = dict(ticks)
        self.model = model if model is not None else CurveImpact(cfg)
        self._perm: dict[str, float] = {v: 0.0 for v in ticks}              # the permanent part of every order so far, bps (never decays)
        self._live: dict[str, list[_Order]] = {v: [] for v in ticks}        # orders whose transient is still material
        self._last_ts: dict[str, float] = {v: -math.inf for v in ticks}
        self._last_q: dict[str, float] = {v: -math.inf for v in ticks}
        self.orders: list[_Order] = []
        self.applied_ticks: dict[str, int] = {v: 0 for v in ticks}          # the whole-tick shift the replay's feed of each venue carries NOW (set by the driver at each event; the taker walk reads it, so both see one world)
        self.counts = {"orders": 0, "orders_without_peak": 0}

    def g(self, age_s: float) -> float:
        c = self.cfg
        return math.exp(-age_s / c.tau_s) if c.kernel == "exp" else (1.0 + age_s / c.tau_s) ** (-c.beta)

    def add_order(self, venue: str, ts: float, side: str, qty: float, peak_bps: float | None = None, **state) -> float | None:
        """Record an aggressive order of ours that filled `qty` on `venue` at `ts`. The peak is given, or comes from the model with the decision-time `state` (depth_k, daily_volume, ...). Returns the peak used, None
        (and counts it) when no peak can be computed: such an order moves nothing."""
        if venue not in self.ticks:
            raise ValueError(f"unknown venue {venue!r}")
        peak = peak_bps if peak_bps is not None else self.model.cost_bps(qty, **state)
        self.counts["orders"] += 1
        if peak is None or peak <= 0:
            self.counts["orders_without_peak"] += 1
            return None
        o = _Order(venue, float(ts), 1 if str(side).lower().startswith("b") else -1, float(peak))
        self.orders.append(o)
        self._last_ts[venue] = max(self._last_ts[venue], o.ts)
        phi = self.cfg.permanent_fraction
        self._perm[venue] += o.sign * o.peak_bps * phi
        if phi < 1.0:
            self._live[venue].append(o)
        return o.peak_bps

    def own_shift_bps(self, venue: str, t: float) -> float:
        if venue not in self._perm:
            return 0.0
        phi = self.cfg.permanent_fraction
        if t > self._last_ts[venue] and t >= self._last_q[venue]:            # fast path: every order is in the past and time moves forward
            self._last_q[venue] = t
            total, keep = self._perm[venue], []
            for o in self._live[venue]:
                term = o.sign * o.peak_bps * (1.0 - phi) * self.g(t - o.ts)
                total += term
                if abs(term) > 1e-9:
                    keep.append(o)
            self._live[venue] = keep
            return total
        return sum(o.sign * o.peak_bps * (phi + (1.0 - phi) * self.g(t - o.ts)) for o in self.orders if o.venue == venue and o.ts < t)

    def cross_coefficient(self, from_venue: str, to_venue: str) -> float:
        c = self.cfg
        if (from_venue, to_venue) == ("bybit", "binance"):
            return c.cross_bybit_to_binance
        if (from_venue, to_venue) == ("binance", "bybit"):
            return c.cross_binance_to_bybit
        return 0.0

    def shift_bps(self, venue: str, t: float) -> float:
        total = self.own_shift_bps(venue, t)
        for w in self.ticks:
            if w != venue:
                k = self.cross_coefficient(w, venue)
                if k:
                    total += k * self.own_shift_bps(w, t)
        return total

    def shift_ticks(self, venue: str, t: float, ref_price: float) -> int:
        """The shift as a whole number of the venue's OWN ticks at `ref_price` (nearest; half a tick or less rounds to zero)."""
        return int(round(self.shift_bps(venue, t) * ref_price / 1e4 / self.ticks[venue]))
