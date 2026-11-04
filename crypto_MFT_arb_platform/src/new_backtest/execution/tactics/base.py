"""Tactic base: a small state machine that turns one intent into child orders through the engine's sender.

Phase 0: the price rule is the strategy's (the leg arrives priced); the tactic validates it against the intent's limits.
A tactic is the ONLY way an order is sent (ExecutionLayer.send)."""
from __future__ import annotations

from ..intent import ExecutionIntent, IntentError, Role


class Tactic:
    name = ""
    roles: tuple[str, ...] = ()
    passive = False                 # True: the child rests (PostOnly); False: it crosses (IOC-like)
    implemented = True
    escalates_to = ""

    def __init__(self, cfg) -> None:
        self.cfg = cfg              # ExecutionConfig

    def check_child(self, intent: ExecutionIntent, leg) -> None:
        """Risk checks that hold for every tactic; subclasses add their own and call super()."""
        if intent.role not in self.roles:
            raise IntentError(f"tactic {self.name} cannot work a {intent.role} intent")
        if leg.is_taker == self.passive:
            raise IntentError(f"tactic {self.name} {'rests' if self.passive else 'crosses'}; the child is "
                              f"{'a taker' if leg.is_taker else 'passive'}")
        if leg.side != intent.side:
            raise IntentError(f"child side {leg.side} differs from intent side {intent.side}")
        if leg.venue not in intent.venues:
            raise IntentError(f"child venue {leg.venue} not eligible for intent {intent.intent_id} ({intent.venues})")
        if leg.qty > intent.qty + 1e-9:
            raise IntentError(f"child qty {leg.qty} exceeds intent qty {intent.qty}")
        self.check_cap(intent, leg)

    def check_cap(self, intent: ExecutionIntent, leg) -> None:
        w = intent.worst_price
        if w is None:
            if self.cfg.price_cap_required and intent.role not in (Role.UNWIND, Role.FLATTEN):
                raise IntentError(f"intent {intent.intent_id} has no worst_price (price cap is mandatory)")
            return
        eps = 1e-9 * max(1.0, abs(w))
        if intent.side == "buy" and leg.limit_price > w + eps:
            raise IntentError(f"child limit {leg.limit_price} is above the intent's worst price {w}")
        if intent.side == "sell" and leg.limit_price < w - eps:
            raise IntentError(f"child limit {leg.limit_price} is below the intent's worst price {w}")
