"""Owner 7 Nov items 2 and 3, report only: the March slices 03-08..03-11 (00:00-02:00 UTC) on the FINAL v1_2 (March windows 0.15 s / 50 ms, placement_book view, through rule off, ledger on, stamps untrusted = cell ft).
Cells (run_ledger_cells.py FINAL): f0 / f1 = ledger off / on, stamps trusted; ft = the profile as it stands; fp = ft + placement_book truth; fbf / faf / fco = ft with cancel model behind_first / ahead_first / cohort uniform.
Per cell and day: signals, maker fills by cause, cash P&L, gross P&L, fees, equity, maker markout 1 / 5 / 30 s, hedge move, plus whether the result files equal the reference cell's byte for byte. EXPLORATORY: size 1, singapore, fees ASSUMED, 2-hour slices, SELECT days.
Output: final_cells_report.json."""
import json, sys
from pathlib import Path
R = Path("/Users/vaibhav/Documents/Newproject/out/integration/phase_b/ledger/runs")
sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/scripts")
import compare_baselines as cb
DAYS = ("2024-03-08", "2024-03-09", "2024-03-10", "2024-03-11")
CELLS = ("ft", "f0", "f1", "fp", "fbf", "faf", "fco")
PAIRS = {"ledger (f1 vs f0, stamps trusted)": ("f1", "f0"), "stamps (ft vs f1, ledger on)": ("ft", "f1"), "placement truth alone (fp vs ft)": ("fp", "ft"),
         "behind_first vs headline": ("fbf", "ft"), "ahead_first vs headline": ("faf", "ft"), "cohort uniform vs headline": ("fco", "ft")}
P = "perp_perp_maker_taker_v1_2"


def d(cell, day):
    return R / cell / "base" / P / day


def row(cell, day):
    p = d(cell, day)
    if not (p / "summary.json").exists():
        return None
    s = json.load(open(p / "summary.json"))
    q = s.get("qv2", {})
    mo = q.get("maker_observations", {})
    mk = q.get("markout_summary", {})
    lg = s.get("legging", {}).get("hedge_move_bps", {}).get("bid", {})
    return {"signals": s.get("signals"), "maker_fills": (mo.get("counts") or {}).get("maker_fill", 0), "causes": mo.get("maker_fill_causes"), "orders_placed": (q.get("maker_stats") or {}).get("maker_orders_placed"),
            "cash_pnl": s.get("cash_pnl"), "gross_pnl": s.get("gross_pnl"), "fees": s.get("fees_paid"), "equity": s.get("ending_equity_common_timestamp"),
            "markout_1s": (mk.get("1s") or {}).get("qty_weighted_markout_bps"), "markout_5s": (mk.get("5s") or {}).get("qty_weighted_markout_bps"), "markout_30s": (mk.get("30s") or {}).get("qty_weighted_markout_bps"),
            "markout_n": (mk.get("1s") or {}).get("count"), "hedge_move_bid_mean": lg.get("mean"), "hedge_move_n": lg.get("n"),
            "placement_stats": {k: v for k, v in ((s.get("book_stats") or {}).items()) if "placement" in k} or None,
            "wall_s": s.get("wall_s")}


def same(a, b, day):
    pa, pb = d(a, day), d(b, day)
    if not ((pa / "summary.json").exists() and (pb / "summary.json").exists()):
        return None
    files = {f: cb.sha(pa / f) == cb.sha(pb / f) for f in cb.FILES}
    diff = cb.diff(json.load(open(pa / "summary.json")), json.load(open(pb / "summary.json")))
    return {"files_equal": files, "all_files_equal": all(files.values()), "summary_fields_differing": len(diff), "first": [(p, str(x)[:50], str(y)[:50]) for p, x, y in diff[:4]]}


res = {"label": "final v1_2 cells on the March SELECT slices; EXPLORATORY", "rows": {c: {dd: row(c, dd) for dd in DAYS} for c in CELLS}, "pairs": {}}
for name, (a, b) in PAIRS.items():
    res["pairs"][name] = {dd: same(a, b, dd) for dd in DAYS}
tot = {}
for c in CELLS:
    rs = [r for r in res["rows"][c].values() if r]
    tot[c] = {"days": len(rs), "signals": sum(r["signals"] or 0 for r in rs), "maker_fills": sum(r["maker_fills"] or 0 for r in rs), "orders_placed": sum(r["orders_placed"] or 0 for r in rs),
              "cash_pnl": sum(r["cash_pnl"] or 0 for r in rs), "gross_pnl": sum(r["gross_pnl"] or 0 for r in rs), "fees": sum(r["fees"] or 0 for r in rs)}
res["totals"] = tot
(R.parent / "final_cells_report.json").write_text(json.dumps(res, indent=1, default=str))
for c in CELLS:
    print(c, tot[c])
    for dd in DAYS:
        r = res["rows"][c][dd]
        print("   ", dd, None if not r else (r["signals"], r["maker_fills"], r["causes"], round(r["cash_pnl"], 4), r["markout_30s"]))
for name, v in res["pairs"].items():
    print(name, {dd: (None if x is None else ("IDENTICAL" if x["all_files_equal"] and x["summary_fields_differing"] == 0 else f"differs ({x['summary_fields_differing']} summary fields, files {x['files_equal']})")) for dd, x in v.items()})
