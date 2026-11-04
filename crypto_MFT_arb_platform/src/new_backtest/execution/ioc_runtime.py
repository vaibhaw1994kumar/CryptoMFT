"""Engine glue for the capped-IOC policy: the fundamental, the break-even cap, the offset from the DP, the limit price.

The policy conditions ONLY on what the strategy can see at decision time: the strategy-view touch (never the venue-truth book, which
holds look-ahead), its median over the last 0.25 s as the fundamental, the 1 s range of the view mid as volatility and the clock hour.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from .ioc_policy import Choice, FlickerLaw, PolicyParams, attempts_from_latency, choose, limit_price, load_flicker_law


@dataclass
class Plan:
    limit: float
    offset_bps: float
    p_fill: float
    attempts_left: int
    fundamental: float
    d_cap_bps: float
    clipped: bool
    law_level: str
    law_n: int


@dataclass
class IocRuntime:
    flicker_path: Path
    set_name: str
    v0_bps: float
    phi: float
    deadline_s: float
    attempts_cfg: object           # "from_latency" or an int
    cap_buffer_bps: float
    price_model: str = "limit"
    max_attempts_cap: int = 8
    venues: tuple = ("binance",)      # hedge venues the fitted law covers; hedges on any other venue keep the phase-0 behaviour
    min_p_at_cap: float = 0.02
    counts: dict = field(default_factory=lambda: {"plans": 0, "cap_unreachable": 0, "clipped": 0})
    levels: dict = field(default_factory=dict)

    def params(self, fee_bps: float, q: float, cycle_s: float) -> PolicyParams:
        return PolicyParams(fee_bps=fee_bps, v0_bps=self.v0_bps, phi=self.phi, q=round(q, 3), dt_s=round(cycle_s, 4), price_model=self.price_model)

    def attempts(self, cycle_s: float) -> int:
        if isinstance(self.attempts_cfg, int):
            return max(1, self.attempts_cfg)
        return attempts_from_latency(self.deadline_s, cycle_s, self.max_attempts_cap)

    def break_even_cap(self, side: str, fill_price: float, maker_bps: float, taker_bps: float) -> float:
        """Worst hedge price: the price at which the two legs' net edge is zero after fees, moved against us by cap_buffer_bps.
        A sell hedge after a maker BUY at fill_price; a buy hedge after a maker SELL."""
        fees = (maker_bps + taker_bps) / 1e4
        if side == "sell":
            return fill_price * (1.0 + fees) * (1.0 - self.cap_buffer_bps / 1e4)
        return fill_price * (1.0 - fees) * (1.0 + self.cap_buffer_bps / 1e4)

    def plan(self, *, side: str, fundamental: float, worst_price: float, attempts_left: int, hour: int, vol_bps: float,
             fee_bps: float, q: float, cycle_s: float) -> Plan | None:
        """None = the cap is unreachable at decision time (P(fill at the cap) below min_p_at_cap): no attempt is sent and the
        existing ladder takes the chain to the banded unwind. Counted."""
        d_cap = ((fundamental - worst_price) / fundamental if side == "sell" else (worst_price - fundamental) / fundamental) * 1e4
        law, lvl = load_flicker_law(self.flicker_path, side=side, hour=hour, vol_bps=vol_bps, set_name=self.set_name)
        self.levels[lvl["level"]] = self.levels.get(lvl["level"], 0) + 1
        if float(law.p_fill(d_cap)) < self.min_p_at_cap:
            self.counts["cap_unreachable"] += 1
            return None
        ch: Choice = choose(law, self.params(fee_bps, q, cycle_s), d_cap, max(1, attempts_left))
        lim = worst_price if ch.clipped else limit_price(side, fundamental, ch.offset_bps, worst_price)    # exact cap, not a rounding of it
        self.counts["plans"] += 1
        self.counts["clipped"] += int(ch.clipped)
        return Plan(lim, ch.offset_bps, ch.p_fill, attempts_left, fundamental, d_cap, ch.clipped, lvl["level"], lvl["n"])


def median(xs) -> float:
    s = sorted(xs)
    return s[len(s) // 2]
