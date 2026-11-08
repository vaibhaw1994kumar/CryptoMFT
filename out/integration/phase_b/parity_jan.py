"""Phase D, D5: January parity runs (a run wrapper). Usage: parity_jan.py DAY old|new OUT_RUNS
old = v1_4 on the backtester's loop (order events on), new = v1_5 on the session; same profile values, same overrides, labelled EXPLORATORY, size 1 SOL, funding NOT modelled on either side.
Deviations from the profiles, the same on both runtimes and recorded in the summary: (1) the profile's pinned hedge-move file is for March TRAIN days, which are not before a January run start, so it is
removed and the January hedge-move file is used where one exists (2022-01-13) and none (0.0 = no information) for 2022-01-05, as the January cells always did; (2) venue_ticks are the January grid
(Bybit 0.005, Binance 0.001), not the profile's March values; (3) the data manifest gap (Binance trade tape truncated at about 17 h) is written, as for every v1_2 and later run."""
import dataclasses
import json
import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src")
from new_backtest.backtester import CrossVenueArbBacktester
from new_backtest.profiles import apply_profile, load_profile

day, which, runs = sys.argv[1], sys.argv[2], Path(sys.argv[3])
X = Path("/Users/vaibhav/Documents/Newproject/out/queue_v2")
REASON = "exploratory: Binance trade tape truncated at ~17 h on 2022-01-05 and 2022-01-13 (queue_v2 manifest); each maker order is judged against the tape of the venue it rests on"
name = "perp_perp_maker_taker_v1_4" if which == "old" else "perp_perp_maker_taker_v1_5"
out = runs / ("g1o" if which == "old" else "g1s") / "base" / name / day
out.mkdir(parents=True, exist_ok=True)
doc = load_profile(name)
doc.pop("hedge_move_calibration", None)
cfg = apply_profile(doc, dataset="jan2022", data_root="/Users/vaibhav/Documents/Newproject/data", pair="SOLUSDT", start_date=day, end_date=day, output_dir=str(out), funding_rate_dir=None,
                    max_trade_log=10 ** 7, equity_bridge="warn", allow_manifest_gap=REASON, record_order_events=True)
cfg.maker = dataclasses.replace(cfg.maker, unknown_queue_fit=str(X / f"unknown_queue_fit_for_{day}.json"))
cfg.data_manifest = str(X / "manifest.json")
cfg.arb.venue_ticks = {"bybit": 0.005, "binance": 0.001}
cfg.arb.max_position_base = 1.0
hm = X / "hedge_move" / f"jan_train_for_{day}.json"
if hm.exists():
    d = json.loads(hm.read_text())
    assert all(x < day for x in d["days"])
    cfg.arb.expected_hedge_move_bps_bid, cfg.arb.expected_hedge_move_bps_ask = float(d["expected_hedge_move_bps"]["bid"]), float(d["expected_hedge_move_bps"]["ask"])
else:
    cfg.arb.expected_hedge_move_bps_bid = cfg.arb.expected_hedge_move_bps_ask = 0.0
t0 = time.time()
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    if which == "old":
        bt = CrossVenueArbBacktester(cfg)
        r = bt.run()
        hr = r.hedge_report or {}
        summary = {"label": "EXPLORATORY, funding not modelled", "day": day, "runtime": "backtester", "size_base": cfg.arb.max_position_base, "signals": r.signals_generated, "orders_sent": r.orders_sent, "fills": r.orders_filled,
                   "cash_pnl": r.cash_pnl, "ending_equity_common_timestamp": r.ending_equity, "fees_paid": r.fees_paid, "final_inventory": r.final_inventory,
                   "hedge_counters": {k: v for k, v in hr.items() if isinstance(v, (int, float))}, "hedge_move_bps": [cfg.arb.expected_hedge_move_bps_bid, cfg.arb.expected_hedge_move_bps_ask],
                   "timing_stats": dict(bt.timing_stats), "breaker_trips": [{"ts": t["ts"], "cause": t["cause"]} for t in ((hr.get("circuit_breaker") or {}).get("trips") or [])], "wall_s": time.time() - t0}
    else:
        from new_backtest.session_replay import SessionReplay, order_times_new, write_result
        rep = SessionReplay(cfg, doc)
        res = rep.run()
        write_result(res, out)
        (out / "order_times.json").write_text(json.dumps(order_times_new(rep), indent=1, default=str))
        summary = {"label": res["label"], "day": day, "runtime": "session", "size_base": cfg.arb.max_position_base, "signals": len(res["signals"]), "orders": len(res["orders"]), "fills": len(res["fills"]),
                   "events": res["events"], "suspended_seconds_total_by_day": res["manifest"]["invalid_book_policy"]["suspended_seconds_total_by_day"],
                   "hedge_move_bps": [cfg.arb.expected_hedge_move_bps_bid, cfg.arb.expected_hedge_move_bps_ask], "wall_s": time.time() - t0}
(out / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
print(day, which, "done", f"{time.time() - t0:.0f}s", {k: summary.get(k) for k in ("signals", "orders_sent", "orders", "fills")}, flush=True)
