"""Order 2 comparison (a run wrapper, no logic of the platform): reads the end-to-end run directories and prints the numbers of the v1_9 / v1_10 table. READ ONLY. EXPLORATORY.
usage: compare_e2e.py DIR [DIR ...]"""
import json, sys
from pathlib import Path

def rows(d, n):
    p = d / n
    return [json.loads(l) for l in open(p)] if p.exists() else []

def summarize(d):
    d = Path(d)
    m = json.load(open(d / "session_manifest.json"))
    lim = json.load(open(d / "session_limits_and_stops.json"))
    ev = lim["ledger_event_type_counts"]
    orders, fills, sig = rows(d, "session_orders.jsonl"), rows(d, "session_fills.jsonl"), rows(d, "session_signals.jsonl")
    journal = rows(d, "session_intent_journal.jsonl")
    created = {j["intent_id"]: j["created_ts"] for j in journal if j.get("event") == "intent" and j.get("role") == "primary"}
    makers = [o for o in orders if o.get("intent_id") in created]
    ages = [o["terminal_ts_ns"] / 1e9 - created[o["intent_id"]] for o in makers if o.get("terminal_ts_ns")]
    ts = [s["ts"] for s in sig]
    first, last = (m["run"].get("first_event_ts"), m["run"].get("last_event_ts"))
    span_h = m["run"].get("span_hours")
    w = ev.get("QUOTE_WITHDRAWN", 0)
    out = {
        "run": d.name, "profile": m["profile"], "size_base": m["size_base"],
        "signals": len(sig), "maker_orders": len(makers), "orders_total": len(orders), "fills": len(fills),
        "maker_fills": sum(1 for f in fills if f.get("venue") == "BYBIT"),
        "quote_withdrawn": w, "cancel_before_close": ev.get("WORKING_ORDER_CANCEL_BEFORE_CLOSE", 0),
        "amend_requested": ev.get("AMEND_REQUESTED", 0), "amend_confirmed": ev.get("AMEND_CONFIRMED", 0),
        "quote_age_s": {"n": len(ages), "mean": round(sum(ages) / len(ages), 3) if ages else None, "max": round(max(ages), 3) if ages else None},
        "equity_at_book_mids": m["equity_at_book_mids"], "positions": m["positions"],
        "pair_economics": {k: v.get("mean") for k, v in json.load(open(d / "session_pair_economics.json"))["summary"].items() if k in ("pair_gross_bps", "pair_net_bps")},
        "margin_price_pause": ev.get("MARGIN_PRICE_PAUSE", 0), "hedge_lot_residual": ev.get("HEDGE_LOT_RESIDUAL", 0),
        "dust": lim["stop_quoting"].get("hedge_dust_carry"),
        "reprice_counters": m["strategy_counters"].get("reprice") or m.get("reprice_counters"),
        "no_requote": lim.get("no_requote") or lim["stop_quoting"].get("no_requote"),
    }
    return out

for a in sys.argv[1:]:
    print(json.dumps(summarize(a), default=str, indent=1))
