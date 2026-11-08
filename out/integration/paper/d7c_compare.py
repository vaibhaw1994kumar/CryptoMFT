"""Phase D, D7c (a run wrapper, offline, no network): the SAME recorded stream and the SAME probe orders through the old paper simulator (maker_sim, the C++ engine) and the new path (the shared
SimulatedVenueFeeder, QueueAwareVenueAdapter, the Python queue simulator, queue v2).
Usage: d7c_compare.py DAY MINUTES OUT_JSON [MAKER_OVERRIDES_JSON]   (overrides go to the new path's MakerConfig, e.g. {"cancel_model": "behind_first"})
Input: a recorded March-2024 slice (no recorded paper session with trades exists on this Mac: the paper_watch cycles are on the VM), converted to canonical events in receive order by the profile's timing, as the
replay runs use. Old path: `MakerDryRunSimulator` quotes at the touch every second on each venue, with its own latency draws. The orders it sent (arrival time, side, price, size) and the cancels it applied are
recorded and injected, at the same times, into the new path; the two engines then answer the same questions about the same orders. EXPLORATORY; March slice, probe orders, not a strategy result."""
import dataclasses
import json
import sys
import time
import warnings
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src")
from crypto_stat_arb.execution import CanonicalOrderRequest, ExecutionEventType, OrderAction, OrderType, TimeInForce
from crypto_stat_arb.maker_sim import maker_simulator_from_config, replay_stream
from crypto_stat_arb.models import Side, TradeEvent
from crypto_stat_arb.session import ExecutionMode, TradingSession
from crypto_stat_arb.simulation.queue_adapter import SimulatedVenueFeeder, build_queue_simulation
from new_backtest.profiles import apply_profile, load_profile
from new_backtest.session_replay import SessionReplay

day, minutes, out_path = sys.argv[1], float(sys.argv[2]), Path(sys.argv[3])
maker_overrides = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}
SLICES = "/Users/vaibhav/Documents/Newproject/out/queue_v2/slices_mar2024"
X = Path("/Users/vaibhav/Documents/Newproject/out/queue_v2")
profile = load_profile("perp_perp_maker_taker_v1_5")
bt_cfg = apply_profile(profile, dataset="mar2024", data_root=SLICES, pair="SOLUSDT", start_date=day, end_date=day, output_dir="/tmp/x_d7c")
bt_cfg.arb.max_position_base = 1.0
rep = SessionReplay(bt_cfg, profile)                       # only for its feed (profile timing, decimal numbers) and the core config
cfg = rep.app_config
cfg.strategies["maker_simulation"] = {"enabled": True, "quote_size": "1.0", "max_inventory": "2.0", "engine": "auto", "quote_interval_ms": 1000, "depth": 50}

t0 = time.time()
events, first = [], None
for ev in rep.feed.convert(rep._timed_stream()):
    first = first or ev.receive_ts_ns
    if ev.receive_ts_ns - first > minutes * 60 * 1e9:
        break
    events.append(ev)
print(f"stream: {len(events)} events ({sum(isinstance(e, TradeEvent) for e in events)} prints) in {time.time() - t0:.0f}s", flush=True)

# ---- old path ----
sim = maker_simulator_from_config(cfg)
cancels = []
original = sim._on_cancel_applied


def spy(order, now):
    cancels.append((order.client_order_id, now, order.state))
    return original(order, now)


sim._on_cancel_applied = spy
t1 = time.time()
counts = replay_stream(cfg, sim, [("TRADE" if isinstance(e, TradeEvent) else "BOOK", e) for e in events])
print(f"old path ({sim.engine}): {counts}, orders {len(sim.orders)}, fills {len(sim.fills)} in {time.time() - t1:.0f}s", flush=True)

# ---- new path ----
adapters, views, states, executor = build_queue_simulation(cfg, maker_overrides)
session = TradingSession(cfg, execution_mode=ExecutionMode.INTERNAL_SIMULATION, adapters=adapters, strategies=[])
feeder = SimulatedVenueFeeder(session, {v: v for v in cfg.venues}, views, states, adapters, ets_of=lambda ns: None)
new_fills, new_acks, queue_ahead = [], {}, {}
session.router._deliver_resting_fills = lambda venue, produced, now_ns, exchange_ts_ns=None: new_fills.extend(
    {"order_id": req.order_id, "ts_ns": now_ns, "qty": float(cb.last_fill_quantity), "cum": float(cb.cumulative_filled_quantity), "price": float(cb.average_price), "venue": venue} for req, cb in produced) or len(produced)
inj = []
for o in sim.orders.values():
    inj.append((o.arrival_ts_ns, 0, "arrive", o))
for oid, ts, state in cancels:
    if state == "RESTING":
        inj.append((ts, 1, "cancel", sim.orders[oid]))
inj.sort(key=lambda r: (r[0], r[1]))
i = 0


def request(action, o, ts):
    return CanonicalOrderRequest.create(
        action=action, order_id=o.client_order_id, target_order_id=o.client_order_id if action == OrderAction.CANCEL else None, parent_opportunity_id="d7c", parent_order_id="d7c",
        instrument_id=cfg.instrument_id, venue=o.venue, side=Side.BUY if o.side == "BUY" else Side.SELL, price=Decimal(repr(o.price)), quantity=Decimal(repr(o.size)),
        order_type=OrderType.LIMIT, time_in_force=TimeInForce.GTC, post_only=True, created_ts_ns=ts)


t2 = time.time()
for ev in events:
    while i < len(inj) and inj[i][0] <= ev.receive_ts_ns:
        ts, _, kind, o = inj[i]
        i += 1
        book = session.books[(o.venue, cfg.instrument_id)]
        if kind == "arrive":
            (cb, *_) = adapters[o.venue].handle_at(request(OrderAction.SUBMIT, o, ts), book, ts)
            new_acks[o.client_order_id] = cb.event_type.value
            mid = adapters[o.venue]._maker_id.get(o.client_order_id)
            if mid is not None:
                queue_ahead[o.client_order_id] = executor.orders[mid].queue_ahead
        else:
            adapters[o.venue].handle_at(request(OrderAction.CANCEL, o, ts), book, ts)
    if isinstance(ev, TradeEvent):
        feeder.on_trade(ev)
    else:
        feeder.on_book(ev)
print(f"new path: acks {len(new_acks)}, fills {len(new_fills)} in {time.time() - t2:.0f}s", flush=True)

# ---- compare ----
old_by = {}
for f in sim.fills:
    old_by.setdefault(f["client_order_id"], []).append(f)
new_by = {}
for f in new_fills:
    new_by.setdefault(f["order_id"], []).append(f)
rows, same_ack = [], 0
for oid, o in sim.orders.items():
    ack_old = "REJECTED" if o.state == "REJECTED" else "ACKNOWLEDGED"
    ack_new = new_acks.get(oid)
    same_ack += ack_old == ack_new
    of, nf = old_by.get(oid, []), new_by.get(oid, [])
    rows.append({"order": oid, "venue": o.venue, "side": o.side, "price": o.price, "size": o.size, "arrival_ns": o.arrival_ts_ns, "ack_old": ack_old, "ack_new": ack_new,
                 "queue_ahead_old": o.queue_ahead_at_arrival, "queue_ahead_new": queue_ahead.get(oid),
                 "old_fills": [(f["match_ts_ns"], f["quantity"], f["price"], f["cause"]) for f in of], "new_fills": [(f["ts_ns"], f["qty"], f["price"]) for f in nf]})
both = [r for r in rows if r["old_fills"] and r["new_fills"]]
only_old = [r for r in rows if r["old_fills"] and not r["new_fills"]]
only_new = [r for r in rows if r["new_fills"] and not r["old_fills"]]
exact = [r for r in both if abs(sum(q for _, q, _, _ in r["old_fills"]) - sum(q for _, q, _ in r["new_fills"])) < 1e-9 and abs(r["old_fills"][0][0] - r["new_fills"][0][0]) <= 1_000_000]
qa = [(r["queue_ahead_old"], r["queue_ahead_new"]) for r in rows if r["queue_ahead_old"] is not None and r["queue_ahead_new"] is not None]
summary = {
    "label": "EXPLORATORY: D7c, probe orders of the old paper simulator on a recorded March slice; not a strategy result", "day": day, "minutes": minutes, "new_path_maker_overrides": maker_overrides, "events": len(events), "engine_old": sim.engine,
    "orders": len(rows), "same_acceptance": same_ack, "old_filled_orders": sum(1 for r in rows if r["old_fills"]), "new_filled_orders": sum(1 for r in rows if r["new_fills"]),
    "filled_by_both": len(both), "filled_only_by_old": len(only_old), "filled_only_by_new": len(only_new), "both_same_qty_and_first_fill_within_1ms": len(exact),
    "old_fill_rows": len(sim.fills), "new_fill_rows": len(new_fills), "old_filled_qty": sum(f["quantity"] for f in sim.fills), "new_filled_qty": sum(f["qty"] for f in new_fills),
    "queue_ahead_pairs": len(qa), "queue_ahead_mean_old": sum(a for a, _ in qa) / len(qa) if qa else None, "queue_ahead_mean_new": sum(b for _, b in qa) / len(qa) if qa else None,
    "old_fills_by_cause": {c: sum(1 for f in sim.fills if f["cause"] == c) for c in {f["cause"] for f in sim.fills}}, "new_fills_by_source": {v: dict(a.fills_by_source) for v, a in adapters.items()},
    "executor_diagnostics": {k: v for k, v in executor.diagnostics().items() if isinstance(v, (int, float))} if hasattr(executor, "diagnostics") else None,
}
out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(json.dumps({"summary": summary, "orders": rows}, indent=1, default=str))
print(json.dumps(summary, indent=1, default=str))
