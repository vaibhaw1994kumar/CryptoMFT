"""Replay order under a backwards wall-clock step (capture 2026-10-22, Bybit linear)."""

import random
import unittest

from crypto_stat_arb.models import BookState, MarketEvent
from crypto_stat_arb.orderbook import OrderBook
from crypto_stat_arb.replay import deterministic_order, event_order_key
from crypto_stat_arb.time_integrity import TimeIntegrityMonitor, TimePolicy

PERP = "BTC-USDT-PERP"


def book_event(venue, sequence, receive, *, snapshot=False, exchange=None, start=None, native=None):
    return MarketEvent.from_dict({
        "event_id": f"{venue}-{sequence}-{'s' if snapshot else 'd'}",
        "event_type": "BOOK_SNAPSHOT" if snapshot else "BOOK_DELTA",
        "venue": venue, "instrument_id": PERP,
        "exchange_ts_ns": exchange if exchange is not None else sequence * 20,
        "receive_ts_ns": receive, "sequence": sequence,
        "sequence_start": start if start is not None else sequence,
        "bids": [["100", "1"]], "asks": [["101", "1"]],
        "native": native or {},
    })


def replay_states(events):
    books = {}
    for e in deterministic_order(events):
        book = books.setdefault(e.venue, OrderBook(e.venue, PERP))
        book.apply(e)
    return books


def clock_step_stream(venue="BYBIT"):
    # As recorded: u=677 was stamped 62 ms *earlier* than u=673..676 (host clock stepped back).
    receives = {670: 1_000, 671: 1_020, 672: 1_040, 673: 1_060, 674: 1_080, 675: 1_100, 676: 1_120,
                677: 1_058, 678: 1_078, 679: 1_098, 680: 1_118}
    events = [book_event(venue, 669, 990, snapshot=True)]
    events += [book_event(venue, u, t) for u, t in receives.items()]
    return events


class ReplayOrderTests(unittest.TestCase):
    def test_backwards_clock_step_no_longer_creates_a_false_gap(self):
        events = clock_step_stream()
        naive = sorted(events, key=event_order_key)                # the previous ORDERING_RULE-only order
        self.assertNotEqual([e.sequence for e in naive], sorted(e.sequence for e in naive))
        ordered = deterministic_order(events)
        self.assertEqual([e.sequence for e in ordered], list(range(669, 681)))
        book = replay_states(events)["BYBIT"]
        self.assertEqual((book.state, book.stats.gaps, book.sequence), (BookState.VALID, 0, 680))

    def test_timestamps_are_not_modified(self):
        events = clock_step_stream()
        by_id = {e.event_id: (e.receive_ts_ns, e.exchange_ts_ns, e.source_ts_ns) for e in events}
        for e in deterministic_order(events):
            self.assertEqual((e.receive_ts_ns, e.exchange_ts_ns, e.source_ts_ns), by_id[e.event_id])

    def test_a_genuine_gap_is_still_detected(self):
        events = [e for e in clock_step_stream() if e.sequence != 674]   # really lost
        book = replay_states(events)["BYBIT"]
        self.assertEqual(book.stats.gaps, 1)
        self.assertIn("sequence_gap:673->675", book.invalid_reason)

    def test_binance_futures_pu_chain_survives_a_clock_step(self):
        def delta(u, receive):                                     # U jumps; continuity is pu == previous u
            return book_event("BINANCE", u * 10, receive, start=u * 10 - 12, native={"pu": (u - 1) * 10})
        events = [book_event("BINANCE", 100, 990, snapshot=True, start=100)]
        # first delta bridges lastUpdateId=100 (U=98 <= 100 <= u=110); 14 is stamped early
        events += [delta(11, 1_000), delta(12, 1_020), delta(13, 1_040), delta(14, 1_010)]
        book = replay_states(events)["BINANCE"]
        self.assertEqual((book.state, book.stats.gaps, book.sequence), (BookState.VALID, 0, 140))

    def test_clean_data_keeps_the_ordering_rule_order(self):
        events = [book_event("BINANCE", 10, 5, snapshot=True)]
        events += [book_event("BINANCE", s, 10 + s) for s in range(11, 20)]
        events += [book_event("BYBIT", 500, 7, snapshot=True)]
        events += [book_event("BYBIT", s, 3 * s - 1_480) for s in range(501, 510)]
        self.assertEqual(deterministic_order(events), sorted(events, key=event_order_key))

    def test_cross_venue_interleaving_still_follows_receive_time(self):
        events = clock_step_stream("BYBIT") + [book_event("BINANCE", 1, 1_059, snapshot=True),
                                              book_event("BINANCE", 2, 1_061)]
        ordered = deterministic_order(events)
        binance = [i for i, e in enumerate(ordered) if e.venue == "BINANCE"]
        naive = [i for i, e in enumerate(sorted(events, key=event_order_key)) if e.venue == "BINANCE"]
        self.assertEqual(binance, naive)                           # other venue's slots unchanged

    def test_deltas_are_not_moved_across_a_snapshot(self):
        events = [book_event("BYBIT", 100, 1_000, snapshot=True), book_event("BYBIT", 101, 1_010),
                  book_event("BYBIT", 90, 1_020, snapshot=True),   # venue restart: sequence reset
                  book_event("BYBIT", 91, 1_030), book_event("BYBIT", 92, 1_040)]
        self.assertEqual([e.sequence for e in deterministic_order(events)], [100, 101, 90, 91, 92])

    def test_order_is_deterministic_for_any_input_order(self):
        events = clock_step_stream() + [book_event("BINANCE", 5, 1_050, snapshot=True)]
        expected = deterministic_order(events)
        for seed in range(20):
            shuffled = list(events)
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(deterministic_order(shuffled), expected)

    def test_negative_latency_is_still_diagnosed_on_reordered_events(self):
        # u=677 keeps its recorded receive time (58 ns before its exchange time here): still flagged.
        events = [e for e in clock_step_stream() if e.sequence != 677]
        events.append(book_event("BYBIT", 677, 1_058, exchange=1_116))
        monitor = TimeIntegrityMonitor(TimePolicy(), ("BYBIT",))
        flagged = []
        for e in deterministic_order(events):
            assessment = monitor.observe(e, now_ns=e.receive_ts_ns)
            if assessment.blocking:
                flagged.append(e.sequence)
        self.assertIn(677, flagged)


if __name__ == "__main__":
    unittest.main()
