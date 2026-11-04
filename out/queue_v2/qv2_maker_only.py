"""Maker-only analysis through the MAIN runner (ArbBacktestConfig.analysis_mode='maker_only'): same profile, PASSIVE intent, latency path, queue models, PostOnly option and
provenance as a strategy replay; no strategy, no hedge; one venue's book only (the other venue is a trade tape outside the engine).
usage: QV2_SPEC='{...}' qv2_maker_only.py    spec: {tag, day, venue, orders_file | grid{...}, maker{...}, max_event_ts, gate_class, out_root}"""
import dataclasses, json, os, sys, time, warnings
warnings.filterwarnings("ignore")
SNAP = os.environ.get("EXEC_SNAP", "/Users/vaibhav/Documents/Newproject/out/queue_v2/snap_qv2c")
sys.path.insert(0, SNAP + "/src")
from pathlib import Path
from new_backtest.backtester import CrossVenueArbBacktester
from new_backtest.profiles import apply_profile, load_profile

spec = json.loads(os.environ["QV2_SPEC"])
day, venue, tag = spec["day"], spec.get("venue", "bybit"), spec["tag"]
out = Path(spec.get("out_root", "/Users/vaibhav/Documents/Newproject/out/queue_v2/maker_only")) / tag / day
out.mkdir(parents=True, exist_ok=True)
doc = load_profile(Path(SNAP) / "profiles" / "perp_perp_maker_taker_v1.yaml")
doc["latency_preset"] = "singapore"
if isinstance(doc.get("timing"), dict):
    doc["timing"]["from_latency_preset"] = "singapore"
orders = [tuple(o) for o in json.load(open(spec["orders_file"]))] if spec.get("orders_file") else []
if spec.get("grid"):
    g = spec["grid"]
    t = g["start"]
    while t <= g["end"]:
        for side in ("buy", "sell"):
            for rule in g["rules"]:
                orders.append((t, side, rule, g.get("qty", 1.0)))
        t += g["every_s"]
kw = dict(data_root="/Users/vaibhav/Documents/Newproject/data", pair="SOLUSDT", start_date=day, end_date=day, output_dir=str(out),
          funding_rate_dir="/Users/vaibhav/Documents/Newproject/out/funding/SOLUSDT", max_trade_log=10 ** 7, equity_bridge="warn", record_exposure=False)
cfg = apply_profile(doc, **kw)
cfg.venues = tuple(spec.get('venues') or [venue])        # first = the maker venue; extra venues only feed the replay (feed-interleaving parity check)
cfg.hedge_faults = None
cfg.margin = None
cfg.circuit_breaker = None
if spec.get("maker"):
    cfg.maker = dataclasses.replace(cfg.maker, **spec["maker"])
cfg.arb.maker_max_live_orders = 10 ** 6
cfg.arb.max_position_base = 1e12
cfg.arb.max_venue_imbalance_base = 1e12
cfg.analysis_mode = "maker_only"
cfg.scheduled_orders = tuple(orders)
cfg.max_event_ts = spec.get("max_event_ts")
cfg.gate_class = spec.get("gate_class", "")
cfg.allow_rejected_data = spec.get("allow_rejected_data", "maker-only analysis on one venue; see gate_class")
t0 = time.time()
r = CrossVenueArbBacktester(cfg).run()
summary = {"tag": tag, "day": day, "venue": venue, "gate_class": cfg.gate_class, "label": "EXPLORATORY", "analysis": r.analysis, "wall_s": time.time() - t0, "events": r.events_processed,
           "maker_orders_placed": r.maker_stats.get("maker_orders_placed"), "maker_orders_filled": r.maker_stats.get("maker_orders_filled"),
           "maker_observations": r.maker_observations, "markout_summary": r.markout_summary, "release_identity": r.release_identity,
           "postonly": r.postonly_report, "spec": {k: v for k, v in spec.items() if k != "orders_file"}}
(out / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
print(tag, day, "fired", r.analysis.get("fired"), "skipped", r.analysis.get("skipped"), "maker filled", r.maker_stats.get("maker_orders_filled"), f"wall {summary['wall_s']:.0f}s", flush=True)
