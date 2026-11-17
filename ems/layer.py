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
from decimal import Decimal
from types import SimpleNamespace
from typing import Callable, NamedTuple

from ..hedge_lifecycle import touch_slack_price
from .config import ExecutionConfig
from .intent import ExecutionIntent, IntentError, IntentState, RiskRefused, Role
from .risk import ReservationBook, RiskLimits
from .tactics import get_tactic
from .venue_selector import RestHedgeVenue, assign_rest_hedge

JOURNAL_MAX = 200_000


class _Journal(list):
    """The intent journal: a list, as before, with two additions (W2a, owner 17 Nov; audit A2 0.2d and 2.1). `room()` is the old `len < JOURNAL_MAX` test and COUNTS every record that is not kept once the
    journal is full (`truncated`: until now it stopped at 200,000 silently). `sink`, when set, is called with each record as it is appended (a paper session streams the journal to disk, appended
    and flushed per record). Neither changes a record or its order."""

    def __init__(self) -> None:
        super().__init__()
        self.truncated = 0
        self.sink = None

    def room(self) -> bool:
        if len(self) < JOURNAL_MAX:
            return True
        self.truncated += 1
        return False

    def append(self, rec: dict) -> None:
        super().append(rec)
        if self.sink is not None:
            self.sink(rec)
FILLS_MAX = 2_000_000
_KIND = {"primary": "P", "hedge": "H", "unwind": "U", "rebalance": "R", "flatten": "F"}


# The hedge rules, ONCE (E0 step b, owner 10 Nov). Pure functions on purpose: the OMS hedge dispatch calls them whether or not a layer instance (the intent journal) exists in the
# session, so a run with the journal off decides exactly as one with it on. The ExecutionLayer methods below delegate to the same functions.
def touch_slack_bps(max_walk_bps: float, floor_bps: float) -> float:
    """The effective slack of the cap rule `touch_slack`: max(max_walk_bps, slack_bps_floor)."""
    return max(float(max_walk_bps), float(floor_bps))


def hedge_slack_bps(hedge_cfg: dict, max_walk_bps: float) -> float | None:
    """The slack the OMS hedge policy carries, from the profile's `execution.hedge` block: the effective slack when the cap rule is `touch_slack`, else None (the cap is the opportunity's price)."""
    if hedge_cfg.get("cap") not in ("touch_slack", "size_aware"):
        return None
    return touch_slack_bps(max_walk_bps, hedge_cfg.get("slack_bps_floor", 5.0))


def hedge_policy_fields(hedge_cfg: dict, max_walk_bps: float) -> dict:
    """The cap fields of the OMS HedgePolicy, from the profile's `execution.hedge` block (one place, read by every session constructor). cap `size_aware` (owner 12 Nov) adds its three numbers;
    max_slip_bps 15 and vol_k 0 are ASSUMED, latency_s is the hedge latency the volatility term scales with."""
    out = {"touch_slack_bps": hedge_slack_bps(hedge_cfg, max_walk_bps), "cap_basis": str(hedge_cfg.get("cap_basis", "touch"))}
    if hedge_cfg.get("cap") == "size_aware":
        out.update(size_aware=True, max_slip_bps=float(hedge_cfg.get("max_slip_bps", 15.0)), vol_k=float(hedge_cfg.get("vol_k", 0.0)), latency_s=float(hedge_cfg.get("latency_s", 0.0)))
    return out


def limit_on_grid(side: str, price: Decimal, tick: Decimal) -> Decimal:
    """A limit price moved onto the venue's grid WITHOUT loosening it: a buy limit rounds down, a sell limit rounds up (owner 12 Nov). The one rounding of every IOC, hedge and unwind limit."""
    return (price / tick).to_integral_value(rounding="ROUND_FLOOR" if side.lower() == "buy" else "ROUND_CEILING") * tick


def hedge_cap_on_grid(side: str, touch: Decimal, slack_bps, tick: Decimal) -> Decimal:
    """Cap rule `touch_slack` for one hedge attempt: the CURRENT hedge touch moved against us by `slack_bps`, on the venue's price grid, never inside the cap
    (a buy rounds down, a sell rounds up). Decimal throughout; re-taken from the touch at every attempt by the caller."""
    return limit_on_grid(side, touch_slack_price(side, touch, Decimal(str(slack_bps))), tick)


def hedge_cap_size_aware(side: str, touch: Decimal, deepest: Decimal, slack_bps, vol_bps, max_slip_bps, tick: Decimal) -> tuple[Decimal, bool]:
    """Cap rule `size_aware` for one hedge attempt (owner 12 Nov). `deepest` is the price of the last level the hedge quantity reaches on the bot's VIEW of the hedge book (the touch itself when the
    touch covers the size); the cap is that level moved against us by the allowance (the slack floor plus the volatility/latency term `vol_bps`), never beyond the touch by more than the absolute
    limit max(max_slip_bps, slack_bps) (a floor on the limit keeps the touch-covers case equal to touch_slack), on the venue's grid without loosening (a buy rounds down, a sell rounds up).
    Returns (cap, the absolute limit bound it)."""
    buy = side.lower() == "buy"
    raw = touch_slack_price(side, deepest, Decimal(str(slack_bps)) + Decimal(str(vol_bps)))
    limit = touch_slack_price(side, touch, Decimal(str(max(float(max_slip_bps), float(slack_bps)))))
    binds = raw > limit if buy else raw < limit
    if binds:
        raw = limit
    return limit_on_grid(side, raw, tick), binds


class HedgeRetry(NamedTuple):
    retry: bool
    due_ns: int | None         # when the retry is due (None = no retry)
    why_not: str               # "" when retrying


def hedge_retry_decision(retries: int, retry_limit: int, permitted: bool, backoff_ns: int, ts_ns: int) -> HedgeRetry:
    """Retry rule of a hedge chain: a retry is sent after the backoff when the reject (or the IOC remainder) permits one and the budget is not used; else the chain is unwound.
    The chain's own limit check (HedgeLifecycle) stays as the second line."""
    if permitted and retries < retry_limit:
        return HedgeRetry(True, ts_ns + backoff_ns, "")
    return HedgeRetry(False, None, "reject not retryable" if not permitted else f"retry budget {retry_limit} used")


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
        self.journal: _Journal = _Journal()
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
        self._routing: tuple[str, str] | None = None
        self.reservations = ReservationBook()
        self._limits_fn: Callable[[], RiskLimits] | None = None
        self._base_fn: Callable[[], dict[str, float]] | None = None
        self._evidence_fn: Callable[..., dict | None] | None = None      # routing evidence (owner 14 Nov, steps B, C, E, F): (venue, side the order consumes, now, qty) -> {evidence name: value} or None. None: nothing is added to any journal record

    def set_evidence_provider(self, fn: Callable[..., dict | None] | None) -> None:
        """Routing evidence: with a provider set, every intent and every child the journal records also carries, for each evidence name the provider returns, `rec[name] = {venue: value}` (for example `depth_quality`,
        `impact`, `order_split`), computed at the decision from what the venue's book showed. Observation only: nothing reads it back and nothing is sent from it. With no provider the records have no such key at all,
        so every existing journal is unchanged."""
        self._evidence_fn = fn

    def _evidence(self, venues, side: str, now: float, qty: float | None = None) -> dict | None:
        if self._evidence_fn is None:
            return None
        consumed = "ask" if side == "buy" else "bid"                       # a buy takes the asks, a sell the bids
        out: dict = {}
        for v in venues:
            for name, value in (self._evidence_fn(v, consumed, now, qty) or {}).items():
                if value is not None:
                    out.setdefault(name, {})[v] = value
        return out or None

    def suggest_tactic(self, state, cfg=None):
        """Gap F: the tactic choose_tactic picks for this market state, or None while execution.tactic_choice is off (it never changes what the layer sends; the caller decides what to do with the answer)."""
        if self.cfg.tactic_choice == "off":
            return None
        from .tactics.base import choose_tactic
        return choose_tactic(state, cfg, allow_market_banded=self.cfg.pairing == "maker_maker")      # MARKET_BANDED is for the market-making pairing only, and then only for its emergency reasons

    # ── routing decision (E0 step (d), owner 12 Nov) ─────────────────────────
    def set_routing(self, rest_venue: str, hedge_venue: str) -> None:
        """The fixed assignment: `rest_venue` rests the passive leg, `hedge_venue` hedges it (the venue selector in `fixed` mode; `dynamic` would only be reported, and no cost inputs exist here, so it is not computed)."""
        if rest_venue == hedge_venue:
            raise ValueError("the resting venue and the hedge venue must differ")
        decision = assign_rest_hedge([RestHedgeVenue(rest_venue, True, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0), RestHedgeVenue(hedge_venue, True, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)], mode="fixed", fixed=(rest_venue, hedge_venue))
        self._routing = (decision.chosen.rest, decision.chosen.hedge)

    def decide_route(self, *, role: str, passive: bool, order_venue: str) -> "RouteDecision":
        """The ONE place a child's venue is decided; the router (StrategyDirectedRouter) only executes it. Fixed mode: a passive primary leg goes to the resting venue, a hedge to the hedge venue; a taker primary leg
        and an unwind or flatten keep the venue they were built for (a reduce-only order belongs to the venue that holds the position). With no assignment configured the decision is the order's own venue."""
        if self._routing is None:
            decision = RouteDecision(order_venue, "NO_ROUTING_RULE")
        elif role == Role.PRIMARY and passive:
            decision = RouteDecision(self._routing[0], "FIXED_REST")
        elif role == Role.HEDGE:
            decision = RouteDecision(self._routing[1], "FIXED_HEDGE")
        else:
            decision = RouteDecision(order_venue, "OWN_VENUE")
        self.counts["route_decisions"] += 1
        return decision

    # ── risk wiring ──────────────────────────────────────────────────────────
    def set_risk(self, limits_fn: Callable[[], RiskLimits], base_fn: Callable[[], dict[str, float]]) -> None:
        self._limits_fn, self._base_fn = limits_fn, base_fn

    def _risk_gate(self, legs: list[tuple[str, str, float]], signal_id: int) -> None:
        """Check ONLY (pure). Raises RiskRefused in `enforce`; in `count` it records what would have been refused. E0 step (c): this is gate 1 of the two atomic gates (the layer's base ceiling over filled +
        reserved exposure); the core's notional / margin / residual reservation (`risk.reserve`) is gate 2. Both are decided before anything is written; the layer's check runs first."""
        if self.cfg.pairing == "none":                                    # a market-making quote (owner 13 Nov): the per-pair inventory cap, beside the other reservations; there is no arb ceiling
            if self.cfg.risk_reservation == "off" or self._base_fn is None:
                return
            why = self.reservations.quote_breach(float(self.cfg.quote["inventory_cap_base"]), legs, sum(self._base_fn().values()))
        elif self.cfg.risk_reservation == "off" or self._limits_fn is None or self._base_fn is None:
            return
        else:
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
                reason: str, tactic_hint: str, dust_included: float = 0.0) -> ExecutionIntent:
        kind = _KIND[role]
        self._serial[(signal_id, kind)] += 1
        iid = f"sig{signal_id}-{kind}{self._serial[(signal_id, kind)]}"
        it = ExecutionIntent(intent_id=iid, role=role, instrument=self.instrument, side=side, qty=qty, venues=tuple(venues),
                             worst_price=worst_price, urgency=urgency, deadline_ts=deadline_ts, tactic_hint=tactic_hint, reason=reason,
                             parent_id=parent.intent_id if parent is not None else "", parent_filled_qty=parent_filled_qty,
                             signal_id=signal_id, created_ts=now, dust_included=dust_included)
        self.intents[iid] = it
        self._by_signal.setdefault(signal_id, []).append(iid)
        if self.journal.room():
            rec = {"seq": len(self.journal), "event": "intent", **it.as_record()}
            ev = self._evidence(venues, side, now, qty)
            if ev is not None:
                rec.update(ev)                                         # routing evidence (owner 14 Nov): present only when a provider is set
            self.journal.append(rec)
        self.counts["intents_" + role] += 1
        if role == Role.HEDGE:
            self._live[iid] = set()
        return it

    def _validate_open(self, role: str, qty: float, worst_price: float | None, signal_id: int, parent, parent_filled_qty: float, dust_only: bool = False) -> None:
        if role == Role.HEDGE:
            if parent is None and not dust_only:
                raise IntentError("a hedge intent needs its parent intent")
            if parent_filled_qty < qty - 1e-9 and self.dispatch_mode != "simultaneous" and not dust_only:
                raise IntentError(f"hedge for {qty} opened with only {parent_filled_qty} filled on {parent.intent_id} "
                                  f"(a hedge exists only after the maker fill)")
        if worst_price is None and self.cfg.price_cap_required and role not in (Role.UNWIND, Role.FLATTEN):
            raise IntentError(f"{role} intent for signal {signal_id} has no worst_price (price cap is mandatory)")

    def open_intent(self, *, role: str, side: str, qty: float, venues: tuple[str, ...], worst_price: float | None, now: float,
                    signal_id: int, parent: ExecutionIntent | None = None, parent_filled_qty: float = 0.0, urgency: str = "normal",
                    deadline_ts: float | None = None, reason: str = "", tactic_hint: str = "", dust_included: float = 0.0, dust_only: bool = False) -> ExecutionIntent:
        if role == Role.PRIMARY:
            return self.open_primary_group([dict(side=side, qty=qty, venues=venues, worst_price=worst_price, urgency=urgency,
                                                 deadline_ts=deadline_ts, reason=reason, tactic_hint=tactic_hint)],
                                           now=now, signal_id=signal_id)[0]
        self._validate_open(role, qty, worst_price, signal_id, parent, parent_filled_qty, dust_only)
        it = self._create(role=role, side=side, qty=qty, venues=venues, worst_price=worst_price, now=now, signal_id=signal_id,
                          parent=parent, parent_filled_qty=parent_filled_qty, urgency=urgency, deadline_ts=deadline_ts,
                          reason=reason, tactic_hint=tactic_hint, dust_included=dust_included)
        return it

    def open_primary_group(self, specs: list[dict], *, now: float, signal_id: int, gated: bool = False) -> list[ExecutionIntent]:
        """All primary legs of one signal: validate, check the worst-case exposure with every leg added, then create and reserve
        them — all or none, in one call (4h.2). A refusal leaves the layer exactly as it was."""
        for sp in specs:
            self._validate_open(Role.PRIMARY, sp["qty"], sp["worst_price"], signal_id, None, 0.0)
        legs = [(sp["venues"][0], sp["side"], sp["qty"]) for sp in specs]
        if not gated:                                                     # `gated`: the caller already ran this check (IntentObserver.admit) and must not count it twice
            self._risk_gate(legs, signal_id)                              # may raise; nothing has been written yet
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
        if self.journal.room():
            self.journal.append({"seq": len(self.journal), "event": "state", "intent_id": it.intent_id, "from": frm.value,
                                 "to": to.value, "why": why, "ts": now})

    def retarget(self, intent: ExecutionIntent, worst_price: float | None, *, qty: float, reason: str,
                 touch: float | None = None, max_walk_bps: float | None = None) -> None:
        """A retry or a repeated unwind attempt re-prices the cap from the then-current book (cap rule `touch_slack`, and the
        unwind band): the cap moves with the touch, never past what the cap rule allows (4h.3). A fixed cap (phase 1:
        conditional break-even) refuses retargeting. The retry covers the unfilled remainder only."""
        cap_rule = self.cfg.hedge.get("cap", "touch_slack")
        if intent.role == Role.HEDGE and cap_rule not in ("touch_slack", "size_aware"):      # size_aware re-prices from the then-current book at every attempt, like touch_slack
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
        if (intent.role == Role.HEDGE and cap_rule == "touch_slack" and touch is not None and max_walk_bps is not None and worst_price is not None):      # a size_aware cap may span levels: the touch_slack bound is not its rule
            rule = self.touch_slack_cap(intent.side, touch, max_walk_bps)
            tol = abs(rule) * 1e-12
            if (intent.side == "buy" and worst_price > rule + tol) or (intent.side == "sell" and worst_price < rule - tol):
                raise IntentError(f"intent {intent.intent_id}: retargeted cap {worst_price} is looser than the touch_slack rule {rule}")
        old = intent.worst_price
        intent.worst_price = worst_price
        self.counts["retargets"] += 1
        if self.journal.room():
            self.journal.append({"seq": len(self.journal), "event": "retarget", "intent_id": intent.intent_id, "from": old,
                                 "to": worst_price, "reason": reason})

    def touch_slack_cap(self, side: str, touch: float, max_walk_bps: float) -> float:
        """Cap rule `touch_slack`: the hedge-venue touch moved against us by max(max_walk_bps, slack_bps_floor) bps."""
        return touch_slack_price(side, touch, touch_slack_bps(max_walk_bps, self.cfg.hedge.get("slack_bps_floor", 5.0)))

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
        if self.journal.room():
            rec = {"seq": len(self.journal), "event": "child", "intent_id": intent.intent_id, "order_id": order_id, "tactic": tactic, "ts": now}
            ev = self._evidence(intent.venues, intent.side, now, intent.qty)
            if ev is not None:
                rec.update(ev)
            self.journal.append(rec)
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
            if intent.state == IntentState.ACCEPTED and not self._live.get(intent.intent_id):
                # The intent never reached `working`: nothing was sent (a hedge refused by an instrument rule such as a minimum notional before any order existed), so there is nothing to escalate. It fails
                # as not sent; the unwind that follows is its own intent. (Found by the 25 SOL sweep run: the illegal accepted -> escalated move ended the whole session.)
                self.settle(intent, now, "refused before send: " + why)
                return
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
        if self.journal.room():
            self.journal.append({"seq": len(self.journal), "event": "instant", "role": role, "side": side, "qty": qty, "venue": venue,
                                 "ts": now, "signal_id": signal_id, "reason": reason, "tactic": "MARKET_BANDED"})

    # ── report ───────────────────────────────────────────────────────────────
    def report(self) -> dict:
        by_state = Counter(i.state.value for i in self.intents.values())
        return {"config": self.cfg.as_dict(), "config_sha256": self.cfg.sha256(), "intents": len(self.intents),
                "intents_by_state": dict(by_state), "counts": dict(self.counts), "journal_entries": len(self.journal), **({"journal_truncated": True, "journal_records_dropped": self.journal.truncated} if self.journal.truncated else {}),
                "fills_journaled": len(self.fills),
                "one_hedge_child_mode": self.cfg.one_hedge_child, "risk_reservation_mode": self.cfg.risk_reservation,
                "violations": list(self.violations), "risk_refusals": list(self.refusals),
                "reservations_open": len(self.reservations.held), "reservations_released": self.reservations.released,
                "reservations_leaked": self.leaked_reservations()}


class RouteDecision(NamedTuple):
    venue: str
    reason: str                                 # FIXED_REST | FIXED_HEDGE | OWN_VENUE | NO_ROUTING_RULE


class IntentObserver:
    """E0 step (a): the session runtime's orders, journaled as intents next to the OMS plan. OBSERVATION ONLY.

    The OMS tells this class what it did (a plan opened, a hedge chain opened, a child added, an order changed); the class opens the
    matching intent in an `ExecutionLayer`, records the child and the fills, and counts what the layer's rules would have said
    (`check` violations, over-fills). Nothing here is read back by the OMS: it never refuses, delays or reprices an order, and a rule
    refusal (IntentError) is counted and kept in `errors`, never raised into the engine. Step (b) onward makes the layer decide.
    One OMS parent plan = the primary intent of each of its children; one hedge chain = one HEDGE intent; one unwind child = one UNWIND intent.
    Until step (b) the hedge cap is read from the child itself, so the layer's price-cap check cannot fail on the hedge;
    `cap_looser_than_opportunity_price` counts the hedge children whose limit is beyond the opportunity's price (the touch_slack rule moves the cap)."""

    def __init__(self, layer: "ExecutionLayer") -> None:
        self.layer = layer
        self.errors: list[dict] = []
        self.error_counts: Counter = Counter()
        self.counts: Counter = Counter()
        self._signal: dict[str, int] = {}
        self._by_order: dict[str, ExecutionIntent] = {}
        self._by_chain: dict[str, ExecutionIntent] = {}
        self._spec_price: dict[str, float] = {}
        self._first_child: dict[str, str] = {}
        self._filled: dict[str, float] = {}
        self._closed: set[str] = set()
        self._admitted: set[str] = set()                # plans whose layer gate ran in admit()

    def pairing(self) -> str:
        """The layer's execution.pairing: the OMS reads it once, to admit a market-making quote only under pairing none (execute_quote)."""
        return self.layer.cfg.pairing

    def _guard(self, what: str, fn, *args, **kw):
        try:
            return fn(*args, **kw)
        except IntentError as exc:
            self.error_counts[what] += 1
            if len(self.errors) < 50:
                self.errors.append({"call": what, "why": str(exc)})
            return None

    @staticmethod
    def _px(v) -> float | None:
        return None if v is None else float(v)

    def _child(self, intent: ExecutionIntent, order, now: float, passive: bool) -> None:
        leg = SimpleNamespace(venue=order.venue, side=order.side.value.lower(), qty=float(order.quantity), limit_price=self._px(order.limit_price), is_taker=not passive)
        name = self._guard("check", self.layer.check, intent, leg)
        if name is None:
            name = "PASSIVE" if passive else ("MARKET_BANDED" if intent.role in (Role.UNWIND, Role.FLATTEN) else "IOC_CAPPED")
        self._by_order[order.order_id] = intent
        order.intent_id = intent.intent_id                          # E0 step (e): every order carries its intent id, journaled before this first child (open_intent writes the intent record first)
        origin = self._first_child.setdefault(intent.intent_id, order.order_id)
        self.layer.child_sent(intent, order.order_id, origin, name, now)

    def admit(self, plan, now: float) -> str | None:
        """E0 step (c), gate 1 of 2, BEFORE the OMS reserves anything: the layer's base-ceiling check for the package's legs. Returns the refusal reason in `enforce` (the OMS then rejects the plan with
        nothing reserved and nothing written), None otherwise (`count` only counts what would have been refused). Pure: it writes nothing but the layer's counters."""
        if self.layer.cfg.risk_reservation == "off":
            return None
        sid = self._signal.setdefault(plan.parent_id, len(self._signal) + 1)
        legs = [(c.venue, c.side.value.lower(), float(c.quantity)) for c in plan.children]
        try:
            self.layer._risk_gate(legs, sid)
        except RiskRefused as exc:
            return str(exc)
        self._admitted.add(plan.parent_id)
        return None

    def route_venue(self, plan, order, passive: bool, now: float) -> str:
        """E0 step (d): the venue the layer decides for this order (the OMS builds the router's instruction from it, and the router may not send anywhere else)."""
        it = self._by_order.get(order.order_id)
        role = it.role if it is not None else (order.order_role or Role.PRIMARY)
        role = {"PRIMARY": Role.PRIMARY, "HEDGE": Role.HEDGE, "UNWIND": Role.UNWIND, "FLATTEN": Role.FLATTEN}.get(str(role).upper(), role)
        decision = self.layer.decide_route(role=role, passive=passive, order_venue=order.venue)
        if decision.venue != order.venue:
            self.layer.counts["route_decision_differs_from_order_venue"] += 1
        if decision.venue != order.venue and self.layer.journal.room():
            self.layer.journal.append({"seq": len(self.layer.journal), "event": "route_decision_differs", "order_id": order.order_id, "order_venue": order.venue, "decided": decision.venue, "reason": decision.reason, "ts": now})
        return decision.venue

    def route_rejected(self, plan, order, reason: str, now: float) -> None:
        """A route check failed at the router: the order goes back to the layer as a rejection. It is never sent to another venue (one eligible venue, no reroute)."""
        self.counts["route_rejected_back_to_layer"] += 1
        if self.layer.journal.room():
            self.layer.journal.append({"seq": len(self.layer.journal), "event": "route_rejected", "order_id": order.order_id, "venue": order.venue, "reason": reason, "ts": now})

    def plan_opened(self, plan, now: float) -> str | None:
        sid = self._signal.setdefault(plan.parent_id, len(self._signal) + 1)
        specs = [dict(side=c.side.value.lower(), qty=float(c.quantity), venues=(c.venue,), worst_price=self._px(c.limit_price),
                      reason="maker leg" if c.post_only else "taker leg") for c in plan.children]
        try:
            intents = self.layer.open_primary_group(specs, now=now, signal_id=sid, gated=plan.parent_id in self._admitted)
        except RiskRefused as exc:                         # only when the layer's state moved between admit and here (cannot happen inside one OMS call): the OMS rolls the plan back
            self.counts["refused_after_reserve"] += 1
            return str(exc)
        except IntentError as exc:
            self.error_counts["open_primary"] += 1
            if len(self.errors) < 50:
                self.errors.append({"call": "open_primary", "why": str(exc)})
            intents = None
        for c, it in zip(plan.children, intents or []):
            self._child(it, c, now, passive=bool(c.post_only))
            self.counts["primary_children"] += 1
        return None

    def chain_opened(self, plan, chain_id: str, maker, spec: dict, qty, now: float, dust_included: float = 0.0) -> None:
        sid = self._signal.setdefault(plan.parent_id, len(self._signal) + 1)
        px = self._px(spec["limit_price"])
        if maker is None:                      # the hedge of the carried dust alone (owner 16 Nov): no maker, no parent intent; the dust was filled earlier
            it = self._guard("open_hedge", self.layer.open_intent, role=Role.HEDGE, side=spec["side"].value.lower(), qty=float(qty), venues=(spec["venue"],), worst_price=px, now=now,
                             signal_id=sid, parent=None, parent_filled_qty=float(qty), urgency="urgent", reason="hedge of carried dust", dust_included=dust_included, dust_only=True)
        else:
            extra = {"dust_included": dust_included} if dust_included else {}
            it = self._guard("open_hedge", self.layer.open_intent, role=Role.HEDGE, side=spec["side"].value.lower(), qty=float(qty), venues=(spec["venue"],),
                             worst_price=px, now=now, signal_id=sid, parent=self._by_order.get(maker.order_id),
                             parent_filled_qty=float(maker.filled_quantity) + dust_included, urgency="urgent", reason="hedge after maker fill", **extra)
        if it is not None:
            self._by_chain[chain_id] = it
            self._spec_price[it.intent_id] = px

    def hedge_child(self, chain_id: str, order, now: float) -> None:
        it = self._by_chain.get(chain_id)
        if it is None:
            return
        px, spec_px = self._px(order.limit_price), self._spec_price.get(it.intent_id)
        if it.children:
            self._guard("retarget", self.layer.retarget, it, px, qty=float(order.quantity), reason="retry reprice")
        else:
            it.worst_price = px                     # the first child's cap, as sent (see the class note)
        if spec_px is not None and px is not None and ((it.side == "buy" and px > spec_px) or (it.side == "sell" and px < spec_px)):
            self.counts["cap_looser_than_opportunity_price"] += 1
        self._child(it, order, now, passive=False)
        self.counts["hedge_children"] += 1

    def amend_child(self, plan, old, new, now: float) -> None:
        """An amend's replacement order belongs to the SAME intent as the order it replaces (owner 13 Nov): its fills are journaled and consume the intent's reservation, so the layer's fill total and the
        inventory-cap arithmetic count each fill once."""
        it = self._by_order.get(old.order_id)
        if it is not None:
            self._child(it, new, now, passive=bool(new.post_only))

    def unwind_child(self, plan, order, now: float) -> None:
        sid = self._signal.setdefault(plan.parent_id, len(self._signal) + 1)
        it = self._guard("open_unwind", self.layer.open_intent, role=Role.UNWIND, side=order.side.value.lower(), qty=float(order.quantity), venues=(order.venue,),
                         worst_price=self._px(order.limit_price), now=now, signal_id=sid, urgency="urgent", reason="unwind of a residual")
        if it is not None:
            self._child(it, order, now, passive=bool(order.post_only))
            self.counts["unwind_children"] += 1

    def chain_escalated(self, chain_id: str, now: float, why: str) -> None:
        it = self._by_chain.get(chain_id)
        if it is not None:
            self.layer.escalate(it, now, why)

    def order_changed(self, plan, order, terminal: bool, timed_out: bool, now: float) -> None:
        """After the OMS reducer applied an update: book the new fill quantity once, and tell the layer when the child is over."""
        it = self._by_order.get(order.order_id)
        if it is None:
            return
        done = float(order.filled_quantity)
        delta = done - self._filled.get(order.order_id, 0.0)
        if delta > 1e-12:
            self._filled[order.order_id] = done
            price = float(order.average_price) if order.average_price else None      # the cumulative average: exact when the first fill is the only one, else a close reference
            self.layer.note_filled(it, delta, now)
            self.layer.note_fill({"ts": now, "order_id": order.order_id, "child_id": order.order_id, "intent_id": it.intent_id, "role": it.role, "venue": order.venue,
                                  "side": it.side, "qty": delta, "cum_avg_price": price})
        if timed_out and order.order_id not in self._closed:
            self.layer.child_ghosted(order.order_id, now)
        elif terminal and order.order_id not in self._closed:
            self._closed.add(order.order_id)
            self.layer.child_closed(order.order_id)

    def plan_closed(self, plan, now: float) -> None:
        sid = self._signal.get(plan.parent_id)
        if sid is not None:
            self.layer.settle_signal(sid, now, "plan closed")

    def finalize(self, now: float) -> None:
        self.layer.finalize(now)

    def orders_without_intent(self, plans) -> list[str]:
        """E0 step (e): orders that were sent (state beyond CREATED) without an intent id. Must be empty."""
        return [o.order_id for plan in plans for o in plan.children if not o.intent_id and o.state.value != "CREATED"]

    def report(self, plans=None) -> dict:
        if plans is not None:
            missing = self.orders_without_intent(plans)
            self.counts["orders_without_intent"] = len(missing)
        return {"layer": self.layer.report(), "observer_counts": dict(self.counts), "layer_rule_errors": dict(self.error_counts), "layer_rule_error_samples": list(self.errors),
                "observation_only": True}
