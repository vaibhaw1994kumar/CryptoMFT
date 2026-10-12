"""Transaction cost analysis and opportunity attribution (MR-12, section 8).

Every number here is derived from ledger events and the same cost model the
router used to decide, so the post-trade report cannot drift from the
pre-trade estimate. Gross edge, fees, slippage, funding and unwind cost are
reported separately and only then netted.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from trading_platform_poc.minreq.ledger import EventLedger
from trading_platform_poc.minreq.router import ParentPlan, PlanState

BPS = Decimal(10_000)


def _decimal(value: Any, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    return Decimal(str(value))


@dataclass(slots=True)
class Bucket:
    key: str
    orders: int = 0
    fills: int = 0
    rejects: int = 0
    filled_quantity: Decimal = Decimal(0)
    notional: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)
    slippage_cost: Decimal = Decimal(0)
    maker_fills: int = 0
    taker_fills: int = 0

    @property
    def average_slippage_bps(self) -> Decimal:
        if self.notional == 0:
            return Decimal(0)
        return self.slippage_cost / self.notional * BPS

    @property
    def fee_bps(self) -> Decimal:
        if self.notional == 0:
            return Decimal(0)
        return self.fees / self.notional * BPS

    @property
    def fill_rate(self) -> Decimal:
        if self.orders == 0:
            return Decimal(0)
        return Decimal(self.fills) / Decimal(self.orders)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "orders": self.orders,
            "fills": self.fills,
            "rejects": self.rejects,
            "filled_quantity": str(self.filled_quantity),
            "notional": str(self.notional),
            "fees": str(self.fees),
            "fee_bps": str(self.fee_bps),
            "slippage_cost": str(self.slippage_cost),
            "average_slippage_bps": str(self.average_slippage_bps),
            "fill_rate": str(self.fill_rate),
            "maker_fills": self.maker_fills,
            "taker_fills": self.taker_fills,
        }


@dataclass(slots=True)
class OpportunityAttribution:
    plan_id: str
    opportunity_id: str
    economic_group: str
    state: str
    expected_edge: Decimal
    gross_edge: Decimal
    fees: Decimal
    slippage_cost: Decimal
    funding_cost: Decimal
    unwind_cost: Decimal
    net_pnl: Decimal
    residual_quantity: Decimal
    residual_duration_ns: int
    hedge_attempts: int
    legs: int

    @property
    def edge_capture(self) -> Decimal:
        if self.expected_edge == 0:
            return Decimal(0)
        return self.net_pnl / self.expected_edge

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "opportunity_id": self.opportunity_id,
            "economic_group": self.economic_group,
            "state": self.state,
            "expected_edge": str(self.expected_edge),
            "gross_edge": str(self.gross_edge),
            "fees": str(self.fees),
            "slippage_cost": str(self.slippage_cost),
            "funding_cost": str(self.funding_cost),
            "unwind_cost": str(self.unwind_cost),
            "net_pnl": str(self.net_pnl),
            "edge_capture": str(self.edge_capture),
            "residual_quantity": str(self.residual_quantity),
            "residual_duration_ns": self.residual_duration_ns,
            "hedge_attempts": self.hedge_attempts,
            "legs": self.legs,
        }


@dataclass(slots=True)
class TCAReport:
    run_id: str
    generated_ns: int
    by_venue: dict[str, Bucket]
    by_instrument: dict[str, Bucket]
    by_order_type: dict[str, Bucket]
    opportunities: list[OpportunityAttribution]
    missed_opportunities: list[dict[str, Any]]
    rejects_by_reason: dict[str, int]
    totals: dict[str, str]
    latency: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "generated_ns": self.generated_ns,
            "totals": self.totals,
            "by_venue": {
                key: bucket.to_dict()
                for key, bucket in sorted(self.by_venue.items())
            },
            "by_instrument": {
                key: bucket.to_dict()
                for key, bucket in sorted(self.by_instrument.items())
            },
            "by_order_type": {
                key: bucket.to_dict()
                for key, bucket in sorted(self.by_order_type.items())
            },
            "opportunities": [item.to_dict() for item in self.opportunities],
            "missed_opportunities": self.missed_opportunities,
            "rejects_by_reason": dict(sorted(self.rejects_by_reason.items())),
            "latency": self.latency,
        }

    def write_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
        return target

    def write_csv(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        for scope, buckets in (
            ("venue", self.by_venue),
            ("instrument", self.by_instrument),
            ("order_type", self.by_order_type),
        ):
            for key, bucket in sorted(buckets.items()):
                row = {"scope": scope}
                row.update(bucket.to_dict())
                rows.append(row)
        fieldnames = ["scope"] + list(Bucket("x").to_dict())
        with target.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return target

    def write_markdown(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# Transaction cost analysis: {self.run_id}",
            "",
            "## Totals",
            "",
            "| Metric | Value |",
            "| --- | --- |",
        ]
        for key, value in sorted(self.totals.items()):
            lines.append(f"| {key} | {value} |")
        lines.extend(["", "## By venue", ""])
        lines.append(
            "| Venue | Orders | Fills | Rejects | Notional | Fees (bps) | "
            "Slippage (bps) | Fill rate |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for key, bucket in sorted(self.by_venue.items()):
            lines.append(
                f"| {key} | {bucket.orders} | {bucket.fills} | {bucket.rejects} "
                f"| {bucket.notional:.2f} | {bucket.fee_bps:.4f} "
                f"| {bucket.average_slippage_bps:.4f} | {bucket.fill_rate:.4f} |"
            )
        lines.extend(["", "## Opportunity attribution", ""])
        lines.append(
            "| Plan | State | Expected edge | Gross | Fees | Slippage | "
            "Unwind | Net | Residual |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for item in self.opportunities:
            lines.append(
                f"| {item.plan_id} | {item.state} | {item.expected_edge:.6f} "
                f"| {item.gross_edge:.6f} | {item.fees:.6f} "
                f"| {item.slippage_cost:.6f} | {item.unwind_cost:.6f} "
                f"| {item.net_pnl:.6f} | {item.residual_quantity} |"
            )
        if self.missed_opportunities:
            lines.extend(
                [
                    "",
                    "## Missed opportunities",
                    "",
                    "| Reason | Count |",
                    "| --- | --- |",
                ]
            )
            counts: dict[str, int] = {}
            for item in self.missed_opportunities:
                counts[item["reason"]] = counts.get(item["reason"], 0) + 1
            for reason, count in sorted(counts.items()):
                lines.append(f"| {reason} | {count} |")
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return target


def build_tca_report(
    *,
    run_id: str,
    generated_ns: int,
    ledger: EventLedger,
    plans: Mapping[str, ParentPlan],
    missed_opportunities: Sequence[Mapping[str, Any]] = (),
    rejects: Sequence[Mapping[str, Any]] = (),
    latency: Mapping[str, Any] | None = None,
    funding_cost: Mapping[str, Decimal] | None = None,
) -> TCAReport:
    """Fold ledger events into venue, instrument and opportunity views."""

    by_venue: dict[str, Bucket] = {}
    by_instrument: dict[str, Bucket] = {}
    by_order_type: dict[str, Bucket] = {}
    order_index: dict[str, dict[str, Any]] = {}

    def bucket(store: dict[str, Bucket], key: str) -> Bucket:
        if key not in store:
            store[key] = Bucket(key)
        return store[key]

    for event in ledger.events(run_id=run_id):
        payload = event.payload
        if event.event_type == "order_created":
            order_index[str(event.child_order_id)] = {
                "venue": event.venue,
                "instrument_id": event.instrument_id,
                "order_type": payload.get("order_type", "limit"),
                "role": payload.get("role", "leg"),
            }
            bucket(by_venue, str(event.venue)).orders += 1
            bucket(by_instrument, str(event.instrument_id)).orders += 1
            bucket(
                by_order_type, str(payload.get("order_type", "limit"))
            ).orders += 1
        elif event.event_type == "order_state" and payload.get("state") == "rejected":
            meta = order_index.get(str(event.child_order_id), {})
            bucket(by_venue, str(event.venue or meta.get("venue"))).rejects += 1
            bucket(
                by_instrument, str(event.instrument_id or meta.get("instrument_id"))
            ).rejects += 1
            bucket(
                by_order_type, str(meta.get("order_type", "limit"))
            ).rejects += 1
        elif event.event_type == "fill":
            meta = order_index.get(str(event.child_order_id), {})
            quantity = _decimal(payload.get("quantity"))
            price = _decimal(payload.get("price"))
            fee = _decimal(payload.get("fee"))
            slippage_bps = _decimal(payload.get("slippage_bps"))
            notional = quantity * price
            slippage_cost = abs(notional * slippage_bps / BPS)
            liquidity = str(payload.get("liquidity", "taker"))
            for store, key in (
                (by_venue, str(event.venue)),
                (by_instrument, str(event.instrument_id)),
                (by_order_type, str(meta.get("order_type", "limit"))),
            ):
                target = bucket(store, key)
                target.fills += 1
                target.filled_quantity += quantity
                target.notional += notional
                target.fees += fee
                target.slippage_cost += slippage_cost
                if liquidity == "maker":
                    target.maker_fills += 1
                else:
                    target.taker_fills += 1

    attributions: list[OpportunityAttribution] = []
    funding = dict(funding_cost or {})
    for plan_id in sorted(plans):
        plan = plans[plan_id]
        gross = Decimal(0)
        fees = Decimal(0)
        slippage = Decimal(0)
        unwind = Decimal(0)
        for order in plan.children:
            if order.filled_quantity <= 0 or order.average_price is None:
                continue
            notional = order.filled_quantity * order.average_price
            direction = Decimal(-1) if order.side == "buy" else Decimal(1)
            contribution = direction * notional
            gross += contribution
            fees += order.fees
            if order.role in {"hedge", "unwind"}:
                unwind += abs(order.fees)
        for leg in plan.realized_legs:
            slippage += abs(leg.notional * leg.slippage_bps / BPS)
        funding_leg = funding.get(plan_id, Decimal(0))
        residual_duration = (
            max(0, plan.updated_ns - plan.residual_opened_ns)
            if plan.residual_opened_ns
            else 0
        )
        attributions.append(
            OpportunityAttribution(
                plan_id=plan.plan_id,
                opportunity_id=plan.opportunity_id,
                economic_group=plan.economic_group,
                state=plan.state.value,
                expected_edge=plan.candidate.net_edge,
                gross_edge=gross,
                fees=fees,
                slippage_cost=slippage,
                funding_cost=funding_leg,
                unwind_cost=unwind,
                net_pnl=gross - fees - funding_leg,
                residual_quantity=plan.residual_quantity,
                residual_duration_ns=residual_duration,
                hedge_attempts=plan.hedge_attempts,
                legs=len(plan.children),
            )
        )

    reject_counts: dict[str, int] = defaultdict(int)
    for reject in rejects:
        reject_counts[str(reject.get("reason", "unknown"))] += 1

    total_notional = sum((b.notional for b in by_venue.values()), Decimal(0))
    total_fees = sum((b.fees for b in by_venue.values()), Decimal(0))
    total_slippage = sum((b.slippage_cost for b in by_venue.values()), Decimal(0))
    total_net = sum((item.net_pnl for item in attributions), Decimal(0))
    total_gross = sum((item.gross_edge for item in attributions), Decimal(0))
    total_expected = sum((item.expected_edge for item in attributions), Decimal(0))
    closed = sum(
        1 for plan in plans.values() if plan.state is PlanState.CLOSED
    )
    totals = {
        "plans": str(len(plans)),
        "closed_plans": str(closed),
        "orders": str(sum(b.orders for b in by_venue.values())),
        "fills": str(sum(b.fills for b in by_venue.values())),
        "rejects": str(sum(b.rejects for b in by_venue.values())),
        "notional": str(total_notional),
        "gross_edge": str(total_gross),
        "expected_edge": str(total_expected),
        "fees": str(total_fees),
        "slippage_cost": str(total_slippage),
        "net_pnl": str(total_net),
        "missed_opportunities": str(len(missed_opportunities)),
        "residual_plans": str(
            sum(1 for item in attributions if item.residual_quantity > 0)
        ),
    }

    return TCAReport(
        run_id=run_id,
        generated_ns=generated_ns,
        by_venue=by_venue,
        by_instrument=by_instrument,
        by_order_type=by_order_type,
        opportunities=attributions,
        missed_opportunities=[dict(item) for item in missed_opportunities],
        rejects_by_reason=dict(reject_counts),
        totals=totals,
        latency=dict(latency or {}),
    )
