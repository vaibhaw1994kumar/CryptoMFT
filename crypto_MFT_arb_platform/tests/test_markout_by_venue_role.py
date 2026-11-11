"""MarkoutTracker keeps the pooled markout_summary exactly as before and adds a (venue, maker|taker) split of the same numbers."""
from __future__ import annotations

import unittest

from new_backtest.latency_model import MarkoutTracker


class MarkoutByVenueRoleTests(unittest.TestCase):
    def _run(self, with_roles: bool):
        mt = MarkoutTracker((1.0, 5.0))
        mt.record_fill(10.0, "bybit", "buy", 100.0, 1.0, "maker" if with_roles else "")
        mt.record_fill(10.1, "binance", "sell", 100.2, 2.0, "taker" if with_roles else "")
        mt.resolve(20.0, {"bybit": 100.1, "binance": 100.0})
        return mt

    def test_pooled_summary_is_unchanged_by_the_split(self):
        self.assertEqual(self._run(True).summary(), self._run(False).summary())

    def test_split_matches_the_hand_computed_numbers(self):
        s = self._run(True).summary_by_venue_role()
        self.assertEqual(sorted(s), ["binance/taker", "bybit/maker"])
        self.assertAlmostEqual(s["bybit/maker"]["1s"]["qty_weighted_markout_bps"], 10.0)      # buy at 100, mid 100.1 -> +10 bps
        self.assertAlmostEqual(s["binance/taker"]["1s"]["qty_weighted_markout_bps"], 19.96007984, places=5)   # sell at 100.2, mid 100.0
        self.assertEqual(s["bybit/maker"]["1s"]["count"], 1.0)

    def test_pooled_is_the_qty_weighted_mean_of_the_split(self):
        mt = self._run(True)
        p, s = mt.summary()["1s"], mt.summary_by_venue_role()
        q = 1.0 * s["bybit/maker"]["1s"]["qty_weighted_markout_bps"] + 2.0 * s["binance/taker"]["1s"]["qty_weighted_markout_bps"]
        self.assertAlmostEqual(p["qty_weighted_markout_bps"], q / 3.0)
        self.assertEqual(p["count"], 2.0)

    def test_no_roles_no_split(self):
        self.assertEqual(self._run(False).summary_by_venue_role(), {})


class MarkoutDecomposeTests(unittest.TestCase):
    """CR-B: MarkoutTracker(decompose=True) splits a fill into captured / adverse / fee at exact horizons; decompose=False is untouched."""

    def _tracker(self):
        mt = MarkoutTracker((1.0, 5.0), decompose=True)
        # buy 2 at 99.98 with the mid at 100.00 (captured 2 bps), rebate 0.01 on a notional of 199.96 (+0.5 bps)
        mt.record_fill(10.0, "bybit", "buy", 99.98, 2.0, "maker", mid_at_fill=100.0, fee=-0.01, tags={"pair": "ADAUSDT"})
        # sell 1 at 100.03, mid 100.00 (captured 3 bps), fee paid 0.02 on 100.03 (-2 bps)
        mt.record_fill(10.5, "bybit", "sell", 100.03, 1.0, "maker", mid_at_fill=100.0, fee=0.02, tags={"pair": "MNTUSDT"})
        return mt

    def test_default_off_and_the_old_path_is_the_one_in_use(self):
        mt = MarkoutTracker((1.0,))
        self.assertFalse(mt.decompose)
        mt.record_fill(1.0, "bybit", "buy", 100.0, 1.0)
        mt.resolve(5.0, {"bybit": 100.1})
        self.assertAlmostEqual(mt.summary()["1s"]["qty_weighted_markout_bps"], 10.0)
        with self.assertRaises(RuntimeError):
            mt.resolve_asof(5.0, {"bybit": 100.1})
        with self.assertRaises(ValueError):
            mt.record_fill(2.0, "bybit", "buy", 100.0, 1.0, mid_at_fill=100.0)

    def test_the_old_resolve_refuses_when_decomposing(self):
        with self.assertRaises(RuntimeError):
            self._tracker().resolve(20.0, {"bybit": 100.0})

    def test_a_fill_needs_the_mid_at_the_fill(self):
        mt = MarkoutTracker((1.0,), decompose=True)
        with self.assertRaises(ValueError):
            mt.record_fill(1.0, "bybit", "buy", 100.0, 1.0)

    def test_rows_match_the_hand_computed_numbers(self):
        mt = self._tracker()
        mt.resolve_asof(10.8, {"bybit": 100.0})            # nothing is due yet: the first target is 11.0
        self.assertEqual(mt.rows, [])
        mt.resolve_asof(11.2, {"bybit": 100.02})           # the 1 s markout of fill 1 (target 11.0) reads the mid valid at 11.0
        r = mt.rows[0]
        self.assertEqual((r["horizon_s"], r["side"], r["mid_at_fill"], r["mid_at_h"], r["pair"], r["at_end"]), (1.0, "buy", 100.0, 100.02, "ADAUSDT", False))
        self.assertAlmostEqual(r["captured_bps"], 2.0)                    # (100.00 - 99.98) / 100 * 1e4
        self.assertAlmostEqual(r["adverse_bps"], 2.0)                     # (100.02 - 100.00) / 100 * 1e4
        self.assertAlmostEqual(r["fee_bps"], 0.01 / 199.96 * 1e4)         # a rebate is positive
        self.assertAlmostEqual(r["net_bps"], r["captured_bps"] + r["adverse_bps"] + r["fee_bps"])
        mt.resolve_asof(11.6, {"bybit": 99.97})            # fill 2, sell: target 11.5; mid fell 3 bps = good for a seller
        r = mt.rows[1]
        self.assertAlmostEqual(r["captured_bps"], 3.0)
        self.assertAlmostEqual(r["adverse_bps"], 3.0)
        self.assertAlmostEqual(r["fee_bps"], -0.02 / 100.03 * 1e4)

    def test_a_target_equal_to_now_waits_for_the_event_at_now(self):
        mt = self._tracker()
        mt.resolve_asof(11.0, {"bybit": 100.0})            # strictly before: the target at 11.0 is not resolved yet
        self.assertEqual(mt.rows, [])

    def test_finalize_flags_the_rest_at_end_and_the_summary_leaves_them_out(self):
        mt = self._tracker()
        mt.resolve_asof(12.0, {"bybit": 100.0})
        mt.resolve_asof(12.0, {"bybit": 100.0}, finalize=True)
        self.assertEqual(len(mt.rows), 4)
        self.assertEqual(sorted(r["at_end"] for r in mt.rows), [False, False, True, True])
        self.assertEqual(mt.decomposed_summary()["all"]["1s"]["n"], 2)
        self.assertNotIn("5s", mt.decomposed_summary()["all"])

    def test_summary_by_tag_and_notional_weighting(self):
        mt = self._tracker()
        mt.resolve_asof(30.0, {"bybit": 100.0})
        by = mt.decomposed_summary(by=("pair",))
        self.assertEqual(sorted(by), ["ADAUSDT", "MNTUSDT"])
        self.assertAlmostEqual(by["ADAUSDT"]["1s"]["captured_bps"], 2.0)
        allr = mt.decomposed_summary()["all"]["1s"]
        self.assertEqual(allr["n"], 2)
        self.assertAlmostEqual(allr["captured_bps"], 2.5)
        w1, w2 = 99.98 * 2.0, 100.03 * 1.0
        c1, c2 = 2.0 + 0.01 / 199.96 * 1e4, 3.0 - 0.02 / 100.03 * 1e4
        self.assertAlmostEqual(allr["notional_weighted_net_bps"], (c1 * w1 + c2 * w2) / (w1 + w2))
        self.assertAlmostEqual(allr["net_bps"], allr["captured_bps"] + allr["adverse_bps"] + allr["fee_bps"])

    def test_a_missing_mid_skips_the_row_like_the_old_path(self):
        mt = self._tracker()
        mt.resolve_asof(30.0, {})
        self.assertEqual(mt.rows, [])


def _fills(n=2000, seed=7):
    import random
    rnd, t, out = random.Random(seed), 0.0, []
    for i in range(n):
        t += rnd.random() * 0.7
        out.append((t, rnd.choice(["bybit", "binance"]), rnd.choice(["buy", "sell"]), 100 + rnd.random() * 2, rnd.random() * 3 + 0.01,
                    rnd.choice(["maker", "taker", ""]), 100 + rnd.random() * 2, rnd.random() * 0.05 - 0.02, {"pair": rnd.choice("AB"), "k": i % 3}))
    return out


def _drive(tr, decompose):
    fills, mid = _fills(), {"bybit": 100.0, "binance": 100.0}
    for ts, venue, side, price, qty, role, m0, fee, tags in fills:
        tr.resolve_asof(ts, mid) if decompose else tr.resolve(ts, mid)
        mid = {"bybit": 100 + ts * 7 % 2.0, "binance": 100 + ts * 3 % 2.0}
        if decompose:
            tr.record_fill(ts, venue, side, price, qty, role, mid_at_fill=m0, fee=fee, tags=tags)
        else:
            tr.record_fill(ts, venue, side, price, qty, role)
    end = fills[-1][0] + 1
    tr.resolve_asof(end, mid, finalize=True) if decompose else tr.resolve(end, mid, finalize=True)


class MarkoutDecomposeParityTests(unittest.TestCase):
    MM_FILE = "/Users/vaibhav/Documents/mm_wt/Newproject/crypto_MFT_arb_platform/src/strategies/mm/markout.py"

    def test_decompose_false_gives_the_numbers_the_tracker_gave_before_cr_b(self):
        """The pre-CR-B resolve loop, copied here, run on 2,000 random fills against the tracker as it is now (the pooled and the venue/role stats)."""
        import heapq
        from collections import defaultdict
        horizons, pend, n = (1.0, 5.0, 30.0), [], [0]
        stats = {h: [0.0, 0.0, 0.0, 0.0] for h in horizons}
        by = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0.0, 0.0]))
        fills, mid = _fills(), {"bybit": 100.0, "binance": 100.0}

        def pop(now, mids, fin):
            while pend and (fin or pend[0][0] <= now):
                _, _, (h, v, side, price, qty, role) = heapq.heappop(pend)
                m = mids.get(v, 0.0)
                if m <= 0:
                    continue
                bps = (1.0 if side == "buy" else -1.0) * (m - price) / price * 10_000.0
                for st in [stats[h]] + ([by[f"{v}/{role}"][h]] if role else []):
                    st[0] += 1; st[1] += qty; st[2] += bps * qty; st[3] += float(bps < 0.0)

        for ts, v, side, price, qty, role, *_ in fills:
            pop(ts, mid, False)
            mid = {"bybit": 100 + ts * 7 % 2.0, "binance": 100 + ts * 3 % 2.0}
            for h in horizons:
                heapq.heappush(pend, (ts + h, n[0], (h, v, side, price, qty, role))); n[0] += 1
        pop(fills[-1][0] + 1, mid, True)
        mt = MarkoutTracker(horizons)
        _drive(mt, False)
        for h in horizons:
            self.assertEqual([mt.stats[h][k] for k in ("count", "qty", "bps_qty_sum", "adverse")], stats[h])
        for key, per_h in by.items():
            for h, st in per_h.items():
                self.assertEqual([mt.by_venue_role[key][h][k] for k in ("count", "qty", "bps_qty_sum", "adverse")], st)

    @unittest.skipUnless(__import__("os").path.exists(MM_FILE), "the MM worktree is not on this machine")
    def test_decompose_true_matches_the_mm_decomposing_tracker_row_for_row(self):
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location("mm_markout_ref", self.MM_FILE)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["mm_markout_ref"] = mod        # the module's dataclass needs to find itself
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.modules.pop("mm_markout_ref", None)
        ref, new = mod.DecomposingMarkoutTracker((1.0, 5.0, 30.0)), MarkoutTracker((1.0, 5.0, 30.0), decompose=True)
        _drive(ref, True)
        _drive(new, True)
        self.assertEqual(len(new.rows), 6000)
        self.assertEqual(new.rows, ref.rows)
        self.assertEqual(new.decomposed_summary(by=("pair", "k")), ref.decomposed_summary(by=("pair", "k")))


if __name__ == "__main__":
    unittest.main()
