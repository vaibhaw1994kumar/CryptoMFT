"""ExecutionLayer: the one place that decides how an order is worked (canonical execution layer).

Sits between the strategy and the engine's sender. The strategy opens an intent; the primary exposure is checked and reserved in
one call; the intent is JOURNALED; the tactic checks the child against the intent's limits and risk rules; only then does the
engine's sender run. Everything here is deterministic and draws nothing random, so a replay with the layer routed in is identical to
one without it (gate 0).

Rules enforced here (the profile makes them MANDATORY in perp; docs/strategy_contract.md 4g, 4h):
  - every intent but unwind / flatten carries a worst_price, and every child's limit stays inside it            (price cap)
  - a hedge intent exists only for a quantity already filled on its parent, unless dispatch_mode is "simultaneous"
  - one live hedge child per hedge intent: `count` records violations, `enforce` refuses the send; a ghost child makes the
    intent `unknown` and blocks a second child until it is resolved
  - primary exposure is checked and reserved atomically, released at the terminal state                            (4h.2)
  - the intent is journaled before its first child is sent; its state moves only along the transition table       (4h.1)
  - every fill the engine books is journaled here with its intent, for the independent reconciliation             (4h.5)
"""
from __future__ import annotations

from collections import Counter
from typing import Callable

from ..hedge_lifecycle import touch_slack_price
from .config import ExecutionConfig
from .intent import ExecutionIntent, IntentError, IntentState, RiskRefused, Role
from .risk import ReservationBook, RiskLimits
from .tactics import get_tactic

JOURNAL_MAX = 200_000
FILLS_MAX = 2_000_000
_KIND = {"primary": "P", "hedge": "H", "unwind": "U", "rebalance": "R", "flatten": "F"}


def _log_violation(rec: dict, mode: str) -> None:
    """Opt-in measurement (EXEC_VIOLATION_LOG=<file>): append every one-hedge-child violation as a JSON line, so a test-suite or
    scenario run can be counted before the rule is switched to `enforce`. Never read by the engine."""
    import os
    path = os.environ.get("EXEC_VIOLATION_LOG")
    if path:
        import json
        with open(path, "a") as f:
            f.write(json.dumps({"test": os.environ.get("PYTEST_CURRENT_TEST", ""), "mode": mode, **rec}) + "\n")


class ExecutionLayer:
    def __init__(self, cfg: ExecutionConfig | None, *, instrument: str, dispatch_mode: str = "fill_triggered") -> None:
        self.cfg = cfg or ExecutionConfig()
        self.instrument = instrument
        self.dispatch_mode = dispatch_mode
        self.intents: dict[str, ExecutionIntent] = {}
        self.journal: list[dict] = []
        self.fills: list[dict] = []                          # every fill the engine booked (4h.5); not capped by JOURNAL_MAX
        self._serial: Counter = Counter()
        self._tactics: dict[str, object] = {}
        self._live: dict[str, set[int]] = {}               # hedge intent id -> child order ids still open
        self._child_intent: dict[int, str] = {}
        self._child_cap: dict[int, float | None] = {}      # order id -> the cap the child was sent with
        self._ghost: set[int] = set()
        self._ghost_release: dict[int, float] = {}
        self._by_signal: dict[int, list[str]] = {}
        self.intent_of_origin: dict[tuple[int, str], str] = {}      # (origin order id, role) -> intent id (retries find their intent)
        self.counts: Counter = Counter()
        self.violations: list[dict] = []
        self.refusals: list[dict] = []
        self.reservations = ReservationBook()
        self._limits_fn: Callable[[], RiskLimits] | None = None
        self._base_fn: Callable[[], dict[str, float]] | None = None

    def suggest_tactic(self, state, cfg=None):
        """Gap F: the tactic choose_tactic picks for this market state, or None while execution.tactic_choice is off (it never changes what the layer sends; the caller decides what to do with the answer)."""
        if self.cfg.tactic_choice == "off":
            return None
        from .tactics.base import choose_tactic
        return choose_tactic(state, cfg, allow_market_banded=self.cfg.pairing == "maker_maker")      # MARKET_BANDED is for the market-making pairing only, and then only for its emergency reasons

    # ── risk wiring ──────────────────────────────────────────────────────────
    def set_risk(self, limits_fn: Callable[[], RiskLimits], base_fn: Callable[[], dict[str, float]]) -> None:
        self._limits_fn, self._base_fn = limits_fn, base_fn

    def _risk_gate(self, legs: list[tuple[str, str, float]], signal_id: int) -> None:
        """Check ONLY (pure). Raises RiskRefused in `enforce`; in `count` it records what would have been refused."""
        if self.cfg.risk_reservation == "off" or self._limits_fn is None or self._base_fn is None:
            return
        why = self.reservations.headroom_breach(self._limits_fn(), legs, self._base_fn())
        if not why:
            return
        self.counts["risk_would_refuse"] += 1
        if len(self.refusals) < 50:
            self.refusals.append({"signal_id": signal_id, "why": why, "legs": [list(l) for l in legs]})
        if self.cfg.risk_reservation == "enforce":
            self.counts["risk_refusals"] += 1
            raise RiskRefused(f"signal {signal_id}: {why}")

    # ── intents ──────────────────────────────────────────────────────────────
    def _create(self, *, role: str, side: str, qty: float, venues: tuple[str, ...], worst_price: float | None, now: float,
                signal_id: int, parent: ExecutionIntent | None, parent_filled_qty: float, urgency: str, deadline_ts: float | None,
                reason: str, tactic_hint: str) -> ExecutionIntent:
        kind = _KIND[role]
        self._serial[(signal_id, kind)] += 1
        iid = f"sig{signal_id}-{kind}{self._serial[(signal_id, kind)]}"
        it = ExecutionIntent(intent_id=iid, role=role, instrument=self.instrument, side=side, qty=qty, venues=tuple(venues),
                             worst_price=worst_price, urgency=urgency, deadline_ts=deadline_ts, tactic_hint=tactic_hint, reason=reason,
                             parent_id=parent.intent_id if parent is not None else "", parent_filled_qty=parent_filled_qty,
                             signal_id=signal_id, created_ts=now)
        self.intents[iid] = it
        self._by_signal.setdefault(signal_id, []).append(iid)
        if len(self.journal) < JOURNAL_MAX:
            self.journal.append({"seq": len(self.journal), "event": "intent", **it.as_record()})
        self.counts["intents_" + role] += 1
        if role == Role.HEDGE:
            self._live[iid] = set()
        return it

    def _validate_open(self, role: str, qty: float, worst_price: float | None, signal_id: int, parent, parent_filled_qty: float) -> None:
        if role == Role.HEDGE:
            if parent is None:
                raise IntentError("a hedge intent needs its parent intent")
            if parent_filled_qty < qty - 1e-9 and self.dispatch_mode != "simultaneous":
                raise IntentError(f"hedge for {qty} opened with only {parent_filled_qty} filled on {parent.intent_id} "
                                  f"(a hedge exists only after the maker fill)")
        if worst_price is None and self.cfg.price_cap_required and role not in (Role.UNWIND, Role.FLATTEN):
            raise IntentError(f"{role} intent for signal {signal_id} has no worst_price (price cap is mandatory)")

    def open_intent(self, *, role: str, side: str, qty: float, venues: tuple[str, ...], worst_price: float | None, now: float,
                    signal_id: int, parent: ExecutionIntent | None = None, parent_filled_qty: float = 0.0, urgency: str = "normal",
                    deadline_ts: float | None = None, reason: str = "", tactic_hint: str = "") -> ExecutionIntent:
        if role == Role.PRIMARY:
            return self.open_primary_group([dict(side=side, qty=qty, venues=venues, worst_price=worst_price, urgency=urgency,
                                                 deadline_ts=deadline_ts, reason=reason, tactic_hint=tactic_hint)],
                                           now=now, signal_id=signal_id)[0]
        self._validate_open(role, qty, worst_price, signal_id, parent, parent_filled_qty)
        return self._create(role=role, side=side, qty=qty, venues=venues, worst_price=worst_price, now=now, signal_id=signal_id,
                            parent=parent, parent_filled_qty=parent_filled_qty, urgency=urgency, deadline_ts=deadline_ts,
                            reason=reason, tactic_hint=tactic_hint)

    def open_primary_group(self, specs: list[dict], *, now: float, signal_id: int) -> list[ExecutionIntent]:
        """All primary legs of one signal: validate, check the worst-case exposure with every leg added, then create and reserve
        them — all or none, in one call (4h.2). A refusal leaves the layer exactly as it was."""
        for sp in specs:
            self._validate_open(Role.PRIMARY, sp["qty"], sp["worst_price"], signal_id, None, 0.0)
        legs = [(sp["venues"][0], sp["side"], sp["qty"]) for sp in specs]
        self._risk_gate(legs, signal_id)                                  # may raise; nothing has been written yet
        out = []
        for sp in specs:
            it = self._create(role=Role.PRIMARY, side=sp["side"], qty=sp["qty"], venues=sp["venues"], worst_price=sp["worst_price"],
                              now=now, signal_id=signal_id, parent=None, parent_filled_qty=0.0, urgency=sp.get("urgency", "normal"),
                              deadline_ts=sp.get("deadline_ts"), reason=sp.get("reason", ""), tactic_hint=sp.get("tactic_hint", ""))
            self.reservations.reserve(it.intent_id, sp["venues"][0], sp["side"], sp["qty"])
            out.append(it)
        self.counts["reservations_made"] += len(out)
        return out

    def _move(self, it: ExecutionIntent, to: IntentState, now: float | None, why: str) -> None:
        if it.state == to:
            return
        frm = it.state
        it.move(to)
        if len(self.journal) < JOURNAL_MAX:
            self.journal.append({"seq": len(self.journal), "event": "state", "intent_id": it.intent_id, "from": frm.value,
                                 "to": to.value, "why": why, "ts": now})

    def retarget(self, intent: ExecutionIntent, worst_price: float | None, *, qty: float, reason: str,
                 touch: float | None = None, max_walk_bps: float | None = None) -> None:
        """A retry or a repeated unwind attempt re-prices the cap from the then-current book (cap rule `touch_slack`, and the
        unwind band): the cap moves with the touch, never past what the cap rule allows (4h.3). A fixed cap (phase 1:
        conditional break-even) refuses retargeting. The retry covers the unfilled remainder only."""
        if intent.role == Role.HEDGE and self.cfg.hedge.get("cap", "touch_slack") != "touch_slack":
            raise IntentError(f"hedge intent {intent.intent_id}: cap rule {self.cfg.hedge.get('cap')!r} is fixed for the intent")
        remaining = intent.qty - intent.filled_qty
        if qty > intent.qty + 1e-9:
            raise IntentError(f"retry qty {qty} exceeds intent qty {intent.qty}")
        if qty > remaining + 1e-9:
            self.counts["retry_exceeds_remaining"] += 1
            if self.cfg.one_hedge_child == "enforce" and intent.role == Role.HEDGE:
                raise IntentError(f"retry qty {qty} exceeds the unfilled remainder {remaining} of {intent.intent_id}")
        if worst_price is None and self.cfg.price_cap_required and intent.role not in (Role.UNWIND, Role.FLATTEN):
            raise IntentError(f"intent {intent.intent_id}: retarget without a worst_price")
        if (intent.role == Role.HEDGE and touch is not None and max_walk_bps is not None and worst_price is not None):
            rule = self.touch_slack_cap(intent.side, touch, max_walk_bps)
            tol = abs(rule) * 1e-12
            if (intent.side == "buy" and worst_price > rule + tol) or (intent.side == "sell" and worst_price < rule - tol):
                raise IntentError(f"intent {intent.intent_id}: retargeted cap {worst_price} is looser than the touch_slack rule {rule}")
        old = intent.worst_price
        intent.worst_price = worst_price
        self.counts["retargets"] += 1
        if len(self.journal) < JOURNAL_MAX:
            self.journal.append({"seq": len(self.journal), "event": "retarget", "intent_id": intent.intent_id, "from": old,
                                 "to": worst_price, "reason": reason})

    def touch_slack_cap(self, side: str, touch: float, max_walk_bps: float) -> float:
        """Cap rule `touch_slack`: the hedge-venue touch moved against us by max(max_walk_bps, slack_bps_floor) bps."""
        return touch_slack_price(side, touch, max(max_walk_bps, float(self.cfg.hedge.get("slack_bps_floor", 5.0))))

    def tactic_name(self, intent: ExecutionIntent, leg) -> str:
        if intent.role == Role.HEDGE:
            return self.cfg.hedge.get("tactic", "IOC_CAPPED")
        if intent.role in (Role.UNWIND, Role.FLATTEN):
            return self.cfg.unwind.get("tactic", "MARKET_BANDED")
        if leg.is_taker:                                       # taker_taker legs
            return "IOC_CAPPED"
        return "PASSIVE"

    def passive_price(self, side: str, best_bid: float, best_ask: float, tick: float, edge_price: float) -> float | None:
        """The resting price for the configured placement, or None when the strategy's own price is to be sent unchanged (`deep`)."""
        tac = self._tactics.get("PASSIVE")
        if tac is None:
            tac = self._tactics["PASSIVE"] = get_tactic("PASSIVE", self.cfg)
        return tac.price(side, best_bid, best_ask, tick, edge_price) if tac.reprices else None

    def hedge_blocked(self, intent: ExecutionIntent) -> bool:
        """True while the intent has a live child (a ghost, or one in flight): the contract allows no second one (4h.4)."""
        return intent.role == Role.HEDGE and self.cfg.one_hedge_child != "off" and bool(self._live.get(intent.intent_id))

    def ghost_children(self, intent: ExecutionIntent) -> list[int]:
        return sorted(self._live.get(intent.intent_id, set()) & self._ghost)

    def ghost_release_ts(self, intent: ExecutionIntent) -> float | None:
        ts = [self._ghost_release[o] for o in self.ghost_children(intent) if o in self._ghost_release]
        return max(ts) if ts else None

    def check(self, intent: ExecutionIntent, leg) -> str:
        """Run the tactic's and the layer's pre-send checks for one child. Raises IntentError; returns the tactic name."""
        name = self.tactic_name(intent, leg)
        tac = self._tactics.get(name)
        if tac is None:
            tac = self._tactics[name] = get_tactic(name, self.cfg)
        tac.check_child(intent, leg)
        if intent.terminal:
            raise IntentError(f"intent {intent.intent_id} is {intent.state.value}: no further child")
        if intent.role == Role.HEDGE and self.cfg.one_hedge_child != "off":
            live = self._live.get(intent.intent_id) or set()
            if live:
                self.counts["one_hedge_child_violations"] += 1
                rec = {"intent_id": intent.intent_id, "live_children": sorted(live), "ghost": sorted(live & self._ghost)}
                if len(self.violations) < 50:
                    self.violations.append(rec)
                _log_violation(rec, self.cfg.one_hedge_child)
                if self.cfg.one_hedge_child == "enforce":
                    raise IntentError(f"hedge intent {intent.intent_id} already has a live child {sorted(live)}")
        return name

    # ── children ─────────────────────────────────────────────────────────────
    def child_sent(self, intent: ExecutionIntent, order_id: int, origin_id: int, tactic: str, now: float) -> None:
        """Called by the engine's sender once the order exists. The intent was journaled in open_intent, i.e. before this call."""
        intent.children.append(order_id)
        self._child_intent[order_id] = intent.intent_id
        self._child_cap[order_id] = intent.worst_price
        if intent.state in (IntentState.ACCEPTED, IntentState.UNKNOWN):
            self._move(intent, IntentState.WORKING, now, "first child sent" if intent.state == IntentState.ACCEPTED else "child sent")
        self.intent_of_origin[(origin_id or order_id, intent.role)] = intent.intent_id
        self.counts["children"] += 1
        self.counts["children_" + tactic] += 1
        if len(self.journal) < JOURNAL_MAX:
            self.journal.append({"seq": len(self.journal), "event": "child", "intent_id": intent.intent_id, "order_id": order_id,
                                 "tactic": tactic, "ts": now})
        if intent.role == Role.HEDGE:
            live = self._live[intent.intent_id]
            live.add(order_id)
            self.counts["max_live_hedge_children"] = max(self.counts["max_live_hedge_children"], len(live))

    def child_closed(self, order_id: int) -> None:
        iid = self._child_intent.get(order_id)
        if iid is not None and iid in self._live:
            self._live[iid].discard(order_id)
            was_ghost = order_id in self._ghost
            self._ghost.discard(order_id)
            self._ghost_release.pop(order_id, None)
            it = self.intents[iid]
            if was_ghost and it.state == IntentState.UNKNOWN and not (self._live[iid] & self._ghost):
                self._move(it, IntentState.PARTIALLY_DONE if it.filled_qty > 1e-9 else IntentState.WORKING, None, "ghost resolved")
                self.counts["ghosts_resolved"] += 1

    def child_ghosted(self, order_id: int, release_ts: float | None = None) -> None:
        """The strategy gave the order up but a copy is still delivered at reconnect: it is live at the venue until it executes.
        The intent is `unknown` until the copy is resolved (4h.1)."""
        iid = self._child_intent.get(order_id)
        if iid is not None and iid in self._live:
            self._live[iid].add(order_id)
            self._ghost.add(order_id)
            if release_ts is not None:
                self._ghost_release[order_id] = release_ts
            self.counts["ghost_children"] += 1
            it = self.intents[iid]
            if not it.terminal:
                self._move(it, IntentState.UNKNOWN, release_ts, "child given up but still live at the venue")

    def bind_origin(self, origin_id: int, role: str, intent: ExecutionIntent) -> None:
        """A chain whose first attempt was never sent (the cap was unreachable) still belongs to its intent when it is retried."""
        self.intent_of_origin[(origin_id, role)] = intent.intent_id

    def intent_for(self, origin_id: int, role: str) -> ExecutionIntent | None:
        iid = self.intent_of_origin.get((origin_id, role))
        return self.intents.get(iid) if iid else None

    # ── fills, settlement, reservations ──────────────────────────────────────
    def note_filled(self, intent: ExecutionIntent | None, qty: float, now: float | None = None) -> None:
        if intent is None:
            return
        if intent.terminal or intent.filled_qty + qty > intent.qty + 1e-9:
            # a fill the intent has no room for: reported (a ghost over-hedge, a late fill after the intent closed); never a transition
            self.counts["overfills"] += 1
            self.counts["overfill_qty"] += max(0.0, min(qty, intent.filled_qty + qty - intent.qty))
        intent.filled_qty += qty
        self.reservations.consume(intent.intent_id, qty)
        if intent.terminal:
            return
        to = IntentState.DONE if intent.filled_qty >= intent.qty - 1e-9 else IntentState.PARTIALLY_DONE
        if intent.state == IntentState.ESCALATED and to == IntentState.PARTIALLY_DONE:
            return                                                  # stays escalated until it settles
        self._move(intent, to, now, "fill")
        if to == IntentState.DONE:
            self.reservations.release(intent.intent_id)

    def escalate(self, intent: ExecutionIntent | None, now: float, why: str) -> None:
        if intent is not None and not intent.terminal and intent.state != IntentState.ESCALATED:
            self._move(intent, IntentState.ESCALATED, now, why)

    def settle(self, intent: ExecutionIntent | None, now: float | None, why: str = "") -> None:
        """The only way to a terminal state besides a full fill: done if the filled quantity covers the intent, else failed.
        A no-op while a child is live — then the intent is `unknown` and is settled when the child is resolved."""
        if intent is None or intent.terminal:
            return
        if self._live.get(intent.intent_id):
            if intent.state != IntentState.UNKNOWN:
                self._move(intent, IntentState.UNKNOWN, now, why or "settle with a live child")
            return
        done = intent.filled_qty >= intent.qty - 1e-9
        if intent.state == IntentState.ACCEPTED and done:
            self._move(intent, IntentState.WORKING, now, "settle")
        self._move(intent, IntentState.DONE if done else IntentState.FAILED, now, why or "settled")
        self.reservations.release(intent.intent_id)

    def settle_signal(self, signal_id: int, now: float | None, why: str = "signal resolved") -> None:
        for iid in self._by_signal.get(signal_id, ()):
            self.settle(self.intents[iid], now, why)

    def finalize(self, now: float | None) -> None:
        """End of data: whatever is not settled is settled or stays unknown; reservations of terminal intents must be gone."""
        for it in list(self.intents.values()):
            self.settle(it, now, "end of data")
        self.counts["reservations_open_at_end"] = len(self.reservations.held)

    def leaked_reservations(self) -> list[str]:
        return [iid for iid in self.reservations.held if self.intents[iid].terminal]

    def note_fill(self, rec: dict) -> None:
        """Journal one booked fill (record-only: nothing the engine does depends on it)."""
        if len(self.fills) < FILLS_MAX:
            rec["seq"] = len(self.fills)
            rec["cap"] = self._child_cap.get(rec.get("child_id")) if rec.get("intent_id") and rec.get("child_id") else None
            self.fills.append(rec)
        else:
            self.counts["fills_truncated"] += 1

    def note_instant(self, *, role: str, side: str, qty: float, venue: str, now: float, signal_id: int, reason: str) -> None:
        """A latency-free flatten that bypasses the sender (legacy oracle _unwind): journaled, not routed."""
        self.counts["instant_flattens"] += 1
        if len(self.journal) < JOURNAL_MAX:
            self.journal.append({"seq": len(self.journal), "event": "instant", "role": role, "side": side, "qty": qty, "venue": venue,
                                 "ts": now, "signal_id": signal_id, "reason": reason, "tactic": "MARKET_BANDED"})

    # ── report ───────────────────────────────────────────────────────────────
    def report(self) -> dict:
        by_state = Counter(i.state.value for i in self.intents.values())
        return {"config": self.cfg.as_dict(), "config_sha256": self.cfg.sha256(), "intents": len(self.intents),
                "intents_by_state": dict(by_state), "counts": dict(self.counts), "journal_entries": len(self.journal),
                "fills_journaled": len(self.fills),
                "one_hedge_child_mode": self.cfg.one_hedge_child, "risk_reservation_mode": self.cfg.risk_reservation,
                "violations": list(self.violations), "risk_refusals": list(self.refusals),
                "reservations_open": len(self.reservations.held), "reservations_released": self.reservations.released,
                "reservations_leaked": self.leaked_reservations()}
