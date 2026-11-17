"""Owner 12 Nov, amend semantics in the core OMS (CR-MQ part 2's order part; shared by the arb and the MM strategy), OFF by default.

AMEND_PENDING: the old order stays live at the old price and its fills count; the new price counts only after the venue CONFIRMS (not when it accepts the request); a reject or a timeout is followed by a query, never a
blind second order; the lineage keeps a late fill on the old order from over-filling the quote; an urgent cancel during a pending amend takes down every order of the lineage; a route that cannot amend falls back to a
cancel and never leaves two live orders. The simulator side (queue_adapter._replace_own) treats any price amend as a loss of queue place, conservatively (labelled in AMEND_CONFIRMED)."""
import sys
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from crypto_stat_arb.execution import (
    CanonicalOrderUpdate, DispatchPolicy, ExecutionEventType, OrderAction, OrderUpdateSource, StrategyDirectedRouter, VenueOrderStatus,
)
from crypto_stat_arb.hedge_lifecycle import HedgePolicy
from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.models import OrderState, PlanState, Side
from crypto_stat_arb.oms import OMS
from ems_evidence import write_case
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

    def build(self, mode="confirm", enabled=True, directory=None, policy=None, hedge_min_notional=None, hedge_script=("fill",), close_pair=False):
        inst = _ft_instruments()
        if hedge_min_notional is not None:                                                  # a hedge-venue minimum notional above one lot, so a one-lot partial fill is dust
            inst._versions[("BINANCE", INSTR_FT)][0] = replace(inst.get("BINANCE", INSTR_FT), min_notional=Decimal(hedge_min_notional))
        self.bybit, self.binance = AmendVenue("BYBIT", mode), ScriptedVenue("BINANCE", list(hedge_script), price="60000")
        self.oms = OMS(inst, risk(), Ledger(), router=StrategyDirectedRouter(inst, adapters={"BYBIT": self.bybit, "BINANCE": self.binance}),
                       dispatch_policy=DispatchPolicy.FILL_TRIGGERED, hedge_policy=policy or HedgePolicy(retry_limit=1))
        self.oms.amend_enabled = enabled
        self.plan = self.oms.execute(_ft_opportunity(), _ft_books(), maker_venue="BYBIT", **({"close_pair": True} if close_pair else {}))
        self.maker = self.plan.children[0]
        self.assertEqual(self.maker.state, OrderState.ACKNOWLEDGED)

    def tearDown(self):
        """Evidence (owner 12 Nov): the stage trace of every amend case, written when the case passed."""
        outcome = getattr(self, "_outcome", None)
        failed = bool(outcome and (getattr(outcome, "errors", None) or getattr(outcome, "failures", None))) if outcome is not None else False
        result = getattr(self._outcome, "result", None) if hasattr(self, "_outcome") else None
        problems = [t for t, _ in (getattr(result, "errors", []) + getattr(result, "failures", []))] if result is not None else []
        if hasattr(self, "oms") and self not in problems:
            write_case("case7_amend_" + self._testMethodName.removeprefix("test_")[:70], self.oms.ledger.events)

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

    def test_an_amend_confirmed_after_a_partial_fill_that_was_carried_as_dust_keeps_the_plan_partial(self):
        """Found by the v1_10 case 2i / 2n replays (16 Nov): a partial fill below the hedge minimum is carried (no hedge chain), the quote is then amended; the confirmation moves the quote's head to the replacement, which has no
        fill of its own, and the plan was judged ACKNOWLEDGED, an illegal PARTIAL -> ACKNOWLEDGED. The quote's lineage fill keeps it PARTIAL."""
        self.build("accept_only", policy=HedgePolicy(retry_limit=1, dust_carry="next_hedge"), hedge_min_notional="100")
        self.oms.ingest_order_update(stream_update(self.plan, self.maker, 1, ExecutionEventType.PARTIAL, "0.001", self.t + MS))       # 60 USDT of BTC against a 100 USDT minimum: carried
        self.assertEqual(self.binance.submits, [])
        self.assertEqual(self.plan.state, PlanState.PARTIAL)
        self.assertEqual(self.oms.request_amend(self.maker.order_id, Decimal("60070.0"), self.t + 2 * MS), "REQUESTED")
        new = self.replacement()
        self.oms.ingest_order_update(stream_update(self.plan, new, 1, ExecutionEventType.ACKNOWLEDGED, "0", self.t + 5 * MS, price="60070"))
        self.assertEqual(len(self.events("AMEND_CONFIRMED")), 1)
        self.assertEqual((self.maker.state, new.state), (OrderState.CANCELLED, OrderState.ACKNOWLEDGED))
        self.assertEqual(self.plan.state, PlanState.PARTIAL)                              # never back to ACKNOWLEDGED

    def test_an_amend_pending_on_a_part_filled_quote_with_an_acknowledged_hedge_keeps_the_plan_partial(self):
        """Found by the v1_10 case 2i timeout variant (16 Nov): the quote is 0.4 filled, its hedge IOC is acknowledged and not yet filled, an amend is pending (the old order reads AMEND_PENDING, which no plan state maps to):
        the plan was judged by the acknowledged hedge alone, ACKNOWLEDGED, an illegal move back from PARTIAL."""
        self.build("accept_only", hedge_script=("ack",))
        self.oms.ingest_order_update(stream_update(self.plan, self.maker, 1, ExecutionEventType.PARTIAL, "0.004", self.t + MS))
        hedges = [o for o in self.plan.children if o.order_role == "HEDGE"]
        self.assertEqual([h.state for h in hedges], [OrderState.ACKNOWLEDGED])
        self.assertEqual(self.plan.state, PlanState.PARTIAL)
        self.assertEqual(self.oms.request_amend(self.maker.order_id, Decimal("60070.0"), self.t + 2 * MS), "REQUESTED")
        self.assertEqual(self.maker.state, OrderState.AMEND_PENDING)
        self.oms.ingest_order_update(stream_update(self.plan, hedges[0], 1, ExecutionEventType.ACKNOWLEDGED, "0", self.t + 3 * MS, price="60000"))   # a report about the hedge while the amend is pending
        self.assertEqual(self.plan.state, PlanState.PARTIAL)

    def test_an_amended_closing_quote_stays_reduce_only_and_its_hedge_too(self):
        """Order 3 B, lineage unchanged: the replacement of a closing quote is reduce-only like the quote, the lineage's fills are counted once and the hedge that follows is reduce-only."""
        self.build("confirm", close_pair=True)
        self.assertTrue(self.maker.reduce_only)
        self.assertEqual(self.oms.request_amend(self.maker.order_id, Decimal("60070.0"), self.t), "REQUESTED")
        new = self.replacement()
        self.assertTrue(new.reduce_only)                                                    # the replacement carries the flag
        self.assertTrue(self.oms._hedge_specs[self.plan.parent_id]["close_pair"])
        self.oms.ingest_order_update(stream_update(self.plan, new, 2, ExecutionEventType.FILLED, "0.010", self.t + 5 * MS, price="60070"))
        (hedge,) = [o for o in self.plan.children if o.order_role == "HEDGE"]
        self.assertTrue(hedge.reduce_only)
        self.assertEqual(hedge.quantity, Decimal("0.010"))                                  # hedged = filled, once

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


class MakerQuoteTests(unittest.TestCase):
    """Owner 13 Nov, CR-MQ part 2 on the ems layer: a market-making quote is a PASSIVE intent with pairing "none" through the existing plan model (one post-only child, no hedge spec), with fills booked
    to inventory, amend through request_amend, and the per-pair inventory cap as the layer's admission gate. Off unless the layer is built with pairing none."""
    CAP = "0.020"

    def build(self, mode="confirm", cap=CAP, risk_mode="enforce"):
        from crypto_stat_arb.ems.config import ExecutionConfig
        from crypto_stat_arb.ems.layer import ExecutionLayer, IntentObserver
        inst = _ft_instruments()
        self.bybit, self.binance = AmendVenue("BYBIT", mode), ScriptedVenue("BINANCE", ["fill"], price="60000")
        self.oms = OMS(inst, risk(), Ledger(), router=StrategyDirectedRouter(inst, adapters={"BYBIT": self.bybit, "BINANCE": self.binance}),
                       dispatch_policy=DispatchPolicy.FILL_TRIGGERED, hedge_policy=HedgePolicy(retry_limit=1))
        self.oms.amend_enabled = True
        self.layer = ExecutionLayer(ExecutionConfig(pairing="none", quote={"inventory_cap_base": cap}, risk_reservation=risk_mode), instrument=INSTR_FT)
        self.layer.set_risk(lambda: None, lambda: {v: float(self.oms.ledger.positions.get((v, INSTR_FT), Decimal("0"))) for v in ("BYBIT", "BINANCE")})
        self.oms.observer = IntentObserver(self.layer)
        self.t = 100 * MS

    def quote(self, qid="q1", side=Side.BUY, price="60050.0", qty="0.010"):
        return self.oms.execute_quote(quote_id=qid, strategy_id="mm", instrument_id=INSTR_FT, venue="BYBIT", side=side, price=Decimal(price), quantity=Decimal(qty),
                                      books=_ft_books(), observed_ts_ns=self.t)

    def events(self, *names):
        return [e for e in self.oms.ledger.events if e["event_type"] in names]

    def fills(self):
        return [e for e in self.oms.ledger.events if e["event_type"] == "FILL"]

    def no_hedge_anywhere(self):
        self.assertEqual(self.oms._hedge_specs, {})
        self.assertEqual(self.oms.hedge_chains, {})
        self.assertEqual(self.binance.submits, [])
        self.assertEqual(self.events("HEDGE_CHAIN_OPENED", "HEDGE_SPEC", "HEDGE_LOT_RESIDUAL", "HEDGE_LOT_RESIDUAL_ALARM", "RESIDUAL_DETECTED", "RESIDUAL_LIMIT_BREACH", "UNWIND_EXHAUSTED_ALARM"), [])
        self.assertEqual({j["role"] for j in self.layer.journal if j["event"] == "intent"} - {"primary"}, set())      # the layer journal holds no HEDGE and no UNWIND intent

    def test_a_quote_is_one_post_only_child_with_no_hedge_spec(self):
        self.build()
        plan = self.quote()
        self.assertEqual(len(plan.children), 1)
        order = plan.children[0]
        self.assertEqual((order.post_only, order.time_in_force, order.order_role, order.side, order.state), (True, "GTC", "PRIMARY", Side.BUY, OrderState.ACKNOWLEDGED))
        self.assertIn(plan.parent_id, self.oms._quote_plans)
        self.assertEqual(len(self.bybit.submits), 1)
        self.no_hedge_anywhere()
        self.assertTrue(order.intent_id)                                                     # the order carries its layer intent, journaled before it was sent
        self.assertEqual(self.oms.execute_quote.__name__, "execute_quote")
        self.assertEqual(self.oms.carried_residual_base(), Decimal("0"))

    def test_a_fill_is_booked_to_inventory_once_with_no_hedge_and_the_plan_closes(self):
        self.build()
        plan = self.quote()
        order = plan.children[0]
        self.oms.ingest_order_update(stream_update(plan, order, 1, ExecutionEventType.PARTIAL, "0.004", self.t + 2 * MS, price="60050"))
        self.oms.ingest_order_update(stream_update(plan, order, 2, ExecutionEventType.PARTIAL, "0.004", self.t + 3 * MS, price="60050"))      # a repeat of the same report
        self.assertEqual(sum((Decimal(str(f["fill_quantity"])) for f in self.fills()), Decimal("0")), Decimal("0.004"))
        self.assertNotEqual(plan.state, PlanState.CLOSED)                                    # still resting
        self.oms.ingest_order_update(stream_update(plan, order, 3, ExecutionEventType.FILLED, "0.010", self.t + 4 * MS, price="60050"))
        self.assertEqual(sum((Decimal(str(f["fill_quantity"])) for f in self.fills()), Decimal("0")), Decimal("0.010"))
        self.assertEqual(self.oms.ledger.positions[("BYBIT", INSTR_FT)], Decimal("0.010"))      # inventory, not flat: nothing hedges it
        self.assertEqual((plan.state, plan.terminal_outcome), (PlanState.CLOSED, "QUOTE_FILLED"))
        self.no_hedge_anywhere()
        self.assertEqual(self.oms.carried_residual_base(), Decimal("0"))                      # the quote's fills never count as an arb residual
        snap = self.oms.residual_risk_snapshot()
        self.assertEqual((snap["open_residual_plans"], snap["total_residual_base"]), (0, "0"))

    def test_a_fill_during_amend_pending_is_booked_once_and_the_amended_quote_completes(self):
        self.build("accept_only")
        plan = self.quote()
        old = plan.children[0]
        self.assertEqual(self.oms.request_amend(old.order_id, Decimal("60055.0"), self.t + MS), "REQUESTED")
        self.assertEqual(old.state, OrderState.AMEND_PENDING)
        new = next(x for x in plan.children if x.replaces_order_id)
        self.oms.ingest_order_update(stream_update(plan, old, 1, ExecutionEventType.PARTIAL, "0.004", self.t + 2 * MS, price="60050"))      # the old order fills while the amend is pending
        self.oms.ingest_order_update(stream_update(plan, old, 2, ExecutionEventType.PARTIAL, "0.004", self.t + 3 * MS, price="60050"))      # and the venue repeats the report
        self.assertEqual(len([f for f in self.fills() if f.get("order_id") == old.order_id]), 1)                                              # booked once
        self.oms.ingest_order_update(stream_update(plan, new, 1, ExecutionEventType.ACKNOWLEDGED, "0", self.t + 4 * MS, price="60055"))      # the venue confirms the amend
        self.assertEqual(len(self.events("AMEND_CONFIRMED")), 1)
        self.assertNotEqual(plan.state, PlanState.CLOSED)                                    # the replacement is live: the quote is still working (the old order's CANCELLED does not end it)
        self.oms.ingest_order_update(stream_update(plan, new, 2, ExecutionEventType.FILLED, "0.006", self.t + 5 * MS, price="60055"))
        self.assertEqual(sum((Decimal(str(f["fill_quantity"])) for f in self.fills()), Decimal("0")), Decimal("0.010"))
        self.assertEqual((plan.state, plan.terminal_outcome), (PlanState.CLOSED, "QUOTE_FILLED"))
        self.assertEqual(self.oms.ledger.positions[("BYBIT", INSTR_FT)], Decimal("0.010"))
        self.no_hedge_anywhere()
        self.assertEqual(len(self.bybit.submits), 1)                                         # one order was ever submitted; the amend was a REPLACE

    def test_a_quote_is_cancelled_and_its_plan_closes_with_nothing_filled(self):
        self.build()
        plan = self.quote()
        order = plan.children[0]
        self.assertTrue(self.oms.urgent_cancel(order.order_id, self.t + MS, "withdraw"))
        self.assertEqual(order.state, OrderState.CANCELLED)
        self.assertEqual((plan.state, plan.terminal_outcome), (PlanState.CLOSED, "QUOTE_ENDED_UNFILLED"))
        self.assertEqual(self.fills(), [])
        self.no_hedge_anywhere()

    def test_the_inventory_cap_refuses_a_quote_that_would_exceed_it_and_leaves_nothing_reserved(self):
        self.build(cap="0.015")
        first = self.quote("q1", qty="0.010")
        self.assertEqual(first.children[0].state, OrderState.ACKNOWLEDGED)
        reserved_before = (dict(self.oms.risk.reserved_position), self.oms.risk.reserved_gross, len(self.layer.reservations.held))
        refused = self.quote("q2", qty="0.010")                                              # 0.010 resting + 0.010 more > 0.015
        self.assertEqual((refused.state, refused.terminal_outcome), (PlanState.CLOSED, "REJECTED"))
        self.assertEqual(self.events("LAYER_RISK_REFUSED")[-1]["reason"].split(": ")[-1], "inventory_cap")
        self.assertEqual(len(self.bybit.submits), 1)                                         # the refused quote was never sent
        self.assertEqual((dict(self.oms.risk.reserved_position), self.oms.risk.reserved_gross, len(self.layer.reservations.held)), reserved_before)
        sell = self.quote("q3", side=Side.SELL, price="60070.0", qty="0.010")                # the other side nets against the resting buy: it is inside the cap
        self.assertEqual(sell.children[0].state, OrderState.ACKNOWLEDGED)
        self.no_hedge_anywhere()

    def test_in_count_mode_the_cap_only_counts(self):
        self.build(cap="0.015", risk_mode="count")
        self.quote("q1", qty="0.010")
        over = self.quote("q2", qty="0.010")
        self.assertEqual(over.children[0].state, OrderState.ACKNOWLEDGED)
        self.assertEqual(self.layer.counts["risk_would_refuse"], 1)

    def test_a_quote_needs_the_layer_with_pairing_none(self):
        from crypto_stat_arb.ems.config import ExecutionConfig
        from crypto_stat_arb.ems.layer import ExecutionLayer, IntentObserver
        self.build()
        self.oms.observer = IntentObserver(ExecutionLayer(ExecutionConfig(), instrument=INSTR_FT))      # the arb pairing
        with self.assertRaises(ValueError):
            self.quote()
        self.oms.observer = None
        with self.assertRaises(ValueError):
            self.quote()
        self.assertEqual(self.bybit.submits, [])

    def test_an_arb_package_is_refused_under_pairing_none_and_nothing_is_sent(self):
        """One OMS is a market-making quote book or an arb book: under pairing none the arb entry refuses (the arb gate would otherwise read as the inventory cap)."""
        self.build()
        with self.assertRaises(ValueError):
            self.oms.execute(_ft_opportunity("arb-1"), _ft_books(), maker_venue="BYBIT")
        self.assertEqual((self.bybit.submits, self.binance.submits, self.oms.plans), ([], [], {}))

    def test_the_layer_journals_the_replacements_fills_and_the_cap_counts_each_fill_once(self):
        """Amend replacement orders belong to the quote's intent: the layer's fill total equals the ledger's, and the reservation shrinks with each fill, so the inventory cap neither double counts nor loses a fill."""
        self.build("accept_only", cap="0.015")
        plan = self.quote("q1", qty="0.010")
        old = plan.children[0]
        self.oms.request_amend(old.order_id, Decimal("60055.0"), self.t + MS)
        new = next(x for x in plan.children if x.replaces_order_id)
        self.assertEqual(new.intent_id, old.intent_id)
        self.oms.ingest_order_update(stream_update(plan, old, 1, ExecutionEventType.PARTIAL, "0.004", self.t + 2 * MS, price="60050"))
        self.oms.ingest_order_update(stream_update(plan, new, 1, ExecutionEventType.ACKNOWLEDGED, "0", self.t + 3 * MS, price="60055"))      # confirmed
        self.oms.ingest_order_update(stream_update(plan, new, 2, ExecutionEventType.PARTIAL, "0.003", self.t + 4 * MS, price="60055"))       # the replacement part-fills and keeps resting
        ledger_base = self.oms.ledger.positions[("BYBIT", INSTR_FT)]
        self.assertEqual(ledger_base, Decimal("0.007"))
        self.assertEqual(sum((Decimal(str(f["qty"])) for f in self.layer.fills), Decimal("0")), ledger_base)       # the layer saw both orders' fills
        held = next(iter(self.layer.reservations.held.values()))
        self.assertEqual(round(held.remaining, 9), 0.003)                                                    # 0.010 quoted less the 0.007 filled: what is still resting
        over = self.quote("q2", qty="0.006")                                                                 # 0.007 filled + 0.003 resting + 0.006 = 0.016 > 0.015
        self.assertEqual(over.terminal_outcome, "REJECTED")
        fits = self.quote("q3", qty="0.005")                                                                 # = 0.015
        self.assertEqual(fits.children[0].state, OrderState.ACKNOWLEDGED)

    def test_a_quote_plan_survives_a_restart_as_a_quote_with_no_residual_and_no_unwind(self):
        """The QUOTE_PLAN marker is persisted: after a restart a filled quote is still inventory, never an arb residual (no HEDGE_UNWIND, no UNWIND child, nothing carried)."""
        import tempfile
        from crypto_stat_arb.plan_store import ParentPlanStore
        self.build()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oms.sqlite"
            store = ParentPlanStore(path)
            from crypto_stat_arb.ems.config import ExecutionConfig
            from crypto_stat_arb.ems.layer import ExecutionLayer, IntentObserver
            inst = _ft_instruments()
            bybit = AmendVenue("BYBIT", "confirm")
            oms = OMS(inst, risk(), Ledger(), plan_store=store, router=StrategyDirectedRouter(inst, adapters={"BYBIT": bybit, "BINANCE": ScriptedVenue("BINANCE", ["fill"], price="60000")}),
                      dispatch_policy=DispatchPolicy.FILL_TRIGGERED, hedge_policy=HedgePolicy(retry_limit=1))
            oms.observer = IntentObserver(ExecutionLayer(ExecutionConfig(pairing="none", quote={"inventory_cap_base": "0.020"}), instrument=INSTR_FT))
            plan = oms.execute_quote(quote_id="q1", strategy_id="mm", instrument_id=INSTR_FT, venue="BYBIT", side=Side.BUY, price=Decimal("60050.0"), quantity=Decimal("0.010"), books=_ft_books(), observed_ts_ns=self.t)
            oms.ingest_order_update(stream_update(plan, plan.children[0], 1, ExecutionEventType.PARTIAL, "0.004", self.t + 2 * MS, price="60050"))      # filled in part, still resting
            store.close()
            store = ParentPlanStore(path)
            again = OMS(inst, risk(), Ledger(), plan_store=store, router=StrategyDirectedRouter(inst, adapters={"BYBIT": AmendVenue("BYBIT"), "BINANCE": ScriptedVenue("BINANCE", ["fill"])}),
                        dispatch_policy=DispatchPolicy.FILL_TRIGGERED, hedge_policy=HedgePolicy(retry_limit=1))
            self.assertIn(plan.parent_id, again._quote_plans)                                                 # the marker came back
            restored = again.plans[plan.parent_id]
            again._recompute_plan_economics(restored, self.t + 3 * MS)
            self.assertEqual(restored.residual_base, Decimal("0.004"))
            again.enforce_residual_limits(self.t + 5 * MS)
            self.assertNotEqual(restored.state, PlanState.HEDGE_UNWIND)
            self.assertEqual([x for x in restored.children if x.order_role == "UNWIND"], [])
            self.assertEqual(again.carried_residual_base(), Decimal("0"))
            self.assertEqual(again.residual_risk_snapshot()["open_residual_plans"], 0)
            store.close()


class QuoteBookStrategy:
    """A market-making strategy test double: scripted actions per call, every callback logged."""
    quote_book = True
    strategy_id = "mm"

    def __init__(self):
        self.script, self.log, self.calls = [], [], 0

    def quotes(self, books, now_ns):
        self.calls += 1
        return self.script.pop(0) if self.script else []

    def on_quote_ack(self, quote_id, accepted, reason, ts):
        self.log.append(("ack", quote_id, accepted, reason))

    def on_quote_fill(self, quote_id, qty, price, fee, liquidity, ts):
        self.log.append(("fill", quote_id, qty, price, liquidity))

    def on_quote_closed(self, quote_id, reason, ts):
        self.log.append(("closed", quote_id, reason))

    def on_quote_amend_refused(self, quote_id, reason, ts):
        self.log.append(("amend_refused", quote_id, reason))


class SessionQuoteBookTests(unittest.TestCase):
    """Owner 13 Nov, CR-MQ part 3: a strategy with `quote_book = True` is asked for quote actions inside TradingSession.on_event (replay, live paper and live share that call site) and gets ack / fill / closed
    callbacks keyed by its own quote id. place -> execute_quote, amend -> request_amend, cancel -> the normal urgent cancel, an exit IOC for inventory; the layer's inventory cap and the rate-limit reserve apply."""

    def build(self, *, cap="0.60", cancel_answers=True, risk_mode="enforce"):
        import test_session_breaker as sb
        from crypto_stat_arb.ems.config import ExecutionConfig
        from crypto_stat_arb.session import ExecutionMode, TradingSession
        self.sb, self.A, self.B = sb, sb.A, sb.B
        parent = self

        class Venue(AmendVenue):
            def handle(self, req, book):
                if req.action == OrderAction.CANCEL and not cancel_answers:
                    self.cancels.append(req.target_order_id)
                    return ()                                                            # the venue has not answered the cancel yet: the order is CANCEL_PENDING and can still fill
                if req.action == OrderAction.SUBMIT and req.time_in_force.value == "IOC":
                    self.submits.append(req)
                    return (_cb(req, ExecutionEventType.FILLED, cum=str(req.quantity), last=str(req.quantity), price=str(req.price)),)
                return super().handle(req, book)

        self.bybit, self.binance = Venue(self.B), Venue(self.A)
        self.strategy = QuoteBookStrategy()
        self.session = TradingSession(sb.CFG, execution_mode=ExecutionMode.INTERNAL_SIMULATION, adapters={self.B: self.bybit, self.A: self.binance}, strategies=[self.strategy],
                                      intent_journal=True, layer_risk_mode=risk_mode, execution_config=ExecutionConfig(pairing="none", quote={"inventory_cap_base": cap}))
        self.session.oms.amend_enabled = True
        sb.with_books(self.session)
        self.t = 10 * 1_000_000_000
        self.tick()                                                                                 # warm-up: both books fresh before the first action
    def tick(self, script=None, step=1000):
        if script is not None:
            self.strategy.script.append(script)
        self.t += step * 1_000_000
        for venue in (self.B, self.A):                                                              # both books fresh at every step (the pre-trade check refuses a stale book)
            self.session.on_event(self.sb.snapshot(venue, self.t, seq=int(self.t / 1_000_000)), gate_allowed=True)

    def act(self, script):
        """The actions go out at one event; the session's latency model releases the order and the venue answers at the NEXT event: two steps, so the callbacks of the action have been delivered."""
        self.tick(script)
        self.tick()

    def plan_of(self, qid):
        return self.session.oms.plans[self.session._quote_book[qid]["parent_id"]]

    def update(self, plan, order, n, state, cum, price="99.5"):
        self.session.oms.ingest_order_update(stream_update(plan, order, n, state, cum, self.t + n, price=price))
        self.tick()

    def fills(self):
        return [e for e in self.session.ledger.events if e["event_type"] == "FILL"]

    def test_a_quote_is_placed_amended_partly_filled_and_cancelled_with_callbacks_keyed_by_its_id(self):
        from crypto_stat_arb.strategy import AmendQuote, CancelQuote, PlaceQuote
        self.build()
        self.act([PlaceQuote("q1", self.B, "buy", Decimal("99.5"), Decimal("0.5"))])
        plan = self.plan_of("q1")
        self.assertEqual(self.strategy.log[0], ("ack", "q1", True, ""))
        self.assertEqual((len(plan.children), plan.children[0].post_only, plan.children[0].state), (1, True, OrderState.ACKNOWLEDGED))
        self.assertEqual(self.session.oms._hedge_specs, {})                                       # a quote has no hedge
        self.act([AmendQuote("q1", Decimal("99.6"))])
        new = next(x for x in plan.children if x.replaces_order_id)
        self.assertEqual((new.limit_price, plan.children[0].state), (Decimal("99.6"), OrderState.CANCELLED))      # REPLACE confirmed: the replacement is the live head
        self.update(plan, new, 1, ExecutionEventType.PARTIAL, "0.2", "99.6")
        self.assertEqual([x for x in self.strategy.log if x[0] == "fill"], [("fill", "q1", Decimal("0.2"), Decimal("99.6"), "MAKER")])
        self.act([CancelQuote("q1")])
        self.assertIn(new.order_id, self.bybit.cancels)
        self.assertEqual(self.strategy.log[-1], ("closed", "q1", "cancelled_after_partial_fill"))
        self.assertEqual(plan.state, PlanState.CLOSED)
        self.assertEqual(self.session.ledger.positions[(self.B, self.session.cfg.instrument_id)], Decimal("0.2"))          # the partial fill is inventory
        self.assertEqual(self.session.oms.carried_residual_base(), Decimal("0"))
        self.assertEqual((self.binance.submits, self.session.oms.hedge_chains), ([], {}))                                      # no hedge was ever sent, no chain exists

    def test_a_late_fill_after_the_cancel_request_is_booked_once_and_reported_once(self):
        from crypto_stat_arb.strategy import CancelQuote, PlaceQuote
        self.build(cancel_answers=False)
        self.act([PlaceQuote("q1", self.B, "buy", Decimal("99.5"), Decimal("0.5"))])
        plan = self.plan_of("q1")
        order = plan.children[0]
        self.act([CancelQuote("q1")])
        self.assertEqual(order.state, OrderState.CANCEL_PENDING)                                   # cancel sent, not answered: still live
        self.update(plan, order, 1, ExecutionEventType.FILLED, "0.5")                              # the fill that raced the cancel
        self.update(plan, order, 2, ExecutionEventType.FILLED, "0.5")                              # and the venue repeats it
        self.assertEqual(len(self.fills()), 1)                                                     # booked once
        self.assertEqual([x for x in self.strategy.log if x[0] == "fill"], [("fill", "q1", Decimal("0.5"), Decimal("99.5"), "MAKER")])      # reported once
        self.assertEqual(self.strategy.log[-1], ("closed", "q1", "filled"))
        self.assertEqual(self.strategy.log.count(("closed", "q1", "filled")), 1)

    def test_the_inventory_cap_refuses_a_quote_with_a_reason_and_nothing_is_sent(self):
        from crypto_stat_arb.strategy import PlaceQuote
        self.build(cap="0.60")
        self.act([PlaceQuote("q1", self.B, "buy", Decimal("99.5"), Decimal("0.5"))])
        self.act([PlaceQuote("q2", self.B, "buy", Decimal("99.4"), Decimal("0.5"))])               # 0.5 resting + 0.5 > 0.60
        self.assertEqual([x for x in self.strategy.log if x[1] == "q2"][0][:3], ("ack", "q2", False))
        closed = [x for x in self.strategy.log if x[0] == "closed" and x[1] == "q2"]
        self.assertTrue(closed and closed[0][2].startswith("rejected:") and "inventory_cap" in closed[0][2], closed)
        self.assertEqual(len(self.bybit.submits), 1)

    def test_an_exit_ioc_reduces_inventory_at_its_cap_and_is_never_blocked_by_the_stops(self):
        from crypto_stat_arb.strategy import ExitInventory, PlaceQuote
        self.build()
        self.act([PlaceQuote("q1", self.B, "buy", Decimal("99.5"), Decimal("0.5"))])
        plan = self.plan_of("q1")
        self.update(plan, plan.children[0], 1, ExecutionEventType.FILLED, "0.5")
        inst = self.session.cfg.instrument_id
        self.assertEqual(self.session.ledger.positions[(self.B, inst)], Decimal("0.5"))
        self.session.flattening = True                                                             # a stop that blocks new quotes
        self.act([ExitInventory("x1", self.B, "sell", Decimal("0.5"), Decimal("99.0"))])
        exit_order = self.plan_of("x1").children[0]
        self.assertEqual((exit_order.order_role, exit_order.time_in_force, exit_order.reduce_only, exit_order.limit_price, exit_order.state), ("UNWIND", "IOC", True, Decimal("99.0"), OrderState.FILLED))
        self.assertEqual(self.session.ledger.positions[(self.B, inst)], Decimal("0"))
        self.assertEqual(self.strategy.log[-1], ("closed", "x1", "filled"))
        self.assertEqual(self.session.oms.carried_residual_base(), Decimal("0"))

    def test_the_shadow_reconciliation_compares_the_fee_of_each_fill_not_the_orders_cumulative_fee(self):
        """Found in the 29 Oct rehearsal: a FILL event's `fee` is the order's cumulative fee, so a second partial fill of one order was flagged as a fee mismatch. The journal now uses `fee_delta`."""
        from crypto_stat_arb.ems.reconcile import reconcile_session
        from crypto_stat_arb.fees import Liquidity
        from crypto_stat_arb.strategy import PlaceQuote
        self.build()
        self.act([PlaceQuote("q1", self.B, "buy", Decimal("99.5"), Decimal("0.5"))])
        plan = self.plan_of("q1")
        self.update(plan, plan.children[0], 1, ExecutionEventType.PARTIAL, "0.2")
        self.update(plan, plan.children[0], 2, ExecutionEventType.FILLED, "0.5")                    # the second fill: 0.3 more, the order's fee is cumulative
        self.assertEqual(len(self.fills()), 2)
        inst = self.session.cfg.instrument_id
        fee_bps = {v: tuple(float(self.session.oms.instruments.fee_rule(v, inst, self.t).effective_bps(liq)[0]) for liq in (Liquidity.MAKER, Liquidity.TAKER)) for v in (self.A, self.B)}
        doc = reconcile_session(self.session, fee_bps=fee_bps)
        self.assertEqual((doc["ok"], doc["problems"].get("fee", [])), (True, []), doc["problems"])
        self.assertAlmostEqual(doc["fees_journal"], doc["fees_engine"], places=9)

    def test_a_new_quote_under_a_stop_is_blocked_with_a_reason(self):
        from crypto_stat_arb.strategy import PlaceQuote
        self.build()
        self.session.flattening = True
        self.act([PlaceQuote("q1", self.B, "buy", Decimal("99.5"), Decimal("0.5"))])
        self.assertEqual(self.strategy.log, [("ack", "q1", False, "blocked:flattening"), ("closed", "q1", "blocked:flattening")])
        self.assertEqual(self.bybit.submits, [])

    def test_a_strategy_without_the_exact_opt_in_is_never_called_so_arb_strategies_are_untouched(self):
        self.build()

        class Looks:
            strategy_id = "arb-like"
            quote_book = 1                                                                        # truthy but not True
            def __init__(self):
                self.quotes_called, self.evaluated = 0, 0
            def quotes(self, books, now):
                self.quotes_called += 1
                return []
            def evaluate(self, books, now):
                self.evaluated += 1
                return None
        arb = Looks()
        self.session.strategies.append(arb)
        self.tick()
        self.assertEqual((arb.quotes_called, arb.evaluated), (0, 2))                               # evaluated like any arb strategy (once per book event), never asked for quotes
        self.assertEqual(self.session._quote_book, {})


if __name__ == "__main__":
    unittest.main()
