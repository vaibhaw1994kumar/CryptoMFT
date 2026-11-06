"""L3 audit decision A (2026-11-04): the three missing tests and the truncated-file count. None of this changes a v1 result.

1. truncated-file count: the loader still yields exactly what it read before the cut; a side channel counts the cut files, and every result carries the count.
2. scalar path: a snapshot / reset restores the external book and never cancels a live own order.
3. dedupe without ids: data with no trade id keeps its genuine identical prints in `multiset` mode and drops them in `set` mode; a repeat delivery (second file) is
   dropped in both. (Documents what each mode does on id-less data such as Jan 2022.)
4. restart-like recovery: the engine has no replay-level process restart. The narrowest recovery path it does have is a lost acknowledgement (UNKNOWN):
   the order stays counted as exposure, the strategy learns of the fill only at the reconciliation, and the inventory reconciles afterwards.
   That is all this test claims."""
from __future__ import annotations

import gzip
import json
import tempfile
import unittest
import warnings
from pathlib import Path

from qv2_support import delta, make_book, make_executor, place, sell_print

from new_backtest import event_loader
from new_backtest.backtester import ArbBacktestConfig, CrossVenueArbBacktester
from new_backtest.event_loader import iter_venue_events, reset_truncation, truncation_report
from test_fault_layer import boot, rec_harness
from test_hedge_failures import fh, mark, walk
from test_maker_order_lifetime import BINANCE, BYBIT
from test_queue_v2_manifest import DAY, build, write_gz

warnings.simplefilter("ignore")


class TruncationCount(unittest.TestCase):
    def test_a_truncated_file_is_counted_and_the_rows_before_the_cut_are_still_yielded(self):
        with tempfile.TemporaryDirectory() as d:
            lines = [json.dumps({"exchange_ts": 1641340800.0 + i, "local_ts": 1641340800.0 + i, "price": 152.4, "volume": 1.0, "side": "b"}) for i in range(5000)]
            full, cut = Path(d) / "full.jsonl.gz", Path(d) / "cut.jsonl.gz"
            write_gz(full, lines)
            write_gz(cut, lines, truncate=True)
            reset_truncation()
            n_full = len(list(event_loader._iter_jsonl_gz(full)))
            self.assertEqual(truncation_report()["truncated_files"], 0)
            n_cut = len(list(event_loader._iter_jsonl_gz(cut)))
            rep = truncation_report()
            self.assertEqual(n_full, 5000)
            self.assertTrue(0 < n_cut <= 5000)                                                # what was read is kept, exactly as before the counter existed
            self.assertEqual(rep["truncated_files"], 1)
            self.assertEqual(rep["files"], [str(cut)])
            reset_truncation()
            self.assertEqual(truncation_report()["truncated_files"], 0)

    def test_the_counter_does_not_change_the_events_yielded(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "data"
            build(root, truncated=True)
            reset_truncation()
            a = [e.payload for e in iter_venue_events(str(root), "bybit", "SOLUSDT", DAY, DAY)]
            b = [e.payload for e in iter_venue_events(str(root), "bybit", "SOLUSDT", DAY, DAY)]
            self.assertEqual(a, b)
            self.assertGreater(len(a), 0)

    def test_every_result_carries_the_count_and_the_queue_parameter_label(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "data"
            build(root, truncated=True)
            cfg = ArbBacktestConfig(data_root=str(root), venues=("bybit",), pair="SOLUSDT", start_date=DAY, end_date=DAY, bybit_fee_preset="bybit_perp_mm",
                                    binance_fee_preset="binance_perp_vip0", allow_rejected_data="fixture: truncated on purpose")
            r = CrossVenueArbBacktester(cfg).run()
            self.assertEqual(r.data_integrity["truncated_files"], 1)
            self.assertIn("SPOT_PRIOR_PERP_UNVALIDATED", r.queue_parameter_status)

    def test_a_clean_run_reports_zero(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "data"
            build(root)
            cfg = ArbBacktestConfig(data_root=str(root), venues=("bybit",), pair="SOLUSDT", start_date=DAY, end_date=DAY, bybit_fee_preset="bybit_perp_mm",
                                    binance_fee_preset="binance_perp_vip0", allow_rejected_data="fixture")
            self.assertEqual(CrossVenueArbBacktester(cfg).run().data_integrity["truncated_files"], 0)


class ResetOnTheScalarPath(unittest.TestCase):
    def _case(self, snapshot_bids):
        ex = make_executor(cancel_model="proportional", fill_evidence="off", exchange_time_eligibility=True)
        book = make_book(bids=[(100.0, 20.0), (99.0, 100.0)])
        o = place(ex, book, price=100.0, size=3.0)
        self.assertEqual(ex.orders_cancelled, 0)
        book.apply_snapshot(snapshot_bids, [(101.0, 50.0)], 5.0)
        ex.on_snapshot(book.venue, book, 5.0, 1.0, 0.0, event_ts=5.0)
        return ex, o

    def test_a_snapshot_that_resizes_the_level_never_cancels_the_order(self):
        for size in (5.0, 60.0):
            ex, o = self._case([(100.0, size), (99.0, 100.0)])
            self.assertIn(o.order_id, ex.orders, size)
            self.assertEqual(ex.orders_cancelled, 0, size)
            self.assertGreaterEqual(o.queue_ahead, 0.0)
            self.assertAlmostEqual(o.filled_qty + o.remaining, 3.0)                       # quantity conserved: the commitment is intact

    def test_a_snapshot_that_drops_the_price_level_does_not_cancel_it_either(self):
        ex, o = self._case([(99.0, 100.0), (98.0, 50.0)])                                  # our price is no longer in the reset book
        self.assertEqual(ex.orders_cancelled, 0)
        self.assertTrue(o.order_id in ex.orders or o.filled_qty > 0.0)                      # still live, or it was FILLED by the model: never silently dropped
        self.assertAlmostEqual(o.filled_qty + o.remaining, 3.0)


class DedupeWithoutIds(unittest.TestCase):
    """Two genuinely different prints with the same (ts, price, volume, side) and no trade id: the data cannot tell them from a repeat delivery."""
    def _tree(self, d, second_file: bool):
        root = Path(d) / "data"
        base = 1641340800.0
        pr = lambda i: json.dumps({"exchange_ts": base + 1.0, "local_ts": base + 1.0 + 0.01 * i, "price": 152.4, "volume": 2.0, "side": "s"})
        write_gz(root / "bybit" / "trades" / "pair=SOLUSDT" / f"date={DAY}" / "1641340800000.jsonl.gz", [pr(0), pr(1), json.dumps({"exchange_ts": base + 2.0, "local_ts": base + 2.0, "price": 152.5, "volume": 1.0, "side": "b"})])
        if second_file:                                                                    # the same prints delivered again by an overlapping file
            write_gz(root / "bybit" / "trades" / "pair=SOLUSDT" / f"date={DAY}" / "1641340900000.jsonl.gz", [pr(0), pr(1)])
        return root

    def _count(self, root, mode):
        return len([e for e in iter_venue_events(str(root), "bybit", "SOLUSDT", DAY, DAY, include_snapshots=False, include_deltas=False, trade_dedupe=mode)])

    def test_set_mode_drops_a_genuine_identical_print_multiset_keeps_it(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._tree(d, second_file=False)
            self.assertEqual(self._count(root, "set"), 2)                                  # the second identical print is lost: no id to tell it apart
            self.assertEqual(self._count(root, "multiset"), 3)                             # kept: nothing repeats ACROSS files here

    def test_a_repeat_delivery_in_a_second_file_is_dropped_by_multiset(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._tree(d, second_file=True)
            self.assertEqual(self._count(root, "multiset"), 3)                             # the re-delivered pair is recognised as a repeat, the genuine pair survives
            self.assertEqual(self._count(root, "set"), 2)


class FittedUnknownQueueFallback(unittest.TestCase):
    """Ladder step 6: the fitted queue replaces the universal floor where the fit has data; where it has none the fallback is used AND counted."""
    def _fit(self, d, median):
        fit = Path(d) / "fit.json"
        b = {"d_lo": 1, "d_hi": 10 ** 6, "n": 100 if median else 0, "median": median, "sigma": 0.2 if median else None}
        fit.write_text(json.dumps({"venues": {"bybit": {"tick": 1.0, "buckets": [b] * 7}}}))
        return str(fit)

    def test_with_data_the_draw_is_not_floored_at_25_when_the_floor_is_zero(self):
        with tempfile.TemporaryDirectory() as d:
            ex = make_executor(unknown_queue_model="depth_aware", unknown_queue_dist="fitted", unknown_queue_fit=self._fit(d, 8.0), unknown_queue_floor=0.0,
                               unknown_queue_fallback=25.0, cancel_model="proportional", fill_evidence="off")
            o = place(ex, make_book(bids=[(100.0, 5.0), (99.0, 5.0)]), price=90.0, size=1.0)
            self.assertTrue(0.0 < o.queue_ahead < 25.0)
            self.assertNotIn("unknown_queue_fit_missing_venue_used_floor", ex.diag)

    def test_without_data_the_flagged_fallback_is_used(self):
        with tempfile.TemporaryDirectory() as d:
            ex = make_executor(unknown_queue_model="depth_aware", unknown_queue_dist="fitted", unknown_queue_fit=self._fit(d, None), unknown_queue_floor=0.0,
                               unknown_queue_fallback=25.0, cancel_model="proportional", fill_evidence="off")
            o = place(ex, make_book(bids=[(100.0, 5.0), (99.0, 5.0)]), price=90.0, size=1.0)
            self.assertEqual(o.queue_ahead, 25.0)
            self.assertEqual(ex.diag["unknown_queue_fit_missing_venue_used_floor"], 1.0)

    def test_the_default_fallback_keeps_the_floor_behaviour(self):
        with tempfile.TemporaryDirectory() as d:
            ex = make_executor(unknown_queue_model="depth_aware", unknown_queue_dist="fitted", unknown_queue_fit=self._fit(d, None), unknown_queue_floor=17.0,
                               cancel_model="proportional", fill_evidence="off")
            self.assertEqual(place(ex, make_book(bids=[(100.0, 5.0), (99.0, 5.0)]), price=90.0, size=1.0).queue_ahead, 17.0)


class ProfileV11(unittest.TestCase):
    ALLOWED = {"name", "description", "trade_dedupe", "own_impact", "book_tape_repair", "maker.ledger_epoch", "maker.unknown_queue_dist", "maker.unknown_queue_fit",
               "maker.unknown_queue_floor", "maker.unknown_queue_fallback", "maker.vanish_clears_queue", "maker.fill_evidence",
               "decision_status.queue_parameters", "decision_status.data_manifest", "decision_status.unknown_queue_fit",
               # step 8 (cross needs a print) and step 9 (2026-11-04): cap to the print, tick-rounded quotes, one hedge arrival book
               "maker.opposite_cross_confirm_ms", "maker.opposite_cross_cap_to_print", "arb.round_quotes_to_tick", "hedge_arrival_book"}

    @staticmethod
    def _flat(d, p=""):
        o = {}
        for k, v in d.items():
            if k.startswith("_"):
                continue
            o.update(ProfileV11._flat(v, p + k + ".") if isinstance(v, dict) else {p + k: v})
        return o

    def test_v1_1_differs_from_v1_in_exactly_the_ladder_switches(self):
        from new_backtest.profiles import load_profile
        a, b = self._flat(load_profile("perp_perp_maker_taker_v1")), self._flat(load_profile("perp_perp_maker_taker_v1_1"))
        changed = {k for k in set(a) | set(b) if a.get(k, "<absent>") != b.get(k, "<absent>")}
        self.assertEqual(changed, self.ALLOWED)

    def test_v1_1_builds_a_config_with_the_ladder_values_and_keeps_arb_profile_v1(self):
        from new_backtest.profiles import apply_profile, load_profile
        cfg = apply_profile(load_profile("perp_perp_maker_taker_v1_1"), data_root="/nonexistent", pair="SOLUSDT", start_date=DAY, end_date=DAY)
        self.assertEqual((cfg.trade_dedupe, cfg.own_impact, cfg.book_tape_repair), ("multiset", "overlay", False))
        m = cfg.maker
        self.assertEqual((m.ledger_epoch, m.vanish_clears_queue, m.fill_evidence, m.unknown_queue_dist, m.unknown_queue_floor, m.unknown_queue_fallback),
                         (True, False, "trade_confirm", "fitted", 0.0, 25.0))
        self.assertTrue(m.opposite_cross_fills)                                              # inherited from v1 and labelled in the ladder report, not removed
        self.assertEqual(cfg.arb.profile, "perp_perp_maker_taker_v1")                        # the strategy gate only knows the v1 name

    def test_v1_2_differs_from_v1_1_in_exactly_the_join_switch_and_the_run_defining_settings_it_now_carries(self):
        from new_backtest.profiles import load_profile
        a, b = self._flat(load_profile("perp_perp_maker_taker_v1_1")), self._flat(load_profile("perp_perp_maker_taker_v1_2"))
        changed = {k for k in set(a) | set(b) if a.get(k, "<absent>") != b.get(k, "<absent>")}
        self.assertEqual(changed, {"name", "description", "arb.join_touch_on_tick_rounding", "arb.maker_venues", "arb.venue_ticks.bybit", "arb.venue_ticks.binance", "arb.block_on_toxic",
                                   "arb.max_cross_venue_skew_s", "arb.fill_gate_horizon_s", "hedge_move_calibration.file", "hedge_move_calibration.sha256",
                                   "decision_status.hedge_move", "timing.floors_ms.singapore.binance.entry", "timing.floors_ms.singapore.binance.cancel", "message_atomic_book", "margin.above_last_tier", "hedge_faults.query_before_replace"})

    def test_the_join_switch_is_on_in_v1_2_and_off_in_v1_1(self):
        from new_backtest.profiles import apply_profile, load_profile
        kw = dict(data_root="/nonexistent", pair="SOLUSDT", start_date=DAY, end_date=DAY)
        self.assertFalse(apply_profile(load_profile("perp_perp_maker_taker_v1_1"), **kw).arb.join_touch_on_tick_rounding)
        self.assertTrue(apply_profile(load_profile("perp_perp_maker_taker_v1_2"), **{**kw, "start_date": "2024-03-08", "end_date": "2024-03-08"}).arb.join_touch_on_tick_rounding)

    def test_v1_2_pins_its_hedge_move_by_hash_from_train_days_and_a_profile_alone_identifies_the_run(self):
        import copy
        from new_backtest.profiles import ProfileError, apply_profile, load_profile
        doc = load_profile("perp_perp_maker_taker_v1_2")
        kw = dict(data_root="/nonexistent", pair="SOLUSDT", start_date="2024-03-08", end_date="2024-03-08")
        cfg = apply_profile(doc, **kw)
        a = cfg.arb
        self.assertAlmostEqual(a.expected_hedge_move_bps_bid, -0.48796848488618055)         # owner 6 Nov: the 7-day, fixed 45 ms arrival estimate (not the 1 s window of -0.771)
        hm = cfg.release_identity["hedge_move_calibration"]
        self.assertEqual((hm["horizon_s"], hm["n"]["bid"], len(hm["days"])), (0.045, 2037, 7))        # recorded as an arrival estimate in the release identity, with the file's hash
        self.assertEqual((a.maker_venues, a.venue_ticks, a.fill_gate_horizon_s), (("bybit",) if isinstance(a.maker_venues, tuple) else ["bybit"], {"bybit": 0.001, "binance": 0.001}, 5))
        with self.assertRaises(ProfileError):                                             # a TRAIN day on or after the run's start
            apply_profile(doc, **{**kw, "start_date": "2024-03-02", "end_date": "2024-03-02"})
        bad = copy.deepcopy(doc)
        bad["hedge_move_calibration"]["sha256"] = "0" * 64                                # the pinned file no longer matches its recorded hash
        with self.assertRaises(ProfileError):
            apply_profile(bad, **kw)
        v11 = apply_profile(load_profile("perp_perp_maker_taker_v1_1"), **kw)
        self.assertEqual(v11.arb.expected_hedge_move_bps_bid, 0.0)                         # v1.1 pins nothing: the runner supplies it, as before

    def test_v1_2_floors_the_cross_region_timelines_at_35_ms_for_singapore_only_and_v1_1_has_none(self):
        from new_backtest.profiles import apply_profile, load_profile
        kw = dict(data_root="/nonexistent", pair="SOLUSDT", start_date="2024-03-08", end_date="2024-03-08")
        v = apply_profile(load_profile("perp_perp_maker_taker_v1_2"), **kw).timing.venues
        self.assertEqual((v["binance"].entry.min_ms, v["binance"].cancel.min_ms, v["binance"].fill_report.min_ms, v["bybit"].entry.min_ms), (35.0, 35.0, 0.0, 0.0))
        doc = load_profile("perp_perp_maker_taker_v1_2")
        doc["latency_preset"] = "tokyo"; doc["timing"]["from_latency_preset"] = "tokyo"
        self.assertEqual(apply_profile(doc, **kw).timing.venues["bybit"].entry.min_ms, 0.0)          # the floor names singapore's cross-region venue only
        v11 = apply_profile(load_profile("perp_perp_maker_taker_v1_1"), **{**kw, "start_date": "2022-01-13", "end_date": "2022-01-13"}).timing.venues
        self.assertTrue(all(sp.min_ms == 0.0 for tl in v11.values() for sp in (tl.entry, tl.cancel)))

    def test_the_release_identity_names_both_repositories_commits_and_dirty_flags(self):
        from new_backtest.profiles import apply_profile, load_profile, release_identity
        doc = load_profile("perp_perp_maker_taker_v1_2")
        cfg = apply_profile(doc, data_root="/nonexistent", pair="SOLUSDT", start_date="2024-03-08", end_date="2024-03-08")
        g = release_identity(cfg, doc)["git"]
        self.assertEqual(set(g), {"outer", "core"})
        for repo in g.values():
            self.assertTrue(("commit" in repo and isinstance(repo["dirty"], bool)) or "error" in repo)      # a checkout git cannot read says so instead of raising

    def test_the_integrity_switches_are_registered(self):
        from new_backtest.profiles import SWITCHES
        keys = {s.key for s in SWITCHES}
        self.assertLessEqual({"book_tape_repair", "own_impact", "maker.ledger_epoch", "maker.unknown_queue_dist"}, keys)


class UnknownThenReconciled(unittest.TestCase):
    """The narrowest restart-like recovery the engine has (no replay-level process restart exists): a lost acknowledgement."""
    def test_the_order_stays_counted_as_exposure_until_the_reconciliation_and_the_books_agree_afterwards(self):
        h = rec_harness(ttl=30.0, faults=fh(ack_unknown_prob=1.0, ack_timeout_s=2.0, unknown_resolve_s=3.0, response="unwind"))
        boot(h)
        pend = h.bt._pending_exposure()
        self.assertAlmostEqual(pend.buys.get(BYBIT, 0.0), 2.0, msg="the UNKNOWN resting order is reserved against new risk")
        self.assertEqual(h.bt._live_maker_orders(1.0), 1)
        h.sell_trade(4.0, BYBIT, volume=2.5)                                               # it fills at the exchange while the strategy does not know the state
        mark(h, 4.0)
        self.assertEqual(h.bt.hedge["fill_reports_deferred_unknown"], 1)
        self.assertAlmostEqual(h.bt.inventory.base[BINANCE], 0.0)                          # no hedge before the reconciliation (no new risk on a guess)
        walk(h, [5.01, 5.02, 5.1])
        self.assertAlmostEqual(h.bt.inventory.base[BYBIT], 2.0)                            # after reconciliation the legs agree: +2 maker, -2 hedge
        self.assertAlmostEqual(h.bt.inventory.base[BINANCE], -2.0)
        self.assertAlmostEqual(h.bt._pending_exposure().buys.get(BYBIT, 0.0), 0.0)         # the reservation is released once the order is resolved


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
