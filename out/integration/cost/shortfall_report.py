"""Execution cost model, step A (a run wrapper; the logic is crypto_stat_arb.metrics.shortfall_split / opportunity_cost): the Perold split of the arb fills of EXISTING runs on the 2026-10-29 capture.
The hedge venue's touch at each maker fill and each hedge arrival is read from the capture itself (feed only: the replay's view book of the hedge venue, tape repair on, as the replay builds it). EXPLORATORY.
usage: shortfall_report.py OUT_DIR RUN_DIR [RUN_DIR ...]"""
import bisect
import json
import sys
from pathlib import Path

SRC = "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src"
sys.path.insert(0, SRC)
from crypto_stat_arb.derivatives import DerivativeTicker
from crypto_stat_arb.metrics import distribution_stats, opportunity_cost, shortfall_split
from crypto_stat_arb.models import EventType, TradeEvent
from new_backtest.profiles import apply_profile, load_profile
from new_backtest.session_replay import SessionReplay, dated_ticks, iter_recorded_events

SESSION = Path("/Users/vaibhav/Documents/Newproject/data/live_sessions/solusdt/2026-10-29/SOLUSDT/e9b81728a3054812a48035a1b3e02d42")
OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
RUNS = [Path(p) for p in sys.argv[2:]]


def jl(path):
    return [json.loads(l) for l in open(path)] if Path(path).exists() else []


def load_run(d):
    rows = jl(d / "session_pair_economics.jsonl")
    orders, fills, sigs = jl(d / "session_orders.jsonl"), jl(d / "session_fills.jsonl"), {s["opportunity_id"]: s for s in jl(d / "session_signals.jsonl")}
    order_ids = {}
    for o in orders:
        order_ids.setdefault(o["intent_id"], []).append(o["order_id"])
    opp_of_order = {f["order_id"]: f["opportunity_id"] for f in fills if f.get("opportunity_id")}
    signal_of = {}
    for iid, oids in order_ids.items():
        for oid in oids:
            opp = opp_of_order.get(oid)
            if opp in sigs:
                signal_of[iid] = sigs[opp]
    filled_opps = {f["opportunity_id"] for f in fills if f.get("opportunity_id")}
    m = json.load(open(d / "session_manifest.json"))
    rows_fee = m["manifest"]["release_identity"]["fee_schedule"]["rows"]
    assumed = float(rows_fee[0]["maker_fee_bps"]) + float(rows_fee[1]["taker_fee_bps"])          # Bybit maker + Binance taker (the schedule's own order)
    return {"rows": rows, "signal_of": signal_of, "signals": list(sigs.values()), "filled": filled_opps, "assumed": assumed}


runs = {d.name: load_run(d) for d in RUNS}
queries = set()
for r in runs.values():
    shortfall_split(r["rows"], r["signal_of"], lambda v, ts, side: queries.add(ts) or 1.0, r["assumed"], reps=1)
qs = sorted(queries)
print(len(qs), "touch times to read", flush=True)

profile = load_profile("perp_perp_maker_taker_v1_9")
cfg = apply_profile(profile, dataset="mar2024", data_root="", pair="SOLUSDT", start_date="2026-10-29", end_date="2026-10-29", output_dir=str(OUT))
replay = SessionReplay(cfg, profile, ticks=dated_ticks("2026-10-29"), recorded=True)
view = replay.views["binance"]
touch = {}
i = 0
for ev in iter_recorded_events(SESSION):
    if isinstance(ev, DerivativeTicker):
        continue
    ts = ev.receive_ts_ns / 1e9
    while i < len(qs) and qs[i] < ts:                               # the book as it stood BEFORE the first event after the query time
        touch[qs[i]] = (view.best_bid, view.best_ask) if view.ready else None
        i += 1
    if i >= len(qs):
        break
    if replay._feed_venue(ev.venue) != "binance":
        continue
    if isinstance(ev, TradeEvent):
        p, q = float(ev.price), float(ev.quantity)
        if p > 0 and q > 0:
            view.on_trade(p, q, 1 if ev.aggressor_side == "BUY" else -1, ts)
    elif ev.event_type == EventType.BOOK_SNAPSHOT:
        b, a = [[float(l.price), float(l.quantity)] for l in ev.bids], [[float(l.price), float(l.quantity)] for l in ev.asks]
        if b and a:
            view.apply_snapshot(b, a, ts)
    elif ev.event_type == EventType.BOOK_DELTA:
        for side, lv in (("bid", ev.bids), ("ask", ev.asks)):
            for l in lv:
                view.apply_delta(side, float(l.price), float(l.quantity), "modify", ts)
        view.end_message()


def touch_at(venue, ts, side):
    t = touch.get(ts)
    return None if t is None else (t[0] if side == "bid" else t[1])


report = {"label": "EXPLORATORY: Perold split of the arb fills of existing runs, 2026-10-29 capture; hedge-venue touch read from the capture (feed view), not from the replay's own truth book", "runs": {}}
pooled = []
for name, r in runs.items():
    res = shortfall_split(r["rows"], r["signal_of"], touch_at, r["assumed"], reps=1000)
    opp = opportunity_cost(r["signals"], r["filled"])
    same = sum(1 for x in res["rows"] if abs(x["hedge_impact_bps"]) < 1e-9)
    report["runs"][name] = {"assumed_fees_bps": r["assumed"], "fills_split": len(res["rows"]), "excluded": res["excluded"], "max_abs_residual_bps": res["max_abs_residual_bps"], "summary": res["summary"],
                            "opportunity": opp, "impact_exactly_zero_share": (same / len(res["rows"])) if res["rows"] else None}
    pooled += [dict(x, run=name) for x in res["rows"]]
parts = ("believed_net_bps", "realised_net_bps", "shortfall_bps", "delay_drift_bps", "hedge_latency_drift_bps", "hedge_impact_bps", "fee_diff_bps", "quote_price_term_bps", "believed_allowances_bps", "residual_bps")
report["pooled"] = {"n": len(pooled), "summary": {p: distribution_stats([x[p] for x in pooled], [int(x["maker_fill_ts"] // 3600) for x in pooled], reps=1000) for p in parts},
                    "max_abs_residual_bps": max((abs(x["residual_bps"]) for x in pooled), default=0.0)}
(OUT / "shortfall_split.json").write_text(json.dumps(report, indent=1))
with open(OUT / "shortfall_rows.jsonl", "w") as fh:
    for x in pooled:
        fh.write(json.dumps(x) + "\n")
lines = ["# Shortfall split (Perold), existing runs", "", report["label"], "", "Mean bps per fully hedged maker fill (bps of the maker fill price); 95% interval from an hourly block bootstrap where there are at least 2 blocks.", "",
         "| run | fills | believed net | realised net | shortfall | delay drift | hedge latency drift | hedge impact | fee diff | quote price | -allowances | residual |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
def mean(s):
    return "-" if s.get("mean") is None else f"{s['mean']:.3f}"
for name, v in list(report["runs"].items()) + [("POOLED", report["pooled"])]:
    s = v["summary"]
    n = v.get("fills_split", v.get("n"))
    lines.append(f"| {name} | {n} | " + " | ".join(mean(s[p]) if p != "believed_allowances_bps" else ("-" if s[p].get("mean") is None else f"{-s[p]['mean']:.3f}") for p in parts) + " |")
lines += ["", "## Opportunity (unfilled quotes)", "", "| run | signals | unfilled | unfilled share | mean believed edge of unfilled (bps) | believed edge of unfilled (quote ccy) |", "|---|---|---|---|---|---|"]
for name, v in report["runs"].items():
    o = v["opportunity"]
    lines.append(f"| {name} | {o['signals']} | {o['unfilled']} | {o['unfilled_share']:.3f} | {o['mean_believed_edge_bps_unfilled']} | {o['believed_edge_quote_ccy_unfilled']:.4f} |")
(OUT / "shortfall_split.md").write_text("\n".join(lines))
print("\n".join(lines))
print("max abs residual", report["pooled"]["max_abs_residual_bps"])
