from __future__ import annotations

from ..intent import IntentError, Role
from .base import Tactic

PLACEMENTS = ("touch", "improve", "deep")


def placement_price(placement: str, side: str, best_bid: float, best_ask: float, tick: float, edge_price: float,
                    deep_ticks: int | None = None) -> float:
    """Where a PostOnly order rests. `edge_price` is the price the conditional edge requires: a buy never rests above it, a sell
    never below it, so every placement keeps at least the edge the signal was gated on.
      touch   the best bid (buy) / best ask (sell)
      improve one tick inside the spread when the spread is wider than one tick, else the touch
      deep    the edge-required price; with `deep_ticks`, that many ticks behind the touch instead
    A price that would cross the book (>= best ask for a buy, <= best bid for a sell) falls back to the touch."""
    if placement not in PLACEMENTS:
        raise IntentError(f"unknown placement {placement!r}; known: {PLACEMENTS}")
    buy = side == "buy"
    touch = best_bid if buy else best_ask
    if placement == "touch":
        px = touch
    elif placement == "improve":
        wide = (best_ask - best_bid) > tick * (1.0 + 1e-9)
        px = (best_bid + tick if buy else best_ask - tick) if wide and tick > 0 else touch
    elif deep_ticks:
        px = best_bid - deep_ticks * tick if buy else best_ask + deep_ticks * tick
    else:
        px = edge_price
    px = min(px, edge_price) if buy else max(px, edge_price)
    if (buy and px >= best_ask) or (not buy and px <= best_bid):
        px = touch
    return px


class Passive(Tactic):
    """PostOnly limit at one of three placements (touch, improve, deep) — one tactic, one order type, one fill model, one lifetime and
    cancel/replace rule. Today both profiles use `deep`: the strategy rests at the price the edge requires. The intent's worst_price
    is that edge price, so a touch / improve child sits inside it."""
    name = "PASSIVE"
    roles = (Role.PRIMARY,)
    passive = True
    escalates_to = "IOC_CAPPED"

    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        self.placement = cfg.primary.get("placement", "deep")
        self.deep_ticks = cfg.primary.get("deep_ticks")
        if self.placement not in PLACEMENTS:
            raise IntentError(f"unknown placement {self.placement!r}; known: {PLACEMENTS}")

    @property
    def reprices(self) -> bool:
        """False for plain `deep`: the strategy's own price is sent unchanged (bit-identical to the pre-layer engine)."""
        return self.placement != "deep" or bool(self.deep_ticks)

    def price(self, side: str, best_bid: float, best_ask: float, tick: float, edge_price: float) -> float:
        return placement_price(self.placement, side, best_bid, best_ask, tick, edge_price, self.deep_ticks)
