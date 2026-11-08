"""The profile's `execution:` block (canonical execution layer). Phase 0 values reproduce today's behaviour exactly."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

PAIRINGS = ("maker_taker", "maker_maker", "taker_taker")
ONE_HEDGE_CHILD = ("off", "count", "enforce")
RISK_RESERVATION = ("off", "count", "enforce")
PLACEMENTS = ("touch", "improve", "deep")
PRICE_RULES = ("conditional_edge", "naive_edge")   # execution.primary.price_rule: conditional_edge (every perp profile) is the edge the signal was gated on; naive_edge is legacy_v0's, kept for that reference only
# execution.hedge.cap: touch_slack = today's rule, re-taken from the touch on every attempt; conditional_edge_breakeven = the capped_ioc_dp
# policy's cap; breakeven_plus_buffer (phase 1-lite) = ONE fixed limit for the whole hedge chain: the price at which the trade nets zero after
# maker + taker fees, moved cap_buffer_bps against us; every retry re-sends that same limit (no chasing).
# the only tactics: PASSIVE (placement touch|improve|deep), IOC_CAPPED (hedge, taker_taker), MARKET_BANDED (breaker flatten, unwinds)
SECTION_TACTIC = {"primary": "PASSIVE", "hedge": "IOC_CAPPED", "unwind": "MARKET_BANDED"}
_RENAMED = {"PASSIVE_TOUCH": "PASSIVE with placement: touch", "PASSIVE_IMPROVE": "PASSIVE with placement: improve",
            "PASSIVE_DEEP": "PASSIVE with placement: deep"}

_SECTION_KEYS = {
    "primary": {"tactic", "placement", "deep_ticks", "max_age_s", "reprice", "price_rule"},
    "hedge": {"tactic", "cap", "cap_buffer_bps", "deadline_s", "attempts", "urgency_phi", "flicker_model", "slack_bps_floor",
              "policy", "price_model", "flicker_set", "v0_bps", "max_attempts_cap", "min_p_at_cap", "policy_venues"},
    "maker_maker": set(),        # hedge.q, maker_maker.escalate_after_s, unwind.window_s and unwind.fallback were declared here and read by nothing; no profile sets them: removed (Phase C, 8 Nov)
    "unwind": {"tactic", "child", "band_bps"},
}


class ExecutionConfigError(ValueError):
    pass


@dataclass
class ExecutionConfig:
    pairing: str = "maker_taker"
    primary: dict = field(default_factory=lambda: {"tactic": "PASSIVE", "placement": "deep", "price_rule": "conditional_edge", "reprice": "none"})
    # cap: touch_slack = today's rule (touch +/- max(max_walk_bps, slack_bps_floor) bps); phase 1 adds conditional_edge_breakeven
    hedge: dict = field(default_factory=lambda: {"tactic": "IOC_CAPPED", "cap": "touch_slack", "slack_bps_floor": 5.0})
    maker_maker: dict = field(default_factory=dict)
    unwind: dict = field(default_factory=lambda: {"tactic": "MARKET_BANDED"})
    # MANDATORY in a perp profile (profiles.SWITCHES): False / "off" is refused at startup there
    price_cap_required: bool = True
    one_hedge_child: str = "count"
    risk_reservation: str = "count"             # MANDATORY in perp: atomic check-and-reserve of primary exposure (4h.2)

    def __post_init__(self) -> None:
        if self.pairing not in PAIRINGS:
            raise ExecutionConfigError(f"execution.pairing must be one of {PAIRINGS}, not {self.pairing!r}")
        if self.one_hedge_child not in ONE_HEDGE_CHILD:
            raise ExecutionConfigError(f"execution.one_hedge_child must be one of {ONE_HEDGE_CHILD}")
        if self.risk_reservation not in RISK_RESERVATION:
            raise ExecutionConfigError(f"execution.risk_reservation must be one of {RISK_RESERVATION}")
        for name, want in SECTION_TACTIC.items():
            t = getattr(self, name).get("tactic", want)
            if t in _RENAMED:
                raise ExecutionConfigError(f"execution.{name}.tactic {t!r} no longer exists: use {_RENAMED[t]}")
            if t != want:
                raise ExecutionConfigError(f"execution.{name}.tactic must be {want!r} (the only tactics are PASSIVE, IOC_CAPPED, MARKET_BANDED), not {t!r}")
        pl = self.primary.get("placement", "deep")
        if pl not in PLACEMENTS:
            raise ExecutionConfigError(f"execution.primary.placement must be one of {PLACEMENTS}, not {pl!r}")
        pr = self.primary.get("price_rule", "conditional_edge")
        if pr not in PRICE_RULES:
            raise ExecutionConfigError(f"execution.primary.price_rule must be one of {PRICE_RULES}, not {pr!r}")
        dt = self.primary.get("deep_ticks")
        if dt is not None and (pl != "deep" or not isinstance(dt, int) or isinstance(dt, bool) or dt < 1):
            raise ExecutionConfigError("execution.primary.deep_ticks is a positive integer and only valid with placement: deep")
        for name, keys in _SECTION_KEYS.items():
            bad = sorted(set(getattr(self, name)) - keys)
            if bad:
                raise ExecutionConfigError(f"execution.{name}: unknown key(s) {bad}")
        if self.hedge.get("cap") == "breakeven_plus_buffer":
            if self.hedge.get("policy") is not None:
                raise ExecutionConfigError("execution.hedge.cap breakeven_plus_buffer is the fixed limit (phase 1-lite): it takes no policy")
            b = self.hedge.get("cap_buffer_bps")
            if isinstance(b, bool) or not isinstance(b, (int, float)) or b < 0:
                raise ExecutionConfigError("execution.hedge.cap breakeven_plus_buffer needs cap_buffer_bps >= 0 (bps given up against the break-even price)")

    def as_dict(self) -> dict:
        return {"pairing": self.pairing, "primary": dict(self.primary), "hedge": dict(self.hedge), "maker_maker": dict(self.maker_maker),
                "unwind": dict(self.unwind), "price_cap_required": self.price_cap_required, "one_hedge_child": self.one_hedge_child,
                "risk_reservation": self.risk_reservation}

    def sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.as_dict(), sort_keys=True, default=str).encode()).hexdigest()
