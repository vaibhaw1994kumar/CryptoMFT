from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import StrEnum
import json
from pathlib import Path
from typing import Any

from .models import ChildOrder, Opportunity, OrderState, ParentPlan, stable_id


UNBOUNDED = Decimal("Infinity")


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_order_notional: Decimal
    max_gross_notional: Decimal
    max_residual_base: Decimal
    max_book_age_ns: int
    max_position_base: Decimal = UNBOUNDED
    # Worst-case one-sided base exposure: every admitted package reserves its
    # full leg quantity (one leg may fill while the other fails), and that sum
    # plus the residual already carried from closed plans must stay within
    # this limit. Distinct from ``max_residual_base``, the residual a plan may
    # be left holding without an unwind.
    max_legging_exposure_base: Decimal = UNBOUNDED
    max_cash_reserved: Decimal = UNBOUNDED
    max_margin_reserved: Decimal = UNBOUNDED
    max_message_capacity: int = 1_000_000_000
    max_price_deviation_bps: Decimal = UNBOUNDED
    max_strategy_package_notional: Decimal = UNBOUNDED
    max_strategy_gross_notional: Decimal = UNBOUNDED
    max_strategy_net_notional: Decimal = UNBOUNDED
    max_instrument_gross_notional: Decimal = UNBOUNDED
    max_venue_gross_notional: Decimal = UNBOUNDED
    strategy_gross_limits: dict[str, Decimal] = field(default_factory=dict)
    strategy_net_limits: dict[str, Decimal] = field(default_factory=dict)
    instrument_gross_limits: dict[str, Decimal] = field(default_factory=dict)
    venue_gross_limits: dict[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AccountRiskSnapshot:
    """Paper or simulated account capacity observed before package admission."""

    venue: str
    account_enabled: bool = True
    available_cash: Decimal = UNBOUNDED
    available_margin: Decimal = UNBOUNDED
    position_base: Decimal = Decimal("0")
    max_abs_position_base: Decimal = UNBOUNDED
    message_capacity: int = 1_000_000_000
    message_used: int = 0
    initial_margin_rate: Decimal = Decimal("0")
    margin_enabled: bool = False
    collateral_balance: Decimal = UNBOUNDED
    maintenance_margin_rate: Decimal = Decimal("0")
    liquidation_buffer_rate: Decimal = Decimal("0.25")
    venue_enabled: bool = True
    strategy_enabled: bool = True
    max_order_notional: Decimal = UNBOUNDED


class PreTradeDecision(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ExposureControlState(StrEnum):
    NORMAL = "NORMAL"
    BREACHED = "BREACHED"


class KillSwitchState(StrEnum):
    INACTIVE = "INACTIVE"
    ACTIVE = "ACTIVE"


class MarginState(StrEnum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    HEALTHY = "HEALTHY"
    WARNING = "WARNING"
    BREACH = "BREACH"


@dataclass(frozen=True, slots=True)
class VenueMarginState:
    venue: str
    state: MarginState
    margin_enabled: bool
    gross_notional: Decimal
    margin_balance: Decimal
    venue_reported_margin_balance: Decimal | None
    effective_margin_balance: Decimal
    initial_margin_required: Decimal
    maintenance_margin_required: Decimal
    liquidation_buffer: Decimal
    required_liquidation_buffer: Decimal
    maintenance_margin_utilization: Decimal
    alert_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["state"] = self.state.value
        for key, value in tuple(row.items()):
            if isinstance(value, Decimal):
                row[key] = str(value)
        row["alert_codes"] = list(self.alert_codes)
        return row


@dataclass(frozen=True, slots=True)
class MarginReport:
    report_id: str
    ts_ns: int
    state: MarginState
    venues: tuple[VenueMarginState, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "report_id": self.report_id,
            "ts_ns": self.ts_ns,
            "state": self.state.value,
            "alerts": [
                {"venue": venue.venue, "code": code}
                for venue in self.venues for code in venue.alert_codes
            ],
            "venues": {venue.venue: venue.to_dict() for venue in self.venues},
        }

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )


@dataclass(frozen=True, slots=True)
class ExposureLimitTransition:
    transition_id: str
    ts_ns: int
    previous_state: ExposureControlState
    state: ExposureControlState
    binding_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id,
            "ts_ns": self.ts_ns,
            "previous_state": self.previous_state.value,
            "state": self.state.value,
            "binding_codes": list(self.binding_codes),
        }


@dataclass(frozen=True, slots=True)
class PreTradeCheck:
    code: str
    scope: str
    passed: bool
    venue: str = ""
    instrument_id: str = ""
    observed: str = ""
    limit: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PreTradeValidationReport:
    validation_id: str
    ts_ns: int
    opportunity_id: str
    strategy_id: str
    decision: PreTradeDecision
    checks: tuple[PreTradeCheck, ...]

    @property
    def failures(self) -> tuple[PreTradeCheck, ...]:
        return tuple(check for check in self.checks if not check.passed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "validation_id": self.validation_id,
            "ts_ns": self.ts_ns,
            "opportunity_id": self.opportunity_id,
            "strategy_id": self.strategy_id,
            "decision": self.decision.value,
            "checks": [check.to_dict() for check in self.checks],
            "failure_codes": [check.code for check in self.failures],
        }

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )


class ReconciliationDecision(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


TRADE_BREAK_CODES = {
    "MISSING_LOCAL_FILL",
    "EXTRA_LOCAL_FILL",
    "CONFLICTING_ORDER_STATE",
    "INCOMPLETE_OPPORTUNITY",
    "MISSING_VENUE_ORDER",
    "ORPHAN_VENUE_ORDER",
}


@dataclass(frozen=True, slots=True)
class VenueRiskSnapshot:
    """REST/private-stream account state relative to the reconciliation session."""

    venue: str
    instrument_id: str
    position_base: Decimal
    balance_delta: Decimal
    mark_price: Decimal | None = None
    average_price: Decimal | None = None
    realized_pnl: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    initial_capital: Decimal = Decimal("0")
    available_cash: Decimal | None = None
    available_capital: Decimal | None = None
    reserved_cash: Decimal | None = None
    reserved_margin: Decimal | None = None
    reserved_position: Decimal | None = None
    reserved_messages: int | None = None
    account_enabled: bool = True
    venue_enabled: bool = True
    strategy_enabled: bool = True
    observed_ts_ns: int = 0
    margin_balance: Decimal | None = None
    initial_margin_required: Decimal | None = None
    maintenance_margin_required: Decimal | None = None
    liquidation_buffer: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    reconciliation_id: str
    ts_ns: int
    decision: ReconciliationDecision
    issues: tuple[dict[str, Any], ...]
    local_orders: int
    venue_orders: int
    fills: int
    venue_snapshots: int
    suspended_venues: tuple[str, ...]
    resolved_venues: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "reconciliation_id": self.reconciliation_id,
            "ts_ns": self.ts_ns,
            "decision": self.decision.value,
            "issues": list(self.issues),
            "venue_controls": {
                "suspended": list(self.suspended_venues),
                "resolved": list(self.resolved_venues),
            },
            "counts": {
                "local_orders": self.local_orders,
                "venue_orders": self.venue_orders,
                "fills": self.fills,
                "venue_snapshots": self.venue_snapshots,
                "issues": len(self.issues),
            },
        }

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )


class InventoryModel(StrEnum):
    # Legacy: a spot sell is admitted against the position limit only, so the
    # simulation may sell base it does not hold (an implicit short).
    UNCONSTRAINED = "UNCONSTRAINED"
    # Each venue starts with explicit base and quote balances; a spot sell
    # needs held base, a spot buy needs held quote. No inter-venue transfers.
    PRE_FUNDED = "PRE_FUNDED"


@dataclass(frozen=True, slots=True)
class InventoryPolicy:
    model: InventoryModel = InventoryModel.UNCONSTRAINED
    instrument_id: str = ""
    initial_base_by_venue: dict[str, Decimal] = field(default_factory=dict)
    initial_quote_by_venue: dict[str, Decimal] = field(default_factory=dict)

    @property
    def label(self) -> str:
        if self.model == InventoryModel.PRE_FUNDED:
            return "PRE_FUNDED_INVENTORY_NO_REBALANCING"
        return "UNCONSTRAINED_SPOT_SHORTING_ALLOWED"


@dataclass(slots=True)
class PackageReservation:
    reservation_id: str
    opportunity_id: str
    gross_notional: Decimal
    cash_by_venue: dict[str, Decimal]
    margin_by_venue: dict[str, Decimal]
    position_delta_by_venue: dict[str, Decimal]
    messages_by_venue: dict[str, int]
    residual_base: Decimal
    active: bool = True
    base_by_venue: dict[str, Decimal] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        for field_name in (
            "gross_notional", "cash_by_venue", "margin_by_venue",
            "position_delta_by_venue", "residual_base", "base_by_venue",
        ):
            value = row[field_name]
            if isinstance(value, Decimal):
                row[field_name] = str(value)
            elif isinstance(value, dict):
                row[field_name] = {key: str(item) for key, item in value.items()}
        return row


class RiskEngine:
    """All-or-none local admission for an intended multi-leg package."""

    def __init__(
        self,
        limits: RiskLimits,
        account_snapshots: dict[str, AccountRiskSnapshot] | None = None,
        inventory: InventoryPolicy | None = None,
    ):
        self.limits = limits
        self.inventory = inventory or InventoryPolicy()
        # Fills move balances; the OMS attaches its ledger so admission sees
        # held = opening balance + filled deltas (not a static snapshot).
        self.ledger: Any = None
        self.reserved_base: dict[str, Decimal] = {}
        self.kill_switch = False
        self.kill_switch_reason = ""
        self.kill_switch_source = ""
        self.kill_switch_activated_ts_ns: int | None = None
        self.kill_switch_events: list[dict[str, Any]] = []
        self.kill_switch_last_reconciliation_id = ""
        self.account_snapshots = dict(account_snapshots or {})
        self.reserved_gross = Decimal("0")
        self.reserved_cash: dict[str, Decimal] = {}
        self.reserved_margin: dict[str, Decimal] = {}
        self.reserved_position: dict[str, Decimal] = {}
        self.reserved_messages: dict[str, int] = {}
        # Worst-case one-sided exposure reserved by active packages.
        self.reserved_residual = Decimal("0")
        # Signed residual left open by closed plans (set by the OMS, which
        # derives it from its plans so it survives a restart).
        self.carried_residual_base = Decimal("0")
        self.reservations: dict[str, PackageReservation] = {}
        self.last_reconciliation: ReconciliationReport | None = None
        self.last_pretrade_validation: PreTradeValidationReport | None = None
        self.suspended_venues: dict[str, dict[str, Any]] = {}
        self.exposure_state = ExposureControlState.NORMAL
        self.exposure_transitions: list[ExposureLimitTransition] = []
        self.last_exposure_snapshot: dict[str, Any] | None = None
        self.margin_states: dict[str, MarginState] = {}
        self.last_margin_report: MarginReport | None = None

    def suspend_venue(
        self, venue: str, *, code: str, now_ns: int, detail: str = ""
    ) -> None:
        current = self.suspended_venues.get(venue)
        if current is None:
            self.suspended_venues[venue] = {
                "code": code, "since_ts_ns": now_ns,
                "last_detected_ts_ns": now_ns, "detail": detail,
            }
            return
        current.update({
            "code": code, "last_detected_ts_ns": now_ns, "detail": detail,
        })

    def activate_kill_switch(
        self, *, now_ns: int, reason: str, source: str,
        ledger: Any | None = None, operator_id: str = "",
    ) -> dict[str, Any]:
        previous = KillSwitchState.ACTIVE if self.kill_switch else KillSwitchState.INACTIVE
        self.kill_switch = True
        self.kill_switch_reason = reason
        self.kill_switch_source = source
        if self.kill_switch_activated_ts_ns is None:
            self.kill_switch_activated_ts_ns = now_ns
        event = {
            "event_id": stable_id(
                "kill-switch", now_ns, previous.value, "ACTIVE",
                reason, source, operator_id,
            ),
            "ts_ns": now_ns,
            "previous_state": previous.value,
            "state": KillSwitchState.ACTIVE.value,
            "action": "ACTIVATED" if previous == KillSwitchState.INACTIVE else "REASSERTED",
            "reason": reason, "source": source,
            "operator_id": operator_id,
            "reconciliation_required_before_resume": True,
        }
        self.kill_switch_events.append(event)
        if ledger is not None:
            ledger.record(
                "KILL_SWITCH_STATE_TRANSITION", now_ns,
                **{key: value for key, value in event.items() if key != "ts_ns"},
            )
        return event

    def reset_kill_switch(
        self, *, now_ns: int, reconciliation: ReconciliationReport,
        ledger: Any, operator_id: str,
    ) -> dict[str, Any]:
        if reconciliation.decision != ReconciliationDecision.APPROVED:
            raise ValueError("kill-switch reset requires approved reconciliation")
        if (
            self.last_reconciliation is None
            or reconciliation.reconciliation_id
            != self.last_reconciliation.reconciliation_id
        ):
            raise ValueError("kill-switch reset requires latest reconciliation")
        if self.suspended_venues:
            raise ValueError("kill-switch reset blocked by suspended venues")
        if self.exposure_state != ExposureControlState.NORMAL:
            raise ValueError("kill-switch reset blocked by exposure limit breach")
        if any(state == MarginState.BREACH for state in self.margin_states.values()):
            raise ValueError("kill-switch reset blocked by margin breach")
        previous = KillSwitchState.ACTIVE if self.kill_switch else KillSwitchState.INACTIVE
        self.kill_switch = False
        self.kill_switch_reason = ""
        self.kill_switch_source = ""
        self.kill_switch_activated_ts_ns = None
        self.kill_switch_last_reconciliation_id = reconciliation.reconciliation_id
        event = {
            "event_id": stable_id(
                "kill-switch", now_ns, previous.value, "INACTIVE",
                reconciliation.reconciliation_id, operator_id,
            ),
            "ts_ns": now_ns,
            "previous_state": previous.value,
            "state": KillSwitchState.INACTIVE.value,
            "action": "RESET",
            "reason": "approved reconciliation",
            "source": "PM",
            "operator_id": operator_id,
            "reconciliation_id": reconciliation.reconciliation_id,
            "reconciliation_required_before_resume": False,
        }
        self.kill_switch_events.append(event)
        ledger.record(
            "KILL_SWITCH_STATE_TRANSITION", now_ns,
            **{key: value for key, value in event.items() if key != "ts_ns"},
        )
        return event

    def kill_switch_snapshot(
        self, *, allow_risk_reducing_orders: bool
    ) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "state": (
                KillSwitchState.ACTIVE.value
                if self.kill_switch else KillSwitchState.INACTIVE.value
            ),
            "reason": self.kill_switch_reason,
            "source": self.kill_switch_source,
            "activated_ts_ns": self.kill_switch_activated_ts_ns,
            "strategy_new_risk_allowed": not self.kill_switch,
            "cancel_all_on_activation": True,
            "risk_reducing_orders_allowed": allow_risk_reducing_orders,
            "reconciliation_required_before_resume": self.kill_switch,
            "last_reset_reconciliation_id": self.kill_switch_last_reconciliation_id,
            "events": list(self.kill_switch_events),
        }

    @staticmethod
    def _is_margin_instrument(
        instruments: Any | None, venue: str, instrument_id: str
    ) -> bool:
        if instruments is None:
            return False
        try:
            spec = instruments.get(venue, instrument_id)
        except ValueError:
            return False
        return (
            str(getattr(spec, "product_type", "SPOT")).upper()
            not in {"SPOT", "CASH"}
            or Decimal(str(getattr(spec, "leverage", "1"))) > 1
        )

    def evaluate_margin(
        self,
        *,
        ledger: Any,
        now_ns: int,
        mark_prices: dict[tuple[str, str], Decimal] | None = None,
        initial_capital_by_venue: dict[str, Decimal] | None = None,
        instruments: Any | None = None,
        venue_risk: tuple[VenueRiskSnapshot, ...] = (),
        enforce_controls: bool = True,
    ) -> MarginReport:
        """Mark derivative collateral and warn before maintenance is breached."""
        marks = mark_prices or {}
        initial = initial_capital_by_venue or {}
        account_state = ledger.recalculate_account_state(
            mark_prices=marks,
            initial_capital_by_venue=initial,
            reserved_capacity=self.capacity_snapshot(),
        )
        reported_by_venue = {snapshot.venue: snapshot for snapshot in venue_risk}
        venues = {
            *self.account_snapshots,
            *initial,
            *account_state["venues"],
            *(venue for venue, _ in marks),
            *(snapshot.venue for snapshot in venue_risk),
        }
        rows: list[VenueMarginState] = []
        severity = {
            MarginState.NOT_APPLICABLE: 0,
            MarginState.HEALTHY: 1,
            MarginState.WARNING: 2,
            MarginState.BREACH: 3,
        }
        for venue in sorted(venues):
            account = self._account(venue)
            positions = [
                row for row in account_state["positions"].values()
                if str(row["venue"]) == venue
            ]
            configured = (
                account.margin_enabled
                or account.initial_margin_rate > 0
                or account.maintenance_margin_rate > 0
            )
            derivative_positions = [
                row for row in positions
                if self._is_margin_instrument(
                    instruments, venue, str(row["instrument_id"])
                )
            ]
            applicable = configured or bool(derivative_positions)
            margin_positions = positions if configured else derivative_positions
            gross_notional = sum((
                abs(Decimal(str(row["quantity"])))
                * Decimal(str(row["mark_price"]))
                for row in margin_positions
            ), Decimal("0"))
            venue_row = account_state["venues"].get(venue, {})
            if account.collateral_balance.is_finite():
                margin_balance = (
                    account.collateral_balance
                    + Decimal(str(venue_row.get("realized_pnl", "0")))
                    + Decimal(str(venue_row.get("unrealized_pnl", "0")))
                    - Decimal(str(venue_row.get("fees", "0")))
                )
            else:
                margin_balance = Decimal(str(venue_row.get("equity", "0")))
            initial_required = gross_notional * account.initial_margin_rate
            maintenance_required = (
                gross_notional * account.maintenance_margin_rate
            )
            reported = reported_by_venue.get(venue)
            reported_balance = (
                reported.margin_balance if reported is not None else None
            )
            effective_balance = (
                min(margin_balance, reported_balance)
                if reported_balance is not None else margin_balance
            )
            liquidation_buffer = effective_balance - maintenance_required
            required_buffer = (
                maintenance_required * account.liquidation_buffer_rate
            )
            if not applicable:
                state = MarginState.NOT_APPLICABLE
                alert_codes: tuple[str, ...] = ()
            elif effective_balance < maintenance_required:
                state = MarginState.BREACH
                alert_codes = ("MARGIN_BELOW_MAINTENANCE",)
            elif liquidation_buffer < required_buffer:
                state = MarginState.WARNING
                alert_codes = ("LIQUIDATION_BUFFER_LOW",)
            else:
                state = MarginState.HEALTHY
                alert_codes = ()
            utilization = (
                Decimal("0") if effective_balance <= 0 and maintenance_required == 0
                else (
                    Decimal("Infinity") if effective_balance <= 0
                    else maintenance_required / effective_balance
                )
            )
            row = VenueMarginState(
                venue=venue, state=state, margin_enabled=applicable,
                gross_notional=gross_notional, margin_balance=margin_balance,
                venue_reported_margin_balance=reported_balance,
                effective_margin_balance=effective_balance,
                initial_margin_required=initial_required,
                maintenance_margin_required=maintenance_required,
                liquidation_buffer=liquidation_buffer,
                required_liquidation_buffer=required_buffer,
                maintenance_margin_utilization=utilization,
                alert_codes=alert_codes,
            )
            rows.append(row)
            previous = self.margin_states.get(venue, MarginState.NOT_APPLICABLE)
            self.margin_states[venue] = state
            if enforce_controls and previous != state:
                ledger.record(
                    "MARGIN_STATE_TRANSITION", now_ns,
                    transition_id=stable_id(
                        "margin-state", venue, now_ns, previous.value, state.value
                    ),
                    venue=venue, previous_state=previous.value, state=state.value,
                    alert_codes=list(alert_codes),
                    liquidation_buffer=str(liquidation_buffer),
                    required_liquidation_buffer=str(required_buffer),
                )
                if state in {MarginState.WARNING, MarginState.BREACH}:
                    ledger.record(
                        "MARGIN_BUFFER_ALERT", now_ns, venue=venue,
                        state=state.value, alert_codes=list(alert_codes),
                        margin_balance=str(effective_balance),
                        maintenance_margin_required=str(maintenance_required),
                        liquidation_buffer=str(liquidation_buffer),
                        required_liquidation_buffer=str(required_buffer),
                    )
                if state == MarginState.BREACH:
                    self.activate_kill_switch(
                        now_ns=now_ns, reason=f"margin breach: {venue}",
                        source="RMS_MARGIN", ledger=ledger,
                    )
        overall = max(
            (row.state for row in rows),
            key=lambda state: severity[state],
            default=MarginState.NOT_APPLICABLE,
        )
        report = MarginReport(
            report_id=stable_id(
                "margin-report", now_ns, overall.value,
                *(f"{row.venue}:{row.state.value}" for row in rows),
            ),
            ts_ns=now_ns, state=overall, venues=tuple(rows),
        )
        self.last_margin_report = report
        return report

    def venue_is_suspended(self, venue: str) -> bool:
        return venue in self.suspended_venues

    def update_account_snapshot(self, snapshot: AccountRiskSnapshot) -> None:
        self.account_snapshots[snapshot.venue] = snapshot

    def capacity_snapshot(self) -> dict[str, object]:
        return {
            "reserved_gross": self.reserved_gross,
            "reserved_cash": dict(self.reserved_cash),
            "reserved_margin": dict(self.reserved_margin),
            "reserved_position": dict(self.reserved_position),
            "reserved_messages": dict(self.reserved_messages),
            "reserved_residual": self.reserved_residual,
            "carried_residual_base": self.carried_residual_base,
            "reserved_base": dict(self.reserved_base),
            "inventory": self.inventory_snapshot(),
        }

    def held_base(self, venue: str) -> Decimal:
        opening = self.inventory.initial_base_by_venue.get(venue, Decimal("0"))
        if self.ledger is None:
            return opening
        return opening + self.ledger.positions.get(
            (venue, self.inventory.instrument_id), Decimal("0")
        )

    def held_quote(self, venue: str) -> Decimal:
        opening = self.inventory.initial_quote_by_venue.get(venue, Decimal("0"))
        if self.ledger is None:
            return opening
        return opening + self.ledger.cash_deltas.get(venue, Decimal("0"))

    def inventory_snapshot(self) -> dict[str, Any]:
        venues = sorted(
            set(self.inventory.initial_base_by_venue) | set(self.inventory.initial_quote_by_venue)
        )
        return {
            "model": self.inventory.model.value,
            "label": self.inventory.label,
            "instrument_id": self.inventory.instrument_id,
            "venues": {
                venue: {
                    "initial_base": str(self.inventory.initial_base_by_venue.get(venue, Decimal("0"))),
                    "held_base": str(self.held_base(venue)),
                    "reserved_base": str(self.reserved_base.get(venue, Decimal("0"))),
                    "available_base": str(self.held_base(venue) - self.reserved_base.get(venue, Decimal("0"))),
                    "initial_quote": str(self.inventory.initial_quote_by_venue.get(venue, Decimal("0"))),
                    "held_quote": str(self.held_quote(venue)),
                    "reserved_quote": str(self.reserved_cash.get(venue, Decimal("0"))),
                    "available_quote": str(self.held_quote(venue) - self.reserved_cash.get(venue, Decimal("0"))),
                }
                for venue in venues
            },
        }

    def rehydrate_reservation(self, opportunity: Opportunity) -> PackageReservation:
        """Recreate a durable nonterminal package reservation after process restart."""
        return self.reserve(opportunity)

    def _account(self, venue: str) -> AccountRiskSnapshot:
        return self.account_snapshots.get(venue) or AccountRiskSnapshot(
            venue=venue,
            available_cash=self.limits.max_cash_reserved,
            available_margin=self.limits.max_margin_reserved,
            max_abs_position_base=self.limits.max_position_base,
            message_capacity=self.limits.max_message_capacity,
        )

    @staticmethod
    def _limit_for(
        overrides: dict[str, Decimal], key: str, fallback: Decimal
    ) -> Decimal:
        return overrides.get(key, fallback)

    @staticmethod
    def _utilization(observed: Decimal, limit: Decimal) -> str:
        if limit.is_infinite():
            return "0"
        if limit == 0:
            return "0" if observed == 0 else "Infinity"
        return str(observed / limit)

    def _exposure_values(
        self,
        *,
        plans: dict[str, ParentPlan] | None,
        ledger: Any | None,
        mark_prices: dict[tuple[str, str], Decimal] | None = None,
        projected_strategy_id: str = "",
        projected_orders: tuple[ChildOrder, ...] = (),
    ) -> tuple[
        dict[str, dict[str, Decimal]],
        dict[str, dict[str, Decimal]],
        dict[str, dict[str, Decimal]],
    ]:
        """Return strategy, instrument and venue exposure from fill events.

        Exposure is valued at the supplied mark, then the latest fill, then the
        projected order price. Gross sums absolute position notionals while net
        preserves direction.
        """
        plans = plans or {}
        mark_prices = dict(mark_prices or {})
        order_strategy = {
            order.order_id: plan.opportunity.strategy_id
            for plan in plans.values() for order in plan.children
        }
        opportunity_strategy = {
            plan.opportunity.opportunity_id: plan.opportunity.strategy_id
            for plan in plans.values()
        }
        quantities: dict[tuple[str, str, str], Decimal] = {}
        last_prices: dict[tuple[str, str], Decimal] = {}
        if ledger is not None:
            for event in ledger.events:
                if event.get("event_type") != "FILL":
                    continue
                venue = str(event["venue"])
                instrument_id = str(event["instrument_id"])
                strategy_id = order_strategy.get(str(event.get("order_id", "")))
                strategy_id = strategy_id or opportunity_strategy.get(
                    str(event.get("opportunity_id", "")), "UNKNOWN"
                )
                quantity = Decimal(str(event["fill_quantity"]))
                if str(event["side"]) == "SELL":
                    quantity = -quantity
                key = (strategy_id, venue, instrument_id)
                quantities[key] = quantities.get(key, Decimal("0")) + quantity
                last_prices[(venue, instrument_id)] = Decimal(
                    str(event["fill_price"])
                )
        for order in projected_orders:
            key = (projected_strategy_id, order.venue, order.instrument_id)
            quantity = order.quantity if order.side.value == "BUY" else -order.quantity
            quantities[key] = quantities.get(key, Decimal("0")) + quantity
            mark_prices.setdefault((order.venue, order.instrument_id), order.limit_price)

        strategy_values: dict[str, dict[str, Decimal]] = {}
        instrument_values: dict[str, dict[str, Decimal]] = {}
        venue_values: dict[str, dict[str, Decimal]] = {}
        for (strategy_id, venue, instrument_id), quantity in quantities.items():
            price = mark_prices.get(
                (venue, instrument_id), last_prices.get((venue, instrument_id), Decimal("0"))
            )
            signed_notional = quantity * price
            strategy = strategy_values.setdefault(
                strategy_id, {"gross": Decimal("0"), "net": Decimal("0")}
            )
            strategy["gross"] += abs(signed_notional)
            strategy["net"] += signed_notional
            instrument = instrument_values.setdefault(
                instrument_id, {"gross": Decimal("0")}
            )
            instrument["gross"] += abs(signed_notional)
            venue_row = venue_values.setdefault(venue, {"gross": Decimal("0")})
            venue_row["gross"] += abs(signed_notional)
        return strategy_values, instrument_values, venue_values

    def exposure_snapshot(
        self,
        *,
        plans: dict[str, ParentPlan] | None,
        ledger: Any | None,
        now_ns: int,
        mark_prices: dict[tuple[str, str], Decimal] | None = None,
        projected_strategy_id: str = "",
        projected_orders: tuple[ChildOrder, ...] = (),
    ) -> dict[str, Any]:
        strategies, instruments, venues = self._exposure_values(
            plans=plans, ledger=ledger, mark_prices=mark_prices,
            projected_strategy_id=projected_strategy_id,
            projected_orders=projected_orders,
        )
        binding: list[dict[str, str]] = []
        strategy_rows: dict[str, Any] = {}
        for key, values in sorted(strategies.items()):
            gross_limit = self._limit_for(
                self.limits.strategy_gross_limits, key,
                self.limits.max_strategy_gross_notional,
            )
            net_limit = self._limit_for(
                self.limits.strategy_net_limits, key,
                self.limits.max_strategy_net_notional,
            )
            row_binding: list[str] = []
            for code, observed, limit in (
                ("STRATEGY_GROSS_EXPOSURE", values["gross"], gross_limit),
                ("STRATEGY_NET_EXPOSURE", abs(values["net"]), net_limit),
            ):
                if observed > limit:
                    row_binding.append(code)
                    binding.append({
                        "code": code, "scope": "STRATEGY", "key": key,
                        "observed": str(observed), "limit": str(limit),
                        "utilization": self._utilization(observed, limit),
                    })
            strategy_rows[key] = {
                "gross_notional": str(values["gross"]),
                "net_notional": str(values["net"]),
                "gross_limit": str(gross_limit), "net_limit": str(net_limit),
                "gross_utilization": self._utilization(values["gross"], gross_limit),
                "net_utilization": self._utilization(abs(values["net"]), net_limit),
                "binding_codes": row_binding,
            }

        def gross_rows(
            values_by_key: dict[str, dict[str, Decimal]], *, scope: str,
            code: str, overrides: dict[str, Decimal], fallback: Decimal,
        ) -> dict[str, Any]:
            rows: dict[str, Any] = {}
            for key, values in sorted(values_by_key.items()):
                limit = self._limit_for(overrides, key, fallback)
                observed = values["gross"]
                breached = observed > limit
                if breached:
                    binding.append({
                        "code": code, "scope": scope, "key": key,
                        "observed": str(observed), "limit": str(limit),
                        "utilization": self._utilization(observed, limit),
                    })
                rows[key] = {
                    "gross_notional": str(observed), "limit": str(limit),
                    "utilization": self._utilization(observed, limit),
                    "binding_codes": [code] if breached else [],
                }
            return rows

        instrument_rows = gross_rows(
            instruments, scope="INSTRUMENT", code="INSTRUMENT_GROSS_EXPOSURE",
            overrides=self.limits.instrument_gross_limits,
            fallback=self.limits.max_instrument_gross_notional,
        )
        venue_rows = gross_rows(
            venues, scope="VENUE", code="VENUE_GROSS_EXPOSURE",
            overrides=self.limits.venue_gross_limits,
            fallback=self.limits.max_venue_gross_notional,
        )
        return {
            "schema_version": "1.0.0", "ts_ns": now_ns,
            "state": (
                ExposureControlState.BREACHED.value
                if binding else ExposureControlState.NORMAL.value
            ),
            "strategies": strategy_rows, "instruments": instrument_rows,
            "venues": venue_rows, "binding_events": binding,
            "transitions": [item.to_dict() for item in self.exposure_transitions],
        }

    def update_exposure_state(
        self,
        *,
        plans: dict[str, ParentPlan],
        ledger: Any,
        now_ns: int,
        mark_prices: dict[tuple[str, str], Decimal] | None = None,
    ) -> dict[str, Any]:
        snapshot = self.exposure_snapshot(
            plans=plans, ledger=ledger, now_ns=now_ns, mark_prices=mark_prices,
        )
        next_state = ExposureControlState(snapshot["state"])
        if next_state != self.exposure_state:
            codes = tuple(sorted({item["code"] for item in snapshot["binding_events"]}))
            transition = ExposureLimitTransition(
                transition_id=stable_id(
                    "exposure-limit-transition", now_ns,
                    self.exposure_state.value, next_state.value, *codes,
                ),
                ts_ns=now_ns, previous_state=self.exposure_state,
                state=next_state, binding_codes=codes,
            )
            self.exposure_transitions.append(transition)
            ledger.record(
                "EXPOSURE_LIMIT_STATE_TRANSITION", now_ns,
                transition_id=transition.transition_id,
                previous_state=transition.previous_state.value,
                state=transition.state.value,
                binding_codes=list(transition.binding_codes),
                binding_events=snapshot["binding_events"],
            )
            self.exposure_state = next_state
            if next_state == ExposureControlState.BREACHED:
                self.activate_kill_switch(
                    now_ns=now_ns, reason="exposure limit breach",
                    source="RMS_EXPOSURE", ledger=ledger,
                )
        snapshot["transitions"] = [
            item.to_dict() for item in self.exposure_transitions
        ]
        self.last_exposure_snapshot = snapshot
        return snapshot

    def validate_pretrade(
        self,
        *,
        opportunity: Opportunity,
        orders: tuple[ChildOrder, ...],
        books: dict[tuple[str, str], Any],
        instruments: Any,
        route_status: dict[str, Any],
        now_ns: int,
        plans: dict[str, ParentPlan] | None = None,
        ledger: Any | None = None,
    ) -> PreTradeValidationReport:
        """Evaluate the complete package without mutating reservation state."""
        checks: list[PreTradeCheck] = []

        def check(
            code: str, scope: str, passed: bool, *, venue: str = "",
            instrument_id: str = "", observed: object = "",
            limit: object = "", detail: str = "",
        ) -> None:
            checks.append(PreTradeCheck(
                code=code, scope=scope, passed=bool(passed), venue=venue,
                instrument_id=instrument_id, observed=str(observed),
                limit=str(limit), detail=detail,
            ))

        notionals = {
            order.order_id: order.quantity * order.limit_price for order in orders
        }
        package_gross = sum(notionals.values(), Decimal("0"))
        check(
            "STRATEGY_PACKAGE_NOTIONAL", "STRATEGY",
            package_gross <= self.limits.max_strategy_package_notional,
            observed=package_gross,
            limit=self.limits.max_strategy_package_notional,
        )
        check(
            "PLATFORM_GROSS_NOTIONAL", "PLATFORM",
            self.reserved_gross + package_gross <= self.limits.max_gross_notional,
            observed=self.reserved_gross + package_gross,
            limit=self.limits.max_gross_notional,
        )
        mark_prices = {
            key: (book.best_bid + book.best_ask) / Decimal("2")
            for key, book in books.items() if book.bids and book.asks
        }
        projected = self.exposure_snapshot(
            plans=plans, ledger=ledger, now_ns=now_ns, mark_prices=mark_prices,
            projected_strategy_id=opportunity.strategy_id,
            projected_orders=orders,
        )
        for binding in projected["binding_events"]:
            check(
                binding["code"], binding["scope"], False,
                venue=binding["key"] if binding["scope"] == "VENUE" else "",
                instrument_id=(
                    binding["key"] if binding["scope"] == "INSTRUMENT" else ""
                ),
                observed=binding["observed"], limit=binding["limit"],
                detail=f"projected utilization={binding['utilization']}",
            )
        binding_pairs = {
            (item["code"], item["key"]) for item in projected["binding_events"]
        }
        for code, scope, key, row, observed_field, limit_field in (
            (
                "STRATEGY_GROSS_EXPOSURE", "STRATEGY", opportunity.strategy_id,
                projected["strategies"].get(opportunity.strategy_id, {}),
                "gross_notional", "gross_limit",
            ),
            (
                "STRATEGY_NET_EXPOSURE", "STRATEGY", opportunity.strategy_id,
                projected["strategies"].get(opportunity.strategy_id, {}),
                "net_notional", "net_limit",
            ),
        ):
            if row and (code, key) not in binding_pairs:
                observed = abs(Decimal(row[observed_field]))
                check(
                    code, scope, True, observed=observed, limit=row[limit_field],
                    detail=f"projected utilization={row[observed_field.replace('notional', 'utilization')]}",
                )
        for scope_key, code, scope in (
            ("instruments", "INSTRUMENT_GROSS_EXPOSURE", "INSTRUMENT"),
            ("venues", "VENUE_GROSS_EXPOSURE", "VENUE"),
        ):
            for key, row in projected[scope_key].items():
                if (code, key) in binding_pairs:
                    continue
                check(
                    code, scope, True,
                    venue=key if scope == "VENUE" else "",
                    instrument_id=key if scope == "INSTRUMENT" else "",
                    observed=row["gross_notional"], limit=row["limit"],
                    detail=f"projected utilization={row['utilization']}",
                )

        for order in orders:
            venue = order.venue
            instrument_id = order.instrument_id
            notional = notionals[order.order_id]
            account = self._account(venue)
            status = route_status.get(venue)
            route_account_enabled = bool(
                getattr(status, "account_enabled", True)
            )
            route_venue_enabled = bool(getattr(status, "venue_enabled", True))
            check(
                "ACCOUNT_STATUS", "VENUE", account.account_enabled and route_account_enabled,
                venue=venue, instrument_id=instrument_id,
                observed=account.account_enabled and route_account_enabled,
                limit=True,
            )
            check(
                "VENUE_STATUS", "VENUE", account.venue_enabled and route_venue_enabled,
                venue=venue, instrument_id=instrument_id,
                observed=account.venue_enabled and route_venue_enabled,
                limit=True,
            )
            check(
                "STRATEGY_STATUS", "STRATEGY", account.strategy_enabled,
                venue=venue, instrument_id=instrument_id,
                observed=account.strategy_enabled, limit=True,
            )
            check(
                "TRADE_BREAK_STATUS", "VENUE", not self.venue_is_suspended(venue),
                venue=venue, instrument_id=instrument_id,
                observed="SUSPENDED" if self.venue_is_suspended(venue) else "ACTIVE",
                limit="ACTIVE",
            )

            try:
                spec = instruments.get(venue, instrument_id, now_ns)
            except ValueError:
                spec = None
            check(
                "INSTRUMENT_EXISTS", "INSTRUMENT", spec is not None,
                venue=venue, instrument_id=instrument_id,
                observed="FOUND" if spec is not None else "UNKNOWN", limit="FOUND",
            )
            if spec is not None:
                check(
                    "INSTRUMENT_STATUS", "INSTRUMENT", spec.status == "TRADING",
                    venue=venue, instrument_id=instrument_id,
                    observed=spec.status, limit="TRADING",
                )
                check(
                    "MINIMUM_QUANTITY", "ORDER", order.quantity >= spec.min_quantity,
                    venue=venue, instrument_id=instrument_id,
                    observed=order.quantity, limit=spec.min_quantity,
                )
                check(
                    "LOT_SIZE", "ORDER",
                    order.quantity > 0 and order.quantity % spec.quantity_step == 0,
                    venue=venue, instrument_id=instrument_id,
                    observed=order.quantity, limit=spec.quantity_step,
                )
                check(
                    "TICK_SIZE", "ORDER",
                    order.limit_price > 0 and order.limit_price % spec.tick_size == 0,
                    venue=venue, instrument_id=instrument_id,
                    observed=order.limit_price, limit=spec.tick_size,
                )
                check(
                    "MINIMUM_NOTIONAL", "ORDER", notional >= spec.min_notional,
                    venue=venue, instrument_id=instrument_id,
                    observed=notional, limit=spec.min_notional,
                )

            check(
                "MAXIMUM_ORDER_NOTIONAL", "ORDER",
                notional <= self.limits.max_order_notional,
                venue=venue, instrument_id=instrument_id,
                observed=notional, limit=self.limits.max_order_notional,
            )
            check(
                "VENUE_ORDER_NOTIONAL", "VENUE",
                notional <= account.max_order_notional,
                venue=venue, instrument_id=instrument_id,
                observed=notional, limit=account.max_order_notional,
            )

            book = books.get((venue, instrument_id))
            check(
                "LIVE_BOOK_AVAILABLE", "MARKET_DATA", book is not None,
                venue=venue, instrument_id=instrument_id,
                observed="AVAILABLE" if book is not None else "MISSING",
                limit="AVAILABLE",
            )
            if book is not None:
                last_receive = getattr(book, "last_receive_ts_ns", None)
                age_ns = now_ns - last_receive if last_receive is not None else None
                check(
                    "LIVE_BOOK_VALID", "MARKET_DATA", bool(book.is_tradable),
                    venue=venue, instrument_id=instrument_id,
                    observed=getattr(book.state, "value", book.state), limit="VALID",
                    detail=str(getattr(book, "invalid_reason", "")),
                )
                check(
                    "LIVE_DATA_AGE", "MARKET_DATA",
                    age_ns is not None and 0 <= age_ns <= self.limits.max_book_age_ns,
                    venue=venue, instrument_id=instrument_id,
                    observed=age_ns, limit=self.limits.max_book_age_ns,
                )
                if book.bids and book.asks:
                    reference = (book.best_bid + book.best_ask) / Decimal("2")
                    deviation = (
                        abs(order.limit_price - reference) / reference
                        * Decimal("10000")
                    )
                    check(
                        "PRICE_BAND", "ORDER",
                        deviation <= self.limits.max_price_deviation_bps,
                        venue=venue, instrument_id=instrument_id,
                        observed=deviation,
                        limit=self.limits.max_price_deviation_bps,
                        detail=f"reference_mid={reference}",
                    )
                else:
                    check(
                        "PRICE_BAND", "ORDER", False,
                        venue=venue, instrument_id=instrument_id,
                        observed="NO_REFERENCE", limit=self.limits.max_price_deviation_bps,
                    )

            cash_required = (
                notional
                if order.side.value == "BUY" and not account.margin_enabled
                else Decimal("0")
            )
            check(
                "AVAILABLE_CASH", "ACCOUNT",
                self.reserved_cash.get(venue, Decimal("0")) + cash_required
                <= account.available_cash,
                venue=venue, instrument_id=instrument_id,
                observed=self.reserved_cash.get(venue, Decimal("0")) + cash_required,
                limit=account.available_cash,
            )
            margin_required = notional * account.initial_margin_rate
            check(
                "AVAILABLE_MARGIN", "ACCOUNT",
                self.reserved_margin.get(venue, Decimal("0")) + margin_required
                <= account.available_margin,
                venue=venue, instrument_id=instrument_id,
                observed=self.reserved_margin.get(venue, Decimal("0")) + margin_required,
                limit=account.available_margin,
            )

        decision = (
            PreTradeDecision.APPROVED
            if all(item.passed for item in checks)
            else PreTradeDecision.REJECTED
        )
        report = PreTradeValidationReport(
            validation_id=stable_id(
                "pretrade-validation", opportunity.opportunity_id, now_ns,
                *(item.code for item in checks if not item.passed),
            ),
            ts_ns=now_ns,
            opportunity_id=opportunity.opportunity_id,
            strategy_id=opportunity.strategy_id,
            decision=decision,
            checks=tuple(checks),
        )
        self.last_pretrade_validation = report
        return report

    def reserve(self, opportunity: Opportunity) -> PackageReservation:
        if self.kill_switch:
            raise ValueError("strategy kill switch active")
        blocked = sorted({
            opportunity.buy_venue, opportunity.sell_venue
        } & self.suspended_venues.keys())
        if blocked:
            raise ValueError(
                "venue suspended by unresolved trade break: " + ",".join(blocked)
            )
        existing = self.reservations.get(opportunity.opportunity_id)
        if existing is not None and existing.active:
            return existing
        buy_notional = opportunity.buy_price * opportunity.quantity
        sell_notional = opportunity.sell_price * opportunity.quantity
        if max(buy_notional, sell_notional) > self.limits.max_order_notional:
            raise ValueError("max order notional exceeded")
        package_gross = buy_notional + sell_notional
        if package_gross > self.limits.max_strategy_package_notional:
            raise ValueError("max strategy package notional exceeded")
        if self.reserved_gross + package_gross > self.limits.max_gross_notional:
            raise ValueError("max gross notional exceeded")

        buy_account = self._account(opportunity.buy_venue)
        cash = {
            opportunity.buy_venue: (
                Decimal("0") if buy_account.margin_enabled else buy_notional
            )
        }
        margin: dict[str, Decimal] = {}
        positions: dict[str, Decimal] = {}
        messages: dict[str, int] = {}
        legs = (
            (opportunity.buy_venue, opportunity.quantity, buy_notional),
            (opportunity.sell_venue, -opportunity.quantity, sell_notional),
        )
        for venue, position_delta, notional in legs:
            account = self._account(venue)
            margin[venue] = margin.get(venue, Decimal("0")) + (
                notional * account.initial_margin_rate
            )
            positions[venue] = positions.get(venue, Decimal("0")) + position_delta
            messages[venue] = messages.get(venue, 0) + 1

        # The intended package is flat, but either leg can fail alone, so the
        # reservation covers the larger leg (a balanced package would
        # otherwise reserve nothing and many could leg out at once).
        planned_residual = max(abs(delta) for _, delta, _ in legs)
        carried = abs(self.carried_residual_base)
        if carried and carried >= self.limits.max_residual_base:
            raise ValueError("carried residual at limit")
        if (
            self.reserved_residual + planned_residual + carried
            > self.limits.max_legging_exposure_base
        ):
            raise ValueError("max legging exposure exceeded")

        # Validate the complete package using temporary totals. No reservation
        # state is mutated unless every venue and resource passes.
        for venue in sorted(set((*cash, *margin, *positions, *messages))):
            account = self._account(venue)
            if not account.account_enabled:
                raise ValueError(f"account disabled: {venue}")
            total_cash = (
                self.reserved_cash.get(venue, Decimal("0"))
                + cash.get(venue, Decimal("0"))
            )
            if total_cash > account.available_cash:
                raise ValueError(f"insufficient cash: {venue}")
            total_margin = (
                self.reserved_margin.get(venue, Decimal("0"))
                + margin.get(venue, Decimal("0"))
            )
            if total_margin > account.available_margin:
                raise ValueError(f"insufficient margin: {venue}")
            projected_position = (
                account.position_base
                + self.reserved_position.get(venue, Decimal("0"))
                + positions.get(venue, Decimal("0"))
            )
            if abs(projected_position) > account.max_abs_position_base:
                raise ValueError(f"position limit exceeded: {venue}")
            message_total = (
                account.message_used
                + self.reserved_messages.get(venue, 0)
                + messages.get(venue, 0)
            )
            if message_total > account.message_capacity:
                raise ValueError(f"message capacity exceeded: {venue}")

        base: dict[str, Decimal] = {}
        if self.inventory.model == InventoryModel.PRE_FUNDED:
            sell_account = self._account(opportunity.sell_venue)
            if not buy_account.margin_enabled:
                needed = (
                    self.reserved_cash.get(opportunity.buy_venue, Decimal("0")) + buy_notional
                )
                if needed > self.held_quote(opportunity.buy_venue):
                    raise ValueError(f"insufficient quote inventory: {opportunity.buy_venue}")
            if not sell_account.margin_enabled:
                base[opportunity.sell_venue] = opportunity.quantity
                needed = (
                    self.reserved_base.get(opportunity.sell_venue, Decimal("0"))
                    + opportunity.quantity
                )
                if needed > self.held_base(opportunity.sell_venue):
                    raise ValueError(f"insufficient base inventory: {opportunity.sell_venue}")

        reservation = PackageReservation(
            reservation_id=stable_id("risk-reservation", opportunity.opportunity_id),
            opportunity_id=opportunity.opportunity_id,
            gross_notional=package_gross,
            cash_by_venue=cash,
            margin_by_venue=margin,
            position_delta_by_venue=positions,
            messages_by_venue=messages,
            residual_base=planned_residual,
            base_by_venue=base,
        )
        self.reserved_gross += package_gross
        self.reserved_residual += planned_residual
        for venue, amount in cash.items():
            self.reserved_cash[venue] = self.reserved_cash.get(venue, Decimal("0")) + amount
        for venue, amount in base.items():
            self.reserved_base[venue] = self.reserved_base.get(venue, Decimal("0")) + amount
        for venue, amount in margin.items():
            self.reserved_margin[venue] = self.reserved_margin.get(venue, Decimal("0")) + amount
        for venue, amount in positions.items():
            self.reserved_position[venue] = self.reserved_position.get(venue, Decimal("0")) + amount
        for venue, count in messages.items():
            self.reserved_messages[venue] = self.reserved_messages.get(venue, 0) + count
        self.reservations[opportunity.opportunity_id] = reservation
        return reservation

    def release(self, reservation: PackageReservation | Decimal) -> None:
        if isinstance(reservation, Decimal):
            self.reserved_gross = max(Decimal("0"), self.reserved_gross - reservation)
            return
        if not reservation.active:
            return
        self.reserved_gross = max(
            Decimal("0"), self.reserved_gross - reservation.gross_notional
        )
        self.reserved_residual = max(
            Decimal("0"), self.reserved_residual - reservation.residual_base
        )
        for venue, amount in reservation.cash_by_venue.items():
            self.reserved_cash[venue] = max(
                Decimal("0"), self.reserved_cash.get(venue, Decimal("0")) - amount
            )
        for venue, amount in reservation.margin_by_venue.items():
            self.reserved_margin[venue] = max(
                Decimal("0"), self.reserved_margin.get(venue, Decimal("0")) - amount
            )
        for venue, amount in reservation.base_by_venue.items():
            self.reserved_base[venue] = max(
                Decimal("0"), self.reserved_base.get(venue, Decimal("0")) - amount
            )
        for venue, amount in reservation.position_delta_by_venue.items():
            self.reserved_position[venue] = (
                self.reserved_position.get(venue, Decimal("0")) - amount
            )
        for venue, count in reservation.messages_by_venue.items():
            self.reserved_messages[venue] = max(
                0, self.reserved_messages.get(venue, 0) - count
            )
        reservation.active = False

    def stats_snapshot(self) -> dict[str, Any]:
        return {
            "reserved_gross": str(self.reserved_gross),
            "reserved_cash": {key: str(value) for key, value in self.reserved_cash.items()},
            "reserved_margin": {key: str(value) for key, value in self.reserved_margin.items()},
            "reserved_position": {key: str(value) for key, value in self.reserved_position.items()},
            "reserved_messages": dict(self.reserved_messages),
            "reserved_residual": str(self.reserved_residual),
            "carried_residual_base": str(self.carried_residual_base),
            "active_reservations": sum(x.active for x in self.reservations.values()),
            "suspended_venues": {
                venue: dict(state)
                for venue, state in sorted(self.suspended_venues.items())
            },
            "reconciliation_decision": (
                self.last_reconciliation.decision.value
                if self.last_reconciliation is not None else "NOT_RUN"
            ),
            "pretrade_decision": (
                self.last_pretrade_validation.decision.value
                if self.last_pretrade_validation is not None else "NOT_RUN"
            ),
            "pretrade_failure_codes": (
                [
                    check.code
                    for check in self.last_pretrade_validation.failures
                ]
                if self.last_pretrade_validation is not None else []
            ),
            "exposure_control_state": self.exposure_state.value,
            "exposure_binding_events": (
                list(self.last_exposure_snapshot["binding_events"])
                if self.last_exposure_snapshot is not None else []
            ),
            "exposure_transition_count": len(self.exposure_transitions),
            "kill_switch_active": self.kill_switch,
        }

    def reconcile(
        self,
        *,
        plans: dict[str, ParentPlan],
        ledger: Any,
        now_ns: int,
        venue_orders: dict[str, Any] | None = None,
        venue_risk: tuple[VenueRiskSnapshot, ...] = (),
        require_venue_orders: bool = False,
        require_complete_opportunities: bool = False,
        position_tolerance: Decimal = Decimal("0"),
        balance_tolerance: Decimal = Decimal("0"),
        instruments: Any | None = None,
    ) -> ReconciliationReport:
        """Reconcile OMS, fill ledger and testnet REST/private account state."""
        issues = list(ledger.audit_order_fills(plans))
        local_orders = {
            order.order_id: order
            for plan in plans.values() for order in plan.children
        }
        for issue in issues:
            local = local_orders.get(str(issue.get("order_id") or ""))
            if local is not None:
                issue.setdefault("venue", local.venue)
                issue.setdefault("instrument_id", local.instrument_id)
        observed_orders = venue_orders or {}
        submitted = {
            str(event.get("order_id"))
            for event in ledger.events
            if event.get("event_type") == "ORDER_LIFECYCLE"
            and event.get("lifecycle_event") == "SUBMIT"
        }
        for order_id, snapshot in observed_orders.items():
            local = local_orders.get(order_id)
            if local is None:
                issues.append({
                    "code": "ORPHAN_VENUE_ORDER", "severity": "ERROR",
                    "order_id": order_id,
                    "venue": str(getattr(snapshot, "venue", "UNKNOWN")),
                    "instrument_id": str(
                        getattr(snapshot, "instrument_id", "UNKNOWN")
                    ),
                })
                continue
            venue_state = getattr(snapshot, "state", None)
            if venue_state is not None and OrderState(venue_state) != local.state:
                issues.append({
                    "code": "CONFLICTING_ORDER_STATE", "severity": "ERROR",
                    "order_id": order_id,
                    "venue": local.venue,
                    "instrument_id": local.instrument_id,
                    "local_state": local.state.value,
                    "venue_state": OrderState(venue_state).value,
                })
            venue_fill = Decimal(str(getattr(snapshot, "filled_quantity", "0")))
            if venue_fill != local.filled_quantity:
                issues.append({
                    "code": (
                        "MISSING_LOCAL_FILL"
                        if venue_fill > local.filled_quantity
                        else "EXTRA_LOCAL_FILL"
                    ),
                    "severity": "ERROR",
                    "order_id": order_id,
                    "venue": local.venue,
                    "instrument_id": local.instrument_id,
                    "local_quantity": str(local.filled_quantity),
                    "venue_quantity": str(venue_fill),
                })
        if require_venue_orders:
            for order_id in sorted(submitted - set(observed_orders)):
                local = local_orders.get(order_id)
                issues.append({
                    "code": "MISSING_VENUE_ORDER", "severity": "ERROR",
                    "order_id": order_id,
                    "venue": local.venue if local is not None else "UNKNOWN",
                    "instrument_id": (
                        local.instrument_id if local is not None else "UNKNOWN"
                    ),
                })

        if require_complete_opportunities:
            for plan in plans.values():
                if plan.state.value == "CLOSED":
                    continue
                venues = sorted({order.venue for order in plan.children})
                issues.append({
                    "code": "INCOMPLETE_OPPORTUNITY", "severity": "ERROR",
                    "parent_id": plan.parent_id,
                    "opportunity_id": plan.opportunity.opportunity_id,
                    "venues": venues,
                    "parent_state": plan.state.value,
                    "child_states": {
                        order.order_id: order.state.value
                        for order in plan.children
                    },
                })

        account_state = ledger.recalculate_account_state(
            mark_prices={
                (snapshot.venue, snapshot.instrument_id): snapshot.mark_price
                for snapshot in venue_risk if snapshot.mark_price is not None
            },
            initial_capital_by_venue={
                snapshot.venue: snapshot.initial_capital
                for snapshot in venue_risk
            },
            reserved_capacity=self.capacity_snapshot(),
        )
        margin_report = self.evaluate_margin(
            ledger=ledger, now_ns=now_ns,
            mark_prices={
                (snapshot.venue, snapshot.instrument_id): snapshot.mark_price
                for snapshot in venue_risk if snapshot.mark_price is not None
            },
            initial_capital_by_venue={
                snapshot.venue: snapshot.initial_capital
                for snapshot in venue_risk
            },
            instruments=instruments, venue_risk=venue_risk,
            enforce_controls=False,
        )
        margin_by_venue = {row.venue: row for row in margin_report.venues}

        def compare_decimal(
            code: str, venue: str, field: str,
            local_value: Decimal, venue_value: Decimal | None,
            tolerance: Decimal,
            instrument_id: str | None = None,
        ) -> None:
            if venue_value is None or abs(local_value - venue_value) <= tolerance:
                return
            issue: dict[str, Any] = {
                "code": code, "severity": "ERROR", "venue": venue,
                "field": field, "local_value": str(local_value),
                "venue_value": str(venue_value),
            }
            if instrument_id is not None:
                issue["instrument_id"] = instrument_id
            issues.append(issue)

        for snapshot in venue_risk:
            key = (snapshot.venue, snapshot.instrument_id)
            position_row = account_state["positions"].get(
                f"{snapshot.venue}:{snapshot.instrument_id}", {}
            )
            venue_row = account_state["venues"].get(snapshot.venue, {})
            local_position = Decimal(str(position_row.get("quantity", "0")))
            if abs(local_position - snapshot.position_base) > position_tolerance:
                issues.append({
                    "code": "POSITION_MISMATCH", "severity": "ERROR",
                    "venue": snapshot.venue,
                    "instrument_id": snapshot.instrument_id,
                    "local_position": str(local_position),
                    "venue_position": str(snapshot.position_base),
                })
            local_balance_delta = ledger.cash_deltas.get(
                snapshot.venue, Decimal("0")
            )
            if abs(local_balance_delta - snapshot.balance_delta) > balance_tolerance:
                issues.append({
                    "code": "BALANCE_MISMATCH", "severity": "ERROR",
                    "venue": snapshot.venue,
                    "local_balance_delta": str(local_balance_delta),
                    "venue_balance_delta": str(snapshot.balance_delta),
                })
            compare_decimal(
                "AVERAGE_PRICE_MISMATCH", snapshot.venue, "average_price",
                Decimal(str(position_row.get("average_price", "0"))),
                snapshot.average_price, position_tolerance,
                snapshot.instrument_id,
            )
            compare_decimal(
                "REALIZED_PNL_MISMATCH", snapshot.venue, "realized_pnl",
                Decimal(str(position_row.get("realized_pnl", "0"))),
                snapshot.realized_pnl, balance_tolerance,
                snapshot.instrument_id,
            )
            compare_decimal(
                "UNREALIZED_PNL_MISMATCH", snapshot.venue, "unrealized_pnl",
                Decimal(str(position_row.get("unrealized_pnl", "0"))),
                snapshot.unrealized_pnl, balance_tolerance,
                snapshot.instrument_id,
            )
            compare_decimal(
                "AVAILABLE_CASH_MISMATCH", snapshot.venue, "available_cash",
                Decimal(str(venue_row.get("available_cash", "0"))),
                snapshot.available_cash, balance_tolerance,
            )
            compare_decimal(
                "AVAILABLE_CAPITAL_MISMATCH", snapshot.venue,
                "available_capital",
                Decimal(str(venue_row.get("available_capital", "0"))),
                snapshot.available_capital, balance_tolerance,
            )
            compare_decimal(
                "RESERVED_CAPACITY_MISMATCH", snapshot.venue, "reserved_cash",
                Decimal(str(venue_row.get("reserved_cash", "0"))),
                snapshot.reserved_cash, balance_tolerance,
            )
            compare_decimal(
                "RESERVED_CAPACITY_MISMATCH", snapshot.venue, "reserved_margin",
                Decimal(str(venue_row.get("reserved_margin", "0"))),
                snapshot.reserved_margin, balance_tolerance,
            )
            margin_row = margin_by_venue.get(snapshot.venue)
            if margin_row is not None:
                compare_decimal(
                    "MARGIN_BALANCE_MISMATCH", snapshot.venue, "margin_balance",
                    margin_row.margin_balance, snapshot.margin_balance,
                    balance_tolerance,
                )
                compare_decimal(
                    "INITIAL_MARGIN_MISMATCH", snapshot.venue,
                    "initial_margin_required",
                    margin_row.initial_margin_required,
                    snapshot.initial_margin_required, balance_tolerance,
                )
                compare_decimal(
                    "MAINTENANCE_MARGIN_MISMATCH", snapshot.venue,
                    "maintenance_margin_required",
                    margin_row.maintenance_margin_required,
                    snapshot.maintenance_margin_required, balance_tolerance,
                )
                compare_decimal(
                    "LIQUIDATION_BUFFER_MISMATCH", snapshot.venue,
                    "liquidation_buffer", margin_row.liquidation_buffer,
                    snapshot.liquidation_buffer, balance_tolerance,
                )
            compare_decimal(
                "RESERVED_CAPACITY_MISMATCH", snapshot.venue,
                "reserved_position",
                Decimal(str(venue_row.get("reserved_position", "0"))),
                snapshot.reserved_position, position_tolerance,
            )
            if (
                snapshot.reserved_messages is not None
                and int(venue_row.get("reserved_messages", 0))
                != snapshot.reserved_messages
            ):
                issues.append({
                    "code": "RESERVED_CAPACITY_MISMATCH",
                    "severity": "ERROR", "venue": snapshot.venue,
                    "field": "reserved_messages",
                    "local_value": int(venue_row.get("reserved_messages", 0)),
                    "venue_value": snapshot.reserved_messages,
                })
            if not (
                snapshot.account_enabled
                and snapshot.venue_enabled
                and snapshot.strategy_enabled
            ) and not self.kill_switch:
                issues.append({
                    "code": "CONTROL_STATE_MISMATCH", "severity": "ERROR",
                    "venue": snapshot.venue,
                    "account_enabled": snapshot.account_enabled,
                    "venue_enabled": snapshot.venue_enabled,
                    "strategy_enabled": snapshot.strategy_enabled,
                    "local_kill_switch": self.kill_switch,
                })

        issue_venues: set[str] = set()
        for issue in issues:
            venue = str(issue.get("venue") or "")
            if venue and venue != "UNKNOWN":
                issue_venues.add(venue)
            issue_venues.update(str(item) for item in issue.get("venues", ()))
        observed_venues = {
            str(getattr(snapshot, "venue", ""))
            for snapshot in observed_orders.values()
        } | {snapshot.venue for snapshot in venue_risk}
        previously_suspended = set(self.suspended_venues)
        for venue in sorted(issue_venues):
            venue_issue = next(
                issue for issue in issues
                if issue.get("venue") == venue or venue in issue.get("venues", ())
            )
            self.suspend_venue(
                venue, code=str(venue_issue["code"]), now_ns=now_ns,
                detail=str(venue_issue.get("order_id") or venue_issue.get("parent_id") or ""),
            )
        resolved_venues = sorted(
            (previously_suspended & observed_venues) - issue_venues
        )
        for venue in resolved_venues:
            del self.suspended_venues[venue]

        decision = (
            ReconciliationDecision.APPROVED
            if not issues else ReconciliationDecision.REJECTED
        )
        report = ReconciliationReport(
            reconciliation_id=stable_id(
                "rms-reconciliation", now_ns, len(local_orders),
                len(observed_orders), len(issues),
            ),
            ts_ns=now_ns,
            decision=decision,
            issues=tuple(issues),
            local_orders=len(local_orders),
            venue_orders=len(observed_orders),
            fills=sum(event["event_type"] == "FILL" for event in ledger.events),
            venue_snapshots=len(venue_risk),
            suspended_venues=tuple(sorted(self.suspended_venues)),
            resolved_venues=tuple(resolved_venues),
        )
        self.last_reconciliation = report
        if (
            decision == ReconciliationDecision.REJECTED
            and any(str(issue["code"]) not in TRADE_BREAK_CODES for issue in issues)
        ):
            self.activate_kill_switch(
                now_ns=now_ns, reason="reconciliation rejected",
                source="RMS_RECONCILIATION", ledger=ledger,
            )
        return report
