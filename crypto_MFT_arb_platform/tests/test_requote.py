"""S0 (owner 7 Nov): quote repricing. The rule (core, pure), the target (the strategy's edge at the CURRENT hedge-venue price) and the simulator glue (Requoter), each tested
on its own; with the setting off nothing changes. No number below is a recommended setting: they are fixtures."""
from __future__ import annotations

import dataclasses
import unittest

from crypto_stat_arb.ems.config import ExecutionConfig
from crypto_stat_arb.ems.intent import IntentError
from crypto_stat_arb.ems.tactics.passive import (AMEND, CANCEL, CANCEL_REPLACE, HOLD, RepriceConfig, RepriceState, after_amend_rejected, decide_reprice)

from test_maker_order_lifetime import BINANCE, BYBIT, Harness

BASE = dict(enabled=True, source="ILLUSTRATIVE_NOT_FITTED", band_down_bps=5.0, band_up_bps=20.0, debounce_ms=200.0, jump_bps=15.0, min_interval_ms=250.0, max_age_s=30.0)


def cfg(**kw):
    return RepriceConfig(**{**BASE, **kw})


class Rule(unittest.TestCase):
    """decide_reprice: pure; the same inputs, the same answer."""
    def test_a_config_that_is_not_enabled_never_acts_and_none_parses_to_it(self):
        self.assertEqual(decide_reprice(RepriceConfig(), "buy", 100.0, 90.0, 1.0, RepriceState(0.0)).reason, "disabled")
        self.assertFalse(RepriceConfig.parse("none").enabled)

    def test_nothing_is_defaulted_when_enabled(self):
        with self.assertRaises(IntentError):
            RepriceConfig(enabled=True, band_down_bps=5.0)                       # min_interval_ms and max_age_s missing
        with self.assertRaises(IntentError):
            RepriceConfig(enabled=True, band_down_bps=5.0, min_interval_ms=1.0, max_age_s=1.0, debounce_ms=1.0)   # debounce without a jump size
        with self.assertRaises(IntentError):
            RepriceConfig.parse({"enabled": True, "bogus": 1})
        with self.assertRaises(IntentError):
            RepriceConfig(source="somebody")

    def test_an_adverse_move_beyond_the_band_amends_and_inside_the_band_holds(self):
        st = RepriceState(0.0)
        d = decide_reprice(cfg(), "buy", 100.0, 99.90, 1.0, st)                  # target 10 bps below the resting bid
        self.assertEqual((d.action, d.price, d.reason), (AMEND, 99.90, "adverse_move"))
        self.assertEqual(decide_reprice(cfg(), "buy", 100.0, 99.97, 1.0, RepriceState(0.0)).reason, "inside_band")      # 3 bps: sticky
        d = decide_reprice(cfg(), "sell", 100.0, 100.10, 1.0, RepriceState(0.0))                                        # an ask: adverse is a target ABOVE it
        self.assertEqual((d.action, d.reason), (AMEND, "adverse_move"))

    def test_minimum_interval_delays_a_reprice_but_never_a_cancel(self):
        st = RepriceState(0.0, last_request_ts=1.0)
        self.assertEqual(decide_reprice(cfg(), "buy", 100.0, 99.80, 1.1, st).reason, "min_interval")
        self.assertEqual(decide_reprice(cfg(), "buy", 100.0, 99.80, 1.3, st).action, AMEND)
        self.assertEqual(decide_reprice(cfg(), "buy", 100.0, None, 1.1, st), dataclasses.replace(decide_reprice(cfg(), "buy", 100.0, None, 1.1, st)))
        self.assertEqual(decide_reprice(cfg(), "buy", 100.0, None, 1.1, st).action, CANCEL)

    def test_a_favourable_move_needs_a_band_up_no_queue_ahead_and_no_recent_jump(self):
        up = 100.5                                                               # 50 bps above a resting bid
        self.assertEqual(decide_reprice(cfg(band_up_bps=None, debounce_ms=None, jump_bps=None), "buy", 100.0, up, 5.0, RepriceState(0.0)).reason, "inside_band")
        self.assertEqual(decide_reprice(cfg(), "buy", 100.0, up, 5.0, RepriceState(0.0), queue_ahead=3.0).reason, "queue_hold")
        self.assertEqual(decide_reprice(cfg(queue_hold=False), "buy", 100.0, up, 5.0, RepriceState(0.0), queue_ahead=3.0).action, AMEND)
        st = RepriceState(0.0, last_jump_ts=4.9)
        self.assertEqual(decide_reprice(cfg(), "buy", 100.0, up, 5.0, st).reason, "debounce_after_jump")
        self.assertEqual(decide_reprice(cfg(), "buy", 100.0, up, 5.2, st).action, AMEND)          # 300 ms after the jump

    def test_the_jump_detector_and_a_pending_request(self):
        c, st = cfg(), RepriceState(0.0)
        st.observe_hedge(c, 1.0, 100.0)
        st.observe_hedge(c, 2.0, 100.30)                                          # 30 bps: a jump
        self.assertEqual(st.last_jump_ts, 2.0)
        st.pending = True
        self.assertEqual(decide_reprice(c, "buy", 100.0, 90.0, 3.0, st).reason, "pending")

    def test_age_and_amend_first(self):
        d = decide_reprice(cfg(), "buy", 100.0, 100.0, 31.0, RepriceState(0.0))
        self.assertEqual((d.action, d.reason), (CANCEL_REPLACE, "max_age"))
        self.assertEqual(decide_reprice(cfg(amend_first=False), "buy", 100.0, 99.0, 1.0, RepriceState(0.0)).action, CANCEL_REPLACE)

    def test_after_a_rejected_amend(self):
        c = cfg()
        self.assertEqual(after_amend_rejected(c, 99.0, order_gone=True).reason, "order_gone_resync")        # a fill is a fill: nothing is resent
        self.assertEqual(after_amend_rejected(c, 99.0, order_gone=False).action, CANCEL_REPLACE)
        self.assertEqual(after_amend_rejected(c, None, order_gone=False).action, CANCEL)

    def test_the_execution_setting_is_read(self):
        ex = ExecutionConfig(pairing="maker_taker", primary={"tactic": "PASSIVE", "placement": "deep", "price_rule": "conditional_edge", "reprice": dict(BASE)})
        from crypto_stat_arb.ems.tactics.passive import Passive
        self.assertTrue(Passive(ex).reprice.enabled)
        self.assertFalse(Passive(ExecutionConfig(pairing="maker_taker")).reprice.enabled)


def harness(reprice=True, **kw):
    primary = {"tactic": "PASSIVE", "placement": "deep", "price_rule": "conditional_edge", "reprice": (dict(BASE, **kw.pop("rq", {})) if reprice else "none")}
    h = Harness(ttl=60.0, entry_s=0.003, cancel_s=0.05, queue=0.0, profile="perp_perp_maker_taker_v1",
                cfg_kw={"execution": ExecutionConfig(pairing="maker_taker", primary=primary), **kw})
    h.book(0.5, BYBIT, bids=((99.0, 5.0), (98.0, 5.0)), asks=((101.0, 5.0), (102.0, 5.0)))
    h.book(0.5, BINANCE, bids=((100.0, 50.0), (99.5, 50.0)), asks=((100.1, 50.0), (100.6, 50.0)))
    return h


def move_binance(h, t, bid):
    """The hedge venue's book changes; run() then asks the requoter about the resting quotes (the hook sits after _apply_event in the loop, so the harness calls it)."""
    h.book(t, BINANCE, bids=((bid, 50.0), (bid - 0.5, 50.0)), asks=((bid + 0.1, 50.0), (bid + 0.6, 50.0)))
    if h.bt.requoter is not None:
        h.bt.requoter.on_hedge_event(BINANCE, t)


class Glue(unittest.TestCase):
    def _quote(self, h):
        t = h.bt.strategy.requote_target(BYBIT, "buy", 2.0, h.bt.books, 0.6)
        self.assertEqual(t[1], "ok")
        h.send_maker(1.0, venue=BYBIT, qty=2.0, price=t[0])
        h.advance(1.1)
        return t[0]

    def test_off_is_the_default_and_builds_nothing(self):
        h = harness(reprice=False)
        self.assertIsNone(h.bt.requoter)
        self._quote(h)
        move_binance(h, 2.0, 99.0)
        h.book(2.5, BYBIT)
        self.assertEqual(len(h.resting), 1)

    def test_an_adverse_hedge_move_reprices_the_quote_after_the_amend_latency(self):
        h = harness()
        p0 = self._quote(h)
        old_id = h.order().order_id
        move_binance(h, 2.0, 99.5)                                                # Binance bid -50 bps: the resting bid has lost its edge
        rq = h.bt.requoter
        self.assertEqual(rq.stats["requests_amend"], 1)
        self.assertEqual(h.order().price, p0)                                    # the old order still rests (and is fillable) until the amend lands
        h.book(2.01, BYBIT, bids=((99.0, 5.0), (98.0, 5.0)))                      # the first Bybit event after the effective time
        o = h.order()
        self.assertLess(o.price, p0)
        self.assertNotEqual(o.order_id, old_id)
        self.assertEqual(rq.stats["amends_applied"], 1)
        self.assertEqual(h.bt._maker_to_signal[o.order_id], 1)                   # same signal: its hedge on a fill is unchanged
        self.assertNotIn(old_id, h.bt._maker_to_signal)
        t = h.bt.strategy.requote_target(BYBIT, "buy", 2.0, h.bt.books, 2.01)
        self.assertAlmostEqual(o.price, t[0])                                    # the edge at the CURRENT hedge price
        rep = rq.report()
        self.assertEqual(rep["amends_total"], 1)
        self.assertEqual(rep["config"]["source"], "ILLUSTRATIVE_NOT_FITTED")

    def test_a_small_hedge_move_inside_the_band_holds(self):
        h = harness()
        self._quote(h)
        move_binance(h, 2.0, 99.97)                                               # -3 bps
        self.assertEqual(h.bt.requoter.stats["requests_amend"], 0)
        self.assertGreaterEqual(h.bt.requoter.holds["inside_band"], 1)

    def test_a_fill_while_the_amend_is_in_flight_is_kept_and_nothing_is_resent(self):
        h = harness()
        p0 = self._quote(h)
        move_binance(h, 2.0, 99.5)
        h.sell_trade(2.001, BYBIT, volume=10.0, price=p0)                        # the old quote is still resting at p0: it fills in full
        self.assertEqual(len(h.resting), 0)
        h.book(2.01, BYBIT)
        rq = h.bt.requoter
        self.assertEqual(rq.stats["amend_order_gone"], 1)
        self.assertEqual(rq.stats["amends_applied"], 0)
        self.assertEqual(len(h.resting), 0)

    def test_a_partial_fill_inside_the_window_is_counted(self):
        h = harness()
        p0 = self._quote(h)
        move_binance(h, 2.0, 99.5)
        h.sell_trade(2.001, BYBIT, volume=0.5, price=p0)                         # queue ahead 0: a part of the 2.0 fills
        h.book(2.01, BYBIT)
        rq = h.bt.requoter
        self.assertEqual(rq.stats["fills_inside_amend_window"], 1)
        self.assertAlmostEqual(h.order().remaining, 1.5)                         # the unfilled rest moved

    def test_a_replacement_the_post_only_check_refuses_leaves_nothing_resting(self):
        h = harness(postonly_reject_model="on", postonly_notice_s=0.05)
        p0 = self._quote(h)
        h.book(1.9, BYBIT, bids=((99.0, 5.0), (98.0, 5.0)), asks=((99.4, 5.0), (102.0, 5.0)))   # the Bybit ask is now below where the new quote would rest
        move_binance(h, 2.0, 99.5)
        h.book(2.01, BYBIT, bids=((99.0, 5.0), (98.0, 5.0)), asks=((99.4, 5.0), (102.0, 5.0)))
        rq = h.bt.requoter
        self.assertEqual(rq.stats["amend_rejected_postonly"], 1)
        self.assertEqual(len(h.resting), 0)

    def test_no_edge_at_the_current_hedge_price_cancels_even_inside_the_interval(self):
        h = harness()
        self._quote(h)
        move_binance(h, 2.0, 99.5)                                                # first reprice request
        h.book(2.01, BYBIT)
        h.bt.config.arb.min_net_edge_bps = 10_000.0                               # the edge can no longer clear: no target
        move_binance(h, 2.05, 99.0)
        self.assertEqual(h.bt.requoter.stats["requests_cancel"], 1)

    def test_a_max_age_requote_starts_a_new_quote_and_is_not_repeated_every_interval(self):
        h = harness(rq={"max_age_s": 1.0, "band_down_bps": 5.0})
        self._quote(h)
        move_binance(h, 2.1, 99.99)                                               # inside the band, but the quote (placed at 1.003) is older than 1 s: re-quote at the current target
        rq = h.bt.requoter
        self.assertEqual(rq.stats["requests_cancel_replace"], 1)
        h.book(2.2, BYBIT)                                                         # a cancel and a new order: 0.05 + 0.003 s
        self.assertEqual(rq.stats["amends_applied"], 1)
        for t in (2.5, 2.8, 3.1):                                                 # more than min_interval apart, the new quote is younger than 1 s
            move_binance(h, t, 99.99)
        self.assertEqual(rq.stats["requests_cancel_replace"], 1)

    def test_a_stale_hedge_touch_is_never_acted_on(self):
        h = harness()
        self._quote(h)
        h.bt.config.arb.max_touch_age_s = -1.0                                     # every touch counts as stale
        move_binance(h, 2.0, 99.5)
        self.assertEqual(h.bt.requoter.stats["requests_amend"], 0)
        self.assertGreaterEqual(h.bt.requoter.holds["stale_hedge_touch"], 1)


class NoUnusedSettings(unittest.TestCase):
    """Owner 7 Nov: no strategy setting may be declared and unused. A field of ArbConfig or RepriceConfig that nothing reads, or an EMS section key that nothing reads, fails here.
    The EMS keys below are declared by ExecutionConfig, named in the frozen profiles and read by nothing today: a listed debt, not a licence (a new unused key fails)."""
    KNOWN_UNUSED_EMS_KEYS = {"primary": {"price_rule"}, "hedge": {"q"}, "maker_maker": {"escalate_after_s"}, "unwind": {"fallback", "window_s"}}

    @staticmethod
    def _source():
        import re
        from pathlib import Path
        src = Path(__file__).resolve().parents[1] / "src"
        files = [f for f in list(src.glob("new_backtest/**/*.py")) + list(src.glob("crypto_stat_arb/ems/**/*.py")) if "nested" not in str(f)]
        return {f: "\n".join(l.split("#", 1)[0] for l in f.read_text().splitlines()) for f in files}

    def test_every_arb_and_reprice_field_is_read(self):
        import re
        from new_backtest.arb_strategy import ArbConfig
        texts = self._source()
        for cls in (ArbConfig, RepriceConfig):
            for f in dataclasses.fields(cls):
                n = sum(len(re.findall(rf"\b{f.name}\b", t)) for t in texts.values())
                self.assertGreaterEqual(n, 2, f"{cls.__name__}.{f.name} is declared and read by nothing")

    def test_every_ems_section_key_is_read_outside_its_declaration(self):
        import re
        from pathlib import Path
        from crypto_stat_arb.ems.config import _SECTION_KEYS
        texts = {f: t for f, t in self._source().items() if f.name != "config.py" or "ems" not in str(f)}
        blob = " ".join(texts.values())
        unused = {sec: sorted(k for k in keys if not re.search(rf"[\"']{k}[\"']", blob)) for sec, keys in _SECTION_KEYS.items()}
        for sec, ks in unused.items():
            self.assertEqual(set(ks) - self.KNOWN_UNUSED_EMS_KEYS.get(sec, set()), set(), f"execution.{sec}: keys declared and read by nothing")


class ProfileV1_3(unittest.TestCase):
    """v1_3 = v1_2 (frozen) + the one reprice switch, off, every number null: nothing is set for the trader."""
    def test_v1_3_differs_from_v1_2_only_in_the_reprice_switch_and_its_labels(self):
        from new_backtest.profiles import load_profile
        def flat(o, p=""):
            out = {}
            if isinstance(o, dict):
                for k, v in o.items():
                    if not k.startswith("_"):
                        out.update(flat(v, p + k + "."))
            else:
                out[p[:-1]] = o
            return out
        a, b = flat(load_profile("perp_perp_maker_taker_v1_2")), flat(load_profile("perp_perp_maker_taker_v1_3"))
        diff = {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
        self.assertTrue(all(k in ("name", "description", "decision_status.requote") or k.startswith("execution.primary.reprice") for k in diff), diff)

    def test_v1_3_ships_with_the_rule_off_and_no_number_set(self):
        from new_backtest.profiles import load_profile
        r = load_profile("perp_perp_maker_taker_v1_3")["execution"]["primary"]["reprice"]
        self.assertIs(r["enabled"], False)
        self.assertTrue(all(r[k] is None for k in ("band_down_bps", "band_up_bps", "debounce_ms", "jump_bps", "min_interval_ms", "max_age_s")))
        self.assertFalse(RepriceConfig.parse(r).enabled)

    def test_the_profile_builds_a_backtester_with_no_requoter(self):
        from new_backtest.backtester import CrossVenueArbBacktester
        from new_backtest.profiles import apply_profile, load_profile
        cfg = apply_profile(load_profile("perp_perp_maker_taker_v1_3"), dataset="mar2024", data_root="/nonexistent", pair="SOLUSDT", start_date="2024-03-08", end_date="2024-03-08", output_dir="/tmp/x_v13")
        cfg.allow_manifest_gap = "test"
        self.assertIsNone(CrossVenueArbBacktester(cfg).requoter)

    def test_a_profile_that_enables_it_without_the_numbers_is_refused(self):
        with self.assertRaises(IntentError):
            RepriceConfig.parse({"enabled": True, "band_down_bps": None})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
