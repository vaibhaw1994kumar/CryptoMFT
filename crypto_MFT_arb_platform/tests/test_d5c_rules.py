"""Phase D, D5c: rules the session path needed to run the same as the backtester's loop. Wiring checks on synthetic input, never a profit figure."""
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crypto_stat_arb.hedge_lifecycle import touch_slack_price
from new_backtest.cross_venue_feed import TimedEvent
from new_backtest.event_loader import BOOK
from new_backtest.profiles import apply_profile, load_profile
from new_backtest.session_replay import SessionReplay

SLICES = Path(__file__).resolve().parents[2] / "out" / "queue_v2" / "slices_mar2024"


class HedgeCap(unittest.TestCase):
    def test_touch_slack_is_the_touch_moved_against_us_and_the_ems_layer_uses_the_same_formula(self):
        self.assertEqual(touch_slack_price("sell", Decimal("100"), Decimal("5")), Decimal("99.95"))
        self.assertEqual(touch_slack_price("buy", Decimal("100"), Decimal("5")), Decimal("100.05"))
        from new_backtest.execution.layer import ExecutionLayer          # the re-export of crypto_stat_arb.ems.layer
        from new_backtest.execution import ExecutionConfig
        layer = ExecutionLayer(ExecutionConfig(), instrument="SOLUSDT")
        self.assertAlmostEqual(layer.touch_slack_cap("sell", 100.0, 2.0), float(touch_slack_price("sell", 100.0, 5.0)))      # max(2 bps, the 5 bps floor)


class HedgeRulesLiveOnceInTheLayer(unittest.TestCase):
    """E0 step (b) (owner 10 Nov): the hedge cap and the retry decision are computed in `ems/layer.py` only; the OMS asks for them, with the intent journal off (no layer instance in the session)."""

    @staticmethod
    def old_oms_cap(side, touch, slack_bps, tick):          # the OMS formula before step (b), copied
        raw = touch_slack_price(side, touch, Decimal(str(slack_bps)))
        return (raw / tick).to_integral_value(rounding="ROUND_FLOOR" if side == "buy" else "ROUND_CEILING") * tick

    def test_cap_on_the_grid_equals_the_old_oms_formula_exactly(self):
        import random
        from crypto_stat_arb.ems.layer import hedge_cap_on_grid
        rnd = random.Random(11)
        for _ in range(20_000):
            tick = Decimal(rnd.choice(["0.1", "0.01", "0.001", "0.0001", "0.5"]))
            touch = (Decimal(rnd.randint(1, 3_000_000)) * tick).quantize(tick)
            slack = rnd.choice([0, 2, 5, 5.0, 7.5, 12, 0.3, rnd.uniform(0, 30)])
            side = rnd.choice(["buy", "sell"])
            self.assertEqual(hedge_cap_on_grid(side, touch, slack, tick), self.old_oms_cap(side, touch, slack, tick))
        self.assertEqual(hedge_cap_on_grid("sell", Decimal("99.0"), 5.0, Decimal("0.1")), Decimal("99.0"))       # 98.9505 rounds UP to the grid, never inside the cap
        self.assertEqual(hedge_cap_on_grid("buy", Decimal("100.0"), 5.0, Decimal("0.1")), Decimal("100.0"))      # 100.05 rounds DOWN

    def test_the_slack_the_policy_carries_equals_the_old_inline_expression(self):
        from crypto_stat_arb.ems.layer import hedge_slack_bps
        for cap in ("touch_slack", "breakeven_plus_buffer", None):
            for walk in (0, 2, 2.0, 5, 8.5, 12):
                for floor in (None, 0, 3, 5.0, 10):
                    cfg = {k: v for k, v in (("cap", cap), ("slack_bps_floor", floor)) if v is not None}
                    old = max(float(walk), float(cfg.get("slack_bps_floor", 5.0))) if cfg.get("cap") == "touch_slack" else None
                    self.assertEqual(hedge_slack_bps(cfg, walk), old)

    def test_the_retry_decision_equals_the_old_oms_rule_over_the_whole_table(self):
        from crypto_stat_arb.ems.layer import hedge_retry_decision
        for retries in range(5):
            for limit in range(4):
                for permitted in (True, False):
                    got = hedge_retry_decision(retries, limit, permitted, 250, 1_000)
                    if permitted and retries < limit:
                        self.assertEqual(tuple(got), (True, 1_250, ""))
                    else:
                        self.assertEqual(tuple(got), (False, None, "reject not retryable" if not permitted else f"retry budget {limit} used"))

    def test_the_layer_instance_and_the_module_functions_give_one_answer(self):
        from crypto_stat_arb.ems.layer import ExecutionLayer, hedge_slack_bps
        from new_backtest.execution import ExecutionConfig
        layer = ExecutionLayer(ExecutionConfig(), instrument="SOLUSDT")
        for walk in (0.0, 2.0, 5.0, 9.0):
            self.assertEqual(layer.touch_slack_cap("buy", 100.0, walk), float(touch_slack_price("buy", 100.0, hedge_slack_bps(layer.cfg.hedge, walk))))

    def test_the_oms_has_no_copy_of_either_rule(self):
        import inspect
        import crypto_stat_arb.oms as oms_mod
        from crypto_stat_arb.ems import layer as layer_mod
        self.assertIs(oms_mod.hedge_cap_on_grid, layer_mod.hedge_cap_on_grid)
        self.assertIs(oms_mod.hedge_retry_decision, layer_mod.hedge_retry_decision)
        src = inspect.getsource(oms_mod)
        self.assertNotIn("touch_slack_price(", src)
        self.assertNotIn("chain.retries < chain.retry_limit", src)

    def _hedged(self, **policy):
        from test_core_hedge_scenarios import Scenario
        from crypto_stat_arb.execution import ExecutionEventType
        from crypto_stat_arb.hedge_lifecycle import HedgePolicy
        class Harness(Scenario):
            def runTest(self):
                pass
        h = Harness()
        h.build(hedge=[("reject", "-1003")] * 3, policy=HedgePolicy(**policy))
        self.assertIsNone(h.oms.observer)                              # the intent journal is OFF in this session
        h.maker_fill("2.0", 5_000_000_000, ExecutionEventType.FILLED)
        return h

    def test_the_oms_takes_the_cap_from_the_layer_with_the_journal_off(self):
        import crypto_stat_arb.oms as oms_mod
        h = self._hedged(touch_slack_bps=5.0)
        self.assertEqual([o.limit_price for o in h.plan.children if o.order_role == "HEDGE"][0], Decimal("99.0"))      # touch 99.0 sell, 5 bps, ceil to the 0.10 grid
        real, oms_mod.hedge_cap_on_grid = oms_mod.hedge_cap_on_grid, lambda side, touch, slack, tick: Decimal("98.7")
        try:
            h2 = self._hedged(touch_slack_bps=5.0)
        finally:
            oms_mod.hedge_cap_on_grid = real
        self.assertEqual([o.limit_price for o in h2.plan.children if o.order_role == "HEDGE"][0], Decimal("98.7"))      # what the layer says is what is sent

    def test_the_oms_takes_the_retry_decision_from_the_layer_with_the_journal_off(self):
        import crypto_stat_arb.oms as oms_mod
        from crypto_stat_arb.ems.layer import HedgeRetry
        from test_fill_triggered import MS
        h = self._hedged(retry_limit=2, retry_backoff_ns=250 * MS)
        h.oms.advance_time(int(5.3e9)); h.oms.advance_time(int(5.6e9)); h.oms.advance_time(int(6.0e9))
        self.assertEqual(h.chain.retries, 2)                           # the layer's rule retries twice (as the old rule did)
        real, oms_mod.hedge_retry_decision = oms_mod.hedge_retry_decision, lambda *a, **k: HedgeRetry(False, None, "the layer says no")
        try:
            h2 = self._hedged(retry_limit=2, retry_backoff_ns=250 * MS)
            h2.oms.advance_time(int(5.3e9)); h2.oms.advance_time(int(5.6e9)); h2.oms.advance_time(int(6.0e9))
        finally:
            oms_mod.hedge_retry_decision = real
        self.assertEqual(h2.chain.retries, 0)                          # no retry the layer did not allow
        self.assertEqual(len(h2.unwinds()), 1)


def timed(t, venue, bid, ask):
    return TimedEvent(t + 0.002, t, venue, "SOLUSDT", BOOK, {"timestamp": t, "bids": [[str(bid), "50"], [str(round(bid - 0.01, 2)), "50"]], "asks": [[str(ask), "50"], [str(round(ask + 0.01, 2)), "50"]]})


@unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
class MakerLifetimeAndIocReport(unittest.TestCase):
    def _run(self, **arb):
        profile = load_profile("perp_perp_maker_taker_v1_5")
        cfg = apply_profile(profile, dataset="mar2024", data_root=str(SLICES), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_d5c")
        cfg.arb.max_position_base = 1.0
        for k, v in arb.items():
            setattr(cfg.arb, k, v)
        stream, t = [timed(1.0, "bybit", 100.00, 100.30), timed(1.0, "binance", 100.30, 100.31)], 1.05
        for _ in range(120):                                              # no print ever reaches the resting bid
            stream += [timed(t, "binance", 100.30, 100.31), timed(t + 0.01, "bybit", 100.00, 100.30)]
            t += 0.05
        replay = SessionReplay(cfg, profile, stream=sorted(stream, key=lambda e: (e.exchange_ts, e.venue)))
        return replay, replay.run()

    def test_a_resting_maker_order_is_cancelled_a_lifetime_after_the_venue_accepted_it(self):
        replay, result = self._run(maker_max_age_s=2.0)
        first = result["orders"][0]
        self.assertEqual((first["venue"], first["state"]), ("BYBIT", "CANCELLED"))
        expired = [e for e in replay.session.ledger.events if e["event_type"] == "ORDER_LIFETIME_EXPIRED"]
        ack = next(e for e in replay.session.ledger.events if e["event_type"] == "ORDER_STATE_UPDATE_APPLIED" and e["order_id"] == first["order_id"]
                   and e["update"]["state"] == "ACKNOWLEDGED")
        self.assertEqual(expired[0]["ts_ns"], ack["update"]["event_ts_ns"] + 2_000_000_000)           # at its own deadline, not at the next event

    def test_without_a_lifetime_the_order_rests_on(self):
        replay, result = self._run(maker_max_age_s=0.0)
        self.assertEqual(result["orders"][0]["state"], "ACKNOWLEDGED")
        self.assertFalse([e for e in replay.session.ledger.events if e["event_type"] == "ORDER_LIFETIME_EXPIRED"])


if __name__ == "__main__":
    unittest.main()
