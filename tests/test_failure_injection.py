"""Audit Step 12: failure-injection scenarios that had no test.

* a fill arriving while the feed-control cancel is in flight (OMS);
* exposure reservations during the cancel window; new risk refused on the invalid
  venue by the OMS itself (not only by the book gate); recovery restores trading;
* one venue healthy and the other invalid, in both directions (book gate);
* a process restart after a previously VALID book (live service);
* randomized long sequences with injected faults against an independent reference
  book, for Binance spot, Binance USD-M and Bybit continuity rules (reproducible seeds).
"""

import asyncio
from dataclasses import replace
from decimal import Decimal
import inspect
import json
from pathlib import Path
import random
import tempfile
import unittest

from crypto_stat_arb.book_gate import BookIntegrityGate
from crypto_stat_arb.execution import StrategyDirectedRouter
from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.live_feed import LiveFeedSettings, LiveOrderBookService
from crypto_stat_arb.models import BookState, MarketEvent, OrderState, PlanState
from crypto_stat_arb.oms import OMS
from crypto_stat_arb.orderbook import OrderBook
from crypto_stat_arb.session import TradingSession
from test_execution_realism import fixed_model
from test_live_feed import FakeConnector, FakeWebSocket, StepClock
from test_oms import instrument_master, opportunity, risk

ROOT = Path(__file__).parents[1]
MS = 1_000_000
INSTRUMENT = "BTC-USDT-SPOT"


def fresh_books(ts_ns):
    def book(venue, bid, ask):
        b = OrderBook(venue, INSTRUMENT)
        b.apply(MarketEvent.from_dict({
            "event_type": "BOOK_SNAPSHOT", "venue": venue, "instrument_id": INSTRUMENT,
            "exchange_ts_ns": ts_ns, "receive_ts_ns": ts_ns, "sequence": ts_ns,
            "bids": [[bid, "1"]], "asks": [[ask, "1"]]}))
        return b
    return {("BINANCE", INSTRUMENT): book("BINANCE", "59990.00", "60000.00"),
            ("BYBIT", INSTRUMENT): book("BYBIT", "60060.00", "60070.00")}


def events_of(ledger, kind):
    return [e for e in ledger.events if e["event_type"] == kind]


class FeedInvalidationWithOrdersInFlightTests(unittest.TestCase):
    def setUp(self):
        self.ledger, self.engine = Ledger(), risk()
        self.router = StrategyDirectedRouter(instrument_master(), latency_model=fixed_model())
        self.oms = OMS(instrument_master(), self.engine, self.ledger, router=self.router)

    def test_fill_arriving_during_the_cancel_window_is_tracked(self):
        plan = self.oms.execute(opportunity(), fresh_books(1))
        bybit = next(o for o in plan.children if o.venue == "BYBIT")
        self.assertTrue(self.router.has_pending_order(bybit.order_id))            # in flight
        reservation = self.engine.reservations[plan.opportunity.opportunity_id]

        cancelled = self.oms.apply_feed_control("BYBIT", False, "BYBIT:GAP_DETECTED:sequence_gap", 1 * MS)

        # 1. eligibility: the venue is risk-reducing only; 5. diagnostics recorded
        self.assertEqual(cancelled, 1)
        self.assertFalse(self.router.route_status["BYBIT"].venue_enabled)
        transition = events_of(self.ledger, "FEED_CONTROL_STATE_TRANSITION")[-1]
        self.assertEqual((transition["mode"], transition["new_risk_allowed"]), ("RISK_REDUCING_ONLY", False))
        self.assertEqual(events_of(self.ledger, "FEED_CONTROL_VENUE_CANCEL")[-1]["cancelled_orders"], 1)
        # 4. exposure stays reserved while the cancel is in flight
        self.assertTrue(reservation.active)

        self.oms.drain(1_000 * MS)

        # The order reached the venue before the cancel and filled: the fill is booked,
        # the cancel is rejected (target not open) and re-queried, the plan closes hedged.
        self.assertEqual((bybit.state, bybit.filled_quantity), (OrderState.FILLED, Decimal("0.01")))
        self.assertEqual(len([e for e in events_of(self.ledger, "FILL") if e.get("order_id") == bybit.order_id]), 1)
        self.assertTrue(events_of(self.ledger, "CANCEL_REJECTED_ORDER_REQUERIED"))
        self.assertEqual(plan.state, PlanState.CLOSED)
        self.assertFalse(self.engine.kill_switch)
        self.assertFalse(reservation.active)                                     # released once resolved

    def test_new_risk_on_the_invalid_venue_is_refused_by_the_oms_and_restored_after_recovery(self):
        self.oms.execute(opportunity(), fresh_books(1))
        self.oms.drain(100 * MS)
        self.oms.apply_feed_control("BYBIT", False, "BYBIT:STALE:market_data_stale", 200 * MS)

        refused = self.oms.execute(replace(opportunity(), opportunity_id="op-during", observed_ts_ns=300 * MS),
                                   fresh_books(300 * MS))
        self.assertEqual(self.engine.last_pretrade_validation.decision.value, "REJECTED")
        failed = [(c.code, c.venue) for c in self.engine.last_pretrade_validation.checks if not c.passed]
        self.assertEqual(failed, [("VENUE_STATUS", "BYBIT")])                       # only the invalid venue
        self.assertTrue(all(o.filled_quantity == 0 for o in refused.children))

        self.oms.apply_feed_control("BYBIT", True, "", 2_000 * MS)               # 7. valid data restores
        restored = self.oms.execute(replace(opportunity(), opportunity_id="op-after", observed_ts_ns=2_100 * MS),
                                    fresh_books(2_100 * MS))
        self.oms.drain(3_000 * MS)
        self.assertEqual(self.engine.last_pretrade_validation.decision.value, "APPROVED")
        self.assertEqual({o.state for o in restored.children}, {OrderState.FILLED})


class VenueAsymmetryTests(unittest.TestCase):
    """A healthy venue never makes a second, invalid venue acceptable (both directions)."""

    def check(self, bad):
        good = "BYBIT" if bad == "BINANCE" else "BINANCE"
        transitions = []
        gate = BookIntegrityGate(1_000 * MS, lambda reason, now: 0,
                                 venue_control_callback=lambda v, ok, why, now: transitions.append((v, ok)) or 0)
        books = {venue: fresh_books(1)[(venue, INSTRUMENT)] for venue in ("BINANCE", "BYBIT")}
        self.assertTrue(gate.allow(tuple(books.values()), 1))
        books[bad].state, books[bad].invalid_reason = BookState.GAP_DETECTED, "sequence_gap:1->3-3"
        self.assertFalse(gate.allow(tuple(books.values()), 2))
        self.assertEqual((gate.venue_state(bad), gate.venue_state(good)), ("RISK_REDUCING_ONLY", "ACTIVE"))
        self.assertIn(f"{bad}:GAP_DETECTED", gate.reason)
        self.assertEqual(transitions[-1], (bad, False))
        books[bad] = fresh_books(3)[(bad, INSTRUMENT)]                            # recovered by a snapshot
        self.assertTrue(gate.allow(tuple(books.values()), 3))
        self.assertEqual(gate.venue_state(bad), "ACTIVE")

    def test_binance_healthy_bybit_invalid(self):
        self.check("BYBIT")

    def test_bybit_healthy_binance_invalid(self):
        self.check("BINANCE")

    def test_decision_path_has_no_suspension_point(self):
        # Recovery cannot complete "during" a strategy evaluation: book apply, gate and
        # strategy.evaluate run in one synchronous call on the event loop.
        self.assertFalse(inspect.iscoroutinefunction(TradingSession.on_event))
        self.assertFalse(inspect.iscoroutinefunction(LiveOrderBookService._apply_and_record))


class RestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_after_a_valid_book_waits_for_a_fresh_snapshot(self):
        snapshot = json.dumps({"topic": "orderbook.50.BTCUSDT", "type": "snapshot", "ts": 1000,
                               "data": {"s": "BTCUSDT", "u": 10, "b": [["100", "1"]], "a": [["101", "1"]]}})
        delta = lambda u: json.dumps({"topic": "orderbook.50.BTCUSDT", "type": "delta", "ts": 1000 + u,  # noqa: E731
                                      "data": {"s": "BTCUSDT", "u": u, "b": [["100", "2"]], "a": []}})
        with tempfile.TemporaryDirectory() as directory:
            settings = LiveFeedSettings(venues=("BYBIT",), max_events=2, durable=False)
            first = LiveOrderBookService(ROOT / "config/research.json", Path(directory), "before",
                                         settings, websocket_connector=FakeConnector({"BYBIT": [
                                             FakeWebSocket([snapshot, delta(11)])]}), clock_ns=StepClock())
            await asyncio.wait_for(first.run(), timeout=5)
            self.assertEqual(first.books["BYBIT"].state, BookState.VALID)

            # New process: nothing is carried over; the book starts unsynchronised and the
            # gate suspended, a delta alone cannot make it VALID.
            cancels = []
            second = LiveOrderBookService(ROOT / "config/research.json", Path(directory), "after",
                                          LiveFeedSettings(venues=("BYBIT",), durable=False),
                                          clock_ns=StepClock(), cancel_callback=lambda r, n: cancels.append(r) or 0)
            book = second.books["BYBIT"]
            self.assertEqual((book.state, book.sequence), (BookState.SYNCING, None))
            self.assertTrue(second.book_gate.suspended)
            second._process_websocket_frame("BYBIT", "wss://bybit", delta(12), channel="orderbook.50.BTCUSDT")
            self.assertNotEqual(book.state, BookState.VALID)
            self.assertTrue(second.book_gate.suspended)
            second._process_websocket_frame("BYBIT", "wss://bybit", snapshot.replace('"u": 10', '"u": 20'),
                                            channel="orderbook.50.BTCUSDT")
            self.assertEqual(book.state, BookState.VALID)
            self.assertFalse(second.book_gate.suspended)
            second.recorder.close({})
            second.ohlc_writer.close()
            second.ltp_writer.close()


# -- randomized long sequences -------------------------------------------------------------
class _Feed:
    """True message stream for one venue rule; levels stay uncrossed by construction."""

    def __init__(self, rule, rnd):
        self.rule, self.rnd, self.u = rule, rnd, 1000
        self.bids, self.asks = {}, {}

    def _levels(self):
        bids = {Decimal(100 - i) / 1: Decimal(self.rnd.randint(1, 5)) for i in range(1, 6)}
        asks = {Decimal(100 + i) / 1: Decimal(self.rnd.randint(1, 5)) for i in range(1, 6)}
        return bids, asks

    def snapshot(self):
        self.bids, self.asks = self._levels()
        self.u += 7
        return self._event("BOOK_SNAPSHOT", self.u, self.u, dict(self.bids), dict(self.asks), None)

    def delta(self):
        bids, asks = {}, {}
        for levels, out, base, sign in ((self.bids, bids, 100, -1), (self.asks, asks, 100, 1)):
            price = Decimal(base + sign * self.rnd.randint(1, 6))
            quantity = Decimal(0) if price in levels and self.rnd.random() < 0.3 else Decimal(self.rnd.randint(1, 9))
            out[price] = quantity
            if quantity == 0:
                levels.pop(price, None)
            else:
                levels[price] = quantity
        previous = self.u
        if self.rule == "BINANCE_SPOT":           # U = previous u + 1, ranged
            start, self.u = previous + 1, previous + self.rnd.randint(1, 4)
        elif self.rule == "BINANCE_USDM":         # U jumps; continuity is pu
            self.u = previous + self.rnd.randint(2, 9)
            start = self.u - self.rnd.randint(0, 1)
        else:                                      # Bybit: u + 1
            start = self.u = previous + 1
        return self._event("BOOK_DELTA", start, self.u, bids, asks, previous)

    def _event(self, kind, start, end, bids, asks, previous):
        native = {"pu": previous} if self.rule == "BINANCE_USDM" and previous is not None else {}
        return MarketEvent.from_dict({
            "event_id": f"{self.rule}-{kind}-{end}", "event_type": kind,
            "venue": "BYBIT" if self.rule == "BYBIT" else "BINANCE", "instrument_id": INSTRUMENT,
            "exchange_ts_ns": end, "receive_ts_ns": end, "sequence": end, "sequence_start": start,
            "bids": [[str(p), str(q)] for p, q in bids.items()], "asks": [[str(p), str(q)] for p, q in asks.items()],
            "native": native}), (dict(self.bids), dict(self.asks))


def run_sequence(rule, seed, length=400):
    """Feed a faulty stream to OrderBook; return a list of invariant violations (empty = pass)."""
    rnd = random.Random(seed)
    feed = _Feed(rule, rnd)
    truth = []                                     # (event, true book after it)
    for i in range(length):
        truth.append(feed.snapshot() if i == 0 or rnd.random() < 0.02 else feed.delta())
    reference = {event.sequence: state for event, state in truth}
    faulty, faults = [], []
    i = 0
    while i < len(truth):
        event, _ = truth[i]
        roll = rnd.random()
        if event.event_type.value == "BOOK_DELTA" and roll < 0.03:
            faults.append(("drop", event.sequence))
        elif event.event_type.value == "BOOK_DELTA" and roll < 0.05 and i + 1 < len(truth):
            faulty += [truth[i + 1][0], event]                                 # swap with the next
            faults.append(("swap", event.sequence))
            i += 2
            continue
        elif roll < 0.07:
            faulty += [event, event]                                           # duplicate
        elif event.event_type.value == "BOOK_DELTA" and roll < 0.08:
            faulty.append(replace(event, bids=(replace(event.bids[0], quantity=Decimal("NaN")),) + event.bids[1:]))
            faults.append(("nan", event.sequence))
        else:
            faulty.append(event)
        i += 1
    book = OrderBook("VENUE", INSTRUMENT, max_mid_jump_bps=None, max_spread_bps=None)
    violations = []
    for event in faulty:
        book.venue = event.venue
        state = book.apply(event)
        if state == BookState.VALID:
            bids, asks = reference[book.sequence]
            if dict(book.bids) != bids or dict(book.asks) != asks:
                violations.append(f"VALID at u={book.sequence} but levels differ from the true book")
            if book.best_bid >= book.best_ask:
                violations.append(f"VALID and crossed at u={book.sequence}")
    return violations, faults


class RandomizedSequenceTests(unittest.TestCase):
    SEEDS = range(150)

    def test_a_valid_book_always_equals_the_true_book(self):
        for rule in ("BINANCE_SPOT", "BINANCE_USDM", "BYBIT"):
            injected = 0
            for seed in self.SEEDS:
                violations, faults = run_sequence(rule, seed)
                injected += len(faults)
                self.assertEqual(violations, [], f"rule={rule} seed={seed} faults={faults[:8]}")
            self.assertGreater(injected, 100, rule)                 # the faults really happened

    def test_the_check_detects_a_book_that_trusts_a_gap(self):
        # Guard on the guard: with gap detection disabled the same harness must fail.
        original = OrderBook._breaks_chain
        try:
            OrderBook._breaks_chain = lambda self, delta: False
            failures = sum(bool(run_sequence("BYBIT", seed)[0]) for seed in range(20))
        finally:
            OrderBook._breaks_chain = original
        self.assertGreater(failures, 0)


if __name__ == "__main__":
    unittest.main()
