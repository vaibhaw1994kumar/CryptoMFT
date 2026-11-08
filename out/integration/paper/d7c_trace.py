"""Phase D, D7c follow-up (a run wrapper, offline): where do the old paper engine and the new path part for the early Binance fills?
Usage: d7c_trace.py DAY MINUTES OUT_JSON [MAKER_OVERRIDES_JSON]
Same recorded stream, same probe orders as d7c_compare. Both engines are driven event by event and the QUEUE AHEAD of every resting probe order is recorded after each event. For the Binance orders the new
path fills earlier than the old one, the first event after which the two queues differ is reported with the event and both values."""
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src")
from crypto_stat_arb.execution import CanonicalOrderRequest, OrderAction, OrderType, TimeInForce
from crypto_stat_arb.maker_sim import maker_simulator_from_config
from crypto_stat_arb.models import Side, TradeEvent
from crypto_stat_arb.session import ExecutionMode, TradingSession
from crypto_stat_arb.simulation.queue_adapter import SimulatedVenueFeeder, build_queue_simulation
from new_backtest.profiles import apply_profile, load_profile
from new_backtest.session_replay import SessionReplay

day, minutes, out_path = sys.argv[1], float(sys.argv[2]), Path(sys.argv[3])
overrides = json.loads(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] != "profile" else {}
use_profile_maker = len(sys.argv) > 4 and sys.argv[4] == "profile"
profile = load_profile("perp_perp_maker_taker_v1_5")
bt_cfg = apply_profile(profile, dataset="mar2024", data_root="/Users/vaibhav/Documents/Newproject/out/queue_v2/slices_mar2024", pair="SOLUSDT", start_date=day, end_date=day, output_dir="/tmp/x_d7t")
bt_cfg.arb.max_position_base = 1.0
rep = SessionReplay(bt_cfg, profile)
cfg = rep.app_config
cfg.strategies["maker_simulation"] = {"enabled": True, "quote_size": "1.0", "max_inventory": "2.0", "engine": "auto", "quote_interval_ms": 1000, "depth": 50}
events, first = [], None
for ev in rep.feed.convert(rep._timed_stream()):
    first = first or ev.receive_ts_ns
    if ev.receive_ts_ns - first > minutes * 60 * 1e9:
        break
    events.append(ev)
print(f"stream {len(events)} events", flush=True)

# ---- old path, event by event ----
sim = maker_simulator_from_config(cfg)
cancels = []
orig_cancel = sim._on_cancel_applied
sim._on_cancel_applied = lambda order, now: (cancels.append((order.client_order_id, now, order.state)), orig_cancel(order, now))[1]
books = {v: cfg.book_sanity.new_book(v, cfg.instrument_id) for v in cfg.venues}
trace_old, old_fill_at, old_seen = {}, {}, []
for n, ev in enumerate(events):
    if isinstance(ev, TradeEvent):
        sim.on_trade(ev)
    else:
        books[ev.venue].apply(ev)
        sim.on_book(ev.venue, books[ev.venue], ev.receive_ts_ns, ev.event_id)
    for f in sim.fills[len(old_seen):]:
        old_fill_at.setdefault(f["client_order_id"], n)
    old_seen = list(sim.fills)
    for st in sim.venues.values():
        for oid, o in st.active.items():
            if o.state == "RESTING":
                trace_old.setdefault(oid, {})[n] = st.engine.queue_ahead(oid)
sim.finish(events[-1].receive_ts_ns)
print(f"old: orders {len(sim.orders)}, fills {len(sim.fills)}", flush=True)

# ---- new path, event by event ----
import dataclasses
if use_profile_maker:
    overrides = dataclasses.asdict(bt_cfg.maker)
    overrides["unknown_queue_fit"] = str(Path("/Users/vaibhav/Documents/Newproject/out/queue_v2") / "unknown_queue_fit_mar2024_NONE.json")
adapters, views, states, executor = build_queue_simulation(cfg, overrides)
session = TradingSession(cfg, execution_mode=ExecutionMode.INTERNAL_SIMULATION, adapters=adapters, strategies=[])
feeder = SimulatedVenueFeeder(session, {v: v for v in cfg.venues}, views, states, adapters, ets_of=lambda ns: None)
new_fills, trace_new, new_fill_at = [], {}, {}
session.router._deliver_resting_fills = lambda venue, produced, now_ns, exchange_ts_ns=None: new_fills.extend(
    {"order_id": req.order_id, "ts_ns": now_ns, "qty": float(cb.last_fill_quantity), "venue": venue} for req, cb in produced) or len(produced)
inj = sorted([(o.arrival_ts_ns, 0, "arrive", o) for o in sim.orders.values()] + [(ts, 1, "cancel", sim.orders[oid]) for oid, ts, st in cancels if st == "RESTING"], key=lambda r: (r[0], r[1]))
i = 0


def request(action, o, ts):
    return CanonicalOrderRequest.create(
        action=action, order_id=o.client_order_id, target_order_id=o.client_order_id if action == OrderAction.CANCEL else None, parent_opportunity_id="d7c", parent_order_id="d7c",
        instrument_id=cfg.instrument_id, venue=o.venue, side=Side.BUY if o.side == "BUY" else Side.SELL, price=Decimal(repr(o.price)), quantity=Decimal(repr(o.size)),
        order_type=OrderType.LIMIT, time_in_force=TimeInForce.GTC, post_only=True, created_ts_ns=ts)


for n, ev in enumerate(events):
    while i < len(inj) and inj[i][0] <= ev.receive_ts_ns:
        ts, _, kind, o = inj[i]
        i += 1
        book = session.books[(o.venue, cfg.instrument_id)]
        adapters[o.venue].handle_at(request(OrderAction.SUBMIT if kind == "arrive" else OrderAction.CANCEL, o, ts), book, ts)
    before = len(new_fills)
    feeder.on_trade(ev) if isinstance(ev, TradeEvent) else feeder.on_book(ev)
    for f in new_fills[before:]:
        new_fill_at.setdefault(f["order_id"], n)
    for venue, a in adapters.items():
        for mo in executor.live_orders(venue):
            req = a._order.get(mo.order_id)
            if req is not None:
                trace_new.setdefault(req.order_id, {})[n] = mo.queue_ahead
print(f"new: fills {len(new_fills)}", flush=True)

# ---- targets: Binance orders whose new first fill is earlier ----
old_first = {}
for f in sim.fills:
    old_first.setdefault(f["client_order_id"], f["match_ts_ns"])
new_first = {}
for f in new_fills:
    new_first.setdefault(f["order_id"], f["ts_ns"])
targets = [oid for oid in old_first if oid in new_first and sim.orders[oid].venue == "BINANCE" and (new_first[oid] - old_first[oid]) / 1e6 < -1]
print(f"early Binance orders: {len(targets)}", flush=True)
def describe(n):
    e = events[n]
    if isinstance(e, TradeEvent):
        return {"n": n, "kind": "TRADE", "venue": e.venue, "price": float(e.price), "qty": float(e.quantity), "aggressor": e.aggressor_side, "receive_ns": e.receive_ts_ns}
    return {"n": n, "kind": "BOOK", "venue": e.venue, "bid": [(float(l.price), float(l.quantity)) for l in e.bids][:1], "ask": [(float(l.price), float(l.quantity)) for l in e.asks][:1], "receive_ns": e.receive_ts_ns}


report = []
firsts = {}
for oid in targets:
    a, b = trace_old.get(oid, {}), trace_new.get(oid, {})
    parted = next((n for n in sorted(set(a) & set(b)) if abs(a[n] - b[n]) > 1e-6), None)
    row = {"old_fill_event": describe(old_fill_at[oid]) if oid in old_fill_at else None, "new_fill_event": describe(new_fill_at[oid]) if oid in new_fill_at else None, "order": oid, "side": sim.orders[oid].side, "price": sim.orders[oid].price, "arrival_ns": sim.orders[oid].arrival_ts_ns, "old_first_fill_ns": old_first[oid], "new_first_fill_ns": new_first[oid],
           "first_event_with_different_queue": parted}
    if parted is not None:
        ev = events[parted]
        kind = "TRADE" if isinstance(ev, TradeEvent) else ev.event_type.value
        detail = {"kind": kind, "venue": ev.venue, "receive_ns": ev.receive_ts_ns}
        if isinstance(ev, TradeEvent):
            detail.update(price=float(ev.price), qty=float(ev.quantity), aggressor=ev.aggressor_side)
        else:
            detail.update(bids=[(float(l.price), float(l.quantity)) for l in ev.bids][:3], asks=[(float(l.price), float(l.quantity)) for l in ev.asks][:3])
        row["event"] = detail
        row["queue_ahead_old_before_new_after"] = {"old": a[parted], "new": b[parted], "previous_old": a.get(parted - 1), "previous_new": b.get(parted - 1)}
        firsts[detail["kind"]] = firsts.get(detail["kind"], 0) + 1
    else:
        import bisect
        rts = [e.receive_ts_ns for e in events]
        lo, hi = bisect.bisect_left(rts, sim.orders[oid].arrival_ts_ns), bisect.bisect_right(rts, old_first[oid])
        win = []
        for n in range(max(lo - 1, 0), min(hi + 1, len(events))):
            e = events[n]
            if e.venue != "BINANCE":
                continue
            d = {"n": n, "t_ms_after_arrival": round((e.receive_ts_ns - sim.orders[oid].arrival_ts_ns) / 1e6, 2), "qa_old": trace_old.get(oid, {}).get(n), "qa_new": trace_new.get(oid, {}).get(n)}
            if isinstance(e, TradeEvent):
                d.update(kind="TRADE", price=float(e.price), qty=float(e.quantity), aggressor=e.aggressor_side)
            else:
                d.update(kind="BOOK", bid=[(float(l.price), float(l.quantity)) for l in e.bids][:1], ask=[(float(l.price), float(l.quantity)) for l in e.asks][:1])
            win.append(d)
        row["window_binance_events"] = win[-14:]
        row["note"] = "queues equal at every event before the fill: the difference is in the fill rule, not the queue"
        firsts["queues_equal_until_the_fill"] = firsts.get("queues_equal_until_the_fill", 0) + 1
    report.append(row)
summary = {"day": day, "minutes": minutes, "overrides": ("PROFILE maker settings" if use_profile_maker else overrides), "early_binance_orders": len(targets), "first_parting_event_kind": firsts}
out_path.write_text(json.dumps({"summary": summary, "orders": report}, indent=1, default=str))
print(json.dumps(summary, indent=1))
kinds = {}
for r in report:
    o_, n_ = r["old_fill_event"], r["new_fill_event"]
    key = (o_["kind"] if o_ else None, n_["kind"] if n_ else None, "same event" if o_ and n_ and o_["n"] == n_["n"] else "different events")
    kinds[str(key)] = kinds.get(str(key), 0) + 1
print("early Binance orders by (old fill event kind, new fill event kind):", json.dumps(kinds, indent=1))
for r in [x for x in report if "window_binance_events" in x][:4]:
    print(json.dumps({"order": r["order"], "side": r["side"], "price": r["price"], "old_fill_event": r["old_fill_event"], "new_fill_event": r["new_fill_event"]}, default=str))

# ---- detail of the first early order: both queues and the events around the new fill ----
r0 = report[0]
oid = r0["order"]
n0 = new_fill_at[oid]
print("DETAIL", oid, sim.orders[oid].side, sim.orders[oid].price)
for n in range(max(n0 - 12, 0), n0 + 3):
    e = events[n]
    if e.venue != "BINANCE":
        continue
    d = describe(n)
    print("   ", json.dumps({k: v for k, v in d.items() if k not in ("receive_ns", "venue")}), "qa_old", trace_old.get(oid, {}).get(n), "qa_new", trace_new.get(oid, {}).get(n))

# ---- the pattern, checked on every early Binance order ----
pattern = {"new fill is a print AT the order's price": 0, "...and the last Binance book event before it no longer shows the order's level": 0, "...and the old engine's queue ahead was 0 at that event": 0, "old fills at a print strictly THROUGH the price": 0, "both": 0}
for oid in targets:
    o = sim.orders[oid]
    n = new_fill_at[oid]
    ev = events[n]
    at_price = isinstance(ev, TradeEvent) and abs(float(ev.price) - o.price) < 1e-9
    pattern["new fill is a print AT the order's price"] += at_price
    prev = next((events[k] for k in range(n - 1, -1, -1) if events[k].venue == "BINANCE" and not isinstance(events[k], TradeEvent)), None)
    gone = False
    if prev is not None:
        side = prev.asks if o.side == "SELL" else prev.bids
        gone = not any(abs(float(l.price) - o.price) < 1e-9 for l in side)
    pattern["...and the last Binance book event before it no longer shows the order's level"] += at_price and gone
    qa0 = trace_old.get(oid, {}).get(n, None) == 0.0
    pattern["...and the old engine's queue ahead was 0 at that event"] += at_price and gone and qa0
    oev = events[old_fill_at[oid]]
    through = isinstance(oev, TradeEvent) and ((o.side == "SELL" and float(oev.price) > o.price + 1e-9) or (o.side == "BUY" and float(oev.price) < o.price - 1e-9))
    pattern["old fills at a print strictly THROUGH the price"] += through
    pattern["both"] += at_price and gone and through
print("PATTERN over", len(targets), "early Binance orders:", json.dumps(pattern, indent=1))
