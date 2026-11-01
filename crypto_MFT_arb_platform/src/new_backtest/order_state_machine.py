"""One definition of the order lifecycle: states, allowed transitions, and what a fill report does.

This module is the written-down specification the core OMS (src/crypto_stat_arb/oms.py) and the backtester's maker
lifecycle (fill_engine.MakerExecutor + CrossVenueArbBacktester) are compared against in tests/test_order_state_parity.py.
Neither implementation imports it yet: a refactor of the OMS reducer is out of scope here, so the specification is exercised
by feeding identical event scripts to all three and asserting identical end states, booked quantities and fill sequences.
Where an implementation differs the difference is listed there as a numbered disagreement.

States
    SUBMITTED         sent, nothing heard back
    ACKNOWLEDGED      exchange confirmed receipt (the instant of confirmation)
    RESTING           on the book, no fill yet
    PARTIALLY_FILLED  0 < cumulative filled < size
    FILLED            cumulative filled == size                         terminal
    REJECTED          exchange refused it; nothing filled                terminal
    CANCEL_REQUESTED  a cancel was sent and is not confirmed; the order is still live and can fill
    CANCELLED         cancel confirmed (possibly with a partial fill)    terminal
    UNKNOWN           no answer within the deadline; resolved by a later update or a reconciliation query

Rules
    R1  Fills are cumulative. The booked quantity is the largest cumulative quantity ever reported; an increment is booked
        once, whatever the arrival order, duplication or source of the reports.
    R2  A fill report is always kept, in any state, including after CANCELLED and after REJECTED (late fill: KEEP, book once).
        The terminal state is not replaced by it, except that cumulative == size makes the order FILLED.
    R3  The end state does not depend on the order in which the same facts arrive (`resolve`).
    R4  UNKNOWN is entered by TIMEOUT from SUBMITTED only (no answer to the submit; an acknowledged order that is simply quiet is
        RESTING, not unknown) and is left by any ACK, fill, REJECT, CANCEL_CONFIRM or
        RECONCILE. A TIMEOUT never overrides evidence already held.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Iterable

EPS = 1e-12


class State(StrEnum):
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    RESTING = "resting"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


TERMINAL = frozenset({State.FILLED, State.REJECTED, State.CANCELLED})

ALLOWED_TRANSITIONS: dict[State, frozenset[State]] = {
    State.SUBMITTED: frozenset({State.ACKNOWLEDGED, State.RESTING, State.REJECTED, State.UNKNOWN,
                                State.PARTIALLY_FILLED, State.FILLED}),       # a fill report may overtake the ack
    State.ACKNOWLEDGED: frozenset({State.RESTING, State.PARTIALLY_FILLED, State.FILLED, State.CANCEL_REQUESTED}),
    State.RESTING: frozenset({State.PARTIALLY_FILLED, State.FILLED, State.CANCEL_REQUESTED}),
    State.PARTIALLY_FILLED: frozenset({State.PARTIALLY_FILLED, State.FILLED, State.CANCEL_REQUESTED}),
    State.CANCEL_REQUESTED: frozenset({State.CANCELLED, State.FILLED, State.PARTIALLY_FILLED}),   # late fill: keep and book once
    State.UNKNOWN: frozenset({State.ACKNOWLEDGED, State.RESTING, State.PARTIALLY_FILLED, State.FILLED, State.REJECTED,
                              State.CANCEL_REQUESTED, State.CANCELLED}),
    State.FILLED: frozenset(),
    State.REJECTED: frozenset(),
    State.CANCELLED: frozenset({State.FILLED}),       # a late fill report completing a cancelled order
}


class Kind(StrEnum):
    SUBMIT = "submit"
    ACK = "ack"
    REJECT = "reject"
    FILL = "fill"                      # cum = cumulative filled fraction of the order size
    CANCEL_REQUEST = "cancel_request"
    CANCEL_CONFIRM = "cancel_confirm"
    TIMEOUT = "timeout"
    RECONCILE = "reconcile"            # a venue query answered: cum and `terminal` describe what the venue holds


CUM_KINDS = (Kind.FILL, Kind.RECONCILE, Kind.CANCEL_CONFIRM)     # events that carry a cumulative filled quantity


@dataclass(frozen=True)
class Event:
    kind: Kind
    ts: float                          # when it happened at the exchange (or was decided locally for TIMEOUT / CANCEL_REQUEST)
    arrive: float                      # when the strategy learned of it; arrival order is the sequence order
    cum: float = 0.0                   # FILL / RECONCILE / CANCEL_CONFIRM: cumulative filled, as a fraction of the order size
    terminal: str = ""                 # RECONCILE: "" | "filled" | "cancelled" | "rejected" | "resting"
    source: str = "modelled"           # "historical" | "modelled" | "injected" (Task 4 vocabulary)


@dataclass
class Result:
    state: State
    booked: float                      # largest cumulative fraction reported (R1)
    fills: list[float] = field(default_factory=list)       # increments, in booking order
    violations: list[str] = field(default_factory=list)    # transitions the table does not allow, seen on the way


def resolve(events: Iterable[Event]) -> State:
    """End state from the facts alone (R3): independent of arrival order."""
    ev = list(events)
    cum = max([e.cum for e in ev if e.kind in CUM_KINDS] + [0.0])
    kinds = {e.kind for e in ev}
    rec_terminal = {e.terminal for e in ev if e.kind == Kind.RECONCILE and e.terminal}
    if cum >= 1.0 - EPS or "filled" in rec_terminal:
        return State.FILLED
    if Kind.CANCEL_CONFIRM in kinds or "cancelled" in rec_terminal:
        return State.CANCELLED
    if Kind.REJECT in kinds or "rejected" in rec_terminal:
        return State.REJECTED if cum <= EPS else State.PARTIALLY_FILLED     # a reject cannot coexist with a fill: the fill wins
    if cum > EPS:
        return State.CANCEL_REQUESTED if Kind.CANCEL_REQUEST in kinds else State.PARTIALLY_FILLED
    if Kind.CANCEL_REQUEST in kinds:
        return State.CANCEL_REQUESTED
    if Kind.ACK in kinds or "resting" in rec_terminal:
        return State.RESTING
    if Kind.TIMEOUT in kinds:
        return State.UNKNOWN
    return State.SUBMITTED


def replay(events: Iterable[Event]) -> Result:
    """Process the events in the order given (arrival order), booking fills and recording illegal steps."""
    ev = list(events)
    booked = 0.0
    fills: list[float] = []
    violations: list[str] = []
    state = State.SUBMITTED
    seen: list[Event] = []
    for e in ev:
        seen.append(e)
        if e.kind in CUM_KINDS and e.cum > booked + EPS:
            fills.append(round(e.cum - booked, 12))
            booked = e.cum
        nxt = resolve(seen)
        if nxt != state and nxt not in ALLOWED_TRANSITIONS[state]:
            violations.append(f"{state.value}->{nxt.value} on {e.kind.value}")
        state = nxt
    return Result(state=state, booked=booked, fills=fills, violations=violations)
