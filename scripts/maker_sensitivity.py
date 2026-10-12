"""Maker-simulation sensitivity to feed staleness and queue position.

Replays recorded live sessions through ``maker_sim`` for every combination of

* ``feed_lag_ms``          market-data staleness assumed between the venue
                           state at order arrival and the book we observe;
* ``queue_position_frac``  share of the visible level assumed ahead of a new
                           order (1.0 = back of the queue),

and reports, per venue: orders, fills, fill rate, trade-through share, queue
ahead at arrival, markouts (adverse selection), fees and PnL.

Every run is deterministic (seeded latency, one event clock), so differences
between rows are caused by the swept assumption alone.

    python scripts/maker_sensitivity.py --root runs/paper_watch/live --top 6
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from crypto_stat_arb.config import load_config  # noqa: E402
from crypto_stat_arb.maker_sim import (  # noqa: E402
    load_recorded_stream,
    maker_simulator_from_config,
    replay_stream,
)

_STREAMS: dict[str, tuple] = {}


def _cycle_size(cycle: Path) -> int:
    return sum(
        (part / name).stat().st_size
        for part in cycle.glob("part-*") for name in ("canonical.jsonl", "trades.jsonl")
        if (part / name).exists()
    )


def _stream(config: Path, cycle: Path):
    key = str(cycle)
    if key not in _STREAMS:
        _STREAMS.clear()  # one cycle in memory per worker
        cfg, _ = load_config(config)
        parts = sorted(cycle.glob("part-*"))
        _STREAMS[key] = (cfg, *load_recorded_stream(
            cfg, [part / "canonical.jsonl" for part in parts],
            [part / "trades.jsonl" for part in parts],
        ))
    return _STREAMS[key]


def run_case(config: Path, cycle: Path, lag_ms: float, frac: float, engine: str) -> list[dict]:
    cfg, stream, ordering = _stream(config, cycle)
    settings = dict(cfg.strategies.get("maker_simulation") or {})
    settings.update(enabled=True, feed_lag_ms=lag_ms, queue_position_frac=frac)
    cfg.strategies["maker_simulation"] = settings
    simulator = maker_simulator_from_config(cfg, engine=engine)
    started = time.time()
    counts = replay_stream(cfg, simulator, stream)
    report = simulator.report()
    rows = []
    for venue, row in sorted(report["venues"].items()):
        markouts = row["markout_bps"]
        rows.append({
            "cycle": cycle.name, "venue": venue, "feed_lag_ms": lag_ms,
            "queue_position_frac": frac, "engine": report["engine"], "ordering": ordering,
            "orders_sent": row["quotes"], "orders_kept": row["quotes_kept"],
            "orders_rested": row["orders_rested"], "fills": row["fills"],
            "fills_through": row["fills_trade_through"],
            "orders_filled": row["orders_filled"],
            "quote_size": report["quote_size"],
            "fill_qty": row["buy_quantity"] + row["sell_quantity"],
            "post_only_rejects": row["post_only_rejects"],
            "fills_while_cancel_pending": row["fills_while_cancel_pending"],
            "mean_queue_ahead": row["mean_queue_ahead_at_arrival"],
            "markout_1s_bps": (markouts.get("1000ms") or {}).get("mean"),
            "markout_1s_n": (markouts.get("1000ms") or {}).get("count", 0),
            "markout_5s_bps": (markouts.get("5000ms") or {}).get("mean"),
            "markout_5s_n": (markouts.get("5000ms") or {}).get("count", 0),
            "fees": row["fees"], "pnl": row["mark_to_market_pnl_quote"],
            "inventory": row["inventory"],
            "inventory_min": row["inventory_range"][0], "inventory_max": row["inventory_range"][1],
            "sanity_violations": row["sanity_violation_count"],
            "book_events": counts["book_events"], "trades": counts["trades"],
            "runtime_s": round(time.time() - started, 2),
        })
    return rows


def _weighted(rows, value, weight):
    total = sum(row[weight] for row in rows if row[value] is not None)
    if not total:
        return None
    return sum(row[value] * row[weight] for row in rows if row[value] is not None) / total


def aggregate(rows: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        groups.setdefault((row["venue"], row["queue_position_frac"], row["feed_lag_ms"]), []).append(row)
    table = []
    for (venue, frac, lag), items in sorted(groups.items()):
        orders = sum(r["orders_sent"] for r in items)
        fills = sum(r["fills"] for r in items)
        fill_qty = sum(r["fill_qty"] for r in items)
        offered = sum(r["orders_sent"] * float(r.get("quote_size") or 0.001) for r in items)
        table.append({
            "venue": venue, "queue_position_frac": frac, "feed_lag_ms": lag,
            "cycles": len(items), "orders_sent": orders, "fills": fills,
            "fill_events": fills,
            "fill_qty": fill_qty,
            # Quantity-based: partial fills make event counts misleading.
            "fill_rate": fill_qty / offered if offered else 0.0,
            "orders_filled_rate": sum(r["orders_filled"] for r in items) / orders if orders else 0.0,
            "through_share": sum(r["fills_through"] for r in items) / fills if fills else 0.0,
            "mean_queue_ahead": _weighted(items, "mean_queue_ahead", "orders_rested"),
            "markout_1s_bps": _weighted(items, "markout_1s_bps", "markout_1s_n"),
            "markout_5s_bps": _weighted(items, "markout_5s_bps", "markout_5s_n"),
            "post_only_rejects": sum(r["post_only_rejects"] for r in items),
            "fills_while_cancel_pending": sum(r["fills_while_cancel_pending"] for r in items),
            "pnl": sum(r["pnl"] or 0.0 for r in items),
            "pnl_per_fill": sum(r["pnl"] or 0.0 for r in items) / fills if fills else None,
            "fees": sum(r["fees"] for r in items),
            "max_abs_inventory_dev": max(
                max(abs(r["inventory_max"] - 0.005), abs(r["inventory_min"] - 0.005)) for r in items
            ),
            "sanity_violations": sum(r["sanity_violations"] for r in items),
        })
    return table


def _fmt(value, digits=2):
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render(table: list[dict], meta: dict) -> str:
    lines = [
        "# Maker simulation sensitivity",
        "",
        f"Generated {meta['generated']} | engine `{meta['engine']}` | "
        f"{len(meta['cycles'])} cycles | {meta['runs']} runs | ordering `{meta['ordering']}`",
        "",
        "Cycles: " + ", ".join(f"`{c}`" for c in meta["cycles"]),
        "",
        "Markouts are signed mid moves after each fill (positive = favourable, bps).",
        "Qty fill rate = filled quantity / (orders sent x quote size); fully filled = orders filled",
        "completely / orders sent. Through share = fill events caused by a print beyond our price.",
        "PnL is mark-to-market including fees and inventory drift.",
        "",
    ]
    for venue in sorted({row["venue"] for row in table}):
        for frac in sorted({row["queue_position_frac"] for row in table}, reverse=True):
            rows = [r for r in table if r["venue"] == venue and r["queue_position_frac"] == frac]
            base = next((r for r in rows if r["feed_lag_ms"] == 0), rows[0])
            lines += [
                f"## {venue}, queue_position_frac = {frac}",
                "",
                "| feed lag ms | orders | filled qty | qty fill rate | Δ qty fill rate | fully filled "
                "| through % | queue ahead | markout 1s | markout 5s | PnL USDT | Δ PnL | fees | PO rejects |",
                "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
            for r in rows:
                d_rate = (r["fill_rate"] / base["fill_rate"] - 1) * 100 if base["fill_rate"] else None
                lines.append(
                    f"| {_fmt(r['feed_lag_ms'], 0)} | {r['orders_sent']} | {_fmt(r['fill_qty'], 4)} "
                    f"| {_fmt(r['fill_rate'] * 100, 1)}% | {_fmt(d_rate, 1)}% "
                    f"| {_fmt(r['orders_filled_rate'] * 100, 1)}% "
                    f"| {_fmt(r['through_share'] * 100, 1)}% | {_fmt(r['mean_queue_ahead'], 3)} "
                    f"| {_fmt(r['markout_1s_bps'])} | {_fmt(r['markout_5s_bps'])} "
                    f"| {_fmt(r['pnl'])} | {_fmt(r['pnl'] - base['pnl'])} "
                    f"| {_fmt(r['fees'])} | {r['post_only_rejects']} |"
                )
            lines.append("")
    return "\n".join(lines)


_INT_FIELDS = {"orders_sent", "orders_kept", "orders_rested", "fills", "fills_through", "orders_filled",
               "post_only_rejects", "fills_while_cancel_pending", "markout_1s_n", "markout_5s_n",
               "sanity_violations", "book_events", "trades"}
_TEXT_FIELDS = {"cycle", "venue", "engine", "ordering"}


def _rerender(csv_path: Path) -> None:
    rows = []
    with csv_path.open(encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            row: dict = {}
            for key, value in raw.items():
                if key in _TEXT_FIELDS:
                    row[key] = value
                elif value in ("", "None"):
                    row[key] = None
                else:
                    row[key] = int(float(value)) if key in _INT_FIELDS else float(value)
            rows.append(row)
    output = csv_path.parent
    previous = json.loads((output / "summary.json").read_text())["meta"] if (output / "summary.json").exists() else {}
    meta = {**previous, "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "engine": rows[0]["engine"], "cycles": sorted({row["cycle"] for row in rows}),
            "runs": len(rows) // len({row["venue"] for row in rows}),
            "ordering": ",".join(sorted({row["ordering"] for row in rows}))}
    table = aggregate(rows)
    (output / "summary.json").write_text(json.dumps({"meta": meta, "table": table}, indent=2), encoding="utf-8")
    (output / "SENSITIVITY.md").write_text(render(table, meta), encoding="utf-8")
    print(output / "SENSITIVITY.md")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "paper_watch.json")
    parser.add_argument("--root", type=Path, default=ROOT / "runs" / "paper_watch" / "live")
    parser.add_argument("--cycles", nargs="*", default=None, help="cycle directory names (default: --top largest)")
    parser.add_argument("--top", type=int, default=6)
    parser.add_argument("--lags-ms", type=float, nargs="+", default=[0, 5, 10, 20, 35, 50, 75])
    parser.add_argument("--fracs", type=float, nargs="+", default=[1.0, 0.7])
    parser.add_argument("--engine", default="cpp", choices=("cpp", "python", "auto"))
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--from-csv", type=Path, default=None,
                        help="re-aggregate an existing results.csv instead of replaying")
    options = parser.parse_args()

    if options.from_csv is not None:
        _rerender(options.from_csv)
        return

    if options.cycles:
        cycles = [options.root / name for name in options.cycles]
    else:
        cycles = sorted(options.root.glob("cycle-*"), key=_cycle_size, reverse=True)[: options.top]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = options.output_dir or ROOT / "runs" / "maker_sensitivity" / stamp
    output.mkdir(parents=True, exist_ok=True)

    cases = [
        (options.config, cycle, lag, frac, options.engine)
        for cycle in cycles for frac in options.fracs for lag in options.lags_ms
    ]
    print(f"{len(cases)} runs over {len(cycles)} cycles with {options.workers} workers -> {output}", flush=True)
    rows: list[dict] = []
    started = time.time()
    # Cases are grouped by cycle so each worker reuses its loaded stream.
    with ProcessPoolExecutor(max_workers=options.workers) as pool:
        for result in pool.map(run_case, *zip(*cases), chunksize=len(options.fracs) * len(options.lags_ms)):
            rows.extend(result)
            print(f"  {result[0]['cycle']} lag={result[0]['feed_lag_ms']} frac={result[0]['queue_position_frac']} "
                  f"({len(rows) // 2}/{len(cases)}, {time.time() - started:.0f}s)", flush=True)

    with (output / "results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    table = aggregate(rows)
    meta = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "engine": rows[0]["engine"], "cycles": [cycle.name for cycle in cycles],
        "runs": len(cases), "ordering": ",".join(sorted({row["ordering"] for row in rows})),
        "lags_ms": options.lags_ms, "fracs": options.fracs, "config": str(options.config),
    }
    (output / "summary.json").write_text(json.dumps({"meta": meta, "table": table}, indent=2), encoding="utf-8")
    (output / "SENSITIVITY.md").write_text(render(table, meta), encoding="utf-8")
    print(f"done in {time.time() - started:.0f}s: {output / 'SENSITIVITY.md'}")


if __name__ == "__main__":
    main()
