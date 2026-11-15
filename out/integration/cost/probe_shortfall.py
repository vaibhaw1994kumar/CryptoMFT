"""Step A follow-up (a run wrapper; the logic is crypto_stat_arb.metrics.shortfall_split): the Perold split of the PROBE fills of the 7 TRAIN days (03-01..07, 2,035 fills) and the SELECT days (03-08..11), per day and pooled,
each part with a block-bootstrap 95% interval (blocks = BURSTS of fills less than 60 s apart; the pooled row also with DAY blocks). HOLDOUT (03-13..15) is not read. EXPLORATORY.
The probe (qv2_probe.py): a 1 SOL Bybit bid every 15 s at the strategy's quote depth, NO hedge, NO fees, 00:00 to 02:00 UTC. So the hedge is ASSUMED: it sells at the Binance BID 45 ms after the fill (the probe's own
hedge-move convention, b0/quote_to_hedge_move.py); hedge impact is 0 by construction (the touch is assumed to fit the size), the fee difference is 0 by construction (no fees), and the believed edge is the believed gross edge
at the quote (the Binance bid at placement against the quote price), with no allowances. Binance books from data/binance (read only), first 2 h of each day.
usage: probe_shortfall.py"""
import bisect, glob, gzip, json, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src")
from crypto_stat_arb.metrics import distribution_stats, shortfall_split

B0 = Path("/Users/vaibhav/Documents/Newproject/out/integration/b0")
DATA = Path("/Users/vaibhav/Documents/Newproject/data/binance/book_snapshots/pair=SOLUSDT")
MODE = "decision" if "--decision-touch" in sys.argv else "series"                  # series (main): the quote-time hedge price is read from the SAME Binance snapshot series as the fill and hedge prices (no clock-basis mix); decision (sensitivity): the touch the probe saw at the decision
OUT = Path("/Users/vaibhav/Documents/Newproject/out/integration/cost/probe_shortfall" + ("_decision_touch" if MODE == "decision" else "")); OUT.mkdir(exist_ok=True)
TRAIN = [f"2024-03-0{i}" for i in range(1, 8)]
SELECT = ["2024-03-08", "2024-03-09", "2024-03-10", "2024-03-11"]
HEDGE_S, BURST_GAP_S, REPS = 0.045, 60.0, 2000
PARTS = ("believed_net_bps", "realised_net_bps", "shortfall_bps", "delay_drift_bps", "hedge_latency_drift_bps", "hedge_impact_bps", "fee_diff_bps", "quote_price_term_bps", "believed_allowances_bps", "residual_bps")


def bids(day):
    t0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    ts, bid = [], []
    for fp in sorted(glob.glob(f"{DATA}/date={day}/*.gz")):
        with gzip.open(fp, "rt") as fh:
            for l in fh:
                r = json.loads(l)
                t = float(r.get("local_ts") or r["timestamp"])
                if t > t0 + 7200 + 60:
                    break
                ts.append(t); bid.append(float(r["best_bid"]))
    return ts, bid


def day_rows(day, group):
    base = B0 / ("probe_adverse" if group == "TRAIN" else "probe/select")
    d = json.load(open(base / f"{day}.json"))
    place, sig_of_fill = {}, {}
    for l in open(base / f"run_{day}" / "maker_observations.jsonl"):
        o = json.loads(l)
        if o["obs"] == "maker_place":
            place[o["signal_id"]] = o
        elif o["obs"] == "maker_fill":
            sig_of_fill[round(o["ts"], 6)] = o["signal_id"]                         # the SELECT rows carry no signal id: the fill's observation does
    ts, bid = bids(day)
    at = lambda t: bid[max(0, bisect.bisect_right(ts, t) - 1)]
    rows, sigs = [], {}
    excluded_no_place = [0]
    for r in d["rows"]:
        sid = r.get("signal_id", sig_of_fill.get(round(r["ts"], 6)))
        p = place.get(sid)
        if not p:
            excluded_no_place[0] += 1
            continue
        r = dict(r, signal_id=sid)
        h_arr = at(r["ts"] + HEDGE_S)
        gross = (h_arr - r["price"]) / r["price"] * 1e4
        iid = f"{day}-{r['signal_id']}-{r['ts']:.6f}"
        rows.append({"maker_intent_id": iid, "maker_side": "buy", "maker_price": r["price"], "maker_fill_ts": r["ts"], "first_hedge_fill_delay_s": HEDGE_S, "hedge_venue": "BINANCE", "hedge_avg_price": h_arr,
                     "qty": r["qty"], "fully_hedged": True, "pair_gross_bps": gross, "pair_net_bps": gross})
        q, hq = float(p["price"]), (float(p["hedge_touch_at_decision"]["bid"]) if MODE == "decision" else at(p["ts"]))
        sigs[iid] = {"buy_price": q, "sell_price": hq, "net_edge_bps": (hq - q) / q * 1e4}
    res = shortfall_split(rows, sigs, lambda v, t, side: at(t), 0.0, reps=1)
    res["excluded"]["no_placement_record"] = excluded_no_place[0]
    return res, len(d["rows"]), group


def bursts(rows):
    out, b, last = [], 0, None
    for x in sorted(rows, key=lambda x: x["maker_fill_ts"]):
        if last is not None and x["maker_fill_ts"] - last > BURST_GAP_S:
            b += 1
        out.append((x, b)); last = x["maker_fill_ts"]
    return out


def stats(rows, block_key):
    return {p: distribution_stats([x[p] for x in rows], [block_key(x) for x in rows], reps=REPS) for p in PARTS}


report = {"label": "EXPLORATORY: Perold split of PROBE fills (no hedge sent, no fees; hedge assumed at the Binance bid 45 ms after the fill; quote-time hedge price from the " + ("probe's decision touch (SENSITIVITY)" if MODE == "decision" else "same Binance snapshot series as the other two times (MAIN)") + "); TRAIN 03-01..07 and SELECT 03-08..11; HOLDOUT not read", "days": {}, "groups": {}}
allrows = {"TRAIN": [], "SELECT": [], "ALL": []}
for group, days in (("TRAIN", TRAIN), ("SELECT", SELECT)):
    for day in days:
        res, n_json, _ = day_rows(day, group)
        bl = {id(x): b for x, b in bursts(res["rows"])}
        for x in res["rows"]:
            x["day"], x["burst"] = day, f"{day}#{bl[id(x)]}"
        report["days"][day] = {"group": group, "fills_in_probe_file": n_json, "fills_split": len(res["rows"]), "excluded": res["excluded"], "max_abs_residual_bps": res["max_abs_residual_bps"],
                               "bursts": len({x['burst'] for x in res['rows']}), "summary_burst_blocks": stats(res["rows"], lambda x: x["burst"])}
        allrows[group] += res["rows"]; allrows["ALL"] += res["rows"]
        print(day, group, "fills", len(res["rows"]), "of", n_json, flush=True)
for g, rows in allrows.items():
    report["groups"][g] = {"fills": len(rows), "bursts": len({x["burst"] for x in rows}), "days": len({x["day"] for x in rows}), "summary_burst_blocks": stats(rows, lambda x: x["burst"]), "summary_day_blocks": stats(rows, lambda x: x["day"]),
                           "max_abs_residual_bps": max((abs(x["residual_bps"]) for x in rows), default=0.0)}
(OUT / "probe_shortfall.json").write_text(json.dumps(report, indent=1))
m = lambda s: "-" if s.get("mean") is None else f"{s['mean']:.3f}"
ci = lambda s: "-" if not s.get("ci95") else f"[{s['ci95'][0]:.2f}, {s['ci95'][1]:.2f}]"
lines = ["# Shortfall split on the probe fills (Perold), TRAIN 03-01..07 and SELECT 03-08..11", "", report["label"], "",
         "Mean bps per fill (of the fill price) with the 95% block-bootstrap interval (blocks = bursts of fills less than 60 s apart). Hedge impact and fee difference are 0 by construction in this probe.", "",
         "| group / day | fills | believed | realised | shortfall | delay drift | hedge latency drift | hedge impact | fee diff | quote price | -allowances |", "|---|---|---|---|---|---|---|---|---|---|---|"]
cols = ("believed_net_bps", "realised_net_bps", "shortfall_bps", "delay_drift_bps", "hedge_latency_drift_bps", "hedge_impact_bps", "fee_diff_bps", "quote_price_term_bps", "believed_allowances_bps")
def line(name, n, s, sign_last=True):
    cells = []
    for c in cols:
        v = s[c]
        cells.append(f"{(-v['mean'] if c == 'believed_allowances_bps' and v.get('mean') is not None else v.get('mean')) if v.get('mean') is not None else '-':.3f} {ci(v) if c != 'believed_allowances_bps' else ''}".strip() if v.get("mean") is not None else "-")
    return f"| {name} | {n} | " + " | ".join(cells) + " |"
for day, v in report["days"].items():
    lines.append(line(f"{v['group']} {day}", v["fills_split"], v["summary_burst_blocks"]))
for g in ("TRAIN", "SELECT", "ALL"):
    v = report["groups"][g]
    lines.append(line(f"**{g} pooled (burst blocks)**", v["fills"], v["summary_burst_blocks"]))
    lines.append(line(f"{g} pooled (DAY blocks, {v['days']} days)", v["fills"], v["summary_day_blocks"]))
(OUT / "probe_shortfall.md").write_text("\n".join(lines))
print("\n".join(lines))
for g in ("TRAIN", "SELECT", "ALL"):
    print(g, "max residual", report["groups"][g]["max_abs_residual_bps"])
