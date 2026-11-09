"""S2 aggregate (owner 9 Nov), a run wrapper (no logic beyond counting): per cell and per day, from the S2 run folders (summary.json, session_manifest.json, session_orders.jsonl, session_signals.jsonl):
signals (trades attempted = opportunities), orders, fills, hedge orders by final state, hedge chains opened, cap breaches at dispatch, equity at the book mids (USDT, EXPLORATORY), final positions (a nonzero net = an unhedged leg left open).
Cells with fewer than MIN_TRADES pooled trades (signals over the days run) are FLAGGED TOO_FEW_TO_COMPARE: with about one trade a day no ranking of the caps is supported.
usage: aggregate_s2.py RUNS_DIR OUT_JSON"""
import json, sys
from collections import Counter
from pathlib import Path

RUNS, OUT = Path(sys.argv[1]), Path(sys.argv[2])
MIN_TRADES = 30
PROFILE = "perp_perp_maker_taker_v1_6"
rows, cells = [], {}
for cell_dir in sorted(RUNS.iterdir()):
    if not (cell_dir / "base").exists():
        continue
    for day_dir in sorted((cell_dir / "base" / PROFILE).iterdir()):
        if not (day_dir / "summary.json").exists():
            continue
        s, m = json.loads((day_dir / "summary.json").read_text()), json.loads((day_dir / "session_manifest.json").read_text())
        orders = [json.loads(l) for l in (day_dir / "session_orders.jsonl").read_text().splitlines() if l.strip()]
        sig = sum(1 for l in (day_dir / "session_signals.jsonl").read_text().splitlines() if l.strip())
        by_state = Counter((o["venue"], o["state"]) for o in orders)
        ev = m.get("ledger_event_counts", {})
        pos = {k: float(v) for k, v in m["positions"].items()}
        r = {"cell": cell_dir.name, "day": day_dir.name, "signals": s["signals"], "orders": s["orders"], "fills": s["fills"], "equity_at_book_mids": float(m["equity_at_book_mids"]), "net_position_base": round(sum(pos.values()), 9),
             "hedge_orders_by_state": {f"{v}:{st}": n for (v, st), n in sorted(by_state.items()) if v == "BINANCE"}, "hedge_chains_opened": ev.get("HEDGE_CHAIN_OPENED", 0), "cap_breach_at_dispatch": ev.get("HEDGE_CAP_BREACH_AT_DISPATCH", 0),
             "hedge_submits": ev.get("HEDGE_SUBMIT", 0), "positions": pos}
        rows.append(r)
        c = cells.setdefault(cell_dir.name, {"days": 0, "signals": 0, "fills": 0, "equity_at_book_mids": 0.0, "hedge_submits": 0, "cap_breach_at_dispatch": 0, "days_with_open_leg": 0})
        c["days"] += 1; c["signals"] += r["signals"]; c["fills"] += r["fills"]; c["equity_at_book_mids"] += r["equity_at_book_mids"]; c["hedge_submits"] += r["hedge_submits"]
        c["cap_breach_at_dispatch"] += r["cap_breach_at_dispatch"]; c["days_with_open_leg"] += 1 if abs(r["net_position_base"]) > 1e-9 else 0
for c in cells.values():
    c["flag"] = "TOO_FEW_TO_COMPARE" if c["signals"] < MIN_TRADES else "ok"
OUT.write_text(json.dumps({"label": "EXPLORATORY: S2 hedge cap sweep, v1_6, 2-hour March slices, SELECT days, size 1, Singapore latency, fees from the profile (Bybit VIP 5 + Binance VIP 9, PUBLIC_SCHEDULE_UNVERIFIED), no funding", "min_trades_to_compare": MIN_TRADES, "cells": cells, "rows": rows}, indent=1))
print(f"{'cell':8s} {'days':>4s} {'signals':>7s} {'fills':>5s} {'hedge_sub':>9s} {'breach':>6s} {'open_leg_days':>13s} {'equity@mid':>11s}  flag")
for k, c in cells.items():
    print(f"{k:8s} {c['days']:4d} {c['signals']:7d} {c['fills']:5d} {c['hedge_submits']:9d} {c['cap_breach_at_dispatch']:6d} {c['days_with_open_leg']:13d} {c['equity_at_book_mids']:11.4f}  {c['flag']}")
