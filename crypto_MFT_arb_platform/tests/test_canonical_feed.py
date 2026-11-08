"""Phase D, step D4: the research-feed converter. Synthetic records prove the rules; one March slice (Bybit, read only) shows the core book built from the converted events holds the
same levels as the backtester's float book."""
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crypto_stat_arb.canonical import validate_canonical_event
from crypto_stat_arb.models import EventType, MarketEvent, TradeEvent
from crypto_stat_arb.orderbook import BookState, OrderBook as CoreBook
from new_backtest import event_loader
from new_backtest.canonical_feed import CanonicalFeed, message_frames
from new_backtest.cross_venue_feed import TimedEvent
from new_backtest.event_loader import BOOK, DELTA, TRADE, iter_venue_events

SLICES = Path(__file__).resolve().parents[2] / "out" / "queue_v2" / "slices_mar2024"
INSTR = "SOL-USDT-PERP"


def timed(kind, venue, exchange_ts, payload, receive=None):
    return TimedEvent(exchange_ts + 0.002 if receive is None else receive, exchange_ts, venue, "SOLUSDT", kind, payload)


def delta(venue, ts, side, price, volume, event_type="modify", receive=None):
    return timed(DELTA, venue, ts, {"exchange_ts": ts, "local_ts": 0.0, "side": side, "price": price, "volume": volume, "event_type": event_type}, receive)


class ConverterRules(unittest.TestCase):
    def test_numbers_are_read_from_text_only_when_asked_and_the_request_never_leaks_into_other_readers(self):
        import gzip, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            d = root / "bybit" / "book_snapshots" / "pair=SOLUSDT" / "date=2024-03-01"
            d.mkdir(parents=True)
            with gzip.open(d / "1.jsonl.gz", "wt") as fh:
                fh.write('{"timestamp": 1709251200.123456789, "bids": [[100.12345678901234567, 0.30000000000000004]], "asks": [[100.2, 1.5]]}\n')
                fh.write('{"timestamp": 1709251201.5, "bids": [[100.1, 2.5]], "asks": [[100.2, 1.5]]}\n')
            plain = next(iter_venue_events(root, "bybit", "SOLUSDT", "2024-03-01", "2024-03-01"))
            self.assertIsInstance(plain.payload["bids"][0][0], float)
            exact_stream = (TimedEvent(e.exchange_ts + 0.002, e.exchange_ts, "bybit", "SOLUSDT", e.kind, e.payload)
                            for e in iter_venue_events(root, "bybit", "SOLUSDT", "2024-03-01", "2024-03-01", decimal_numbers=True))
            converter = CanonicalFeed(INSTR, venue_names={"bybit": "BYBIT"}).convert(exact_stream)
            first = next(converter)                                                       # the converter is now suspended between two events
            self.assertEqual((first.bids[0].price, first.bids[0].quantity), (Decimal("100.12345678901234567"), Decimal("0.30000000000000004")))
            again = next(iter_venue_events(root, "bybit", "SOLUSDT", "2024-03-01", "2024-03-01"))          # any other reader in that window still gets floats
            self.assertIsInstance(again.payload["bids"][0][0], float)
            converter.close()                                                              # an abandoned converter changes nothing for anyone else
            self.assertIsInstance(next(iter_venue_events(root, "bybit", "SOLUSDT", "2024-03-01", "2024-03-01")).payload["bids"][0][0], float)

    def test_a_record_whose_numbers_are_floats_is_refused(self):
        stream = [timed(BOOK, "bybit", 1.0, {"timestamp": 1.0, "bids": [[100.0, 5.0]], "asks": [[100.1, 4.0]]})]
        with self.assertRaisesRegex(ValueError, "decimal_numbers=True"):
            list(CanonicalFeed(INSTR).convert(stream))

    def test_the_backtester_runs_the_same_grouping_rule_as_the_converter(self):
        """`_mark_frames` delegates to `message_frames`: same frames, the last-of-message flags follow them, and the moved-event count is the same one."""
        from collections import Counter
        from new_backtest.backtester import CrossVenueArbBacktester
        a = [delta("bybit", 2.0, "bid", Decimal("100"), Decimal(i + 1), receive=10.0 + i * 1e-7) for i in range(4)]
        other = timed(TRADE, "binance", 2.0, {"exchange_ts": 2.0, "price": Decimal("100"), "volume": Decimal("1"), "side": "b"}, receive=10.0 + 1.5e-7)
        snap = timed(BOOK, "bybit", 1.0, {"timestamp": 1.0, "bids": [], "asks": []}, receive=9.0)
        later = delta("bybit", 3.0, "bid", Decimal("100"), Decimal("1"), receive=10.5)
        stream = [snap, a[0], a[1], other, a[2], a[3], later]
        moved = []
        frames = list(message_frames(stream, on_moved=moved.append))

        class Stub:
            timing_stats = Counter()
            _frame_last = None
        stub, seen = Stub(), []
        for ev in CrossVenueArbBacktester._mark_frames(stub, stream):
            seen.append((ev, stub._frame_last))
        flat = [(ev, k == len(f) - 1) for f in frames for k, ev in enumerate(f)]
        self.assertEqual([(id(e), last) for e, last in seen], [(id(e), last) for e, last in flat])
        self.assertEqual(stub.timing_stats["message_atomic_other_venue_events_moved_after_message"], sum(moved))
        self.assertEqual(sum(moved), 1)

    def test_deltas_of_one_message_become_one_event_with_new_absolute_sizes_and_cancel_is_decremental(self):
        stream = [
            timed(BOOK, "bybit", 1.0, {"timestamp": 1.0, "bids": [["100.00", "5"]], "asks": [["100.10", "4"]]}),
            delta("bybit", 2.0, "bid", Decimal("100.00"), Decimal("2"), "cancel"),         # 5 - 2 = 3
            delta("bybit", 2.0, "bid", Decimal("99.90"), Decimal("7"), "add"),
            delta("bybit", 2.0, "ask", Decimal("100.10"), Decimal("0"), "delete"),
            delta("bybit", 3.0, "bid", Decimal("100.00"), Decimal("9"), "modify"),
        ]
        events = list(CanonicalFeed(INSTR, venue_names={"bybit": "BYBIT"}).convert(stream))
        self.assertEqual([e.event_type for e in events], [EventType.BOOK_SNAPSHOT, EventType.BOOK_DELTA, EventType.BOOK_DELTA])
        message = events[1]
        self.assertEqual([(l.price, l.quantity) for l in message.bids], [(Decimal("100.00"), Decimal("3")), (Decimal("99.90"), Decimal("7"))])
        self.assertEqual([(l.price, l.quantity) for l in message.asks], [(Decimal("100.10"), Decimal("0"))])
        self.assertEqual([e.sequence for e in events], [1, 2, 3])                        # constructed, contiguous
        self.assertTrue(all("SEQUENCE_CONSTRUCTED" in e.quality_flags for e in events))
        self.assertEqual(events[2].bids[0].quantity, Decimal("9"))
        for e in events:
            validate_canonical_event(e)

    def test_another_venues_event_inside_a_message_is_held_until_after_it(self):
        a = [delta("bybit", 2.0, "bid", Decimal("100"), Decimal(i + 1), receive=10.0 + i * 1e-7) for i in range(3)]
        other = timed(TRADE, "binance", 2.0, {"exchange_ts": 2.0, "price": Decimal("100"), "volume": Decimal("1"), "side": "b"}, receive=10.0 + 1.5e-7)
        frames = list(message_frames([a[0], a[1], other, a[2]]))
        self.assertEqual([[e.kind for e in f] for f in frames], [[DELTA, DELTA, DELTA], [TRADE]])
        later = delta("bybit", 3.0, "bid", Decimal("100"), Decimal("1"), receive=10.5)         # another exchange time ends the message
        self.assertEqual([len(f) for f in message_frames([a[0], later])], [1, 1])

    def test_a_trade_keeps_its_text_digits_side_and_a_constructed_id_is_flagged(self):
        stream = [timed(TRADE, "bybit", 5.0, {"exchange_ts": Decimal("5.123456789"), "price": Decimal("100.1"), "volume": Decimal("0.35"), "side": "s"})]
        (trade,) = CanonicalFeed(INSTR, venue_names={"bybit": "BYBIT"}).convert(stream)
        self.assertIsInstance(trade, TradeEvent)
        self.assertEqual((trade.price, trade.quantity, trade.aggressor_side, trade.exchange_ts_ns), (Decimal("100.1"), Decimal("0.35"), "SELL", 5_123_456_789))
        self.assertIn("TRADE_ID_CONSTRUCTED", trade.quality_flags)
        validate_canonical_event(trade)


@unittest.skipUnless((SLICES / "bybit").is_dir(), "March slice not available")
class MarchSliceAgainstTheBacktestersBook(unittest.TestCase):
    def test_core_book_from_converted_events_holds_the_same_levels_as_the_float_book(self):
        from new_backtest.order_book import OrderBook as FloatBook
        day, limit = "2024-03-01", 120_000
        raw, exact_raw = [], []
        for ev in iter_venue_events(SLICES, "bybit", "SOLUSDT", day, day):
            raw.append(ev)
            if len(raw) >= limit:
                break
        for ev in iter_venue_events(SLICES, "bybit", "SOLUSDT", day, day, decimal_numbers=True):
            exact_raw.append(ev)
            if len(exact_raw) >= limit:
                break
        wrap = lambda evs: [TimedEvent(e.exchange_ts + 0.002, e.exchange_ts, "bybit", "SOLUSDT", e.kind, e.payload) for e in evs]
        stream = wrap(raw)
        feed = CanonicalFeed(INSTR, venue_names={"bybit": "BYBIT"})
        converted = [e for e in feed.convert(wrap(exact_raw)) if isinstance(e, MarketEvent)]
        self.assertGreater(feed.counts["delta_messages"], 100)
        core = CoreBook("BYBIT", INSTR)
        flt = FloatBook("bybit", "SOLUSDT", delta_semantics="hybrid", message_atomic=True)
        book_frames = checked = not_valid = unexplained = crossed_messages = worst = 0
        it = iter(converted)
        for n, frame in enumerate(message_frames(stream)):
            head = frame[0]
            if head.kind == BOOK:
                flt.apply_snapshot(head.payload.get("bids") or [], head.payload.get("asks") or [], head.exchange_ts)
            elif head.kind == DELTA:
                crossed_before = flt.crossed_events
                for ev in frame:
                    p = ev.payload
                    flt.apply_delta(str(p.get("side", "bid")).lower(), float(p.get("price", 0) or 0), float(p.get("volume", p.get("size", 0)) or 0), str(p.get("event_type", "modify")).lower(), ev.exchange_ts)
                flt.end_message()
                crossed_messages += flt.crossed_events > crossed_before
                cross_here = flt.crossed_events > crossed_before
            else:
                continue
            book_frames += 1
            core.apply(next(it))
            if core.state != BookState.VALID:
                # PARITY FINDING (BY_DESIGN): a message that leaves the book crossed at its end makes the core book invalid until the next snapshot (Phase B rule: the core's way),
                # where the backtester's book repairs the cross. Every non-valid frame must be a frame the float book also saw crossed at message end; anything else is a bug.
                not_valid += 1
                unexplained += 0 if (head.kind == DELTA and cross_here) else 1
                continue
            if n % 5 == 0 and flt.ready:
                checked += 1
                for side in ("bid", "ask"):
                    mine = sorted(core.bids.items(), reverse=True)[:10] if side == "bid" else sorted(core.asks.items())[:10]
                    theirs = flt.levels(side, 10)
                    self.assertEqual(len(mine), len(theirs))
                    for (px, sz), (fpx, fsz) in zip(mine, theirs):
                        self.assertEqual(float(px), fpx)
                        worst = max(worst, abs(float(sz) - fsz))
        print(f"\n[canonical feed, Bybit {day}, first {limit} records] book frames {book_frames}, core book not VALID after {not_valid} (messages the float book saw crossed at message end: {crossed_messages}; not explained by a cross: {unexplained}), checkpoints compared {checked}, worst size difference {worst:.3g}")
        self.assertEqual(unexplained, 0)
        self.assertEqual(not_valid, crossed_messages)
        self.assertGreater(checked, 50)
        self.assertLess(worst, 1e-9)


if __name__ == "__main__":
    unittest.main()
