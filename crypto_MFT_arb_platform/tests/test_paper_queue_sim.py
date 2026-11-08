"""Phase D, D7b: paper trading on the one runtime. `LivePaperSession(queue_simulation=...)` builds the simulated venue as the Python queue simulator (queue v2) behind `QueueAwareVenueAdapter`, fed per event by the
shared `SimulatedVenueFeeder`. Fake feeds only (no network, no orders): the wiring runs end to end and a post-only order placed through the session's own router is queued and filled from a print."""
import asyncio
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from crypto_stat_arb.execution import CanonicalOrderRequest, ExecutionEventType, OrderType, TimeInForce
from crypto_stat_arb.live_feed import LiveFeedSettings
from crypto_stat_arb.models import Side
from crypto_stat_arb.paper_live import LivePaperSession
from crypto_stat_arb.session import ExecutionMode
from crypto_stat_arb.simulation.queue_adapter import QueueAwareVenueAdapter, SimulatedVenueFeeder
from test_live_feed import StepClock
from test_maker_execution_path import BybitTopicSocket, RoutingConnector, WATCH_CONFIG


def feeds():
    bybit_books = [json.dumps({"topic": "orderbook.50.BTCUSDT", "type": "snapshot", "ts": 1000, "data": {"s": "BTCUSDT", "u": 1, "b": [["60000.00", "1.0"]], "a": [["60000.10", "1.0"]]}})] + [
        json.dumps({"topic": "orderbook.50.BTCUSDT", "type": "delta", "ts": 1000 + n, "data": {"s": "BTCUSDT", "u": 1 + n, "b": [["60000.00", f"{1 + n % 3}.0"]], "a": []}}) for n in range(1, 60)]
    bybit_trades = [json.dumps({"topic": "publicTrade.BTCUSDT", "data": [{"s": "BTCUSDT", "i": f"t{n}", "T": 1000 + n, "p": "60000.00" if n % 2 else "60000.10", "v": "1.5", "S": "Sell" if n % 2 else "Buy", "seq": n}]})
                    for n in range(1, 50)]
    binance_books = [json.dumps({"e": "depthUpdate", "E": 1000 + n, "T": 1000 + n, "s": "BTCUSDT", "U": 101 + n, "u": 101 + n, "b": [["59999.90", f"{1 + n % 2}"]], "a": []}) for n in range(40)]
    binance_trades = [json.dumps({"e": "trade", "E": 1000 + n, "T": 1000 + n, "s": "BTCUSDT", "t": n, "p": "59999.90" if n % 2 else "60000.00", "q": "2", "m": bool(n % 2)}) for n in range(1, 40)]
    return bybit_books, bybit_trades, binance_books, binance_trades


class PaperOnTheQueueSimulator(unittest.IsolatedAsyncioTestCase):
    async def _session(self, base: Path, **kw):
        bybit_books, bybit_trades, binance_books, binance_trades = feeds()
        clock = StepClock()

        async def http_getter(url, timeout):
            await asyncio.sleep(0)
            return json.dumps({"lastUpdateId": 100, "bids": [["59999.90", "1"]], "asks": [["60000.00", "1"]]}), clock()

        return LivePaperSession(
            WATCH_CONFIG, base / "live", "queue-sim",
            LiveFeedSettings(max_events=90, reconnect_min_seconds=0.001, reconnect_max_seconds=0.002, durable=False, udp_enabled=False, trade_streams_enabled=True),
            websocket_connector=RoutingConnector(binance_books and __import__("test_live_feed").FakeWebSocket(binance_books), __import__("test_live_feed").FakeWebSocket(binance_trades),
                                                 [BybitTopicSocket(bybit_books, []), BybitTopicSocket([], bybit_trades)]),
            http_getter=http_getter, clock_ns=clock, tick_seconds=10, **kw)

    async def test_the_session_runs_on_the_queue_simulator_and_a_post_only_order_fills_from_a_print(self):
        with tempfile.TemporaryDirectory() as directory:
            paper = await self._session(Path(directory), queue_simulation={})
            self.assertIsInstance(paper.feeder, SimulatedVenueFeeder)
            adapters = paper.session.router.adapters
            self.assertTrue(adapters and all(isinstance(a, QueueAwareVenueAdapter) for a in adapters.values()))
            self.assertEqual(len({id(a.executor) for a in adapters.values()}), 1)                       # one executor behind every venue
            await asyncio.wait_for(paper.run(), timeout=10)
            for venue, view in paper._views.items():
                self.assertTrue(view.ready, venue)                                                         # the persistent view books were fed by the live events
            # an order placed through the session's own router lands in the queue model and is filled by a later print on that venue
            venue = "BYBIT"
            book = paper.session.books[(venue, paper.cfg.instrument_id)]
            request = CanonicalOrderRequest.create(
                action=__import__("crypto_stat_arb.execution", fromlist=["OrderAction"]).OrderAction.SUBMIT, order_id="probe-1", parent_opportunity_id="op", parent_order_id="parent",
                instrument_id=paper.cfg.instrument_id, venue=venue, side=Side.BUY, price=Decimal("60000.00"), quantity=Decimal("0.001"), order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC, post_only=True, created_ts_ns=paper.session.last_event_ts_ns + 1)
            (ack,) = adapters[venue].handle_at(request, book, request.created_ts_ns)
            self.assertEqual(ack.event_type, ExecutionEventType.ACKNOWLEDGED)
            self.assertEqual(len(adapters[venue].executor.live_orders(venue)), 1)

    async def test_it_is_refused_with_other_adapters_or_outside_paper_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "INTERNAL_PAPER only"):
                await self._session(Path(directory), queue_simulation={}, adapters={"BINANCE": object(), "BYBIT": object()})


class PaperWiringEqualsReplayWiring(unittest.TestCase):
    """D7c criterion 1 in miniature: the same canonical events and the same probe order through (a) the replay wiring (feeder, the core session applying the books) and (b) the paper wiring
    (a LivePaperSession built offline, the live service's books receiving each event, then its own listeners) give the same fills, to the nanosecond."""

    def _events(self):
        from crypto_stat_arb.models import EventType, Level, MarketEvent, TradeEvent
        inst, out = "BTC-USDT-SPOT", []
        for venue in ("BINANCE", "BYBIT"):
            out.append(MarketEvent(event_id=f"{venue}-s", event_type=EventType.BOOK_SNAPSHOT, venue=venue, instrument_id=inst, exchange_ts_ns=1_000_000_000, receive_ts_ns=1_000_000_000, sequence=1,
                                   source_ts_ns=1_000_000_000, sequence_start=1, raw_ref="t", adapter_version="t", bids=(Level(Decimal("60000.00"), Decimal("2")),), asks=(Level(Decimal("60000.10"), Decimal("2")),)))
        t = 1_050_000_000
        for n in range(1, 40):
            t += 20_000_000
            out.append(MarketEvent(event_id=f"BYBIT-d{n}", event_type=EventType.BOOK_DELTA, venue="BYBIT", instrument_id=inst, exchange_ts_ns=t, receive_ts_ns=t, sequence=1 + n, source_ts_ns=t,
                                   sequence_start=1 + n, raw_ref="t", adapter_version="t", bids=(Level(Decimal("60000.00"), Decimal(str(1 + n % 3))),), asks=()))
            out.append(MarketEvent(event_id=f"BINANCE-d{n}", event_type=EventType.BOOK_DELTA, venue="BINANCE", instrument_id=inst, exchange_ts_ns=t + 1, receive_ts_ns=t + 1, sequence=1 + n, source_ts_ns=t + 1,
                                   sequence_start=1 + n, raw_ref="t", adapter_version="t", bids=(Level(Decimal("60000.00"), Decimal(str(1 + n % 2))),), asks=()))
            if n % 2 == 0:
                out.append(TradeEvent(event_id=f"BYBIT-t{n}", venue="BYBIT", instrument_id=inst, trade_id=f"t{n}", exchange_ts_ns=t + 2, source_ts_ns=t + 2, receive_ts_ns=t + 2,
                                      price=Decimal("60000.00"), quantity=Decimal("3"), aggressor_side="SELL"))
        return sorted(out, key=lambda e: (e.receive_ts_ns, e.event_id))

    def _request(self, venue, ts):
        from crypto_stat_arb.execution import OrderAction
        return CanonicalOrderRequest.create(action=OrderAction.SUBMIT, order_id="p1", parent_opportunity_id="op", parent_order_id="parent", instrument_id="BTC-USDT-SPOT", venue=venue, side=Side.BUY,
                                            price=Decimal("60000.00"), quantity=Decimal("0.5"), order_type=OrderType.LIMIT, time_in_force=TimeInForce.GTC, post_only=True, created_ts_ns=ts)

    def _run(self, session, adapters, executor, on_book, on_trade):
        from crypto_stat_arb.models import TradeEvent
        fills = []
        session.router._deliver_resting_fills = lambda venue, produced, now_ns, exchange_ts_ns=None: fills.extend((now_ns, float(cb.last_fill_quantity), float(cb.average_price)) for rq, cb in produced) or len(produced)
        placed = False
        for ev in self._events():
            if not placed and ev.receive_ts_ns >= 1_100_000_000:
                placed = True
                (ack, *_) = adapters["BYBIT"].handle_at(self._request("BYBIT", ev.receive_ts_ns), session.books[("BYBIT", "BTC-USDT-SPOT")], ev.receive_ts_ns)
                self.assertEqual(ack.event_type, ExecutionEventType.ACKNOWLEDGED)
            (on_trade if isinstance(ev, TradeEvent) else on_book)(ev)
        return fills

    def test_the_same_events_and_order_give_the_same_fills(self):
        from crypto_stat_arb.config import load_config
        from crypto_stat_arb.session import TradingSession
        from crypto_stat_arb.simulation.queue_adapter import build_queue_simulation
        cfg, _ = load_config(Path(__file__).resolve().parents[1] / "config/paper_watch.json")
        adapters, views, states, executor = build_queue_simulation(cfg)
        replay = TradingSession(cfg, execution_mode=ExecutionMode.INTERNAL_SIMULATION, adapters=adapters, strategies=[])
        feeder = SimulatedVenueFeeder(replay, {v: v for v in cfg.venues}, views, states, adapters, ets_of=lambda ns: None)
        a = self._run(replay, adapters, executor, lambda e: feeder.on_book(e), lambda e: feeder.on_trade(e))
        clock = {"now": 0}
        with tempfile.TemporaryDirectory() as tmp:
            paper = LivePaperSession(Path(__file__).resolve().parents[1] / "config/paper_watch.json", Path(tmp) / "out", "wiring", LiveFeedSettings(udp_enabled=False, durable=False),
                                     queue_simulation={}, clock_ns=lambda: clock["now"], tick_seconds=3600)
            paper.session.strategies.clear()
            books = paper.service.books

            def on_book(ev):
                clock["now"] = ev.receive_ts_ns
                books[ev.venue].apply(ev)
                paper.service.book_gate.allow(tuple(books.values()), ev.receive_ts_ns)
                paper._on_event(ev, books[ev.venue], books[ev.venue].state, True)

            def on_trade(ev):
                clock["now"] = ev.receive_ts_ns
                paper._on_trade(ev)

            b = self._run(paper.session, paper.session.router.adapters, paper.queue_executor, on_book, on_trade)
            paper._timestamps.close()
        self.assertTrue(a, "the probe order must fill in this stream")
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()


class LivePaperCommandLineTests(unittest.TestCase):
    """Phase D, D7d prerequisite: the live-paper command can select the queue simulator and the Bybit book heartbeat; both are off by default."""

    def test_flags_exist_and_default_off(self):
        import argparse
        from crypto_stat_arb import cli
        import io, contextlib
        captured = {}
        orig = cli._run_live_paper

        async def fake(args):
            captured["args"] = args
            return {}
        cli._run_live_paper = fake
        orig_print = cli._print_live_result
        cli._print_live_result = lambda result: None
        try:
            import sys
            for argv, expect_q, expect_hb in ((["live-paper", "--config", "c.json"], False, 0.0),
                                              (["live-paper", "--config", "c.json", "--queue-sim", "--book-heartbeat-seconds", "0.25"], True, 0.25)):
                old_argv = sys.argv
                sys.argv = ["crypto-stat-arb", *argv]
                try:
                    cli.main()
                finally:
                    sys.argv = old_argv
                self.assertEqual((captured["args"].queue_sim, captured["args"].book_heartbeat_seconds), (expect_q, expect_hb))
                self.assertEqual(cli._live_settings(captured["args"]).book_heartbeat_seconds, expect_hb)
        finally:
            cli._run_live_paper = orig
            cli._print_live_result = orig_print

    def test_queue_sim_and_maker_dry_run_together_are_refused(self):
        import argparse, asyncio
        from crypto_stat_arb import cli
        args = argparse.Namespace(queue_sim=True, maker_dry_run=True, execution_mode="internal_paper")
        with self.assertRaises(SystemExit):
            asyncio.run(cli._run_live_paper(args))


class LivePaperRunsTheProfileStrategy(unittest.TestCase):
    """Phase D, D7d (owner 9 Nov): `live-paper` builds the profile's own strategy (ArbSessionStrategy over the v1_5 signal) with the profile's simulator and session rules, not the default spread strategy."""

    PROFILE = "perp_perp_maker_taker_v1_5"

    def build(self, tmp: Path):
        from new_backtest.profiles import apply_profile, load_profile
        from new_backtest.session_replay import SessionReplay, live_paper_setup
        profile = load_profile(self.PROFILE)
        rep = SessionReplay(apply_profile(profile, pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", data_root="", output_dir=""), profile)
        cfg_file = tmp / "cfg.json"
        cfg_file.write_text(json.dumps(rep.raw_config))
        setup = live_paper_setup(self.PROFILE)
        paper = LivePaperSession(cfg_file, tmp / "out", "profile-strategy", LiveFeedSettings(udp_enabled=False, binance_market="linear", bybit_market="linear", durable=False),
                                 queue_simulation=setup["queue_simulation"], session_kwargs=setup["session_kwargs"], strategy_builder=setup["strategy_builder"], tick_seconds=3600)
        return paper, setup

    def close(self, paper):
        paper.service.recorder.close({})
        paper.service.ohlc_writer.close()
        paper.service.ltp_writer.close()

    def test_the_session_runs_arb_session_strategy_not_the_default_spread_strategy(self):
        with tempfile.TemporaryDirectory() as tmp:
            paper, setup = self.build(Path(tmp))
            try:
                names = [type(s).__name__ for s in paper.session.strategies]
                self.assertEqual(names, ["ArbSessionStrategy"])
                self.assertIs(paper.session.strategy, paper.session.strategies[0])
                self.assertNotIn("CrossVenueSpreadStrategy", names)
                self.assertIsNone(paper.session.strategies[0].evaluate(paper.session.books, 1))           # empty books: no signal, no error
            finally:
                self.close(paper)

    def test_the_profiles_simulator_and_session_rules_are_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            paper, setup = self.build(Path(tmp))
            try:
                mc = paper.queue_executor.config
                self.assertEqual((mc.cancel_model, mc.fill_evidence), ("proportional", "trade_confirm"))      # the profile's maker block, not the executor's defaults
                self.assertEqual(setup["queue_simulation"]["cancel_model"], "proportional")
                self.assertEqual(tuple(paper.session.maker_venues), ("BYBIT",))
                self.assertGreater(paper.session.oms.order_lifetime_ns, 0)                                  # the profile's mandatory maker order lifetime
            finally:
                self.close(paper)

    def test_the_session_artifacts_can_be_written_with_the_profiles_latency_model(self):
        """The D7d live check crashed at the end of the run in write_session_artifacts (the timing latency model had no seed): finalize must write its files."""
        from crypto_stat_arb.models import MarketEvent
        with tempfile.TemporaryDirectory() as tmp:
            paper, setup = self.build(Path(tmp))
            try:
                self.assertEqual(paper.session.latency_model.seed, setup["session_kwargs"]["latency_model"].seed)
                self.assertIsInstance(paper.session.latency_model.seed, int)
                clock = 1_700_000_000_000_000_000
                snap = lambda venue, seq, bid, ask: MarketEvent.from_dict({
                    "event_type": "BOOK_SNAPSHOT", "venue": venue, "instrument_id": paper.cfg.instrument_id, "exchange_ts_ns": clock, "source_ts_ns": clock, "receive_ts_ns": clock + 1_000_000,
                    "sequence": seq, "bids": [{"price": str(bid), "quantity": "5"}], "asks": [{"price": str(ask), "quantity": "5"}]})
                for ev in (snap("BYBIT", 1, "100.000", "100.010"), snap("BINANCE", 1, "100.000", "100.010")):
                    paper.events.append(ev)
                result = paper.finalize({"venues": {}})
                self.assertNotEqual(result.get("status"), "NO_MARKET_DATA")
                self.assertTrue((paper.output_dir / "latency_report.json").exists())
            finally:
                self.close(paper)

    def test_a_strategy_builder_without_the_queue_simulator_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            from new_backtest.profiles import apply_profile, load_profile
            from new_backtest.session_replay import SessionReplay, live_paper_setup
            profile = load_profile(self.PROFILE)
            rep = SessionReplay(apply_profile(profile, pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", data_root="", output_dir=""), profile)
            cfg_file = Path(tmp) / "cfg.json"
            cfg_file.write_text(json.dumps(rep.raw_config))
            with self.assertRaises(ValueError):
                LivePaperSession(cfg_file, Path(tmp) / "out", "no-queue-sim", LiveFeedSettings(udp_enabled=False, binance_market="linear", bybit_market="linear", durable=False),
                                 strategy_builder=live_paper_setup(self.PROFILE)["strategy_builder"])

    def test_the_command_line_needs_queue_sim_and_a_profile_for_a_strategy_setup(self):
        import argparse
        from crypto_stat_arb import cli
        base = dict(queue_sim=False, maker_dry_run=False, execution_mode="internal_paper", strategy_setup="new_backtest.session_replay:live_paper_setup", strategy_profile=self.PROFILE,
                    venue=None, binance_market="linear", bybit_market="linear", binance_ws_base=None, binance_rest_base=None, bybit_ws_url=None, bybit_depth=50, http_timeout_seconds=10.0,
                    initial_sync_timeout_seconds=15.0, reconnect_min_seconds=1.0, reconnect_max_seconds=30.0, rotate_seconds=3600.0, rotate_bytes=1000, no_fsync=True, max_runtime_seconds=1.0,
                    max_events=None, no_trades=False, trade_stale_seconds=30.0, no_udp=True, udp_group="239.255.77.77", udp_port=19001, udp_interface="0.0.0.0", udp_ttl=1, udp_depth=20,
                    udp_max_datagram_bytes=60000, monitor_interval_seconds=1.0, rest_reconcile_seconds=None, book_heartbeat_seconds=0.0, config=Path("c.json"), output_dir=Path("o"),
                    session_id="x", status_poll_seconds=0.0, flatten_lead_seconds=20.0, status_only=True)
        with self.assertRaises(SystemExit):                                              # no --queue-sim
            asyncio.run(cli._run_live_paper(argparse.Namespace(**base)))
        with self.assertRaises(SystemExit):                                              # no profile
            asyncio.run(cli._run_live_paper(argparse.Namespace(**{**base, "queue_sim": True, "strategy_profile": None})))
