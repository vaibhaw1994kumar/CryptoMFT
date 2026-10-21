from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
import json

from .economics import EconomicEvent, EconomicEventType, EconomicLedger, EventSource, Posting
from .models import ChildOrder, ParentPlan, json_default, stable_id


def _parse_assets(venue: str, instrument_id: str) -> tuple[str, str]:
    parts = instrument_id.split("-")
    return (parts[0], parts[1]) if len(parts) >= 2 else (instrument_id, "USDT")


# parent_id of fills outside every plan: venue buy-in of a recalled loan
# (borrow.py) and simulated venue liquidation (margin.py).
OUT_OF_PLAN_PARENTS = frozenset({"borrow-forced-close", "venue-liquidation"})


def _match_ts(order: ChildOrder, report_ts_ns: int) -> int:
    """Venue time at which the order's current cumulative fill was first reached.

    Fills reach the ledger after report latency; economics that depend on
    *when* a position existed (funding) need the venue match time. Falls back
    to the report time when no venue-timestamped update carries the fill.
    """
    times = [
        int(item["event_ts_ns"]) for item in order.state_updates
        if item.get("event_ts_ns") is not None
        and Decimal(str(item.get("cumulative_filled_quantity", "0"))) >= order.filled_quantity
    ]
    return min([report_ts_ns, *times])


@dataclass(slots=True)
class PositionAccountState:
    venue: str
    instrument_id: str
    quantity: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    mark_price: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    gross_traded_notional: Decimal = Decimal("0")

    def to_dict(self) -> dict[str, object]:
        return {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in asdict(self).items()
        }


@dataclass(slots=True)
class VenueCapitalState:
    venue: str
    initial_capital: Decimal
    cash_delta: Decimal
    cash_balance: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees: Decimal
    funding: Decimal
    borrow_cost: Decimal
    transfer_fees: Decimal
    equity: Decimal
    reserved_cash: Decimal
    reserved_margin: Decimal
    reserved_gross_notional: Decimal
    reserved_position: Decimal
    reserved_messages: int
    available_cash: Decimal
    available_capital: Decimal

    def to_dict(self) -> dict[str, object]:
        return {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in asdict(self).items()
        }


class Ledger:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.positions: dict[tuple[str, str], Decimal] = {}
        self.cash_deltas: dict[str, Decimal] = {}
        self._fill_cumulative: dict[str, Decimal] = {}
        self._fill_notional_cumulative: dict[str, Decimal] = {}
        self._fee_cumulative: dict[str, Decimal] = {}
        self._fee_quantity_cumulative: dict[str, Decimal] = {}
        # Asset-level economics of the same fills (see economics.py). The trade
        # bookkeeping above stays; risk reads asset balances from here.
        self.economics = EconomicLedger()
        self.asset_resolver = _parse_assets
        # Called with each booked TRADE_FILL (the funding engine rebooks a
        # settlement when a fill effective before it is reported late).
        self.trade_fill_listeners: list = []
        # FinancingJournal when a plan store is attached: funding and borrow
        # events (and venue buy-ins) are written durably as they are booked.
        self.journal = None

    def record(self, event_type: str, ts_ns: int, **payload: object) -> None:
        ordinal = len(self.events) + 1
        self.events.append({
            "ledger_event_id": stable_id(
                "ledger-event", ordinal, event_type, ts_ns,
                payload.get("parent_id", ""), payload.get("order_id", ""),
            ),
            "event_type": event_type,
            "ts_ns": ts_ns,
            **payload,
        })

    def record_order_action(
        self,
        *,
        action: str,
        status: str,
        ts_ns: int,
        parent_id: str,
        opportunity_id: str,
        order_id: str,
        venue: str,
        instrument_id: str,
        source: str = "OMS",
        request_id: str = "",
        idempotency_key: str = "",
        target_order_id: str = "",
    ) -> None:
        self.record(
            "ORDER_LIFECYCLE", ts_ns,
            lifecycle_event=action,
            action_status=status,
            source=source,
            parent_id=parent_id,
            opportunity_id=opportunity_id,
            order_id=order_id,
            target_order_id=target_order_id,
            request_id=request_id,
            idempotency_key=idempotency_key,
            venue=venue,
            instrument_id=instrument_id,
        )

    def record_order_update(
        self,
        *,
        lifecycle_event: str,
        ts_ns: int,
        parent_id: str,
        opportunity_id: str,
        order_id: str,
        venue: str,
        instrument_id: str,
        source: str,
        update_id: str,
        cumulative_filled_quantity: Decimal,
        average_price: Decimal,
        fee: Decimal,
        reason: str = "",
    ) -> None:
        self.record(
            "ORDER_LIFECYCLE", ts_ns,
            lifecycle_event=lifecycle_event,
            action_status="OBSERVED",
            source=source,
            parent_id=parent_id,
            opportunity_id=opportunity_id,
            order_id=order_id,
            update_id=update_id,
            venue=venue,
            instrument_id=instrument_id,
            cumulative_filled_quantity=cumulative_filled_quantity,
            average_price=average_price,
            fee=fee,
            reason=reason,
        )

    def apply_fill(
        self, order: ChildOrder, ts_ns: int, *,
        economic_type: EconomicEventType = EconomicEventType.TRADE_FILL,
    ) -> None:
        """Apply only the unexplained cumulative delta; repeated reports are idempotent.

        ``economic_type`` is TRADE_FILL except for a venue buy-in of a recalled
        loan (BORROW_FORCED_CLOSE), which is a fill outside every plan.
        """
        previous_fill = self._fill_cumulative.get(order.order_id, Decimal("0"))
        previous_notional = self._fill_notional_cumulative.get(
            order.order_id, Decimal("0")
        )
        previous_fee = self._fee_cumulative.get(order.order_id, Decimal("0"))
        if order.filled_quantity < previous_fill:
            raise ValueError(f"fill quantity regressed for {order.order_id}")
        # Rebates are negative and grow more negative: compare magnitudes.
        if abs(order.fee) < abs(previous_fee) or order.fee * previous_fee < 0:
            raise ValueError(f"fee regressed for {order.order_id}")
        base_asset, quote_asset = self.asset_resolver(order.venue, order.instrument_id)
        fee_asset = order.fee_asset or quote_asset
        fill_delta = order.filled_quantity - previous_fill
        cumulative_notional = order.filled_quantity * order.average_price
        notional_delta = cumulative_notional - previous_notional
        fee_delta = order.fee - previous_fee
        if fill_delta:
            signed = fill_delta if order.side.value == "BUY" else -fill_delta
            key = (order.venue, order.instrument_id)
            self.positions[key] = self.positions.get(key, Decimal("0")) + signed
            signed_cash = (
                -notional_delta if order.side.value == "BUY" else notional_delta
            )
            self.cash_deltas[order.venue] = (
                self.cash_deltas.get(order.venue, Decimal("0")) + signed_cash
            )
            self._fill_cumulative[order.order_id] = order.filled_quantity
            self._fill_notional_cumulative[order.order_id] = cumulative_notional
            fill_id = stable_id("fill", order.order_id, order.filled_quantity, order.average_price)
            fill_event = EconomicEvent(
                event_type=economic_type,
                idempotency_key=f"fill|{order.order_id}|{order.filled_quantity}|{cumulative_notional}",
                ts_ns=ts_ns, effective_ts_ns=_match_ts(order, ts_ns),
                postings=(
                    Posting(order.venue, "main", base_asset, signed),
                    Posting(order.venue, "main", quote_asset, signed_cash),
                ),
                venue=order.venue, instrument_id=order.instrument_id,
                order_id=order.order_id, fill_id=fill_id, reference_id=order.parent_id,
                valuation_price=notional_delta / fill_delta, valuation_source="fill price",
                details=(("side", order.side.value), ("liquidity", order.liquidity)),
            )
            if self.economics.apply(fill_event):
                if economic_type != EconomicEventType.TRADE_FILL and self.journal is not None:
                    self.journal.append_forced_fill(order, ts_ns, economic_type)
                for listener in self.trade_fill_listeners:
                    listener(fill_event)
            self.record(
                "FILL", ts_ns,
                fill_id=fill_id,
                parent_id=order.parent_id,
                opportunity_id=order.opportunity_id,
                order_id=order.order_id,
                venue=order.venue,
                instrument_id=order.instrument_id,
                side=order.side.value,
                fill_quantity=fill_delta,
                fill_price=(
                    notional_delta / fill_delta
                    if fill_delta else order.average_price
                ),
                fill_notional=notional_delta,
                cumulative_filled_quantity=order.filled_quantity,
                average_price=order.average_price,
                fee=order.fee,
                fee_delta=fee_delta,
                order=asdict(order),
            )
        elif notional_delta:
            signed_cash = (
                -notional_delta if order.side.value == "BUY" else notional_delta
            )
            self.cash_deltas[order.venue] = (
                self.cash_deltas.get(order.venue, Decimal("0")) + signed_cash
            )
            self._fill_notional_cumulative[order.order_id] = cumulative_notional
            self.economics.apply(EconomicEvent(
                event_type=EconomicEventType.TRADE_FILL,
                idempotency_key=f"fill-adjust|{order.order_id}|{order.filled_quantity}|{cumulative_notional}",
                ts_ns=ts_ns,
                postings=(Posting(order.venue, "main", quote_asset, signed_cash),),
                venue=order.venue, instrument_id=order.instrument_id,
                order_id=order.order_id, reference_id=order.parent_id,
                details=(("adjustment", "notional"),),
            ))
            self.record(
                "FILL_NOTIONAL_ADJUSTMENT", ts_ns,
                parent_id=order.parent_id,
                opportunity_id=order.opportunity_id,
                order_id=order.order_id,
                venue=order.venue,
                instrument_id=order.instrument_id,
                notional_delta=notional_delta,
                cumulative_notional=cumulative_notional,
                average_price=order.average_price,
            )
        if fee_delta:
            fee_quantity = order.fee_quantity if order.fee_asset else order.fee
            previous_quantity = self._fee_quantity_cumulative.get(order.order_id, Decimal("0"))
            quantity_delta = fee_quantity - previous_quantity
            unpriced = "FEE_ASSET_UNPRICED" in order.fee_source
            self.economics.apply(EconomicEvent(
                event_type=EconomicEventType.FEE,
                idempotency_key=f"fee|{order.order_id}|{order.fee}|{fee_quantity}",
                ts_ns=ts_ns,
                # Cost: the fee asset leaves the account; rebate: it arrives.
                postings=() if unpriced else (Posting(order.venue, "main", fee_asset, -quantity_delta),),
                venue=order.venue, instrument_id=order.instrument_id,
                order_id=order.order_id, reference_id=order.parent_id,
                source=EventSource.ASSUMED if "ASSUMED" in order.fee_source else EventSource.SIMULATED,
                reporting_value=fee_delta,
                valuation_price=(fee_delta / quantity_delta) if quantity_delta and not unpriced else None,
                valuation_source="fill price" if fee_asset in (base_asset, quote_asset) else "asset_prices",
                details=(
                    ("fee_asset", fee_asset), ("fee_quantity_delta", str(quantity_delta)),
                    ("fee_rate_bps", "" if order.fee_rate_bps is None else str(order.fee_rate_bps)),
                    ("liquidity", order.liquidity), ("fee_source", order.fee_source),
                    ("fee_quantity_unknown", "true" if unpriced else "false"),
                ),
            ))
            self._fee_quantity_cumulative[order.order_id] = fee_quantity
            if fee_asset == quote_asset:
                # Legacy venue cash is the quote currency; a fee charged in
                # base or another token does not come out of it.
                self.cash_deltas[order.venue] = (
                    self.cash_deltas.get(order.venue, Decimal("0")) - fee_delta
                )
            self._fee_cumulative[order.order_id] = order.fee
            if not fill_delta:
                self.record(
                    "FEE_ADJUSTMENT", ts_ns,
                    parent_id=order.parent_id,
                    opportunity_id=order.opportunity_id,
                    order_id=order.order_id,
                    venue=order.venue,
                    instrument_id=order.instrument_id,
                    fee_delta=fee_delta,
                    cumulative_fee=order.fee,
                )

    def apply_funding(self, event: EconomicEvent) -> bool:
        """Book a FUNDING settlement in both ledgers (legacy cash and equity included)."""
        if not self.economics.apply(event):
            return False
        self._record_funding(event, reversal=False)
        self._journal(event)
        return True

    def reverse_funding(self, event_id: str, ts_ns: int, reason: str) -> EconomicEvent:
        reversal = self.economics.reverse(event_id, ts_ns, reason)
        self._record_funding(reversal, reversal=True)
        self._journal(reversal)
        return reversal

    def apply_borrow(self, event: EconomicEvent) -> bool:
        """Book a BORROW_* event; accrued interest also enters legacy equity as a cost."""
        if not self.economics.apply(event):
            return False
        self._record_borrow(event, reversal=False)
        self._journal(event)
        return True

    def reverse_borrow(self, event_id: str, ts_ns: int, reason: str) -> EconomicEvent:
        reversal = self.economics.reverse(event_id, ts_ns, reason)
        self._record_borrow(reversal, reversal=True)
        self._journal(reversal)
        return reversal

    def apply_transfer(self, event: EconomicEvent) -> bool:
        """Book a TRANSFER / TRANSFER_FEE event (ECO05); journaled for restarts."""
        if not self.economics.apply(event):
            return False
        self._record_transfer(event, reversal=False)
        self._journal(event)
        return True

    def _record_transfer(self, event: EconomicEvent, *, reversal: bool) -> None:
        kind = dict(event.details).get("reversed_type", "") if reversal else event.event_type.value
        reporting = self.economics.reporting_currency
        for posting in event.postings:
            if posting.account == "main" and posting.asset == reporting:
                # Legacy venue cash is the reporting/quote currency.
                self.cash_deltas[posting.venue] = self.cash_deltas.get(posting.venue, Decimal("0")) + posting.quantity
        fee_cost = (
            -event.reporting_value
            if kind == EconomicEventType.TRANSFER_FEE.value and event.reporting_value is not None else ""
        )
        self.record(
            "TRANSFER", event.effective, economic_event_id=event.event_id, transfer_event_type=kind,
            reversal=reversal, venue=event.venue, transfer_fee_cost=fee_cost,
            postings=[(p.venue, p.account, p.asset, str(p.quantity)) for p in event.postings],
            details=dict(event.details),
        )

    def _journal(self, event: EconomicEvent) -> None:
        if self.journal is not None:
            self.journal.append_event(event)

    def replay_financing(self, event: EconomicEvent) -> None:
        """Re-book a journaled FUNDING/BORROW_* event or reversal after a restart."""
        reversal = event.event_type == EconomicEventType.REVERSAL
        kind = dict(event.details).get("reversed_type", "") if reversal else event.event_type.value
        if not self.economics.apply(event):
            return
        if kind == EconomicEventType.FUNDING.value:
            self._record_funding(event, reversal=reversal)
        elif kind in (EconomicEventType.TRANSFER.value, EconomicEventType.TRANSFER_FEE.value):
            self._record_transfer(event, reversal=reversal)
        else:
            self._record_borrow(event, reversal=reversal)

    def _record_borrow(self, event: EconomicEvent, *, reversal: bool) -> None:
        kind = dict(event.details).get("reversed_type", "") if reversal else event.event_type.value
        cost = (
            -event.reporting_value
            if kind == EconomicEventType.BORROW_ACCRUAL.value and event.reporting_value is not None
            else ""
        )
        self.record(
            "BORROW", event.effective,
            economic_event_id=event.event_id, borrow_event_type=kind, reverses=event.reverses,
            reversal=reversal, venue=event.venue, instrument_id=event.instrument_id,
            borrow_cost=cost,
            postings=[(p.account, p.asset, str(p.quantity)) for p in event.postings],
            details=dict(event.details),
        )

    def _record_funding(self, event: EconomicEvent, *, reversal: bool) -> None:
        reporting = self.economics.reporting_currency
        value = event.reporting_value
        if value is not None:
            # Legacy venue cash is the reporting/quote currency.
            self.cash_deltas[event.venue] = self.cash_deltas.get(event.venue, Decimal("0")) + value
        self.record(
            "FUNDING", event.effective,
            economic_event_id=event.event_id, reverses=event.reverses,
            reversal=reversal, venue=event.venue, instrument_id=event.instrument_id,
            amount=value if value is not None else "",
            postings=[(p.asset, str(p.quantity)) for p in event.postings],
            reporting_currency=reporting, details=dict(event.details),
        )

    def rebuild_from_plans(
        self, plans: dict[str, ParentPlan], ts_ns: int
    ) -> None:
        """Rebuild accounting from durable nonterminal parent snapshots after restart."""
        for plan in sorted(plans.values(), key=lambda item: item.parent_id):
            for order in plan.children:
                if order.state.value == "CREATED" and order.filled_quantity == 0:
                    continue
                self.record_order_action(
                    action="SUBMIT", status="RECONSTRUCTED", ts_ns=ts_ns,
                    parent_id=plan.parent_id,
                    opportunity_id=order.opportunity_id,
                    order_id=order.order_id,
                    venue=order.venue,
                    instrument_id=order.instrument_id,
                    source="RECOVERY",
                )
                self.record_order_update(
                    lifecycle_event=order.state.value, ts_ns=ts_ns,
                    parent_id=plan.parent_id,
                    opportunity_id=order.opportunity_id,
                    order_id=order.order_id,
                    venue=order.venue,
                    instrument_id=order.instrument_id,
                    source="RECOVERY",
                    update_id=stable_id("recovered-order", order.order_id, ts_ns),
                    cumulative_filled_quantity=order.filled_quantity,
                    average_price=order.average_price,
                    fee=order.fee,
                    reason="rebuilt from durable parent snapshot",
                )
                self.apply_fill(order, ts_ns)
        self.record(
            "RECOVERY_ACCOUNT_STATE_REBUILT", ts_ns,
            parent_count=len(plans),
            order_count=sum(len(plan.children) for plan in plans.values()),
        )

    def recalculate_account_state(
        self,
        *,
        mark_prices: dict[tuple[str, str], Decimal] | None = None,
        initial_capital_by_venue: dict[str, Decimal] | None = None,
        reserved_capacity: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Rebuild positions, PnL, balances and available capacity from events."""
        marks = mark_prices or {}
        initial = initial_capital_by_venue or {}
        positions: dict[tuple[str, str], PositionAccountState] = {}
        fees_by_venue: dict[str, Decimal] = {}
        funding_by_venue: dict[str, Decimal] = {}
        borrow_cost_by_venue: dict[str, Decimal] = {}
        transfer_fees_by_venue: dict[str, Decimal] = {}
        for event in self.events:
            event_type = event["event_type"]
            if event_type == "FILL":
                key = (str(event["venue"]), str(event["instrument_id"]))
                state = positions.setdefault(
                    key, PositionAccountState(key[0], key[1])
                )
                quantity = Decimal(str(event["fill_quantity"]))
                signed_fill = quantity if event["side"] == "BUY" else -quantity
                fill_price = Decimal(str(
                    event.get("fill_price", event["average_price"])
                ))
                old_quantity = state.quantity
                if old_quantity == 0 or old_quantity * signed_fill > 0:
                    new_quantity = old_quantity + signed_fill
                    state.average_price = (
                        abs(old_quantity) * state.average_price
                        + abs(signed_fill) * fill_price
                    ) / abs(new_quantity)
                else:
                    closed = min(abs(old_quantity), abs(signed_fill))
                    direction = Decimal("1") if old_quantity > 0 else Decimal("-1")
                    state.realized_pnl += (
                        fill_price - state.average_price
                    ) * closed * direction
                    new_quantity = old_quantity + signed_fill
                    if new_quantity == 0:
                        state.average_price = Decimal("0")
                    elif old_quantity * new_quantity < 0:
                        state.average_price = fill_price
                state.quantity = new_quantity
                state.gross_traded_notional += abs(Decimal(str(
                    event.get(
                        "fill_notional",
                        quantity * fill_price,
                    )
                )))
                fee_delta = Decimal(str(event.get("fee_delta", "0")))
                state.fees += fee_delta
                fees_by_venue[key[0]] = fees_by_venue.get(
                    key[0], Decimal("0")
                ) + fee_delta
            elif event_type == "FUNDING" and event.get("amount") not in (None, ""):
                venue = str(event["venue"])
                funding_by_venue[venue] = funding_by_venue.get(
                    venue, Decimal("0")
                ) + Decimal(str(event["amount"]))
            elif event_type == "TRANSFER" and event.get("transfer_fee_cost") not in (None, ""):
                venue = str(event["venue"])
                transfer_fees_by_venue[venue] = transfer_fees_by_venue.get(
                    venue, Decimal("0")
                ) + Decimal(str(event["transfer_fee_cost"]))
            elif event_type == "BORROW" and event.get("borrow_cost") not in (None, ""):
                venue = str(event["venue"])
                borrow_cost_by_venue[venue] = borrow_cost_by_venue.get(
                    venue, Decimal("0")
                ) + Decimal(str(event["borrow_cost"]))
            elif event_type == "FEE_ADJUSTMENT":
                venue = str(event["venue"])
                key = (venue, str(event["instrument_id"]))
                state = positions.setdefault(
                    key, PositionAccountState(key[0], key[1])
                )
                fee_delta = Decimal(str(event["fee_delta"]))
                state.fees += fee_delta
                fees_by_venue[venue] = fees_by_venue.get(
                    venue, Decimal("0")
                ) + fee_delta

        for key, state in positions.items():
            state.mark_price = marks.get(key, state.average_price)
            state.unrealized_pnl = (
                state.mark_price - state.average_price
            ) * state.quantity

        capacity = reserved_capacity or {}
        reserved_cash = capacity.get("reserved_cash", {})
        reserved_margin = capacity.get("reserved_margin", {})
        reserved_position = capacity.get("reserved_position", {})
        reserved_messages = capacity.get("reserved_messages", {})
        reserved_gross = Decimal(str(capacity.get("reserved_gross", "0")))
        venues = sorted({
            *initial, *self.cash_deltas, *fees_by_venue, *funding_by_venue, *borrow_cost_by_venue,
            *transfer_fees_by_venue,
            *(venue for venue, _ in positions),
            *getattr(reserved_cash, "keys", lambda: ())(),
            *getattr(reserved_margin, "keys", lambda: ())(),
        })
        venue_states: dict[str, VenueCapitalState] = {}
        for venue in venues:
            venue_positions = [
                state for (item_venue, _), state in positions.items()
                if item_venue == venue
            ]
            realized = sum(
                (state.realized_pnl for state in venue_positions), Decimal("0")
            )
            unrealized = sum(
                (state.unrealized_pnl for state in venue_positions), Decimal("0")
            )
            fees = fees_by_venue.get(venue, Decimal("0"))
            opening = initial.get(venue, Decimal("0"))
            cash_delta = self.cash_deltas.get(venue, Decimal("0"))
            cash_reserved = Decimal(str(getattr(reserved_cash, "get", lambda *_: "0")(
                venue, "0"
            )))
            margin_reserved = Decimal(str(getattr(reserved_margin, "get", lambda *_: "0")(
                venue, "0"
            )))
            position_reserved = Decimal(str(getattr(
                reserved_position, "get", lambda *_: "0"
            )(venue, "0")))
            messages_reserved = int(getattr(
                reserved_messages, "get", lambda *_: 0
            )(venue, 0))
            funding = funding_by_venue.get(venue, Decimal("0"))
            borrow_cost = borrow_cost_by_venue.get(venue, Decimal("0"))
            # Interest at its accrual-time value; the economic reconciliation
            # reports the borrowed asset's later revaluation separately.
            transfer_fees = transfer_fees_by_venue.get(venue, Decimal("0"))
            equity = opening + realized + unrealized - fees + funding - borrow_cost - transfer_fees
            venue_states[venue] = VenueCapitalState(
                venue=venue,
                initial_capital=opening,
                cash_delta=cash_delta,
                cash_balance=opening + cash_delta,
                realized_pnl=realized,
                unrealized_pnl=unrealized,
                fees=fees,
                funding=funding,
                borrow_cost=borrow_cost,
                transfer_fees=transfer_fees,
                equity=equity,
                reserved_cash=cash_reserved,
                reserved_margin=margin_reserved,
                reserved_gross_notional=reserved_gross,
                reserved_position=position_reserved,
                reserved_messages=messages_reserved,
                available_cash=opening + cash_delta - cash_reserved,
                available_capital=equity - cash_reserved - margin_reserved,
            )
        return {
            "schema_version": "1.0.0",
            "positions": {
                f"{venue}:{instrument_id}": state.to_dict()
                for (venue, instrument_id), state in sorted(positions.items())
            },
            "venues": {
                venue: state.to_dict()
                for venue, state in sorted(venue_states.items())
            },
        }

    def audit_order_fills(self, plans: dict[str, ParentPlan]) -> list[dict[str, object]]:
        """Return correlation, duplication and accounting defects without mutation."""
        issues: list[dict[str, object]] = []
        orders = {
            order.order_id: order
            for plan in plans.values() for order in plan.children
        }
        event_ids = [str(event["ledger_event_id"]) for event in self.events]
        duplicate_event_ids = sorted(
            event_id for event_id, count in Counter(event_ids).items() if count > 1
        )
        for event_id in duplicate_event_ids:
            issues.append({
                "code": "DUPLICATE_LEDGER_EVENT", "severity": "ERROR",
                "ledger_event_id": event_id,
            })
        fill_ids: set[str] = set()
        fill_totals: dict[str, Decimal] = {}
        # Fills that belong to no plan by design (venue buy-ins, simulated
        # liquidations): known when the economic ledger booked them as such.
        out_of_plan = {
            e.order_id for e in self.economics.events
            if e.event_type in (EconomicEventType.BORROW_FORCED_CLOSE, EconomicEventType.LIQUIDATION)
        }
        for event in self.events:
            order_id = str(event.get("order_id") or "")
            if event["event_type"] in {
                "ORDER_LIFECYCLE", "FILL", "FEE_ADJUSTMENT",
                "FILL_NOTIONAL_ADJUSTMENT",
            }:
                if order_id in out_of_plan and str(event.get("parent_id", "")) in OUT_OF_PLAN_PARENTS:
                    pass                       # still checked for duplicates below
                elif not order_id or order_id not in orders:
                    issues.append({
                        "code": "ORPHAN_LEDGER_EVENT", "severity": "ERROR",
                        "ledger_event_id": event["ledger_event_id"],
                        "order_id": order_id,
                    })
            if event["event_type"] != "FILL":
                continue
            fill_id = str(event.get("fill_id") or "")
            if not fill_id:
                issues.append({
                    "code": "UNEXPLAINED_FILL", "severity": "ERROR",
                    "ledger_event_id": event["ledger_event_id"],
                    "order_id": order_id,
                })
            elif fill_id in fill_ids:
                issues.append({
                    "code": "DUPLICATE_FILL", "severity": "ERROR",
                    "fill_id": fill_id, "order_id": order_id,
                })
            fill_ids.add(fill_id)
            fill_totals[order_id] = fill_totals.get(order_id, Decimal("0")) + Decimal(
                str(event.get("fill_quantity", "0"))
            )
        for order_id, order in orders.items():
            accounted = fill_totals.get(order_id, Decimal("0"))
            if accounted != order.filled_quantity:
                issues.append({
                    "code": "FILL_ACCOUNTING_MISMATCH", "severity": "ERROR",
                    "order_id": order_id,
                    "ledger_quantity": str(accounted),
                    "order_quantity": str(order.filled_quantity),
                })
        return issues

    def record_plan(self, plan: ParentPlan, ts_ns: int) -> None:
        self.record("PLAN_CLOSED", ts_ns, plan=asdict(plan))

    def write_jsonl(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for event in self.events:
                handle.write(json.dumps(event, default=json_default, sort_keys=True) + "\n")
