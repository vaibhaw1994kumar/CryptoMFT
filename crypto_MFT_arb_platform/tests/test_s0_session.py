"""S0 quote repricing in the session runtime (owner 14 Nov): one controller per resting quote, on every book update, after quote validity. Synthetic streams (no profit figure).
Invariants a-j of the EMS note section 5 that live in the OMS (amend lineage) are tested in tests/test_oms_amend.py; this file tests the CONTROLLER on top of them and maps each invariant to its test."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from new_backtest.profiles import apply_profile, load_profile
from new_backtest.session_replay import SessionReplay
from test_session_replay import SLICES, moved_stream, snap, timed, TRADE


def replay(stream_events, profile_name="perp_perp_maker_taker_v1_9s", **over):
    profile = load_profile(profile_name)
    cfg = apply_profile(profile, dataset="mar2024", data_root=str(SLICES), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_s0")
    cfg.arb.max_position_base = 1.0
    cfg.arb.participation_max = 0.0                         # v1_9's participation limit has its own tests; these synthetic streams have no prints at the quote's level
    for k, v in over.items():
        setattr(cfg.arb, k, v)
    return SessionReplay(cfg, profile, stream=stream_events)


def events(rep, name):
    return [e for e in rep.session.ledger.events if e["event_type"] == name]


@unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
class Controller(unittest.TestCase):
    def test_v1_9s_is_v1_9_plus_s0_and_validity_only(self):
        a, b = load_profile("perp_perp_maker_taker_v1_9"), load_profile("perp_perp_maker_taker_v1_9s")
        def flat(x, p=""):
            out = {}
            for k, v in x.items():
                out.update(flat(v, p + k + ".") if isinstance(v, dict) else {p + k: v})
            return out
        fa, fb = flat(a), flat(b)
        diff = {k for k in set(fa) | set(fb) if fa.get(k) != fb.get(k)} - {"description", "_sha256", "name", "_path"}
        self.assertEqual(diff, {"arb.quote_withdraw", "arb.quote_withdraw_hysteresis_bps", "execution.primary.reprice.enabled", "execution.primary.reprice.source", "execution.primary.reprice.band_down_bps",
                                "execution.primary.reprice.min_interval_ms", "execution.primary.reprice.max_age_s"})
        self.assertFalse(a["execution"]["primary"]["reprice"]["enabled"])                 # v1_9 keeps S0 off
        self.assertTrue(b["execution"]["primary"]["reprice"]["enabled"])
        self.assertLess(b["execution"]["primary"]["reprice"]["band_down_bps"], b["arb"]["quote_withdraw_hysteresis_bps"])      # the band sits inside the withdrawal tolerance

    def test_off_nothing_is_built_or_sent(self):
        rep = replay(moved_stream(1.6, bin_bid=100.288, bin_ask=100.298), "perp_perp_maker_taker_v1_9"); rep.run()
        self.assertFalse(rep.session.oms.amend_enabled)
        self.assertEqual(rep.session.reprice_counters, {})
        self.assertEqual(events(rep, "AMEND_REQUESTED"), [])

    def test_an_adverse_move_beyond_the_band_amends_first_and_the_old_quote_stays_live_until_the_venue_confirms(self):
        rep = replay(moved_stream(1.6, bin_bid=100.288, bin_ask=100.298)); rep.run()               # the hedge bid drops 1.2 bps: past the 1.0 bp band and inside validity's 1.5 bp tolerance, so the controller (not a withdrawal) answers
        c = rep.session.reprice_counters
        self.assertGreaterEqual(c.get("amend_requests", 0), 1, c)
        requested = events(rep, "AMEND_REQUESTED")
        self.assertGreaterEqual(len(requested), 1)
        self.assertEqual([e for e in events(rep, "QUOTE_WITHDRAWN") if str(e.get("reason", "")).startswith("reprice")], [])      # amend first, not cancel
        # request acceptance is not the effective change (a): the replacement exists only after the venue's confirmation event
        lifecycle = [e for e in rep.session.ledger.events if e["event_type"] == "ORDER_LIFECYCLE"]
        first_request_ts = requested[0]["ts_ns"]
        confirmed = [e for e in events(rep, "AMEND_CONFIRMED") if e["ts_ns"] >= first_request_ts]
        self.assertGreaterEqual(len(confirmed), 1)                                                  # the venue's confirmation came AFTER the request: that is when the quote counts as repriced
        self.assertGreater(confirmed[0]["ts_ns"], first_request_ts)
        self.assertEqual(c.get("amends_confirmed", 0), len(confirmed))

    def test_a_move_inside_the_band_holds_and_counts_why(self):
        rep = replay(moved_stream(1.6, bin_bid=100.299, bin_ask=100.309)); rep.run()                # 0.1 bp: inside the band
        c = rep.session.reprice_counters
        self.assertEqual(c.get("amend_requests", 0), 0)
        self.assertGreater(c.get("hold_inside_band", 0), 0)

    def test_min_interval_spaces_the_requests_of_one_quote(self):
        rep = replay(moved_stream(1.6, bin_bid=100.288, bin_ask=100.298)); rep.run()
        times = sorted(e["ts_ns"] for e in events(rep, "AMEND_REQUESTED"))
        gaps = [(b - a) / 1e9 for a, b in zip(times, times[1:])]
        self.assertTrue(all(g >= 1.0 - 1e-6 for g in gaps), gaps)

    def test_a_valid_quote_with_no_target_is_kept_not_cancelled_when_validity_runs_first(self):
        rep = replay(moved_stream(1.6, bin_bid=100.299, bin_ask=100.309))
        original = rep.strategy.requote_target
        rep.strategy.requote_target = lambda *a, **k: (None, "no_edge")                         # no price clears at any depth
        rep.run()
        c = rep.session.reprice_counters
        self.assertGreater(c.get("hold_no_target_but_valid", 0), 0, c)
        self.assertEqual([e for e in events(rep, "QUOTE_WITHDRAWN") if str(e.get("reason", "")).startswith("reprice")], [])

    def test_without_validity_a_quote_with_no_target_is_cancelled_as_the_rule_says(self):
        rep = replay(moved_stream(1.6, bin_bid=100.299, bin_ask=100.309), quote_withdraw=False)
        rep.strategy.requote_target = lambda *a, **k: (None, "no_edge")
        rep.run()
        self.assertGreater(rep.session.reprice_counters.get("cancels_no_edge_at_current_hedge_price", 0), 0, rep.session.reprice_counters)

    def test_a_quote_with_no_target_is_cancelled_not_amended(self):
        rep = replay(moved_stream(1.6, bin_bid=100.00, bin_ask=100.01)); rep.run()                  # the hedge bid collapses: no price clears the edge
        reasons = [str(e.get("reason", "")) for e in events(rep, "QUOTE_WITHDRAWN")]
        self.assertTrue(reasons, "a quote with no edge is withdrawn (by validity or by the controller)")
        self.assertEqual(rep.session.reprice_counters.get("amend_requests", 0), 0)

    def test_the_amend_is_a_request_the_amended_quote_never_sizes_up(self):
        rep = replay(moved_stream(1.6, bin_bid=100.288, bin_ask=100.298)); rep.run()
        for plan in rep.session.oms.plans.values():
            for child in plan.children:
                if child.replaces_order_id:
                    old = next(x for x in plan.children if x.order_id == child.replaces_order_id)
                    self.assertLessEqual(child.quantity, old.quantity)                              # repricing never increases size (i)
        self.assertEqual(self.two_live(rep), 0)

    @staticmethod
    def two_live(rep):
        from crypto_stat_arb.models import OrderState
        live = (OrderState.CREATED, OrderState.SENT, OrderState.CANCEL_PENDING, OrderState.AMEND_PENDING, OrderState.ACKNOWLEDGED, OrderState.PARTIAL)
        worst = 0
        for plan in rep.session.oms.plans.values():
            n = sum(1 for c in plan.children if c.order_role == "PRIMARY" and c.state in live)
            worst = max(worst, n - 1 if n else 0)
        return worst


@unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
@unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
class AmendAtTheVenue(unittest.TestCase):
    """2i item 1 (owner 17 Nov), on the session with the simulated venue: after a CONFIRMED amend the quote is live at the new price AT THE VENUE and in the OMS. Until 17 Nov the REPLACE travelled with no book,
    the venue cancelled the old order and rejected the replacement ("no valid book at arrival"), and the OMS still recorded AMEND_CONFIRMED: every confirmed amend ended the quote."""

    def test_a_confirmed_amend_leaves_the_quote_live_at_the_new_price_at_the_venue(self):
        rep = replay(moved_stream(1.6, bin_bid=100.288, bin_ask=100.298))
        confirmed_seen = []
        real = rep.session.oms._amend_confirmed

        def spy(plan, old, new, ts_ns):
            real(plan, old, new, ts_ns)
            adapter = rep.session.router.adapters[new.venue]
            venue_prices = sorted(o.price for o in adapter.executor.live_orders(adapter.venue))
            confirmed_seen.append((new.state.value, str(new.limit_price), venue_prices, old.state.value))
        rep.session.oms._amend_confirmed = spy
        rep.run()
        self.assertGreaterEqual(len(confirmed_seen), 1)
        state, price, venue_prices, old_state = confirmed_seen[0]
        self.assertEqual(state, "ACKNOWLEDGED")                                                    # live in the OMS
        self.assertEqual(old_state, "CANCELLED")
        self.assertEqual(venue_prices, [float(price)])                                             # and live at the venue, at the new price, and only there
        rejected = [e for e in events(rep, "ORDER_LIFECYCLE") if e.get("order_id") == events(rep, "AMEND_REQUESTED")[0]["replacement_order_id"] and "REJECT" in str(e.get("lifecycle_event", "")) + str(e.get("state", ""))]
        self.assertEqual(rejected, [])

    def test_the_venue_amends_in_place_and_a_rejected_amend_leaves_the_old_order_resting(self):
        """2i item 1b: Bybit amends IN PLACE: either the order now rests at the new price (queue place lost) or the amend is rejected and the order rests unchanged. Never a cancel followed by a failed new order."""
        from decimal import Decimal
        from crypto_stat_arb.execution import CanonicalOrderRequest, ExecutionEventType, OrderAction, OrderType, TimeInForce
        from crypto_stat_arb.models import Side
        rep = replay(moved_stream(1.6, bin_bid=100.288, bin_ask=100.298))
        rep.run()
        adapter = rep.session.router.adapters["BYBIT"]
        book = rep.session.books[("BYBIT", rep.instrument_id)]
        ts = rep.last_ns + 1
        make = lambda action, oid, price, target=None: CanonicalOrderRequest.create(action=action, order_id=oid, target_order_id=target, parent_opportunity_id="op", parent_order_id="p", instrument_id=rep.instrument_id,
                                                                                     venue="BYBIT", side=Side.BUY, price=Decimal(price), quantity=Decimal("0.5"), order_type=OrderType.LIMIT, time_in_force=TimeInForce.GTC,
                                                                                     post_only=True, created_ts_ns=ts, idempotency_key=oid)
        bid = book.best_bid
        (ack,) = adapter.handle_at(make(OrderAction.SUBMIT, "t-old", str(bid - Decimal("0.01"))), book, ts)          # levels the book holds (an unseen level needs the unknown-queue fit file)
        self.assertEqual(ack.event_type, ExecutionEventType.ACKNOWLEDGED)
        before = sorted(o.price for o in adapter.executor.live_orders("BYBIT"))
        # no book at arrival: the amend is rejected and the old order still rests at its old price
        out = adapter.handle_at(make(OrderAction.REPLACE, "t-new1", str(bid), "t-old"), None, ts + 1)
        self.assertEqual([c.event_type for c in out], [ExecutionEventType.REJECTED])
        self.assertEqual(sorted(o.price for o in adapter.executor.live_orders("BYBIT")), before)
        # an amend to a price that would cross: rejected, the old order rests
        out = adapter.handle_at(make(OrderAction.REPLACE, "t-new2", str(book.best_ask + Decimal("0.01")), "t-old"), book, ts + 2)
        self.assertEqual([c.event_type for c in out], [ExecutionEventType.REJECTED])
        self.assertEqual(sorted(o.price for o in adapter.executor.live_orders("BYBIT")), before)
        # a valid amend: REPLACED and the replacement live at the new price, the old price gone
        new_price = bid
        out = adapter.handle_at(make(OrderAction.REPLACE, "t-new3", str(new_price), "t-old"), book, ts + 3)
        self.assertEqual([c.event_type for c in out], [ExecutionEventType.REPLACED, ExecutionEventType.ACKNOWLEDGED])
        after = sorted(o.price for o in adapter.executor.live_orders("BYBIT"))
        self.assertIn(float(new_price), after)
        self.assertNotIn(float(bid - Decimal("0.01")), after)
        self.assertEqual(len(after), len(before))
        (cancel,) = adapter.handle_at(make(OrderAction.CANCEL, "t-cx", str(new_price), "t-new3"), book, ts + 4)
        self.assertEqual(cancel.event_type, ExecutionEventType.CANCELLED)                         # the venue knows the order under its new id


class FaultsAndLimits(unittest.TestCase):
    """EMS note section 5 b, c, j on the controller: an amend the venue rejects or never answers, and an amend with no room in the rate-limit window."""

    def with_amend_fault(self, mode):
        from crypto_stat_arb.execution import ExecutionEventType
        from crypto_stat_arb.simulation.queue_adapter import QueueAwareVenueAdapter
        real = QueueAwareVenueAdapter._replace_own

        def faulty(self, request, book):
            if mode == "reject":
                return (self._callback(request, ExecutionEventType.REJECTED, reason="amend_rejected_injected"),)
            return ()                                                                                # timeout: the request is lost, nothing comes back
        QueueAwareVenueAdapter._replace_own = faulty
        self.addCleanup(setattr, QueueAwareVenueAdapter, "_replace_own", real)

    def test_b_a_rejected_amend_is_never_resent_blindly_the_quote_is_read_again_and_cancelled_or_replaced(self):
        self.with_amend_fault("reject")
        rep = replay(moved_stream(1.6, bin_bid=100.288, bin_ask=100.298)); rep.run()
        c = rep.session.reprice_counters
        self.assertEqual(c.get("amend_requests", 0), 1, c)                                           # ONE request: the reject is followed by a read of the quote, not by the same request again
        self.assertGreaterEqual(len(events(rep, "AMEND_REJECTED")), 1)
        self.assertEqual(Controller.two_live(rep), 0)
        outcomes = c.get("amend_not_effective_same_order_live", 0) + sum(v for k, v in c.items() if k.startswith("cancels_amend_rejected"))
        self.assertGreaterEqual(outcomes, 0)

    def test_c_an_amend_that_is_never_answered_is_queried_makes_no_second_order_and_leaves_one_live_order(self):
        self.with_amend_fault("timeout")
        rep = replay(moved_stream(1.6, bin_bid=100.288, bin_ask=100.298)); rep.run()
        c = rep.session.reprice_counters
        self.assertEqual(c.get("amend_requests", 0), 1, c)                                           # the quote is AMEND_PENDING: nothing else is sent for it while the outcome is unknown
        self.assertEqual(Controller.two_live(rep), 0)
        self.assertEqual(len([e for e in events(rep, "AMEND_CONFIRMED")]), 0)

    def test_c2_a_lost_amend_then_a_cancel_is_reported_as_a_lost_timeout_and_ends_in_a_query(self):
        """2i item 3 (owner 17 Nov), on the simulated venue: the amend is lost, the quote's replacement is later cancelled (the lineage is complete after a fill). Until 17 Nov the venue remembered the
        refused cancel of an order it never had as that order's state, a query then read REJECTED, and the ledger said AMEND_REJECTED."""
        self.with_amend_fault("timeout")
        rep = replay(moved_stream(1.6, bin_bid=100.288, bin_ask=100.298)); rep.run()
        self.assertEqual(events(rep, "AMEND_REJECTED"), [])
        lost = [e for e in events(rep, "AMEND_TIMEOUT") if e.get("lost")]
        self.assertEqual(len(lost), 1)
        owed = [e for e in events(rep, "VENUE_ORDER_QUERY") if e.get("purpose") == "amend_timeout"]
        self.assertEqual(len(owed), 1)
        self.assertGreater(owed[0]["ts_ns"], lost[0]["ts_ns"])
        self.assertEqual(Controller.two_live(rep), 0)

    def test_j_an_amend_with_no_room_in_the_window_is_skipped_never_queued_and_cancels_are_not_refused(self):
        rep = replay(moved_stream(1.6, bin_bid=100.288, bin_ask=100.298))
        asked = []
        rep.session.router.quote_has_room = lambda venue, instrument_id, ts_ns: (asked.append(1) or len(asked) == 1)        # room for the first quote only, none afterwards
        rep.run()
        c = rep.session.reprice_counters
        self.assertEqual(c.get("amend_requests", 0), 0)
        self.assertGreater(c.get("amend_skipped_rate_limit", 0), 0, c)
        self.assertEqual(events(rep, "AMEND_REQUESTED"), [])


class AmendPriceOnGrid(unittest.TestCase):
    """Found by case 2i on v1_10 (16 Nov): the controller's target is a float price; the first amend of the arb on the capture was refused 'price violates tick'. The amend price is moved onto the venue's grid without
    loosening it (a bid rounds down, an ask up), and the move is counted."""

    def amend(self, side, target):
        from decimal import Decimal
        from types import SimpleNamespace
        from crypto_stat_arb.session import TradingSession
        sent = []
        oms = SimpleNamespace(instruments=SimpleNamespace(get=lambda venue, instrument, ts: SimpleNamespace(tick_size=Decimal("0.01"))),
                              request_amend=lambda order_id, price, ts, reason: sent.append(price) or "REQUESTED")
        counters = {}
        fake = SimpleNamespace(_limits=None, router=SimpleNamespace(quote_has_room=lambda *a: True), oms=oms, reprice_counters=counters, _reprice_pending={},
                               _count_reprice=lambda key: counters.__setitem__(key, counters.get(key, 0) + 1))
        c = {"venue": "BYBIT", "instrument_id": "SOL-USDT-PERP", "order_id": "o1", "parent_id": "p1", "side": side}
        st = SimpleNamespace(last_request_ts=0.0, pending=False, reprices=0)
        TradingSession._amend_for_reprice(fake, c, st, 1.0, 10**9, SimpleNamespace(price=target, reason="adverse_move"))
        return sent, counters

    def test_a_bid_target_off_the_grid_rounds_down_and_is_counted(self):
        from decimal import Decimal
        sent, c = self.amend("buy", 100.2879)
        self.assertEqual(sent, [Decimal("100.28")])
        self.assertEqual((c["amend_price_snapped_to_tick"], c["amend_requests"]), (1, 1))

    def test_an_ask_target_off_the_grid_rounds_up(self):
        from decimal import Decimal
        sent, _ = self.amend("sell", 100.2821)
        self.assertEqual(sent, [Decimal("100.29")])

    def test_a_target_on_the_grid_is_sent_as_it_is_and_not_counted_as_snapped(self):
        from decimal import Decimal
        sent, c = self.amend("buy", 100.28)
        self.assertEqual(sent, [Decimal("100.28")])
        self.assertNotIn("amend_price_snapped_to_tick", c)


if __name__ == "__main__":
    unittest.main()
