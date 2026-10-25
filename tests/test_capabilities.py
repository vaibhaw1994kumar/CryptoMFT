"""REF05: venue / product / adapter capability matrix, enforced on every route."""

from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from crypto_stat_arb import capabilities
from crypto_stat_arb.config import load_config
from crypto_stat_arb.execution import (
    CanonicalOrderRequest, OrderAction, OrderType, StrategyDirectedRouter, TimeInForce, VenueRouteStatus,
)
from crypto_stat_arb.models import Side
from crypto_stat_arb.session import ExecutionMode, TradingSession
from test_oms import instrument_master

ROOT = Path(__file__).parents[1]


def request(action=OrderAction.SUBMIT, tif=TimeInForce.GTC, post_only=False, order_type=OrderType.LIMIT):
    return CanonicalOrderRequest.create(
        action=action, order_id="o-2", target_order_id="o-1" if action != OrderAction.SUBMIT else None,
        parent_opportunity_id="op", parent_order_id="p", instrument_id="BTC-USDT-SPOT", venue="BYBIT",
        side=Side.BUY, price=Decimal("60000.00") if order_type == OrderType.LIMIT else None,
        quantity=Decimal("0.01"), order_type=order_type, time_in_force=tif, created_ts_ns=1, post_only=post_only)


class MatrixTests(unittest.TestCase):
    def test_every_entry_says_where_its_facts_come_from(self):
        for key, caps in capabilities.VENUES.items():
            self.assertTrue(caps.sources, key)
            self.assertIn(key, capabilities.ADAPTERS)
            for limit in caps.rate_limits:
                self.assertIn(limit.status, ("DOCUMENTED", "ASSUMED"), key)
                self.assertIn(limit.scope, ("IP", "ACCOUNT", "UID_ENDPOINT"), key)
            if any(limit.status == "ASSUMED" for limit in caps.rate_limits):
                self.assertTrue(any(field.startswith("rate_limits") for field in caps.assumed), key)

    def test_reduce_only_is_venue_enforced_on_perpetuals_and_platform_only_on_spot(self):
        for venue in ("BINANCE", "BYBIT"):
            self.assertEqual(capabilities.effective(venue, "SPOT", testnet=True)["reduce_only"], "PLATFORM_ONLY")
            self.assertEqual(capabilities.effective(venue, "PERP", testnet=True)["reduce_only"], "VENUE")

    def test_testnet_routes_cannot_replace_internal_ones_can(self):
        for venue in ("BINANCE", "BYBIT"):
            for product in ("SPOT", "PERP"):
                self.assertFalse(capabilities.effective(venue, product, testnet=True)["replace"])
                self.assertTrue(capabilities.effective(venue, product, testnet=False)["replace"])

    def test_unknown_venue_or_product_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "no capability entry for OKX SPOT"):
            capabilities.capabilities("OKX", "SPOT")

    def test_shipped_configs_load_with_a_matrix_entry_for_every_leg(self):
        for name in ("research", "paper_watch", "perp_research"):
            load_config(ROOT / f"config/{name}.json")


class RouteEnforcementTests(unittest.TestCase):
    def router(self, *, testnet):
        router = StrategyDirectedRouter(instrument_master())
        capabilities.configure_route(router.route_status.setdefault("BYBIT", VenueRouteStatus()), "BYBIT", "SPOT",
                                     testnet=testnet)
        return router

    def test_replace_is_refused_on_a_testnet_route(self):
        # The testnet transports would place a second order and leave the first live.
        with self.assertRaisesRegex(ValueError, "does not support replace; cancel and resubmit"):
            self.router(testnet=True).route(request(OrderAction.REPLACE))

    def test_unsupported_time_in_force_and_post_only_are_refused(self):
        router = self.router(testnet=False)
        status = router.route_status["BYBIT"]
        status.supported_time_in_force = frozenset({TimeInForce.GTC, TimeInForce.IOC})   # a venue without FOK
        with self.assertRaisesRegex(ValueError, "time in force FOK"):
            router.route(request(tif=TimeInForce.FOK))
        status.supports_post_only = False
        with self.assertRaisesRegex(ValueError, "post-only"):
            router.route(request(post_only=True))
        router.route(request())                                                    # GTC limit still routes

    def test_session_configures_every_venue_route_from_the_matrix(self):
        from dataclasses import replace
        from unittest import mock
        cfg, _ = load_config(ROOT / "config/perp_research.json")
        # A route whose adapter lacks FOK must not offer FOK: proves the session reads the matrix.
        without_fok = replace(capabilities.INTERNAL, time_in_force=("GTC", "IOC"))
        with mock.patch.object(capabilities, "INTERNAL", without_fok):
            restricted = TradingSession(cfg, execution_mode=ExecutionMode.INTERNAL_SIMULATION)
        for venue in cfg.venues:
            self.assertEqual(restricted.router.route_status[venue].supported_time_in_force,
                             frozenset({TimeInForce.GTC, TimeInForce.IOC}))
        session = TradingSession(cfg, execution_mode=ExecutionMode.INTERNAL_SIMULATION)
        for venue in cfg.venues:
            status = session.router.route_status[venue]
            self.assertEqual(status.supported_order_types, frozenset({OrderType.LIMIT, OrderType.MARKET}))
            self.assertEqual(status.supported_time_in_force,
                             frozenset({TimeInForce.GTC, TimeInForce.IOC, TimeInForce.FOK}))
            self.assertTrue(status.supports_replace)                      # internal adapter


class CliTests(unittest.TestCase):
    def test_capabilities_command_prints_the_matrix(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        done = subprocess.run([sys.executable, "-m", "crypto_stat_arb.cli", "capabilities"], cwd=ROOT, env=env,
                              capture_output=True, text=True, check=True)
        rows = json.loads(done.stdout)
        self.assertEqual({(r["venue_capabilities"]["venue"], r["venue_capabilities"]["product"]) for r in rows},
                         {("BINANCE", "SPOT"), ("BINANCE", "PERP"), ("BYBIT", "SPOT"), ("BYBIT", "PERP")})


if __name__ == "__main__":
    unittest.main()
