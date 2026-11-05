"""Hedge-move PROBE (owner, 2026-11-05): keep a 1 SOL Bybit bid resting at the strategy's usual quote depth and measure the Binance mid move over the next 1 s after each FILL.
Quote = Binance best bid (host/local clock) x (1 - Q/1e4), Q = the v1.1 strategy's required edge at hedge move 0 = Bybit maker -0.5 + Binance taker 5.0 + min edge 1.0 + safety 0.5 + p_fail*loss_fail 0.07*7.8 = 6.546 bps,
rounded DOWN to the Bybit tick 0.001. One order per 15 s slot (the order lifetime, maker_max_age_s): it is re-placed at the next slot after a fill or an expiry (a fill leaves a gap up to 15 s).
Fills follow the v1.1 rules (the profile file's maker block: trade-confirmed, cross needs a print within 500 ms and is capped at the print, fitted/fallback unknown queue 25), through the maker-only analysis mode of the main engine.
usage: qv2_probe.py DAY   (data = out/queue_v2/slices_mar2024, 00:00-02:00 UTC)  ->  hedge_move/probe/<DAY>.json"""
import bisect, calendar, dataclasses, glob, gzip, json, math, os, sys, time, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
X = Path("/Users/vaibhav/Documents/Newproject/out/queue_v2")
SNAP = Path(os.environ["QV2_SNAP"]) if os.environ.get("QV2_SNAP") else X / "snap_qv2i"       # QV2_SNAP: run another tree (B0: the live tree, for the queue-update counter)
OUT_TAG = os.environ.get("QV2_PROBE_OUT")
sys.path.insert(0, str(SNAP / "src"))
from new_backtest.backtester import CrossVenueArbBacktester
from new_backtest.profiles import apply_profile, load_profile

day = sys.argv[1]
DATA = str(X / "slices_mar2024")
t0 = calendar.timegm(time.strptime(day, "%Y-%m-%d")); t1 = t0 + 7200
Q_BPS, TICK, SLOT = 6.546, 0.001, 15.0
out = Path(OUT_TAG) if OUT_TAG else X / "hedge_move" / "probe"; out.mkdir(parents=True, exist_ok=True)
run_dir = out / f"run_{day}"; run_dir.mkdir(exist_ok=True)

# Binance L1 on the local clock
ts, bid, ask = [], [], []
for fp in sorted(glob.glob(f"{DATA}/binance/book_snapshots/pair=SOLUSDT/date={day}/*.gz")):
    with gzip.open(fp, "rt") as fh:
        for line in fh:
            r = json.loads(line)
            ts.append(float(r.get("local_ts") or r["timestamp"])); bid.append(float(r["best_bid"])); ask.append(float(r["best_ask"]))
def at(t):
    i = bisect.bisect_right(ts, t) - 1
    return (bid[i], ask[i]) if i >= 0 else None

orders, k = [], 0
t = t0 + 5.0
while t < t1 - 20:
    b = at(t)
    if b:
        px = math.floor(b[0] * (1 - Q_BPS / 1e4) / TICK + 1e-9) * TICK
        orders.append((t, "buy", round(px, 10), 1.0))
    t += SLOT
doc = load_profile(SNAP / "profiles" / "perp_perp_maker_taker_v1_1.yaml")
doc["latency_preset"] = "singapore"
if isinstance(doc.get("timing"), dict):
    doc["timing"]["from_latency_preset"] = "singapore"
cfg = apply_profile(doc, data_root=DATA, pair="SOLUSDT", start_date=day, end_date=day, output_dir=str(run_dir), funding_rate_dir=None, max_trade_log=10 ** 7, equity_bridge="warn", record_exposure=False)
cfg.venues = ("bybit",)
cfg.hedge_faults = None; cfg.margin = None; cfg.circuit_breaker = None
cfg.maker = dataclasses.replace(cfg.maker, unknown_queue_fit=str(X / "unknown_queue_fit_mar2024_NONE.json"))
cfg.arb.maker_max_live_orders = 10 ** 6; cfg.arb.max_position_base = 1e12; cfg.arb.max_venue_imbalance_base = 1e12
cfg.arb.venue_ticks = {"bybit": 0.001}; cfg.arb.round_quotes_to_tick = False      # the probe prices its own orders (rounded above)
cfg.analysis_mode = "maker_only"; cfg.scheduled_orders = tuple(orders); cfg.max_event_ts = t1 + 1.0
cfg.gate_class = "BYBIT_ONLY_WINDOW"
cfg.allow_rejected_data = "probe on a 2-hour slice; Bybit book only (Binance is read here as a price series)"
w0 = time.time()
_bt = CrossVenueArbBacktester(cfg)
r = _bt.run()
rows = [json.loads(l) for l in (run_dir / "trades.jsonl").read_text().splitlines() if l.strip()]
fills = [x for x in rows if not x.get("taker") and x["venue"] == "bybit" and x["side"] == "buy"]
res, drop = [], 0
for x in fills:
    a, b = at(x["ts"]), at(x["ts"] + 1.0)
    if not a or not b or (ts[bisect.bisect_right(ts, x["ts"] + 1.0) - 1] < x["ts"] - 5):
        drop += 1; continue
    m0, m1 = (a[0] + a[1]) / 2, (b[0] + b[1]) / 2
    res.append({"ts": x["ts"], "price": x["price"], "qty": x["qty"], "binance_mid_at_fill": m0, "move_bps": (m1 - m0) / m0 * 1e4})
summary = {"day": day, "orders_scheduled": len(orders), "fired": r.analysis.get("fired"), "skipped": r.analysis.get("skipped"), "fills": len(fills), "fills_measured": len(res), "dropped": drop,
           "maker_fill_causes": (r.maker_observations or {}).get("maker_fill_causes"), "queue_updates": dict(getattr(_bt.maker, "queue_updates", {})), "q_bps": Q_BPS, "slot_s": SLOT, "wall_s": time.time() - w0, "rows": res}
(out / f"{day}.json").write_text(json.dumps(summary, indent=1, default=str))
print(day, "orders", len(orders), "fills", len(fills), "measured", len(res), "causes", summary["maker_fill_causes"], f"wall {summary['wall_s']:.0f}s", flush=True)
