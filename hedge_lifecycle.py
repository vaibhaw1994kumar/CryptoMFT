"""The hedge lifecycle: one state machine for the hedge of a maker fill, in backtest, paper and live (Phase C, 8 Nov 2026).

A maker fill opens exposure; the hedge exists to close it. The states and the one transition table below are the parent-level vocabulary: `move` raises on any other
transition, nothing writes `state` directly, and a retry or a replacement needs the evidence the rule asks for (a classified reject that permits it, a venue query that did not
find the order). The older vocabularies map onto it (PLAN_TO_HEDGE, INTENT_TO_HEDGE); the mapping is pinned by tests/test_hedge_lifecycle.py.

    MAKER_FILLED -> HEDGE_PENDING -> SUBMITTED -> PARTIALLY_FILLED / HEDGED -> RECONCILED          (the normal path)
    SUBMITTED / RETRYING -> REJECTED -> RETRYING (only if the reject code permits it and the budget is left) | UNWINDING
    SUBMITTED / RETRYING -> UNKNOWN (no answer: query before anything is resent; an unacknowledged order is never assumed failed)
    UNKNOWN -> HEDGED | PARTIALLY_FILLED | SUBMITTED (the venue has it) | RETRYING / REJECTED (the venue does not know it) | UNWINDING | FAILED (queries exhausted)
    any exposed state -> ESCALATED (a limit is breached) -> UNWINDING -> RECONCILED | FAILED (unwind budget exhausted: the persistent alarm)

Rules live here; injected failures (a delayed or lost message, an outage) stay in simulation/ and only produce the events this machine reacts to.
It records, per event, what the specification asks for: retry intervals, reject labels, timeouts, partial fills, the recovery outcome and the residual-exposure duration.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .models import PlanState


class HedgeState(StrEnum):
    MAKER_FILLED = "MAKER_FILLED"            # exposure opened by a maker fill; no hedge yet
    HEDGE_PENDING = "HEDGE_PENDING"          # hedge child created and reserved for the residual, not sent
    SUBMITTED = "SUBMITTED"                  # sent (or re-sent), nothing final heard
    PARTIALLY_FILLED = "PARTIALLY_FILLED"    # part of the hedge executed; a residual remains
    HEDGED = "HEDGED"                        # the whole exposure is hedged
    REJECTED = "REJECTED"                    # the venue refused the hedge (classified by the venue code table)
    RETRYING = "RETRYING"                    # a retry is scheduled (reject permits it, or the query found nothing)
    UNKNOWN = "UNKNOWN"                      # no answer: the venue is queried before anything is resent
    UNWINDING = "UNWINDING"                  # the unhedged residual is being unwound
    ESCALATED = "ESCALATED"                  # a limit (residual size or age, unknown too long) is breached: deterministic unwind
    RECONCILED = "RECONCILED"                # positions and outstanding orders agree with the venue; recovery complete
    FAILED = "FAILED"                        # unwind budget exhausted or queries exhausted: persistent alarm, no new risk until reset


_H = HedgeState
TRANSITIONS: dict[HedgeState, frozenset[HedgeState]] = {
    _H.MAKER_FILLED: frozenset({_H.HEDGE_PENDING, _H.ESCALATED}),
    _H.HEDGE_PENDING: frozenset({_H.SUBMITTED, _H.REJECTED, _H.ESCALATED}),
    _H.SUBMITTED: frozenset({_H.PARTIALLY_FILLED, _H.HEDGED, _H.REJECTED, _H.UNKNOWN, _H.ESCALATED}),
    _H.PARTIALLY_FILLED: frozenset({_H.PARTIALLY_FILLED, _H.HEDGED, _H.RETRYING, _H.UNWINDING, _H.UNKNOWN, _H.ESCALATED}),
    _H.REJECTED: frozenset({_H.RETRYING, _H.UNWINDING, _H.ESCALATED}),
    _H.RETRYING: frozenset({_H.SUBMITTED, _H.UNKNOWN, _H.UNWINDING, _H.ESCALATED}),
    _H.UNKNOWN: frozenset({_H.SUBMITTED, _H.PARTIALLY_FILLED, _H.HEDGED, _H.REJECTED, _H.RETRYING, _H.UNWINDING, _H.ESCALATED, _H.FAILED}),
    _H.UNWINDING: frozenset({_H.UNWINDING, _H.UNKNOWN, _H.ESCALATED, _H.RECONCILED, _H.FAILED}),
    _H.ESCALATED: frozenset({_H.UNWINDING, _H.UNKNOWN, _H.FAILED}),
    _H.HEDGED: frozenset({_H.RECONCILED}),
    _H.RECONCILED: frozenset(),
    _H.FAILED: frozenset({_H.RECONCILED}),                # only by an operator reset after reconciliation (OMS.reset_unwind_exhausted_alarm)
}
TERMINAL = frozenset({_H.RECONCILED})
EXPOSED = frozenset({_H.MAKER_FILLED, _H.HEDGE_PENDING, _H.SUBMITTED, _H.PARTIALLY_FILLED, _H.REJECTED, _H.RETRYING, _H.UNKNOWN, _H.UNWINDING, _H.ESCALATED, _H.FAILED})

# The older parent vocabularies, projected onto this one. A key missing from a map has no hedge meaning (the plan or intent is not at a hedge stage yet).
PLAN_TO_HEDGE: dict[PlanState, HedgeState] = {
    PlanState.ACKNOWLEDGED: HedgeState.SUBMITTED,
    PlanState.PARTIAL: HedgeState.PARTIALLY_FILLED,
    PlanState.FILLED: HedgeState.HEDGED,
    PlanState.REJECTED: HedgeState.REJECTED,
    PlanState.TIMED_OUT: HedgeState.UNKNOWN,
    PlanState.HEDGE_UNWIND: HedgeState.UNWINDING,
    PlanState.RECONCILED: HedgeState.RECONCILED,
    PlanState.CLOSED: HedgeState.RECONCILED,
}
INTENT_TO_HEDGE: dict[str, tuple[HedgeState, ...]] = {   # ems.intent.IntentState values (strings: the core does not import the EMS package). Coarser than this machine, so a tuple.
    "accepted": (HedgeState.HEDGE_PENDING,),
    "working": (HedgeState.SUBMITTED, HedgeState.RETRYING),
    "partially_done": (HedgeState.PARTIALLY_FILLED,),
    "done": (HedgeState.HEDGED, HedgeState.RECONCILED),
    "escalated": (HedgeState.ESCALATED, HedgeState.UNWINDING),
    "failed": (HedgeState.REJECTED, HedgeState.FAILED),
    "unknown": (HedgeState.UNKNOWN,),
}


def touch_slack_price(side: str, touch, slack_bps):
    """The touch_slack cap: the hedge-venue touch moved against us by `slack_bps` (a buy may pay up to it, a sell may receive down to it). The one formula (EMS layer and the OMS hedge dispatch)."""
    slack = touch * slack_bps / 10_000
    return touch + slack if side.lower() == "buy" else touch - slack


@dataclass(frozen=True)
class HedgePolicy:
    """The hedge rules the OMS applies under FILL_TRIGGERED. No production value is invented: retrying is OFF until a caller sets a limit, and every number below is
    labelled DECISION_PENDING_CALIBRATION in the run record (the unwind band, the unwind attempt budget and the residual age and kill limits live in ResidualPolicy, one setting each)."""
    retry_limit: int = 0                      # same-leg retries after a reject that permits one, or after a query that did not find the order
    retry_backoff_ns: int = 0
    query_attempt_limit: int = 3              # venue status queries for an UNKNOWN hedge before the alarm (H16: alarm, see OMS._hedge_unknown_escalation)
    max_book_age_ns: int | None = None        # a hedge book older than this at dispatch counts as not valid; None = the book's own state decides
    touch_slack_bps: float | None = None      # cap rule touch_slack (profile execution.hedge.cap): each hedge attempt is capped at the hedge venue's CURRENT touch moved against us by this many bps; None = the cap is the opportunity's price
    calibration_label: str = "DECISION_PENDING_CALIBRATION"


class IllegalHedgeTransition(ValueError):
    """A move the table does not allow, or one that lacks the evidence its rule needs. A bug in the caller, never absorbed."""


QUERY_RESULTS = ("found_executed", "found_partial", "found_live", "not_found", "failed")


@dataclass
class HedgeLifecycle:
    """One hedge chain: the state, its history and the figures the specification wants per event."""

    chain_id: str
    exposure_qty: float                          # base quantity the hedge must close (the filled quantity of the maker)
    retry_limit: int                             # configurable; labelled DECISION_PENDING_CALIBRATION by the caller (no production value is invented here)
    unwind_attempt_limit: int
    max_residual_age_s: float | None = None      # the residual-exposure limit; None means not set (the caller says so in the run record)
    query_attempt_limit: int = 3
    state: HedgeState = HedgeState.MAKER_FILLED
    opened_ts: float = 0.0
    hedged_qty: float = 0.0
    unwound_qty: float = 0.0
    retries: int = 0
    unwind_attempts: int = 0
    query_failures: int = 0
    timeouts: int = 0
    partial_fills: int = 0
    reject_labels: list[str] = field(default_factory=list)
    retry_intervals_s: list[float] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    outcome: str = ""
    exposure_closed_ts: float | None = None
    _last_reject_retry_permitted: bool | None = None
    _last_reject_ts: float | None = None
    _retry_origin: float | None = None
    _query_result: str | None = None

    @property
    def residual_qty(self) -> float:
        return max(0.0, self.exposure_qty - self.hedged_qty - self.unwound_qty)

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    @property
    def residual_exposure_duration_s(self) -> float | None:
        """From the maker fill until the exposure was closed (hedged or unwound); None while it is still open."""
        return None if self.exposure_closed_ts is None else self.exposure_closed_ts - self.opened_ts

    def residual_age_breached(self, now_ts: float) -> bool:
        return (self.max_residual_age_s is not None and self.exposure_closed_ts is None and self.residual_qty > 1e-12
                and now_ts - self.opened_ts > self.max_residual_age_s)

    def note_reject(self, ts: float, label: str, retry_permitted: bool) -> None:
        """Record a classified reject; it is what a later RETRYING move is checked against."""
        self.reject_labels.append(label)
        self._last_reject_retry_permitted = bool(retry_permitted)
        self._last_reject_ts = ts

    def note_query(self, result: str) -> None:
        if result not in QUERY_RESULTS:
            raise IllegalHedgeTransition(f"chain {self.chain_id}: unknown query result {result!r}")
        self._query_result = result
        if result == "failed":
            self.query_failures += 1

    def move(self, to: HedgeState, ts: float, reason: str = "", *, filled_qty: float | None = None, unwound_qty: float | None = None) -> None:
        """The only way the state changes. Raises on an illegal move or one missing its evidence."""
        frm = self.state
        if to not in TRANSITIONS[frm]:
            raise IllegalHedgeTransition(f"chain {self.chain_id}: illegal transition {frm.value} -> {to.value}")
        if to is _H.RETRYING:
            if frm is _H.UNKNOWN:
                if self._query_result != "not_found":
                    raise IllegalHedgeTransition(f"chain {self.chain_id}: UNKNOWN -> RETRYING needs a venue query that did not find the order (got {self._query_result!r})")
            elif frm is _H.REJECTED and not self._last_reject_retry_permitted:
                raise IllegalHedgeTransition(f"chain {self.chain_id}: the last reject does not permit a retry")
            if self.retries >= self.retry_limit:
                raise IllegalHedgeTransition(f"chain {self.chain_id}: retry limit {self.retry_limit} reached")
        if to is _H.UNWINDING:
            if self.unwind_attempts >= self.unwind_attempt_limit:
                raise IllegalHedgeTransition(f"chain {self.chain_id}: unwind budget {self.unwind_attempt_limit} used up")
        if to is _H.FAILED and frm is _H.UNKNOWN and self.query_failures < self.query_attempt_limit:
            raise IllegalHedgeTransition(f"chain {self.chain_id}: UNKNOWN -> FAILED only after {self.query_attempt_limit} failed venue queries (have {self.query_failures})")
        if to is _H.SUBMITTED and frm is _H.UNKNOWN and self._query_result not in ("found_live", "found_partial"):
            raise IllegalHedgeTransition(f"chain {self.chain_id}: UNKNOWN -> SUBMITTED means the venue has the order live; the query said {self._query_result!r}")
        if frm is _H.UNKNOWN and to in (_H.HEDGED, _H.PARTIALLY_FILLED):
            wanted = {"found_executed": _H.HEDGED, "found_partial": _H.PARTIALLY_FILLED}.get(self._query_result or "")
            if to is not wanted:
                raise IllegalHedgeTransition(f"chain {self.chain_id}: UNKNOWN -> {to.value} does not follow from the query result {self._query_result!r}")
        if to is _H.REJECTED and frm is _H.UNKNOWN and self._query_result != "not_found":
            raise IllegalHedgeTransition(f"chain {self.chain_id}: UNKNOWN -> REJECTED needs the venue to say it does not know the order")
        hedged, unwound = self.hedged_qty, self.unwound_qty
        if filled_qty is not None:
            if filled_qty < hedged - 1e-12 or filled_qty > self.exposure_qty + 1e-9:
                raise IllegalHedgeTransition(f"chain {self.chain_id}: hedged quantity {filled_qty} outside [{hedged}, {self.exposure_qty}]")
            hedged = filled_qty
        if unwound_qty is not None:
            if unwound_qty < unwound - 1e-12:
                raise IllegalHedgeTransition(f"chain {self.chain_id}: unwound quantity {unwound_qty} below the {unwound} already unwound")
            unwound = unwound_qty
        if hedged + unwound > self.exposure_qty + 1e-9:
            raise IllegalHedgeTransition(f"chain {self.chain_id}: hedged {hedged} + unwound {unwound} exceed the exposure {self.exposure_qty}")
        residual = max(0.0, self.exposure_qty - hedged - unwound)
        if to is _H.RECONCILED and frm is not _H.FAILED and residual > 1e-9:
            raise IllegalHedgeTransition(f"chain {self.chain_id}: RECONCILED with an open residual of {residual} (only the operator reset from FAILED may close one)")
        if to is _H.HEDGED and residual > 1e-9:
            raise IllegalHedgeTransition(f"chain {self.chain_id}: HEDGED with a residual of {residual}")
        # every check passed: nothing above changed the chain, everything below does
        if to is _H.PARTIALLY_FILLED and hedged > self.hedged_qty + 1e-12:
            self.partial_fills += 1
        self.hedged_qty, self.unwound_qty = hedged, unwound
        if to is _H.RETRYING:
            self.retries += 1
        if to is _H.UNWINDING:
            self.unwind_attempts += 1
        if to is _H.UNKNOWN:
            self.timeouts += 1
            self._query_result = None
        retry_interval = None
        if to is _H.RETRYING:
            self._retry_origin = self._last_reject_ts if frm is _H.REJECTED and self._last_reject_ts is not None else ts       # when the failure was known
        elif to is _H.SUBMITTED and frm is _H.RETRYING and self._retry_origin is not None:
            retry_interval = ts - self._retry_origin                                    # failure known -> retry sent: the retry interval
            self.retry_intervals_s.append(retry_interval)
        if to in (_H.HEDGED, _H.RECONCILED) and self.exposure_closed_ts is None:
            self.exposure_closed_ts = ts
        if to in (_H.HEDGED, _H.RECONCILED, _H.FAILED):
            self.outcome = ({_H.HEDGED: "hedged", _H.FAILED: "failed_alarm"}[to] if to is not _H.RECONCILED
                            else (self.outcome if self.outcome.endswith("reconciled") else (self.outcome or "unwound") + "_reconciled"))
        self.events.append({"ts": ts, "from": frm.value, "to": to.value, "reason": reason, "hedged_qty": self.hedged_qty, "residual_qty": self.residual_qty,
                            "retries": self.retries, "retry_interval_s": retry_interval, "reject_label": self.reject_labels[-1] if self.reject_labels else "",
                            "timeouts": self.timeouts, "partial_fills": self.partial_fills})
        self.state = to

    _SNAP = ("chain_id", "exposure_qty", "retry_limit", "unwind_attempt_limit", "max_residual_age_s", "query_attempt_limit", "opened_ts", "hedged_qty", "unwound_qty",
             "retries", "unwind_attempts", "query_failures", "timeouts", "partial_fills", "reject_labels", "retry_intervals_s", "outcome", "exposure_closed_ts")

    def snapshot(self) -> dict[str, Any]:
        """Everything needed to rebuild the chain after a restart (OMS03: the OMS persists this after every move)."""
        d = {k: getattr(self, k) for k in self._SNAP}
        d.update(state=self.state.value, last_reject_retry_permitted=self._last_reject_retry_permitted, last_reject_ts=self._last_reject_ts, query_result=self._query_result,
                 retry_origin=self._retry_origin,
                 events=list(self.events))
        return d

    @classmethod
    def restore(cls, snap: dict[str, Any]) -> "HedgeLifecycle":
        c = cls(**{k: snap[k] for k in ("chain_id", "exposure_qty", "retry_limit", "unwind_attempt_limit", "max_residual_age_s", "query_attempt_limit", "opened_ts")})
        for k in cls._SNAP[7:]:
            setattr(c, k, list(snap[k]) if isinstance(snap[k], list) else snap[k])
        c.state = HedgeState(snap["state"])
        c._last_reject_retry_permitted, c._last_reject_ts, c._query_result = snap["last_reject_retry_permitted"], snap["last_reject_ts"], snap["query_result"]
        c._retry_origin = snap.get("retry_origin")
        c.events = list(snap["events"])
        return c

    def as_record(self) -> dict[str, Any]:
        return {"chain_id": self.chain_id, "state": self.state.value, "outcome": self.outcome, "exposure_qty": self.exposure_qty, "hedged_qty": self.hedged_qty,
                "retries": self.retries, "retry_intervals_s": list(self.retry_intervals_s), "reject_labels": list(self.reject_labels), "timeouts": self.timeouts,
                "partial_fills": self.partial_fills, "unwind_attempts": self.unwind_attempts, "query_failures": self.query_failures,
                "residual_exposure_duration_s": self.residual_exposure_duration_s, "events": len(self.events)}


def hedge_state_of_plan(state: PlanState) -> HedgeState | None:
    return PLAN_TO_HEDGE.get(state)


def hedge_states_of_intent(state: str) -> tuple[HedgeState, ...]:
    return INTENT_TO_HEDGE.get(getattr(state, "value", state), ())
