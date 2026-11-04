"""Queue model v2 step 13 (and 8, 2, 4): strategy sensitivity table from the replay outputs. Every P&L number is EXPLORATORY.
usage: qv2_sens.py OUT.json"""
import bisect, glob, gzip, json, sys
from pathlib import Path
X = Path("/Users/vaibhav/Documents/Newproject/out/queue_v2")
DATA = Path("/Users/vaibhav/Documents/Newproject/data")
RUNS = X / "runs" / "perp_perp_maker_taker_v1"
TAGPFX = "singapore_bybit_perp_mm_clean_s1_"
DAYS = ("2022-01-05", "2022-01-13")
HZ = (1.0, 5.0, 30.0)
_mids: dict = {}

def mids(venue, day):
    k = (venue, day)
    if k not in _mids:
        t, m = [], []
        for f in sorted(glob.glob(f"{DATA}/{venue}/book_snapshots/pair=SOLUSDT/date={day}/*.jsonl.gz")):
            try:
                with gzip.open(f, "rt") as fh:
                    for line in fh:
                        r = json.loads(line)
                        if r.get("best_bid") and r.get("best_ask"):
                            t.append(r["timestamp"]); m.append((r["best_bid"] + r["best_ask"]) / 2)
            except EOFError:
                pass
        _mids[k] = (t, m)
    return _mids[k]

def mid_at(venue, day, ts):
    t, m = mids(venue, day)
    j = bisect.bisect_right(t, ts) - 1
    return m[j] if j >= 0 else None

def markouts(rows, day):
    out = {}
    for venue in ("bybit", "binance"):
        rs = [r for r in rows if r["venue"] == venue]
        o = {}
        for H in HZ:
            q = s = adv = n = 0.0
            for r in rs:
                m = mid_at(venue, day, r["ts"] + H)
                if m is None: continue
                sign = 1.0 if r["side"] == "buy" else -1.0
                b = sign * (m - r["price"]) / r["price"] * 1e4
                q += r["qty"]; s += b * r["qty"]; adv += float(b < 0); n += 1
            o[f"{int(H)}s"] = {"n": int(n), "qty_weighted_bps": (s / q) if q else None, "adverse_rate": (adv / n) if n else None}
        out[venue] = o
    return out

def load(tag, day):
    d = RUNS / f"{TAGPFX}{tag}" / day
    if not (d / "summary.json").exists(): return None
    s = json.loads((d / "summary.json").read_text())
    rows = [json.loads(l) for l in open(d / "maker_observations.jsonl")] if (d / "maker_observations.jsonl").exists() else []
    mf = [r for r in rows if r["obs"] == "maker_fill"]
    return s, mf

def main(out):
    res = {"label": "EXPLORATORY: 2 days, clean preset, size 1; markouts from 1 s snapshot mids (offline), maker fills only", "days": {}}
    tags = sorted({p.name[len(TAGPFX):] for p in RUNS.glob(TAGPFX + "*")})
    for day in DAYS:
        base = load("base", day)
        base_sigs = {r["signal_id"]: r for r in base[1]} if base else {}
        dd = {}
        for tag in tags:
            x = load(tag, day)
            if x is None: continue
            s, mf = x
            sigs = {r["signal_id"]: r for r in mf}
            ob = s["qv2"]["maker_observations"]
            dd[tag] = {"signals": s["signals"], "fills": s["fills"], "maker_fills": len(mf), "cash_pnl": s["cash_pnl"], "equity_common_ts": s["ending_equity_common_timestamp"],
                       "fees": s["fees_paid"], "fill_causes": ob.get("maker_fill_causes"), "flags": ob.get("maker_fill_uncertainty_flags"),
                       "markout_maker_by_venue": markouts(mf, day),
                       "maker_fill_signals_added_vs_base": sorted(set(sigs) - set(base_sigs)), "maker_fill_signals_removed_vs_base": sorted(set(base_sigs) - set(sigs)),
                       "qv2": s["qv2"]["maker"] | {"cfg": s["qv2"]["cfg"]}, "unknown_queue_draw_fallbacks": (s["qv2"]["maker_diagnostics"] or {}).get("unknown_queue_fit_missing_venue_used_floor"),
                       "reconciliation_ok": json.loads((RUNS / f"{TAGPFX}{tag}" / day / "execution_reconciliation.json").read_text())["ok"],
                       "own_taker_consumption": ob.get("own_taker_book_consumption")}
        res["days"][day] = dd
    Path(out).write_text(json.dumps(res, indent=1, default=str))
    for day, dd in res["days"].items():
        print(day)
        for tag, v in dd.items():
            print(f"  {tag:18s} sig {v['signals']:3d} maker_fills {v['maker_fills']:3d} cash {v['cash_pnl']:8.4f} eq {v['equity_common_ts']:8.4f} +{len(v['maker_fill_signals_added_vs_base'])}/-{len(v['maker_fill_signals_removed_vs_base'])} causes {v['fill_causes']}")
main(sys.argv[1])
