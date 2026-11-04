#!/usr/bin/env python3
"""Queue model v2, step 2: rebuild acceptance for one venue-day under one event order. Read-only on data/; writes OUT/<venue>_<day>[_capture].json

    python3.12 scripts/queue_v2_rebuild_acceptance.py --data-root DATA --venue binance --pair SOLUSDT --day 2022-01-05 --event-order legacy --out-dir out/queue_v2/rebuild_acceptance
A day whose touch price match is under 99% (any variant that the engine would use) is flagged flag_below_99 and the flag is carried into every result that uses it."""
import argparse, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from new_backtest.event_loader import iter_venue_events          # noqa: E402
from new_backtest.queue_v2.rebuild import acceptance, default_variants   # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--data-root", required=True); ap.add_argument("--venue", required=True); ap.add_argument("--pair", default="SOLUSDT")
ap.add_argument("--day", required=True); ap.add_argument("--event-order", default="legacy", choices=("legacy", "capture"))
ap.add_argument("--out-dir", required=True)
a = ap.parse_args()
t0 = time.time()
ev = iter_venue_events(a.data_root, a.venue, a.pair, a.day, a.day, event_order=a.event_order)
res = acceptance(ev, default_variants(a.venue), a.day)
res.update({"venue": a.venue, "pair": a.pair, "event_order": a.event_order, "wall_s": round(time.time() - t0, 1)})
engine = res["variants"]["engine_tape_repair_on"]
res["flag_below_99"] = engine["touch_price_match_pct"] < 99.0
res["flag_note"] = "touch price match of the engine book (tape repair on, the legacy default) is below 99%" if res["flag_below_99"] else ""
out = Path(a.out_dir); out.mkdir(parents=True, exist_ok=True)
(out / f"{a.venue}_{a.day}{'_capture' if a.event_order == 'capture' else ''}.json").write_text(json.dumps(res, indent=1))
print(a.venue, a.day, a.event_order, {k: (v["touch_price_match_pct"], v["touch_size_match_pct"], v["top10_level_match_pct"]) for k, v in res["variants"].items()}, f"{res['wall_s']}s")
