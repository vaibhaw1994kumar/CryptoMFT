"""Owner 7 Nov, C15 follow-up, report only: does the real strategy show the probe's quote-to-fill drift? One row per signal with a maker fill, from EXISTING strategy runs (no replay):
2022-01-13 v1.1 + message-atomic (phase_b/ledger l0), 2022-01-05 v1.1 (baselines b1), March 2024 v1_2 slices 03-08..03-11 (phase_b/ledger t1). Adverse = negative in every drift column.
ref = the HEDGE venue's touch on the side the hedge trades (bid for a bought maker fill, ask for a sold one), in the strategy's own view book: at the quote decision (the quote is priced from it and is never repriced:
see the code reference in the report), at the maker fill report, and at hedge arrival (the truth book). Drifts are in bps of the decision-time ref. believed gross edge = (expected hedge price - quote) / quote with the run's expected hedge move,
before fees; realised = (hedge VWAP - maker price) / maker price for a bought fill (reverse for a sold one), before fees. Output: strategy_quote_drift.json. EXPLORATORY."""
import glob, json, statistics as st
from pathlib import Path
R = Path("/Users/vaibhav/Documents/Newproject/out/integration")
RUNS = [("jan2022 01-13 v1.1", glob.glob(str(R / "phase_b/ledger/runs/l0/jan/perp_perp_maker_taker_v1_1/*/2022-01-13"))[0]),
        ("jan2022 01-05 v1.1", glob.glob(str(R / "baselines/runs/b1/jan/perp_perp_maker_taker_v1_1/*/2022-01-05"))[0])] + \
       [(f"mar2024 {d[5:]} v1_2", str(R / f"phase_b/ledger/runs/t1/base/perp_perp_maker_taker_v1_2/{d}")) for d in ("2024-03-08", "2024-03-09", "2024-03-10", "2024-03-11")]


def run_rows(label, d):
    s = json.load(open(d + "/summary.json"))
    mv = s.get("expected_hedge_move_bps_used") or {"bid": 0.0, "ask": 0.0}
    obs = [json.loads(l) for l in open(d + "/maker_observations.jsonl")]
    trades = [json.loads(l) for l in open(d + "/trades.jsonl")]
    place = {o["signal_id"]: o for o in obs if o["obs"] == "maker_place"}
    fills = {}
    for o in obs:
        if o["obs"] == "maker_fill":
            fills.setdefault(o["signal_id"], []).append(o)
    harr = {}
    for o in obs:
        if o["obs"] == "hedge_arrival" and o.get("attempt", 0) == 0:
            harr.setdefault(o["signal_id"], o)
    out = []
    for sid, fl in sorted(fills.items()):
        p = place.get(sid)
        if not p:
            continue
        fl.sort(key=lambda x: x["ts"])
        f0 = fl[0]
        buy = p["side"] == "buy"
        mv_venue = p["venue"]
        hv = "binance" if mv_venue == "bybit" else "bybit"
        side = "bid" if buy else "ask"
        ref0 = (p["hedge_touch_at_decision"] or {}).get(side)
        reff = (f0["touch"].get(hv) or {}).get(side)
        ha = harr.get(sid)
        refa = (ha["touch_at_arrival"] or {}).get(side if False else ("bid" if buy else "ask")) if ha else None
        adv = (lambda a, b: (a - b) / ref0 * 1e4 if buy else (b - a) / ref0 * 1e4)       # adverse = negative: a bid falling, an ask rising
        mq = sum(x["qty"] * x["price"] for x in fl) / sum(x["qty"] for x in fl)
        mt = [t for t in trades if t["signal_id"] == sid and not t["taker"]]
        ht = [t for t in trades if t["signal_id"] == sid and t["taker"]]
        hq = sum(t["qty"] for t in ht)
        hvwap = sum(t["qty"] * t["price"] for t in ht) / hq if hq > 0 else None
        mp = p["price"]
        move = mv["bid"] if buy else mv["ask"]
        exp_hedge = ref0 * (1.0 + move / 1e4) if buy else ref0 * (1.0 - move / 1e4)
        believed = ((exp_hedge - mp) / mp if buy else (mp - exp_hedge) / mp) * 1e4
        realised = (((hvwap - mq) / mq) if buy else ((mq - hvwap) / mq)) * 1e4 if hvwap else None
        out.append({"run": label, "signal_id": sid, "maker_venue": mv_venue, "hedge_venue": hv, "side": p["side"], "quote": mp, "size": p["size"], "filled_qty": sum(x["qty"] for x in fl), "fills": len(fl),
                    "decision_ts": p["decision_ts"], "arrival_ts": f0["fill"]["arrival_ts"], "fill_report_ts": f0["ts"], "match_ts": f0["fill"].get("match_ts"),
                    "quote_age_decision_to_fill_report_s": f0["ts"] - p["decision_ts"], "quote_age_arrival_to_match_s": (f0["fill"].get("match_ts") or f0["ts"]) - f0["fill"]["arrival_ts"],
                    "ref_decision": ref0, "ref_fill": reff, "ref_arrival": refa,
                    "drift_quote_to_fill_bps": adv(reff, ref0) if reff and ref0 else None, "drift_fill_to_arrival_bps": ((refa - reff) / ref0 * 1e4 if buy else (reff - refa) / ref0 * 1e4) if refa and reff and ref0 else None,
                    "drift_quote_to_arrival_bps": adv(refa, ref0) if refa and ref0 else None,
                    "expected_hedge_move_bps_used": move, "believed_gross_edge_bps": believed, "hedge_vwap": hvwap, "hedge_qty": hq, "realised_gross_edge_bps": realised,
                    "shortfall_bps": (realised - believed) if realised is not None else None, "cause": f0["fill"]["cause"],
                    "fees_bps_maker_plus_taker": (mt[0]["fee_bps"] + ht[0]["fee_bps"]) if mt and ht else None,
                    "realised_net_after_fees_bps": (realised - (mt[0]["fee_bps"] + ht[0]["fee_bps"])) if realised is not None and mt and ht else None})
    return out, {"run": label, "believed_conditional_net_edge_bps_p50": (s.get("conditional_edge_bps") or {}).get("p50"), "believed_naive_edge_bps_p50": (s.get("naive_edge_bps") or {}).get("p50"),
                 "legging_hedge_move_bps": (s.get("legging") or {}).get("hedge_move_bps"), "signals": s.get("signals"), "fills": s.get("fills"), "expected_hedge_move_bps_used": mv}


def agg(rows, k):
    v = [r[k] for r in rows if r.get(k) is not None]
    return {"n": len(v), "mean": sum(v) / len(v), "median": st.median(v), "min": min(v), "max": max(v)} if v else {"n": 0}


def main():
    rows, runs = [], []
    for label, d in RUNS:
        r, m = run_rows(label, d); rows += r; runs.append(m)
    res = {"label": "strategy quote-to-fill drift; EXPLORATORY; before fees; adverse negative", "runs": runs, "rows": rows}
    groups = {"all": rows, "jan2022": [r for r in rows if r["run"].startswith("jan")], "mar2024_v1_2": [r for r in rows if r["run"].startswith("mar")],
              "maker_bybit_hedge_binance": [r for r in rows if r["maker_venue"] == "bybit"], "maker_binance_hedge_bybit": [r for r in rows if r["maker_venue"] == "binance"]}
    res["aggregate"] = {g: {k: agg(rs, k) for k in ("drift_quote_to_fill_bps", "drift_fill_to_arrival_bps", "drift_quote_to_arrival_bps", "believed_gross_edge_bps", "realised_gross_edge_bps", "shortfall_bps", "realised_net_after_fees_bps",
                                                  "quote_age_decision_to_fill_report_s", "quote_age_arrival_to_match_s")} | {"signals": len(rs)} for g, rs in groups.items()}
    (R / "phase_b/strategy_quote_drift.json").write_text(json.dumps(res, indent=1, default=str))
    f = lambda x: "  -  " if x is None else f"{x:+6.2f}"
    for r in rows:
        print(f"{r['run'][:20]:20s} sig{r['signal_id']:3d} {r['maker_venue'][:3]}/{r['side'][:1]} q2f {f(r['drift_quote_to_fill_bps'])} f2a {f(r['drift_fill_to_arrival_bps'])} q2a {f(r['drift_quote_to_arrival_bps'])} believed {f(r['believed_gross_edge_bps'])} realised {f(r['realised_gross_edge_bps'])} shortfall {f(r['shortfall_bps'])} age {r['quote_age_decision_to_fill_report_s']:6.1f}s {r['cause']}")
    for g, a in res["aggregate"].items():
        print(g, a["signals"], {k: (None if not v.get("n") else (v["n"], round(v["mean"], 2), round(v["median"], 2))) for k, v in a.items() if k != "signals"})
    for m in runs: print(m["run"], m["believed_conditional_net_edge_bps_p50"], m["expected_hedge_move_bps_used"], (m["legging_hedge_move_bps"] or {}).get("bid", {}).get("mean"))


main()
