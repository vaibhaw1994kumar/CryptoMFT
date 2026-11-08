"""Phase D, D7d, offline step (owner 9 Nov; a run wrapper, no network, no orders): the v1_5 session strategy through BOTH wirings on a recorded live session, order by order.
  REPLAY  a `TradingSession` (internal simulation) with the strategy of `live_paper_setup` and the shared `SimulatedVenueFeeder`, as the session replay runs it
  PAPER   a `LivePaperSession` built offline with the same setup (strategy, queue-simulator settings, session rules), fed through its own `_on_event` / `_on_trade`, the live service's books and gate
The recorded books, trades and the book connection's pong frames (as venue-message notes) are the SAME for both; the clock is each event's receive time. Compared: the ledger's events (type, time, venue, side, price, quantity),
the final positions and cash, the OMS plans and their orders. Also reported: the strategy's own counters (evaluations, signals, the best net edge it saw, the reject reasons) against its entry threshold, which is the
answer to "why no orders" when there are none.
Usage: offline_strategy_check.py SESSION_DIR MINUTES OUT_JSON [PROFILE]"""
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src")
from crypto_stat_arb.config import load_config
from crypto_stat_arb.live_feed import LiveFeedSettings
from crypto_stat_arb.models import TradeEvent
from crypto_stat_arb.paper_live import LivePaperSession
from crypto_stat_arb.session import ExecutionMode, TradingSession
from crypto_stat_arb.simulation.queue_adapter import SimulatedVenueFeeder, build_queue_simulation
from new_backtest.session_replay import live_paper_setup, load_keepalive_notes, load_recorded_events

session_dir, minutes, out_path = Path(sys.argv[1]), float(sys.argv[2]), Path(sys.argv[3])
profile = sys.argv[4] if len(sys.argv) > 4 else "perp_perp_maker_taker_v1_5"
CONFIG = Path("/Users/vaibhav/Documents/Newproject/out/integration/paper_live/config_v1_5.json")

events_all = load_recorded_events(session_dir)
t_first = events_all[0].receive_ts_ns
events = [e for e in events_all if e.receive_ts_ns - t_first <= minutes * 60e9]
notes = [n for n in load_keepalive_notes(session_dir) if n[0] - t_first <= minutes * 60e9]
print(f"{len(events)} events, {len(notes)} keepalive notes", flush=True)


def drive(deliver_event, deliver_trade, note_cb):
    ni = 0
    for ev in events:
        while ni < len(notes) and notes[ni][0] <= ev.receive_ts_ns:
            note_cb(notes[ni][1], notes[ni][0])
            ni += 1
        (deliver_trade if isinstance(ev, TradeEvent) else deliver_event)(ev)


def snapshot(session, strategy):
    ledger = session.ledger
    rows = []
    for e in ledger.events:
        p = e if isinstance(e, dict) else dict(e)
        rows.append((p.get("event_type"), p.get("ts_ns"), p.get("venue"), p.get("side"), str(p.get("price", p.get("average_price", ""))), str(p.get("quantity", p.get("filled_quantity", p.get("last_fill_quantity", ""))))))
    rows.sort(key=lambda r: tuple("" if x is None else str(x) for x in r))      # events stamped with the same nanosecond are compared as a set (the two wirings may order them differently)
    plans = sorted((str(pl.state.value), len(pl.children)) for pl in session.oms.plans.values())
    inner = strategy.strategy
    return {"ledger_events": rows, "ledger_event_count": len(rows), "positions": {f"{k[0]}|{k[1]}": str(v) for k, v in sorted(ledger.positions.items())},
            "cash": {k: str(v) for k, v in sorted(ledger.cash_deltas.items())}, "plans": plans, "orders_in_plans": sum(n for _, n in plans),
            "strategy": {"evaluations": inner.evaluations, "signals": inner.signals, "best_edge_seen_bps": None if inner.best_edge_seen_bps == float("-inf") else inner.best_edge_seen_bps,
                         "rejects": {k: v for k, v in inner.rejects.items() if v}, "entry_threshold_min_net_edge_bps": inner.config.min_net_edge_bps, "safety_buffer_bps": inner.config.safety_buffer_bps}}


# ---------------- REPLAY ----------------
setup = live_paper_setup(profile)
cfg, _ = load_config(CONFIG)
adapters, views, states, executor = build_queue_simulation(cfg, setup["queue_simulation"])
sess = TradingSession(cfg, execution_mode=ExecutionMode.INTERNAL_SIMULATION, adapters=adapters, strategies=[], **setup["session_kwargs"])
feeder = SimulatedVenueFeeder(sess, {v: v for v in cfg.venues}, views, states, adapters, ets_of=lambda ns: None)
sess.strategies[:] = setup["strategy_builder"](SimpleNamespace(session=sess, cfg=cfg, _states=states, _views=views))
sess.strategy = sess.strategies[0]
headroom = {"bid": [], "ask": []}


def on_book_with_headroom(ev):
    """After every book event: how far the strategy's own required quote price (`_quote_limit`, the one place it is computed: fees, minimum edge, buffer, expected hedge move, slippage, funding, failure cost) is from a quote
    AT THE MAKER TOUCH: headroom in bps, positive = a quote at the touch would already clear the entry rule. The maker venue is the profile's (Bybit), the hedge venue the other."""
    feeder.on_book(ev)
    inner = sess.strategies[0].strategy
    mv, hv = "BYBIT", "BINANCE"
    mb, hb = views[mv], views[hv]
    if not (mb.ready and hb.ready) or ev.venue not in (mv, hv):
        return
    fees = inner.venues["bybit"].fees.maker_bps + inner.venues["binance"].fees.taker_bps
    if mb.best_bid > 0 and hb.best_bid > 0:
        px = inner._quote_limit("bid", hb.best_bid, fees)
        headroom["bid"].append((px - mb.best_bid) / mb.best_bid * 1e4)
    if mb.best_ask > 0 and hb.best_ask > 0:
        px = inner._quote_limit("ask", hb.best_ask, fees)
        headroom["ask"].append((mb.best_ask - px) / mb.best_ask * 1e4)


drive(on_book_with_headroom, lambda ev: feeder.on_trade(ev), lambda v, t: sess.note_venue_message(v, t))
replay = snapshot(sess, sess.strategies[0])
_dist = lambda v: {"n": len(v)} if not v else {"n": len(v), "p1": sorted(v)[int(len(v) * .01)], "p10": sorted(v)[int(len(v) * .1)], "p50": sorted(v)[len(v) // 2], "p90": sorted(v)[int(len(v) * .9)], "p99": sorted(v)[min(len(v) - 1, int(len(v) * .99))],
                                                  "max": max(v), "share_headroom_ge_0": sum(1 for x in v if x >= 0) / len(v)}
edge_diagnostics = {"headroom_bps_to_a_quote_at_the_maker_touch": {k: _dist(v) for k, v in headroom.items()}, "maker_venue": "BYBIT", "hedge_venue": "BINANCE",
                    "required_fee_bps_maker_plus_taker": None}


# ---------------- PAPER ----------------
setup2 = live_paper_setup(profile)
clock = {"now": 0}
with tempfile.TemporaryDirectory() as tmp:
    paper = LivePaperSession(CONFIG, Path(tmp) / "out", "offline-v15", LiveFeedSettings(udp_enabled=False, binance_market="linear", bybit_market="linear", durable=False),
                             queue_simulation=setup2["queue_simulation"], session_kwargs=setup2["session_kwargs"], strategy_builder=setup2["strategy_builder"], clock_ns=lambda: clock["now"], tick_seconds=3600)
    books = paper.service.books

    def paper_book(ev):
        clock["now"] = ev.receive_ts_ns
        book = books[ev.venue]
        book.apply(ev)                                                  # what the live service does before it calls the session's listener
        paper.service.book_gate.allow(tuple(books.values()), ev.receive_ts_ns)
        paper._on_event(ev, book, book.state, True)

    def paper_trade(ev):
        clock["now"] = ev.receive_ts_ns
        paper._on_trade(ev)

    def paper_note(v, t):
        clock["now"] = max(clock["now"], t)
        paper.session.note_venue_message(v, t)

    drive(paper_book, paper_trade, paper_note)
    pap = snapshot(paper.session, paper.session.strategies[0])
    paper.service.recorder.close({})
    paper.service.ohlc_writer.close()
    paper.service.ltp_writer.close()
print("PAPER done", pap["ledger_event_count"], "ledger events,", pap["strategy"]["signals"], "signals", flush=True)

same = {"ledger_events": replay["ledger_events"] == pap["ledger_events"], "positions": replay["positions"] == pap["positions"], "cash": replay["cash"] == pap["cash"], "plans": replay["plans"] == pap["plans"],
        "strategy_counters": replay["strategy"] == pap["strategy"]}
differ = [i for i, (a, b) in enumerate(zip(replay["ledger_events"], pap["ledger_events"])) if a != b][:10]
ledger_diff = {"event_type_counts_replay": dict(Counter(r[0] for r in replay["ledger_events"])), "event_type_counts_paper": dict(Counter(r[0] for r in pap["ledger_events"])),
               "only_in_replay_by_type": dict(Counter(r[0] for r in (Counter(map(tuple, replay["ledger_events"])) - Counter(map(tuple, pap["ledger_events"]))).elements())),
               "only_in_paper_by_type": dict(Counter(r[0] for r in (Counter(map(tuple, pap["ledger_events"])) - Counter(map(tuple, replay["ledger_events"]))).elements()))}
summary = {"label": "EXPLORATORY: D7d offline step, recorded Mac shakedown stream; not a strategy result", "session": session_dir.name, "minutes": minutes, "profile": profile, "events": len(events), "keepalive_notes": len(notes),
           "identical": same, "ledger_difference": ledger_diff, "ledger_events_replay_paper": [replay["ledger_event_count"], pap["ledger_event_count"]], "first_differing_ledger_rows": differ,
           "edge_diagnostics": edge_diagnostics, "replay": {k: v for k, v in replay.items() if k != "ledger_events"}, "paper": {k: v for k, v in pap.items() if k != "ledger_events"}}
out_path.write_text(json.dumps(summary, indent=1, default=str))
print(json.dumps({k: summary[k] for k in ("identical", "ledger_events_replay_paper", "first_differing_ledger_rows", "ledger_difference")}, indent=1))
print(json.dumps(replay["strategy"], indent=1))
print(json.dumps(edge_diagnostics, indent=1))
