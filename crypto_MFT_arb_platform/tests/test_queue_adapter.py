"""Phase D, step D3: QueueAwareVenueAdapter. Synthetic books and prints prove the wiring only (no profit figure): a resting post-only order is placed in the queue model, fills come
from prints, a depth change never fills an order, taker requests keep the deterministic walk."""
import dataclasses
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crypto_stat_arb.execution import (
    CanonicalOrderRequest, DeterministicVenueAdapter, ExecutionEventType, OrderAction, OrderType, StrategyDirectedRouter, TimeInForce,
)
from crypto_stat_arb.models import MarketEvent, Side, TradeEvent
from crypto_stat_arb.orderbook import OrderBook
from crypto_stat_arb.simulation.fill_engine import MakerConfig, MakerExecutor
from crypto_stat_arb.simulation.queue_adapter import QueueAwareVenueAdapter
from new_backtest.fill_model import EmpiricalFillModel

INSTR, VENUE, S = "SOL-USDT-PERP", "BYBIT", 1_000_000_000


def core_book(bids, asks, ts=1 * S, seq=1):
    book = OrderBook(VENUE, INSTR)
    book.apply(MarketEvent.from_dict({
        "event_type": "BOOK_SNAPSHOT", "venue": VENUE, "instrument_id": INSTR, "exchange_ts_ns": ts, "receive_ts_ns": ts, "sequence": seq, "bids": bids, "asks": asks,
    }))
    return book


def request(order_id, side, price, qty, *, tif=TimeInForce.GTC, post_only=True, ts=2 * S, action=OrderAction.SUBMIT, target=None, order_type=OrderType.LIMIT):
    return CanonicalOrderRequest.create(
        action=action, order_id=order_id, parent_opportunity_id="op", parent_order_id="parent", instrument_id=INSTR, venue=VENUE, side=side,
        price=None if price is None else Decimal(price), quantity=Decimal(qty), order_type=order_type, time_in_force=tif, post_only=post_only,
        created_ts_ns=ts, target_order_id=target,
    )


def sell_print(price, qty, ts, tid="1"):
    return TradeEvent(event_id=f"t{tid}", venue=VENUE, instrument_id=INSTR, trade_id=tid, exchange_ts_ns=ts, source_ts_ns=ts, receive_ts_ns=ts,
                      price=Decimal(price), quantity=Decimal(qty), aggressor_side="SELL")


def adapter(**cfg):
    config = {"fill_evidence": "trade_confirm", **cfg}
    return QueueAwareVenueAdapter(VENUE, MakerExecutor(EmpiricalFillModel(), MakerConfig(**config)), tick_size=0.01, maker_fee_bps=-0.5, pair="SOLUSDT")


BIDS, ASKS = [["100.00", "50"], ["99.99", "100"]], [["100.02", "50"], ["100.03", "100"]]


class QueueAdapterTests(unittest.TestCase):
    def test_an_executor_that_lets_a_depth_change_fill_an_order_is_refused(self):
        for evidence in ("off", "fractional"):
            with self.assertRaisesRegex(ValueError, "a fill needs a real trade"):
                QueueAwareVenueAdapter(VENUE, MakerExecutor(EmpiricalFillModel(), MakerConfig(fill_evidence=evidence)), tick_size=0.01, maker_fee_bps=-0.5)

    def test_a_post_only_order_rests_behind_the_displayed_queue_and_fills_from_prints_only(self):
        a, book = adapter(), core_book(BIDS, ASKS)
        (ack,) = a.handle(request("m1", Side.BUY, "100.00", "10"), book)
        self.assertEqual(ack.event_type, ExecutionEventType.ACKNOWLEDGED)
        (maker,) = a.executor.live_orders(VENUE)
        self.assertEqual(maker.queue_ahead, 50.0)
        self.assertEqual(a.on_trade(sell_print("100.00", "30", 3 * S)), ())                        # the print drains the queue ahead, nothing reaches us
        self.assertEqual(maker.queue_ahead, 20.0)
        (pair,) = a.on_trade(sell_print("100.00", "30", 4 * S, "2"))                                # 20 clear the queue, 10 fill us
        request_, fill = pair
        self.assertEqual((fill.event_type, fill.liquidity), (ExecutionEventType.FILLED, "MAKER"))
        self.assertEqual((fill.cumulative_filled_quantity, fill.last_fill_quantity, fill.average_price), (Decimal("10"), Decimal("10"), Decimal("100.0")))
        self.assertEqual(request_.order_id, "m1")
        self.assertEqual(a.query_order("m1").state, ExecutionEventType.FILLED)
        self.assertEqual(a.fills_by_source, {"trade": 1})
        self.assertEqual(a.executor.live_orders(VENUE), [])

    def test_a_partial_print_gives_a_partial_report_with_the_running_total(self):
        a, book = adapter(), core_book([["100.00", "5"]], ASKS)
        a.handle(request("m1", Side.BUY, "100.00", "10"), book)
        (pair,) = a.on_trade(sell_print("100.00", "8", 3 * S))                                      # 5 ahead, 3 left for us
        self.assertEqual((pair[1].event_type, pair[1].cumulative_filled_quantity), (ExecutionEventType.PARTIAL, Decimal("3")))
        (pair,) = a.on_trade(sell_print("100.00", "20", 4 * S, "2"))
        self.assertEqual((pair[1].event_type, pair[1].cumulative_filled_quantity, pair[1].last_fill_quantity), (ExecutionEventType.FILLED, Decimal("10"), Decimal("7")))

    def test_a_depth_change_moves_the_queue_but_never_fills(self):
        a, book = adapter(), core_book(BIDS, ASKS)
        a.handle(request("m1", Side.BUY, "100.00", "10"), book)
        self.assertEqual(a.on_book_update(book), ())                                                 # first look at the level
        # the whole level vanishes with no print: the queue model may move our place, the order must stay resting and unfilled
        book.apply(MarketEvent.from_dict({
            "event_type": "BOOK_DELTA", "venue": VENUE, "instrument_id": INSTR, "exchange_ts_ns": 3 * S, "receive_ts_ns": 3 * S, "sequence": 2,
            "bids": [["100.00", "0"]], "asks": [],
        }))
        self.assertEqual(a.on_book_update(book), ())
        self.assertEqual(len(a.executor.live_orders(VENUE)), 1)
        self.assertEqual(a.query_order("m1").state, ExecutionEventType.ACKNOWLEDGED)
        self.assertEqual(a.fills_by_source, {})

    def test_a_post_only_price_that_would_take_liquidity_is_rejected_and_never_rests(self):
        a, book = adapter(), core_book(BIDS, ASKS)
        (rej,) = a.handle(request("m1", Side.BUY, "100.02", "10"), book)
        self.assertEqual((rej.event_type, rej.reason), (ExecutionEventType.REJECTED, "postonly_would_take_liquidity"))
        self.assertEqual(a.executor.live_orders(VENUE), [])

    def test_a_resting_order_that_is_not_post_only_is_refused_and_a_taker_order_keeps_the_deterministic_walk(self):
        a, book = adapter(), core_book(BIDS, ASKS)
        (rej,) = a.handle(request("g1", Side.BUY, "100.00", "10", post_only=False), book)
        self.assertEqual(rej.event_type, ExecutionEventType.REJECTED)
        self.assertEqual(a.executor.live_orders(VENUE), [])
        taker = a.handle(request("t1", Side.BUY, "100.02", "10", tif=TimeInForce.IOC, post_only=False), book)
        reference = DeterministicVenueAdapter(VENUE).handle(request("t1", Side.BUY, "100.02", "10", tif=TimeInForce.IOC, post_only=False), core_book(BIDS, ASKS))
        self.assertEqual([(c.event_type, c.cumulative_filled_quantity, c.average_price) for c in taker],
                         [(c.event_type, c.cumulative_filled_quantity, c.average_price) for c in reference])
        self.assertEqual(taker[-1].event_type, ExecutionEventType.FILLED)

    def test_cancel_removes_the_order_so_a_later_print_fills_nothing_and_a_second_cancel_is_refused(self):
        a, book = adapter(), core_book([["100.00", "5"]], ASKS)
        a.handle(request("m1", Side.BUY, "100.00", "10"), book)
        (cancelled,) = a.handle(request("c1", Side.BUY, None, "10", action=OrderAction.CANCEL, target="m1", ts=3 * S, order_type=OrderType.LIMIT), book)
        self.assertEqual(cancelled.event_type, ExecutionEventType.CANCELLED)
        self.assertEqual(a.on_trade(sell_print("100.00", "50", 4 * S)), ())
        (again,) = a.handle(request("c2", Side.BUY, None, "10", action=OrderAction.CANCEL, target="m1", ts=5 * S), book)
        self.assertEqual(again.event_type, ExecutionEventType.REJECTED)

    def test_replace_loses_the_queue_place(self):
        a, book = adapter(), core_book([["100.00", "5"]], ASKS)
        a.handle(request("m1", Side.BUY, "100.00", "10"), book)
        a.on_trade(sell_print("100.00", "5", 3 * S))                                                 # queue ahead is now zero
        callbacks = a.handle(request("m2", Side.BUY, "100.00", "10", action=OrderAction.REPLACE, target="m1", ts=4 * S), book)
        self.assertEqual([c.event_type for c in callbacks], [ExecutionEventType.REPLACED, ExecutionEventType.ACKNOWLEDGED])
        (maker,) = a.executor.live_orders(VENUE)
        self.assertGreater(maker.queue_ahead, 0.0)

    def test_replace_carries_the_quote_total_and_does_not_offer_again_what_the_old_order_filled(self):
        """Owner 12 Nov (amend lineage): the REPLACE names the quote's total; the venue's new order has the total less what the old one had filled by then."""
        a, book = adapter(), core_book([["100.00", "5"]], ASKS)
        a.handle(request("m1", Side.BUY, "100.00", "10"), book)
        a.on_trade(sell_print("100.00", "9", 3 * S))                                                 # 5 drains the queue, 4 fill the old order
        callbacks = a.handle(request("m2", Side.BUY, "100.00", "10", action=OrderAction.REPLACE, target="m1", ts=4 * S), book)
        self.assertEqual([c.event_type for c in callbacks], [ExecutionEventType.REPLACED, ExecutionEventType.ACKNOWLEDGED])
        (maker,) = a.executor.live_orders(VENUE)
        self.assertAlmostEqual(maker.remaining, 6.0)                                                 # 10 - 4, not 10: the quote is never over-filled

    def test_the_router_calls_the_hook_and_reports_the_fill_on_the_match_time(self):
        from crypto_stat_arb.instruments import InstrumentMaster
        a, book = adapter(), core_book([["100.00", "5"]], ASKS)
        master = InstrumentMaster(())          # the hook path does not read the instrument master
        router = StrategyDirectedRouter(master, adapters={VENUE: a})
        router.adapters[VENUE].handle(request("m1", Side.BUY, "100.00", "5"), book)
        self.assertEqual(router.on_trade(VENUE, sell_print("100.00", "50", 3 * S), 7 * S), 1)
        results = router.process_throttled(7 * S)
        self.assertEqual((results[0].callbacks[0].event_type, results[0].callbacks[0].event_ts_ns), (ExecutionEventType.FILLED, 7 * S))


def rpi_print(price, qty, ts, tid="1"):
    return TradeEvent(event_id=f"t{tid}", venue=VENUE, instrument_id=INSTR, trade_id=tid, exchange_ts_ns=ts, source_ts_ns=ts, receive_ts_ns=ts,
                      price=Decimal(price), quantity=Decimal(qty), aggressor_side="SELL", native={"rpi": True})


class RpiPrintTests(unittest.TestCase):
    """Owner 13 Nov (upstreamed from the MM session): MakerConfig.exclude_rpi_trades, off by default. Off: a print flagged RPI is an ordinary print (byte-identical to a feed with no flag). On: it never fills an order and does not drain the queue."""

    def _run(self, prints, **cfg):
        a, book = adapter(**cfg), core_book(BIDS, ASKS)
        a.handle(request("m1", Side.BUY, "100.00", "10"), book)
        out = [a.on_trade(p) for p in prints]
        maker = a.executor.live_orders(VENUE)
        return a, out, [(o.queue_ahead, o.filled_qty) for o in maker], dict(a.executor.diag)

    def test_default_off_a_flagged_print_behaves_exactly_like_an_unflagged_one(self):
        flagged = [rpi_print("100.00", "30", 3 * S), rpi_print("100.00", "30", 4 * S, "2")]
        plain = [sell_print("100.00", "30", 3 * S), sell_print("100.00", "30", 4 * S, "2")]
        a1, out1, state1, diag1 = self._run(flagged)
        a2, out2, state2, diag2 = self._run(plain)
        self.assertEqual((out1, state1, diag1), (out2, state2, diag2))
        self.assertEqual(a1.fills_by_source, {"trade": 1})
        self.assertNotIn("rpi_trades_excluded", diag1)

    def test_on_an_rpi_print_never_fills_and_does_not_drain_the_queue_but_an_ordinary_print_still_does(self):
        a, book = adapter(exclude_rpi_trades=True), core_book(BIDS, ASKS)
        a.handle(request("m1", Side.BUY, "100.00", "10"), book)
        (maker,) = a.executor.live_orders(VENUE)
        for i, ts in enumerate((3 * S, 4 * S, 5 * S)):
            self.assertEqual(a.on_trade(rpi_print("100.00", "500", ts, str(i))), ())               # 500 would clear the queue and fill the order if it counted
        self.assertEqual((maker.queue_ahead, maker.filled_qty), (50.0, 0.0))
        self.assertEqual(a.executor.diag["rpi_trades_excluded"], 3)
        self.assertEqual(a.fills_by_source, {})
        (pair,) = a.on_trade(sell_print("100.00", "60", 6 * S, "9"))                               # an ordinary print: 50 clear the queue, 10 fill us
        self.assertEqual(pair[1].last_fill_quantity, Decimal("10"))

    def test_an_unflagged_or_false_flag_print_is_never_rpi(self):
        from crypto_stat_arb.simulation.queue_adapter import trade_is_rpi
        self.assertEqual([trade_is_rpi(sell_print("100", "1", S)), trade_is_rpi(rpi_print("100", "1", S))], [False, True])
        base = sell_print("100", "1", S)
        for flag, want in ((False, False), ("false", False), ("true", True), ("1", True), ("0", False), (None, False)):
            self.assertEqual(trade_is_rpi(dataclasses.replace(base, native={"rpi": flag})), want, flag)

    def test_the_drivers_feed_trade_passes_the_flag(self):
        a, book = adapter(exclude_rpi_trades=True), core_book(BIDS, ASKS)
        a.handle(request("m1", Side.BUY, "100.00", "10"), book)
        self.assertEqual(list(a.feed_trade(100.0, 500.0, -1, 3.0, 3.0, rpi=True)), [])              # excluded: no fill, no drain
        self.assertEqual(a.executor.diag["rpi_trades_excluded"], 1)
        self.assertEqual(len(list(a.feed_trade(100.0, 500.0, -1, 4.0, 4.0))), 1)                     # the default flag (False): an ordinary print fills the order


class UnfilledIocTests(unittest.TestCase):
    def test_an_ioc_that_finds_nothing_inside_its_limit_ends_unfilled_not_rejected(self):
        """The venue ends such an order unfilled (Bybit Cancelled, Binance EXPIRED). A reject with an unclassified reason would fail closed to an unwind at once; the OMS reads an unfilled IOC as retry-or-unwind by its own rule."""
        a, book = adapter(), core_book(BIDS, ASKS)
        (cb,) = a.handle(request("h1", Side.SELL, "100.10", "1", tif=TimeInForce.IOC, post_only=False), book)          # the best bid is 100.00: nothing at or above 100.10
        self.assertEqual((cb.event_type, cb.cumulative_filled_quantity), (ExecutionEventType.CANCELLED, Decimal("0")))
        reports = a.handle(request("h2", Side.SELL, "99.90", "1", tif=TimeInForce.IOC, post_only=False), book)         # inside the book: filled
        self.assertIn(ExecutionEventType.FILLED, [r.event_type for r in reports])


class ArrivalTimeTests(unittest.TestCase):
    """A request travels for the entry latency: the queue adapter must place (and cancel) at ARRIVAL, against the book as it is then, not at the creation time the request carries."""

    def test_a_delayed_submit_is_placed_and_cancelled_at_its_arrival_time(self):
        from test_execution_realism import fixed_model, instrument_master
        from test_oms import make_book
        a = adapter()
        router = StrategyDirectedRouter(instrument_master(), adapters={"BYBIT": a}, latency_model=fixed_model(entry_ms=10, cancel_ms=10))
        book = make_book("BYBIT", "59990.00", "60000.00", "5")
        spot = "BTC-USDT-SPOT"
        place = CanonicalOrderRequest.create(
            action=OrderAction.SUBMIT, order_id="m1", parent_opportunity_id="op", parent_order_id="parent", instrument_id=spot, venue="BYBIT", side=Side.BUY,
            price=Decimal("59990.00"), quantity=Decimal("1"), order_type=OrderType.LIMIT, time_in_force=TimeInForce.GTC, post_only=True, created_ts_ns=2 * S,
        )
        router.route(place, book)
        self.assertEqual(a.executor.live_orders("BYBIT"), [])                                   # still in flight
        router.process_throttled(2 * S + 10_000_000)
        (maker,) = a.executor.live_orders("BYBIT")
        self.assertEqual((maker.placed_ts, maker.arrival_ts), (2.01, 2.01))                       # arrival = creation + 10 ms entry latency, not the creation time
        cancel = CanonicalOrderRequest.create(
            action=OrderAction.CANCEL, order_id="c1", parent_opportunity_id="op", parent_order_id="parent", instrument_id=spot, venue="BYBIT", side=Side.BUY,
            price=Decimal("59990.00"), quantity=Decimal("1"), order_type=OrderType.LIMIT, time_in_force=TimeInForce.GTC, created_ts_ns=3 * S, target_order_id="m1",
        )
        router.route(cancel, book)
        self.assertEqual(len(a.executor.live_orders("BYBIT")), 1)                              # the cancel is still travelling: the order rests and can still fill until it lands
        router.process_throttled(3 * S + 10_000_000)
        self.assertEqual(a.executor.live_orders("BYBIT"), [])


class MakerGatewayOutageTests(unittest.TestCase):
    """Case 2l (owner 12 Nov): the maker venue's order entry and private stream are down for a window. Requests arriving inside it are lost with no report; a resting order stays live at the venue,
    fills from a real print, and its report waits for the reconnect. Synthetic books prove the wiring only."""
    def faulty(self, start, end):
        from crypto_stat_arb.simulation.hedge_faults import HedgeFaultConfig, Outage
        a = adapter()
        a._fault_config = HedgeFaultConfig(outages=(Outage(VENUE, start, end),), outage_policy="blackhole")
        a.maker_outage_here = True
        return a

    def test_a_submit_inside_the_window_is_lost_and_one_after_it_is_placed(self):
        a, book = self.faulty(3.0, 10.0), core_book(BIDS, ASKS)
        self.assertEqual(a.handle_at(request("lost", Side.BUY, "100.00", "1", ts=4 * S), book, 4 * S), ())
        self.assertEqual(a.executor.live_orders(VENUE), [])
        self.assertEqual(a.hedge_faults_injected, {"maker_outage:submit": 1})
        (ack,) = a.handle_at(request("later", Side.BUY, "100.00", "1", ts=11 * S), book, 11 * S)
        self.assertEqual(ack.event_type, ExecutionEventType.ACKNOWLEDGED)

    def test_a_cancel_inside_the_window_is_lost_and_the_order_stays_live_and_can_fill(self):
        a, book = self.faulty(3.0, 10.0), core_book(BIDS, ASKS)
        a.handle_at(request("m1", Side.BUY, "100.00", "10", ts=2 * S), book, 2 * S)
        cancel = request("c1", Side.BUY, "100.00", "10", ts=4 * S, action=OrderAction.CANCEL, target="m1")
        self.assertEqual(a.handle_at(cancel, book, 4 * S), ())
        self.assertEqual(len(a.executor.live_orders(VENUE)), 1)                                   # still resting at the venue
        self.assertEqual(a.hedge_faults_injected, {"maker_outage:cancel": 1})
        a.on_trade(sell_print("100.00", "50", 5 * S))
        (pair,) = a.on_trade(sell_print("100.00", "10", 6 * S, "2"))
        self.assertEqual(pair[1].event_type, ExecutionEventType.FILLED)                          # the fill happens inside the outage, from a real print

    def test_the_report_of_a_fill_inside_the_window_is_due_at_the_reconnect(self):
        a = self.faulty(3.0, 10.0)
        self.assertEqual(a.report_blackout_end_ns(5 * S), 10 * S)
        self.assertIsNone(a.report_blackout_end_ns(2 * S))
        self.assertIsNone(a.report_blackout_end_ns(10 * S))                                       # the window is half open: the gateway is back at its end
        self.assertIsNone(adapter().report_blackout_end_ns(5 * S))                                # no injection: no blackout

    def test_without_the_switch_a_static_outage_of_the_maker_venue_changes_nothing(self):
        a, book = self.faulty(3.0, 10.0), core_book(BIDS, ASKS)
        a.maker_outage_here = False
        (ack,) = a.handle_at(request("m1", Side.BUY, "100.00", "1", ts=4 * S), book, 4 * S)
        self.assertEqual(ack.event_type, ExecutionEventType.ACKNOWLEDGED)


class ThinnedHedgeBookTests(unittest.TestCase):
    """Case 2c (owner 12 Nov): attempt i of a hedge plan executes against a book showing `levels` levels with at most `quantity` each; later attempts see the real book. It only ever shows LESS."""
    BOOK = ([["100.00", "50"], ["99.99", "100"], ["99.80", "100"]], [["100.02", "50"], ["100.03", "100"]])

    def thin(self, *attempts):
        a = adapter()
        a.hedge_thin_attempts = tuple(attempts)
        return a

    def sell(self, a, order_id, book, qty="1.0", parent="parent"):
        r = CanonicalOrderRequest.create(action=OrderAction.SUBMIT, order_id=order_id, parent_opportunity_id="op", parent_order_id=parent, instrument_id=INSTR, venue=VENUE, side=Side.SELL,
                                         price=Decimal("99.90"), quantity=Decimal(qty), order_type=OrderType.LIMIT, time_in_force=TimeInForce.IOC, post_only=False, created_ts_ns=2 * S)
        return a.handle(r, book)

    def test_a_zero_book_gives_an_unfilled_ioc_and_the_retry_fills_on_the_real_book(self):
        a, book = self.thin((0, 0.0)), core_book(*self.BOOK)
        (first,) = self.sell(a, "h1", book)
        self.assertEqual((first.event_type, first.cumulative_filled_quantity), (ExecutionEventType.CANCELLED, Decimal("0")))
        second = self.sell(a, "h2", book)
        self.assertEqual((second[-1].event_type, second[-1].cumulative_filled_quantity), (ExecutionEventType.FILLED, Decimal("1.0")))
        self.assertEqual(a.hedge_faults_injected, {"thin_book:attempt0:0x0.0": 1})

    def test_two_levels_of_a_fraction_give_a_partial_and_only_the_residual_is_left(self):
        a, book = self.thin((2, 0.4)), core_book(*self.BOOK)
        reports = self.sell(a, "h1", book)
        self.assertEqual([r.event_type for r in reports], [ExecutionEventType.ACKNOWLEDGED, ExecutionEventType.PARTIAL, ExecutionEventType.CANCELLED])
        self.assertEqual(reports[-1].cumulative_filled_quantity, Decimal("0.8"))                   # 0.4 at each of the 2 bid levels, the rest of the order cancelled by the IOC
        second = self.sell(a, "h2", book, qty="0.2")
        self.assertEqual(second[-1].cumulative_filled_quantity, Decimal("0.2"))

    def test_the_attempt_count_is_per_parent_and_the_real_book_is_not_changed(self):
        a, book = self.thin((0, 0.0)), core_book(*self.BOOK)
        before = dict(book.bids)
        self.sell(a, "h1", book, parent="p1")
        (other,) = self.sell(a, "h3", book, parent="p2")                                           # another plan: its own first attempt, also thinned
        self.assertEqual(other.event_type, ExecutionEventType.CANCELLED)
        self.assertEqual(dict(book.bids), before)

    def test_a_thin_touch_with_the_deeper_levels_intact_fills_the_touch_then_the_real_levels_inside_the_limit(self):
        a, book = self.thin((-1, 0.3)), core_book(*self.BOOK)                                       # owner 13 Nov: only the touch is thinned
        before = dict(book.bids)
        reports = self.sell(a, "h1", book, qty="1.0")                                              # limit 99.90: the 100.00 level (0.3 shown) and the 99.99 level (100 real) are inside it, 99.80 is not
        self.assertEqual(reports[-1].cumulative_filled_quantity, Decimal("1.0"))
        self.assertEqual(reports[-1].average_price, (Decimal("0.3") * Decimal("100.00") + Decimal("0.7") * Decimal("99.99")) / Decimal("1.0"))      # the touch at 0.3, the real second level for the other 0.7
        self.assertEqual(a.hedge_faults_injected, {"thin_book:attempt0:topx0.3": 1})
        self.assertEqual(dict(book.bids), before)                                                  # no depth added, the real book untouched

    def test_a_thin_touch_never_shows_more_than_the_real_touch(self):
        a, book = self.thin((-1, 500.0)), core_book(*self.BOOK)                                    # asking for more than the touch holds changes nothing
        self.assertEqual(self.sell(a, "h1", book, qty="40")[-1].average_price, Decimal("100.00"))

    def test_nothing_is_thinned_by_default(self):
        a, book = adapter(), core_book(*self.BOOK)
        self.assertEqual(self.sell(a, "h1", book)[-1].event_type, ExecutionEventType.FILLED)


if __name__ == "__main__":
    unittest.main()
