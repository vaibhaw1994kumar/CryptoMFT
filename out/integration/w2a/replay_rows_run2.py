"""Read-only: the REPLAY wiring of offline_strategy_check.py (gate_first) on the d7d-run2 recording, 60 min, dumping every ledger row, to diff against the live ledger (860 vs 837)."""
import json, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src")
from crypto_stat_arb.config import load_config
from crypto_stat_arb.models import TradeEvent
from crypto_stat_arb.derivatives import DerivativeTicker
from crypto_stat_arb.session import ExecutionMode, TradingSession
from crypto_stat_arb.simulation.queue_adapter import SimulatedVenueFeeder, build_queue_simulation
from new_backtest.session_replay import live_paper_setup, load_keepalive_notes, iter_recorded_events
session_dir = Path("/Users/vaibhav/Documents/Newproject/out/integration/paper_live/run2/d7d-run2")
CONFIG = Path("/Users/vaibhav/Documents/Newproject/out/integration/paper_live/config_v1_5.json")
profile = sys.argv[1] if len(sys.argv) > 1 else "perp_perp_maker_taker_v1_5"
out = Path(sys.argv[2])
notes = load_keepalive_notes(session_dir)
setup = live_paper_setup(profile)
cfg, _ = load_config(CONFIG)
adapters, views, states, executor = build_queue_simulation(cfg, setup["queue_simulation"])
sess = TradingSession(cfg, execution_mode=ExecutionMode.INTERNAL_SIMULATION, adapters=adapters, strategies=[], **setup["session_kwargs"])
feeder = SimulatedVenueFeeder(sess, {v: v for v in cfg.venues}, views, states, adapters, ets_of=lambda ns: None)
sess.strategies[:] = setup["strategy_builder"](SimpleNamespace(session=sess, cfg=cfg, _states=states, _views=views))
sess.strategy = sess.strategies[0]
ni = 0; n = 0; last = None
for ev in iter_recorded_events(session_dir):
    while ni < len(notes) and notes[ni][0] <= ev.receive_ts_ns:
        sess.note_venue_message(notes[ni][1], notes[ni][0]); ni += 1
    if isinstance(ev, DerivativeTicker):
        continue                      # the 9 Nov check had no derivative events in the stream; the same here
    if isinstance(ev, TradeEvent):
        feeder.on_trade(ev)
    else:
        sess.books[(ev.venue, ev.instrument_id)].apply(ev)
        sess.book_gate.allow(tuple(sess.books[(v, sess.cfg.instrument_id)] for v in sess.cfg.venues), ev.receive_ts_ns)
        feeder.on_book(ev, apply_core=False, gate_allowed=not sess.book_gate.suspended)
    n += 1; last = ev.receive_ts_ns
with out.open("w") as fh:
    for e in sess.ledger.events:
        fh.write(json.dumps(e, default=str, sort_keys=True) + "\n")
print(json.dumps({"profile": profile, "events": n, "ledger_rows": len(sess.ledger.events), "last_ns": last}))
