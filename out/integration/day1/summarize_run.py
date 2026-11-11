"""Day-1 rerun summary (a run wrapper, no logic): reads one runs/<dir> and prints the numbers the C70/C73 report tables use. READ ONLY.
usage: summarize_run.py RUN_DIR"""
import json, sys, time
from pathlib import Path
d = Path(sys.argv[1])
m = json.load(open(d / "session_manifest.json"))
rows = lambda n: [json.loads(l) for l in open(d / n)] if (d / n).exists() else []
utc = lambda ns: time.strftime("%H:%M:%S", time.gmtime(ns / 1e9))
sig, orders, fills, pe = rows("session_signals.jsonl"), rows("session_orders.jsonl"), rows("session_fills.jsonl"), rows("session_pair_economics.jsonl")
print("profile", m["profile"], "| wall_s", m["run"]["wall_s"], "| events", m["events"])
print("signals", len(sig), "orders", len(orders), "fills", len(fills))
for s in sig:
    print("  signal", {k: s[k] for k in s if k in ("ts_ns", "buy_venue", "sell_venue", "buy_price", "sell_price", "quantity", "estimated_net_edge_bps", "gross_edge_bps", "observed_ts_ns")})
for f in fills:
    print("  fill", utc(f.get("ts_ns", 0)), f.get("venue"), f.get("side"), f.get("fill_quantity"), "@", f.get("fill_price"), "fee", f.get("fee"), f.get("liquidity", ""))
print("positions", m["positions"], "| cash", m["cash_by_venue"], "| equity_at_book_mids", m["equity_at_book_mids"])
print("residual_at_end", m["residual_at_end"])
c = m["ledger_event_counts"]
print("ledger", {k: c[k] for k in c if any(t in k for t in ("KILL", "MARGIN", "HEDGE", "UNWIND", "BREAKER", "CANCEL", "LIFETIME"))})
print("strategy_counters", json.dumps(m["strategy_counters"], default=str)[:900])
print("pair_economics", json.dumps(json.load(open(d / "session_pair_economics.json")).get("summary"), default=str)[:900])
print("markout_by_venue_role", json.dumps(m["markout_by_venue_role"], default=str)[:900])
print("gate_events", [(g.get("event_type"), g.get("reason")) for g in m["gate_events"]])
print("suspended_s_by_day", m["manifest"].get("invalid_book_policy", {}).get("suspended_seconds_total_by_day"))
