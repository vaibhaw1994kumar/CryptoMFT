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


# ---- W6 (owner decision 5, 16 Nov): dislocation episodes, NOTIFY-ONLY ------------------------------------------------------------------------------------------------------------------------
from crypto_stat_arb.cross_venue import DislocationMonitor, Episode, FAIL_CLOSED_RULE, assess_episode, dislocation_policy, dislocation_scan, false_alarm_report  # noqa: E402

POLICY = {"enabled": True, "open_bps": 10.0, "close_bps": 8.0, "close_hold_s": 1.0, "baseline_window_s": 20.0, "max_episode_s": 60.0, "pair_max_skew_ms": 500.0, "max_unobservable_s": 5.0,
          "data_lookback_s": 5.0, "venue_event_min_baseline_n": 3}
T0 = 1_700_000_000 * SEC


def snap(venue, seq, mid, ts, *, depth="50", spread="0.01"):
    half = Decimal(spread) / 2
    return MarketEvent.from_dict({"event_type": "BOOK_SNAPSHOT", "venue": venue, "instrument_id": "SOL-USDT-PERP", "exchange_ts_ns": ts, "source_ts_ns": ts, "receive_ts_ns": ts, "sequence": seq,
                                  "bids": [{"price": str(Decimal(mid) - half), "quantity": depth}], "asks": [{"price": str(Decimal(mid) + half), "quantity": depth}]})


def stream(gap_bps_at, seconds=60, step_ms=100, depth="50"):
    """Two venues every `step_ms`; BYBIT at 100 plus `gap_bps_at(t)` bps (t in seconds from the start), BINANCE at 100."""
    ev, seq = [], 0
    for k in range(int(seconds * 1000 / step_ms)):
        t = k * step_ms / 1000
        ts = T0 + int(t * SEC)
        seq += 1
        ev.append(snap("BINANCE", seq, "100.00", ts, depth=depth))
        ev.append(snap("BYBIT", seq, f"{100 * (1 + gap_bps_at(t) / 10000):.4f}", ts + 1_000_000, depth=depth))
    return ev


class DislocationEpisodes(unittest.TestCase):
    def policy(self, **over):
        return dislocation_policy({**POLICY, **over}, size_base=1.0)

    def test_off_and_refusals(self):
        self.assertIsNone(dislocation_policy(None))
        self.assertIsNone(dislocation_policy({"enabled": False, "open_bps": 5}))
        with self.assertRaisesRegex(ValueError, "defined, not built"):
            dislocation_policy({**POLICY, "fail_closed": {"enabled": True}})
        with self.assertRaisesRegex(ValueError, "not built"):
            dislocation_policy({**POLICY, "reference": {"enabled": True}})
        with self.assertRaises(ValueError):
            dislocation_policy({**POLICY, "surprise": 1})
        with self.assertRaises(ValueError):
            dislocation_policy({**POLICY, "open_bps": 5.0, "close_bps": 8.0})

    def test_an_episode_opens_against_the_baseline_and_closes_with_hysteresis(self):
        gap = lambda t: 20.0 if 30 <= t < 35 else 2.0                          # a persistent 2 bp basis (the baseline) and a 5 s jump of 18 bp above it
        out = dislocation_scan(stream(gap), self.policy())
        (ep,) = out["records"]
        self.assertEqual(ep["end_reason"], "RECOVERED")
        self.assertAlmostEqual(ep["baseline_bps"], -2.0, delta=0.2)           # gap = first venue (sorted, BINANCE) minus second: the 2 bp basis is the baseline, not an episode
        self.assertAlmostEqual(ep["duration_s"], 6.0, delta=0.3)              # 5 s plus the 1 s close hold
        self.assertGreater(abs(ep["deviation_extreme_bps"]), 17.0)
        self.assertEqual(ep["rich_venue"], "BYBIT")                            # BYBIT is the one priced above the baseline
        self.assertEqual(ep["decision"], "NOTIFY_ONLY")
        self.assertEqual(ep["class"], "UNCLASSIFIED")                          # the scan had no feed-health monitor: never TRADEABLE without one
        self.assertIn("NO_FEED_HEALTH_MONITOR", ep["reason_codes"])

    def test_a_thin_book_has_no_executable_price_at_size_and_a_deep_one_has_a_net_edge(self):
        gap = lambda t: 40.0 if 30 <= t < 33 else 0.0
        deep = dislocation_scan(stream(gap, depth="50"), self.policy())["records"][0]["executable"]
        self.assertIsNotNone(deep["net_edge_bps_at_open"])
        thin = dislocation_scan(stream(gap, depth="0.5"), self.policy())["records"][0]["executable"]
        self.assertIsNone(thin["net_edge_bps_at_open"])
        self.assertEqual(thin["BYBIT"]["reason"], "INSUFFICIENT_DEPTH")

    def test_a_venue_that_stops_keeps_the_episode_open_then_closes_it_unobservable_as_data(self):
        events = [e for e in stream(lambda t: 20.0 if t >= 30 else 0.0) if not (e.venue == "BYBIT" and e.receive_ts_ns > T0 + 32 * SEC)]
        (ep,) = dislocation_scan(events, self.policy())["records"]
        self.assertEqual(ep["end_reason"], "UNOBSERVABLE")
        self.assertEqual(ep["class"], "DATA")
        self.assertEqual(ep["would_be_action"], FAIL_CLOSED_RULE["DATA"])

    def test_classes_by_evidence(self):
        ep = lambda dur=5.0, net=3.0, end="RECOVERED": Episode(1, ("A", "B"), 0, 0.0, 20.0, 20.0, 20.0, end_ns=int(dur * SEC), end_reason=end, executable={"net_edge_bps_at_open": net})
        healthy = {"feed_health_monitor": True, "books_valid_at_open": True}
        self.assertEqual(assess_episode(ep(), healthy, durations_non_data=[], min_baseline_n=3)["class"], "TRADEABLE")
        self.assertEqual(assess_episode(ep(net=-1.0), healthy, durations_non_data=[], min_baseline_n=3)["class"], "UNCLASSIFIED")
        self.assertEqual(assess_episode(ep(net=None), healthy, durations_non_data=[], min_baseline_n=3)["reason_codes"], ["NO_EXECUTABLE_PRICE_AT_SIZE"])
        long = ep(dur=100.0)
        self.assertEqual(assess_episode(long, healthy, durations_non_data=[1.0, 2.0, 3.0], min_baseline_n=3)["reason_codes"], ["PERSISTENT_BEYOND_P99", "NO_VENUE_SIGNAL"])      # never VENUE_EVENT by persistence alone
        self.assertEqual(assess_episode(long, {**healthy, "venue_signal": {"mark_gap_bps": 15.0}}, durations_non_data=[1.0, 2.0, 3.0], min_baseline_n=3)["class"], "VENUE_EVENT")
        self.assertEqual(assess_episode(ep(), {"books_valid_at_open": True}, durations_non_data=[], min_baseline_n=3)["class"], "UNCLASSIFIED")     # no feed-health monitor

    def test_no_data_episode_is_ever_tradeable_for_any_combination_of_data_triggers(self):
        import itertools
        triggers = ("feed_not_healthy", "clock_not_synced", "book_not_valid", "sequence_gaps", "own_lag", "feed_delayed")
        for bits in itertools.product((False, True), repeat=len(triggers)):
            if not any(bits):
                continue
            evidence = {"feed_health_monitor": True, "books_valid_at_open": True, **dict(zip(triggers, bits))}
            got = assess_episode(Episode(1, ("A", "B"), 0, 0.0, 30.0, 30.0, 30.0, end_ns=SEC, end_reason="RECOVERED", executable={"net_edge_bps_at_open": 50.0}), evidence,
                                 durations_non_data=[], min_baseline_n=3)
            self.assertEqual(got["class"], "DATA", bits)

    def test_the_false_alarm_report_counts_what_the_definition_would_block(self):
        eps = [{"class": "DATA", "start_ns": 10 * SEC, "end_ns": 20 * SEC}, {"class": "TRADEABLE", "start_ns": 30 * SEC, "end_ns": 31 * SEC}]
        self.assertEqual(false_alarm_report(eps, [15.0, 30.5, 40.0]), {"episodes": 2, "would_block_episodes": 1, "would_block_seconds": 10.0, "signals_inside_would_block_windows": 1})

    def test_the_monitor_imports_no_order_risk_or_execution_module(self):
        import ast
        tree = ast.parse((ROOT / "src/crypto_stat_arb/cross_venue.py").read_text())
        names = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        self.assertFalse({m for m in names if any(part in ("oms", "risk", "ems", "circuit_breaker", "session") for part in m.split("."))}, names)
