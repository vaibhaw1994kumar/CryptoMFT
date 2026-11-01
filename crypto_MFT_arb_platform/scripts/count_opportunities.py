"""Count taker/taker opportunities on one real day with the contract's edge function (edge.py) and compare with a backtest's
ledger (docs/strategy_contract.md, acceptance item "opportunity count").

    python3.12 scripts/count_opportunities.py --pair SOLUSDT --day 2022-01-13 [--size 1.0] [--run-dir out/.../SOLUSDT]

Replays the day's events through two OrderBooks (same loader and book as the backtester), and after every event that moved a
touch evaluates taker_taker_edge_bps in BOTH directions for `size` with the perp fee presets. An "opportunity" is an evaluation
with net edge >= min_net_edge + safety buffer; a "distinct opportunity" is a run of consecutive opportunity evaluations (the
same inversion persisting across events counts once). This is a count of what the edge function sees in the data; it is not a
P&L and it ignores latency, queue, fill probability and inventory limits, which is why it differs from the ledger's trades.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

from new_backtest import edge  # noqa: E402
from new_backtest.config import FEE_PRESETS  # noqa: E402
from new_backtest.event_loader import BOOK, DELTA, TRADE, delta_fields, iter_venue_events, snapshot_levels, trade_fields  # noqa: E402
from new_backtest.order_book import OrderBook  # noqa: E402


def count_day(data_root: str, pair: str, day: str, size: float = 1.0, min_net_edge_bps: float = 1.0, safety_buffer_bps: float = 0.5,
              bybit_fee: str = "bybit_perp_mm", binance_fee: str = "binance_perp_vip0", max_book_age_s: float = 0.5) -> dict:
    venues = ("bybit", "binance")
    fees = {"bybit": FEE_PRESETS[bybit_fee].taker_bps, "binance": FEE_PRESETS[binance_fee].taker_bps}
    books = {v: OrderBook(v, pair) for v in venues}          # same book class and default (hybrid) delta semantics as the backtester
    streams = []
    for v in venues:
        for ev in iter_venue_events(data_root, v, pair, day, day):
            streams.append((ev.exchange_ts, v, ev))
    streams.sort(key=lambda x: x[0])
    stats = Counter()
    in_opp = {d: False for d in ((venues[0], venues[1]), (venues[1], venues[0]))}
    best = {d: float("-inf") for d in in_opp}
    edges = []
    for ts, v, ev in streams:
        b = books[v]
        if ev.kind == BOOK:
            bids, asks = snapshot_levels(ev.payload)
            b.apply_snapshot(bids, asks, ts)
        elif ev.kind == DELTA:
            side, price, vol, et = delta_fields(ev.payload)
            b.apply_delta(side, price, vol, et, ts)
        elif ev.kind == TRADE:
            price, vol, sign = trade_fields(ev.payload)
            b.on_trade(price, vol, sign, ts)
        stats["events"] += 1
        if not all(books[x].ready for x in venues) or any(books[x].is_stale(ts, max_book_age_s) for x in venues):
            continue
        stats["evaluations"] += 1
        for buy, sell in in_opp:
            er = edge.taker_taker_edge_bps(books[buy], books[sell], size, fees[buy], fees[sell])
            if er.status != edge.OK:
                stats[f"insufficient_depth_{buy}->{sell}"] += 1
                in_opp[(buy, sell)] = False
                continue
            net = er.net_bps - safety_buffer_bps
            edges.append(net)
            best[(buy, sell)] = max(best[(buy, sell)], net)
            if net >= min_net_edge_bps:
                stats[f"opportunity_evaluations_{buy}->{sell}"] += 1
                if not in_opp[(buy, sell)]:
                    stats[f"distinct_opportunities_{buy}->{sell}"] += 1
                    in_opp[(buy, sell)] = True
            else:
                in_opp[(buy, sell)] = False
    edges.sort()
    q = lambda p: edges[min(len(edges) - 1, int(p * len(edges)))] if edges else None
    return {"pair": pair, "day": day, "size": size, "fees_taker_bps": fees, "events": stats["events"], "evaluations": stats["evaluations"],
            "opportunities": sum(v for k, v in stats.items() if k.startswith("distinct_opportunities")),
            "opportunity_evaluations": sum(v for k, v in stats.items() if k.startswith("opportunity_evaluations")),
            "by_direction": {k: v for k, v in stats.items() if "->" in k}, "best_net_edge_bps": {f"{a}->{b}": x for (a, b), x in best.items()},
            "net_edge_bps_distribution": {"n": len(edges), "p10": q(0.1), "p50": q(0.5), "p90": q(0.9), "p99": q(0.99), "max": edges[-1] if edges else None}}


def ledger_trades(run_dir: str) -> dict:
    """Realised trades from a backtest output directory (trades.jsonl): count of signals with at least one fill, fills, hedges."""
    p = Path(run_dir) / "trades.jsonl"
    if not p.exists():
        return {"trades_file": None}
    rows = [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
    sigs = {r.get("signal_id") for r in rows}
    return {"trades_file": str(p), "fills": len(rows), "signals_with_fills": len(sigs), "taker_fills": sum(1 for r in rows if r.get("taker")),
            "maker_fills": sum(1 for r in rows if not r.get("taker"))}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-root", default="/Users/vaibhav/Documents/Newproject/data")
    ap.add_argument("--pair", default="SOLUSDT")
    ap.add_argument("--day", required=True)
    ap.add_argument("--size", type=float, default=1.0)
    ap.add_argument("--run-dir", default=None, help="backtest output dir with trades.jsonl, for the ledger side of the comparison")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    out = count_day(a.data_root, a.pair, a.day, a.size)
    if a.run_dir:
        out["ledger"] = ledger_trades(a.run_dir)
    text = json.dumps(out, indent=1)
    print(text)
    if a.out:
        Path(a.out).write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
