"""Minimum risk controls and the strategy kill switch (MR-10, section 7).

The reservation model is package level and atomic: either the whole parent plan
is admitted or none of it is, so a local limit can never leave one leg of a
two-venue opportunity stranded.

The daily-loss limit is measured on realised plus unrealised trading PnL, net
of fees and funding, marked to the live books. The kill switch is tripped
automatically by :class:`KillSwitchSupervisor` on daily loss, residual
breaches, unresolved orders, reconciliation breaks, prolonged stale feeds,
venue outages and gross-exposure breaches; risk-reducing orders are still
admitted so positions can be flattened.
"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Sequence

from trading_platform_poc.minreq.securitymaster import (
    SecurityMaster,
    TradingStatus,
)

BPS = Decimal(10_000)
SECOND_NS = 1_000_000_000
DAY_NS = 86_400 * SECOND_NS


class LimitId(str, Enum):
    KILL_SWITCH = "kill_switch"
    INSTRUMENT_STATUS = "instrument_status"
    INSTRUMENT_RULES = "instrument_rules"
    STALE_MARKET_DATA = "stale_market_data"
    INVALID_BOOK = "invalid_book"
    PRICE_BAND = "price_band"
    MAX_ORDER_NOTIONAL = "max_order_notional"
    MAX_OPPORTUNITY_NOTIONAL = "max_opportunity_notional"
    MAX_OPEN_ORDERS = "max_open_orders"
    MAX_GROSS_NOTIONAL = "max_gross_notional"
    MAX_NET_NOTIONAL = "max_net_notional"
    MAX_VENUE_NOTIONAL = "max_venue_notional"
    MAX_MESSAGE_RATE = "max_message_rate"
    MAX_RESIDUAL_NOTIONAL = "max_residual_notional"
    MAX_RESIDUAL_DURATION = "max_residual_duration"
    MAX_DAILY_LOSS = "max_daily_loss"
    VENUE_SUSPENDED = "venue_suspended"
    NEGATIVE_EDGE = "negative_edge"


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_order_notional: Decimal = Decimal("25000")
    max_opportunity_notional: Decimal = Decimal("50000")
    max_open_orders: int = 20
    max_gross_notional: Decimal = Decimal("200000")
    max_net_notional: Decimal = Decimal("50000")
    max_venue_notional: Decimal = Decimal("120000")
    max_messages_per_second: int = 25
    max_residual_notional: Decimal = Decimal("2500")
    max_residual_duration_ns: int = 5_000_000_000
    max_daily_loss: Decimal = Decimal("2500")
    price_band_bps: Decimal = Decimal("150")
    max_data_age_ns: int = 1_000_000_000
    minimum_net_edge: Decimal = Decimal("0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_order_notional": str(self.max_order_notional),
            "max_opportunity_notional": str(self.max_opportunity_notional),
            "max_open_orders": self.max_open_orders,
            "max_gross_notional": str(self.max_gross_notional),
            "max_net_notional": str(self.max_net_notional),
            "max_venue_notional": str(self.max_venue_notional),
            "max_messages_per_second": self.max_messages_per_second,
            "max_residual_notional": str(self.max_residual_notional),
            "max_residual_duration_ns": self.max_residual_duration_ns,
            "max_daily_loss": str(self.max_daily_loss),
            "price_band_bps": str(self.price_band_bps),
            "max_data_age_ns": self.max_data_age_ns,
            "minimum_net_edge": str(self.minimum_net_edge),
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> RiskLimits:
        defaults = cls()
        def decimal_field(name: str) -> Decimal:
            value = document.get(name)
            return getattr(defaults, name) if value is None else Decimal(str(value))

        return cls(
            max_order_notional=decimal_field("max_order_notional"),
            max_opportunity_notional=decimal_field("max_opportunity_notional"),
            max_open_orders=int(
                document.get("max_open_orders", defaults.max_open_orders)
            ),
            max_gross_notional=decimal_field("max_gross_notional"),
            max_net_notional=decimal_field("max_net_notional"),
            max_venue_notional=decimal_field("max_venue_notional"),
            max_messages_per_second=int(
                document.get(
                    "max_messages_per_second", defaults.max_messages_per_second
                )
            ),
            max_residual_notional=decimal_field("max_residual_notional"),
            max_residual_duration_ns=int(
                document.get(
                    "max_residual_duration_ns", defaults.max_residual_duration_ns
                )
            ),
            max_daily_loss=decimal_field("max_daily_loss"),
            price_band_bps=decimal_field("price_band_bps"),
            max_data_age_ns=int(
                document.get("max_data_age_ns", defaults.max_data_age_ns)
            ),
            minimum_net_edge=decimal_field("minimum_net_edge"),
        )


@dataclass(frozen=True, slots=True)
class RiskCheckRequest:
    """One leg presented for pre-trade validation."""

    instrument_id: str
    venue: str
    side: str
    quantity: Decimal
    price: Decimal
    notional: Decimal
    reference_price: Decimal | None = None
    data_age_ns: int = 0
    book_valid: bool = True
    risk_reducing: bool = False
    order_type: str = "limit"


@dataclass(frozen=True, slots=True)
class RiskDecision:
    decision_id: str
    approved: bool
    reason: str
    limit_id: str
    timestamp_ns: int
    utilization: dict[str, str] = field(default_factory=dict)
    risk_reducing: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "approved": self.approved,
            "reason": self.reason,
            "limit_id": self.limit_id,
            "timestamp_ns": self.timestamp_ns,
            "utilization": self.utilization,
            "risk_reducing": self.risk_reducing,
        }


@dataclass(frozen=True, slots=True)
class Reservation:
    """Package-level capacity held between admission and terminal state."""

    reservation_id: str
    plan_id: str
    timestamp_ns: int
    notional_by_venue: dict[str, Decimal]
    signed_by_instrument: dict[str, Decimal]
    messages: int
    order_count: int

    @property
    def total_notional(self) -> Decimal:
        return sum(self.notional_by_venue.values(), Decimal(0))

    def to_dict(self) -> dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "plan_id": self.plan_id,
            "timestamp_ns": self.timestamp_ns,
            "notional_by_venue": {
                venue: str(value)
                for venue, value in sorted(self.notional_by_venue.items())
            },
            "signed_by_instrument": {
                instrument: str(value)
                for instrument, value in sorted(self.signed_by_instrument.items())
            },
            "messages": self.messages,
            "order_count": self.order_count,
            "total_notional": str(self.total_notional),
        }


class KillSwitch:
    """Strategy stop. Risk-reducing flow may continue when policy allows."""

    def __init__(self, *, allow_risk_reducing: bool = True) -> None:
        self.active = False
        self.allow_risk_reducing = allow_risk_reducing
        self.activated_at_ns: int | None = None
        self.reason: str = ""
        self.activated_by: str = ""
        self.history: list[dict[str, Any]] = []

    def activate(self, timestamp_ns: int, reason: str, actor: str = "operator") -> None:
        self.active = True
        self.activated_at_ns = timestamp_ns
        self.reason = reason
        self.activated_by = actor
        self.history.append(
            {
                "action": "activate",
                "timestamp_ns": timestamp_ns,
                "reason": reason,
                "actor": actor,
            }
        )

    def trip(self, timestamp_ns: int, trigger: str, detail: str = "") -> bool:
        """Automatic activation. Returns True only when this call engaged it."""

        if self.active:
            self.history.append(
                {
                    "action": "trigger",
                    "timestamp_ns": timestamp_ns,
                    "reason": trigger,
                    "detail": detail,
                    "actor": "auto",
                }
            )
            return False
        self.activate(timestamp_ns, trigger, actor="auto")
        if detail:
            self.history[-1]["detail"] = detail
        return True

    def clear(self, timestamp_ns: int, actor: str, *, reconciled: bool) -> None:
        if not reconciled:
            raise PermissionError(
                "kill switch cannot be cleared before reconciliation completes"
            )
        self.active = False
        self.activated_at_ns = None
        self.reason = ""
        self.history.append(
            {"action": "clear", "timestamp_ns": timestamp_ns, "actor": actor}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "allow_risk_reducing": self.allow_risk_reducing,
            "activated_at_ns": self.activated_at_ns,
            "reason": self.reason,
            "activated_by": self.activated_by,
            "history": list(self.history),
        }


class RiskEngine:
    """Pre-trade validation, exposure tracking and atomic reservation."""

    def __init__(
        self,
        limits: RiskLimits,
        security_master: SecurityMaster,
        *,
        kill_switch: KillSwitch | None = None,
    ) -> None:
        self.limits = limits
        self.security_master = security_master
        self.kill_switch = kill_switch or KillSwitch()
        self.position_by_instrument: dict[str, Decimal] = {}
        self.notional_by_venue: dict[str, Decimal] = {}
        self._positions: dict[tuple[str, str], list[Decimal]] = {}
        self._marks: dict[tuple[str, str], Decimal] = {}
        self.trading_realized = Decimal(0)
        self.fees_paid = Decimal(0)
        self.other_pnl = Decimal(0)
        self._day: int | None = None
        self._day_start_pnl = Decimal(0)
        self.risk_holds: dict[str, dict[str, Any]] = {}
        self.open_orders = 0
        self.suspended_venues: set[str] = set()
        self._reservations: dict[str, Reservation] = {}
        self._messages: deque[int] = deque()
        self.binding_events: list[dict[str, Any]] = []
        self.decisions: list[RiskDecision] = []

    # ------------------------------------------------------------------ pnl
    @property
    def realized_pnl(self) -> Decimal:
        """Realised trading PnL net of fees and funding."""

        return self.trading_realized - self.fees_paid + self.other_pnl

    @property
    def unrealized_pnl(self) -> Decimal:
        total = Decimal(0)
        for key, (quantity, cost_basis) in self._positions.items():
            if quantity == 0:
                continue
            mark = self._marks.get(key)
            if mark is None:
                continue
            total += quantity * mark - cost_basis
        return total

    @property
    def total_pnl(self) -> Decimal:
        return self.realized_pnl + self.unrealized_pnl

    def daily_pnl(self, timestamp_ns: int) -> Decimal:
        """PnL since the start of the UTC day that contains ``timestamp_ns``."""

        day = timestamp_ns // DAY_NS
        if self._day != day:
            if self._day is not None:
                self._day_start_pnl = self.total_pnl
            self._day = day
        return self.total_pnl - self._day_start_pnl

    def mark(self, instrument_id: str, venue: str, price: Decimal | None) -> None:
        if price is not None and price > 0:
            self._marks[(instrument_id, venue)] = price

    def position(self, instrument_id: str, venue: str) -> Decimal:
        entry = self._positions.get((instrument_id, venue))
        return entry[0] if entry else Decimal(0)

    def positions(self) -> dict[tuple[str, str], Decimal]:
        return {
            key: entry[0] for key, entry in sorted(self._positions.items()) if entry[0] != 0
        }

    def held_gross_notional(self) -> Decimal:
        total = Decimal(0)
        for key, (quantity, cost_basis) in self._positions.items():
            if quantity == 0:
                continue
            mark = self._marks.get(key)
            total += abs(quantity * mark) if mark is not None else abs(cost_basis)
        return total

    # ------------------------------------------------------------- exposures
    @property
    def gross_notional(self) -> Decimal:
        return sum(
            (abs(value) for value in self.notional_by_venue.values()), Decimal(0)
        ) + sum(
            (
                abs(reservation.total_notional)
                for reservation in self._reservations.values()
            ),
            Decimal(0),
        )

    @property
    def net_notional(self) -> Decimal:
        return sum(self.notional_by_venue.values(), Decimal(0))

    def venue_notional(self, venue: str) -> Decimal:
        held = abs(self.notional_by_venue.get(venue, Decimal(0)))
        reserved = sum(
            (
                abs(reservation.notional_by_venue.get(venue, Decimal(0)))
                for reservation in self._reservations.values()
            ),
            Decimal(0),
        )
        return held + reserved

    @property
    def reserved_order_count(self) -> int:
        return sum(
            reservation.order_count for reservation in self._reservations.values()
        )

    def utilization(self) -> dict[str, str]:
        return {
            "gross_notional": str(self.gross_notional),
            "gross_limit": str(self.limits.max_gross_notional),
            "net_notional": str(self.net_notional),
            "net_limit": str(self.limits.max_net_notional),
            "open_orders": str(self.open_orders + self.reserved_order_count),
            "open_order_limit": str(self.limits.max_open_orders),
            "realized_pnl": str(self.realized_pnl),
            "unrealized_pnl": str(self.unrealized_pnl),
            "fees_paid": str(self.fees_paid),
            "daily_loss_limit": str(self.limits.max_daily_loss),
            "active_reservations": str(len(self._reservations)),
            "risk_holds": str(len(self.risk_holds)),
        }

    def hold_new_risk(
        self, key: str, limit: LimitId, reason: str, timestamp_ns: int
    ) -> None:
        """Block non-reducing orders until ``release_hold(key)``."""

        if key not in self.risk_holds:
            self.risk_holds[key] = {
                "limit_id": limit.value,
                "reason": reason,
                "timestamp_ns": timestamp_ns,
            }

    def release_hold(self, key: str) -> None:
        self.risk_holds.pop(key, None)

    def suspend_venue(self, venue: str) -> None:
        self.suspended_venues.add(venue)

    def resume_venue(self, venue: str) -> None:
        self.suspended_venues.discard(venue)

    # ---------------------------------------------------------------- checks
    def check(
        self,
        request: RiskCheckRequest,
        timestamp_ns: int,
        *,
        expected_net_edge: Decimal | None = None,
    ) -> RiskDecision:
        limit, reason = self._first_breach(request, timestamp_ns, expected_net_edge)
        decision = RiskDecision(
            decision_id=_decision_id(request, timestamp_ns),
            approved=limit is None,
            reason=reason,
            limit_id="" if limit is None else limit.value,
            timestamp_ns=timestamp_ns,
            utilization=self.utilization(),
            risk_reducing=request.risk_reducing,
        )
        self.decisions.append(decision)
        if limit is not None:
            self.binding_events.append(
                {
                    "timestamp_ns": timestamp_ns,
                    "limit_id": limit.value,
                    "reason": reason,
                    "instrument_id": request.instrument_id,
                    "venue": request.venue,
                }
            )
        return decision

    def _first_breach(
        self,
        request: RiskCheckRequest,
        timestamp_ns: int,
        expected_net_edge: Decimal | None,
    ) -> tuple[LimitId | None, str]:
        allow_reducing = (
            request.risk_reducing and self.kill_switch.allow_risk_reducing
        )
        if self.kill_switch.active and not allow_reducing:
            return LimitId.KILL_SWITCH, "kill switch is active"
        if self.risk_holds and not allow_reducing:
            key, hold = min(self.risk_holds.items())
            return LimitId(hold["limit_id"]), f"new risk held by {key}: {hold['reason']}"
        if request.venue in self.suspended_venues and not allow_reducing:
            return LimitId.VENUE_SUSPENDED, f"venue {request.venue} is suspended"
        if not request.book_valid:
            return LimitId.INVALID_BOOK, "book is invalid"
        if request.data_age_ns > self.limits.max_data_age_ns:
            return (
                LimitId.STALE_MARKET_DATA,
                f"market data age {request.data_age_ns}ns exceeds limit",
            )

        try:
            instrument = self.security_master.resolve(
                request.instrument_id, timestamp_ns
            )
        except Exception:
            return LimitId.INSTRUMENT_RULES, "instrument is not in the master"
        if instrument.status is not TradingStatus.TRADING and not allow_reducing:
            return (
                LimitId.INSTRUMENT_STATUS,
                f"instrument status is {instrument.status.value}",
            )

        sizing = self.security_master.size_order(
            request.instrument_id,
            at_ns=timestamp_ns,
            side=request.side,
            target_quantity=request.quantity,
            price=request.price,
            reference_price=request.reference_price,
            order_type=request.order_type,
            allow_risk_reducing=allow_reducing,
        )
        if not sizing.eligible:
            return LimitId.INSTRUMENT_RULES, sizing.reason

        if request.reference_price is not None and request.reference_price > 0:
            deviation = (
                abs(request.price - request.reference_price)
                / request.reference_price
                * BPS
            )
            if deviation > self.limits.price_band_bps:
                return (
                    LimitId.PRICE_BAND,
                    f"price deviates {deviation:.2f}bps from reference",
                )

        if request.notional > self.limits.max_order_notional:
            return (
                LimitId.MAX_ORDER_NOTIONAL,
                f"order notional {request.notional} exceeds limit",
            )
        if not self._message_headroom(timestamp_ns):
            return LimitId.MAX_MESSAGE_RATE, "message rate limit reached"
        if allow_reducing:
            # Exposure, loss and edge limits exist to stop new risk. An order
            # that only reduces exposure must stay possible when they bind,
            # otherwise the controls would trap the position they protect.
            return None, "approved_risk_reducing"
        if (
            self.open_orders + self.reserved_order_count + 1
            > self.limits.max_open_orders
        ):
            return LimitId.MAX_OPEN_ORDERS, "open order limit reached"
        if self.gross_notional + request.notional > self.limits.max_gross_notional:
            return LimitId.MAX_GROSS_NOTIONAL, "gross exposure limit reached"
        signed = request.notional if request.side == "buy" else -request.notional
        if abs(self.net_notional + signed) > self.limits.max_net_notional:
            return LimitId.MAX_NET_NOTIONAL, "net exposure limit reached"
        if (
            self.venue_notional(request.venue) + request.notional
            > self.limits.max_venue_notional
        ):
            return (
                LimitId.MAX_VENUE_NOTIONAL,
                f"venue exposure limit reached for {request.venue}",
            )
        daily = self.daily_pnl(timestamp_ns)
        if daily <= -self.limits.max_daily_loss:
            return LimitId.MAX_DAILY_LOSS, f"daily loss {daily} reached the limit"
        if (
            expected_net_edge is not None
            and expected_net_edge <= self.limits.minimum_net_edge
        ):
            return (
                LimitId.NEGATIVE_EDGE,
                f"expected net edge {expected_net_edge} is not positive",
            )
        return None, "approved"

    def _message_headroom(self, timestamp_ns: int) -> bool:
        window_start = timestamp_ns - SECOND_NS
        while self._messages and self._messages[0] < window_start:
            self._messages.popleft()
        return len(self._messages) < self.limits.max_messages_per_second

    def record_message(self, timestamp_ns: int) -> None:
        self._messages.append(timestamp_ns)

    def message_headroom(self, timestamp_ns: int) -> int:
        window_start = timestamp_ns - SECOND_NS
        while self._messages and self._messages[0] < window_start:
            self._messages.popleft()
        return max(0, self.limits.max_messages_per_second - len(self._messages))

    # ---------------------------------------------------------- reservations
    def reserve(
        self,
        plan_id: str,
        requests: Sequence[RiskCheckRequest],
        timestamp_ns: int,
        *,
        expected_net_edge: Decimal | None = None,
    ) -> tuple[Reservation | None, tuple[RiskDecision, ...]]:
        """Admit the whole package or nothing at all.

        Every leg is validated against the state that already includes the
        other legs of the same package, which is what prevents a partially
        admitted two-leg opportunity.
        """

        decisions: list[RiskDecision] = []
        notional_by_venue: dict[str, Decimal] = {}
        signed_by_instrument: dict[str, Decimal] = {}
        probe_key = f"__probe__{plan_id}"
        total_notional = sum((item.notional for item in requests), Decimal(0))
        if total_notional > self.limits.max_opportunity_notional:
            decision = RiskDecision(
                decision_id=_plan_decision_id(plan_id, timestamp_ns),
                approved=False,
                reason=f"opportunity notional {total_notional} exceeds limit",
                limit_id=LimitId.MAX_OPPORTUNITY_NOTIONAL.value,
                timestamp_ns=timestamp_ns,
                utilization=self.utilization(),
            )
            self.decisions.append(decision)
            self.binding_events.append(
                {
                    "timestamp_ns": timestamp_ns,
                    "limit_id": LimitId.MAX_OPPORTUNITY_NOTIONAL.value,
                    "reason": decision.reason,
                    "plan_id": plan_id,
                }
            )
            return None, (decision,)

        for index, request in enumerate(requests):
            self._reservations[probe_key] = Reservation(
                reservation_id=probe_key,
                plan_id=plan_id,
                timestamp_ns=timestamp_ns,
                notional_by_venue=dict(notional_by_venue),
                signed_by_instrument=dict(signed_by_instrument),
                messages=index,
                order_count=index,
            )
            decision = self.check(
                request,
                timestamp_ns,
                expected_net_edge=expected_net_edge if index == 0 else None,
            )
            decisions.append(decision)
            if not decision.approved:
                self._reservations.pop(probe_key, None)
                return None, tuple(decisions)
            notional_by_venue[request.venue] = notional_by_venue.get(
                request.venue, Decimal(0)
            ) + request.notional
            signed = request.notional if request.side == "buy" else -request.notional
            signed_by_instrument[request.instrument_id] = signed_by_instrument.get(
                request.instrument_id, Decimal(0)
            ) + signed

        self._reservations.pop(probe_key, None)
        reservation = Reservation(
            reservation_id=f"res-{plan_id}",
            plan_id=plan_id,
            timestamp_ns=timestamp_ns,
            notional_by_venue=notional_by_venue,
            signed_by_instrument=signed_by_instrument,
            messages=len(requests),
            order_count=len(requests),
        )
        self._reservations[reservation.reservation_id] = reservation
        for _ in requests:
            self.record_message(timestamp_ns)
        return reservation, tuple(decisions)

    def release(self, reservation: Reservation | None) -> None:
        if reservation is not None:
            self._reservations.pop(reservation.reservation_id, None)

    def active_reservations(self) -> tuple[Reservation, ...]:
        return tuple(
            reservation
            for key, reservation in sorted(self._reservations.items())
            if not key.startswith("__probe__")
        )

    # ------------------------------------------------------------ accounting
    def apply_fill(
        self,
        instrument_id: str,
        venue: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        *,
        fee: Decimal = Decimal(0),
    ) -> None:
        signed_quantity = quantity if side == "buy" else -quantity
        self.fees_paid += fee
        self._marks.setdefault((instrument_id, venue), price)
        entry = self._positions.setdefault((instrument_id, venue), [Decimal(0), Decimal(0)])
        current, cost_basis = entry
        if current == 0 or (current > 0) == (signed_quantity > 0):
            entry[0] = current + signed_quantity
            entry[1] = cost_basis + signed_quantity * price
        else:
            average = cost_basis / current
            closing = min(abs(signed_quantity), abs(current))
            direction = Decimal(1) if current > 0 else Decimal(-1)
            self.trading_realized += (price - average) * closing * direction
            entry[0] = current + signed_quantity
            remaining = abs(signed_quantity) - closing
            if entry[0] == 0:
                entry[1] = Decimal(0)
            elif remaining > 0:
                entry[1] = entry[0] * price
            else:
                entry[1] = average * entry[0]
        self.position_by_instrument[instrument_id] = (
            self.position_by_instrument.get(instrument_id, Decimal(0))
            + signed_quantity
        )
        self.notional_by_venue[venue] = (
            self.notional_by_venue.get(venue, Decimal(0))
            + signed_quantity * price
        )

    def apply_pnl(self, amount: Decimal) -> None:
        """Funding, rebates or other cash PnL outside trade fills."""

        self.other_pnl += amount

    def residual_breach(
        self, residual_notional: Decimal, residual_age_ns: int
    ) -> tuple[LimitId | None, str]:
        if residual_notional > self.limits.max_residual_notional:
            return (
                LimitId.MAX_RESIDUAL_NOTIONAL,
                f"residual notional {residual_notional} exceeds limit",
            )
        if residual_age_ns > self.limits.max_residual_duration_ns:
            return (
                LimitId.MAX_RESIDUAL_DURATION,
                f"residual age {residual_age_ns}ns exceeds limit",
            )
        return None, "within_residual_bounds"


def _decision_id(request: RiskCheckRequest, timestamp_ns: int) -> str:
    key = (
        f"{request.instrument_id}|{request.venue}|{request.side}"
        f"|{request.quantity}|{request.price}|{timestamp_ns}"
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def _plan_decision_id(plan_id: str, timestamp_ns: int) -> str:
    return hashlib.sha256(f"{plan_id}|{timestamp_ns}".encode("utf-8")).hexdigest()[
        :24
    ]


RISK_LIMIT_MATRIX: tuple[dict[str, str], ...] = (
    {
        "limit_id": LimitId.KILL_SWITCH.value,
        "scope": "strategy",
        "control": "Strategy stop and cancel-all.",
        "order_state_effect": "New orders rejected; open orders cancelled; "
        "risk-reducing orders continue only when policy allows.",
        "owner": "risk/operations",
        "severity": "critical",
    },
    {
        "limit_id": LimitId.INSTRUMENT_STATUS.value,
        "scope": "instrument",
        "control": "Trading status from the security master.",
        "order_state_effect": "Reject unless the order is risk reducing.",
        "owner": "platform engineering",
        "severity": "high",
    },
    {
        "limit_id": LimitId.INSTRUMENT_RULES.value,
        "scope": "instrument",
        "control": "Tick, lot, minimum quantity, minimum notional, order type.",
        "order_state_effect": "Reject before a parent plan is created.",
        "owner": "platform engineering",
        "severity": "high",
    },
    {
        "limit_id": LimitId.STALE_MARKET_DATA.value,
        "scope": "venue",
        "control": "Maximum market-data age.",
        "order_state_effect": "No new opportunities; open legs cancelled.",
        "owner": "platform engineering",
        "severity": "high",
    },
    {
        "limit_id": LimitId.INVALID_BOOK.value,
        "scope": "venue",
        "control": "Book validity from the integrity monitor.",
        "order_state_effect": "Suspend the venue until a snapshot resync.",
        "owner": "platform engineering",
        "severity": "high",
    },
    {
        "limit_id": LimitId.PRICE_BAND.value,
        "scope": "order",
        "control": "Maximum deviation from the reference mid.",
        "order_state_effect": "Reject the child order.",
        "owner": "strategy owner",
        "severity": "medium",
    },
    {
        "limit_id": LimitId.MAX_ORDER_NOTIONAL.value,
        "scope": "order",
        "control": "Maximum single-order notional.",
        "order_state_effect": "Reject the child order.",
        "owner": "risk/operations",
        "severity": "medium",
    },
    {
        "limit_id": LimitId.MAX_OPPORTUNITY_NOTIONAL.value,
        "scope": "parent plan",
        "control": "Maximum package notional.",
        "order_state_effect": "Reject the whole plan; no partial admission.",
        "owner": "risk/operations",
        "severity": "medium",
    },
    {
        "limit_id": LimitId.MAX_OPEN_ORDERS.value,
        "scope": "strategy",
        "control": "Maximum concurrent open orders including reservations.",
        "order_state_effect": "Reject the plan.",
        "owner": "risk/operations",
        "severity": "medium",
    },
    {
        "limit_id": LimitId.MAX_GROSS_NOTIONAL.value,
        "scope": "strategy",
        "control": "Gross exposure across venues.",
        "order_state_effect": "Reject the plan; utilisation exposed in metrics.",
        "owner": "risk/operations",
        "severity": "high",
    },
    {
        "limit_id": LimitId.MAX_NET_NOTIONAL.value,
        "scope": "strategy",
        "control": "Net directional exposure.",
        "order_state_effect": "Reject the plan.",
        "owner": "risk/operations",
        "severity": "high",
    },
    {
        "limit_id": LimitId.MAX_VENUE_NOTIONAL.value,
        "scope": "venue",
        "control": "Per-venue exposure.",
        "order_state_effect": "Reject the plan or reroute to another venue.",
        "owner": "risk/operations",
        "severity": "medium",
    },
    {
        "limit_id": LimitId.MAX_MESSAGE_RATE.value,
        "scope": "venue",
        "control": "Message rate and rate-limit headroom.",
        "order_state_effect": "Queue or reject; binding event recorded.",
        "owner": "platform engineering",
        "severity": "medium",
    },
    {
        "limit_id": LimitId.MAX_RESIDUAL_NOTIONAL.value,
        "scope": "parent plan",
        "control": "Maximum one-sided residual exposure, checked continuously.",
        "order_state_effect": "Plan moves to residual_breach and cannot close; "
        "new risk is held and the unwind is forced with escalating aggression "
        "and rate-limit-aware backoff; the kill switch trips if the breach is "
        "still open after the escalation window.",
        "owner": "strategy owner",
        "severity": "high",
    },
    {
        "limit_id": LimitId.MAX_RESIDUAL_DURATION.value,
        "scope": "parent plan",
        "control": "Maximum residual duration from the first unhedged fill.",
        "order_state_effect": "Plan moves to residual_breach and cannot close; "
        "new risk is held and the unwind is forced with escalating aggression "
        "and rate-limit-aware backoff; the kill switch trips if the breach is "
        "still open after the escalation window.",
        "owner": "strategy owner",
        "severity": "high",
    },
    {
        "limit_id": LimitId.MAX_DAILY_LOSS.value,
        "scope": "strategy",
        "control": "Daily loss on realised plus unrealised PnL net of fees "
        "and funding, marked to the live books.",
        "order_state_effect": "Kill switch trips automatically; working orders "
        "are cancelled and positions are flattened with risk-reducing orders.",
        "owner": "risk/operations",
        "severity": "critical",
    },
    {
        "limit_id": LimitId.NEGATIVE_EDGE.value,
        "scope": "parent plan",
        "control": "Estimated net edge after fees, slippage and latency.",
        "order_state_effect": "Reject the route.",
        "owner": "strategy owner",
        "severity": "medium",
    },
    {
        "limit_id": LimitId.VENUE_SUSPENDED.value,
        "scope": "venue",
        "control": "Operator or feed-integrity suspension of a single venue.",
        "order_state_effect": (
            "New orders on the venue are rejected; open orders are cancelled "
            "and only risk-reducing flow is allowed until the venue resumes."
        ),
        "owner": "operations",
        "severity": "high",
    },
)


def risk_limit_matrix() -> list[dict[str, str]]:
    """Section 10 deliverable: limit matrix and control-to-order-state behaviour."""

    return [dict(row) for row in RISK_LIMIT_MATRIX]


@dataclass(frozen=True, slots=True)
class AutoKillPolicy:
    """Conditions that trip the kill switch without an operator."""

    enabled: bool = True
    daily_loss: bool = True
    residual_breach: bool = True
    unresolved_order: bool = True
    reconciliation_break: bool = True
    residual_kill_after_ns: int = 5_000_000_000
    stale_feed_ns: int = 30_000_000_000
    venue_down_ns: int = 30_000_000_000
    max_consecutive_venue_rejects: int = 5
    gross_exposure_factor: Decimal = Decimal("1.0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "daily_loss": self.daily_loss,
            "residual_breach": self.residual_breach,
            "unresolved_order": self.unresolved_order,
            "reconciliation_break": self.reconciliation_break,
            "residual_kill_after_ns": self.residual_kill_after_ns,
            "stale_feed_ns": self.stale_feed_ns,
            "venue_down_ns": self.venue_down_ns,
            "max_consecutive_venue_rejects": self.max_consecutive_venue_rejects,
            "gross_exposure_factor": str(self.gross_exposure_factor),
        }


class KillSwitchSupervisor:
    """Evaluates automatic kill triggers and trips the kill switch once."""

    def __init__(
        self, risk_engine: RiskEngine, policy: AutoKillPolicy | None = None
    ) -> None:
        self.risk_engine = risk_engine
        self.policy = policy or AutoKillPolicy()
        self.fired: list[dict[str, Any]] = []
        self.venue_down_since: dict[str, int] = {}
        self._tripped: set[str] = set()
        self._seen_unresolved = 0

    def venue_down(self, venue: str, timestamp_ns: int) -> None:
        self.venue_down_since.setdefault(venue, timestamp_ns)

    def venue_up(self, venue: str) -> None:
        self.venue_down_since.pop(venue, None)
        self._tripped.discard(f"venue_down:{venue}")

    def reset(self) -> None:
        """Called after an operator clears the kill switch post-reconciliation."""

        self._tripped.clear()

    def _trip(self, timestamp_ns: int, key: str, detail: str) -> dict[str, Any] | None:
        if key in self._tripped:
            return None
        self._tripped.add(key)
        engaged = self.risk_engine.kill_switch.trip(timestamp_ns, key, detail)
        record = {
            "timestamp_ns": timestamp_ns,
            "trigger": key,
            "detail": detail,
            "engaged_kill_switch": engaged,
        }
        self.fired.append(record)
        self.risk_engine.binding_events.append(
            {
                "timestamp_ns": timestamp_ns,
                "limit_id": LimitId.KILL_SWITCH.value,
                "reason": f"auto:{key}",
                "detail": detail,
            }
        )
        return record

    def evaluate(
        self,
        timestamp_ns: int,
        *,
        feed_ages_ns: Mapping[str, int] | None = None,
        venue_reject_streaks: Mapping[str, int] | None = None,
        open_residual_breaches: Sequence[Mapping[str, Any]] = (),
        unresolved_orders: Sequence[Mapping[str, Any]] = (),
        reconciliation_breaks: int = 0,
    ) -> list[dict[str, Any]]:
        if not self.policy.enabled:
            return []
        fired: list[dict[str, Any]] = []

        def trip(key: str, detail: str) -> None:
            record = self._trip(timestamp_ns, key, detail)
            if record is not None:
                fired.append(record)

        engine = self.risk_engine
        if self.policy.daily_loss:
            daily = engine.daily_pnl(timestamp_ns)
            if daily <= -engine.limits.max_daily_loss:
                trip("daily_loss", f"daily pnl {daily} <= -{engine.limits.max_daily_loss}")
        if self.policy.residual_breach:
            # A breach first holds new risk and forces the unwind (router).
            # It becomes a strategy stop only if it is still open afterwards.
            for breach in open_residual_breaches:
                age = timestamp_ns - int(breach.get("breached_ns", timestamp_ns))
                if age > self.policy.residual_kill_after_ns:
                    trip(
                        f"residual_breach_unresolved:{breach.get('plan_id')}",
                        f"exposure {breach.get('net_exposure')} open {age}ns after breach",
                    )
        if self.policy.unresolved_order and len(unresolved_orders) > self._seen_unresolved:
            latest = unresolved_orders[-1]
            self._seen_unresolved = len(unresolved_orders)
            trip(
                f"unresolved_order:{latest.get('client_order_id')}",
                f"venue {latest.get('venue')} never confirmed the order",
            )
        if self.policy.reconciliation_break and reconciliation_breaks > 0:
            trip("reconciliation_break", f"{reconciliation_breaks} breaks")
        for venue, age in sorted((feed_ages_ns or {}).items()):
            if age > self.policy.stale_feed_ns:
                trip(f"stale_feed:{venue}", f"no market data for {age}ns")
        for venue, since in sorted(self.venue_down_since.items()):
            if timestamp_ns - since > self.policy.venue_down_ns:
                trip(f"venue_down:{venue}", f"down since {since}")
        for venue, streak in sorted((venue_reject_streaks or {}).items()):
            if streak >= self.policy.max_consecutive_venue_rejects:
                trip(f"venue_rejects:{venue}", f"{streak} consecutive connectivity rejects")
        held = engine.held_gross_notional()
        if held > engine.limits.max_gross_notional * self.policy.gross_exposure_factor:
            trip("gross_exposure", f"held gross {held} above limit")
        return fired

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy.to_dict(),
            "fired": list(self.fired),
            "venue_down_since": dict(sorted(self.venue_down_since.items())),
        }
