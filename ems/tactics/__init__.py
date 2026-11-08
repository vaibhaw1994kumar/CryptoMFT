"""Tactic registry. Three tactics, no other way to send an order:
    PASSIVE        PostOnly, placement in {touch, improve, deep}
    IOC_CAPPED     the hedge, and taker_taker legs
    MARKET_BANDED  breaker flatten and unwinds only
There is no speculative-IOC or scheduled tactic."""
from __future__ import annotations

from .base import MARKET_BANDED_REASONS, TACTIC_CHOICE_REASONS, MarketState, Tactic, TacticChoice, TacticChoiceConfig, choose_tactic
from .ioc import IocCapped
from .market_banded import MarketBanded
from .passive import PLACEMENTS, Passive, placement_price

REGISTRY: dict[str, type[Tactic]] = {c.name: c for c in (Passive, IocCapped, MarketBanded)}


def get_tactic(name: str, cfg) -> Tactic:
    try:
        return REGISTRY[name](cfg)
    except KeyError:
        raise KeyError(f"unknown tactic {name!r}; known: {sorted(REGISTRY)}") from None
