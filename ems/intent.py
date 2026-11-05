"""Execution intent: what the strategy wants done, never how (canonical execution layer, phase 0).

The strategy hands the layer one ExecutionIntent per thing it wants; the layer's tactic builds the child orders. Intents
carry no randomness and touch no engine counter, so creating and journaling them cannot change a replay.

Intent states mirror the parent level of the shared order vocabulary (order_state_machine.py):
    accepted -> working -> partially_done -> done        (the normal path)
    working / partially_done -> escalated                (a fallback tactic took over)
    working -> failed | unknown                          (the engine gave up / cannot tell)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Role:
    PRIMARY = "primary"
    HEDGE = "hedge"          # hedge_of(parent intent id) in the journal
    UNWIND = "unwind"
    REBALANCE = "rebalance"
    FLATTEN = "flatten"
    ALL = (PRIMARY, HEDGE, UNWIND, REBALANCE, FLATTEN)


class IntentState(str, Enum):
    ACCEPTED = "accepted"
    WORKING = "working"
    PARTIALLY_DONE = "partially_done"
    DONE = "done"
    ESCALATED = "escalated"
    FAILED = "failed"
    UNKNOWN = "unknown"


TERMINAL = frozenset({IntentState.DONE, IntentState.FAILED})

_S = IntentState
# The one transition table (docs/strategy_contract.md 4h.1). `move` raises on anything else; nothing writes `state` directly.
TRANSITIONS: dict[IntentState, frozenset] = {
    _S.ACCEPTED: frozenset({_S.WORKING, _S.FAILED}),
    _S.WORKING: frozenset({_S.WORKING, _S.PARTIALLY_DONE, _S.DONE, _S.ESCALATED, _S.FAILED, _S.UNKNOWN}),
    _S.PARTIALLY_DONE: frozenset({_S.WORKING, _S.PARTIALLY_DONE, _S.DONE, _S.ESCALATED, _S.FAILED, _S.UNKNOWN}),
    _S.ESCALATED: frozenset({_S.PARTIALLY_DONE, _S.ESCALATED, _S.DONE, _S.FAILED, _S.UNKNOWN}),
    _S.UNKNOWN: frozenset({_S.WORKING, _S.PARTIALLY_DONE, _S.DONE, _S.ESCALATED, _S.FAILED}),
    _S.DONE: frozenset(),
    _S.FAILED: frozenset(),
}

# urgency maps to the paper's urgency parameter (phase 1 turns it into a number)
URGENCY = ("patient", "normal", "urgent", "emergency")


class IntentError(ValueError):
    """The layer refused an intent or a child order (a risk rule, not an engine error)."""


class RiskRefused(IntentError):
    """The atomic risk check refused a primary intent: nothing was created, journaled or reserved."""


@dataclass
class ExecutionIntent:
    intent_id: str                        # stable and deterministic: <prefix>-<serial>-<suffix>; journaled before any send
    role: str                             # Role.*
    instrument: str
    side: str                             # "buy" | "sell"
    qty: float                            # base quantity
    venues: tuple[str, ...]               # eligible venues, in preference order
    worst_price: float | None             # the price beyond which the intent is not worth doing; mandatory except flatten/unwind
    urgency: str = "normal"
    deadline_ts: float | None = None
    tactic_hint: str = ""
    reason: str = ""
    parent_id: str = ""                   # hedge_of(parent_id)
    parent_filled_qty: float = 0.0        # a hedge exists only for a quantity already filled on the parent
    signal_id: int = 0
    created_ts: float = 0.0
    state: IntentState = IntentState.ACCEPTED
    children: list[int] = field(default_factory=list)     # order ids sent for this intent, in order
    meta: dict = field(default_factory=dict)              # tactic bookkeeping (break-even price, attempts budget); not part of the journal record
    filled_qty: float = 0.0

    def move(self, to: IntentState) -> None:
        """The only way a state changes. An illegal transition is a bug in the caller, never silently absorbed."""
        if to not in TRANSITIONS[self.state]:
            raise IntentError(f"intent {self.intent_id}: illegal transition {self.state.value} -> {to.value}")
        self.state = to

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    def __post_init__(self) -> None:
        if self.role not in Role.ALL:
            raise IntentError(f"unknown intent role {self.role!r}")
        if self.side not in ("buy", "sell"):
            raise IntentError(f"intent side must be buy|sell, not {self.side!r}")
        if self.urgency not in URGENCY:
            raise IntentError(f"unknown urgency {self.urgency!r}")
        if not self.qty > 0:
            raise IntentError("intent qty must be positive")

    def as_record(self) -> dict:
        return {"intent_id": self.intent_id, "role": self.role, "instrument": self.instrument, "side": self.side, "qty": self.qty,
                "venues": list(self.venues), "worst_price": self.worst_price, "urgency": self.urgency, "deadline_ts": self.deadline_ts,
                "tactic_hint": self.tactic_hint, "reason": self.reason, "parent_id": self.parent_id,
                "parent_filled_qty": self.parent_filled_qty, "signal_id": self.signal_id, "created_ts": self.created_ts}
