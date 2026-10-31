"""MakerConfig.fill_evidence: an unexplained depth decrease or snapshot shrink must not fill the whole order on its own.

Modes: "off" (current behaviour, default), "trade_confirm", "fractional", "trade_confirm+fractional".
Setup used throughout: a buy order of size 5 at 100.0, already at the FRONT of its queue (queue_ahead 0) behind a displayed
level of 5. A depth decrease of that level from 5 to 2 is then an unexplained decrease of 3 with nothing ahead of us: in the
current model it fills min(5, 3 x delta_fill_share) = 0.45 at once."""
from __future__ import annotations

import hashlib
import random
import unittest

from new_backtest.fill_engine import MakerConfig
from new_backtest.models import Side
from test_fill_engine_queue import _book, _executor, _place

TICK = 1.0
MODELS = ("proportional", "all_ahead")


def front_order(config, level=5.0, size=5.0):
    ex = _executor(config)
    o = _place(ex, _book(bids=[(100.0, level)]), price=100.0, size=size)
    o.arrival_ts = 0.0
    o.queue_ahead = 0.0
    o.observed_level_size = o.level_now = level
    return ex, o


def decrease(ex, new_size, t):
    return ex.on_delta("bybit", "bid", 100.0, new_size, t, TICK, 0.0)


def trade(ex, volume, t, price=100.0):
    return ex.on_trade("bybit", price, volume, -1, t, 0.0)


class OffModeTests(unittest.TestCase):
    def test_default_is_off_and_validated(self):
        self.assertEqual(MakerConfig().fill_evidence, "off")
        for bad in ({"fill_evidence": "bogus"}, {"evidence_window_ms": -1.0}, {"evidence_fill_fraction": 0.0},
                    {"evidence_fill_fraction": 1.5}):
            with self.assertRaises(ValueError):
                MakerConfig(**bad)

    def test_off_fills_on_an_unexplained_decrease(self):
        for model in MODELS:
            ex, o = front_order(MakerConfig(cancel_model=model))
            f = decrease(ex, 2.0, 2.0)
            self.assertEqual(len(f), 1, model)
            self.assertAlmostEqual(f[0].qty, 0.45, places=9)
            self.assertEqual(f[0].cause, "delta_share")
            self.assertIsNone(o.front_ts)

    def test_off_ignores_the_evidence_parameters(self):
        a, _ = front_order(MakerConfig(cancel_model="proportional"))
        b, _ = front_order(MakerConfig(cancel_model="proportional", evidence_window_ms=5.0, evidence_fill_fraction=0.9))
        self.assertEqual([(f.qty, f.cause) for f in decrease(a, 2.0, 2.0)], [(f.qty, f.cause) for f in decrease(b, 2.0, 2.0)])


class TradeConfirmTests(unittest.TestCase):
    def cfg(self, model, **kw):
        return MakerConfig(cancel_model=model, fill_evidence="trade_confirm", **kw)

    def test_unexplained_decrease_alone_does_not_fill_and_marks_the_front(self):
        for model in MODELS:
            ex, o = front_order(self.cfg(model))
            self.assertEqual(decrease(ex, 2.0, 2.0), [], model)
            self.assertEqual(o.filled_qty, 0.0)
            self.assertEqual(o.front_ts, 2.0)
            self.assertAlmostEqual(o.front_decrease, 3.0, places=9)
            self.assertEqual(o.queue_ahead, 0.0)                       # we stay at the front

    def test_trade_inside_the_window_fills_up_to_the_trade_size_even_when_dedupe_would_net_it(self):
        for model in MODELS:
            # delta_fill_share 0 isolates the de-duplication: the trade 30 ms after the decrease is netted against it
            plain, _ = front_order(MakerConfig(cancel_model=model, delta_fill_share=0.0))
            decrease(plain, 2.0, 2.0)
            self.assertEqual(trade(plain, 1.2, 2.03), [], f"{model}: the control is netted away")
            ex, o = front_order(self.cfg(model))
            decrease(ex, 2.0, 2.0)
            f = trade(ex, 1.2, 2.03)
            self.assertEqual([(round(x.qty, 9), x.cause) for x in f], [(1.2, "trade_after_front")], model)
            self.assertAlmostEqual(o.remaining, 3.8, places=9)
            self.assertIsNone(o.front_ts)                              # the evidence was used

    def test_fill_is_capped_at_the_size_of_the_trade_and_the_order(self):
        ex, o = front_order(self.cfg("proportional"), size=1.0)
        decrease(ex, 2.0, 2.0)
        f = trade(ex, 7.0, 2.01)
        self.assertEqual([(x.qty, x.cause) for x in f], [(1.0, "trade_after_front")])
        self.assertFalse(o.is_live)
        ex, o = front_order(self.cfg("proportional", trade_fill_share=0.5))
        decrease(ex, 2.0, 2.0)
        self.assertAlmostEqual(trade(ex, 2.0, 2.01)[0].qty, 1.0, places=9)    # the trade fill share still applies

    def test_trade_after_the_window_is_not_a_confirmation(self):
        # window 10 ms; a trade 30 ms after the decrease is outside it but still inside the 50 ms de-duplication window,
        # so it is netted against the decrease and fills nothing
        ex, o = front_order(self.cfg("proportional", evidence_window_ms=10.0))
        decrease(ex, 2.0, 2.0)
        self.assertEqual(trade(ex, 1.2, 2.03), [])
        self.assertEqual(o.filled_qty, 0.0)
        # long after both windows the order is still at the front, and an ordinary trade fills it by the ordinary rule
        f = trade(ex, 1.2, 5.0)
        self.assertEqual([(round(x.qty, 9), x.cause) for x in f], [(1.2, "trade")])

    def test_trade_stamped_before_the_decrease_does_not_confirm(self):
        ex, o = front_order(self.cfg("proportional"))
        decrease(ex, 2.0, 2.0)
        f = ex.on_trade("bybit", 100.0, 1.0, -1, 2.05, 0.0, event_ts=1.99)       # executed 10 ms BEFORE the decrease
        self.assertEqual([x.cause for x in f], [])
        self.assertIsNotNone(o.front_ts)

    def test_window_zero_only_confirms_a_trade_with_the_same_stamp(self):
        ex, _ = front_order(self.cfg("proportional", evidence_window_ms=0.0))
        decrease(ex, 2.0, 2.0)
        self.assertEqual(trade(ex, 1.0, 2.001), [])
        ex, _ = front_order(self.cfg("proportional", evidence_window_ms=0.0))
        decrease(ex, 2.0, 2.0)
        self.assertEqual([x.cause for x in trade(ex, 1.0, 2.0)], ["trade_after_front"])

    def test_trade_at_another_price_or_on_the_other_side_never_confirms(self):
        ex, o = front_order(self.cfg("proportional"))
        decrease(ex, 2.0, 2.0)
        self.assertEqual(ex.on_trade("bybit", 100.0, 1.0, +1, 2.01, 0.0), [])    # a buy aggressor does not hit a bid
        self.assertEqual(o.filled_qty, 0.0)
        self.assertIsNotNone(o.front_ts)
        # a sell below our bid reaches us by the ordinary rule, but it is not the confirming execution at our price
        f = trade(ex, 1.0, 2.01, price=99.0)
        self.assertEqual([x.cause for x in f], ["trade"])

    def test_vanished_level_moves_to_the_front_without_a_fill_then_a_trade_fills(self):
        for model in MODELS:
            ex = _executor(self.cfg(model))
            o = _place(ex, _book(bids=[(100.0, 5.0)]), price=100.0, size=5.0)
            o.arrival_ts = 0.0
            ex.on_delta("bybit", "bid", 100.0, 8.0, 1.5, TICK, 0.0)          # 3 more join behind us
            self.assertGreater(o.queue_ahead, 0.0)
            self.assertEqual(decrease(ex, 0.0, 2.0), [], model)
            self.assertEqual((o.queue_ahead, o.front_ts, o.filled_qty), (0.0, 2.0, 0.0))
            f = trade(ex, 0.8, 2.02)
            self.assertEqual([(round(x.qty, 9), x.cause) for x in f], [(0.8, "trade_after_front")], model)

    def test_vanished_level_fills_on_its_own_in_the_current_model(self):
        ex = _executor(MakerConfig(cancel_model="proportional"))
        o = _place(ex, _book(bids=[(100.0, 5.0)]), price=100.0, size=5.0)
        o.arrival_ts = 0.0
        ex.on_delta("bybit", "bid", 100.0, 8.0, 1.5, TICK, 0.0)
        f = decrease(ex, 0.0, 2.0)
        self.assertEqual([(round(x.qty, 9), x.cause) for x in f], [(0.45, "delta_vanish")])

    def test_snapshot_shrink_marks_the_front_instead_of_filling(self):
        for mode, filled in (("off", 0.15), ("trade_confirm", 0.0)):
            ex, o = front_order(MakerConfig(cancel_model="proportional", fill_evidence=mode))
            f = ex.on_snapshot("bybit", _book(bids=[(100.0, 2.0)]), 2.0, TICK, 0.0)
            self.assertAlmostEqual(sum(x.qty for x in f), filled, places=9, msg=mode)
            self.assertEqual(o.front_ts is not None, mode == "trade_confirm")

    def test_queue_not_yet_exhausted_means_no_mark_and_no_fill(self):
        ex = _executor(self.cfg("proportional"))
        o = _place(ex, _book(bids=[(100.0, 10.0)]), price=100.0, size=5.0)
        o.arrival_ts = 0.0
        self.assertEqual(decrease(ex, 6.0, 2.0), [])
        self.assertGreater(o.queue_ahead, 0.0)
        self.assertIsNone(o.front_ts)

    def test_a_later_decrease_rearms_the_window(self):
        ex, o = front_order(self.cfg("proportional", evidence_window_ms=100.0))
        decrease(ex, 2.0, 2.0)
        decrease(ex, 1.0, 3.0)
        self.assertEqual(o.front_ts, 3.0)
        self.assertEqual(len(trade(ex, 0.5, 3.04)), 1)

    def test_through_print_still_fills_by_its_own_rule(self):
        ex, o = front_order(self.cfg("proportional", fill_through_prints=True))
        decrease(ex, 2.0, 2.0)
        f = trade(ex, 1.0, 2.01, price=99.0)
        self.assertEqual([x.cause for x in f], ["trade_through"])


class FractionalTests(unittest.TestCase):
    def test_fill_is_capped_at_fraction_times_the_decrease(self):
        for model in MODELS:
            for frac, want in ((0.05, 0.15), (0.15, 0.45), (0.3, 0.45)):      # decrease 3.0; the current rule gives 0.45
                ex, o = front_order(MakerConfig(cancel_model=model, fill_evidence="fractional", evidence_fill_fraction=frac))
                f = decrease(ex, 2.0, 2.0)
                self.assertAlmostEqual(sum(x.qty for x in f), want, places=9, msg=f"{model} {frac}")
                self.assertIsNone(o.front_ts)

    def test_fraction_cannot_exceed_the_remaining_size(self):
        ex, o = front_order(MakerConfig(cancel_model="proportional", fill_evidence="fractional", evidence_fill_fraction=1.0,
                                        delta_fill_share=1.0), size=0.5)
        self.assertAlmostEqual(sum(x.qty for x in decrease(ex, 2.0, 2.0)), 0.5, places=9)

    def test_vanished_level_does_not_fill_in_fractional_mode(self):
        for model in MODELS:
            ex = _executor(MakerConfig(cancel_model=model, fill_evidence="fractional"))
            o = _place(ex, _book(bids=[(100.0, 5.0)]), price=100.0, size=5.0)
            o.arrival_ts = 0.0
            ex.on_delta("bybit", "bid", 100.0, 8.0, 1.5, TICK, 0.0)          # 3 more join behind us, so the vanish has a surplus
            self.assertEqual(decrease(ex, 0.0, 2.0), [], model)
            self.assertEqual((o.queue_ahead, o.filled_qty), (0.0, 0.0), model)
            self.assertIsNotNone(o.front_ts)

    def test_vanished_level_puts_a_stressed_queue_at_the_front(self):
        for model in MODELS:
            for mode in ("trade_confirm", "fractional", "trade_confirm+fractional"):
                ex = _executor(MakerConfig(cancel_model=model, fill_evidence=mode, queue_stress_multiplier=2.0))
                o = _place(ex, _book(bids=[(100.0, 5.0)]), price=100.0, size=5.0)
                o.arrival_ts = 0.0
                self.assertEqual(o.queue_ahead, 10.0)                       # stress: twice the displayed size ahead of us
                self.assertEqual(decrease(ex, 0.0, 2.0), [], (model, mode))
                self.assertEqual((o.queue_ahead, o.front_ts), (0.0, 2.0), (model, mode))      # the level is gone: nothing is ahead

    def test_combined_mode_caps_the_confirming_fill(self):
        ex, o = front_order(MakerConfig(cancel_model="proportional", fill_evidence="trade_confirm+fractional", evidence_fill_fraction=0.1))
        self.assertEqual(decrease(ex, 2.0, 2.0), [])
        f = trade(ex, 5.0, 2.02)
        self.assertEqual([x.cause for x in f], ["trade_after_front"])
        self.assertAlmostEqual(f[0].qty, 0.3, places=9)                  # 0.1 x the decrease of 3


# ── The power model is continuous at n = 1 (Task 2: no special-case artefact around the proportional path) ───────────────
class PowerContinuityTests(unittest.TestCase):
    def drained(self, model, n, ahead, level, shrink):
        ex = _executor(MakerConfig(cancel_model=model, cancel_power_n=n))
        o = _place(ex, _book(bids=[(100.0, max(level, 1e-9))]), price=100.0, size=5.0)
        o.arrival_ts = 0.0
        o.queue_ahead, o.level_now, o.observed_level_size = ahead, level, level
        ex.on_delta("bybit", "bid", 100.0, level - shrink, 2.0, TICK, 0.0)
        return ahead - o.queue_ahead

    def test_share_ahead_is_continuous_in_n_at_one(self):
        rng = random.Random(3)
        for _ in range(400):
            level = rng.uniform(1.0, 50.0)
            ahead = rng.uniform(0.0, 1.3 * level)                  # includes ahead > level (queue stress / stale feed)
            shrink = rng.uniform(0.05, level)
            ref = self.drained("proportional", 1.0, ahead, level, shrink)
            for n in (0.999, 1.001):
                self.assertAlmostEqual(self.drained("power", n, ahead, level, shrink), ref, delta=0.01 * max(shrink, 1e-6), msg=(ahead, level, n))
            self.assertAlmostEqual(self.drained("power", 1.0, ahead, level, shrink), ref, places=12)

    def test_edge_cases_nothing_ahead_and_nothing_behind(self):
        for n in (0.5, 1.0, 1.5):
            self.assertEqual(self.drained("power", n, 0.0, 10.0, 4.0), 0.0)                       # nothing ahead: nothing to advance
            self.assertAlmostEqual(self.drained("power", n, 10.0, 10.0, 4.0), 4.0, places=12)     # nothing behind: the whole cancel is ahead
            self.assertAlmostEqual(self.drained("power", n, 12.0, 10.0, 4.0), 4.0, places=12)     # ahead > level: behind clipped at 0, not negative

    def steps_until_empty(self, n, cut=0.05, cap=4000):
        """Level of 100 with 20 ahead of us; each step it loses `cut` to cancels and joins behind us restore it."""
        ex = _executor(MakerConfig(cancel_model="power", cancel_power_n=n))
        o = _place(ex, _book(bids=[(100.0, 100.0)]), price=100.0, size=5.0)
        o.arrival_ts = 0.0
        o.queue_ahead, o.level_now, o.observed_level_size = 20.0, 100.0, 100.0
        for k in range(cap):
            t = 2.0 + 0.01 * k
            ex.on_delta("bybit", "bid", 100.0, 100.0 * (1 - cut), t, TICK, 0.0)
            ex.on_delta("bybit", "bid", 100.0, 100.0, t + 0.001, TICK, 0.0)
            if o.queue_ahead <= 1e-12:
                return k + 1
        return cap

    def test_cancels_alone_empty_the_queue_ever_slower_as_n_rises(self):
        # Why the hit-rate curve is steep around n = 1: a queue that shrinks by a constant share per cancel decays geometrically and
        # only counts as empty once it is below 1e-12, which takes ~600 cancels at n = 1 but 2.6x as many at n = 1.05 and half
        # as many at n = 0.95. The family is continuous, the exhaustion-by-cancels threshold is what makes it steep.
        steps = {n: self.steps_until_empty(n) for n in (0.95, 1.0, 1.05)}
        self.assertLess(steps[0.95], 0.6 * steps[1.0])
        self.assertGreater(steps[1.05], 2.0 * steps[1.0])

    def test_small_queue_ahead_is_very_sensitive_to_n(self):
        # share ~ (ahead/behind)**n when ahead << behind: the amplification that makes the hit-rate curve steep away from n = 1
        a, b = 1.0, 99.0
        shares = {n: self.drained("power", n, a, a + b, 1.0) for n in (0.9, 1.0, 1.1)}
        self.assertGreater(shares[0.9], 1.5 * shares[1.0])
        self.assertLess(shares[1.1], 0.7 * shares[1.0])


# ── "off" is the current behaviour, bit for bit ──────────────────────────────────────────────────────────────────────
OLD_DIAG_KEYS = (
    "trade_events", "trade_volume_observed", "trade_volume_credited_to_depth", "trade_volume_net", "trade_volume_to_queue_ahead",
    "trade_volume_to_fills", "pre_arrival_event_drains", "pre_arrival_queue_drained", "delta_shrink_events", "delta_shrink_total",
    "delta_shrink_explained_by_trades", "delta_queue_drained", "snapshot_shrink_total", "snapshot_shrink_explained_by_trades",
    "snapshot_queue_drained", "fills_from_trade", "fills_from_delta", "fills_from_snapshot", "fills_from_through_print")


def stream_run(config: MakerConfig, seed: int = 11, n_events: int = 3000):
    """Random depth/trade/snapshot stream over three orders. Returns (fills, pre-existing diagnostics, order states)."""
    rng = random.Random(seed)
    ex = _executor(config)
    book = _book(bids=[(100.0, 8.0), (99.0, 20.0)], asks=[(101.0, 8.0), (102.0, 20.0)])
    specs = ((100.0, Side.BID, 4.0), (100.0, Side.BID, 6.0), (101.0, Side.ASK, 5.0))
    orders = []
    for price, side, size in specs:
        o = _place(ex, book, side=side, price=price, size=size)
        o.arrival_ts = 0.0
        orders.append(o)
    levels = {("bid", 100.0): 8.0, ("ask", 101.0): 8.0}
    log, t = [], 1.0
    for _ in range(n_events):
        t += rng.uniform(0.001, 0.25)
        r = rng.random()
        if r < 0.45:
            key = rng.choice(list(levels))
            levels[key] = max(0.0, levels[key] + rng.uniform(-6, 5))
            fills = ex.on_delta("bybit", key[0], key[1], levels[key], t, TICK, 0.0, event_ts=t - rng.choice((0.0, 0.01)))
        elif r < 0.85:
            price = rng.choice((100.0, 100.0, 99.0, 101.0, 102.0))
            fills = ex.on_trade("bybit", price, rng.uniform(0.01, 3.0), rng.choice((-1, 1)), t, 0.0, event_ts=t - rng.choice((0.0, 0.02)))
        else:
            fills = ex.on_snapshot("bybit", _book(bids=[(100.0, max(0.0, levels[("bid", 100.0)] + rng.uniform(-1, 1)))],
                                                  asks=[(101.0, max(0.0, levels[("ask", 101.0)] + rng.uniform(-1, 1)))]), t, TICK, 0.0)
        log.extend((round(f.ts, 9), f.order_id, round(f.qty, 12), f.cause) for f in fills)
        for i, o in enumerate(orders):                       # keep three orders resting: replace any that completed
            if not o.is_live:
                price, side, size = specs[i]
                orders[i] = _place(ex, _book(bids=[(100.0, 8.0), (99.0, 20.0)], asks=[(101.0, 8.0), (102.0, 20.0)]), side=side,
                                   price=price, size=size, now=t)
                orders[i].arrival_ts = 0.0
    diag = tuple((k, round(ex.diag[k], 9)) for k in OLD_DIAG_KEYS)
    return log, diag, [(o.queue_ahead, o.filled_qty) for o in orders]


def stream_digest(config: MakerConfig, **kw) -> str:
    return hashlib.sha256(repr(stream_run(config, **kw)).encode()).hexdigest()


# Golden values computed with the engine as it was BEFORE fill_evidence existed (src snapshot of 2026-10-30 04:5x).
GOLDEN = {
    "none": "8f1cf1b4b7d4c3ad8a05277a4cc49023a7c5acfa006b5583ac8e494c98d8cbd8",
    "all_ahead": "8f1cf1b4b7d4c3ad8a05277a4cc49023a7c5acfa006b5583ac8e494c98d8cbd8",     # unset behaves as all_ahead
    "proportional": "7d9b854f0b3976cc5f711cbbfcf7ba5d12220855be6c524782d41781afab3672",
    "power13": "48481f93e6bca211a71593f02f230a22ff80c367ccc5afc425847937db4fdc0e",
    "prop_through": "c992218269c1050da2ee471735950127807b4835a215e3ec149528758f99a5f8",
    "aa_legacy": "ae24d05a4f7461d573c550d54e6b96fc039fdd713eb5fed685b77efc9f4f5582",
}


class OffIsIdenticalTests(unittest.TestCase):
    CONFIGS = {
        "none": {"cancel_model": "all_ahead"}, "all_ahead": {"cancel_model": "all_ahead"}, "proportional": {"cancel_model": "proportional"},
        "power13": {"cancel_model": "power", "cancel_power_n": 1.3},
        "prop_through": {"cancel_model": "proportional", "fill_through_prints": True},
        "aa_legacy": {"cancel_model": "all_ahead", "snapshot_trade_explain": False, "dedupe_trade_depth": False},
    }

    def test_off_matches_the_engine_before_the_feature_existed(self):
        for name, kw in self.CONFIGS.items():
            self.assertEqual(stream_digest(MakerConfig(**kw)), GOLDEN[name], name)

    def test_the_default_config_is_proportional_with_evidence_off_and_matches_its_golden_digest(self):
        self.assertEqual(MakerConfig().fill_evidence, "off")
        self.assertEqual(stream_digest(MakerConfig()), GOLDEN["proportional"])
        self.assertEqual(stream_digest(MakerConfig(cancel_model=None)), GOLDEN["proportional"])

    def test_off_ignores_evidence_parameters_on_the_random_stream(self):
        for name, kw in self.CONFIGS.items():
            self.assertEqual(stream_digest(MakerConfig(**kw, evidence_window_ms=3.0, evidence_fill_fraction=0.9)), GOLDEN[name], name)

    def test_the_stream_is_not_degenerate(self):
        # fills come from trades, depth decreases and snapshots, so the golden comparison above is meaningful
        causes = {c for kw in ({"cancel_model": "proportional"}, {"cancel_model": "all_ahead"}, {"cancel_model": "all_ahead", "n": 1})
                  for c in [x[3] for x in stream_run(MakerConfig(**{k: v for k, v in kw.items() if k != "n"}), n_events=3000)[0]]}
        self.assertTrue({"trade", "delta_share", "snapshot"} <= causes, causes)
        self.assertGreater(len(set(GOLDEN.values())), 3)


if __name__ == "__main__":
    unittest.main()
