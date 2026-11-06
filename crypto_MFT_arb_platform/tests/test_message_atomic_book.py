"""Message-atomic book (v1_2 switch ArbBacktestConfig.message_atomic_book; off in v1 / v1.1).

An exchange message is the run of consecutive book deltas of one venue with the same exchange_ts and receive time. With the switch on, a crossed state that exists only
between two deltas of one message is not a cross: it is not repaired and not counted; the cross is judged once at the message end. The strategy, the touch, the marks and
the maker queue updates run after the whole message. Synthetic books: wiring only, no market claim."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from new_backtest.event_loader import BOOK, DELTA, TRADE
from new_backtest.order_book import OrderBook

from test_maker_order_lifetime import BINANCE, BYBIT, Harness


def book(atomic: bool) -> OrderBook:
    b = OrderBook("bybit", "X", message_atomic=atomic)
    b.apply_snapshot([[99.0, 5.0], [98.0, 5.0]], [[101.0, 5.0], [102.0, 5.0]], 1.0)
    return b


class OrderBookMessageAtomic(unittest.TestCase):
    def test_a_transient_cross_inside_one_message_deletes_real_levels_per_delta_but_not_per_message(self):
        for atomic in (False, True):
            b = book(atomic)
            b.apply_delta("bid", 102.0, 1.0, "update", 2.0)            # crosses both asks for a moment ...
            b.apply_delta("bid", 102.0, 0.0, "delete", 2.0)            # ... and the same message removes it again
            b.end_message()
            if atomic:
                self.assertEqual((b.best_bid, b.best_ask, b.crossed_events), (99.0, 101.0, 0))
            else:
                self.assertGreater(b.crossed_events, 0)
                self.assertNotEqual(b.best_ask, 101.0)                 # the per-delta repair deleted the real asks (both, the ask side is empty)

    def test_a_cross_that_is_still_there_at_the_message_end_counts_once_and_is_repaired_like_before(self):
        a, p = book(True), book(False)
        for b in (a, p):
            b.apply_delta("bid", 101.5, 3.0, "update", 2.0)           # a bid above the best ask 101, one delta, still crossed at the end
        a.end_message()
        self.assertEqual((a.crossed_events, a.best_bid, a.best_ask), (p.crossed_events, p.best_bid, p.best_ask))
        self.assertEqual(a.crossed_events, 1)

    def test_end_message_without_a_delta_does_nothing_and_a_snapshot_clears_a_pending_cross(self):
        b = book(True)
        b.end_message()
        self.assertEqual(b.crossed_events, 0)
        b.apply_delta("bid", 101.5, 3.0, "update", 2.0)
        b.apply_snapshot([[99.0, 5.0]], [[101.0, 5.0]], 3.0)
        b.end_message()
        self.assertEqual((b.crossed_events, b.best_bid, b.best_ask), (0, 99.0, 101.0))


def ev(kind, venue, t, ets, **payload):
    return SimpleNamespace(kind=kind, venue=venue, receive_ts=t, exchange_ts=ets, payload=payload)


class FrameMarking(unittest.TestCase):
    def test_a_message_is_consecutive_deltas_of_one_venue_with_the_same_exchange_and_receive_time(self):
        h = Harness(cfg_kw={"message_atomic_book": True})
        d = lambda v, t, e: ev(DELTA, v, t, e, side="bid", price=99.0, volume=1.0)
        evs = [d(BYBIT, 1.0, 1.0), d(BYBIT, 1.0, 1.0), d(BYBIT, 1.0, 1.0),          # one message of three deltas
               d(BYBIT, 1.1, 1.1),                                                  # a message of one
               ev(TRADE, BYBIT, 1.1, 1.1, price=100.0, volume=1.0, side="s"),       # a trade is its own event
               d(BYBIT, 1.2, 1.2), d(BINANCE, 1.2, 1.2),                            # another venue ends the message
               d(BYBIT, 1.3, 1.3), d(BYBIT, 1.3, 1.31),                             # same receive time, other exchange time: two messages
               d(BYBIT, 1.4, 1.4), d(BYBIT, 1.4 + 1e-12, 1.4),                      # the feed spaces one message's receive stamps by one float step: still one message
               d(BYBIT, 1.5, 1.4), d(BYBIT, 1.5001, 1.4)]                           # same exchange time, receive stamps 100 us apart: two messages
        seen = []
        for e in h.bt._mark_frames(iter(evs)):
            seen.append(h.bt._frame_last)
        self.assertEqual(seen, [False, False, True, True, True, True, True, True, True, False, True, True, True])

    def test_another_venues_event_inside_a_messages_span_is_applied_after_the_message(self):
        h = Harness(cfg_kw={"message_atomic_book": True})
        d = lambda v, t, e: ev(DELTA, v, t, e, side="bid", price=99.0, volume=1.0)
        evs = [d(BYBIT, 1.0, 1.0), d(BINANCE, 1.0 + 1e-7, 1.00005), d(BYBIT, 1.0 + 2e-7, 1.0), d(BYBIT, 1.0 + 4e-7, 1.0), d(BYBIT, 1.1, 1.1)]
        out = [(e.venue, h.bt._frame_last) for e in h.bt._mark_frames(iter(evs))]
        self.assertEqual(out, [(BYBIT, False), (BYBIT, False), (BYBIT, True), (BINANCE, True), (BYBIT, True)])
        self.assertEqual(h.bt.timing_stats["message_atomic_other_venue_events_moved_after_message"], 1)

    def test_a_trade_of_the_same_venue_inside_the_run_ends_the_message(self):
        h = Harness(cfg_kw={"message_atomic_book": True})
        d = lambda t: ev(DELTA, BYBIT, t, 1.0, side="bid", price=99.0, volume=1.0)
        evs = [d(1.0), ev(TRADE, BYBIT, 1.0 + 1e-7, 1.0, price=100.0, volume=1.0, side="s"), d(1.0 + 2e-7)]
        self.assertEqual([h.bt._frame_last for _ in h.bt._mark_frames(iter(evs))], [True, True, True])


class WholeMessageBeforeAnythingElse(unittest.TestCase):
    def _run(self, atomic: bool):
        h = Harness(cfg_kw={"message_atomic_book": atomic})
        h.book(0.5, BYBIT)
        h.book(0.5, BINANCE)
        calls = []
        real = h.bt.maker.on_delta
        def spy(venue, side, price, new_size, now, *a, **kw):
            calls.append((side, price, new_size, h.bt.books[venue].best_ask, h.bt.books[venue].crossed_events))
            return real(venue, side, price, new_size, now, *a, **kw)
        h.bt.maker.on_delta = spy
        msg = [ev(DELTA, BYBIT, 1.0, 1.0, side="bid", price=102.0, volume=1.0, event_type="update"),
               ev(DELTA, BYBIT, 1.0, 1.0, side="bid", price=102.0, volume=0.0, event_type="delete")]
        for e in h.bt._mark_frames(iter(msg)):
            h.bt._advance_to(e.receive_ts)
            h.bt._apply_event(e)
        return h, calls

    def test_maker_queue_updates_run_after_the_whole_message_with_the_sizes_the_deltas_carried(self):
        h, calls = self._run(True)
        self.assertEqual([(c[0], c[1], c[2]) for c in calls], [("bid", 102.0, 1.0), ("bid", 102.0, 0.0)])     # both, in arrival order
        self.assertTrue(all(c[3] == 101.0 and c[4] == 0 for c in calls))                                          # both saw the completed book: ask 101 intact, no cross
        self.assertEqual(h.bt.timing_stats["message_atomic_multi_delta_messages"], 1)

    def test_with_the_switch_off_each_delta_is_repaired_and_handed_over_on_its_own(self):
        h, calls = self._run(False)
        self.assertEqual(len(calls), 2)
        self.assertGreater(calls[0][4], 0)                                       # the first delta's cross was already counted and repaired
        self.assertNotEqual(calls[1][3], 101.0)                                  # and the real ask is gone
        self.assertNotIn("message_atomic_multi_delta_messages", h.bt.timing_stats)


if __name__ == "__main__":
    unittest.main()
