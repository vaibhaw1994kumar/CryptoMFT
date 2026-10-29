"""EMS02: venue rate limits by scope, with capacity kept for cancels."""

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.capabilities import RateLimit, scoped_rate_limiter
from crypto_stat_arb.config import load_config
from crypto_stat_arb.execution import (
    CanonicalOrderRequest, OrderAction, OrderType, RateLimitPolicy, StrategyDirectedRouter, TimeInForce,
    VenueRouteStatus,
)
from crypto_stat_arb.models import Side
from crypto_stat_arb.rate_limits import CANCEL, NEW_RISK, RISK_REDUCING, ScopedRateLimiter
from crypto_stat_arb.session import ExecutionMode, TradingSession
from test_execution import book, instruments
from test_execution_realism import fixed_model

ROOT = Path(__file__).parents[1]
S = 1_000_000_000


def limiter(limit=10, interval=1.0, **reserves):
    """One Bybit IP bucket of ``limit`` per ``interval`` (shares: new risk 0.7, risk-reducing 0.9)."""
    return ScopedRateLimiter({("BYBIT", "SPOT"): [RateLimit("IP", "HTTP", limit, interval, "requests", "DOCUMENTED")]},
                             **reserves)


def fill(lim, action, n, ts=0):
    for _ in range(n):
        lim.charge("BYBIT", "SPOT", action, ts)


def cancel_request(venue="BYBIT", ts=100, order_id="o-1"):
    return CanonicalOrderRequest.create(
        action=OrderAction.CANCEL, order_id=order_id, target_order_id=order_id, parent_opportunity_id="op",
        parent_order_id="p", instrument_id="BTC-USDT-SPOT", venue=venue, side=Side.BUY,
        price=Decimal("59000.00"), quantity=Decimal("0.010"), order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC, created_ts_ns=ts)


class TierTests(unittest.TestCase):
    def test_new_risk_stops_first_then_risk_reducing_then_cancels(self):
        lim = limiter()
        fill(lim, "SUBMIT", 7)                                       # 10 x 0.7 used
        self.assertFalse(lim.check("BYBIT", "SPOT", "SUBMIT", NEW_RISK, 0).admitted)
        self.assertTrue(lim.check("BYBIT", "SPOT", "SUBMIT", RISK_REDUCING, 0).admitted)
        fill(lim, "SUBMIT", 2)                                       # 9 used: the risk-reducing share
        self.assertFalse(lim.check("BYBIT", "SPOT", "SUBMIT", RISK_REDUCING, 0).admitted)
        self.assertTrue(lim.check("BYBIT", "SPOT", "CANCEL", CANCEL, 0).admitted)
        fill(lim, "CANCEL", 1)                                       # full capacity
        decision = lim.check("BYBIT", "SPOT", "CANCEL", CANCEL, 0)
        self.assertFalse(decision.admitted)                          # nothing exceeds the venue limit
        self.assertEqual((decision.binding.name, decision.binding.status), ("HTTP", "DOCUMENTED"))

    def test_the_window_slides_and_next_free_is_exact(self):
        lim = limiter()
        fill(lim, "SUBMIT", 4, ts=0)
        fill(lim, "SUBMIT", 3, ts=S // 2)
        decision = lim.check("BYBIT", "SPOT", "SUBMIT", NEW_RISK, S // 2)
        self.assertEqual(decision.next_free_ns, S)                  # the first four expire at t = 1 s
        self.assertTrue(lim.check("BYBIT", "SPOT", "SUBMIT", NEW_RISK, S).admitted)

    def test_reserves_are_validated(self):
        with self.assertRaises(ValueError):
            limiter(cancel_reserve=0.6, risk_reducing_reserve=0.5)


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.lim = scoped_rate_limiter({"BYBIT": "SPOT", "BINANCE": "SPOT"})

    def test_each_scope_binds_on_its_own(self):
        # Bybit create endpoint: 20/s (assumed); 14 new orders fill the new-risk share.
        fill(self.lim, "SUBMIT", 14)
        decision = self.lim.check("BYBIT", "SPOT", "SUBMIT", NEW_RISK, 0)
        self.assertEqual((decision.admitted, decision.binding.name), (False, "/v5/order/create"))
        self.assertTrue(self.lim.check("BYBIT", "SPOT", "CANCEL", CANCEL, 0).admitted)   # own endpoint
        # Binance account order count (100 / 10 s, assumed) binds before its IP weight.
        for _ in range(70):
            self.lim.charge("BINANCE", "SPOT", "SUBMIT", 0)
        binding = self.lim.check("BINANCE", "SPOT", "SUBMIT", NEW_RISK, 0).binding
        self.assertEqual((binding.scope, binding.name), ("ACCOUNT", "ORDERS"))
        for _ in range(30):                                          # the order count is now full
            self.lim.charge("BINANCE", "SPOT", "SUBMIT", 0)
        self.assertTrue(self.lim.check("BINANCE", "SPOT", "CANCEL", CANCEL, 0).admitted)   # cancels skip ORDERS

    def test_bybit_ip_is_shared_by_spot_and_linear(self):
        both = scoped_rate_limiter({"BYBIT": "SPOT"})
        both_linear = scoped_rate_limiter({"BYBIT": "PERP"})
        self.assertEqual({k for k in both.buckets if k[0] == "IP"}, {k for k in both_linear.buckets if k[0] == "IP"})

    def test_queries_spend_ip_weight(self):
        before = self.lim.buckets[("IP", "BINANCE", "api.binance.com", "REQUEST_WEIGHT", "60")].used
        self.lim.charge("BINANCE", "SPOT", "QUERY", 0)
        self.assertEqual(self.lim.buckets[("IP", "BINANCE", "api.binance.com", "REQUEST_WEIGHT", "60")].used,
                         before + 4)                                 # GET /api/v3/order weight (assumed)


class RouterTests(unittest.TestCase):
    def router(self, *, latency=False, policy=RateLimitPolicy.REJECT):
        router = StrategyDirectedRouter(instruments(), latency_model=fixed_model() if latency else None,
                                        route_status={v: VenueRouteStatus(rate_limit_policy=policy)
                                                      for v in ("BINANCE", "BYBIT")})
        router.scoped_limits = limiter(limit=10)
        return router

    def instruction(self, **overrides):
        from test_execution import ExecutionApiTests
        return ExecutionApiTests.routing_instruction(eligible_venues=("BYBIT",), route_priority=("BYBIT",),
                                                     **overrides)

    def test_new_risk_at_its_share_is_refused_and_charges_nothing(self):
        router = self.router()
        fill(router.scoped_limits, "SUBMIT", 7)
        result = router.route_controlled(self.instruction(), {("BYBIT", "BTC-USDT-SPOT"): book("BYBIT")})
        self.assertIsNone(result.selected_venue)
        self.assertIn("scoped_rate_limited", result.checks[0].reasons)
        self.assertEqual(router.scoped_limits.buckets[next(iter(router.scoped_limits.buckets))].used, 7)
        self.assertEqual(router.scoped_limits.throttled["DOCUMENTED"], 1)
        traced = [r for r in router.trace if r["record_type"] == "SCOPED_RATE_LIMITED"]
        self.assertEqual(traced[0]["limit_status"], "DOCUMENTED")

    def test_a_risk_reducing_order_still_routes_and_is_charged(self):
        router = self.router()
        fill(router.scoped_limits, "SUBMIT", 7)
        result = router.route_controlled(self.instruction(risk_reducing=True),
                                         {("BYBIT", "BTC-USDT-SPOT"): book("BYBIT")})
        self.assertEqual(result.selected_venue, "BYBIT")
        self.assertEqual(router.scoped_limits.buckets[next(iter(router.scoped_limits.buckets))].used, 8)

    def test_queue_policy_releases_when_the_bucket_has_room(self):
        router = self.router(policy=RateLimitPolicy.QUEUE)
        fill(router.scoped_limits, "SUBMIT", 7, ts=0)
        result = router.route_controlled(self.instruction(created_ts_ns=100),
                                         {("BYBIT", "BTC-USDT-SPOT"): book("BYBIT")})
        self.assertEqual(result.release_ts_ns, S)                   # when the first seven expire

    def test_a_cancel_waits_for_room_with_latency_and_is_never_refused(self):
        router = self.router(latency=True)
        fill(router.scoped_limits, "SUBMIT", 10, ts=0)              # full: even cancels have no room
        self.assertEqual(router.route(cancel_request(ts=100)), ())
        self.assertEqual(router.scoped_limits.cancels_delayed, 1)
        delayed = [r for r in router.trace if r["record_type"] == "SCOPED_RATE_LIMIT_DELAYED"]
        self.assertEqual((delayed[0]["action"], delayed[0]["next_free_ts_ns"]), ("CANCEL", S))
        in_flight = [p for p in router._pending if p.request.action == OrderAction.CANCEL]
        self.assertEqual(in_flight[0].release_ts_ns, S + 10_000_000)   # sent at 1 s, plus 10 ms cancel latency

    def test_a_cancel_without_latency_is_sent_and_flagged(self):
        router = self.router()
        fill(router.scoped_limits, "SUBMIT", 10, ts=0)
        router.route(cancel_request(ts=100))
        self.assertEqual(router.scoped_limits.over_limit_sent, 1)
        self.assertTrue(any(r["record_type"] == "SCOPED_RATE_LIMIT_EXCEEDED" for r in router.trace))
        self.assertTrue(any(r["record_type"] == "ORDER_REQUEST" for r in router.trace))   # it was sent

    def test_the_snapshot_reports_the_binding_bucket(self):
        router = self.router()
        fill(router.scoped_limits, "SUBMIT", 5)
        self.assertEqual(router.rate_limit_snapshot()["scoped"]["max_utilization"], 0.5)


class SessionTests(unittest.TestCase):
    def config_with(self, settings):
        raw = json.loads((ROOT / "config/perp_research.json").read_text())
        raw.setdefault("oms", {})["rate_limits"] = settings
        directory = tempfile.mkdtemp()
        path = Path(directory) / "config.json"
        path.write_text(json.dumps(raw))
        return load_config(path)[0]

    def test_sessions_use_the_matrix_limits_by_default(self):
        cfg, _ = load_config(ROOT / "config/perp_research.json")
        limits = TradingSession(cfg, execution_mode=ExecutionMode.INTERNAL_SIMULATION).router.scoped_limits
        self.assertEqual({b.status for b in limits.buckets.values() if b.scope == "IP" and b.key[1] == "BYBIT"},
                         {"DOCUMENTED"})
        self.assertEqual((limits.cancel_reserve, limits.risk_reducing_reserve), (0.2, 0.1))

    def test_config_keys_set_the_reserves_or_turn_it_off(self):
        cfg = self.config_with({"cancel_reserve": 0.3, "risk_reducing_reserve": 0.05})
        limits = TradingSession(cfg, execution_mode=ExecutionMode.INTERNAL_SIMULATION).router.scoped_limits
        self.assertEqual((limits.cancel_reserve, limits.risk_reducing_reserve), (0.3, 0.05))
        off = self.config_with({"enabled": False})
        self.assertIsNone(TradingSession(off, execution_mode=ExecutionMode.INTERNAL_SIMULATION).router.scoped_limits)


if __name__ == "__main__":
    unittest.main()
