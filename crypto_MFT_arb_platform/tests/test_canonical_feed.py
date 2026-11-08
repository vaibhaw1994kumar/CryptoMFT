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

    def test_the_data_quality_scan_counts_messages_by_the_same_rule_as_the_frames(self):
        """Phase B's scan and the converter once grouped by two spellings of the rule (exact receive stamp vs within SAME_STAMP_S); both now call `continues_message`, so on one file they count the same messages."""
        import gzip, json, tempfile
        from new_backtest.quality_preflight import scan_venue_day_checks
        rows = [  # (exchange_ts, local_ts, side, price, volume): message A has three deltas (stamps 0.5 us apart), B starts 5 us later at the same exchange time, C has a new exchange time
            (100.0, 100.002, "b", 10.0, 1.0), (100.0, 100.0020005, "b", 9.9, 2.0), (100.0, 100.002, "a", 10.1, 1.0),
            (100.0, 100.002005, "b", 9.8, 1.0), (101.0, 101.002, "a", 10.2, 3.0)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for stream, lines in (("book_snapshots", [{"timestamp": 99.0, "local_ts": 99.002, "bids": [[10.0, 1.0]], "asks": [[10.1, 1.0]]}]),
                                  ("book_events", [{"exchange_ts": x, "local_ts": l, "side": sd, "price": pr, "volume": v, "event_type": "modify"} for x, l, sd, pr, v in rows])):
                d = root / "bybit" / stream / "pair=SOLUSDT" / "date=2024-03-01"
                d.mkdir(parents=True)
                with gzip.open(d / "1.jsonl.gz", "wt") as fh:
                    fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
            scanned = scan_venue_day_checks(root, "bybit", "SOLUSDT", "2024-03-01")["observation"]["messages"]
            stream = [TimedEvent(e.local_ts or e.exchange_ts, e.exchange_ts, "bybit", "SOLUSDT", e.kind, e.payload) for e in iter_venue_events(root, "bybit", "SOLUSDT", "2024-03-01", "2024-03-01")]
            framed = sum(1 for f in message_frames(stream) if f[0].kind == DELTA)
        self.assertEqual((scanned, framed), (3, 3))

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
        def take(evs, out):
            """The first `limit` records, then the rest of the exchange message the cut fell inside: a cut through a message leaves a half-applied message (a crossed book that is an artifact of the cut, not of the data)."""
            for ev in evs:
                if len(out) >= limit and not (ev.kind == DELTA and out[-1].kind == DELTA and ev.exchange_ts == out[-1].exchange_ts):
                    break
                out.append(ev)
        take(iter_venue_events(SLICES, "bybit", "SOLUSDT", day, day), raw)
        take(iter_venue_events(SLICES, "bybit", "SOLUSDT", day, day, decimal_numbers=True), exact_raw)
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
        self.assertEqual(crossed_messages, 0, "complete messages: Phase B's scan found none crossed at message end on the March days; a cross here means the cut or the grouping is wrong")
        self.assertGreater(checked, 50)
        self.assertLess(worst, 1e-9)


class ReplayInvalidBookPolicy(unittest.TestCase):
    """Phase D (owner 8 Nov): one rule for a book that goes invalid with no snapshot in the data to repair it: trading suspended, resubscribe and resync after an ASSUMED delay from the data's own
    state (nothing later than the resync time), suspended seconds counted, nothing frozen for the rest of the slice. Synthetic wiring tests; no profit figure."""
    VENUES = {"bybit": "BYBIT", "binance": "BINANCE"}
    INSTRUMENT = "BTC-USDT-PERP"

    def _stream(self):
        snap = lambda v: timed(BOOK, v, 1.0, {"timestamp": 1.0, "bids": [["100.0", "5"]], "asks": [["100.1", "4"], ["100.3", "2"]]})
        bybit = [delta("bybit", 2.0, "b", "100.2", "3"),                 # crosses the ask at 100.1: the message ends crossed
                 delta("bybit", 3.0, "a", "100.4", "1"),
                 delta("bybit", 6.5, "b", "100.15", "2"),
                 delta("bybit", 8.0, "a", "100.35", "1"),                # after the resync is due (2.002 + 5 = 7.002)
                 delta("bybit", 9.0, "a", "100.36", "1")]
        binance = [delta("binance", 1.0 + 0.4 * i, "a", "100.5", str(i + 1)) for i in range(1, 21)]      # a live book on the other venue (it never goes stale)
        return [snap("bybit"), snap("binance")] + sorted(bybit + binance, key=lambda e: (e.exchange_ts, e.venue))

    def _session(self, delay_s, feed):
        from crypto_stat_arb.config import load_config
        from crypto_stat_arb.session import ExecutionMode, TradingSession
        cfg, _ = load_config(Path(__file__).resolve().parents[1] / "config/perp_research.json")
        kw = {} if delay_s is None else dict(resync_delay_ns=int(delay_s * 1e9), resync_source=feed.resync_snapshot)
        return TradingSession(cfg, execution_mode=ExecutionMode.INTERNAL_SIMULATION, strategies=[], **kw)

    def _run(self, delay_s):
        feed = CanonicalFeed(self.INSTRUMENT, venue_names=self.VENUES)
        session = self._session(delay_s, feed)
        states, last = [], 0
        for event in feed.convert(self._stream()):
            session.on_event(event)
            last = event.receive_ts_ns
            states.append((event.venue, event.exchange_ts_ns / 1e9, session.books[(event.venue, self.INSTRUMENT)].state.value))
        return feed, session, states, last

    def test_the_book_waits_the_assumed_delay_then_resyncs_and_the_suspension_is_counted(self):
        feed, session, states, last = self._run(5.0)
        bybit = {t: st for v, t, st in states if v == "BYBIT"}
        self.assertEqual((bybit[2.0], bybit[3.0], bybit[6.5]), ("CHECKSUM_FAILED", "CHECKSUM_FAILED", "CHECKSUM_FAILED"))     # suspended, not frozen: the stream goes on
        self.assertEqual((bybit[8.0], bybit[9.0]), ("VALID", "VALID"))
        report = session.book_policy_report(last)
        self.assertEqual((report["invalid_book_policy"], report["resync_delay_s"], report["resync_delay_label"]), ("RESUBSCRIBE_AFTER_ASSUMED_DELAY", 5.0, "ASSUMED"))
        self.assertEqual(report["resyncs_by_source"], {"RESYNC_SOURCE_ASSUMED_DELAY": 1})
        (resync,) = report["resyncs"]
        self.assertAlmostEqual(resync["wait_ns"] / 1e9, 5.0, places=6)           # invalid since 2.002; due at 7.002; any venue's first event at or after that time triggers it (Binance's at 7.002)
        (day,) = report["suspended_seconds_by_day"].items()
        self.assertEqual(day[0], "1970-01-01")
        self.assertAlmostEqual(day[1]["BYBIT:CHECKSUM_FAILED"], 5.0, places=6)
        self.assertAlmostEqual(report["suspended_seconds_total_by_day"]["1970-01-01"], 5.0, places=6)
        (window,) = [w for w in report["suspension_intervals_ns"] if w[2].startswith("BYBIT:")]
        self.assertAlmostEqual((window[1] - window[0]) / 1e9, 5.0, places=6)          # the windows a parity comparison masks
        self.assertEqual(feed.counts["crossed_messages"], 1)
        self.assertEqual(feed.counts["resync_snapshots"], 1)

    def test_the_resynced_book_is_the_backtesters_repaired_book_and_uses_no_later_information(self):
        from new_backtest.order_book import OrderBook as FloatBook
        feed, session, states, last = self._run(5.0)
        flt = FloatBook("bybit", "SOLUSDT", delta_semantics="hybrid", message_atomic=True)
        for frame in message_frames(self._stream()):
            head = frame[0]
            if head.venue != "bybit":
                continue
            if head.kind == BOOK:
                flt.apply_snapshot([[float(p), float(q)] for p, q in head.payload["bids"]], [[float(p), float(q)] for p, q in head.payload["asks"]], head.exchange_ts)
            else:
                for ev in frame:
                    p = ev.payload
                    flt.apply_delta(p["side"], float(p["price"]), float(p["volume"]), p["event_type"], ev.exchange_ts)
                flt.end_message()
        core = session.books[("BYBIT", self.INSTRUMENT)]
        self.assertEqual(sorted((float(p), float(q)) for p, q in core.bids.items()), sorted(flt.levels("bid", 50)))
        self.assertEqual(sorted((float(p), float(q)) for p, q in core.asks.items()), sorted(flt.levels("ask", 50)))
        # the snapshot is built at the resync time from what had been read: at the event at 8.0 the feed has read 8.0 and not 9.0
        feed2 = CanonicalFeed(self.INSTRUMENT, venue_names=self.VENUES)
        it = feed2.convert(self._stream())
        for event in it:
            if event.exchange_ts_ns == 8_000_000_000:
                break
        snapshot = feed2.resync_snapshot("BYBIT", self.INSTRUMENT, event.receive_ts_ns)
        self.assertIn(Decimal("100.35"), [lv.price for lv in snapshot.asks])
        self.assertNotIn(Decimal("100.36"), [lv.price for lv in snapshot.asks])
        self.assertEqual(snapshot.quality_flags, ("SEQUENCE_CONSTRUCTED", "RESYNC_SIMULATED"))

    def test_a_resync_snapshot_asked_about_a_time_before_what_the_feed_has_emitted_is_refused(self):
        """No later information is enforced, not incidental: the eager pattern (convert everything first, then replay) would hand every resync the end-of-slice book, so it raises."""
        feed = CanonicalFeed(self.INSTRUMENT, venue_names=self.VENUES)
        events = list(feed.convert(self._stream()))                       # eager: the feed has now read to the end
        crossing = next(e for e in events if e.venue == "BYBIT" and e.exchange_ts_ns == 2_000_000_000)
        with self.assertRaisesRegex(ValueError, "later information"):
            feed.resync_snapshot("BYBIT", self.INSTRUMENT, crossing.receive_ts_ns + 5_000_000_000)
        last = [e for e in events if e.venue == "BYBIT"][-1]
        self.assertIsNotNone(feed.resync_snapshot("BYBIT", self.INSTRUMENT, last.receive_ts_ns))      # asked about the time it has reached: fine

    def test_a_silent_venue_has_nothing_newer_to_resync_from(self):
        """Q-G2: 5 s of silence is the resubscribe trigger, but the data cannot invent the exchange's present state: no snapshot from a venue that stopped sending."""
        feed = CanonicalFeed(self.INSTRUMENT, venue_names=self.VENUES, max_silence_s=5.0)
        stream = [e for e in self._stream() if not (e.venue == "binance" and e.exchange_ts > 3.0)]                  # Binance stops at 3.0 s; Bybit goes on
        for event in feed.convert(stream):
            last = event
        self.assertIsNone(feed.resync_snapshot("BINANCE", self.INSTRUMENT, last.receive_ts_ns))
        self.assertIsNotNone(feed.resync_snapshot("BYBIT", self.INSTRUMENT, last.receive_ts_ns))

    def test_without_the_policy_the_book_stays_invalid_for_the_rest_of_the_slice(self):
        feed, session, states, last = self._run(None)
        self.assertEqual({st for v, t, st in states if v == "BYBIT" and t >= 2.0}, {"CHECKSUM_FAILED"})
        self.assertEqual(session.book_policy_report(last)["invalid_book_policy"], "WAIT_FOR_DATA_SNAPSHOT")

    def test_a_delay_and_a_source_come_together_and_the_feed_manifest_says_synthetic_and_not_measured(self):
        from crypto_stat_arb.config import load_config
        from crypto_stat_arb.session import ExecutionMode, TradingSession
        cfg, _ = load_config(Path(__file__).resolve().parents[1] / "config/perp_research.json")
        with self.assertRaisesRegex(ValueError, "go together"):
            TradingSession(cfg, execution_mode=ExecutionMode.INTERNAL_SIMULATION, resync_delay_ns=1)
        feed, _, _, _ = self._run(5.0)
        m = feed.manifest()
        self.assertEqual(m["sequence_numbers"], "SYNTHETIC")
        self.assertEqual((m["sequence_gaps"]["status"], m["sequence_gaps"]["count"]), ("NOT_MEASURED", None))


if __name__ == "__main__":
    unittest.main()
