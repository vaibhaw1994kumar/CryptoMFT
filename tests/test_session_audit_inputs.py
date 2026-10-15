"""Session audit: missing inputs are INPUT_MISSING, and OK rows describe success.

Review 13.10.2026: an audit run on a session whose raw.jsonl and
paper/canonical_events.jsonl had been deleted reported FAIL and a
FileNotFoundError, indistinguishable from a real defect; and passing checks
printed their failure text ("Python mirror was used", "no ingestion trace").
"""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.config import load_config
from crypto_stat_arb.live_feed import LiveFeedSettings
from crypto_stat_arb.maker_sim import maker_simulator_from_config
from crypto_stat_arb.paper_live import LivePaperSession
from crypto_stat_arb.session_audit import LEVELS, Component, audit_session
from test_live_feed import FakeConnector, FakeWebSocket, StepClock


ROOT = Path(__file__).parents[1]
WATCH_CONFIG = ROOT / "config" / "paper_watch.json"

FAILURE_PHRASES = (
    "not importable", "no ingestion trace", "did not finalize", "no part-* directories",
    "missing live_part_manifest", "not yet measurable", "enable NTP sync", "no paper_summary",
    "left unhedged", "no strategy report", "left open", "poll ran", "polls failed",
)


async def record_session(base: Path) -> Path:
    binance = FakeWebSocket([json.dumps({
        "e": "depthUpdate", "E": 1000, "T": 1000, "s": "BTCUSDT",
        "U": 101, "u": 101, "b": [["59999.90", "2"]], "a": [],
    })])
    bybit = FakeWebSocket([
        json.dumps({"topic": "orderbook.50.BTCUSDT", "type": "snapshot", "ts": 1000,
                    "data": {"s": "BTCUSDT", "u": 10, "b": [["60060.00", "1"]], "a": [["60060.10", "1"]]}}),
        json.dumps({"topic": "orderbook.50.BTCUSDT", "type": "delta", "ts": 1001,
                    "data": {"s": "BTCUSDT", "u": 11, "b": [["60060.00", "2"]], "a": []}}),
    ])
    clock = StepClock()

    async def http_getter(url, timeout):
        await asyncio.sleep(0.05)
        return json.dumps({"lastUpdateId": 100, "bids": [["59999.90", "1"]],
                           "asks": [["60000.00", "1"]]}), clock()

    cfg, _ = load_config(WATCH_CONFIG)
    paper = LivePaperSession(
        WATCH_CONFIG, base / "live", "inputs-test",
        LiveFeedSettings(max_events=4, reconnect_min_seconds=0.001, reconnect_max_seconds=0.002,
                         durable=False, udp_enabled=False, trade_streams_enabled=False),
        websocket_connector=FakeConnector({"BINANCE": [binance], "BYBIT": [bybit]}),
        http_getter=http_getter, clock_ns=clock, tick_seconds=10,
        maker_simulator=maker_simulator_from_config(cfg),
    )
    await asyncio.wait_for(paper.run(), timeout=5)
    return base / "live" / "inputs-test"


class ComponentTests(unittest.TestCase):
    def test_ok_rows_report_success_text_and_missing_ranks_between_warn_and_fail(self):
        component = Component("x")
        component.check("engine", True, "Python mirror was used", ok_detail="engine=cpp")
        self.assertEqual(component.checks[0]["detail"], "engine=cpp")
        component.missing("inputs", [Path("a.jsonl")])
        self.assertEqual(component.status, "INPUT_MISSING")
        self.assertLess(LEVELS["WARN"], LEVELS["INPUT_MISSING"])
        self.assertLess(LEVELS["INPUT_MISSING"], LEVELS["FAIL"])
        component.check("bad", False, "broken")
        self.assertEqual(component.status, "FAIL")


class MissingInputAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_deleted_inputs_are_input_missing_not_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            session = await record_session(base)
            intact = audit_session(WATCH_CONFIG, session, base / "audit-intact", min_opportunities=30)
            for name, component in intact["components"].items():
                for check in component["checks"]:
                    if check["status"] == "OK":
                        self.assertFalse(
                            any(phrase in check["detail"] for phrase in FAILURE_PHRASES),
                            f"{name}.{check['check']} OK row carries failure text: {check['detail']}",
                        )
            self.assertNotEqual(intact["components"]["historical_storage"]["status"], "INPUT_MISSING")

            for part in session.glob("part-*"):
                (part / "raw.jsonl").unlink()
            (session / "paper" / "canonical_events.jsonl").unlink()
            damaged = audit_session(WATCH_CONFIG, session, base / "audit-damaged", min_opportunities=30)
        components = damaged["components"]
        self.assertEqual(components["historical_storage"]["status"], "INPUT_MISSING")
        self.assertEqual(components["backtest_replay"]["status"], "INPUT_MISSING")
        self.assertEqual(components["orderbook_replay"]["status"], "INPUT_MISSING")
        for name in ("historical_storage", "backtest_replay", "orderbook_replay"):
            self.assertFalse(any(
                check["check"] == "audit_completed" for check in components[name]["checks"]
            ), f"{name} crashed instead of reporting missing input")
        missing = [
            check for check in components["historical_storage"]["checks"]
            if check["status"] == "INPUT_MISSING"
        ]
        self.assertTrue(all("raw.jsonl" in check["detail"] for check in missing))
        self.assertIn(damaged["overall_status"], {"INPUT_MISSING", "FAIL"})
        if damaged["overall_status"] == "FAIL":
            # Only a genuine defect elsewhere may outrank missing inputs.
            self.assertTrue(any(issue["status"] == "FAIL" for issue in damaged["issues"]))


if __name__ == "__main__":
    unittest.main()
