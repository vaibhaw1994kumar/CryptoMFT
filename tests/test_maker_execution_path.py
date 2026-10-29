"""Maker simulation on the shared execution path.

Covers the canonical TradeEvent, the event clock, the queue engines (C++ and
its Python mirror), latency-driven arrival/cancel/report timing, replay
ordering, and live-vs-replay parity.
"""

import asyncio
from dataclasses import replace
from decimal import Decimal
import json
import os
from pathlib import Path
import random
import tempfile
import unittest

from crypto_stat_arb.canonical import (
    CallbackEventSink,
    CanonicalEventDispatcher,
    CanonicalEventRecorder,
    validate_canonical_event,
)
from crypto_stat_arb.config import load_config
from crypto_stat_arb.latency import LatencyDistribution, LatencyModel, VenueLatencyProfile
from crypto_stat_arb.live_feed import LiveFeedSettings
from crypto_stat_arb.maker_engine import (
    CppQueueEngine,
    PythonQueueEngine,
    load_cpp_module,
)
from crypto_stat_arb.maker_sim import (
    MakerDryRunSimulator,
    maker_latency_model,
    maker_simulator_from_config,
    replay_order,
    simulate_recorded_session,
)
from crypto_stat_arb.market_data_output import TradeTick, normalize_bybit_trades
from crypto_stat_arb.models import EventType, Level, MarketEvent, TradeEvent
from crypto_stat_arb.paper_live import LivePaperSession
from crypto_stat_arb.session import TradingSession
from crypto_stat_arb.sim_clock import SimulationClock
from test_live_feed import FakeConnector, FakeWebSocket, StepClock


ROOT = Path(__file__).parents[1]
RESEARCH_CONFIG = ROOT / "config" / "research.json"
WATCH_CONFIG = ROOT / "config" / "paper_watch.json"
INSTRUMENT = "BTC-USDT-SPOT"
MS = 1_000_000
T0 = 1_000_000_000_000
CPP = load_cpp_module()


def engines():
    yield PythonQueueEngine
    if CPP is not None:
        yield lambda: CppQueueEngine(CPP)


def book_event(venue, sequence, ts_ns, bids=(), asks=(), *, snapshot=False):
    return MarketEvent(
        event_id=f"{venue}-book-{sequence}",
        event_type=EventType.BOOK_SNAPSHOT if snapshot else EventType.BOOK_DELTA,
        venue=venue, instrument_id=INSTRUMENT,
        exchange_ts_ns=ts_ns, receive_ts_ns=ts_ns, source_ts_ns=ts_ns,
        sequence=sequence, sequence_start=sequence,
        raw_ref=f"raw-{sequence}", adapter_version="test",
        bids=tuple(Level(Decimal(p), Decimal(q)) for p, q in bids),
        asks=tuple(Level(Decimal(p), Decimal(q)) for p, q in asks),
    )


def trade_event(venue, index, ts_ns, price, quantity, aggressor):
    return TradeEvent(
        event_id=f"{venue}-trade-{index}", venue=venue, instrument_id=INSTRUMENT,
        trade_id=str(index), exchange_ts_ns=ts_ns, source_ts_ns=ts_ns, receive_ts_ns=ts_ns,
        price=Decimal(price), quantity=Decimal(quantity), aggressor_side=aggressor,
        raw_ref=f"raw-trade-{index}", adapter_version="test",
    )


def fixed(ns):
    return LatencyDistribution(base_ns=ns)


def fixed_latency(venue="BYBIT", *, entry=10 * MS, cancel=20 * MS, fill=2 * MS, ack=1 * MS):
    profile = VenueLatencyProfile(
        venue=venue, market_data=fixed(0), strategy=fixed(0), risk=fixed(0),
        entry=fixed(entry), submit_ack=fixed(ack), fill=fixed(fill), cancel=fixed(cancel),
    )
    return LatencyModel({venue: profile}, seed=1, source="test-fixed")


class TradeEventTests(unittest.TestCase):
    def test_legacy_trade_row_reads_as_canonical_trade(self):
        legacy = {
            "schema_version": "1.0.0", "event_id": "e1", "venue": "BYBIT",
            "instrument_id": INSTRUMENT, "symbol": "BTCUSDT", "trade_id": "abc",
            "exchange_ts_ns": 5, "source_ts_ns": 5, "receive_ts_ns": 9,
            "price": "60000.1", "quantity": "0.5", "aggressor_side": "Sell",
            "raw_ref": "r", "adapter_version": "bybit-public-trade-v1", "native": {"i": "abc"},
        }
        trade = TradeEvent.from_dict(legacy)
        self.assertEqual(trade.event_type, EventType.TRADE)
        self.assertEqual(trade.aggressor_side, "SELL")
        self.assertEqual(trade.price, Decimal("60000.1"))
        self.assertEqual(trade.arrival_ordinal, 0)
        validate_canonical_event(trade)
        self.assertEqual(TradeEvent.from_dict(trade.to_dict()), trade)

    def test_normalizers_emit_canonical_trades(self):
        ticks = normalize_bybit_trades({"topic": "publicTrade.BTCUSDT", "data": [
            {"s": "BTCUSDT", "i": "x1", "T": 1000, "p": "60000", "v": "0.1", "S": "Buy", "seq": 77},
        ]}, 2_000_000_000, INSTRUMENT, "raw-1")
        self.assertIs(TradeTick, TradeEvent)
        self.assertIsInstance(ticks[0], TradeEvent)
        self.assertEqual(ticks[0].sequence, 77)
        validate_canonical_event(ticks[0])

    def test_validation_rejects_unknown_aggressor(self):
        trade = trade_event("BYBIT", 1, T0, "100", "1", "")
        with self.assertRaisesRegex(ValueError, "aggressor_side"):
            validate_canonical_event(trade)

    def test_dispatcher_routes_by_type_and_shares_the_instance(self):
        books, everything = [], []
        dispatcher = CanonicalEventDispatcher((
            CallbackEventSink(books.append),
            CallbackEventSink(everything.append, accepts=None),
        ))
        book = book_event("BYBIT", 1, T0, [("100", "1")], [("101", "1")], snapshot=True)
        trade = trade_event("BYBIT", 1, T0, "100", "1", "SELL")
        dispatcher.dispatch(book)
        dispatcher.dispatch(trade)
        self.assertEqual(books, [book])
        self.assertIs(everything[1], trade)
        self.assertEqual(dispatcher.delivered_by_type, {"MarketEvent": 1, "TradeEvent": 1})

    def test_recorder_writes_trades_only_when_asked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trades.jsonl"
            with CanonicalEventRecorder(path, accepts=(TradeEvent,)) as recorder:
                dispatcher = CanonicalEventDispatcher((recorder,))
                dispatcher.dispatch(book_event("BYBIT", 1, T0, [("100", "1")], [("101", "1")], snapshot=True))
                dispatcher.dispatch(trade_event("BYBIT", 1, T0, "100", "1", "SELL"))
            rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual([row["event_type"] for row in rows], ["TRADE"])


class SimulationClockTests(unittest.TestCase):
    def test_timers_fire_in_order_before_an_event_at_the_same_time(self):
        clock = SimulationClock()
        fired = []
        clock.advance_to(100)
        clock.schedule(300, lambda t: fired.append(("b", t, clock.now_ns())))
        clock.schedule(200, lambda t: fired.append(("a", t, clock.now_ns())))
        clock.schedule(300, lambda t: fired.append(("c", t, clock.now_ns())))
        clock.advance_to(300)
        self.assertEqual(fired, [("a", 200, 200), ("b", 300, 300), ("c", 300, 300)])
        self.assertEqual(clock.now_ns(), 300)

    def test_time_never_goes_backwards(self):
        clock = SimulationClock()
        clock.advance_to(500)
        clock.advance_to(400)
        self.assertEqual((clock.now_ns(), clock.regressions), (500, 1))

    def test_cancelled_timer_does_not_fire_and_drain_lands_the_rest(self):
        clock = SimulationClock()
        fired = []
        handle = clock.schedule(10, lambda t: fired.append("x"))
        clock.schedule(20, lambda t: fired.append("y"))
        handle.cancel()
        clock.drain()
        self.assertEqual((fired, clock.now_ns()), (["y"], 20))


class QueueEngineTests(unittest.TestCase):
    def test_semantics(self):
        for factory in engines():
            with self.subTest(engine=factory().name):
                engine = factory()
                engine.set_level("bid", 100.0, 5.0)
                self.assertEqual(engine.place("a", "bid", 100.0, 1.0, 0), 5.0)
                self.assertEqual(engine.place("b", "bid", 100.0, 1.0, 0), 5.0)
                # One print shared FIFO: 5 market ahead, then a, then b.
                fills = engine.on_trade(100.0, 6.5, "SELL")
                self.assertEqual([(f.client_order_id, f.quantity) for f in fills], [("a", 1.0), ("b", 0.5)])
                engine.set_level("bid", 100.0, 0.0)  # level emptied in the feed
                self.assertEqual(engine.remaining("b"), 0.5)  # order still queued
                fills = engine.on_trade(99.0, 0.01, "SELL")  # print through our price
                self.assertEqual([(f.client_order_id, f.quantity, f.through) for f in fills], [("b", 0.5, True)])
                self.assertEqual(engine.on_trade(100.0, 5.0, "BUY"), [])  # wrong side

    def test_trade_and_its_book_delta_burn_the_same_queue_in_either_order(self):
        for factory in engines():
            for order in (("trade", "book"), ("book", "trade")):
                with self.subTest(engine=factory().name, order=order):
                    engine = factory()
                    engine.set_level("bid", 100.0, 10.0)
                    engine.place("a", "bid", 100.0, 2.0, 0)
                    engine.on_trade(100.0, 4.0, "SELL")
                    for step in order:
                        if step == "book":
                            engine.set_level("bid", 100.0, 3.0)
                        else:
                            self.assertEqual(engine.on_trade(100.0, 3.0, "SELL"), [])
                    self.assertEqual(engine.queue_ahead("a"), 3.0)

    def test_queue_position_frac_scales_queue_ahead_at_placement(self):
        for factory in (PythonQueueEngine,) + ((lambda frac: CppQueueEngine(CPP, frac)),) * (CPP is not None):
            engine = factory(0.7)
            engine.set_level("bid", 100.0, 10.0)
            self.assertEqual(engine.place("a", "bid", 100.0, 1.0, 0), 10.0 * 0.7)
        with self.assertRaises(ValueError):
            PythonQueueEngine(1.5)

    @unittest.skipIf(CPP is None and not os.environ.get("REQUIRE_CPP_ENGINE"),
                     "compiled mm_matching_engine not importable (REQUIRE_CPP_ENGINE=1 makes this a failure)")
    def test_cpp_and_python_engines_are_bit_identical(self):
        self.assertIsNotNone(CPP, "REQUIRE_CPP_ENGINE is set but mm_matching_engine did not load")
        for frac in (1.0, 0.7, 0.0):
            with self.subTest(frac=frac):
                self._parity(frac)

    def _parity(self, frac):
        rng = random.Random(1234)
        cpp, python = CppQueueEngine(CPP, frac), PythonQueueEngine(frac)
        prices = [99.0, 99.5, 100.0, 100.5, 101.0]
        orders = []
        for step in range(5_000):
            op = rng.random()
            if op < 0.35:
                args = (rng.choice(("bid", "ask")), rng.choice(prices), rng.choice((0.0, 0.5, 1.0, 2.5, 4.0)))
                cpp.set_level(*args)
                python.set_level(*args)
            elif op < 0.55:
                client = f"o{step}"
                args = (client, rng.choice(("bid", "ask")), rng.choice(prices), rng.choice((0.25, 1.0)), step)
                self.assertEqual(cpp.place(*args), python.place(*args))
                orders.append(client)
            elif op < 0.65 and orders:
                client = rng.choice(orders)
                self.assertEqual(cpp.cancel(client), python.cancel(client))
            else:
                args = (rng.choice(prices), rng.choice((0.1, 0.5, 1.0, 3.0)), rng.choice(("BUY", "SELL")))
                self.assertEqual(cpp.on_trade(*args), python.on_trade(*args), f"step {step}")
            for client in orders[-20:]:
                self.assertEqual(cpp.remaining(client), python.remaining(client))
                self.assertEqual(cpp.queue_ahead(client), python.queue_ahead(client))


class MakerSimulatorTests(unittest.TestCase):
    def setUp(self):
        cfg, _ = load_config(RESEARCH_CONFIG)
        self.book = cfg.book_sanity.new_book("BYBIT", INSTRUMENT)
        self.sequence = 0

    def simulator(self, **kwargs):
        options = dict(
            instrument_id=INSTRUMENT, latency_model=fixed_latency(), quote_size=0.001,
            quote_interval_ns=1_000 * MS, max_inventory=0.01, maker_fee_bps={"BYBIT": 1.0},
            engine="python",
        )
        options.update(kwargs)
        return MakerDryRunSimulator(["BYBIT"], "BTC/USDT", **options)

    def apply(self, sim, ts, bids=(), asks=(), snapshot=False):
        self.sequence += 1
        self.book.apply(book_event("BYBIT", self.sequence, ts, bids, asks, snapshot=snapshot))
        sim.on_book("BYBIT", self.book, ts)

    def resting(self, sim, side):
        return [o for o in sim.orders.values() if o.side == side and o.state == "RESTING"]

    def test_queue_position_is_assigned_at_venue_arrival_not_at_decision(self):
        sim = self.simulator()
        self.apply(sim, T0, [("100", "5")], [("101", "5")], snapshot=True)  # decision at T0
        self.apply(sim, T0 + 5 * MS, [("100", "8")])                          # before arrival (T0+10ms)
        self.apply(sim, T0 + 15 * MS, [("100", "9")])                         # after arrival
        bid = self.resting(sim, "BUY")[0]
        self.assertEqual(bid.arrival_ts_ns, T0 + 10 * MS)
        self.assertEqual(bid.queue_ahead_at_arrival, 8.0)
        sim.on_trade(trade_event("BYBIT", 1, T0 + 20 * MS, "100", "8.0005", "SELL"))
        fill = sim.fills[0]
        self.assertEqual((fill["cause"], fill["trade_event_id"]), ("TRADE", "BYBIT-trade-1"))
        self.assertAlmostEqual(fill["quantity"], 0.0005)
        self.assertEqual(fill["match_ts_ns"], T0 + 20 * MS)
        self.assertEqual(fill["report_observed_ts_ns"], T0 + 22 * MS)

    def test_queue_position_shrinks_when_level_drains_before_arrival(self):
        # Level is 10 at decision (T0) but falls to 3 before our order arrives at T0+10ms.
        # queue_ahead must reflect the smaller arrival-time value, not the decision-time value.
        # A trade of 3.0005 should burn the 3.0 queue and then fill 0.0005 of our size.
        sim = self.simulator()
        self.apply(sim, T0, [("100", "10")], [("101", "5")], snapshot=True)
        self.apply(sim, T0 + 5 * MS, [("100", "3")])    # shrinks before arrival
        self.apply(sim, T0 + 15 * MS, [("100", "2")])   # shrinks further AFTER arrival (irrelevant)
        bid = self.resting(sim, "BUY")[0]
        self.assertEqual(bid.arrival_ts_ns, T0 + 10 * MS)
        self.assertEqual(bid.queue_ahead_at_arrival, 3.0)   # not 10 (decision) nor 2 (post-arrival)
        sim.on_trade(trade_event("BYBIT", 1, T0 + 20 * MS, "100", "3.0005", "SELL"))
        fill = sim.fills[0]
        self.assertEqual(fill["cause"], "TRADE")
        self.assertAlmostEqual(fill["quantity"], 0.0005)
        # A smaller trade that only burns 2.9 of the 3.0 queue should NOT fill.
        sim2 = self.simulator()
        self.apply(sim2, T0, [("100", "10")], [("101", "5")], snapshot=True)
        self.apply(sim2, T0 + 5 * MS, [("100", "3")])
        self.apply(sim2, T0 + 15 * MS, [("100", "2")])
        sim2.on_trade(trade_event("BYBIT", 2, T0 + 20 * MS, "100", "2.9", "SELL"))
        self.assertEqual(sim2.fills, [], "queue not cleared → no fill")

    def test_queue_position_is_zero_when_level_disappears_before_arrival(self):
        # Level 100 is cleared to 0 before our order arrives (level 99 keeps the book valid).
        # queue_ahead must be 0: we land at the front, and any print at 100 fills us immediately.
        sim = self.simulator()
        self.apply(sim, T0, [("100", "7"), ("99", "3")], [("101", "5")], snapshot=True)
        # Level 100 cleared; level 99 keeps the book valid so _sync_levels runs.
        self.apply(sim, T0 + 5 * MS, [("100", "0")])
        self.apply(sim, T0 + 15 * MS, [("100", "2")])   # comes back AFTER arrival — does not affect queue
        bid = self.resting(sim, "BUY")[0]
        self.assertEqual(bid.arrival_ts_ns, T0 + 10 * MS)
        self.assertEqual(bid.queue_ahead_at_arrival, 0.0)   # front of queue
        # Even a tiny print at 100 fills us.
        sim.on_trade(trade_event("BYBIT", 1, T0 + 20 * MS, "100", "0.0005", "SELL"))
        fill = sim.fills[0]
        self.assertEqual(fill["cause"], "TRADE")
        self.assertAlmostEqual(fill["quantity"], 0.0005)

    def test_strategy_inventory_changes_only_when_the_fill_report_arrives(self):
        sim = self.simulator()
        self.apply(sim, T0, [("100", "1")], [("101", "1")], snapshot=True)
        self.apply(sim, T0 + 11 * MS, [("100", "1")])
        sim.on_trade(trade_event("BYBIT", 1, T0 + 12 * MS, "99", "1", "SELL"))
        venue = sim.venues["BYBIT"]
        self.assertAlmostEqual(venue.inventory, 0.006)
        self.assertAlmostEqual(venue.known_inventory, 0.005)
        sim.on_trade(trade_event("BYBIT", 2, T0 + 14 * MS, "101.5", "0.0001", "BUY"))
        self.assertAlmostEqual(venue.known_inventory, 0.006)

    def test_order_fills_while_its_cancel_is_in_flight(self):
        sim = self.simulator()
        self.apply(sim, T0, [("100", "5")], [("101", "5")], snapshot=True)
        self.apply(sim, T0 + 500 * MS, [("100", "0"), ("99", "3")])
        # Quote tick at T0+1s moves the bid: cancel applies at +20 ms.
        self.apply(sim, T0 + 1_005 * MS, [("99", "3")])
        old = next(o for o in sim.orders.values() if o.side == "BUY" and o.price == 100.0)
        self.assertTrue(old.cancel_requested)
        sim.on_trade(trade_event("BYBIT", 1, T0 + 1_010 * MS, "99", "0.5", "SELL"))
        self.assertEqual(old.state, "FILLED")
        self.assertTrue(sim.fills[0]["cancel_pending"])
        self.assertEqual(sim.fills[0]["cause"], "TRADE_THROUGH")
        sim.finish()
        report = sim.report()["venues"]["BYBIT"]
        self.assertEqual(report["fills_while_cancel_pending"], 1)
        self.assertEqual(report["cancel_rejects_too_late"], 1)
        self.assertEqual(report["sanity_violation_count"], 0)

    def test_at_price_fill_during_cancel_window_adverse_selection_example(self):
        # Scenario from the audit doc: passive BUY at 100, market moves to 98,
        # cancel takes 20 ms (fixture), an aggressive SELL hits AT 100 during that
        # window.  This exercises the at-price (through=False) cancel-window path,
        # distinct from the through-price case in the test above.
        #
        # Key timing constraint: the quote timer fires at the START of
        # clock.advance_to(), BEFORE on_book updates state.best_bid.  The book
        # move must therefore land in a PRIOR event so the timer at T0+1000ms
        # already sees the new best-bid and decides to cancel.
        sim = self.simulator(markout_horizons_ns=(1_000 * MS,))
        # Step 1 — resting bid at 100; order arrives at T0+10 ms.
        self.apply(sim, T0, [("100", "5")], [("101", "5")], snapshot=True)
        self.apply(sim, T0 + 11 * MS, [("100", "5")])
        old = next(o for o in sim.orders.values() if o.side == "BUY" and o.price == 100.0)
        self.assertEqual(old.state, "RESTING")
        # Step 2 — at T0+500 ms the book moves: ask falls to 98, best bid to 97.5.
        # state.best_bid/ask are updated NOW, before the T0+1000 ms timer fires.
        self.apply(sim, T0 + 500 * MS,
                   [("100", "0"), ("97.5", "3")], [("101", "0"), ("98", "3")])
        self.assertFalse(old.cancel_requested)  # timer hasn't fired yet
        # Step 3 — neutral delta at T0+1005 ms triggers the T0+1000 ms timer,
        # which now sees desired[BUY]=97.5 ≠ current price 100 → cancel sent.
        self.apply(sim, T0 + 1_005 * MS, [("97.5", "3")])
        self.assertTrue(old.cancel_requested, "cancel must be sent at quote-timer tick")
        # Cancel applies at T0+1000ms+20ms = T0+1020ms.
        # Step 4 — at-price SELL at 100 arrives at T0+1010ms (still in the 20ms window).
        # The level was clamped to 0 at T0+500ms → queue_ahead=0, clamp_credit=5.
        # Print volume 5.003 burns the 5.0 credit then fills 0.001 (our size).
        sim.on_trade(trade_event("BYBIT", 1, T0 + 1_010 * MS, "100", "5.003", "SELL"))
        self.assertEqual(old.state, "FILLED")
        self.assertEqual(sim.fills[0]["cause"], "TRADE")        # at-price, not TRADE_THROUGH
        self.assertTrue(sim.fills[0]["cancel_pending"])
        self.assertAlmostEqual(sim.fills[0]["price"], 100.0)
        # Step 5 — real inventory updates immediately; known_inventory waits for report.
        venue = sim.venues["BYBIT"]
        self.assertAlmostEqual(venue.inventory, sim.initial_inventory + 0.001)
        self.assertAlmostEqual(venue.known_inventory, sim.initial_inventory)
        # Step 6 — advance past the 1s markout horizon; mid is now (97.5+98)/2=97.75.
        self.apply(sim, T0 + 2_100 * MS, [("97.5", "3")])
        sim.finish()
        report = sim.report()["venues"]["BYBIT"]
        self.assertEqual(report["cancel_rejects_too_late"], 1)
        self.assertEqual(report["fills_while_cancel_pending"], 1)
        # The fill at 100 is outside 2% of the post-move book (97.5/98) — this IS
        # the adverse selection the test exercises; expect exactly this one violation.
        self.assertEqual(report["sanity_violation_count"], 1)
        self.assertIn("fill price 100.0 outside", report["sanity_violations"][0])
        markout = report["markout_bps"].get("1000ms")
        self.assertIsNotNone(markout, "markout horizon fires before end of data")
        # BUY markout: (97.75 - 100) / 100 * 10000 = -225 bps (adverse)
        self.assertLess(markout["mean"], 0, "adverse selection expected after buying into the move")
        self.assertAlmostEqual(markout["mean"], -225.0, places=1)

    def test_exposure_check_prevents_new_order_during_cancel_window(self):
        # When inventory is almost full the cancel-pending order's outstanding
        # size must be counted so the strategy cannot post a second order that
        # would exceed the band.
        # initial=0.009, max=0.01, quote_size=0.001
        # known_inventory + outstanding + quote_size = 0.009 + 0.001 + 0.001 = 0.011 > 0.01
        sim = self.simulator(initial_inventory=0.009)
        self.apply(sim, T0, [("100", "5")], [("101", "5")], snapshot=True)
        self.apply(sim, T0 + 11 * MS, [("100", "5")])
        old = next(o for o in sim.orders.values() if o.side == "BUY")
        self.assertEqual(old.state, "RESTING")
        # Book moves at T0+500 ms so the T0+1000 ms timer will issue the cancel.
        self.apply(sim, T0 + 500 * MS, [("100", "0"), ("99", "5")])
        self.apply(sim, T0 + 1_005 * MS, [("99", "5")])
        self.assertTrue(old.cancel_requested)
        # _exposure_allows must BLOCK a new BUY because outstanding already fills the band.
        new_bids = [o for o in sim.orders.values()
                    if o.side == "BUY" and not o.cancel_requested]
        self.assertEqual(len(new_bids), 0,
                         "no new buy may be sent while cancel-pending order fills the band")
        sim.finish()
        report = sim.report()["venues"]["BYBIT"]
        self.assertEqual(report["sanity_violation_count"], 0)
        self.assertLessEqual(report["inventory_range"][1], sim.max_inventory + 1e-9)

    def test_markout_truncation_at_end_of_data(self):
        # Markouts whose horizon extends past the last book event are truncated.
        # The report must count them separately so callers can adjust for the bias.
        sim = self.simulator(markout_horizons_ns=(500 * MS,))
        self.apply(sim, T0, [("100", "1")], [("101", "1")], snapshot=True)
        self.apply(sim, T0 + 11 * MS, [("100", "1")])
        # A SELL through our 100 bid fills the order at T0+200ms.
        sim.on_trade(trade_event("BYBIT", 1, T0 + 200 * MS, "99", "1", "SELL"))
        # One more book tick so the fill is observed and recorded.
        self.apply(sim, T0 + 300 * MS, [("100", "1")])
        # Finish at T0+300ms — the 500ms markout horizon (T0+700ms) never fires.
        sim.finish(T0 + 300 * MS)
        report = sim.report()["venues"]["BYBIT"]
        self.assertEqual(report["markouts_truncated"], 1)
        self.assertNotIn("500ms", report["markout_bps"])

    def test_a_crossing_book_never_fills_without_a_print(self):
        sim = self.simulator()
        self.apply(sim, T0, [("100", "5")], [("101", "5")], snapshot=True)
        # Our 100 bid rests (arrived at +10 ms); the market bid leaves 100 and a
        # seller posts at 100 -- in reality it would have hit us, but no print says so.
        self.apply(sim, T0 + 20 * MS, [("100", "0"), ("99", "5")], [("100", "2"), ("101", "0")])
        self.assertEqual(sim.fills, [])
        self.assertEqual(sim.stats["BYBIT"].crossed_without_print, 1)

    def test_post_only_order_is_rejected_if_the_book_crossed_it_before_arrival(self):
        sim = self.simulator()
        self.apply(sim, T0, [("100", "5")], [("101", "5")], snapshot=True)
        self.apply(sim, T0 + 5 * MS, [("100", "0"), ("99", "5")], [("100", "1"), ("101", "0")])
        self.apply(sim, T0 + 30 * MS, [("99", "5")])
        self.assertEqual(sim.stats["BYBIT"].post_only_rejects, 1)
        self.assertEqual(
            [o.state for o in sim.orders.values() if o.side == "BUY"], ["REJECTED"]
        )

    def test_unchanged_quote_keeps_its_queue_priority(self):
        sim = self.simulator()
        self.apply(sim, T0, [("100", "5")], [("101", "5")], snapshot=True)
        self.apply(sim, T0 + 2_500 * MS, [("100", "6")])
        self.assertEqual(sim.stats["BYBIT"].quotes, 2)
        self.assertEqual(sim.stats["BYBIT"].quotes_kept, 4)
        self.assertEqual(sim.stats["BYBIT"].cancels_sent, 0)

    def test_feed_lag_takes_the_queue_from_the_book_received_after_arrival(self):
        sim = self.simulator(feed_lag_ns=30 * MS)
        self.apply(sim, T0, [("100", "5")], [("101", "5")], snapshot=True)
        self.apply(sim, T0 + 20 * MS, [("100", "8")])   # after entry (10 ms) ...
        self.apply(sim, T0 + 35 * MS, [("100", "9")])   # ... but before entry + lag (40 ms)
        self.apply(sim, T0 + 45 * MS, [("100", "9")])
        bid = self.resting(sim, "BUY")[0]
        self.assertEqual((bid.arrival_ts_ns, bid.queue_ahead_at_arrival), (T0 + 40 * MS, 9.0))
        sim.on_trade(trade_event("BYBIT", 1, T0 + 50 * MS, "99", "1", "SELL"))
        # Report: venue instant (match at 50 ms on the feed clock) - lag + 2 ms fill latency.
        self.assertEqual(sim.fills[0]["report_observed_ts_ns"], T0 + 50 * MS)

    def test_markouts_are_signed_mid_moves_after_each_fill(self):
        sim = self.simulator(markout_horizons_ns=(100 * MS,))
        self.apply(sim, T0, [("100", "1")], [("101", "1")], snapshot=True)
        self.apply(sim, T0 + 11 * MS, [("100", "1")])
        sim.on_trade(trade_event("BYBIT", 1, T0 + 12 * MS, "99", "1", "SELL"))  # bought at 100
        self.apply(sim, T0 + 50 * MS, [("100", "0"), ("98", "1")], [("101", "0"), ("99", "1")])  # mid 98.5
        self.apply(sim, T0 + 200 * MS, [("98", "1")])
        markout = sim.report()["venues"]["BYBIT"]["markout_bps"]["100ms"]
        self.assertAlmostEqual(markout["mean"], -150.0)
        self.assertEqual(markout["count"], 1)

    def test_zero_latency_when_latency_is_disabled_is_labelled(self):
        sim = self.simulator(latency_model=None)
        self.apply(sim, T0, [("100", "5")], [("101", "5")], snapshot=True)
        self.apply(sim, T0 + 1, [("100", "5")])
        self.assertEqual(self.resting(sim, "BUY")[0].arrival_ts_ns, T0)
        self.assertEqual(sim.report()["latency"]["source"], "DISABLED_ZERO_LATENCY")


class MakerConfigTests(unittest.TestCase):
    def test_maker_latency_is_the_platform_model_on_its_own_seed(self):
        cfg, _ = load_config(WATCH_CONFIG)
        session = TradingSession(cfg)
        settings = cfg.strategies["maker_simulation"]
        maker = maker_latency_model(cfg, settings)
        self.assertIsNot(maker, session.latency_model)
        self.assertEqual(maker.to_dict()["profiles"], session.latency_model.to_dict()["profiles"])
        self.assertNotEqual(maker.seed, session.latency_model.seed)
        reference = TradingSession(cfg).latency_model
        for _ in range(50):
            maker.draw("BINANCE", "entry")
        self.assertEqual(
            [session.latency_model.draw("BINANCE", "entry") for _ in range(20)],
            [reference.draw("BINANCE", "entry") for _ in range(20)],
        )

    def test_stage_overrides_support_latency_sensitivity(self):
        cfg, _ = load_config(WATCH_CONFIG)
        settings = {**cfg.strategies["maker_simulation"], "latency": {
            "profiles": {"BYBIT": {"entry": {"base_ns": 300}}},
        }}
        model = maker_latency_model(cfg, settings)
        self.assertEqual(model.draw("BYBIT", "entry"), 300)

    def test_queue_and_feed_lag_settings_are_read_from_config(self):
        cfg, _ = load_config(WATCH_CONFIG)
        cfg.strategies["maker_simulation"] = {
            **cfg.strategies["maker_simulation"], "queue_position_frac": 0.7,
            "feed_lag_ms": {"BINANCE": 35, "BYBIT": 5}, "markout_horizons_ms": [500],
        }
        sim = maker_simulator_from_config(cfg)
        report = sim.report()
        self.assertEqual(report["queue_position_frac"], 0.7)
        self.assertEqual(report["feed_lag_ns"], {"BINANCE": 35 * MS, "BYBIT": 5 * MS})
        self.assertEqual(sim.markout_horizons_ns, (500 * MS,))

    def test_legacy_latency_ms_is_rejected(self):
        cfg, _ = load_config(WATCH_CONFIG)
        cfg.strategies["maker_simulation"] = {**cfg.strategies["maker_simulation"], "latency_ms": 10}
        with self.assertRaisesRegex(ValueError, "latency_ms"):
            maker_simulator_from_config(cfg)


class ReplayOrderTests(unittest.TestCase):
    def test_arrival_ordinal_reproduces_the_live_interleaving(self):
        book = book_event("BYBIT", 1, T0 + 5, [("100", "1")], [("101", "1")], snapshot=True)
        trade = trade_event("BYBIT", 1, T0 + 9, "100", "1", "SELL")
        ordered, how = replay_order([book], [trade], ordinals={trade.event_id: (0, 1), book.event_id: (0, 2)})
        self.assertEqual((how, [kind for kind, _ in ordered]), ("arrival_ordinal", ["TRADE", "BOOK"]))

    def test_tie_priority_is_a_declared_per_venue_policy(self):
        book = book_event("BYBIT", 1, T0, [("100", "1")], [("101", "1")], snapshot=True)
        trade = trade_event("BYBIT", 1, T0, "100", "1", "SELL")
        default, how = replay_order([book], [trade])
        self.assertEqual((how, [kind for kind, _ in default]), ("receive_ts+tie_priority", ["TRADE", "BOOK"]))
        custom, _ = replay_order([book], [trade], tie_priority={"BYBIT": ["BOOK", "TRADE"]})
        self.assertEqual([kind for kind, _ in custom], ["BOOK", "TRADE"])

    def test_exchange_time_breaks_receive_time_ties_before_kind(self):
        book = book_event("BYBIT", 9_999, T0, [("100", "1")], [("101", "1")], snapshot=True)
        trade = trade_event("BYBIT", 1, T0, "100", "1", "SELL")
        trade = replace(trade, source_ts_ns=T0 + 1, exchange_ts_ns=T0 + 1)
        ordered, _ = replay_order([book], [trade])
        self.assertEqual([kind for kind, _ in ordered], ["BOOK", "TRADE"])


    def test_duplicate_event_ids_fall_back_to_receive_time_ordering(self):
        first = book_event("BYBIT", 1, T0, [("100", "1")], [("101", "1")], snapshot=True)
        trade = trade_event("BYBIT", 1, T0 + 9, "100", "1", "SELL")
        twin = replace(trade, event_id=first.event_id)
        _, how = replay_order([first], [twin], ordinals={first.event_id: (0, 1)})
        self.assertEqual(how, "receive_ts+tie_priority")
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace.jsonl"
            trace.write_text("".join(json.dumps({
                "canonical_event_id": "same", "disposition": "CANONICAL", "arrival_ordinal": n,
            }) + "\n" for n in (1, 2)))
            from crypto_stat_arb.maker_sim import _trace_ordinals
            self.assertEqual(_trace_ordinals([trace]), {})


class BybitTopicSocket(FakeWebSocket):
    """One Bybit URL serves books and trades; the subscribe topic picks the feed."""

    def __init__(self, book_frames, trade_frames):
        super().__init__([])
        self._frames = {"orderbook": book_frames, "publicTrade": trade_frames}

    async def send(self, message):
        await super().send(message)
        topic = json.loads(message)["args"][0]
        self.messages = list(self._frames["publicTrade" if topic.startswith("publicTrade") else "orderbook"])


class RoutingConnector(FakeConnector):
    def __init__(self, binance_book, binance_trades, bybit):
        super().__init__({})
        self._binance = {"book": binance_book, "trade": binance_trades}
        self._bybit = bybit

    def __call__(self, venue, url):
        self.urls.append((venue, url))
        if venue == "BINANCE":
            key = "trade" if url.endswith("@trade") else "book"
            if self._binance[key] is None:
                raise RuntimeError("no fake connection remaining for BINANCE")
            socket, self._binance[key] = self._binance[key], None
            return socket
        if not self._bybit:
            raise RuntimeError("no fake connection remaining for BYBIT")
        return self._bybit.pop(0)


class LiveReplayParityTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_paper_and_its_replay_produce_identical_fills(self):
        bybit_books = [json.dumps({
            "topic": "orderbook.50.BTCUSDT", "type": "snapshot", "ts": 1000,
            "data": {"s": "BTCUSDT", "u": 1, "b": [["60000.00", "1.0"]], "a": [["60000.10", "1.0"]]},
        })] + [json.dumps({
            "topic": "orderbook.50.BTCUSDT", "type": "delta", "ts": 1000 + n,
            "data": {"s": "BTCUSDT", "u": 1 + n, "b": [["60000.00", f"{1 + n % 3}.0"]], "a": []},
        }) for n in range(1, 60)]
        bybit_trades = [json.dumps({"topic": "publicTrade.BTCUSDT", "data": [{
            "s": "BTCUSDT", "i": f"t{n}", "T": 1000 + n, "p": "60000.00" if n % 2 else "60000.10",
            "v": "1.5", "S": "Sell" if n % 2 else "Buy", "seq": n,
        }]}) for n in range(1, 50)]
        binance_books = [json.dumps({
            "e": "depthUpdate", "E": 1000 + n, "T": 1000 + n, "s": "BTCUSDT",
            "U": 101 + n, "u": 101 + n, "b": [["59999.90", f"{1 + n % 2}"]], "a": [],
        }) for n in range(40)]
        binance_trades = [json.dumps({
            "e": "trade", "E": 1000 + n, "T": 1000 + n, "s": "BTCUSDT", "t": n,
            "p": "59999.90" if n % 2 else "60000.00", "q": "2", "m": bool(n % 2),
        }) for n in range(1, 40)]
        clock = StepClock()

        async def http_getter(url, timeout):
            await asyncio.sleep(0)
            return json.dumps({
                "lastUpdateId": 100, "bids": [["59999.90", "1"]], "asks": [["60000.00", "1"]],
            }), clock()

        cfg, _ = load_config(WATCH_CONFIG)
        maker = maker_simulator_from_config(cfg)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paper = LivePaperSession(
                WATCH_CONFIG, base / "live", "parity",
                LiveFeedSettings(
                    max_events=90, reconnect_min_seconds=0.001, reconnect_max_seconds=0.002,
                    durable=False, udp_enabled=False, trade_streams_enabled=True,
                ),
                websocket_connector=RoutingConnector(
                    FakeWebSocket(binance_books), FakeWebSocket(binance_trades),
                    [BybitTopicSocket(bybit_books, []), BybitTopicSocket([], bybit_trades)],
                ),
                http_getter=http_getter, clock_ns=clock, tick_seconds=10,
                maker_simulator=maker,
            )
            summary = await asyncio.wait_for(paper.run(), timeout=10)
            live_fills = maker.fills
            parts = sorted((base / "live" / "parity").glob("part-*"))
            trades = sum(1 for part in parts for _ in (part / "trades.jsonl").open())
            replay = simulate_recorded_session(
                WATCH_CONFIG, [part / "canonical.jsonl" for part in parts],
                [part / "trades.jsonl" for part in parts], base / "replay.json",
            )
            replay_fills = [
                json.loads(line) for line in (base / "replay_fills.jsonl").read_text().splitlines()
            ]
        self.assertGreater(trades, 0)
        self.assertEqual(replay["replay"]["ordering"], "arrival_ordinal")
        self.assertGreater(len(live_fills), 0, summary["maker_dry_run"]["venues"])
        self.assertEqual(replay_fills, live_fills)
        self.assertEqual(
            {v: r["quotes"] for v, r in replay["venues"].items()},
            {v: r["quotes"] for v, r in summary["maker_dry_run"]["venues"].items()},
        )


if __name__ == "__main__":
    unittest.main()
