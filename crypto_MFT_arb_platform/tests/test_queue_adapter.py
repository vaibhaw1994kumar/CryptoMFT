"""Phase D, step D3: QueueAwareVenueAdapter. Synthetic books and prints prove the wiring only (no profit figure): a resting post-only order is placed in the queue model, fills come
from prints, a depth change never fills an order, taker requests keep the deterministic walk."""
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

    def test_the_router_calls_the_hook_and_reports_the_fill_on_the_match_time(self):
        from crypto_stat_arb.instruments import InstrumentMaster
        a, book = adapter(), core_book([["100.00", "5"]], ASKS)
        master = InstrumentMaster(())          # the hook path does not read the instrument master
        router = StrategyDirectedRouter(master, adapters={VENUE: a})
        router.adapters[VENUE].handle(request("m1", Side.BUY, "100.00", "5"), book)
        self.assertEqual(router.on_trade(VENUE, sell_print("100.00", "50", 3 * S), 7 * S), 1)
        results = router.process_throttled(7 * S)
        self.assertEqual((results[0].callbacks[0].event_type, results[0].callbacks[0].event_ts_ns), (ExecutionEventType.FILLED, 7 * S))


if __name__ == "__main__":
    unittest.main()
