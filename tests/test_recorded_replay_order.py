"""A recorded session replays in the order it applied its events, not re-sorted by receive time."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.live_feed import LiveFeedSettings
from crypto_stat_arb.paper_live import LivePaperSession
from crypto_stat_arb.replay import RECEIVE_TIME_ORDER, RECORDED_ORDER, deterministic_order, replay_order
from crypto_stat_arb.runner import run_backtest
from test_live_feed import FakeConnector, FakeWebSocket, StepClock
from test_replay_order import book_event

ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "config/research.json"


class ReplayOrderRuleTests(unittest.TestCase):
    def events(self):
        # A REST snapshot stamped at 19 applied after a delta of another venue stamped at 20.
        return [book_event("BINANCE", 101, 11), book_event("BYBIT", 10, 15, snapshot=True),
                book_event("BYBIT", 11, 20), book_event("BINANCE", 100, 19, snapshot=True)]

    def test_complete_ordinals_reproduce_the_recorded_order(self):
        events = self.events()
        shuffled = [events[2], events[0], events[3], events[1]]
        ordered, rule = replay_order(shuffled, [3, 1, 4, 2])
        self.assertEqual(rule, RECORDED_ORDER)
        self.assertEqual(ordered, events)                    # 20 before 19, as applied live

    def test_missing_or_repeated_ordinals_fall_back_to_receive_time(self):
        events = self.events()
        for ordinals in (None, [1, 2, None, 4], [1, 2, 2, 3], [1, 2, 3]):
            ordered, rule = replay_order(events, ordinals)
            self.assertEqual(rule, RECEIVE_TIME_ORDER, ordinals)
            self.assertEqual(ordered, deterministic_order(events))


class LivePaperReplayTests(unittest.TestCase):
    def run_paper(self, base: Path, yields_before_snapshot: int, *, stamp_before_waiting: bool = False):
        """The live session from test_forward_testnet_calibration, with the REST snapshot
        returning after a set number of event-loop turns instead of a wall-clock sleep.

        ``stamp_before_waiting`` stamps the response before the frames drain, as a slow
        REST round trip does: the snapshot is then always applied after frames stamped
        later, whatever the event loop's scheduling."""
        binance = FakeWebSocket([json.dumps({"e": "depthUpdate", "E": 1000, "T": 1000, "s": "BTCUSDT",
                                             "U": 101, "u": 101, "b": [["59999.90", "2"]], "a": []})])
        bybit = FakeWebSocket([
            json.dumps({"topic": "orderbook.50.BTCUSDT", "type": "snapshot", "ts": 1000,
                        "data": {"s": "BTCUSDT", "u": 10, "b": [["60060.00", "1"]], "a": [["60060.10", "1"]]}}),
            json.dumps({"topic": "orderbook.50.BTCUSDT", "type": "delta", "ts": 1001,
                        "data": {"s": "BTCUSDT", "u": 11, "b": [["60060.00", "2"]], "a": []}}),
        ])
        clock = StepClock()

        async def http_getter(url, timeout):
            stamp = clock() if stamp_before_waiting else None
            for _ in range(yields_before_snapshot):
                await asyncio.sleep(0)
            return json.dumps({"lastUpdateId": 100, "bids": [["59999.90", "1"]],
                               "asks": [["60000.00", "1"]]}), stamp if stamp is not None else clock()

        paper = LivePaperSession(
            CONFIG, base / "live", "paper", LiveFeedSettings(
                max_events=4, reconnect_min_seconds=0.001, reconnect_max_seconds=0.002, durable=False,
                udp_enabled=False, trade_streams_enabled=False),
            websocket_connector=FakeConnector({"BINANCE": [binance], "BYBIT": [bybit]}),
            http_getter=http_getter, clock_ns=clock, tick_seconds=10)
        return asyncio.run(asyncio.wait_for(paper.run(), timeout=5)), paper.output_dir

    def test_replay_matches_the_session_for_every_interleaving(self):
        # Stamped early, the snapshot is applied after Bybit frames stamped later: the
        # interleaving windows-latest hit (paper 1 opportunity, receive-time replay 2).
        cases = [(turns, False) for turns in (0, 2, 4, 5, 6, 8, 20)] + [(turns, True) for turns in (5, 20)]
        for turns, early in cases:
            with self.subTest(turns=turns, stamp_before_waiting=early), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                summary, paper_dir = self.run_paper(base, turns, stamp_before_waiting=early)
                rows = [json.loads(line) for line in (paper_dir / "canonical_events.jsonl").read_text().splitlines()]
                self.assertEqual([row["processing_ordinal"] for row in rows], list(range(1, len(rows) + 1)))
                replay = run_backtest(CONFIG, paper_dir / "canonical_events.jsonl", base / "replay")
                manifest = json.loads((base / "replay" / "manifest.json").read_text())
                self.assertEqual(manifest["replay_ordering"], RECORDED_ORDER)
                self.assertEqual(replay["opportunities"], summary["opportunities"])
                self.assertEqual(replay["net_pnl"], summary["net_pnl"])

    def test_the_diverging_interleaving_is_out_of_receive_time_order(self):
        with tempfile.TemporaryDirectory() as directory:
            _, paper_dir = self.run_paper(Path(directory), 20, stamp_before_waiting=True)
            stamps = [json.loads(line)["receive_ts_ns"]
                      for line in (paper_dir / "canonical_events.jsonl").read_text().splitlines()]
            self.assertNotEqual(stamps, sorted(stamps))      # the case receive-time replay got wrong


if __name__ == "__main__":
    unittest.main()
