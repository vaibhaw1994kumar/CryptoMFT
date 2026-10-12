"""Mean reversion, multi-strategy session, sim_engine maker model, session audit."""

import asyncio
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.benchmark import synthetic_book_events
from crypto_stat_arb.config import load_config
from crypto_stat_arb.live_feed import LiveFeedSettings
from crypto_stat_arb.maker_sim import maker_simulator_from_config, simulate_recorded_session
from crypto_stat_arb.models import MarketEvent, PlanState, TradeEvent
from crypto_stat_arb.paper_live import LivePaperSession
from crypto_stat_arb.replay import deterministic_order
from crypto_stat_arb.session import TradingSession
from crypto_stat_arb.session_audit import audit_session
from test_live_feed import FakeConnector, FakeWebSocket, StepClock


ROOT = Path(__file__).parents[1]
WATCH_CONFIG = ROOT / "config" / "paper_watch.json"


class MultiStrategySessionTests(unittest.TestCase):
    def test_arb_and_mean_reversion_trade_the_same_books_and_close(self):
        cfg, _ = load_config(WATCH_CONFIG)
        events = deterministic_order([
            MarketEvent.from_dict(row) for row in synthetic_book_events(6_000, seed=11)
        ])
        session = TradingSession(cfg)
        self.assertEqual(
            [strategy.strategy_id for strategy in session.strategies],
            ["cross_venue_btc_v1", "basis_mean_reversion_v1"],
        )
        for event in events:
            session.on_event(event)
        session.drain()
        report = session.strategy_report()["strategies"]
        mean_reversion = report["basis_mean_reversion_v1"]
        self.assertGreater(report["cross_venue_btc_v1"]["plans"], 0)
        self.assertGreater(mean_reversion["plans"], 0)
        actions = mean_reversion["summary"]["actions"]
        self.assertTrue(any(name.startswith("enter_") for name in actions))
        self.assertTrue(any(name.startswith("exit_") for name in actions))
        self.assertTrue(all(plan.state == PlanState.CLOSED for plan in session.oms.plans.values()))
        self.assertEqual(mean_reversion["summary"]["pending_plans"], 0)

    def test_flatten_blocks_entries_and_closes_open_stance(self):
        cfg, _ = load_config(WATCH_CONFIG)
        events = deterministic_order([
            MarketEvent.from_dict(row) for row in synthetic_book_events(6_000, seed=11)
        ])
        session = TradingSession(cfg)
        strategy = session.strategies[1]
        for event in events:
            session.on_event(event)
            if strategy.stance.value != "FLAT" and not strategy.pending:
                break
        self.assertNotEqual(strategy.stance.value, "FLAT")
        session.enter_flatten(session.last_event_ts_ns)
        session.drain()
        session.tick(session.last_event_ts_ns + 20_000_000_000)
        self.assertEqual(strategy.stance.value, "FLAT")
        self.assertFalse(strategy.allow_entries)


class MakerSimulationTests(unittest.TestCase):
    def test_sim_engine_queue_model_fills_from_trades(self):
        rows = synthetic_book_events(400, seed=3, dislocation_every=10_000)
        trades = [
            TradeEvent(
                event_id=f"trade-{index}", venue=row["venue"],
                instrument_id=row["instrument_id"], trade_id=str(index),
                exchange_ts_ns=row["exchange_ts_ns"], source_ts_ns=row["exchange_ts_ns"],
                receive_ts_ns=row["receive_ts_ns"] + 500_000,
                price=Decimal(row["bids"][-1][0]), quantity=Decimal("2.0"),
                aggressor_side="SELL", raw_ref=f"raw-{index}", adapter_version="test",
            ).to_dict()
            for index, row in enumerate(rows[10::5])
        ]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            canonical = base / "canonical.jsonl"
            canonical.write_text("".join(json.dumps(row) + "\n" for row in rows))
            trade_file = base / "trades.jsonl"
            trade_file.write_text("".join(json.dumps(row) + "\n" for row in trades))
            report = simulate_recorded_session(
                WATCH_CONFIG, [canonical], [trade_file], base / "maker.json"
            )
        self.assertIn(report["engine"], {"python_queue_model", "cpp_mm_matching_engine"})
        fills = sum(row["fills"] for row in report["venues"].values())
        self.assertGreater(fills, 0)
        for row in report["venues"].values():
            self.assertEqual(row["sanity_violation_count"], 0)
            self.assertGreater(row["quotes"], 0)


class SessionAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_audit_covers_every_component_of_a_paper_session(self):
        binance = FakeWebSocket([json.dumps({
            "e": "depthUpdate", "E": 1000, "T": 1000, "s": "BTCUSDT",
            "U": 101, "u": 101, "b": [["59999.90", "2"]], "a": [],
        })])
        bybit = FakeWebSocket([
            json.dumps({
                "topic": "orderbook.50.BTCUSDT", "type": "snapshot", "ts": 1000,
                "data": {"s": "BTCUSDT", "u": 10,
                         "b": [["60060.00", "1"]], "a": [["60060.10", "1"]]},
            }),
            json.dumps({
                "topic": "orderbook.50.BTCUSDT", "type": "delta", "ts": 1001,
                "data": {"s": "BTCUSDT", "u": 11, "b": [["60060.00", "2"]], "a": []},
            }),
        ])
        clock = StepClock()

        async def http_getter(url, timeout):
            await asyncio.sleep(0.05)
            return json.dumps({
                "lastUpdateId": 100,
                "bids": [["59999.90", "1"]], "asks": [["60000.00", "1"]],
            }), clock()

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            cfg, _ = load_config(WATCH_CONFIG)
            paper = LivePaperSession(
                WATCH_CONFIG, base / "live", "audit-test",
                LiveFeedSettings(
                    max_events=4, reconnect_min_seconds=0.001,
                    reconnect_max_seconds=0.002, durable=False,
                    udp_enabled=False, trade_streams_enabled=False,
                ),
                websocket_connector=FakeConnector({"BINANCE": [binance], "BYBIT": [bybit]}),
                http_getter=http_getter, clock_ns=clock, tick_seconds=10,
                maker_simulator=maker_simulator_from_config(cfg),
            )
            summary = await asyncio.wait_for(paper.run(), timeout=5)
            self.assertIn("maker_dry_run", summary)
            self.assertIn("basis_mean_reversion_v1", summary["strategies"]["strategies"])
            audit = audit_session(
                WATCH_CONFIG, base / "live" / "audit-test", base / "audit",
                min_opportunities=30,
            )
            self.assertTrue((base / "audit" / "session_audit.md").exists())
        components = audit["components"]
        self.assertEqual(set(components), {
            "feed", "order_books", "time_integrity", "historical_storage",
            "orderbook_replay", "backtest_replay", "simulation", "dry_run",
            "strategies", "security_master", "errors",
        })
        for name, component in components.items():
            self.assertFalse(
                any(check["check"] == "audit_completed" for check in component["checks"]),
                f"{name} audit crashed: {component['checks']}",
            )
        self.assertEqual(components["historical_storage"]["status"], "OK")
        replay_checks = {
            check["check"]: check["status"]
            for check in components["orderbook_replay"]["checks"]
        }
        self.assertEqual(replay_checks["BINANCE_processing_order_rebuilds_live_book"], "OK")
        self.assertEqual(replay_checks["BYBIT_processing_order_rebuilds_live_book"], "OK")
        self.assertEqual(components["dry_run"]["status"], "OK")


if __name__ == "__main__":
    unittest.main()
