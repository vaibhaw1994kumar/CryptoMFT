from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
from typing import Any, Iterable

from .models import MarketEvent, ParentPlan, Side, stable_id


def _utc_date(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / 1_000_000_000, timezone.utc).date().isoformat()


class BasicTCA:
    """Deterministic run/daily transaction-cost analysis for paper execution."""

    CALCULATION_VERSION = "basic-tca-v1"

    @staticmethod
    def _write(report: dict[str, Any], path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)

    @classmethod
    def build(
        cls, *, oms: Any, events: Iterable[MarketEvent], correlation_id: str,
        source_path: str, source_sha256: str, scope: str = "RUN",
        period_date: str | None = None, plans: Iterable[ParentPlan] | None = None,
    ) -> dict[str, Any]:
        selected_plans = sorted(
            list(plans if plans is not None else oms.plans.values()),
            key=lambda plan: (plan.opportunity.observed_ts_ns, plan.parent_id),
        )
        selected_events = list(events)
        events_by_id = {event.event_id: event for event in selected_events}
        request_type: dict[str, str] = {}
        for row in oms.router.trace:
            if row.get("record_type") != "ORDER_REQUEST":
                continue
            request = row.get("request", {})
            if request.get("action") == "SUBMIT":
                request_type[str(request["order_id"])] = str(request["order_type"])

        groups: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(lambda: {
            "orders": 0, "fills": 0, "rejects": 0, "cancels": 0,
            "partials": 0, "timeouts": 0, "unwind_orders": 0,
            "unwind_fills": 0, "missed_opportunities": 0,
            "filled_quantity": Decimal("0"),
            "benchmark_notional": Decimal("0"),
            "filled_notional": Decimal("0"),
            "slippage_quote": Decimal("0"), "fees": Decimal("0"),
        })
        opportunity_rows: list[dict[str, Any]] = []
        all_source_ids: set[str] = set()
        all_raw_refs: set[str] = set()

        for plan in selected_plans:
            primary = [order for order in plan.children if order.order_role == "PRIMARY"]
            executed = bool(primary) and all(order.state.value == "FILLED" for order in primary)
            within_residual = abs(plan.residual_base) <= oms.residual_policy.max_residual_base
            missed = not (executed and within_residual)
            missed_reason = "" if not missed else (plan.terminal_outcome or plan.state.value)
            source_ids = list(plan.opportunity.source_event_ids)
            raw_refs = sorted({
                events_by_id[event_id].raw_ref for event_id in source_ids
                if event_id in events_by_id and events_by_id[event_id].raw_ref
            })
            all_source_ids.update(source_ids)
            all_raw_refs.update(raw_refs)
            opportunity_rows.append({
                "opportunity_id": plan.opportunity.opportunity_id,
                "parent_id": plan.parent_id,
                "observed_ts_ns": plan.opportunity.observed_ts_ns,
                "expected_net_edge_bps": str(plan.opportunity.estimated_net_edge_bps),
                "executed": executed,
                "missed": missed,
                "missed_reason": missed_reason,
                "terminal_outcome": plan.terminal_outcome or "OPEN",
                "residual_base": str(plan.residual_base),
                "source_event_ids": source_ids,
                "raw_refs": raw_refs,
            })
            missed_group_keys: set[tuple[str, str, str]] = set()
            for order in plan.children:
                order_type = request_type.get(order.order_id, order.order_type)
                key = (order.venue, order.instrument_id, order_type)
                stats = groups[key]
                stats["orders"] += 1
                stats["rejects"] += int(order.state.value == "REJECTED")
                stats["cancels"] += int(order.state.value == "CANCELLED")
                stats["partials"] += int(order.state.value == "PARTIAL")
                stats["timeouts"] += int(order.state.value == "TIMED_OUT")
                stats["unwind_orders"] += int(order.order_role == "UNWIND")
                if missed and order.order_role == "PRIMARY":
                    missed_group_keys.add(key)
                if not order.filled_quantity:
                    continue
                stats["fills"] += 1
                stats["unwind_fills"] += int(order.order_role == "UNWIND")
                stats["filled_quantity"] += order.filled_quantity
                benchmark_notional = order.filled_quantity * order.limit_price
                fill_notional = order.filled_quantity * order.average_price
                adverse_per_unit = (
                    order.average_price - order.limit_price
                    if order.side == Side.BUY
                    else order.limit_price - order.average_price
                )
                stats["benchmark_notional"] += benchmark_notional
                stats["filled_notional"] += fill_notional
                stats["slippage_quote"] += adverse_per_unit * order.filled_quantity
                stats["fees"] += order.fee
            for key in missed_group_keys:
                groups[key]["missed_opportunities"] += 1

        group_rows = []
        for (venue, instrument_id, order_type), stats in sorted(groups.items()):
            benchmark = stats["benchmark_notional"]
            slippage_bps = (
                stats["slippage_quote"] / benchmark * Decimal("10000")
                if benchmark else Decimal("0")
            )
            group_rows.append({
                "venue": venue, "instrument_id": instrument_id,
                "order_type": order_type,
                **{
                    key: (str(value) if isinstance(value, Decimal) else value)
                    for key, value in stats.items()
                },
                "weighted_slippage_bps": str(slippage_bps),
            })

        totals = {
            key: sum((row[key] for row in groups.values()), Decimal("0"))
            for key in (
                "filled_quantity", "benchmark_notional", "filled_notional",
                "slippage_quote", "fees",
            )
        }
        weighted_slippage_bps = (
            totals["slippage_quote"] / totals["benchmark_notional"] * Decimal("10000")
            if totals["benchmark_notional"] else Decimal("0")
        )
        event_times = [event.receive_ts_ns for event in selected_events]
        plan_times = [plan.opportunity.observed_ts_ns for plan in selected_plans]
        timestamps = event_times or plan_times or [0]
        source_ids_sorted = sorted(all_source_ids)
        raw_refs_sorted = sorted(all_raw_refs)
        source_event_raw_refs = {
            event_id: (
                events_by_id[event_id].raw_ref
                if event_id in events_by_id and events_by_id[event_id].raw_ref
                else None
            )
            for event_id in source_ids_sorted
        }
        return {
            "schema_version": "1.0.0",
            "calculation_version": cls.CALCULATION_VERSION,
            "report_id": stable_id(
                "tca-report", scope, period_date or "", correlation_id,
                *(plan.parent_id for plan in selected_plans),
            ),
            "scope": scope,
            "correlation_id": correlation_id,
            "period": {
                "date_utc": period_date,
                "start_ts_ns": min(timestamps),
                "end_ts_ns": max(timestamps),
            },
            "summary": {
                "opportunities_detected": len(selected_plans),
                "opportunities_executed": sum(not row["missed"] for row in opportunity_rows),
                "missed_opportunities": sum(row["missed"] for row in opportunity_rows),
                "orders": sum(row["orders"] for row in groups.values()),
                "fills": sum(row["fills"] for row in groups.values()),
                "rejects": sum(row["rejects"] for row in groups.values()),
                "cancels": sum(row["cancels"] for row in groups.values()),
                "partials": sum(row["partials"] for row in groups.values()),
                "timeouts": sum(row["timeouts"] for row in groups.values()),
                "unwind_orders": sum(row["unwind_orders"] for row in groups.values()),
                "unwind_fills": sum(row["unwind_fills"] for row in groups.values()),
                "open_residual_base": str(sum(
                    (abs(plan.residual_base) for plan in selected_plans if plan.state.value != "CLOSED"),
                    Decimal("0"),
                )),
                "filled_quantity": str(totals["filled_quantity"]),
                "benchmark_notional": str(totals["benchmark_notional"]),
                "filled_notional": str(totals["filled_notional"]),
                "slippage_quote": str(totals["slippage_quote"]),
                "weighted_slippage_bps": str(weighted_slippage_bps),
                "fees": str(totals["fees"]),
                "unwind_cost": str(sum((plan.unwind_cost for plan in selected_plans), Decimal("0"))),
                "net_pnl": str(sum((plan.net_pnl for plan in selected_plans), Decimal("0"))),
            },
            "groups": group_rows,
            "opportunities": opportunity_rows,
            "lineage": {
                "canonical_source_path": source_path,
                "canonical_source_sha256": source_sha256,
                "source_event_ids": source_ids_sorted,
                "raw_refs": raw_refs_sorted,
                "source_event_raw_refs": source_event_raw_refs,
                "raw_reference_coverage": (
                    sum(value is not None for value in source_event_raw_refs.values())
                    / len(source_ids_sorted) if source_ids_sorted else 1.0
                ),
            },
            "calculation": {
                "benchmark": "child decision/limit price at dispatch",
                "buy_slippage_quote": "(average_fill_price - benchmark_price) * filled_quantity",
                "sell_slippage_quote": "(benchmark_price - average_fill_price) * filled_quantity",
                "weighted_slippage_bps": "sum(slippage_quote) / sum(filled_quantity * benchmark_price) * 10000",
                "sign": "positive is adverse; negative is price improvement",
                "missed_opportunity": "not all primary children filled or residual outside approved bound",
            },
        }

    @classmethod
    def write_run_and_daily(
        cls, *, oms: Any, events: Iterable[MarketEvent], output_dir: Path,
        correlation_id: str, source_path: str, source_sha256: str,
    ) -> tuple[dict[str, Any], list[Path]]:
        event_rows = list(events)
        run_report = cls.build(
            oms=oms, events=event_rows, correlation_id=correlation_id,
            source_path=source_path, source_sha256=source_sha256,
        )
        cls._write(run_report, output_dir / "tca_report.json")
        plans_by_date: dict[str, list[ParentPlan]] = defaultdict(list)
        for plan in oms.plans.values():
            plans_by_date[_utc_date(plan.opportunity.observed_ts_ns)].append(plan)
        if not plans_by_date and event_rows:
            plans_by_date[_utc_date(event_rows[-1].receive_ts_ns)] = []
        daily_paths = []
        for date_utc, daily_plans in sorted(plans_by_date.items()):
            source_ids = {
                event_id for plan in daily_plans
                for event_id in plan.opportunity.source_event_ids
            }
            daily_events = [event for event in event_rows if event.event_id in source_ids]
            report = cls.build(
                oms=oms, events=daily_events, correlation_id=correlation_id,
                source_path=source_path, source_sha256=source_sha256,
                scope="DAILY_PAPER", period_date=date_utc, plans=daily_plans,
            )
            path = output_dir / "tca_daily" / f"{date_utc}.json"
            cls._write(report, path)
            daily_paths.append(path)
        return run_report, daily_paths
