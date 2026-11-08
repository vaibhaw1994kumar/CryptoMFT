"""Phase D, D5 parity wrapper (a run wrapper, no logic worth reusing): v1_4 on the backtester's loop (cell g1o, order events on) against v1_5 on the session (cell g1s), per day.
Usage: parity_report.py OLD_RUNS NEW_RUNS DAY [DAY ...]    (OLD_RUNS/NEW_RUNS = the ledger_<tag>/runs directories)
Prints signals, orders, fills, hedges and P&L side by side, the per-order times, the session's suspension windows and every difference above the written tolerances. Classification is by hand."""
import json
import sys
from pathlib import Path

sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src")
from new_backtest.session_replay import compare_order_times, compare_parity, order_times_old, TOLERANCES

old_runs, new_runs, days = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3:]
TICKS = {"2022": {"bybit": 0.005, "binance": 0.001}, "2024": {"bybit": 0.001, "binance": 0.001}}
for day in days:
    o = old_runs / "g1o/base/perp_perp_maker_taker_v1_4" / day
    n = new_runs / "g1s/base/perp_perp_maker_taker_v1_5" / day
    if not (o / "summary.json").exists() or not (n / "summary.json").exists():
        print(day, "MISSING", (o / "summary.json").exists(), (n / "summary.json").exists())
        continue
    so, sn = json.loads((o / "summary.json").read_text()), json.loads((n / "summary.json").read_text())
    man = json.loads((n / "session_manifest.json").read_text())
    pol = man["manifest"]["invalid_book_policy"]
    print(f"\n===== {day}  (EXPLORATORY, size {so['size_base']} SOL, fees from the profile) =====")
    print(f"signals       old {so['signals']:>4}   new {sn['signals']:>4}")
    print(f"orders        old {so['orders_sent']:>4}   new {sn['orders']:>4}")
    old_rows = sum(1 for l in (o / "trades.jsonl").read_text().splitlines() if l.strip())
    print(f"fills         old {old_rows:>4} (trade rows; the summary counts maker fills: {so['fills']})   new {sn['fills']:>4} (ledger FILL events)")
    hc = so.get("hedge_counters") or {}
    print(f"hedges        old sent {hc.get('hedges_sent')}, failed {hc.get('hedges_failed')}   new orders on the hedge venue {sum(1 for r in [json.loads(l) for l in (n/'session_orders.jsonl').read_text().splitlines() if l] if r['venue']=='BINANCE')}")
    print(f"P&L           old cash {so['cash_pnl']:.4f} equity {so['ending_equity_common_timestamp']:.4f} fees {so['fees_paid']:.4f} inventory {so['final_inventory']}")
    print(f"              new cash {sum(float(v) for v in man['cash_by_venue'].values()):.4f} equity@mid {man['equity_at_book_mids']} positions {man['positions']}")
    print(f"suspended s   {pol['suspended_seconds_total_by_day']}   resyncs {pol['resyncs_by_source']}   windows {len(pol['suspension_intervals_ns'])}")
    print("truth/timing  ", {k: v for k, v in man["manifest"]["timing"]["truth_books"].items() if k.startswith(("truth_arrivals", "postonly", "truth_book_not"))})
    print("fills by src  ", man["fills_by_source"])
    ev = [json.loads(l) for l in (o / "order_events.jsonl").read_text().splitlines() if l.strip()] if (o / "order_events.jsonl").exists() else []
    old_rows, new_rows = order_times_old(ev), json.loads((n / "order_times.json").read_text())
    print(f"per-order times: old {len(old_rows)} orders, new {len(new_rows)} orders")
    for r in compare_order_times(old_rows, new_rows)[:12]:
        if "what" in r:
            print("   order", r["order"], r["what"])
        else:
            print("   order", r["order"], r["venue"], r["side"], {k: (None if v["diff_s"] is None else round(v["diff_s"], 6)) for k, v in r.items() if isinstance(v, dict)})
    rep = compare_parity(o, n, TICKS[day[:4]], pol["suspension_intervals_ns"])
    print("differences above tolerance:", len(rep["differences"]))
    for d in rep["differences"][:10]:
        print("  ", d)
