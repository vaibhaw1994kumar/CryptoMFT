"""Markout port check (owner 10 Nov), a run wrapper: `markout_by_venue_role` of the old backtester (v1_4, trades from the backtester loop) against the core session runtime (v1_5) on the same March slice.
CRITERION, written before the numbers were read:
  (1) the fills must agree first, by the D5 parity rule (`session_replay.compare_parity`: per (venue, side) quantity, vwap within 1 tick, first and last fill time within 1 ms); if they do not, the markouts are not comparable and that is reported, not the port;
  (2) the same (venue/role) keys and horizons in both;
  (3) per (venue/role, horizon): the quantity-weighted markout in bps within 0.01 bps and the adverse rate within 0.02; the fill COUNT may differ by design (the backtester reports partial chunks) and is shown.
usage: compare_markouts.py OLD_RUN_DIR NEW_RUN_DIR OUT_JSON"""
import json
import sys
from pathlib import Path

sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src")
from new_backtest.session_replay import compare_parity

old, new, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
so, sn = json.loads((old / "summary.json").read_text()), json.loads((new / "summary.json").read_text())
parity = compare_parity(old, new, {"bybit": 0.001, "binance": 0.001})
pick = lambda sm, k: (sm.get(k) or (sm.get("qv2") or {}).get(k) or {})          # the backtester summary carries them under "qv2", the session summary at the top
mo, mn = pick(so, "markout_by_venue_role"), pick(sn, "markout_by_venue_role")
rows, ok = [], not parity["differences"]
for key in sorted(set(mo) | set(mn)):
    for h in sorted(set(mo.get(key, {})) | set(mn.get(key, {}))):
        a, b = mo.get(key, {}).get(h), mn.get(key, {}).get(h)
        if a is None or b is None:
            rows.append({"key": key, "horizon": h, "old": a, "new": b, "ok": False})
            ok = False
            continue
        good = abs(a["qty_weighted_markout_bps"] - b["qty_weighted_markout_bps"]) <= 0.01 and abs(a["adverse_rate"] - b["adverse_rate"]) <= 0.02
        rows.append({"key": key, "horizon": h, "old": a, "new": b, "ok": good})
        ok = ok and good
pooled = {"old": pick(so, "markout_summary"), "new": pick(sn, "markout_summary")}
out.write_text(json.dumps({"criterion": __doc__.split("CRITERION, written before the numbers were read:")[1].split("usage:")[0].strip(), "fill_parity_differences": parity["differences"], "fills_old_new_rows": {k: [v["rows"] for v in (parity["old_legs"].get(k), parity["new_legs"].get(k)) if v] for k in sorted(set(parity["old_legs"]) | set(parity["new_legs"]))},
                           "markout_rows": rows, "pooled_summary": pooled, "ALL_MATCH": ok}, indent=1, default=str))
print("fill parity differences:", parity["differences"])
for r in rows:
    print(r["key"], r["horizon"], "old", r["old"], "new", r["new"], "OK" if r["ok"] else "DIFF")
print("ALL_MATCH", ok)
