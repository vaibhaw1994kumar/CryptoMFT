"""Phase D, D7c (a run wrapper, offline, no network): the full comparison on a whole recorded slice.
Usage: d7c_full.py DAY MINUTES OUT_JSON
Three paths on the SAME recorded stream and the SAME probe orders (the orders the old paper simulator sent, injected at the same times):
  OLD     maker_sim.MakerDryRunSimulator, the C++ engine (the old paper path)
  REPLAY  the new path as the replay runs it: SimulatedVenueFeeder with the core session applying the books
  PAPER   the new path as paper trading wires it: a LivePaperSession(queue_simulation=...) built offline, the live service's own books receive each event, then LivePaperSession._on_event / _on_trade
          (feeder with apply_core=False, the live gate flag); only the network feed is replaced by the recording
Criterion 1: PAPER equals REPLAY order by order (acceptance, fills, times, prices, queue ahead at placement). Criterion 2: every difference of REPLAY against OLD is classified by a pattern, counted.
Also: the volume-limit check of the new rule, and markouts at 1, 5 and 30 s of the early Binance fills against the old engine's later fills of the same orders. EXPLORATORY; probe orders on a March slice."""
import bisect
import dataclasses
import json
import sys
import tempfile
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src")
from crypto_stat_arb.execution import CanonicalOrderRequest, OrderAction, OrderType, TimeInForce
from crypto_stat_arb.live_feed import LiveFeedSettings
from crypto_stat_arb.maker_sim import maker_simulator_from_config
from crypto_stat_arb.models import Side, TradeEvent
from crypto_stat_arb.paper_live import LivePaperSession
from crypto_stat_arb.session import ExecutionMode, TradingSession
from crypto_stat_arb.simulation.queue_adapter import SimulatedVenueFeeder, build_queue_simulation
from new_backtest.event_loader import BOOK, DELTA, delta_fields, iter_venue_events, snapshot_levels
from new_backtest.order_book import OrderBook as FloatBook
from new_backtest.profiles import apply_profile, load_profile
from new_backtest.session_replay import SessionReplay

day, minutes, out_path = sys.argv[1], float(sys.argv[2]), Path(sys.argv[3])
SLICES = "/Users/vaibhav/Documents/Newproject/out/queue_v2/slices_mar2024"
H = (1.0, 5.0, 30.0)
profile = load_profile("perp_perp_maker_taker_v1_5")
bt_cfg = apply_profile(profile, dataset="mar2024", data_root=SLICES, pair="SOLUSDT", start_date=day, end_date=day, output_dir="/tmp/x_d7f")
bt_cfg.arb.max_position_base = 1.0
rep = SessionReplay(bt_cfg, profile)
cfg = rep.app_config
cfg.strategies["maker_simulation"] = {"enabled": True, "quote_size": "1.0", "max_inventory": "2.0", "engine": "auto", "quote_interval_ms": 1000, "depth": 50}
t0 = time.time()
events, first = [], None
for ev in rep.feed.convert(rep._timed_stream()):
    first = first or ev.receive_ts_ns
    if ev.receive_ts_ns - first > minutes * 60 * 1e9:
        break
    events.append(ev)
print(f"stream {len(events)} events in {time.time() - t0:.0f}s", flush=True)


class Trace:
    """Queue ahead per order, stored only where it changes; None once the order is no longer live."""

    def __init__(self):
        self.d, self.live = {}, set()

    def step(self, n, current: dict):
        for oid, qa in current.items():
            ns, qs = self.d.setdefault(oid, ([], []))
            if not qs or qs[-1] != qa:
                ns.append(n)
                qs.append(qa)
        for oid in self.live - set(current):
            ns, qs = self.d[oid]
            ns.append(n)
            qs.append(None)
        self.live = set(current)

    def at(self, oid, n):
        ns, qs = self.d.get(oid, ([], []))
        i = bisect.bisect_right(ns, n) - 1
        return qs[i] if i >= 0 else None


# ---------------- OLD ----------------
sim = maker_simulator_from_config(cfg)
cancels = []
orig_cancel = sim._on_cancel_applied
sim._on_cancel_applied = lambda order, now: (cancels.append((order.client_order_id, now, order.state)), orig_cancel(order, now))[1]
books = {v: cfg.book_sanity.new_book(v, cfg.instrument_id) for v in cfg.venues}
DUST = 1e-9           # a fill below this is float residue, not a fill: the retired engine emits fills of about 1e-15 SOL when a print exhausts the queue ahead exactly (found in the D7c trace sample, owner 8 Nov)
tr_old, old_fill_at, seen = Trace(), {}, 0
t1 = time.time()
for n, ev in enumerate(events):
    if isinstance(ev, TradeEvent):
        sim.on_trade(ev)
    else:
        books[ev.venue].apply(ev)
        sim.on_book(ev.venue, books[ev.venue], ev.receive_ts_ns, ev.event_id)
    if len(sim.fills) > seen:
        for f in sim.fills[seen:]:
            if f["quantity"] >= DUST:
                old_fill_at.setdefault(f["client_order_id"], n)
        seen = len(sim.fills)
    tr_old.step(n, {oid: st.engine.queue_ahead(oid) for st in sim.venues.values() for oid, o in st.active.items() if o.state == "RESTING"})
sim.finish(events[-1].receive_ts_ns)
print(f"OLD ({sim.engine}): orders {len(sim.orders)}, fills {len(sim.fills)} in {time.time() - t1:.0f}s", flush=True)
inj = sorted([(o.arrival_ts_ns, 0, "arrive", o) for o in sim.orders.values()] + [(ts, 1, "cancel", sim.orders[oid]) for oid, ts, st in cancels if st == "RESTING"], key=lambda r: (r[0], r[1]))


def request(action, o, ts):
    return CanonicalOrderRequest.create(
        action=action, order_id=o.client_order_id, target_order_id=o.client_order_id if action == OrderAction.CANCEL else None, parent_opportunity_id="d7c", parent_order_id="d7c",
        instrument_id=cfg.instrument_id, venue=o.venue, side=Side.BUY if o.side == "BUY" else Side.SELL, price=Decimal(repr(o.price)), quantity=Decimal(repr(o.size)),
        order_type=OrderType.LIMIT, time_in_force=TimeInForce.GTC, post_only=True, created_ts_ns=ts)


def run_new(session, adapters, executor, deliver_event, deliver_trade, label):
    fills, acks, reasons, qa_place, fill_at = [], {}, {}, {}, {}
    tr = Trace()
    causes, cur = {}, [0]
    orig_fill_from = executor._fill_from

    def _fill_from_logged(order, *a, **k):
        f = orig_fill_from(order, *a, **k)
        if f is not None:
            causes[(order.order_id, cur[0])] = f.cause
        return f
    executor._fill_from = _fill_from_logged
    session.router._deliver_resting_fills = lambda venue, produced, now_ns, exchange_ts_ns=None: fills.extend(
        {"order_id": rq.order_id, "ts_ns": now_ns, "qty": float(cb.last_fill_quantity), "price": float(cb.average_price)} for rq, cb in produced) or len(produced)
    i, t = 0, time.time()
    for n, ev in enumerate(events):
        cur[0] = n
        while i < len(inj) and inj[i][0] <= ev.receive_ts_ns:
            ts, _, kind, o = inj[i]
            i += 1
            book = session.books[(o.venue, cfg.instrument_id)]
            cbs = adapters[o.venue].handle_at(request(OrderAction.SUBMIT if kind == "arrive" else OrderAction.CANCEL, o, ts), book, ts)
            if kind == "arrive":
                acks[o.client_order_id], reasons[o.client_order_id] = cbs[0].event_type.value, cbs[0].reason
                mid = adapters[o.venue]._maker_id.get(o.client_order_id)
                if mid is not None:
                    qa_place[o.client_order_id] = executor.orders[mid].queue_ahead
        before = len(fills)
        (deliver_trade if isinstance(ev, TradeEvent) else deliver_event)(ev)
        for f in fills[before:]:
            fill_at.setdefault(f["order_id"], n)
        live = {}
        for venue, a in adapters.items():
            for mo in executor.live_orders(venue):
                rq = a._order.get(mo.order_id)
                if rq is not None:
                    live[rq.order_id] = mo.queue_ahead
        tr.step(n, live)
    print(f"{label}: acks {len(acks)}, fills {len(fills)} in {time.time() - t:.0f}s", flush=True)
    return {"fills": fills, "acks": acks, "reasons": reasons, "qa_place": qa_place, "fill_at": fill_at, "trace": tr, "causes": causes, "maker_id": {oid: a._maker_id.get(oid) for a in adapters.values() for oid in list(a._maker_id)}}


# ---------------- REPLAY wiring ----------------
adapters, views, states, executor = build_queue_simulation(cfg)
sess = TradingSession(cfg, execution_mode=ExecutionMode.INTERNAL_SIMULATION, adapters=adapters, strategies=[])
feeder = SimulatedVenueFeeder(sess, {v: v for v in cfg.venues}, views, states, adapters, ets_of=lambda ns: None)
R = run_new(sess, adapters, executor, lambda ev: feeder.on_book(ev), lambda ev: feeder.on_trade(ev), "REPLAY")

# ---------------- PAPER wiring (LivePaperSession built offline) ----------------
clock = {"now": 0}
with tempfile.TemporaryDirectory() as tmp:
    cfg_file = Path(tmp) / "cfg.json"
    cfg_file.write_text(json.dumps(rep.raw_config))
    paper = LivePaperSession(cfg_file, Path(tmp) / "out", "d7c-paper", LiveFeedSettings(udp_enabled=False, binance_market="linear", bybit_market="linear", durable=False), queue_simulation={},
                             clock_ns=lambda: clock["now"], tick_seconds=3600)
    # the paper session builds its own simulator; the strategies list is emptied so the probes are the only orders (the live service's gate and books are used as they are)
    paper.session.strategies.clear()
    svc_books = paper.service.books

    def paper_book(ev):
        clock["now"] = ev.receive_ts_ns
        book = svc_books[ev.venue]
        book.apply(ev)                                       # what the live service does before it calls the session's listener
        paper.service.book_gate.allow(tuple(svc_books.values()), ev.receive_ts_ns)      # and its gate, here with the replay's age rule (any message, 500 ms); the live service measures the liveness of its own book connection instead (owner 8 Nov, Q-P2: a market-data-source difference)
        paper._on_event(ev, book, book.state, True)

    def paper_trade(ev):
        clock["now"] = ev.receive_ts_ns
        paper._on_trade(ev)

    # paper.session.books are the service's books (LivePaperSession passes them in)
    P = run_new(paper.session, paper.session.router.adapters, paper.queue_executor, paper_book, paper_trade, "PAPER")
    paper._timestamps.close()

# ---------------- criterion 1: PAPER == REPLAY ----------------
def sig(path, oid):
    return [(f["ts_ns"], round(f["qty"], 9), round(f["price"], 9)) for f in path["fills"] if f["order_id"] == oid]


mism = {"acceptance": 0, "reason": 0, "fills": 0, "queue_ahead_at_placement": 0}
for oid in sim.orders:
    mism["acceptance"] += R["acks"].get(oid) != P["acks"].get(oid)
    mism["reason"] += R["reasons"].get(oid) != P["reasons"].get(oid)
    mism["fills"] += sig(R, oid) != sig(P, oid)
    a, b = R["qa_place"].get(oid), P["qa_place"].get(oid)
    mism["queue_ahead_at_placement"] += (a is None) != (b is None) or (a is not None and abs(a - b) > 1e-9)
examples = []
for oid in sim.orders:
    if (R["acks"].get(oid), R["reasons"].get(oid)) != (P["acks"].get(oid), P["reasons"].get(oid)) or sig(R, oid) != sig(P, oid):
        o = sim.orders[oid]
        examples.append({"order": oid, "venue": o.venue, "side": o.side, "price": o.price, "arrival_ns": o.arrival_ts_ns, "replay": (R["acks"].get(oid), R["reasons"].get(oid), sig(R, oid)[:2]), "paper": (P["acks"].get(oid), P["reasons"].get(oid), sig(P, oid)[:2])})
criterion1 = {"orders": len(sim.orders), "replay_fill_rows": len(R["fills"]), "paper_fill_rows": len(P["fills"]), "orders_that_differ": mism, "examples": examples[:12]}

# ---------------- criterion 2: REPLAY against OLD ----------------
old_by, new_by = {}, {}
dust = {"old_engine_fills_below_1e-9": Counter(), "old_engine_orders_whose_only_fills_are_dust": 0, "new_path_fills_below_1e-9": 0}
for f in sim.fills:
    if f["quantity"] < DUST:
        dust["old_engine_fills_below_1e-9"][sim.orders[f["client_order_id"]].venue] += 1
        continue
    old_by.setdefault(f["client_order_id"], []).append(f)
dust["old_engine_orders_whose_only_fills_are_dust"] = len({f["client_order_id"] for f in sim.fills if f["quantity"] < DUST} - set(old_by))
new_dust_fills = []
for f in R["fills"]:
    if f["qty"] < DUST:
        dust["new_path_fills_below_1e-9"] += 1
        new_dust_fills.append({"venue": sim.orders[f["order_id"]].venue, "order": f["order_id"], "qty": f["qty"], "order_size": sim.orders[f["order_id"]].size})
        continue
    new_by.setdefault(f["order_id"], []).append(f)
# orders the new path leaves with a remainder below the dust limit (filled all but a sliver), REPLAY path; the paper path equals it order by order (criterion 1)
new_left_open = []
filled_by_order = {}
for f in R["fills"]:
    filled_by_order[f["order_id"]] = filled_by_order.get(f["order_id"], 0.0) + f["qty"]
for oid, filled in filled_by_order.items():
    rem = sim.orders[oid].size - filled
    if 0.0 < rem < DUST and filled > 0:
        new_left_open.append({"venue": sim.orders[oid].venue, "order": oid, "size": sim.orders[oid].size, "filled": filled, "remaining": rem})


def kind_of(ev):
    return "TRADE" if isinstance(ev, TradeEvent) else "BOOK"


def level_shown(n, o):
    """Is the order's price level shown by the last book event of its venue before event n (a one-level book: only the best)?"""
    for k in range(n - 1, -1, -1):
        e = events[k]
        if e.venue == o.venue and not isinstance(e, TradeEvent):
            side = e.asks if o.side == "SELL" else e.bids
            return any(abs(float(l.price) - o.price) < 1e-9 for l in side) if side else None
    return None


def through(ev, o):
    return isinstance(ev, TradeEvent) and ((o.side == "SELL" and float(ev.price) > o.price + 1e-9) or (o.side == "BUY" and float(ev.price) < o.price - 1e-9))


def parting(oid):
    ns = sorted(set(tr_old.d.get(oid, ([], []))[0]) | set(R["trace"].d.get(oid, ([], []))[0]))
    for n in ns:
        a, b = tr_old.at(oid, n), R["trace"].at(oid, n)
        if a is not None and b is not None and abs(a - b) > 1e-6:
            return n
    return None


both = [oid for oid in sim.orders if oid in old_by and oid in new_by]
cls = {"early_binance": Counter(), "later_bybit": Counter(), "early_bybit": Counter(), "later_binance": Counter()}
early_binance = []
unref_A = []          # later Bybit fills where the old engine does not fill at a print through the price (no written reference)
vol_limit = {"checked": 0, "violations": 0, "detail": []}
for oid in both:
    o = sim.orders[oid]
    n_old, n_new = old_fill_at[oid], R["fill_at"][oid]
    dt = (new_by[oid][0]["ts_ns"] - old_by[oid][0]["match_ts_ns"]) / 1e6
    if abs(dt) <= 1:
        continue
    key = ("early_" if dt < 0 else "later_") + o.venue.lower()
    pn = parting(oid)
    ev_new, ev_old = events[n_new], events[n_old]
    pat = (f"new fills at {kind_of(ev_new)}" + (" AT price" if isinstance(ev_new, TradeEvent) and abs(float(ev_new.price) - o.price) < 1e-9 else " (not at price)" if isinstance(ev_new, TradeEvent) else ""),
           "level shown in last book" if level_shown(n_new, o) else "level NOT shown in last book" if level_shown(n_new, o) is False else "no book yet",
           "queues part at " + (kind_of(events[pn]) if pn is not None else "never before the fill") if pn is not None else "queues equal until the fill",
           f"old fills at {kind_of(ev_old)}" + (" through price" if through(ev_old, o) else " at/other"))
    cls[key][pat] += 1
    if key == "later_bybit" and not through(ev_old, o):
        unref_A.append(oid)
    if key == "early_binance":
        early_binance.append(oid)
# volume-limit check of the new rule on every new fill that happens at a print: the fills of one order at one receive time (Bybit batches several prints under one stamp) <= the volume of the reaching prints
# of that stamp left after the queue ahead the order had before the first of them
prints_at = {}
for k, e in enumerate(events):
    if isinstance(e, TradeEvent):
        prints_at.setdefault((e.venue, e.receive_ts_ns), []).append(k)
for oid, rows in new_by.items():
    n = R["fill_at"].get(oid)
    ev = events[n] if n is not None else None
    if ev is None or not isinstance(ev, TradeEvent):
        continue
    o_ = sim.orders[oid]
    group = [k for k in prints_at[(ev.venue, ev.receive_ts_ns)]
             if (o_.side == "BUY" and events[k].aggressor_side == "SELL" and float(events[k].price) <= o_.price + 1e-12)
             or (o_.side == "SELL" and events[k].aggressor_side == "BUY" and float(events[k].price) >= o_.price - 1e-12)]
    qa_before = R["trace"].at(oid, min(group) - 1) if group else None
    if qa_before is None:
        qa_before = R["qa_place"].get(oid, 0.0)
    first = [f for f in rows if f["ts_ns"] == ev.receive_ts_ns]
    if first:
        vol_limit["checked"] += 1
        group_vol = sum(float(events[k].quantity) for k in group)
        if sum(f["qty"] for f in first) > max(0.0, group_vol - qa_before) + 1e-9:
            vol_limit["violations"] += 1
            vol_limit["detail"].append({"venue": o_.venue, "fill_qty": sum(f["qty"] for f in first), "prints_in_the_stamp": len(group), "reaching_volume_in_the_stamp": group_vol, "queue_ahead_before": qa_before,
                                        "queue_ahead_at_placement": R["qa_place"].get(oid), "print_price": float(ev.price), "order_price": o_.price})

reasons_rej = Counter()
for oid, o in sim.orders.items():
    ack_old = "REJECTED" if o.state == "REJECTED" else "ACKNOWLEDGED"
    if ack_old != R["acks"].get(oid):
        reasons_rej[(o.venue, ack_old, R["acks"].get(oid), R["reasons"].get(oid))] += 1
only_new = Counter(sim.orders[oid].venue for oid in new_by if oid not in old_by)
only_old = Counter(sim.orders[oid].venue for oid in old_by if oid not in new_by)
qty_diff = [oid for oid in both if abs(sum(f["qty"] for f in new_by[oid]) - sum(f["quantity"] for f in old_by[oid])) > 1e-9]
qshare = {}
for oid, qa in R["qa_place"].items():
    o = sim.orders[oid]
    d = qshare.setdefault(o.venue, {"acknowledged_orders": 0, "queue_ahead_gt_0": 0, "queue_ahead_ge_order_size": 0, "queue_ahead_ge_10x_order_size": 0, "queue_ahead_values": []})
    d["acknowledged_orders"] += 1
    d["queue_ahead_gt_0"] += qa > 1e-12
    d["queue_ahead_ge_order_size"] += qa >= o.size - 1e-12
    d["queue_ahead_ge_10x_order_size"] += qa >= 10 * o.size - 1e-12
    d["queue_ahead_values"].append(qa)
for d in qshare.values():
    v = sorted(d.pop("queue_ahead_values"))
    d["median_queue_ahead"] = v[len(v) // 2] if v else None
    d["share_behind_a_real_queue_gt_0"] = round(d["queue_ahead_gt_0"] / max(d["acknowledged_orders"], 1), 4)
    d["share_behind_at_least_one_order_size"] = round(d["queue_ahead_ge_order_size"] / max(d["acknowledged_orders"], 1), 4)
only_new_pat, only_old_pat, qty_pat = Counter(), Counter(), Counter()
for oid in new_by:
    if oid in old_by:
        continue
    o, n = sim.orders[oid], R["fill_at"][oid]
    ev = events[n]
    pn = parting(oid)
    only_new_pat[(o.venue, "old engine: " + ("order not acknowledged" if o.state == "REJECTED" else "order live, never filled"),
                  f"new fills at {kind_of(ev)}" + (" AT price" if isinstance(ev, TradeEvent) and abs(float(ev.price) - o.price) < 1e-9 else " (not at price)" if isinstance(ev, TradeEvent) else ""),
                  "level shown in last book" if level_shown(n, o) else "level NOT shown in last book" if level_shown(n, o) is False else "no book yet",
                  "queues part at " + (kind_of(events[pn]) if pn is not None else "never before the fill"))] += 1
for oid in old_by:
    if oid in new_by:
        continue
    o, n = sim.orders[oid], old_fill_at[oid]
    ev = events[n]
    qa_new = R["trace"].at(oid, n)
    only_old_pat[(o.venue, "new path: " + ("order not acknowledged (stale book at arrival)" if R["acks"].get(oid) == "REJECTED" else "order live, never filled"),
                  "old fills at " + (kind_of(ev) + (" through price" if through(ev, o) else " AT/other")),
                  "new queue ahead at that event " + ("unknown (order no longer live)" if qa_new is None else "> 0" if qa_new > 1e-9 else "= 0"))] += 1
for oid in qty_diff:
    a, b = sum(f["qty"] for f in new_by[oid]), sum(f["quantity"] for f in old_by[oid])
    qty_pat[(sim.orders[oid].venue, "new fills more" if a > b else "new fills less", f"{len(new_by[oid])} new rows / {len(old_by[oid])} old rows")] += 1

# ---------------- markouts of the early Binance fills ----------------
def mids_at(venue, times):
    book = FloatBook(venue, "SOLUSDT", "hybrid", message_atomic=True)
    want = sorted(set(times))
    out, i = {}, 0
    for ev in iter_venue_events(SLICES, venue, "SOLUSDT", day, day, include_trades=False):
        while i < len(want) and ev.exchange_ts > want[i]:
            out[want[i]] = book.mid if book.ready else None
            i += 1
        if i >= len(want):
            break
        if ev.kind == BOOK:
            bids, asks = snapshot_levels(ev.payload)
            if bids and asks:
                book.apply_snapshot(bids, asks, ev.exchange_ts)
        elif ev.kind == DELTA:
            side, price, vol, et = delta_fields(ev.payload)
            if price > 0:
                book.apply_delta(side, price, vol, et, ev.exchange_ts)
                book.end_message()
    return out


def markouts(rows_of, ts_key, qty_key, price_key, oids, venue="binance"):
    """Quantity-weighted markout (bps, and USDT) at each horizon over the first fill of each order, against the Binance mid at fill time + h."""
    points = []
    for oid in oids:
        f = rows_of[oid][0]
        points.append((oid, f[ts_key] / 1e9, f[qty_key], f[price_key], sim.orders[oid].side))
    mids = mids_at(venue, [t + h for _, t, _, _, _ in points for h in H])
    out = {}
    for h in H:
        num = den = usdt = 0.0
        n = 0
        for _, t, q, p, side in points:
            m = mids.get(t + h)
            if m:
                sign = 1.0 if side == "BUY" else -1.0
                bps = sign * (m - p) / p * 1e4
                num += bps * q
                den += q
                usdt += bps * q * p / 1e4
                n += 1
        out[f"{h:g}s"] = {"orders": n, "mean_bps_qty_weighted": num / den if den else None, "usdt_total": usdt}
    return out


mk = None
if early_binance:
    mk = {"orders": len(early_binance),
          "new_rule_early_fills": markouts(new_by, "ts_ns", "qty", "price", early_binance),
          "old_engine_later_fills_of_the_same_orders": markouts(old_by, "match_ts_ns", "quantity", "price", early_binance),
          "approximation": "the venue's own mid by exchange time, a replay of its book events; the run's own receive-time markouts are not available per fill"}

# ---------------- the residual with no written reference (owner 8 Nov: trace up to 20, label the rest, markouts at 1/5/30 s) ----------------
unref_B = [oid for oid in old_by if oid not in new_by and sim.orders[oid].venue == "BYBIT" and R["acks"].get(oid) != "REJECTED"]
unref_C = list(qty_diff)
t_first = events[0].receive_ts_ns


def window(oid, n):
    """The last 4 events of the order's venue up to event n and the next one, with each engine's queue ahead after it."""
    o = sim.orders[oid]
    idx = [k for k in range(max(0, n - 40), min(len(events), n + 40)) if events[k].venue == o.venue]
    before = [k for k in idx if k <= n][-4:]
    after = [k for k in idx if k > n][:1]
    rows = []
    for k in before + after:
        e = events[k]
        r = {"n": k, "kind": kind_of(e), "ms_after_order_arrival": round((e.receive_ts_ns - o.arrival_ts_ns) / 1e6, 3), "queue_ahead_old": tr_old.at(oid, k), "queue_ahead_new": R["trace"].at(oid, k)}
        if isinstance(e, TradeEvent):
            r.update(price=float(e.price), qty=float(e.quantity), aggressor=e.aggressor_side)
        rows.append(r)
    return rows


def sample_of(group, label, take):
    out = []
    for oid in sorted(group)[:take]:
        o = sim.orders[oid]
        item = {"group": label, "venue": o.venue, "side": o.side, "price": o.price, "size": o.size,
                "old_fills": [(round((f["match_ts_ns"] - o.arrival_ts_ns) / 1e6, 3), f["quantity"], f["price"]) for f in old_by.get(oid, [])],
                "new_fills": [(round((f["ts_ns"] - o.arrival_ts_ns) / 1e6, 3), f["qty"], f["price"]) for f in new_by.get(oid, [])]}
        if oid in old_fill_at:
            item["around_the_old_fill"] = window(oid, old_fill_at[oid])
        if oid in R["fill_at"]:
            item["around_the_new_fill"] = window(oid, R["fill_at"][oid])
        out.append(item)
    return out


def cause_of(oid, group):
    """The first mechanism the trace shows for an order with no written reference: where its two queues first part, or the through-print rule."""
    o = sim.orders[oid]
    pn = parting(oid)
    if group == "C" and oid in old_fill_at and through(events[old_fill_at[oid]], o):
        return "THROUGH_PRINT_OFF (the old engine fills at a print through the price)"
    if pn is not None:
        return "CANCEL_MODEL (queues first part at a BOOK event)" if kind_of(events[pn]) == "BOOK" else "PRINT_NETTING (queues first part at a print)"
    return "UNEXPLAINED_VS_RETIRED_ENGINE"


def money(rows_of, ts_key, qty_key, oids):
    return markouts(rows_of, ts_key, qty_key, "price", oids, venue="bybit") if oids else None


causes_by_group = {}
for g, grp in (("A", unref_A), ("B", unref_B), ("C", unref_C)):
    c = Counter(cause_of(oid, g) for oid in grp)
    causes_by_group[g] = dict(c)
unexplained = {g: [oid for oid in grp if cause_of(oid, g) == "UNEXPLAINED_VS_RETIRED_ENGINE"] for g, grp in (("A", unref_A), ("B", unref_B), ("C", unref_C))}
unref = {"causes_by_group": causes_by_group,
         "UNEXPLAINED_VS_RETIRED_ENGINE": {g: {"orders": len(v), "venue": "BYBIT", "new_fills_markouts": money(new_by, "ts_ns", "qty", [o for o in v if o in new_by]), "old_fills_markouts": money(old_by, "match_ts_ns", "quantity", [o for o in v if o in old_by])} for g, v in unexplained.items()},
         "A_later_bybit_old_not_through": {"orders": len(unref_A), "new_fills_markouts": money(new_by, "ts_ns", "qty", unref_A), "old_fills_markouts": money(old_by, "match_ts_ns", "quantity", unref_A)},
         "B_only_old_bybit": {"orders": len(unref_B), "old_fills_markouts_the_fills_the_new_path_does_not_have": money(old_by, "match_ts_ns", "quantity", unref_B)},
         "C_quantity_differs_bybit": {"orders": len(unref_C), "new_fills_markouts": money(new_by, "ts_ns", "qty", unref_C), "old_fills_markouts": money(old_by, "match_ts_ns", "quantity", unref_C),
                                      "extra_quantity_new_minus_old": sum(sum(f["qty"] for f in new_by[oid]) - sum(f["quantity"] for f in old_by[oid]) for oid in unref_C)},
         "sample": sample_of(unref_A, "A", 4) + sample_of(unref_B, "B", 3) + sample_of(unref_C, "C", 3)}

summary = {"label": "EXPLORATORY: D7c, probe orders of the old paper simulator on a recorded March slice; not a strategy result", "day": day, "minutes": minutes, "events": len(events), "engine_old": sim.engine,
           "orders": len(sim.orders), "criterion_1_paper_equals_replay": criterion1,
           "old_filled_orders": len(old_by), "replay_filled_orders": len(new_by), "filled_by_both": len(both), "only_new_by_venue": dict(only_new), "only_old_by_venue": dict(only_old),
           "quantity_differs_on": len(qty_diff), "old_qty": sum(f["quantity"] for f in sim.fills if f["quantity"] >= DUST), "dust_fills": {"old_engine_fills_below_1e-9": dict(dust["old_engine_fills_below_1e-9"]), "old_engine_orders_whose_only_fills_are_dust": dust["old_engine_orders_whose_only_fills_are_dust"], "new_path_fills_below_1e-9": dust["new_path_fills_below_1e-9"], "new_path_fills_below_1e-9_by_venue": dict(Counter(x["venue"] for x in new_dust_fills)), "new_path_fills_below_1e-9_list": new_dust_fills[:50],
                                                                                                                                  "new_path_orders_left_open_with_remaining_below_1e-9_by_venue": dict(Counter(x["venue"] for x in new_left_open)), "new_path_orders_left_open_with_remaining_below_1e-9_list": new_left_open[:50]}, "replay_qty": sum(f["qty"] for f in R["fills"]),
           "acceptance_differences": {str(k): v for k, v in reasons_rej.items()}, "volume_limit_check_of_the_new_rule": vol_limit,
           "queue_ahead_at_placement_by_venue_replay_path": qshare, "only_new_patterns": {" | ".join(p): c for p, c in only_new_pat.most_common()}, "only_old_patterns": {" | ".join(p): c for p, c in only_old_pat.most_common()}, "quantity_difference_patterns": {" | ".join(p): c for p, c in qty_pat.most_common()},
           "patterns": {k: {" | ".join(p): c for p, c in v.most_common()} for k, v in cls.items()}, "markouts_early_binance": mk, "unreferenced": unref}
out_path.write_text(json.dumps(summary, indent=1, default=str))
print(json.dumps(summary, indent=1, default=str))
