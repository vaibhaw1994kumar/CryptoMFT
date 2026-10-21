from pathlib import Path
import unittest

from crypto_stat_arb.config import load_config
from crypto_stat_arb.models import PlanState
from crypto_stat_arb.runner import load_events
from crypto_stat_arb.sdk import (
    STRATEGY_SDK_VERSION,
    CrossVenueSDKStrategy,
    RuntimeMode,
    StrategyRuntime,
    TestnetVenueAdapter,
    immediate_fill_testnet_transport,
)


ROOT = Path(__file__).resolve().parents[1]


class StrategySDKAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.config, _ = load_config(ROOT / "config/research.json")
        self.events = load_events(ROOT / "data/samples/two_venue_books.jsonl")

    @staticmethod
    def requests(runtime):
        return [
            row["request"] for row in runtime.router.trace
            if row["record_type"] == "ORDER_REQUEST"
        ]

    def test_minimal_strategy_port_runs_unchanged_in_paper_and_testnet(self):
        paper = StrategyRuntime(
            self.config, CrossVenueSDKStrategy(self.config), mode=RuntimeMode.PAPER,
        )
        testnet_adapters = {
            venue: TestnetVenueAdapter(venue, immediate_fill_testnet_transport)
            for venue in self.config.venues
        }
        testnet = StrategyRuntime(
            self.config, CrossVenueSDKStrategy(self.config),
            mode=RuntimeMode.TESTNET, adapters=testnet_adapters,
        )

        paper_result = paper.run(self.events)
        testnet_result = testnet.run(reversed(self.events))

        self.assertEqual(paper_result.sdk_version, STRATEGY_SDK_VERSION)
        self.assertEqual(paper_result.opportunity_count, 1)
        self.assertEqual(testnet_result.opportunity_count, 1)
        self.assertEqual(paper_result.plan_ids, testnet_result.plan_ids)
        self.assertEqual(self.requests(paper), self.requests(testnet))
        self.assertEqual(
            paper.context.portfolio.snapshot(), testnet.context.portfolio.snapshot()
        )
        self.assertTrue(all(
            plan.state == PlanState.CLOSED
            for plan in (*paper.context.oms.plans(), *testnet.context.oms.plans())
        ))

    def test_sdk_exposes_canonical_data_replay_features_portfolio_risk_and_oms(self):
        runtime = StrategyRuntime(
            self.config, CrossVenueSDKStrategy(self.config),
            mode=RuntimeMode.BACKTEST,
        )
        result = runtime.run(reversed(self.events))
        self.assertEqual(result.event_count, len(self.events))
        self.assertEqual(
            runtime.context.data.latest_event.event_id,
            max(self.events, key=lambda row: row.receive_ts_ns).event_id,
        )
        self.assertIn(
            "cross_venue:expected_net_edge_bps",
            runtime.context.features.snapshot(),
        )
        self.assertEqual(len(runtime.context.portfolio.snapshot()["positions"]), 2)
        self.assertTrue(runtime.context.risk.new_risk_allowed)
        self.assertIn("reserved_gross", runtime.context.risk.capacity())
        self.assertEqual(len(runtime.context.oms.plans()), 1)

    def test_testnet_mode_fails_closed_without_every_venue_adapter(self):
        with self.assertRaisesRegex(ValueError, "one explicit adapter per venue"):
            StrategyRuntime(
                self.config, CrossVenueSDKStrategy(self.config),
                mode=RuntimeMode.TESTNET,
                adapters={self.config.venues[0]: TestnetVenueAdapter(
                    self.config.venues[0], immediate_fill_testnet_transport
                )},
            )


if __name__ == "__main__":
    unittest.main()
