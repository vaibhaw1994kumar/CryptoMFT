"""Offline sweep of the capped-IOC hedge policy over hedge-capture replays (execution layer, phase 1). OFFLINE_FIRST_ORDER.

usage: hedge_sweep_offline.py [--cap-root out/exec_layer/cap] [--fee bybit_perp_mm] [--days 2022-01-05,2022-01-13] [--out FILE]
For each day: validate the offline evaluator against the engine's recorded attempts (must be identical), then run the baseline
(today's hedge, phase 0) and every policy cell on the SAME chains with paired latency draws. Cells: the brief's grid
cap_buffer_bps {0,1,2,4} x phi {0,1e-4,1e-3,1e-2} x deadline_s {1,2,5}, plus a labelled DEVIATION block with scaled phi."""
import argparse, gzip, json, sys
from pathlib import Path

P = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(P / "src"))
from new_backtest.config import FEE_PRESETS
from new_backtest.execution import offline_hedge as oh
from new_backtest.execution.ioc_runtime import IocRuntime
from new_backtest.profiles import load_profile

ap = argparse.ArgumentParser()
ap.add_argument("--cap-root", default=str(P.parent / "out" / "exec_layer" / "cap"))
ap.add_argument("--fee", default="bybit_perp_mm")
ap.add_argument("--days", default="2022-01-05,2022-01-13")
ap.add_argument("--flicker", default=str(P / "config" / "flicker_binance_perp.json"))
ap.add_argument("--out", default=str(P.parent / "out" / "exec_layer" / "sweep_offline.json"))
a = ap.parse_args()

prof = load_profile("perp_perp_maker_taker_v1")
LOSS = float(prof["arb"].get("loss_fail_bps", 7.8))
MAX_WALK = float(prof["arb"].get("max_walk_bps", 2.0))
MAKER_BPS = FEE_PRESETS[a.fee].maker_bps
TAKER_BPS = FEE_PRESETS["binance_perp_vip0"].taker_bps
TAG = f"singapore_{a.fee}_clean_s1"
BUF, PHI, DL = (0.0, 1.0, 2.0, 4.0), (0.0, 1e-4, 1e-3, 1e-2), (1.0, 2.0, 5.0)

cells = [oh.Cell("phase0 (today's hedge)", "phase0")]
for b in BUF:
    for phi in PHI:
        for d in DL:
            cells.append(oh.Cell(f"buf{b:g}_phi{phi:g}_dl{d:g}", "policy", "conditional_edge_breakeven", b, phi, d))
for phi in (0.1, 1.0, 10.0, 100.0):
    cells.append(oh.Cell(f"DEVIATION_scaled_phi{phi:g}_buf1_dl2", "policy", "conditional_edge_breakeven", 1.0, phi, 2.0))


def value(c, r):
    sgn = 1.0 if c["side"] == "sell" else -1.0
    rem = r.get("remaining", 0.0)
    q = r.get("filled", 0.0)
    return sgn * (q * (r["vwap"] or 0.0)) + sgn * rem * c["dec_touch"] * (1.0 - sgn * LOSS / 1e4)


out = {"label": "OFFLINE_FIRST_ORDER, EXPLORATORY", "fee": a.fee, "loss_fail_bps": LOSS, "days": {}, "cells": [c.__dict__ for c in cells],
       "caveat": "slower/failed hedges do not feed back into later signals, breaker trips or the Bybit unwind; arrival latency is re-sampled, paired across cells"}
for day in a.days.split(","):
    d = Path(a.cap_root) / "perp_perp_maker_taker_v1" / TAG / day
    if not (d / "hedge_capture.json.gz").exists():
        out["days"][day] = {"missing": True}; continue
    rows, paths = oh.load_capture(d / "hedge_capture.json.gz")
    trades = [json.loads(l) for l in open(d / "trades.jsonl")]
    val = oh.validate(rows, paths, fee_by_venue={"binance": FEE_PRESETS["binance_perp_vip0"].taker_bps, "bybit": FEE_PRESETS[a.fee].taker_bps})
    chains = oh.chains_from(rows, trades)
    pb = oh.Path5(paths["binance"])
    n_all = len({r["intent_id"] for r in rows if r["intent_id"]})
    res_day = {"validation": val, "chains": len(chains), "hedge_chains_all_venues": n_all, "chains_on_other_venues_excluded": n_all - len(chains), "chains_without_maker_fill_price": sum(1 for c in chains if c["maker_fill_price"] is None), "cells": {}}
    base = None
    for cell in cells:
        rt = None
        if cell.rule == "policy":
            rt = IocRuntime(flicker_path=Path(a.flicker), set_name=f"for_{day}", v0_bps=-LOSS, phi=cell.phi, deadline_s=cell.deadline_s,
                            attempts_cfg="from_latency", cap_buffer_bps=cell.cap_buffer_bps, price_model=cell.price_model)
        results = [oh.simulate(c, pb, cell, runtime=rt, day=day, maker_bps=MAKER_BPS, taker_bps=TAKER_BPS, max_walk_bps=MAX_WALK) for c in chains]
        m = oh.metrics(chains, results, loss_fail_bps=LOSS)
        vals = [value(c, r) for c, r in zip(chains, results) if not r.get("skip")]
        if base is None:
            base = vals
        m["value_usdt"] = sum(vals)
        m["delta_vs_phase0_usdt"] = sum(vals) - sum(base) if len(vals) == len(base) else None
        if rt is not None:
            m["policy_counts"] = dict(rt.counts); m["law_levels"] = dict(rt.levels)
        res_day["cells"][cell.name] = m
    out["days"][day] = res_day
Path(a.out).write_text(json.dumps(out, indent=1, default=str))
print("wrote", a.out)
for day, r in out["days"].items():
    if r.get("missing"):
        print(day, "capture missing"); continue
    print(day, "validation:", {k: r["validation"][k] for k in ("attempts", "identical", "mismatched")}, "chains", r["chains"])
