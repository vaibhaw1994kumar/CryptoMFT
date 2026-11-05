"""Latency-aware capped IOC: flicker law and the attempt DP (Cartea & Sanchez-Betancourt 2022, marketable limit orders).

Units (stated once, used everywhere): prices are bps of the FUNDAMENTAL S (S = 0); a value V is bps per unit of quantity; phi is
bps per (base_unit^2 * second); q is base units; dt is seconds.

Flicker law, in FAVOURABLE bps (for a sell the bid moving up, for a buy the ask moving down):
    F = 0 w.p. p0;   F = +Exp(eta_plus) w.p. p_plus;   F = -Exp(eta_minus) w.p. p_minus      (p0 + p_plus + p_minus = 1)
A hedge limit placed d bps THROUGH the fundamental (d >= 0: a sell limit d below S) fills iff F >= -d:
    P(fill | d >= 0) = p0 + p_plus + p_minus (1 - exp(-eta_minus d))
a speculative limit |d| bps beyond the fundamental in our favour (d < 0) fills iff F >= |d|:
    P(fill | d <  0) = p_plus exp(-eta_plus |d|)
Decision rule (a small DP over the attempts left k, not the paper's PDE):
    V_0 = v0  (the banded-unwind value, negative)
    V_k = max_d [ P(d) (price(d) - c) + (1 - P(d)) (V_{k-1} - phi q^2 dt) ]        d in [d_min, d_cap]
price(d) = -d ("limit": value at the limit, the brief's formula, conservative) or E[F | fill, d] ("arrival": value at the
arrival-time touch). c = taker fee in bps. The chosen offset is clipped so the limit never passes d_cap, the distance from the
fundamental to worst_price. Nothing here draws a random number.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class FlickerLaw:
    p0: float
    p_plus: float
    p_minus: float
    eta_plus: float          # 1 / mean positive flicker, in 1/bps
    eta_minus: float

    def __post_init__(self) -> None:
        if min(self.p0, self.p_plus, self.p_minus) < -1e-12 or abs(self.p0 + self.p_plus + self.p_minus - 1.0) > 1e-9:
            raise ValueError("flicker probabilities must be non-negative and sum to 1")
        if self.eta_plus <= 0 or self.eta_minus <= 0:
            raise ValueError("flicker rates must be positive")

    def p_fill(self, d):
        """P(fill) for a limit d bps through (d >= 0) or beyond (d < 0) the fundamental. Vectorised over d."""
        d = np.asarray(d, dtype=float)
        through = self.p0 + self.p_plus + self.p_minus * (1.0 - np.exp(-self.eta_minus * np.maximum(d, 0.0)))
        spec = self.p_plus * np.exp(-self.eta_plus * np.maximum(-d, 0.0))
        return np.where(d >= 0.0, through, spec)

    def e_flicker_and_fill(self, d):
        """E[F * 1{fill}] (bps): the numerator of E[F | fill, d]. Closed form of the integral of z over the fill region."""
        d = np.asarray(d, dtype=float)
        ep, em = self.eta_plus, self.eta_minus
        dpos = np.maximum(d, 0.0)
        through = self.p_plus / ep + self.p_minus * (dpos * np.exp(-em * dpos) - (1.0 - np.exp(-em * dpos)) / em)
        a = np.maximum(-d, 0.0)
        spec = self.p_plus * np.exp(-ep * a) * (a + 1.0 / ep)
        return np.where(d >= 0.0, through, spec)

    def as_dict(self) -> dict:
        return {"p0": self.p0, "p_plus": self.p_plus, "p_minus": self.p_minus, "eta_plus": self.eta_plus, "eta_minus": self.eta_minus}


@dataclass(frozen=True)
class PolicyParams:
    fee_bps: float               # c: taker fee of the hedge venue
    v0_bps: float                # V_0: value when the attempts are exhausted and the banded unwind takes over (negative)
    phi: float = 0.0             # urgency penalty, bps / (base^2 * s)
    q: float = 1.0               # unhedged quantity, base units
    dt_s: float = 0.3            # one attempt cycle: entry + report (+ retry backoff), seconds
    price_model: str = "limit"   # "limit" (the brief's S - d - c) | "arrival" (E[S + F | fill] - c)
    d_min_bps: float = -10.0     # most speculative limit considered
    d_step_bps: float = 0.05

    def __post_init__(self) -> None:
        if self.price_model not in ("limit", "arrival"):
            raise ValueError("price_model must be 'limit' or 'arrival'")


@dataclass(frozen=True)
class Choice:
    offset_bps: float            # d: bps through (>0) or beyond (<0) the fundamental
    p_fill: float
    value_bps: float
    clipped: bool                # the unconstrained optimum was beyond the cap


def _grid(params: PolicyParams, d_cap: float):
    n = int(math.floor((d_cap - params.d_min_bps) / params.d_step_bps + 1e-9)) + 1
    g = params.d_min_bps + params.d_step_bps * np.arange(max(n, 1))
    g = g[g <= d_cap + 1e-12]
    if g.size == 0 or abs(g[-1] - d_cap) > 1e-9:
        g = np.append(g, d_cap)               # the cap itself is always a candidate
    return g


def solve(law: FlickerLaw, params: PolicyParams, d_cap_bps: float, attempts: int) -> list[Choice]:
    """Choices for k = 1..attempts attempts left (index k-1). `d_cap_bps` may be negative (the cap is better than the fundamental)."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    d = _grid(params, d_cap_bps)
    p = law.p_fill(d)
    if params.price_model == "limit":
        gain = -d - params.fee_bps
        ef = None
    else:
        ef = law.e_flicker_and_fill(d)
    wait = params.phi * params.q ** 2 * params.dt_s
    v_prev = params.v0_bps
    out: list[Choice] = []
    for _ in range(attempts):
        cont = v_prev - wait
        if ef is None:
            val = p * gain + (1.0 - p) * cont
        else:
            val = ef - p * params.fee_bps + (1.0 - p) * cont                 # E[(F - c) 1{fill}] + (1 - P) cont
        i = int(np.argmax(val))
        # ties: prefer the higher fill probability (the later index of equal values)
        best = val[i]
        j = np.flatnonzero(val >= best - 1e-12)
        i = int(j[np.argmax(p[j])])
        out.append(Choice(float(d[i]), float(p[i]), float(val[i]), clipped=bool(abs(d[i] - d_cap_bps) < 1e-9 and i == len(d) - 1)))
        v_prev = float(val[i])
    return out


@lru_cache(maxsize=4096)
def _solve_cached(law: FlickerLaw, params: PolicyParams, d_cap_q: float, attempts: int):
    return tuple(solve(law, params, d_cap_q, attempts))


def choose(law: FlickerLaw, params: PolicyParams, d_cap_bps: float, attempts_left: int) -> Choice:
    """The offset for the attempt with `attempts_left` attempts remaining (this one included). The cap is quantised to 0.05 bps for
    the table cache (the clipped limit is then recomputed from the exact cap by the caller)."""
    cap_q = round(d_cap_bps / params.d_step_bps) * params.d_step_bps
    return _solve_cached(law, params, float(cap_q), int(attempts_left))[attempts_left - 1]


def limit_price(side: str, fundamental: float, offset_bps: float, worst_price: float) -> float:
    """Price for an offset: a sell limit sits `offset_bps` below the fundamental, a buy limit above; never past worst_price."""
    if side == "sell":
        return max(fundamental * (1.0 - offset_bps / 1e4), worst_price)
    return min(fundamental * (1.0 + offset_bps / 1e4), worst_price)


def attempts_from_latency(deadline_s: float, cycle_s: float, cap: int = 8) -> int:
    """Attempts that fit before the deadline: floor(deadline / cycle), at least 1, at most `cap`."""
    return max(1, min(int(deadline_s // cycle_s) if cycle_s > 0 else 1, cap))


def load_flicker_law(path: str | Path, *, side: str, hour: int | None = None, vol_bps: float | None = None, set_name: str | None = None,
                     vol_tercile: int | None = None, hour_bucket_h: int = 8) -> tuple[FlickerLaw, dict]:
    """Law for (side, hour bucket, vol tercile) from config/flicker_binance_perp.json, falling back cell -> side x tercile -> side ->
    pooled. `set_name` picks the training set (a file has one per test day); a plain file with a top-level `cells` is also read.
    The tercile comes from `vol_tercile` or from `vol_bps` and the set's stored edges. Returns (law, {'level','n','key','set'})."""
    doc = json.loads(Path(path).read_text())
    body = doc["sets"][set_name] if "sets" in doc else doc
    cells = body["cells"]
    if vol_tercile is None and vol_bps is not None:
        e = body.get("vol_edges_bps") or [0.0, 0.0]
        vol_tercile = int(vol_bps > e[0]) + int(vol_bps > e[1])
    bucket = None if hour is None else int(hour) // hour_bucket_h
    for level, key in (("cell", f"{side}|b{bucket}|t{vol_tercile}"), ("side_tercile", f"{side}|t{vol_tercile}"), ("side", f"{side}"), ("pooled", "pooled")):
        c = cells.get(key)
        if c is not None and c.get("fit") is not None:
            return FlickerLaw(**c["fit"]), {"level": level, "n": c["n"], "key": key, "set": set_name}
    raise KeyError(f"no flicker cell for side={side} hour={hour} tercile={vol_tercile} in {path}")
