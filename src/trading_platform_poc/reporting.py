from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from trading_platform_poc.engine import TradingEngine


def write_report(
    output_directory: Path,
    run_id: UUID,
    mode: str,
    engine: TradingEngine,
) -> dict[str, Any]:
    output_directory.mkdir(parents=True, exist_ok=False)

    orders = [
        {
            "order_id": str(order.order_id),
            "intent_id": str(order.intent_id),
            "client_order_id": order.client_order_id,
            "venue": order.venue or "",
            "symbol": order.symbol,
            "side": order.side.value,
            "order_type": order.order_type.value,
            "quantity": str(order.quantity),
            "price": "" if order.price is None else str(order.price),
            "state": order.state.value,
            "created_timestamp_ns": order.created_timestamp_ns,
        }
        for order in engine.oms.orders
    ]
    updates = [
        {
            "order_id": str(update.order_id),
            "state": update.state.value,
            "cumulative_filled_quantity": str(
                update.cumulative_filled_quantity
            ),
            "average_fill_price": (
                ""
                if update.average_fill_price is None
                else str(update.average_fill_price)
            ),
            "reason": update.reason or "",
            "update_timestamp_ns": update.update_timestamp_ns,
        }
        for update in engine.oms.updates
    ]
    fills = [
        {
            "fill_id": str(fill.fill_id),
            "order_id": str(fill.order_id),
            "symbol": fill.symbol,
            "side": fill.side.value,
            "price": str(fill.price),
            "quantity": str(fill.quantity),
            "fee": str(fill.fee),
            "fee_asset": fill.fee_asset,
            "fill_timestamp_ns": fill.fill_timestamp_ns,
        }
        for fill in engine.portfolio.fills
    ]
    submitted_at = {
        update.order_id: update.update_timestamp_ns
        for update in engine.oms.updates
        if update.state.value == "submitted"
    }
    signal_to_submit_latencies = [
        submitted_at[order.order_id] - order.created_timestamp_ns
        for order in engine.oms.orders
        if order.order_id in submitted_at
    ]
    submit_to_fill_latencies = [
        fill.fill_timestamp_ns - submitted_at[fill.order_id]
        for fill in engine.portfolio.fills
        if fill.order_id in submitted_at
    ]
    rejected_count = sum(
        order.state.value in {"risk_rejected", "rejected", "expired"}
        for order in engine.oms.orders
    )
    reconciliation_required_count = sum(
        order.state.value == "reconciliation_required"
        for order in engine.oms.orders
    )
    orders_by_venue: dict[str, int] = {}
    for order in engine.oms.orders:
        venue = order.venue or "unassigned"
        orders_by_venue[venue] = orders_by_venue.get(venue, 0) + 1

    _write_csv(output_directory / "orders.csv", orders)
    _write_csv(output_directory / "order_updates.csv", updates)
    _write_csv(output_directory / "fills.csv", fills)
    _write_csv(output_directory / "opportunities.csv", engine.opportunities)
    anomaly_rows = [
        anomaly.to_dict() for anomaly in engine.data_quality.anomalies
    ]
    with (output_directory / "anomalies.jsonl").open(
        "w", encoding="utf-8", newline="\n"
    ) as anomaly_file:
        for anomaly in anomaly_rows:
            anomaly_file.write(
                json.dumps(
                    anomaly,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )

    result_data = {
        "book_checksum": (
            engine.book.checksum() if engine.book.initialized else None
        ),
        "orders": orders,
        "updates": updates,
        "fills": fills,
        "opportunities": engine.opportunities,
        "portfolio": engine.portfolio.snapshot(engine.book.mid_price),
        "data_quality": engine.data_quality.summary(),
        "execution_metrics": {
            "fill_rate": (
                len({fill.order_id for fill in engine.portfolio.fills})
                / len(orders)
                if orders
                else 0
            ),
            "reject_rate": rejected_count / len(orders) if orders else 0,
            "average_signal_to_submit_latency_ns": (
                sum(signal_to_submit_latencies)
                / len(signal_to_submit_latencies)
                if signal_to_submit_latencies
                else None
            ),
            "average_submit_to_fill_latency_ns": (
                sum(submit_to_fill_latencies)
                / len(submit_to_fill_latencies)
                if submit_to_fill_latencies
                else None
            ),
        },
    }
    result_hash = hashlib.sha256(
        json.dumps(
            result_data,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    summary = {
        "run_id": str(run_id),
        "mode": mode,
        "event_count": engine.event_count,
        "order_count": len(orders),
        "fill_count": len(fills),
        "opportunity_count": len(engine.opportunities),
        "open_order_count": len(engine.oms.open_orders()),
        "reconciliation_required_order_count": (
            reconciliation_required_count
        ),
        "orders_by_venue": dict(sorted(orders_by_venue.items())),
        "book_checksum": result_data["book_checksum"],
        "result_hash": result_hash,
        "portfolio": result_data["portfolio"],
        "data_quality": result_data["data_quality"],
        "execution_metrics": result_data["execution_metrics"],
    }
    config_data = engine.config.model_dump(mode="json")
    config_hash = hashlib.sha256(
        json.dumps(
            config_data,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    (output_directory / "pnl_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_directory / "run_manifest.json").write_text(
        json.dumps(
            {
                "run_id": str(run_id),
                "mode": mode,
                "config_hash": config_hash,
                "result_hash": result_hash,
                "configuration": config_data,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return summary


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
