"""Spot simulator validation (new_backtest.validation): the aggregate definitions and the real-label rule of the fill-model fit."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from new_backtest.validation import evaluate
from new_backtest.validation.fill_model_fit import calibration_slope, records_for_run
from new_backtest.validation.spot_order_replay import max_gap


def ts(h, m, s=0.0, day=26):
    return datetime(2026, 4, day, h, m, tzinfo=timezone.utc).timestamp() + s


def row(**kw):
    base = {"pair": "ADAUSDT", "day": "2026-04-26", "created": ts(10, 0), "side": "buy", "real_status": "Filled", "real_end": ts(10, 0, 5),
            "real_first_fill": ts(10, 0, 5), "real_filled_qty": 1.0, "qty": 1.0, "crosses_day_end": False, "max_depth_gap_s": 0.5,
            "placed_ts": ts(10, 0, 0.01), "queue_ahead": 10.0, "level_size": 20.0}
    base.update(kw)
    return base


class EvaluateTests(unittest.TestCase):
    def test_restart_window_and_resting_across_the_resync_are_excluded(self):
        self.assertEqual(evaluate.excluded(row(day="2026-04-23", created=ts(6, 5, day=23), real_end=ts(6, 6, day=23))), "restart_window")
        self.assertEqual(evaluate.excluded(row(day="2026-04-23", created=ts(6, 0, day=23), real_end=ts(6, 25, day=23))), "resting_across_resync")
        self.assertEqual(evaluate.excluded(row(day="2026-04-23", created=ts(6, 0, day=23), real_end=ts(6, 3, day=23))), "")
        self.assertEqual(evaluate.excluded(row(pair="MNTUSDT", day="2026-04-23", created=ts(6, 20, day=23), real_end=ts(6, 30, day=23))), "")     # just after the window

    def test_any_simulated_fill_of_a_really_cancelled_order_is_a_false_fill(self):
        rows = [row(real_status="Cancelled", real_first_fill=None, filled_qty=0.3), row(real_status="Cancelled", real_first_fill=None)]
        r = evaluate.evaluate(rows)["cancelled_false_fill"]["all"]
        self.assertEqual((r["n"], r["k"]), (2, 1))

    def test_first_fill_timing_classes_use_one_second_and_all_orders(self):
        real = ts(10, 0, 5)
        rows = [row(first_fill_exchange_ts=real + 0.9), row(first_fill_exchange_ts=real - 1.1), row(first_fill_exchange_ts=real + 1.1), row(), row(rejected="postonly_reject")]
        t = evaluate.evaluate(rows)["filled_timing"]["all"]
        self.assertEqual([t[k]["k"] for k in ("hit", "premature", "late", "unfilled", "sim_rejected")], [1, 1, 1, 1, 1])
        self.assertEqual(t["hit"]["n"], 5)

    def test_censored_rows_are_counted_not_scored(self):
        res = evaluate.evaluate([row(crosses_day_end=True), row(skipped="book_not_ready"), row()])
        self.assertEqual(res["scored"], 1)
        self.assertEqual(res["censored"], {"resting_across_day_end": 1, "engine_skipped_book_not_ready": 1})

    def test_postonly_confusion(self):
        rows = [row(real_status="Rejected", real_first_fill=None, rejected="postonly_reject"), row(real_status="Rejected", real_first_fill=None), row(rejected="postonly_reject")]
        self.assertEqual(evaluate.evaluate(rows)["postonly_confusion"],
                         {"real_rejected__sim_rejected": 1, "real_rejected__sim_accepted": 1, "real_accepted__sim_rejected": 1})

    def test_wilson_interval(self):
        self.assertEqual(evaluate.wilson(0, 0), [None, None])
        lo, hi = evaluate.wilson(5, 10)
        self.assertAlmostEqual(lo, 0.2366, places=3)
        self.assertAlmostEqual(hi, 0.7634, places=3)

    def test_max_depth_gap(self):
        self.assertEqual(max_gap([1.0, 2.0, 5.0], 0.0, 6.0), 3.0)
        self.assertEqual(max_gap([], 0.0, 2.5), 2.5)


class PreRegisteredTests(unittest.TestCase):
    """The definitions and pass rules of TOLERANCES.md (10 Nov 2026, fixed before any number)."""

    def test_stale_means_the_last_book_message_is_older_than_one_second_at_placement(self):
        self.assertEqual(evaluate.pre_stale(row(book_age_at_placement_s=1.01)), "stale (>1 s)")
        self.assertEqual(evaluate.pre_stale(row(book_age_at_placement_s=1.0)), "fresh")

    def test_queue_buckets_are_in_quote_currency(self):
        self.assertEqual(evaluate.pre_queue(row(queue_ahead=3000.0, price=0.25)), "b. <= 1,000")
        self.assertEqual(evaluate.pre_queue(row(queue_ahead=30000.0, price=0.25)), "c. 1,000-10,000")
        self.assertEqual(evaluate.pre_queue(row(queue_ahead=50000.0, price=0.25)), "d. > 10,000")
        self.assertEqual(evaluate.pre_queue(row(queue_ahead=0.0, price=0.25)), "a. inside the touch (0)")
        self.assertEqual(evaluate.pre_queue(row(placed_ts=None)), "not placed (simulated PostOnly reject)")

    def test_verdict_rules(self):
        def res(k_ff, hit, prem, cm):
            n = 1000
            return {"cancelled_false_fill": {"all": evaluate.rate(k_ff, n)},
                    "filled_timing": {"all": {"hit": {"rate": hit}, "premature": {"rate": prem}}}, "postonly_confusion": cm}
        cm = {"real_rejected__sim_rejected": 37, "real_rejected__sim_accepted": 1, "real_accepted__sim_rejected": 52, "real_accepted__sim_accepted": 1884}
        v = evaluate.verdicts({"proportional": res(4, 0.839, 0.043, cm), "behind_first": res(3, 0.7, 0.02, cm), "ahead_first": res(150, 0.7, 0.1, cm)})
        self.assertEqual(v["2a_false_fill"]["per_model"], {"proportional": "PASS", "behind_first": "PASS", "ahead_first": "FAIL"})
        self.assertEqual(v["2a_false_fill"]["verdict"], "MODEL-DEPENDENT")
        self.assertEqual(v["2b_fill_timing"]["verdict"], "PASS")
        self.assertEqual(v["2c_postonly"]["verdict"], "FAIL")                     # 52 / 1936 = 2.7 % false rejects > 2 %

    def test_probable_amendment_is_an_accepted_order_whose_price_crosses_at_create(self):
        self.assertTrue(evaluate.probable_amendment(row(crosses_at_create=True)))
        self.assertFalse(evaluate.probable_amendment(row(crosses_at_create=True, real_status="Rejected")))
        self.assertFalse(evaluate.probable_amendment(row(crosses_at_create=False)))

    def test_calibration_slope_is_one_for_calibrated_and_lower_for_overconfident_probabilities(self):
        import random
        rnd = random.Random(7)
        p = [rnd.uniform(0.05, 0.95) for _ in range(20000)]
        y = [1.0 if rnd.random() < q else 0.0 for q in p]
        self.assertAlmostEqual(calibration_slope(y, p, [1.0] * len(p)), 1.0, delta=0.08)
        over = [min(0.999, max(0.001, 0.5 + 2 * (q - 0.5))) for q in p]           # too extreme
        self.assertLess(calibration_slope(y, over, [1.0] * len(p)), 0.8)
        self.assertIsNone(calibration_slope([0.0, 0.0], [0.2, 0.3], [1.0, 1.0]))


class RealLabelTests(unittest.TestCase):
    def test_labels_come_from_the_real_order_never_from_the_simulated_fill(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            (run / "engine").mkdir()
            o = row(real_status="Cancelled", real_first_fill=None, real_filled_qty=0.0, real_end=ts(10, 0, 3), filled_qty=1.0)
            (run / "orders.jsonl").write_text(json.dumps(o) + "\n")
            feats = {"side": "bid", "timestamp": o["placed_ts"], "queue_ahead": o["queue_ahead"], "relative_to_touch_ticks": 0, "local_depth": 1.0,
                     "local_depth_baseline": 1.0, "spread_pct": 0.01, "short_vol": 0.0, "trade_count": 1.0, "trade_sign": 0.0, "regime_bucket": "calm",
                     "filled": True, "price_moved_through": False}
            (run / "engine" / "fill_events.jsonl").write_text("\n".join(json.dumps({**feats, "horizon_s": h}) for h in (1, 5, 15)) + "\n")
            recs, n = records_for_run(run)
            self.assertEqual(n["used"], 1)
            by_h = {r.horizon_s: r for r in recs}
            self.assertFalse(any(r.filled for r in recs))                      # the simulator filled it; the real order did not
            self.assertEqual((by_h[1].is_censored, by_h[5].is_censored, by_h[15].is_censored), (False, True, True))   # cancelled after 3 s


if __name__ == "__main__":
    unittest.main()
