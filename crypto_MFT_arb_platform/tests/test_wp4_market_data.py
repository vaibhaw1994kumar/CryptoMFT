"""WP4 market-data semantics and fill model.

Harness books: bybit bids 99 (5.0) / 98 (5.0), asks 101 / 102; the maker bid rests at 100 (an ABSENT level inside the spread) or
at 99 (the existing best bid, 5.0 ahead). Every expected number is arithmetic written here.
"""
from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

from new_backtest.event_loader import DELTA, iter_venue_events
from new_backtest.order_book import OrderBook

from test_maker_order_lifetime import BINANCE, BYBIT, Harness


def delta(h: Harness, t: float, price: float, size: float, venue=BYBIT, side="bid"):
    h.event(t, DELTA, venue, {"side": side, "price": price, "volume": size, "event_type": "modify"})


class SharedTradeVolumeTests(unittest.TestCase):
    def test_one_print_fills_two_own_orders_at_one_level_only_once_in_total(self):
        h = Harness(max_live=2)                                                # queue 0.5 ahead of each (absent level, legacy floor)
        h.book(0.5, BYBIT); h.book(0.5, BINANCE)
        h.send_maker(1.0); h.send_maker(1.0)
        h.advance(1.1)
        h.sell_trade(5.0, BYBIT, volume=3.0)                                   # 0.5 queue + 2.0 fill (order 1) + 0.5 queue (order 2) = 3.0
        self.assertEqual(h.bt.inventory.base[BYBIT], 2.0)
        self.assertEqual(sum(r["qty"] for r in h.bt.trade_log if r["venue"] == BYBIT), 2.0)
        o2 = [o for o in h.resting][0]
        self.assertEqual((o2.filled_qty, o2.queue_ahead), (0.0, 0.0))           # the print's remainder only burnt its queue

    def test_legacy_unshared_volume_hands_the_whole_print_to_every_order(self):
        h = Harness(max_live=2)
        h.bt.maker.config.share_trade_volume = False
        h.book(0.5, BYBIT); h.book(0.5, BINANCE)
        h.send_maker(1.0); h.send_maker(1.0)
        h.advance(1.1)
        h.sell_trade(5.0, BYBIT, volume=3.0)
        self.assertEqual(h.bt.inventory.base[BYBIT], 4.0)                      # 2.0 + 2.0 from a 3.0 print: the legacy double count


class TradeDepthOrderIndependenceTests(unittest.TestCase):
    """A 1.0 execution at 99 is published as a trade AND as the level shrinking 5.0 -> 4.0. Whichever arrives first, the queue
    ahead of our order at 99 ends at 4.0 (never 3.0)."""

    def _order_at_99(self):
        h = Harness(queue=0.0)
        h.book(0.5, BYBIT); h.book(0.5, BINANCE)
        h.send_maker(1.0, price=99.0)
        h.advance(1.1)
        self.assertEqual(h.order().queue_ahead, 5.0)
        return h

    def test_trade_then_depth(self):
        h = self._order_at_99()
        h.sell_trade(5.0, BYBIT, volume=1.0, price=99.0)
        delta(h, 5.01, 99.0, 4.0)
        self.assertAlmostEqual(h.order().queue_ahead, 4.0, places=9)
        self.assertEqual(h.order().filled_qty, 0.0)

    def test_depth_then_trade(self):
        h = self._order_at_99()
        delta(h, 5.0, 99.0, 4.0)
        h.sell_trade(5.01, BYBIT, volume=1.0, price=99.0)
        self.assertAlmostEqual(h.order().queue_ahead, 4.0, places=9)
        self.assertEqual(h.order().filled_qty, 0.0)

    def test_without_dedupe_the_execution_is_charged_twice(self):
        h = self._order_at_99()
        h.bt.maker.config.dedupe_trade_depth = False
        h.sell_trade(5.0, BYBIT, volume=1.0, price=99.0)
        delta(h, 5.01, 99.0, 4.0)
        # the trade drains 1.0 (-> 4.0), then the SAME execution's depth decrease is charged again: proportional share
        # ahead / level_now = 4.0 / 5.0 of the 1.0 shrink -> 3.2 (the legacy double count)
        self.assertAlmostEqual(h.order().queue_ahead, 3.2, places=9)


class LevelCreatorRuleTests(unittest.TestCase):
    def _creator(self, trade_only: bool):
        h = Harness(queue=0.0)                                                 # absent level at 100, queue 0
        h.bt.maker.config.level_creator_trade_only = trade_only
        h.bt.maker.config.vanish_clears_queue = True
        h.book(0.5, BYBIT); h.book(0.5, BINANCE)
        h.send_maker(1.0)
        h.advance(1.1)
        self.assertTrue(h.order().level_was_absent)
        delta(h, 5.0, 100.0, 1.0)                                              # someone joins behind us
        delta(h, 5.1, 100.0, 0.0)                                              # ...and leaves: an unexplained decrease at our price
        return h

    def test_creator_does_not_fill_from_a_depth_decrease_but_does_from_a_trade(self):
        h = self._creator(True)
        self.assertEqual(h.bt.inventory.base[BYBIT], 0.0)
        self.assertGreaterEqual(h.bt.maker.diag["level_creator_depth_fills_blocked"], 1)
        h.sell_trade(6.0, BYBIT, volume=2.5)
        self.assertEqual(h.bt.inventory.base[BYBIT], 2.0)

    def test_legacy_lets_the_decrease_fill_the_creator(self):
        h = self._creator(False)
        self.assertGreater(h.bt.inventory.base[BYBIT], 0.0)
        self.assertEqual(h.bt.maker.diag.get("level_creator_depth_fills_blocked", 0), 0)


class UnknownQueueTests(unittest.TestCase):
    def _h(self, model: str, floor=3.0):
        h = Harness(queue=0.0, max_live=2)
        h.bt.maker.config.unknown_queue_model = model
        h.bt.maker.config.unknown_queue_floor = floor
        h.book(0.5, BYBIT); h.book(0.5, BINANCE)
        return h

    def test_depth_aware_absent_inside_depth_is_zero_and_outside_is_unknown(self):
        h = self._h("depth_aware")
        h.send_maker(1.0, price=100.0)                                         # inside the spread: covered by the capture
        h.send_maker(1.0, price=97.0)                                          # below the deepest captured bid (98): unknown
        h.advance(1.1)
        inside, outside = sorted(h.resting, key=lambda o: -o.price)
        self.assertEqual((inside.queue_ahead, inside.queue_unknown), (0.0, False))
        self.assertEqual((outside.queue_ahead, outside.queue_unknown), (3.0, True))
        d = h.bt.maker.diag
        self.assertEqual((d["absent_level_inside_depth"], d["absent_level_outside_depth_unknown"]), (1, 1))

    def test_legacy_floor_applies_to_every_absent_level(self):
        h = self._h("legacy")
        h.bt.maker.config.min_queue_ahead = 0.5
        h.send_maker(1.0, price=100.0)
        h.send_maker(1.0, price=97.0)
        h.advance(1.1)
        self.assertEqual([o.queue_ahead for o in h.resting], [0.5, 0.5])
        self.assertFalse(any(o.queue_unknown for o in h.resting))

    def test_lognormal_draw_is_seeded_and_floored(self):
        from new_backtest.fill_engine import MakerConfig
        a = Harness(queue=0.0); b = Harness(queue=0.0)
        for h in (a, b):
            c = h.bt.maker.config
            c.unknown_queue_model, c.unknown_queue_dist, c.unknown_queue_median, c.unknown_queue_sigma, c.unknown_queue_floor = \
                "depth_aware", "lognormal", 2.0, 0.5, 1.0
            h.book(0.5, BYBIT); h.book(0.5, BINANCE)
            h.send_maker(1.0, price=97.0)
            h.advance(1.1)
        self.assertEqual(a.order().queue_ahead, b.order().queue_ahead)
        self.assertGreaterEqual(a.order().queue_ahead, 1.0)
        with self.assertRaises(ValueError):
            MakerConfig(unknown_queue_model="guess")


class BookStateTests(unittest.TestCase):
    def test_states_invalid_valid_recovering_stale(self):
        b = OrderBook("bybit", "SOLUSDT")
        self.assertEqual(b.status(0.0, 0.5), "invalid")
        b.apply_delta("bid", 99.0, 1.0, "modify", 0.0); b.apply_delta("ask", 101.0, 1.0, "modify", 0.0)
        self.assertTrue(b.ready)                                               # legacy readiness is unchanged...
        self.assertEqual(b.status(0.0, 0.5), "invalid")                        # ...but no snapshot yet: not valid
        b.apply_snapshot([[99.0, 5.0]], [[101.0, 5.0]], 1.0)
        self.assertEqual(b.status(1.2, 0.5), "valid")
        self.assertEqual((b.captured_depth_bid, b.captured_depth_ask), (1, 1))
        self.assertEqual(b.status(2.0, 0.5), "stale")
        self.assertTrue(b.apply_sequence(10, 12, None, 1.3))                  # first ids seen
        self.assertTrue(b.apply_sequence(13, 15, 12, 1.4))                    # continuous (pu == last u)
        self.assertFalse(b.apply_sequence(20, 22, 18, 1.5))                   # gap: 18 != 15
        self.assertEqual((b.status(1.5, 0.5), b.gaps, b.recovering_since), ("recovering", 1, 1.5))
        self.assertTrue(b.apply_sequence(23, 23, 22, 1.6))                    # continuous again, still recovering until a snapshot
        self.assertEqual(b.state, "recovering")
        b.apply_snapshot([[99.0, 5.0]], [[101.0, 5.0]], 1.7)
        self.assertEqual(b.status(1.7, 0.5), "valid")
        self.assertTrue(b.apply_sequence(23, 23, 22, 1.8))                    # repeated id: counted, ignored
        self.assertEqual(b.seq_duplicates, 1)
        self.assertEqual(b.stats()["sequence_gaps"], 1.0)

    def test_backtester_gate_blocks_entry_while_a_book_is_recovering(self):
        h = Harness(cfg_kw={"enforce_book_state": True})
        h.book(0.5, BYBIT); h.book(0.5, BINANCE)
        self.assertFalse(h.bt._book_state_blocks(0.6))
        h.event(0.7, DELTA, BINANCE, {"side": "bid", "price": 99.0, "volume": 4.0, "event_type": "modify", "U": 100, "u": 101, "pu": 99})
        h.event(0.8, DELTA, BINANCE, {"side": "bid", "price": 99.0, "volume": 3.0, "event_type": "modify", "U": 105, "u": 106, "pu": 104})
        self.assertEqual(h.bt.books[BINANCE].state, "recovering")
        self.assertTrue(h.bt._book_state_blocks(0.9))
        self.assertEqual(h.bt.hedge["signals_blocked_by_book_recovering_binance"], 1)
        h.book(1.0, BINANCE)                                                   # snapshot resyncs
        self.assertEqual(h.bt.books[BINANCE].state, "valid")
        self.assertTrue(h.bt._book_state_blocks(1.1))                          # ...but bybit's book (last 0.5) is now STALE at 1.1
        self.assertEqual(h.bt.hedge["signals_blocked_by_book_stale_bybit"], 1)
        h.book(1.1, BYBIT)
        self.assertFalse(h.bt._book_state_blocks(1.2))

    def test_jan_2022_shaped_records_without_ids_never_touch_the_state(self):
        h = Harness(cfg_kw={"enforce_book_state": True})
        h.book(0.5, BYBIT); h.book(0.5, BINANCE)
        delta(h, 0.7, 99.0, 4.0, venue=BINANCE)
        self.assertEqual(h.bt.books[BINANCE].state, "valid")
        self.assertIsNone(h.bt.books[BINANCE].seq_last)


class TradeIdDedupeTests(unittest.TestCase):
    def _write(self, root: Path, name: str, rows: list[dict]):
        d = root / "bybit" / "trades" / "pair=XUSDT" / "date=2022-01-01"
        d.mkdir(parents=True, exist_ok=True)
        with gzip.open(d / name, "wt") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    def test_native_trade_ids_dedupe_across_collectors_and_keep_genuine_repeats(self):
        base = {"exchange": "bybit", "pair": "XUSDT", "exchange_ts": 1640995200.0, "price": 100.0, "volume": 1.0, "side": "s"}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            # collector A and collector B saw the same two executions (ids 1, 2); each has its own local_ts
            self._write(root, "a.jsonl.gz", [dict(base, trade_id=1, local_ts=1.0), dict(base, trade_id=2, local_ts=1.0)])
            self._write(root, "b.jsonl.gz", [dict(base, trade_id=1, local_ts=1.2), dict(base, trade_id=2, local_ts=1.2)])
            stats = {}
            evs = list(iter_venue_events(root, "bybit", "XUSDT", "2022-01-01", "2022-01-01", include_snapshots=False, include_deltas=False, stats=stats))
            self.assertEqual(sorted(e.payload["trade_id"] for e in evs), [1, 2])              # ids 1 and 2 once each, same content kept
            self.assertEqual(stats["duplicates_dropped"], 2)
            self.assertEqual(stats["trades_keyed_by_id"], 4)
            self.assertEqual(evs[0].payload["side"], "s")                                       # native fields stay in the payload

    def test_records_without_ids_fall_back_to_the_content_key(self):
        base = {"exchange": "bybit", "pair": "XUSDT", "exchange_ts": 1640995200.0, "price": 100.0, "volume": 1.0, "side": "s", "local_ts": 1.0}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._write(root, "a.jsonl.gz", [base, base])                                       # same-content repeat in ONE file
            stats = {}
            evs = list(iter_venue_events(root, "bybit", "XUSDT", "2022-01-01", "2022-01-01", include_snapshots=False, include_deltas=False, stats=stats))
            self.assertEqual(len(evs), 1)                                                       # legacy "set" rule drops it
            self.assertNotIn("trades_keyed_by_id", stats)


if __name__ == "__main__":
    unittest.main()
