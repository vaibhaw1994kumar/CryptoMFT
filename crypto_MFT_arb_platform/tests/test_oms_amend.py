"""Owner 12 Nov, amend semantics in the core OMS (CR-MQ part 2's order part; shared by the arb and the MM strategy), OFF by default.

AMEND_PENDING: the old order stays live at the old price and its fills count; the new price counts only after the venue CONFIRMS (not when it accepts the request); a reject or a timeout is followed by a query, never a
blind second order; the lineage keeps a late fill on the old order from over-filling the quote; an urgent cancel during a pending amend takes down every order of the lineage; a route that cannot amend falls back to a
cancel and never leaves two live orders. The simulator side (queue_adapter._replace_own) treats any price amend as a loss of queue place, conservatively (labelled in AMEND_CONFIRMED)."""
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from crypto_stat_arb.execution import (
    CanonicalOrderUpdate, DispatchPolicy, ExecutionEventType, OrderAction, OrderUpdateSource, StrategyDirectedRouter, VenueOrderStatus,
)
from crypto_stat_arb.hedge_lifecycle import HedgePolicy
from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.models import OrderState, PlanState
from crypto_stat_arb.oms import OMS
from test_restart_resilience import INSTR_FT, MS, ScriptedVenue, _cb, _ft_books, _ft_instruments, _ft_opportunity, risk


class AmendVenue:
    """A venue that rests maker orders and answers REPLACE as scripted: confirm (REPLACED + the new order live), accept_only (the gateway accepted the request, nothing confirmed), reject, silent."""

    def __init__(self, venue, mode="confirm"):
        self.venue, self.mode = venue, mode
        self.submits, self.replaces, self.cancels, self.open = [], [], [], {}
        self.status = {}                                   # order id -> (event type, cumulative, price): what a query answers

    def handle(self, req, book):
        if req.action == OrderAction.CANCEL:
            self.cancels.append(req.target_order_id)
            if req.target_order_id in self.open:
                del self.open[req.target_order_id]
                return (_cb(req, ExecutionEventType.CANCELLED),)
            return (_cb(req, ExecutionEventType.REJECTED, reason="cancel target is not open"),)
        if req.action == OrderAction.REPLACE:
            self.replaces.append(req)
            if self.mode == "confirm":
                self.open.pop(req.target_order_id, None); self.open[req.order_id] = req.price
                return (_cb(req, ExecutionEventType.REPLACED), _cb(req, ExecutionEventType.ACKNOWLEDGED))
            if self.mode == "accept_only":
                return (_cb(req, ExecutionEventType.AMEND_ACCEPTED),)
            if self.mode == "reject":
                return (_cb(req, ExecutionEventType.REJECTED, reason="amend rejected: order not modifiable"),)
            return ()                                       # silent
        self.submits.append(req)
        self.open[req.order_id] = req.price
        return (_cb(req, ExecutionEventType.ACKNOWLEDGED),)

    def query_order(self, order_id):
        if order_id in self.status:
            state, cum, price = self.status[order_id]
            return VenueOrderStatus(order_id=order_id, venue=self.venue, instrument_id=INSTR_FT, state=state, cumulative_filled_quantity=Decimal(cum), average_price=Decimal(price), venue_ts_ns=1)
        return VenueOrderStatus(order_id=order_id, venue=self.venue, instrument_id=INSTR_FT, state=None, reason="order unknown to venue")


def stream_update(plan, order, n, state, cum, ts, price="60060"):
    return CanonicalOrderUpdate(update_id=f"up-{order.order_id[-6:]}-{n}", source=OrderUpdateSource.PRIVATE_STREAM, parent_order_id=plan.parent_id, order_id=order.order_id, venue=order.venue,
                                instrument_id=order.instrument_id, state=state, event_ts_ns=ts, observed_ts_ns=ts + 1, sequence=n, cumulative_filled_quantity=Decimal(cum), average_price=Decimal(price))


class AmendTests(unittest.TestCase):
    def setUp(self):
        self.t = 100 * MS

    def build(self, mode="confirm", enabled=True, directory=None):
        inst = _ft_instruments()
        self.bybit, self.binance = AmendVenue("BYBIT", mode), ScriptedVenue("BINANCE", ["fill"], price="60000")
        self.oms = OMS(inst, risk(), Ledger(), router=StrategyDirectedRouter(inst, adapters={"BYBIT": self.bybit, "BINANCE": self.binance}),
                       dispatch_policy=DispatchPolicy.FILL_TRIGGERED, hedge_policy=HedgePolicy(retry_limit=1))
        self.oms.amend_enabled = enabled
        self.plan = self.oms.execute(_ft_opportunity(), _ft_books(), maker_venue="BYBIT")
        self.maker = self.plan.children[0]
        self.assertEqual(self.maker.state, OrderState.ACKNOWLEDGED)

    def events(self, *names):
        return [e for e in self.oms.ledger.events if e["event_type"] in names]

    def fills(self):
        return [e for e in self.oms.ledger.events if e["event_type"] == "FILL"]

    def replacement(self):
        return next(x for x in self.plan.children if x.replaces_order_id)

    def test_off_by_default_nothing_is_sent(self):
        self.build(enabled=False)
        self.assertFalse(OMS(_ft_instruments(), risk(), Ledger()).amend_enabled)           # False until a caller sets it
        self.assertEqual(self.oms.request_amend(self.maker.order_id, Decimal("60070.0"), self.t), "REFUSED:amend_disabled")
        self.assertEqual(self.bybit.replaces, [])
        self.assertFalse(any(x.replaces_order_id for x in self.plan.children))

    def test_the_old_order_stays_live_at_the_old_price_until_the_venue_confirms(self):
        self.build("accept_only")
        self.assertEqual(self.oms.request_amend(self.maker.order_id, Decimal("60070.0"), self.t), "REQUESTED")
        self.assertEqual(self.maker.state, OrderState.AMEND_PENDING)
        self.assertEqual((self.maker.limit_price, self.maker.amend_to_price), (Decimal("60060"), Decimal("60070.0")))      # the old price is the effective one
        self.assertEqual(len(self.events("AMEND_ACCEPTED_NOT_CONFIRMED")), 1)                                                # the request was accepted: that is all
        self.assertEqual(self.events("AMEND_CONFIRMED"), [])
        self.assertEqual(self.oms.request_amend(self.maker.order_id, Decimal("60080.0"), self.t + MS), "REFUSED:state_amend_pending")   # no second amend while one is pending
        new = self.replacement()
        self.oms.ingest_order_update(stream_update(self.plan, new, 1, ExecutionEventType.ACKNOWLEDGED, "0", self.t + 5 * MS, price="60070"))      # the venue's own report: the new order is live
        self.assertEqual(len(self.events("AMEND_CONFIRMED")), 1)
        self.assertEqual(self.maker.state, OrderState.CANCELLED)                                                              # the old order is gone, the lineage head is the replacement
        self.assertEqual(new.state, OrderState.ACKNOWLEDGED)
        self.assertEqual(self.oms._hedge_specs[self.plan.parent_id]["maker_order_id"], new.order_id)
        self.assertEqual(len(self.bybit.submits), 1)                                                                          # one maker order was ever submitted

    def test_a_fill_during_amend_pending_is_booked_once_and_the_residual_hedged_once(self):
        self.build("accept_only")
        self.oms.request_amend(self.maker.order_id, Decimal("60070.0"), self.t)
        self.oms.ingest_order_update(stream_update(self.plan, self.maker, 1, ExecutionEventType.FILLED, "0.010", self.t + 2 * MS))      # the old order fills while the amend is pending
        self.assertEqual(len([f for f in self.fills() if f.get("order_id") == self.maker.order_id]), 1)                        # booked once
        self.assertEqual(len(self.binance.submits), 1)                                                                          # hedged once, for the filled quantity
        self.assertEqual(self.binance.submits[0].quantity, Decimal("0.010"))
        self.assertEqual(self.oms.ledger.positions[("BYBIT", INSTR_FT)] + self.oms.ledger.positions[("BINANCE", INSTR_FT)], Decimal("0"))      # flat net
        self.assertEqual(len(self.events("AMEND_LINEAGE_COMPLETE_CANCEL")), 1)                                                # the quote is complete: the replacement is taken down, not left to fill again
        new = self.replacement()
        self.assertIn(new.order_id, self.bybit.cancels)
        # a duplicate "Filled" for the old order, and a late fill report on the replacement, change nothing
        self.oms.ingest_order_update(stream_update(self.plan, self.maker, 2, ExecutionEventType.FILLED, "0.010", self.t + 3 * MS))
        self.assertEqual(len(self.binance.submits), 1)
        self.assertEqual(len([f for f in self.fills() if f.get("order_id") == self.maker.order_id]), 1)

    def test_a_late_fill_on_the_old_order_reduces_what_the_replacement_may_still_take(self):
        self.build("accept_only")
        self.oms.request_amend(self.maker.order_id, Decimal("60070.0"), self.t)
        new = self.replacement()
        self.assertEqual(self.oms.lineage_remaining(new.order_id), Decimal("0.010"))
        self.oms.ingest_order_update(stream_update(self.plan, self.maker, 1, ExecutionEventType.PARTIAL, "0.004", self.t + 2 * MS))      # 0.004 filled on the old order after the amend was sent
        self.assertEqual(self.oms.lineage_remaining(new.order_id), Decimal("0.006"))
        self.assertEqual(self.oms.lineage_remaining(self.maker.order_id), Decimal("0.006"))
        self.oms.ingest_order_update(stream_update(self.plan, new, 1, ExecutionEventType.ACKNOWLEDGED, "0", self.t + 3 * MS, price="60070"))   # the venue confirms
        self.oms.ingest_order_update(stream_update(self.plan, new, 2, ExecutionEventType.FILLED, "0.006", self.t + 4 * MS, price="60070"))      # and the replacement takes only the rest
        self.assertEqual(self.oms.lineage_remaining(new.order_id), Decimal("0"))
        self.assertEqual(sum((Decimal(str(f["fill_quantity"])) for f in self.fills() if f.get("venue") == "BYBIT"), Decimal("0")), Decimal("0.010"))
        self.assertEqual(self.oms.carried_residual_base(), Decimal("0"))

    def test_an_amend_timeout_then_a_late_success_makes_no_duplicate(self):
        self.build("silent")
        self.oms.request_amend(self.maker.order_id, Decimal("60070.0"), self.t)
        new = self.replacement()
        self.oms.check_order_deadlines(self.t + 60 * 1_000_000_000)                        # the deadline passes with no answer
        self.assertEqual(len(self.events("AMEND_TIMEOUT")), 1)
        self.assertEqual(new.state, OrderState.TIMED_OUT)                                    # unknown, not failed
        self.bybit.status[self.maker.order_id] = (ExecutionEventType.ACKNOWLEDGED, "0", "60060")   # the venue says the old order is still live at the old price
        self.oms.check_order_deadlines(self.t + 61 * 1_000_000_000)
        self.assertEqual(self.maker.state, OrderState.ACKNOWLEDGED)                          # the query answered: the old order is as it was
        self.assertEqual(self.maker.limit_price, Decimal("60060"))
        self.assertEqual(len(self.bybit.submits), 1)                                         # never a second order
        self.assertEqual(len(self.bybit.replaces), 1)
        # the amend succeeds late: the venue's report of the new order live
        self.oms.ingest_order_update(stream_update(self.plan, new, 1, ExecutionEventType.ACKNOWLEDGED, "0", self.t + 90 * 1_000_000_000, price="60070"))
        self.assertEqual(len(self.events("AMEND_CONFIRMED")), 1)
        self.assertEqual((self.maker.state, new.state), (OrderState.CANCELLED, OrderState.ACKNOWLEDGED))      # exactly one live order
        live = [x for x in self.plan.children if x.order_role == "PRIMARY" and x.state in (OrderState.ACKNOWLEDGED, OrderState.PARTIAL, OrderState.AMEND_PENDING, OrderState.SENT)]
        self.assertEqual([x.order_id for x in live], [new.order_id])
        self.assertEqual(len(self.bybit.submits), 1)
        # and it fills once, hedged once
        self.oms.ingest_order_update(stream_update(self.plan, new, 2, ExecutionEventType.FILLED, "0.010", self.t + 91 * 1_000_000_000, price="60070"))
        self.assertEqual(len(self.binance.submits), 1)

    def test_an_amend_reject_is_followed_by_a_query_not_a_second_order(self):
        self.build("reject")
        self.oms.request_amend(self.maker.order_id, Decimal("60070.0"), self.t)
        self.assertEqual(len(self.events("AMEND_REJECTED")), 1)
        self.assertEqual(self.replacement().state, OrderState.REJECTED)
        self.assertEqual(self.maker.state, OrderState.AMEND_PENDING)                          # still live, still at the old price, until the query answers
        self.bybit.status[self.maker.order_id] = (ExecutionEventType.ACKNOWLEDGED, "0", "60060")
        self.oms.check_order_deadlines(self.t + 1 * MS)
        self.assertEqual(self.maker.state, OrderState.ACKNOWLEDGED)
        self.assertEqual(len(self.bybit.submits), 1)

    def test_after_a_reject_and_the_query_the_old_quote_is_checked_for_validity_again(self):
        """EMS note: a rejected amend leaves the OLD quote resting at its old price; once the query confirms it, it is an ordinary resting quote again, so quote validity (withdrawal) applies to it."""
        self.build("reject")
        self.oms.request_amend(self.maker.order_id, Decimal("60070.0"), self.t)
        self.bybit.status[self.maker.order_id] = (ExecutionEventType.ACKNOWLEDGED, "0", "60060")
        self.oms.check_order_deadlines(self.t + 1 * MS)
        self.assertEqual(self.maker.state, OrderState.ACKNOWLEDGED)
        self.assertTrue(self.oms.withdraw_quote(self.maker.order_id, self.t + 2 * MS, "edge_below_threshold", -0.4))
        self.assertEqual(len(self.events("QUOTE_WITHDRAWN")), 1)
        self.assertIn(self.maker.order_id, self.bybit.cancels)

    def test_an_urgent_cancel_during_a_pending_amend_takes_down_both_orders(self):
        self.build("accept_only")
        self.oms.request_amend(self.maker.order_id, Decimal("60070.0"), self.t)
        new = self.replacement()
        self.bybit.open[new.order_id] = Decimal("60070")                                     # the venue turns out to have processed the amend
        self.assertTrue(self.oms.urgent_cancel(self.maker.order_id, self.t + MS, "kill_drill"))
        self.assertEqual(len(self.events("QUOTE_URGENT_CANCEL")), 1)
        self.assertIn(self.maker.order_id, self.bybit.cancels)
        self.assertIn(new.order_id, self.bybit.cancels)                                      # the replacement is cancelled too: nothing survives
        self.assertEqual(self.bybit.open, {})
        for x in (self.maker, new):
            self.assertIn(x.state, (OrderState.CANCELLED, OrderState.CANCEL_PENDING))
        self.assertEqual(self.fills(), [])

    def test_a_cancelled_quote_with_a_rejected_replacement_and_no_fill_is_never_read_as_a_filled_plan(self):
        self.build("reject")
        self.oms.request_amend(self.maker.order_id, Decimal("60070.0"), self.t)                # the amend is rejected: the replacement is REJECTED, the old order is queried
        self.bybit.status[self.maker.order_id] = (ExecutionEventType.CANCELLED, "0", "0")          # and the venue says the old order is gone
        self.oms.check_order_deadlines(self.t + 1 * MS)
        self.assertEqual(self.maker.state, OrderState.CANCELLED)
        self.assertNotEqual(self.plan.state, PlanState.FILLED)
        self.assertEqual(self.fills(), [])

    def test_a_route_that_cannot_amend_falls_back_to_a_cancel_and_never_two_live_orders(self):
        from crypto_stat_arb.execution import VenueRouteStatus
        self.build("confirm")
        status = self.oms.router.route_status
        class NoReplace:
            supports_replace = False
        status["BYBIT"] = NoReplace()
        self.assertEqual(self.oms.request_amend(self.maker.order_id, Decimal("60070.0"), self.t), "FALLBACK_CANCEL")
        self.assertEqual(self.bybit.replaces, [])                                           # nothing was sent in place of the cancelled order
        self.assertEqual(len(self.bybit.submits), 1)
        self.assertEqual(len(self.events("AMEND_FALLBACK_CANCEL")), 1)
        self.assertIn(self.maker.state, (OrderState.CANCEL_PENDING, OrderState.CANCELLED))
        self.assertFalse(any(x.replaces_order_id for x in self.plan.children))

    def test_a_confirmed_amend_then_a_duplicate_filled_across_the_replacement_is_booked_once(self):
        self.build("confirm")
        self.assertEqual(self.oms.request_amend(self.maker.order_id, Decimal("60070.0"), self.t), "REQUESTED")
        new = self.replacement()
        self.assertEqual((self.maker.state, new.state), (OrderState.CANCELLED, OrderState.ACKNOWLEDGED))
        self.oms.ingest_order_update(stream_update(self.plan, new, 1, ExecutionEventType.FILLED, "0.010", self.t + 2 * MS, price="60070"))
        self.oms.ingest_order_update(stream_update(self.plan, new, 2, ExecutionEventType.FILLED, "0.010", self.t + 3 * MS, price="60070"))      # the venue repeats it
        # the OLD order's last fill report arrives after the replacement already filled (a cancel/fill race on the venue): the whole quote was 0.010, so nothing more is booked or hedged
        self.assertEqual(len(self.binance.submits), 1)
        self.assertEqual(sum(1 for f in self.fills() if f.get("venue") == "BYBIT" and f.get("order_id") == new.order_id), 1)
        self.assertEqual(self.oms.lineage_remaining(self.maker.order_id), Decimal("0"))


if __name__ == "__main__":
    unittest.main()
