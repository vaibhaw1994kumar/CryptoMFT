"""A real ghost hedge in the session-runtime simulator (owner 11 Nov, replaces the Track B version that mapped ghost to REJECTED).

With outage_policy="ghost" the QueueAwareVenueAdapter HOLDS a hedge sent into its venue's gateway outage (no report of any kind, so the strategy cannot tell it from a lost order) and EXECUTES it at the
reconnect, against the book then. What the OMS does meanwhile is its own rule: the report deadline passes, the venue is queried (no answer while the gateway is down), the chain is UNKNOWN, and nothing is
resent or unwound until the state is known (query before replace). So the 1x ceiling holds: at no point is more than one maker quantity of net exposure held, and the held hedge is booked once.

Scenarios: (1) reconnect inside the query budget: SUBMITTED -> UNKNOWN -> executed at the reconnect -> RECONCILED, one hedge order, net back to zero; (2) outage longer than the query budget: fail closed
(the order TIMED_OUT, the chain FAILED, the kill switch active, no replacement and no unwind), then the ghost still executes at the reconnect and is booked once; (3) the reject policy is still an immediate
REJECTED; (4) no faults: the baseline.
"""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from crypto_stat_arb.execution import (
    CanonicalOrderUpdate, DispatchPolicy, ExecutionEventType, OrderAction,
    OrderUpdateSource, ResidualPolicy, StrategyDirectedRouter,
)
from crypto_stat_arb.hedge_lifecycle import HedgePolicy
from crypto_stat_arb.instruments import InstrumentMaster
from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.models import Instrument, MarketEvent, Opportunity, PlanState
from crypto_stat_arb.oms import OMS
from crypto_stat_arb.orderbook import OrderBook
from crypto_stat_arb.risk import RiskEngine, RiskLimits
from crypto_stat_arb.simulation.fill_engine import MakerConfig, MakerExecutor
from crypto_stat_arb.simulation.hedge_faults import HedgeFaultConfig, Outage
from crypto_stat_arb.simulation.queue_adapter import NullOrderEventSink, QueueAwareVenueAdapter

from test_fill_triggered import Venue, cb

INSTR = "BTC-USDT-SPOT"
_NS = 1_000_000_000
MS = 1_000_000
MAKER_QTY = Decimal("0.010")
MAKER_PRICE = Decimal("60060")


def _instruments() -> InstrumentMaster:
    return InstrumentMaster([
        Instrument(
            instrument_id=INSTR, venue=v, symbol="BTCUSDT", base="BTC", quote="USDT",
            tick_size=Decimal("0.10"), quantity_step=Decimal("0.001"),
            min_quantity=Decimal("0.001"), min_notional=Decimal("5"),
            taker_fee_bps=Decimal("5.5"), maker_fee_bps=Decimal("-0.5"),
        )
        for v in ("BINANCE", "BYBIT")
    ])


def _books() -> dict:
    result = {}
    for venue, bid, ask in (("BINANCE", "59999.90", "60000.00"), ("BYBIT", "60060.00", "60060.10")):
        book = OrderBook(venue, INSTR)
        book.apply(MarketEvent.from_dict({
            "event_type": "BOOK_SNAPSHOT", "venue": venue, "instrument_id": INSTR,
            "exchange_ts_ns": 1, "receive_ts_ns": 1, "sequence": 1,
            "bids": [[bid, "5"]], "asks": [[ask, "5"]],
        }))
        result[(venue, INSTR)] = book
    return result


def _opportunity() -> Opportunity:
    return Opportunity(
        opportunity_id="ghost-opp-1", strategy_id="strategy-1", instrument_id=INSTR,
        observed_ts_ns=1, buy_venue="BINANCE", sell_venue="BYBIT",
        buy_price=Decimal("60000"), sell_price=Decimal("60060"),
        quantity=MAKER_QTY, gross_edge_bps=Decimal("10"), estimated_net_edge_bps=Decimal("2"),
        source_event_ids=("e1", "e2"),
    )


def _adapter(policy: str, start_s: float, end_s: float) -> QueueAwareVenueAdapter:
    fc = HedgeFaultConfig(outages=(Outage("BINANCE", start_s, end_s, kind="gateway"),), outage_policy=policy)
    executor = MakerExecutor(NullOrderEventSink(), MakerConfig(fill_evidence="trade_confirm"))
    return QueueAwareVenueAdapter("BINANCE", executor, tick_size=0.01, maker_fee_bps=5.5, pair=INSTR, fault_config=fc)


class GhostHedgeTests(unittest.TestCase):
    def build(self, policy: str = "ghost", start_s: float = 0.05, end_s: float = 3.0):
        self.inst, self.bk = _instruments(), _books()
        self.bybit = Venue("BYBIT", ["ack"], price="60060")
        self.binance = _adapter(policy, start_s, end_s)
        self.oms = OMS(
            self.inst, RiskEngine(RiskLimits(max_order_notional=Decimal("5000"), max_gross_notional=Decimal("50000"), max_residual_base=Decimal("0.005"), max_book_age_ns=10_000_000_000 * 100)),
            Ledger(),
            router=StrategyDirectedRouter(self.inst, adapters={"BYBIT": self.bybit, "BINANCE": self.binance}),
            dispatch_policy=DispatchPolicy.FILL_TRIGGERED, hedge_policy=HedgePolicy(retry_limit=2),
            residual_policy=ResidualPolicy(max_unwind_attempts=3, max_residual_base=Decimal("0.05"), max_residual_age_ns=60 * _NS),
        )
        self.plan = self.oms.execute(_opportunity(), self.bk, maker_venue="BYBIT")
        maker = self.plan.children[0]
        self.oms.ingest_order_update(CanonicalOrderUpdate(
            update_id="mk-1", source=OrderUpdateSource.PRIVATE_STREAM, parent_order_id=self.plan.parent_id, order_id=maker.order_id, venue=maker.venue, instrument_id=maker.instrument_id,
            state=ExecutionEventType.FILLED, event_ts_ns=100 * MS, observed_ts_ns=100 * MS + 1, sequence=1, cumulative_filled_quantity=MAKER_QTY, average_price=MAKER_PRICE))
        self.net_seen = [self.net()]

    def chain(self):
        return next(iter(self.oms.hedge_chains[self.plan.parent_id].values()))

    def net(self) -> Decimal:
        return sum(self.oms.ledger.positions.values(), Decimal("0"))

    def at(self, seconds: float) -> None:
        self.oms.advance_time(int(seconds * _NS), self.bk)
        self.net_seen.append(self.net())

    def hedge_children(self):
        return [c for c in self.plan.children if c.venue == "BINANCE"]

    def test_the_hedge_is_held_during_the_outage_and_executed_at_the_reconnect(self):
        self.build(end_s=3.0)
        self.assertEqual(len(self.binance._held), 1)                                   # held: no report, no fill
        self.assertEqual(self.chain().state.value, "SUBMITTED")
        self.assertEqual(self.binance.executor.live_orders("BINANCE"), [])              # never a resting maker order
        self.at(1.0)
        self.assertEqual(self.net(), Decimal("-0.010"))                                # the maker leg is unhedged, the held hedge has not filled
        self.at(2.7)
        self.assertEqual(self.chain().state.value, "UNKNOWN")                          # the deadline passed, the venue was queried, the gateway gave no answer
        self.assertEqual(len(self.hedge_children()), 1)                                # query before replace: nothing was resent while the state was unknown
        self.assertFalse(self.oms.risk.kill_switch)
        self.at(3.2)
        self.assertEqual(self.binance.ghost_delivered[self.hedge_children()[0].order_id], 3 * _NS)   # executed at the reconnect time
        self.assertEqual(self.chain().state.value, "RECONCILED")
        self.assertEqual(len(self.hedge_children()), 1)
        self.assertEqual(self.oms.ledger.positions[("BINANCE", INSTR)], MAKER_QTY)     # booked once
        self.assertEqual(self.net(), Decimal("0"))
        self.assertEqual(self.binance._held, [])

    def test_the_one_x_ceiling_holds_at_every_step(self):
        self.build(end_s=3.0)
        for t in (0.5, 1.0, 2.2, 2.7, 3.0, 3.2, 12.0):
            self.at(t)
        self.assertTrue(all(abs(n) <= MAKER_QTY for n in self.net_seen), self.net_seen)

    def test_an_outage_longer_than_the_query_budget_fails_closed_and_the_ghost_is_still_booked_once(self):
        self.build(end_s=10.0)
        self.at(2.7)
        self.assertEqual(self.chain().state.value, "UNKNOWN")
        self.at(3.2)                                                                   # three queries unanswered: the order is TIMED_OUT, the chain FAILED, the kill switch active
        self.assertTrue(self.oms.risk.kill_switch)
        self.assertEqual(self.chain().state.value, "FAILED")
        self.assertEqual(self.hedge_children()[0].state.value, "TIMED_OUT")
        self.assertEqual(len(self.plan.children), 2)                                   # no replacement hedge and no unwind child: the state is unknown, so nothing is sent blind
        self.assertEqual(self.net(), Decimal("-0.010"))
        self.at(12.0)                                                                  # the reconnect: the held hedge reaches the venue and fills
        self.assertEqual(self.oms.ledger.positions[("BINANCE", INSTR)], MAKER_QTY)     # once
        self.assertEqual(self.net(), Decimal("0"))
        self.assertEqual(len(self.plan.children), 2)
        self.assertTrue(self.oms.risk.kill_switch)                                     # the alarm stays until an operator resets it
        self.assertTrue(all(abs(n) <= MAKER_QTY for n in self.net_seen), self.net_seen)

    def test_the_reject_policy_is_still_an_immediate_rejection_and_holds_nothing(self):
        self.build(policy="reject", end_s=3.0)
        self.assertEqual(self.binance._held, [])
        self.assertGreaterEqual(sum(self.binance.hedge_faults_injected.values()), 1)
        self.at(1.0)
        self.assertTrue(all(abs(n) <= MAKER_QTY for n in self.net_seen), self.net_seen)

    def test_an_outage_of_the_other_venue_does_not_touch_this_one(self):
        fc = HedgeFaultConfig(outages=(Outage("BYBIT", 0.0, 99.0, kind="gateway"),), outage_policy="ghost")
        adapter = QueueAwareVenueAdapter("BINANCE", MakerExecutor(NullOrderEventSink(), MakerConfig(fill_evidence="trade_confirm")), tick_size=0.01, maker_fee_bps=5.5, pair=INSTR, fault_config=fc)
        self.assertFalse(adapter.query_unanswered(100 * MS))
        self.assertIsNone(adapter._outage_at(100 * MS))

    def test_without_faults_the_hedge_fills_at_once(self):
        executor = MakerExecutor(NullOrderEventSink(), MakerConfig(fill_evidence="trade_confirm"))
        binance = QueueAwareVenueAdapter("BINANCE", executor, tick_size=0.01, maker_fee_bps=5.5, pair=INSTR, fault_config=None)
        inst, bk = _instruments(), _books()
        oms = OMS(inst, RiskEngine(RiskLimits(max_order_notional=Decimal("5000"), max_gross_notional=Decimal("50000"), max_residual_base=Decimal("0.005"), max_book_age_ns=10_000_000_000)), Ledger(),
                  router=StrategyDirectedRouter(inst, adapters={"BYBIT": Venue("BYBIT", ["ack"], price="60060"), "BINANCE": binance}), dispatch_policy=DispatchPolicy.FILL_TRIGGERED,
                  hedge_policy=HedgePolicy(retry_limit=1), residual_policy=ResidualPolicy(max_unwind_attempts=3, max_residual_base=Decimal("0.005"), max_residual_age_ns=0))
        plan = oms.execute(_opportunity(), bk, maker_venue="BYBIT")
        maker = plan.children[0]
        oms.ingest_order_update(CanonicalOrderUpdate(
            update_id="mk-1", source=OrderUpdateSource.PRIVATE_STREAM, parent_order_id=plan.parent_id, order_id=maker.order_id, venue=maker.venue, instrument_id=maker.instrument_id,
            state=ExecutionEventType.FILLED, event_ts_ns=100 * MS, observed_ts_ns=100 * MS + 1, sequence=1, cumulative_filled_quantity=MAKER_QTY, average_price=MAKER_PRICE))
        self.assertEqual(sum(binance.hedge_faults_injected.values()), 0)
        self.assertEqual(binance._held, [])
        self.assertLessEqual(abs(plan.residual_base), MAKER_QTY)


class PartialHedgeUnderFaults(unittest.TestCase):
    """Owner 11 Nov (5b): a partial IOC fill (the hedge book was thinned after the plan was admitted: 0.004 of 0.010 is offered), then the remainder meets an outage. The filled part is booked once; the remainder
    follows the retry / unwind rule (two retries, then an unwind of the unhedged part on the maker venue); the net never exceeds the maker quantity."""

    def build(self, policy: str, start_s: float, end_s: float):
        self.inst, self.bk = _instruments(), _books()
        self.binance = _adapter(policy, start_s, end_s)
        self.oms = OMS(
            self.inst, RiskEngine(RiskLimits(max_order_notional=Decimal("5000"), max_gross_notional=Decimal("50000"), max_residual_base=Decimal("0.005"), max_book_age_ns=10_000_000_000 * 100)),
            Ledger(),
            router=StrategyDirectedRouter(self.inst, adapters={"BYBIT": Venue("BYBIT", ["ack"], price="60060"), "BINANCE": self.binance}),
            dispatch_policy=DispatchPolicy.FILL_TRIGGERED, hedge_policy=HedgePolicy(retry_limit=2, retry_backoff_ns=300 * MS),
            residual_policy=ResidualPolicy(max_unwind_attempts=3, max_residual_base=Decimal("0.002"), max_residual_age_ns=60 * _NS),
        )
        self.plan = self.oms.execute(_opportunity(), self.bk, maker_venue="BYBIT")
        self.book(Decimal("0.004"), 2)                                                   # the liquidity thins after the plan was admitted
        maker = self.plan.children[0]
        self.oms.ingest_order_update(CanonicalOrderUpdate(
            update_id="mk-1", source=OrderUpdateSource.PRIVATE_STREAM, parent_order_id=self.plan.parent_id, order_id=maker.order_id, venue=maker.venue, instrument_id=maker.instrument_id,
            state=ExecutionEventType.FILLED, event_ts_ns=100 * MS, observed_ts_ns=100 * MS + 1, sequence=1, cumulative_filled_quantity=MAKER_QTY, average_price=MAKER_PRICE))
        self.net_seen = [self.net()]

    def book(self, ask_size: Decimal, seq: int) -> None:
        self.bk[("BINANCE", INSTR)].apply(MarketEvent.from_dict({"event_type": "BOOK_SNAPSHOT", "venue": "BINANCE", "instrument_id": INSTR, "exchange_ts_ns": seq, "receive_ts_ns": seq, "sequence": seq,
                                                                 "bids": [["59999.90", "5"]], "asks": [["60000.00", str(ask_size)]]}))

    def net(self) -> Decimal:
        return sum(self.oms.ledger.positions.values(), Decimal("0"))

    def at(self, seconds: float) -> None:
        self.oms.advance_time(int(seconds * _NS), self.bk)
        self.net_seen.append(self.net())

    def chain(self):
        return next(iter(self.oms.hedge_chains[self.plan.parent_id].values()))

    def test_a_partial_fill_then_rejected_retries_books_the_part_once_and_unwinds_the_rest(self):
        self.build("reject", 0.15, 5.0)
        self.assertEqual(self.oms.ledger.positions[("BINANCE", INSTR)], Decimal("0.004"))     # the part that filled, booked at once and once
        self.assertEqual(self.net(), Decimal("-0.006"))
        self.assertEqual(self.chain().state.value, "RETRYING")
        for t in (0.4, 0.7, 1.5, 5.5):
            self.at(t)
        hedges = [c for c in self.plan.children if c.venue == "BINANCE"]
        self.assertEqual([c.state.value for c in hedges], ["CANCELLED", "REJECTED", "REJECTED"])   # the IOC remainder, then two retries refused by the outage
        self.assertEqual(self.oms.ledger.positions[("BINANCE", INSTR)], Decimal("0.004"))     # still once
        unwind = [c for c in self.plan.children if c.venue == "BYBIT" and c.side.value == "BUY"]
        self.assertEqual([(c.state.value, c.filled_quantity) for c in unwind], [("FILLED", Decimal("0.006"))])   # the unhedged part was unwound on the maker venue
        self.assertEqual(self.net(), Decimal("0"))
        self.assertEqual(self.chain().state.value, "RECONCILED")
        self.assertTrue(all(abs(n) <= MAKER_QTY for n in self.net_seen), self.net_seen)
        self.assertEqual(sum(self.binance.hedge_faults_injected.values()), 2)

    def test_a_partial_fill_then_a_ghost_remainder_waits_for_the_state_and_is_booked_once_at_the_reconnect(self):
        self.build("ghost", 0.15, 3.0)
        self.assertEqual(self.oms.ledger.positions[("BINANCE", INSTR)], Decimal("0.004"))
        for t in (0.4, 1.0, 2.0):
            self.at(t)
        self.assertEqual(len(self.binance._held), 1)                                          # the remainder retry is held at the gateway
        self.book(Decimal("5"), 9)                                                            # liquidity is back by the time the gateway is
        self.at(2.7)
        self.assertEqual(self.chain().state.value, "UNKNOWN")
        hedges = [c for c in self.plan.children if c.venue == "BINANCE"]
        self.assertEqual(len(hedges), 2)                                                      # the partial IOC and ONE retry: nothing replaced while the state was unknown
        self.assertEqual(len([c for c in self.plan.children if c.venue == "BYBIT" and c.side.value == "BUY"]), 0)   # and no unwind
        self.at(3.2)
        self.assertEqual(self.oms.ledger.positions[("BINANCE", INSTR)], MAKER_QTY)            # 0.004 + 0.006: each part booked once
        self.assertEqual(self.net(), Decimal("0"))
        self.assertEqual(self.chain().state.value, "RECONCILED")
        self.assertTrue(all(abs(n) <= MAKER_QTY for n in self.net_seen), self.net_seen)


if __name__ == "__main__":
    unittest.main()
