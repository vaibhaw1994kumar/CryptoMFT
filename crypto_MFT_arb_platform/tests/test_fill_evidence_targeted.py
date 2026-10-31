"""Targeted evidence rule ("confirm_delta_snapshot", "confirm_delta") and the empty-queue threshold (queue_empty_threshold).

confirm_delta_snapshot: a delta_share fill (a depth decrease that finds no queue ahead) and a snapshot-shrink fill need a confirming
trade at our price within evidence_window_ms (mark the front, fill up to the trade's size, cause trade_after_front). A vanished level
(delta_vanish) is NOT gated and behaves as "off"; ordinary trade fills are unchanged. confirm_delta gates only delta_share.
queue_empty_threshold: "absolute" (1e-12, today's behaviour, bit-identical), "lot", "frac_of_order"."""
from __future__ import annotations

import unittest

from new_backtest.fill_engine import MakerConfig
from test_fill_engine_queue import _book, _executor, _place
from test_fill_evidence import GOLDEN, OffIsIdenticalTests, TICK, decrease, front_order, stream_digest, stream_run, trade

MODELS = ("proportional", "all_ahead")


def vanish_setup(config):
    ex = _executor(config)
    o = _place(ex, _book(bids=[(100.0, 5.0)]), price=100.0, size=5.0)
    o.arrival_ts = 0.0
    ex.on_delta("bybit", "bid", 100.0, 8.0, 1.5, TICK, 0.0)           # 3 more join behind us
    return ex, o


class ValidationTests(unittest.TestCase):
    def test_new_modes_and_thresholds_are_validated(self):
        for mode in ("confirm_delta_snapshot", "confirm_delta"):
            self.assertEqual(MakerConfig(fill_evidence=mode).fill_evidence, mode)
        for bad in ({"queue_empty_threshold": "bogus"}, {"queue_empty_threshold": "lot"}, {"queue_empty_threshold": "lot", "queue_empty_lot": 0.0},
                    {"queue_empty_threshold": "frac_of_order", "queue_empty_frac": 0.0},
                    {"queue_empty_threshold": "frac_of_order", "queue_empty_frac": 1.5}):
            with self.assertRaises(ValueError, msg=str(bad)):
                MakerConfig(**bad)
        self.assertEqual(MakerConfig().queue_empty_threshold, "absolute")
        self.assertEqual(MakerConfig().fill_evidence, "off")


class GatedDeltaAndSnapshotTests(unittest.TestCase):
    def cfg(self, model, mode="confirm_delta_snapshot", **kw):
        return MakerConfig(cancel_model=model, fill_evidence=mode, **kw)

    def test_a_delta_share_decrease_alone_does_not_fill_and_marks_the_front(self):
        for model in MODELS:
            for mode in ("confirm_delta_snapshot", "confirm_delta"):
                ex, o = front_order(self.cfg(model, mode))
                self.assertEqual(decrease(ex, 2.0, 2.0), [], (model, mode))
                self.assertEqual((o.filled_qty, o.front_ts, o.queue_ahead), (0.0, 2.0, 0.0))
                self.assertAlmostEqual(o.front_decrease, 3.0, places=9)

    def test_a_trade_inside_the_window_fills_up_to_the_trade_size_with_the_new_cause(self):
        for model in MODELS:
            for mode in ("confirm_delta_snapshot", "confirm_delta"):
                ex, o = front_order(self.cfg(model, mode, evidence_fill_fraction=0.01))     # the fraction must play no part
                decrease(ex, 2.0, 2.0)
                f = trade(ex, 1.2, 2.03)
                self.assertEqual([(round(x.qty, 9), x.cause) for x in f], [(1.2, "trade_after_front")], (model, mode))
                self.assertIsNone(o.front_ts)

    def test_fill_is_capped_at_the_trade_size_and_the_order_size(self):
        ex, o = front_order(self.cfg("proportional"), size=1.0)
        decrease(ex, 2.0, 2.0)
        self.assertEqual([(x.qty, x.cause) for x in trade(ex, 7.0, 2.01)], [(1.0, "trade_after_front")])
        self.assertFalse(o.is_live)

    def test_a_trade_after_the_window_is_not_a_confirmation(self):
        ex, o = front_order(self.cfg("proportional", evidence_window_ms=10.0))
        decrease(ex, 2.0, 2.0)
        self.assertEqual(trade(ex, 1.2, 2.03), [])                   # inside the 50 ms de-duplication window, outside the 10 ms one
        self.assertEqual(o.filled_qty, 0.0)
        self.assertEqual([(round(x.qty, 9), x.cause) for x in trade(ex, 1.2, 5.0)], [(1.2, "trade")])      # ordinary rule, much later

    def test_a_trade_at_another_price_never_confirms(self):
        ex, o = front_order(self.cfg("proportional"))
        decrease(ex, 2.0, 2.0)
        f = trade(ex, 1.0, 2.01, price=99.0)                   # a sell below our bid is an ordinary trade for it, not a confirmation
        self.assertNotIn("trade_after_front", [x.cause for x in f])
        ex, o = front_order(self.cfg("proportional"))
        decrease(ex, 2.0, 2.0)
        self.assertEqual(ex.on_trade("bybit", 100.0, 1.0, +1, 2.01, 0.0), [])        # a buy aggressor does not hit a bid
        self.assertIsNotNone(o.front_ts)

    def test_the_vanished_level_is_not_gated_in_either_mode(self):
        for mode in ("confirm_delta_snapshot", "confirm_delta"):
            ex, o = vanish_setup(self.cfg("proportional", mode))
            off, o_off = vanish_setup(MakerConfig(cancel_model="proportional"))
            f, f_off = decrease(ex, 0.0, 2.0), decrease(off, 0.0, 2.0)
            self.assertEqual([(round(x.qty, 9), x.cause) for x in f], [(0.45, "delta_vanish")], mode)
            self.assertEqual([(x.qty, x.cause) for x in f], [(x.qty, x.cause) for x in f_off])
            self.assertEqual((o.queue_ahead, o.front_ts), (o_off.queue_ahead, None))          # no 'queue emptied by the vanish' rule either

    def test_the_vanished_level_is_gated_by_the_older_modes_for_contrast(self):
        ex, o = vanish_setup(self.cfg("proportional", "trade_confirm"))
        self.assertEqual(decrease(ex, 0.0, 2.0), [])
        self.assertEqual(o.front_ts, 2.0)

    def test_snapshot_shrink_is_gated_only_by_confirm_delta_snapshot(self):
        for mode, filled, marked in (("off", 0.15, False), ("confirm_delta_snapshot", 0.0, True), ("confirm_delta", 0.15, False)):
            ex, o = front_order(MakerConfig(cancel_model="proportional", fill_evidence=mode))
            f = ex.on_snapshot("bybit", _book(bids=[(100.0, 2.0)]), 2.0, TICK, 0.0)
            self.assertAlmostEqual(sum(x.qty for x in f), filled, places=9, msg=mode)
            self.assertEqual(o.front_ts is not None, marked, mode)
            self.assertEqual([x.cause for x in f], ["snapshot"] if filled else [], mode)

    def test_snapshot_mark_is_confirmed_by_a_trade(self):
        ex, o = front_order(self.cfg("proportional"))
        ex.on_snapshot("bybit", _book(bids=[(100.0, 2.0)]), 2.0, TICK, 0.0)
        self.assertEqual([(round(x.qty, 9), x.cause) for x in trade(ex, 0.7, 2.04)], [(0.7, "trade_after_front")])

    def test_ordinary_trade_fills_are_unchanged(self):
        for mode in ("off", "confirm_delta_snapshot", "confirm_delta"):
            ex, o = front_order(MakerConfig(cancel_model="proportional", fill_evidence=mode))
            self.assertEqual([(round(x.qty, 9), x.cause) for x in trade(ex, 2.0, 2.0)], [(2.0, "trade")], mode)

    def test_a_queue_not_yet_exhausted_means_no_mark_and_no_fill(self):
        ex = _executor(self.cfg("proportional"))
        o = _place(ex, _book(bids=[(100.0, 10.0)]), price=100.0, size=5.0)
        o.arrival_ts = 0.0
        self.assertEqual(decrease(ex, 6.0, 2.0), [])
        self.assertGreater(o.queue_ahead, 0.0)
        self.assertIsNone(o.front_ts)

    def test_works_with_the_power_model_too(self):
        ex, o = front_order(MakerConfig(cancel_model="power", cancel_power_n=1.1, fill_evidence="confirm_delta_snapshot"))
        self.assertEqual(decrease(ex, 2.0, 2.0), [])
        self.assertEqual(len(trade(ex, 1.0, 2.02)), 1)


class QueueEmptyThresholdTests(unittest.TestCase):
    def test_threshold_is_applied_exactly_and_inclusively(self):
        cases = (("lot", dict(queue_empty_lot=0.5), 5.0), ("frac_of_order", dict(queue_empty_frac=0.1), 5.0))     # both thresholds are 0.5
        for mode, kw, size in cases:
            for q, expect in ((0.5, 0.0), (0.4999999, 0.0), (0.5000001, 0.5000001), (1e-13, 0.0)):
                ex, o = front_order(MakerConfig(queue_empty_threshold=mode, **kw), size=size)
                o.queue_ahead = q
                ex._settle_queue(o)
                self.assertEqual(o.queue_ahead, expect, (mode, q))

    def test_absolute_never_changes_the_queue(self):
        ex, o = front_order(MakerConfig(queue_empty_threshold="absolute", queue_empty_lot=9.0, queue_empty_frac=0.9))
        for q in (1e-13, 0.3, 4.0):
            o.queue_ahead = q
            ex._settle_queue(o)
            self.assertEqual(o.queue_ahead, q)

    def test_frac_of_order_scales_with_the_order_size(self):
        for size, expect in ((1.0, 0.3), (5.0, 0.0)):
            ex, o = front_order(MakerConfig(queue_empty_threshold="frac_of_order", queue_empty_frac=0.1), size=size)
            o.queue_ahead = 0.3
            ex._settle_queue(o)
            self.assertEqual(o.queue_ahead, expect, size)

    def test_a_small_queue_left_after_a_cancel_counts_as_empty_so_the_next_decrease_fills(self):
        for model in MODELS:
            fills = {}
            for mode, kw in (("absolute", {}), ("lot", dict(queue_empty_lot=0.5)), ("frac_of_order", dict(queue_empty_frac=0.1))):
                ex = _executor(MakerConfig(cancel_model=model, queue_empty_threshold=mode, **kw))
                o = _place(ex, _book(bids=[(100.0, 5.3)]), price=100.0, size=5.0)
                o.arrival_ts = 0.0
                o.queue_ahead = 0.3                                    # 0.3 ahead of us in a displayed level of 5.3
                f = decrease(ex, 5.2, 2.0)                             # a cancel of 0.1
                fills[mode] = (round(sum(x.qty for x in f), 9), [x.cause for x in f], o.queue_ahead > 1e-12)
            self.assertEqual(fills["absolute"], (0.0, [], True), model)             # the queue is 0.29... > 1e-12: no fill
            for mode in ("lot", "frac_of_order"):
                self.assertEqual(fills[mode][2], False, (model, mode))              # the 0.29 / 0.2 left ahead counts as empty and is zeroed
                if model == "proportional":                                         # the cancel's share behind us is left over and fills now
                    self.assertGreater(fills[mode][0], 0.0, (model, mode))
                    self.assertEqual(fills[mode][1], ["delta_share"], (model, mode))
                else:                                                               # all_ahead charges the whole cancel to the queue: no leftover
                    self.assertEqual(fills[mode][0], 0.0, (model, mode))
        for mode, kw in (("absolute", {}), ("lot", dict(queue_empty_lot=0.5))):
            ex = _executor(MakerConfig(cancel_model="all_ahead", queue_empty_threshold=mode, **kw))
            o = _place(ex, _book(bids=[(100.0, 5.3)]), price=100.0, size=5.0)
            o.arrival_ts = 0.0
            o.queue_ahead = 0.3
            decrease(ex, 5.2, 2.0)
            f = decrease(ex, 5.0, 3.0)                                              # a second cancel of 0.2
            self.assertEqual([x.cause for x in f], ["delta_share"] if mode == "lot" else [], mode)

    def test_a_queue_at_placement_below_the_threshold_starts_empty_but_keeps_its_initial_record(self):
        ex = _executor(MakerConfig(queue_empty_threshold="lot", queue_empty_lot=0.5))
        o = _place(ex, _book(bids=[(100.0, 0.3)]), price=100.0, size=5.0)
        self.assertEqual((o.queue_ahead, o.initial_queue_ahead), (0.0, 0.3))
        ex = _executor(MakerConfig())
        o = _place(ex, _book(bids=[(100.0, 0.3)]), price=100.0, size=5.0)
        self.assertEqual(o.queue_ahead, 0.3)

    def test_frac_of_order_uses_the_original_size_not_the_remaining_size(self):
        ex, o = front_order(MakerConfig(queue_empty_threshold="frac_of_order", queue_empty_frac=0.1), size=5.0)      # threshold 0.5
        o.remaining = 1.0                                                                                        # partly filled
        o.queue_ahead = 0.4
        ex._settle_queue(o)
        self.assertEqual(o.queue_ahead, 0.0)

    def test_the_forgiven_queue_is_counted_in_the_diagnostics(self):
        ex, o = front_order(MakerConfig(queue_empty_threshold="lot", queue_empty_lot=0.5))
        o.queue_ahead = 0.25
        ex._settle_queue(o)
        self.assertEqual((ex.diag["queue_empty_forgiven"], ex.diag["queue_empty_events"]), (0.25, 1.0))

    def test_works_with_the_power_model_and_with_evidence_modes(self):
        ex = _executor(MakerConfig(cancel_model="power", cancel_power_n=0.9, queue_empty_threshold="lot", queue_empty_lot=0.5,
                                   fill_evidence="confirm_delta_snapshot"))
        o = _place(ex, _book(bids=[(100.0, 5.3)]), price=100.0, size=5.0)
        o.arrival_ts = 0.0
        o.queue_ahead = 0.3
        self.assertEqual(decrease(ex, 5.2, 2.0), [])               # the queue is emptied by the threshold; the delta_share fill is gated
        self.assertEqual((o.queue_ahead, o.front_ts), (0.0, 2.0))
        self.assertEqual([x.cause for x in trade(ex, 0.4, 2.02)], ["trade_after_front"])


class VanishClearsQueueTests(unittest.TestCase):
    """A trade first burns 2.0 of the 5.0 ahead; the level (8.0 displayed: 5.0 ahead of us, 3.0 behind) then vanishes. The trade is netted
    against the vanish, so the proportional drain leaves a residual queue and blocks the fill unless vanish_clears_queue is set."""
    def run_case(self, **kw):
        ex = _executor(MakerConfig(cancel_model="proportional", **kw))
        o = _place(ex, _book(bids=[(100.0, 5.0)]), price=100.0, size=5.0)
        o.arrival_ts = 0.0
        ex.on_delta("bybit", "bid", 100.0, 8.0, 1.5, TICK, 0.0)
        trade(ex, 2.0, 1.99)
        return ex, o, decrease(ex, 0.0, 2.0)

    def test_default_leaves_a_residual_queue_and_no_fill(self):
        ex, o, f = self.run_case()
        self.assertEqual(f, [])
        self.assertGreater(o.queue_ahead, 0.5)

    def test_flag_clears_the_queue_and_fills_with_the_vanish_cause(self):
        ex, o, f = self.run_case(vanish_clears_queue=True)
        self.assertEqual(o.queue_ahead, 0.0)
        self.assertEqual([x.cause for x in f], ["delta_vanish"])
        self.assertGreater(f[0].qty, 0.0)

    def test_flag_changes_nothing_for_a_partial_decrease(self):
        a = front_order(MakerConfig(cancel_model="proportional"))
        b = front_order(MakerConfig(cancel_model="proportional", vanish_clears_queue=True))
        self.assertEqual([(x.qty, x.cause) for x in decrease(a[0], 2.0, 2.0)], [(x.qty, x.cause) for x in decrease(b[0], 2.0, 2.0)])

    def test_gated_modes_without_the_flag_keep_the_residual_queue_like_off(self):
        _, o_off, f_off = self.run_case()
        for mode in ("confirm_delta_snapshot", "confirm_delta"):
            _, o, f = self.run_case(fill_evidence=mode)
            self.assertEqual(f, [], mode)
            self.assertAlmostEqual(o.queue_ahead, o_off.queue_ahead, places=12)
            self.assertIsNone(o.front_ts)

    def test_flag_works_with_the_targeted_evidence_mode_and_leaves_default_digests_alone(self):
        ex, o, f = self.run_case(vanish_clears_queue=True, fill_evidence="confirm_delta_snapshot")
        self.assertEqual([x.cause for x in f], ["delta_vanish"])
        for name, kw in OffIsIdenticalTests.CONFIGS.items():
            self.assertEqual(stream_digest(MakerConfig(**kw, vanish_clears_queue=False)), GOLDEN[name], name)
        self.assertNotEqual(stream_digest(MakerConfig(cancel_model="proportional", vanish_clears_queue=True)), GOLDEN["proportional"])


class BitIdenticalTests(unittest.TestCase):
    def test_off_and_absolute_match_the_golden_digests_even_with_the_new_parameters_set(self):
        for name, kw in OffIsIdenticalTests.CONFIGS.items():
            self.assertEqual(stream_digest(MakerConfig(**kw, queue_empty_threshold="absolute", queue_empty_lot=3.0, queue_empty_frac=0.9)),
                             GOLDEN[name], name)

    def test_the_new_settings_really_change_the_random_stream_and_are_deterministic(self):
        base = stream_digest(MakerConfig(cancel_model="proportional"))
        self.assertEqual(base, GOLDEN["proportional"])
        for kw in (dict(fill_evidence="confirm_delta_snapshot"), dict(fill_evidence="confirm_delta"),
                   dict(queue_empty_threshold="lot", queue_empty_lot=1.0), dict(queue_empty_threshold="frac_of_order", queue_empty_frac=0.2)):
            d1 = stream_digest(MakerConfig(cancel_model="proportional", **kw))
            self.assertNotEqual(d1, base, kw)
            self.assertEqual(d1, stream_digest(MakerConfig(cancel_model="proportional", **kw)), kw)

    def test_confirm_delta_differs_from_confirm_delta_snapshot_on_the_stream(self):
        a = stream_digest(MakerConfig(cancel_model="proportional", fill_evidence="confirm_delta"))
        b = stream_digest(MakerConfig(cancel_model="proportional", fill_evidence="confirm_delta_snapshot"))
        self.assertNotEqual(a, b)

    def test_gated_fills_only_come_from_trades_or_ungated_causes_on_the_stream(self):
        log = stream_run(MakerConfig(cancel_model="proportional", fill_evidence="confirm_delta_snapshot"), n_events=3000)[0]
        causes = {x[3] for x in log}
        self.assertNotIn("delta_share", causes)
        self.assertNotIn("snapshot", causes)
        self.assertIn("trade_after_front", causes)


if __name__ == "__main__":
    unittest.main()
