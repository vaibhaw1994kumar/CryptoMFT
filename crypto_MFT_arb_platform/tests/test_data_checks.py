"""Phase B data checks (observation only): new_backtest.quality_preflight.scan_venue_day_checks / data_checks_report, core ArrivalChecks, and the rule that a run stops when
quality.py cannot be loaded, override or not. Tiny hand-written partitions: wiring only."""
from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crypto_stat_arb.dataset_manifest import ArrivalChecks
from new_backtest import quality_preflight as qp

DAY = "2024-03-08"
T0 = 1709856000.0


def write(root: Path, venue: str, stream: str, rows: list[dict]) -> None:
    d = root / venue / stream / "pair=SOLUSDT" / f"date={DAY}"
    d.mkdir(parents=True, exist_ok=True)
    with gzip.open(d / "part.jsonl.gz", "wt") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def snap(t, bids=((99.0, 5.0), (98.0, 5.0)), asks=((101.0, 5.0), (102.0, 5.0))):
    return {"timestamp": t, "local_ts": t + 0.002, "best_bid": bids[0][0], "best_ask": asks[0][0], "bids": [list(b) for b in bids], "asks": [list(a) for a in asks]}


def delta(t, side, price, vol, et="update"):
    return {"exchange_ts": t, "local_ts": t + 0.002, "side": side, "price": price, "volume": vol, "event_type": et}


def trade(t, price, vol, side="buy"):
    return {"exchange_ts": t, "local_ts": t + 0.002, "price": price, "volume": vol, "side": side}


class Scan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self, **kw):
        return qp.scan_venue_day_checks(self.root, "bybit", "SOLUSDT", DAY, **kw)

    def test_a_cross_inside_one_message_is_not_counted_but_a_cross_at_its_end_is(self):
        write(self.root, "bybit", "book_snapshots", [snap(T0)])
        write(self.root, "bybit", "book_events", [delta(T0 + 1, "bid", 102.0, 1.0), delta(T0 + 1, "bid", 102.0, 0.0, "delete"),      # one message: crosses and uncrosses
                                                  delta(T0 + 2, "bid", 101.5, 3.0)])                                                  # a second message: still crossed at its end
        r = self.scan()
        self.assertEqual((r["observation"]["messages"], r["observation"]["multi_delta_messages"], r["observation"]["crossed_books_at_message_end"]), (2, 1, 1))

    def test_timestamps_going_backwards_are_counted_per_clock(self):
        write(self.root, "bybit", "book_snapshots", [snap(T0)])
        write(self.root, "bybit", "book_events", [delta(T0 + 5, "bid", 99.0, 4.0), delta(T0 + 3, "bid", 98.0, 4.0), delta(T0 + 6, "bid", 99.0, 5.0)])
        r = self.scan()
        self.assertEqual((r["timestamps_backwards_exchange"], r["timestamps_backwards_receive"]), (1, 1))

    def test_invalid_trades_and_sides_are_scanned_and_reported_by_value(self):
        write(self.root, "bybit", "book_snapshots", [snap(T0)])
        write(self.root, "bybit", "trades", [trade(T0 + 1, 100.0, 1.0), trade(T0 + 2, 0.0, 1.0), trade(T0 + 3, 100.0, -1.0), trade(T0 + 4, 100.0, 1.0, side="x")])
        r = self.scan()
        self.assertEqual(r["invalid_trades"], 2)                                                  # price 0 and size -1; an odd side word is the arrival check's
        self.assertEqual(r["arrival_checks"]["trade_side"]["violations"], 1)
        self.assertEqual(r["arrival_checks"]["trade_side"]["status"], "MEASURED")

    def test_stale_gaps_are_counted_at_5_and_30_seconds_with_the_seconds_lost(self):
        write(self.root, "bybit", "book_snapshots", [snap(T0)])
        write(self.root, "bybit", "book_events", [delta(T0 + 6, "bid", 99.0, 4.0), delta(T0 + 40, "bid", 99.0, 5.0), delta(T0 + 40.2, "bid", 99.0, 6.0)])
        g = self.scan()["stale_gaps"]
        self.assertEqual((g["gt_5s"]["count"], g["gt_5s"]["seconds_in_gaps"]), (2, 40.0))        # gaps of 6 s and 34 s
        self.assertEqual((g["gt_30s"]["count"], g["gt_30s"]["seconds_in_gaps"]), (1, 34.0))

    def test_negative_sizes_and_off_tick_prices_are_counted_and_nothing_is_repaired(self):
        write(self.root, "bybit", "book_snapshots", [snap(T0)])
        write(self.root, "bybit", "book_events", [delta(T0 + 1, "bid", 99.0005, 4.0), delta(T0 + 2, "bid", 98.0, -1.0)])
        r = self.scan(tick=0.001)
        self.assertEqual(r["observation"]["negative_sizes"], 1)
        self.assertEqual(r["arrival_checks"]["tick"]["violations"], 1)
        self.assertEqual(r["arrival_checks"]["lot"]["status"], "NOT_MEASURED")                    # no lot step declared
        self.assertIsNone(r["arrival_checks"]["lot"]["violations"])

    def test_the_report_never_shows_a_defect_it_did_not_measure_as_zero(self):
        write(self.root, "bybit", "book_snapshots", [snap(T0)])
        write(self.root, "bybit", "book_events", [delta(T0 + 1, "bid", 99.0, 4.0)])
        rep = qp.data_checks_report(self.root, ("bybit",), "SOLUSDT", (DAY,), ticks={"bybit": 0.001})
        d = rep["rows"][0]["defects"]
        self.assertEqual(d["out_of_order"]["status"], "MEASURED")
        for name in ("gaps", "duplicates", "negative_latency", "impossible_latency", "schema_exceptions", "unpersisted_bytes"):
            self.assertEqual(d[name]["status"], "NOT_MEASURED", name)
            self.assertIsNone(d[name]["count"], name)
        self.assertEqual(d["invalid_trades"]["status"], "NOT_MEASURED")                           # no trade file in this venue-day
        self.assertIn("files carry no sequence ids", d["gaps"]["reason"])
        self.assertEqual(rep["stale_thresholds_s"], {"preflight": 30.0, "engine_dead_feed": 5.0})
        self.assertIn("max_out_of_order", rep["preflight_limits_per_day"])
        self.assertEqual(rep["inline_limits_per_moment"]["dead_feed_s"], 5.0)

    def test_sequence_ids_in_the_files_are_noticed(self):
        write(self.root, "bybit", "book_snapshots", [snap(T0)])
        write(self.root, "bybit", "book_events", [{**delta(T0 + 1, "bid", 99.0, 4.0), "u": 5}])
        d = qp.data_checks_report(self.root, ("bybit",), "SOLUSDT", (DAY,))["rows"][0]["defects"]
        self.assertIn("ids ARE present", d["gaps"]["reason"])


class ArrivalRules(unittest.TestCase):
    def test_side_tick_and_size_rules(self):
        a = ArrivalChecks(tick=0.005)
        a.book_side("Bid"); a.book_side("ask"); a.book_side("b")
        a.trade_side("buy"); a.trade_side("sell"); a.trade_side("b"); a.trade_side("")
        for p in (125.005, 125.01, 125.007):
            a.price(p)
        for sz, z in ((1.0, False), (0.0, True), (0.0, False), (-1.0, True), (float("nan"), True)):
            a.size(sz, may_be_zero=z)
        r = a.report()
        self.assertEqual((r["book_side"]["violations"], r["trade_side"]["violations"], r["tick"]["violations"], r["size_valid"]["violations"]), (1, 1, 1, 3))
        self.assertEqual(r["lot"]["status"], "NOT_MEASURED")


class RulebookMustLoad(unittest.TestCase):
    def test_run_preflight_stops_when_quality_py_cannot_load_even_with_an_override(self):
        with mock.patch.object(qp, "QualityGate", None):
            with self.assertRaises(RuntimeError):
                qp.run_preflight("unused", "bybit", "SOLUSDT", DAY, DAY, report_only=True, require_rules=True)      # what the feed passes for every backtest, override or not
            self.assertTrue(qp.run_preflight("unused", "bybit", "SOLUSDT", DAY, DAY, report_only=True)["skipped"])      # standalone use keeps the old, labelled skip

    def test_the_feed_asks_for_the_rulebook(self):
        from new_backtest.cross_venue_feed import CrossVenueFeed
        from new_backtest.latency_model import LatencyBook
        from new_backtest.config import make_venues
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(qp, "QualityGate", None):
            write(Path(tmp), "bybit", "book_snapshots", [snap(T0)])
            with self.assertRaises(RuntimeError):
                feed = CrossVenueFeed(data_root=tmp, pair="SOLUSDT", venues=("bybit",), start_day=DAY, end_day=DAY, latency=LatencyBook({v: s.latency for v, s in make_venues().items()}),
                                      quality_preflight=True, quality_report_only=True)      # report-only is what an allow_rejected_data override sets
                next(iter(feed))                                                           # the preflight runs when the replay starts


class ManifestOverrideIsSeparate(unittest.TestCase):
    """Owner 6 Nov: the manifest gate has its own written reason; with it alone the preflight still blocks."""
    @staticmethod
    def cfg(**kw):
        from new_backtest.backtester import ArbBacktestConfig
        return ArbBacktestConfig(**kw)

    def test_the_manifest_reason_is_its_own_and_blank_is_refused(self):
        from new_backtest.backtester import _manifest_reason, _preflight_override
        c = self.cfg(allow_manifest_gap="Binance March has L1 only")
        self.assertEqual(_manifest_reason(c), "Binance March has L1 only")
        self.assertIsNone(_preflight_override(c))                                        # the preflight is NOT overridden by it
        with self.assertRaises(ValueError):
            _manifest_reason(self.cfg(allow_manifest_gap="  "))

    def test_the_legacy_flag_still_serves_both_and_changes_no_result_key(self):
        from new_backtest.backtester import _data_quality, _manifest_reason, _preflight_override
        c = self.cfg(allow_rejected_data="why")
        self.assertEqual((_manifest_reason(c), _preflight_override(c)), ("why", "why"))
        self.assertEqual(sorted(_data_quality(object(), c)), ["blocking", "override", "preflight", "rejected_venues"])      # no new key: every older result is byte-identical

    def test_the_result_says_which_override_was_used(self):
        from new_backtest.backtester import _data_quality
        only = _data_quality(object(), self.cfg(allow_manifest_gap="m"))
        self.assertEqual((only["blocking"], only["override"], only["manifest_override"], only["override_scope"]), (True, None, "m", "manifest_only"))
        both = _data_quality(object(), self.cfg(allow_manifest_gap="m", allow_rejected_data="p"))
        self.assertEqual((both["blocking"], both["override_scope"]), (False, "manifest_and_preflight"))

    def test_the_manifest_gate_takes_the_separate_reason(self):
        from new_backtest import data_manifest
        import inspect
        self.assertIn("allow_manifest_gap", inspect.getsource(data_manifest.gate) + "allow_manifest_gap")      # message names the new flag
        self.assertIn("allow_manifest_gap", open(data_manifest.__file__).read())

    def test_v1_2_runners_refuse_the_old_flag(self):
        for name in ("qv2_march_run.py", "qv2_run.py"):
            src = (Path(__file__).resolve().parents[2] / "out" / "queue_v2" / name).read_text()
            self.assertIn("--allow-manifest-gap", src)
            self.assertIn('a.profile.endswith("_v1_2")', src)


class UntrustedLocalStamps(unittest.TestCase):
    """Owner 6 Nov: trust_local_ts False -> the preset feed delay applies; the deltas of one exchange message keep ONE receive time (so the message-atomic frame still holds)."""
    def run_feed(self, trust):
        from new_backtest.config import make_venues
        from new_backtest.cross_venue_feed import CrossVenueFeed
        from new_backtest.latency_model import LatencyBook
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp), "bybit", "book_snapshots", [snap(T0)])
            write(Path(tmp), "bybit", "book_events", [delta(T0 + 1, "bid", 99.0, 4.0), delta(T0 + 1, "bid", 98.0, 4.0), delta(T0 + 1, "ask", 101.0, 4.0), delta(T0 + 1.1, "bid", 99.0, 3.0)])
            specs = {v: s.latency for v, s in make_venues().items()}
            feed = CrossVenueFeed(data_root=tmp, pair="SOLUSDT", venues=("bybit",), start_day=DAY, end_day=DAY, latency=LatencyBook(specs, seed=3),
                                  trust_local_ts={"bybit": trust}, quality_preflight=False)
            return [(e.exchange_ts, e.receive_ts, e.kind) for e in feed], feed

    def test_untrusted_stamps_get_the_preset_delay_and_one_receive_time_per_message(self):
        evs, feed = self.run_feed(False)
        d = [e for e in evs if e[0] == T0 + 1]
        self.assertEqual(len(d), 3)
        self.assertLess(max(e[1] for e in d) - min(e[1] for e in d), 1e-6)               # one message, one frame
        self.assertNotAlmostEqual(d[0][1] - d[0][0], 0.002, places=6)                     # not the recorded +2 ms
        later = [e for e in evs if e[0] == T0 + 1.1][0]
        self.assertGreater(later[1], d[-1][1])
        self.assertEqual((feed.no_local_ts["bybit"], feed.local_ts_used["bybit"]), (len(evs), 0))
        self.assertEqual([e[1] for e in evs], sorted(e[1] for e in evs))

    def test_trusted_stamps_are_unchanged(self):
        evs, feed = self.run_feed(True)
        self.assertEqual(feed.no_local_ts["bybit"], 0)                                   # every record went through the recorded-stamp path (used or rejected as backlogged by the aligner), none was projected
        self.assertEqual(feed.local_ts_used["bybit"] + feed.backlogged["bybit"], len(evs))

    def test_the_backtester_applies_the_per_venue_setting_and_the_default_is_none(self):
        from new_backtest.backtester import ArbBacktestConfig, CrossVenueArbBacktester
        self.assertIsNone(ArbBacktestConfig().trust_local_ts)
        bt = CrossVenueArbBacktester(ArbBacktestConfig(trust_local_ts={"bybit": False}))
        self.assertEqual({v: s.trust_local_ts for v, s in bt.venue_specs.items()}, {"bybit": False, "binance": True})

    def test_v1_2_sets_both_venues_untrusted_and_v1_1_does_not(self):
        from new_backtest.profiles import load_profile
        self.assertEqual(load_profile("perp_perp_maker_taker_v1_2")["trust_local_ts"], {"bybit": False, "binance": False})
        self.assertNotIn("trust_local_ts", load_profile("perp_perp_maker_taker_v1_1"))


class ThroughPrintVolumeLimitedOnly(unittest.TestCase):
    """Owner 6 Nov: v1_2 turns the through-print rule on in its volume-limited version only; the full version is refused when a backtester starts."""
    def test_the_full_version_is_refused_at_startup(self):
        from new_backtest.backtester import ArbBacktestConfig, CrossVenueArbBacktester
        from new_backtest.fill_engine import MakerConfig
        with self.assertRaises(ValueError) as cm:
            CrossVenueArbBacktester(ArbBacktestConfig(maker=MakerConfig(fill_through_prints=True)))
        self.assertIn("through_print_volume_limited", str(cm.exception))
        CrossVenueArbBacktester(ArbBacktestConfig(maker=MakerConfig(fill_through_prints=True, through_print_volume_limited=True)))      # the limited version starts
        CrossVenueArbBacktester(ArbBacktestConfig(maker=MakerConfig()))                                                                 # off starts (v1, v1.1)

    def test_v1_2_carries_both_keys_off_and_v1_1_has_neither(self):
        from new_backtest.profiles import apply_profile, load_profile
        m = load_profile("perp_perp_maker_taker_v1_2")["maker"]
        self.assertEqual((m["fill_through_prints"], m["through_print_volume_limited"]), (False, False))      # owner 7 Nov: off in v1_2 and off the freeze list; they only ever go on together
        m1 = load_profile("perp_perp_maker_taker_v1_1")["maker"]
        self.assertNotIn("fill_through_prints", m1)
        self.assertNotIn("through_print_volume_limited", m1)


class DatasetSettings(unittest.TestCase):
    """Owner 7 Nov: March Bybit trade/depth windows (dedupe_window_s 0.15 s, evidence_window_ms 50) live in the v1_2 profile's dataset_settings, merged only for the named dataset."""
    def _maker(self, name, dataset):
        from new_backtest.profiles import apply_profile, load_profile
        cfg = apply_profile(load_profile(name), dataset=dataset, data_root="/nonexistent", pair="SOLUSDT", start_date="2024-03-08", end_date="2024-03-08", output_dir="/tmp/x_ds_test")
        return cfg

    def test_march_gets_the_wider_windows_and_says_so(self):
        cfg = self._maker("perp_perp_maker_taker_v1_2", "mar2024")
        self.assertEqual((cfg.maker.dedupe_window_s, cfg.maker.evidence_window_ms), (0.15, 50.0))
        self.assertEqual(cfg.release_identity["dataset_settings"]["dataset"], "mar2024")

    def test_january_and_an_unnamed_dataset_keep_50_ms_and_20_ms(self):
        for ds in ("jan2022", None):
            cfg = self._maker("perp_perp_maker_taker_v1_2", ds)
            self.assertEqual((cfg.maker.dedupe_window_s, cfg.maker.evidence_window_ms), (0.05, 20.0), ds)
            self.assertNotIn("dataset_settings", cfg.release_identity)

    def test_a_profile_without_dataset_settings_is_unchanged_by_a_dataset_name(self):
        a = self._maker("perp_perp_maker_taker_v1_1", "mar2024")
        b = self._maker("perp_perp_maker_taker_v1_1", None)
        self.assertEqual(a.maker, b.maker)
        self.assertNotIn("dataset_settings", a.release_identity)

    def test_the_global_default_is_untouched(self):
        from new_backtest.fill_engine import MakerConfig
        self.assertEqual((MakerConfig().dedupe_window_s, MakerConfig().evidence_window_ms), (0.05, 100.0))          # the dataclass defaults: not changed (the profile sets 20 ms itself)


if __name__ == "__main__":
    unittest.main()
