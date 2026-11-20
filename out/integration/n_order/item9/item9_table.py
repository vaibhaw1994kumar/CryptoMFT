"""Item 9 (owner 20 Nov, REPORT ONLY): before / after table of margin.missing_price_action pause (the profile path's default) against breach (MarginConfig's default) on the same 29 Oct replay, one profile
per group of identical margin settings. Writes ITEM9.md and item9.json next to this file."""
import json
from pathlib import Path

ROOT = Path("/Users/vaibhav/Documents/Newproject/out/integration")
H = ROOT / "n_order" / "item9"
RUNS = [("v1_5..v1_9 (v1_9, size 1, gross 5)", ROOT / "phase2/g3_p2", H / "v19_breach"),
        ("v1_10..v1_16 (v1_15, size 1, hedge min 5, regime file)", ROOT / "phase1/g4_v115", H / "v115_breach"),
        ("v1_17 (same arguments)", H / "v117_pause", H / "v117_breach")]


def figures(d: Path) -> dict:
    if not (d / "session_limits_and_stops.json").exists():
        return {"missing": str(d)}
    st = json.loads((d / "session_limits_and_stops.json").read_text())
    c, sq = st["ledger_event_type_counts"], st["stop_quoting"]
    fills = [json.loads(x) for x in (d / "session_fills.jsonl").read_text().splitlines() if x.strip()]
    orders = sum(1 for x in (d / "session_orders.jsonl").read_text().splitlines() if x.strip())
    pick = lambda *ks: {k: c.get(k, 0) for k in ks}
    return {"margin_events": pick("MARGIN_STATE_TRANSITION", "MARGIN_PRICE_PAUSE", "MARGIN_PRICE_RESUME", "MARGIN_BUFFER_ALERT"),
            "margin_state_transitions": c.get("MARGIN_STATE_TRANSITION", 0),
            "quotes_blocked_margin_price": sq.get("signals_blocked_by_margin_price"), "session_limit_blocks": (sq.get("session_limits") or {}).get("stop_quoting_reasons"),
            "kills": pick("KILL_SWITCH_ACTIVATED", "LIQUIDATION", "LIQUIDATION_EXECUTED", "MARGIN_LIQUIDATION", "FORCED_FLATTEN"),
            "liquidation_like_events": {k: v for k, v in c.items() if "LIQUID" in k or "KILL" in k or "FLATTEN" in k},
            "fills": len(fills), "orders": orders}


out = {}
lines = ["# Item 9: margin.missing_price_action, pause against breach (REPORT ONLY, no profile change)", "",
         "| Group (run) | Action | Margin state transitions | Price pause / resume | Buffer alerts | Quotes blocked (missing price) | Kills / liquidations | Fills | Orders |", "|---|---|---|---|---|---|---|---|---|"]
for label, pause, breach in RUNS:
    for action, d in (("pause", pause), ("breach", breach)):
        f = figures(d)
        out[f"{label} | {action}"] = f
        if "missing" in f:
            lines.append(f"| {label} | {action} | NOT RUN | | | | | | |")
            continue
        m = f["margin_events"]
        lines.append(f"| {label} | {action} | {m['MARGIN_STATE_TRANSITION']} | {m['MARGIN_PRICE_PAUSE']} / {m['MARGIN_PRICE_RESUME']} | {m['MARGIN_BUFFER_ALERT']} | {f['quotes_blocked_margin_price']} | "
                     f"{f['liquidation_like_events'] or 0} | {f['fills']} | {f['orders']} |")
(H / "item9.json").write_text(json.dumps(out, indent=1))
(H / "ITEM9.md").write_text("\n".join(lines) + "\n")
print("\n".join(lines))
