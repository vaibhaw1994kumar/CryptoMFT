"""Audit P2: cross-venue consistency (instrument compatibility, divergence classification)."""

import json
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.config import load_config
from crypto_stat_arb.cross_venue import CrossVenueMonitor
from crypto_stat_arb.live_feed import LiveFeedSettings, LiveOrderBookService
from crypto_stat_arb.models import BookState, MarketEvent
from crypto_stat_arb.orderbook import OrderBook

ROOT = Path(__file__).parents[1]
SEC = 1_000_000_000


def write_config(directory, mutate):
    raw = json.loads((ROOT / "config/research.json").read_text())
    mutate(raw)
    path = Path(directory) / "config.json"
    path.write_text(json.dumps(raw))
    return path


def leg(raw, venue):
    return next(x for x in raw["instruments"]
                if x["venue"] == venue and x["instrument_id"] == raw["strategy"]["instrument_id"])


class InstrumentCompatibilityTests(unittest.TestCase):
    def test_shipped_configs_are_compatible(self):
        for name in ("research", "paper_watch", "perp_research"):
            load_config(ROOT / f"config/{name}.json")

    def test_different_quote_assets_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, lambda raw: leg(raw, "BYBIT").update(quote="USDC"))
            with self.assertRaisesRegex(ValueError, "not the same contract.*quote=USDC"):
                load_config(path)

    def test_spot_leg_against_a_perpetual_fails_closed(self):
        def mutate(raw):
            leg(raw, "BINANCE").update(product_type="PERP", contract_type="LINEAR", funding_interval_hours=8)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "not the same contract.*product=PERP"):
                load_config(write_config(directory, mutate))


def book(venue, bid, ask, sequence=1):
    b = OrderBook(venue, "BTC-USDT-SPOT", max_mid_jump_bps=None, max_spread_bps=None)
    b.apply(MarketEvent.from_dict({
        "event_id": f"{venue}-{sequence}", "event_type": "BOOK_SNAPSHOT", "venue": venue,
        "instrument_id": "BTC-USDT-SPOT", "exchange_ts_ns": 1, "receive_ts_ns": 1, "sequence": sequence,
        "bids": [[str(bid), "1"]], "asks": [[str(ask), "1"]],
    }))
    return b


class MonitorTests(unittest.TestCase):
    def monitor(self):
        return CrossVenueMonitor(("BINANCE", "BYBIT"), Decimal("25"), 5 * SEC)

    def test_normal_spread_is_within_threshold(self):
        result = self.monitor().observe({"BINANCE": book("BINANCE", 100_000, 100_001),
                                         "BYBIT": book("BYBIT", 100_001, 100_002)}, 0)
        self.assertEqual(result.classification, "WITHIN_THRESHOLD")
        self.assertAlmostEqual(float(result.bps), -0.1, places=3)

    def test_both_sides_moving_is_a_market_divergence_not_a_fault(self):
        monitor = self.monitor()
        monitor.observe({"BINANCE": book("BINANCE", 100_000, 100_001), "BYBIT": book("BYBIT", 100_000, 100_001)}, 0)
        result = monitor.observe({"BINANCE": book("BINANCE", 100_400, 100_401),
                                  "BYBIT": book("BYBIT", 100_050, 100_051)}, 1 * SEC)
        self.assertEqual((result.classification, result.venue), ("MARKET_DIVERGENCE", ""))

    def test_frozen_venue_is_named(self):
        monitor = self.monitor()
        frozen = book("BYBIT", 100_000, 100_001)
        monitor.observe({"BINANCE": book("BINANCE", 100_000, 100_001), "BYBIT": frozen}, 0)
        result = None
        for second, price in ((3, 100_200), (6, 100_350), (9, 100_500)):     # Binance moves, Bybit never does
            result = monitor.observe({"BINANCE": book("BINANCE", price, price + 1), "BYBIT": frozen}, second * SEC)
        self.assertEqual((result.classification, result.venue), ("STALLED_VENUE", "BYBIT"))

    def test_delayed_feed_is_a_stale_side(self):
        result = self.monitor().observe({"BINANCE": book("BINANCE", 100_500, 100_501),
                                         "BYBIT": book("BYBIT", 100_000, 100_001)}, 0, delayed=["BYBIT"])
        self.assertEqual((result.classification, result.venue), ("STALE_SIDE", "BYBIT"))

    def test_invalid_book_is_not_compared(self):
        bad = book("BYBIT", 100_000, 100_001)
        bad.state = BookState.GAP_DETECTED
        self.assertIsNone(self.monitor().observe({"BINANCE": book("BINANCE", 1, 2), "BYBIT": bad}, 0))


class LiveCrossVenueTests(unittest.TestCase):
    def test_divergence_and_convergence_are_logged_on_transitions(self):
        mono = {"ns": 0}
        with tempfile.TemporaryDirectory() as directory:
            service = LiveOrderBookService(
                ROOT / "config/research.json", Path(directory), "xv",
                LiveFeedSettings(durable=False), clock_ns=lambda: 1_700_000_000 * SEC,
                monotonic_ns=lambda: mono["ns"])

            touch = {}            # each venue's current touch, removed when it moves

            def levels(venue, bid, ask):
                old = touch.get(venue)
                touch[venue] = (bid, ask)
                bids, asks = [[str(bid), "1"]], [[str(ask), "1"]]
                if old:
                    bids.append([str(old[0]), "0"])
                    asks.append([str(old[1]), "0"])
                return bids, asks

            def bybit(kind, u, bid, ask):
                bids, asks = levels("BYBIT", bid, ask)
                service._process_websocket_frame("BYBIT", "wss://bybit", json.dumps({
                    "topic": "orderbook.50.BTCUSDT", "type": kind, "ts": 1_700_000_000_000,
                    "data": {"s": "BTCUSDT", "u": u, "b": bids, "a": asks}}),
                    channel="orderbook.50.BTCUSDT")

            def binance(first, last, bid, ask):
                bids, asks = levels("BINANCE", bid, ask)
                service._process_websocket_frame("BINANCE", "wss://binance", json.dumps({
                    "e": "depthUpdate", "E": 1_700_000_000_000, "s": "BTCUSDT", "U": first, "u": last,
                    "b": bids, "a": asks}), channel="depth@100ms")

            service._process_binance_snapshot("https://binance/depth", json.dumps(
                {"lastUpdateId": 100, "bids": [["100000", "1"]], "asks": [["100001", "1"]]}), 1_700_000_000 * SEC)
            touch["BINANCE"] = (100_000, 100_001)
            bybit("snapshot", 10, 100_000, 100_001)
            mono["ns"] = 1 * SEC
            binance(101, 101, 100_400, 100_401)                    # +40 bps: Binance moved
            bybit("delta", 11, 100_100, 100_101)                   # Bybit moved too: market divergence
            mono["ns"] = 2 * SEC
            bybit("delta", 12, 100_390, 100_391)                   # converged
            rows = [json.loads(l) for l in (service.recorder.session_dir / "structured_logs.jsonl")
                    .read_text().splitlines()]
            events = [(r["event_type"], r["payload"]["classification"]) for r in rows
                      if r["event_type"].startswith("CROSS_VENUE")]
            self.assertEqual(events, [("CROSS_VENUE_DIVERGENCE", "MARKET_DIVERGENCE"),
                                      ("CROSS_VENUE_CONVERGED", "WITHIN_THRESHOLD")])
            report = service._service_summary()["cross_venue"]
            self.assertEqual(report["breaches"], {"MARKET_DIVERGENCE": 2})
            self.assertEqual(report["threshold_bps"], "25.0")
            service.recorder.close({})
            service.ohlc_writer.close()
            service.ltp_writer.close()

    def test_single_venue_service_has_no_cross_venue_monitor(self):
        with tempfile.TemporaryDirectory() as directory:
            service = LiveOrderBookService(ROOT / "config/research.json", Path(directory), "one",
                                           LiveFeedSettings(venues=("BYBIT",), durable=False))
            self.assertIsNone(service.cross_venue)
            self.assertIsNone(service._service_summary()["cross_venue"])
            service.recorder.close({})
            service.ohlc_writer.close()
            service.ltp_writer.close()


if __name__ == "__main__":
    unittest.main()
