"""Source labels on order events and fills (order_events.py), and the strategy-level fault layer (hedge_faults.py extensions,
fault_presets.py): disconnect windows, lost acknowledgements ('unknown'), per-venue rejects, latency scaling, fast-move hedge
rejects, cancel/fill races, presets and determinism by seed.

Harness book: bybit and binance bids 99/98, asks 101/102 (size 5). The bybit maker bid rests at 100.0 for 2.0 (queue ahead 0.5);
`fill()` trades 2.5 into it, which fills all 2.0 and sends the hedge (a sell on binance).
"""
from __future__ import annotations

import random
import tempfile
import unittest
import warnings
from pathlib import Path

from new_backtest import fault_presets
from new_backtest.arb_strategy import MAKER_TAKER, ArbConfig
from new_backtest.backtester import ArbBacktestConfig, CrossVenueArbBacktester
from new_backtest.event_loader import BOOK, TRADE
from new_backtest.fill_engine import MakerConfig
from new_backtest.hedge_faults import DisconnectGen, DisconnectSpec, HedgeFaultConfig, Outage
from new_backtest.order_events import SOURCES, OrderEvent, OrderEventLog, require_source

from synthetic_days import gen
from test_hedge_failures import fh, fill, mark, rows, walk
from test_maker_order_lifetime import BINANCE, BYBIT, Harness, started

warnings.simplefilter("ignore")
CAL = str(Path(__file__).resolve().parent.parent / "config" / "lifecycle_calibration_bybit.json")
FAULT_KINDS = {"send_into_outage", "hedge_delayed", "order_unknown", "cancel_delayed_by_outage"}


def is_fault_event(e) -> bool:
    return e.kind in FAULT_KINDS or e.reason.startswith("fault_")


def rec_harness(**kw):
    cfg = dict(record_order_events=True)
    cfg.update(kw.pop("cfg_kw", {}))
    h = Harness(cfg_kw=cfg, **kw)
    h.bt.latency.aligner.samples = 1
    return h


def boot(h, t=0.0):
    h.book(t, BYBIT)
    h.book(t, BINANCE)
    h.send_maker(t)
    h.advance(t + 0.003)
    mark(h, t + 0.003)


# ── Task 4: source labels ────────────────────────────────────────────────────

class SourceFieldTests(unittest.TestCase):
    def test_an_event_cannot_be_built_without_a_source(self):
        with self.assertRaises(TypeError):
            OrderEvent(ts=1.0, kind="fill", venue="bybit")

    def test_an_unknown_source_is_refused(self):
        for bad in ("real", "", None, "Modelled"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                OrderEvent(ts=1.0, kind="fill", venue="bybit", source=bad)

    def test_the_log_will_not_emit_without_a_source(self):
        with self.assertRaises(TypeError):
            OrderEventLog().emit(1.0, "fill", "bybit")

    def test_the_vocabulary_is_the_three_the_brief_names(self):
        self.assertEqual(SOURCES, ("historical", "modelled", "injected"))

    def test_require_source_flags_rows_without_a_label(self):
        require_source([{"source": "modelled"}])
        with self.assertRaises(ValueError):
            require_source([{"source": "modelled"}, {"qty": 1}])

    def test_counts_are_kept_without_keeping_the_events(self):
        log = OrderEventLog(keep=False)
        log.emit(1.0, "fill", "bybit", source="modelled")
        log.emit(2.0, "fill", "bybit", source="injected")
        self.assertEqual(log.by_source(), {"historical": 0, "modelled": 1, "injected": 1})
        self.assertEqual(log.events, [])


class SourceLabelRunTests(unittest.TestCase):
    def test_injection_off_produces_zero_injected_events_and_every_fill_is_modelled(self):
        h = rec_harness(ttl=10.0)
        boot(h)
        fill(h)
        h.advance(5.003)
        by = h.bt.events.by_source()
        self.assertEqual((by["injected"], by["historical"]), (0, 0))
        self.assertGreater(by["modelled"], 0)
        self.assertGreater(len(rows(h)), 0)
        require_source(rows(h), "trade_log row")
        self.assertTrue(all(r["source"] == "modelled" and r["chain_id"] == 0 for r in rows(h)))
        kinds = {e.kind for e in h.bt.events.events}
        self.assertTrue({"order_sent", "order_resting", "fill"} <= kinds)

    def test_injection_on_labels_every_injected_event_and_only_those(self):
        h = rec_harness(ttl=10.0, faults=fh(reject_prob=1.0, response="unwind"))
        boot(h)
        fill(h)
        walk(h, [5.003, 5.03, 5.05, 5.1])
        events = h.bt.events.events
        injected = [e for e in events if e.source == "injected"]
        self.assertGreater(len(injected), 0)
        for e in events:
            self.assertIn(e.source, SOURCES)
            self.assertEqual(e.source == "injected", is_fault_event(e), f"{e.kind} {e.reason} labelled {e.source}")
        self.assertTrue(any(e.kind == "hedge_failed" and e.reason == "fault_rejected" and e.source == "injected" for e in events))

    def test_the_consequences_of_an_injected_fault_are_modelled_and_carry_the_chain_id(self):
        h = rec_harness(ttl=10.0, faults=fh(reject_prob=1.0, response="unwind"))
        boot(h)
        fill(h)
        walk(h, [5.003, 5.03, 5.05, 5.1])
        fault = next(e for e in h.bt.events.events if e.kind == "hedge_failed" and e.source == "injected")
        unwind_fills = [r for r in rows(h, taker=True)]
        self.assertEqual(len(unwind_fills), 1)
        self.assertEqual(unwind_fills[0]["source"], "modelled")
        self.assertEqual(unwind_fills[0]["chain_id"], fault.chain_id)
        self.assertNotEqual(fault.chain_id, 0)
        maker_fill = next(r for r in rows(h, taker=False))
        self.assertEqual(maker_fill["chain_id"], 0, "the maker fill happened before and independently of the fault")

    def test_postonly_rejects_are_modelled_not_injected(self):
        h = rec_harness(ttl=0.0, cfg_kw={"postonly_reject_model": "on", "postonly_notice_s": 0.05}, queue=0.0)
        h.book(0.0, BYBIT); h.book(0.0, BINANCE)
        h.send_maker(0.0, price=101.0)
        h.event(0.03, TRADE, BYBIT, {"price": 50.0, "volume": 0.001, "side": "s"}, exchange_ts=0.03)
        rej = [e for e in h.bt.events.events if e.kind == "order_rejected"]
        self.assertEqual([(e.source, e.reason) for e in rej], [("modelled", "postonly_would_take_liquidity".replace("would_take_liquidity", "reject"))])

    def test_a_non_postonly_reject_on_a_passive_order_is_injected(self):
        h = rec_harness(ttl=10.0, faults=fh(reject_prob_by_venue=((BYBIT, 1.0),), response="unwind"))
        h.book(0.0, BYBIT); h.book(0.0, BINANCE)
        h.send_maker(0.0)
        h.advance(0.01)
        self.assertEqual(h.resting, [])
        rej = [e for e in h.bt.events.events if e.kind == "order_rejected"]
        self.assertEqual([(e.source, e.reason) for e in rej], [("injected", "fault_rejected")])
        self.assertEqual(h.bt.hedge["maker_orders_rejected_injected"], 1)


# ── Task 5: disconnect windows, unknown state, rejects, scaling ──────────────

class DisconnectWindowTests(unittest.TestCase):
    def _h(self, **kw):
        return Harness(ttl=kw.pop("ttl", 30.0), max_live=3, faults=fh(outages=(Outage(BYBIT, 4.0, 20.0),), outage_policy="blackhole",
                                                                         response="unwind", ack_timeout_s=2.0, **kw))

    def test_a_send_into_the_window_does_not_reach_the_exchange_but_a_resting_order_still_fills_and_is_reported_at_reconnect(self):
        h = self._h()
        boot(h)                                             # first order rests since 0.003
        h.send_maker(5.0)                                   # sent while the bybit gateway is down
        walk(h, [5.01])
        self.assertEqual(h.bt.hedge["maker_sends_into_outage"], 1)
        self.assertEqual(len(h.resting), 1, "the second order never arrived")
        walk(h, [6.9])
        self.assertEqual(h.bt.miss_reasons["fault_outage_blackhole"], 0)
        walk(h, [7.01])
        self.assertEqual(h.bt.miss_reasons["fault_outage_blackhole"], 1, "lost, and noticed only after the acknowledgement timeout")
        # the resting order is hit inside the window: the fill is real, the report is not seen until reconnect
        h.sell_trade(10.0, BYBIT, volume=2.5)
        mark(h, 10.0)
        self.assertAlmostEqual(h.bt.inventory.base[BYBIT], 2.0, msg="the resting order CAN still fill during the window")
        self.assertEqual(h.bt.hedge["fill_reports_deferred"], 1)
        (hedge,) = [o for o in h.bt.pending.in_flight() if o.is_hedge]
        self.assertAlmostEqual(hedge.sent_ts, 20.0, msg="the hedge is sent when the report arrives, at reconnect, not at the fill")
        walk(h, [19.99])
        self.assertAlmostEqual(h.bt.inventory.base[BINANCE], 0.0, msg="nothing hedged while the gateway is down")
        walk(h, [20.0, 20.004])
        self.assertAlmostEqual(h.bt.inventory.base[BINANCE], -2.0)
        h.assert_ledger_reconciles(self)

    def test_a_cancel_requested_inside_the_window_waits_for_reconnect_and_the_order_can_fill_meanwhile(self):
        h = self._h(ttl=4.5)                                # expiry is requested at 4.503, inside the window
        boot(h)
        walk(h, [4.6])
        self.assertEqual(h.bt.hedge["cancels_delayed_by_outage"], 1)
        order = h.order()
        self.assertAlmostEqual(order.cancel_effective_ts, 20.0 + 0.05, places=6)
        h.sell_trade(10.0, BYBIT, volume=2.5)
        self.assertAlmostEqual(h.bt.inventory.base[BYBIT], 2.0, msg="cancel-pending order filled while the cancel could not get through")
        self.assertEqual(h.bt.lifetime["cancels_applied"], 0)

    def test_outside_the_window_everything_is_as_without_the_layer(self):
        h = self._h()
        boot(h)
        h.sell_trade(1.0, BYBIT, volume=2.5)
        walk(h, [1.004])
        self.assertEqual((h.bt.hedge["fill_reports_deferred"], h.bt.hedge["hedges_sent"], h.bt.hedge["maker_sends_into_outage"]), (0, 1, 0))

    def test_a_queued_passive_order_is_delivered_at_reconnect(self):
        h = Harness(ttl=30.0, max_live=3, faults=fh(outages=(Outage(BYBIT, 4.0, 20.0),), outage_policy="queue", response="unwind"))
        h.book(0.0, BYBIT); h.book(0.0, BINANCE)
        h.send_maker(5.0)
        walk(h, [6.0, 19.9])
        self.assertEqual(h.resting, [])
        walk(h, [20.01])
        self.assertEqual(len(h.resting), 1)
        self.assertEqual(h.bt.hedge["maker_queued_through_outage"], 1)


class DisconnectGeneratorTests(unittest.TestCase):
    def test_same_seed_same_windows_other_seed_other_windows(self):
        spec = DisconnectSpec(BINANCE, rate_per_day=3.0, guaranteed_per_day=1, median_s=120.0)
        a, b, c = (DisconnectGen(spec, random.Random(s)) for s in (1, 1, 2))
        for g in (a, b, c):
            g.extend(86400.0 * 4 + 5)
        self.assertEqual(a.windows, b.windows)
        self.assertNotEqual(a.windows, c.windows)

    def test_at_least_the_guaranteed_number_per_day(self):
        g = DisconnectGen(DisconnectSpec(BYBIT, guaranteed_per_day=2, median_s=30.0), random.Random(3))
        g.extend(86400.0 * 6 + 5)
        per_day = {}
        for w in g.windows:
            per_day[int(w.start_ts // 86400)] = per_day.get(int(w.start_ts // 86400), 0) + 1
        self.assertTrue(all(n >= 2 for n in per_day.values()), per_day)
        self.assertEqual(len(per_day), 1, "only the day(s) the replay reaches are generated")

    def test_durations_come_from_the_empirical_set_when_given(self):
        g = DisconnectGen(DisconnectSpec(BYBIT, rate_per_day=50.0, durations_s=(65.2, 122.1)), random.Random(4))
        g.extend(100.0)
        self.assertTrue(g.windows)
        self.assertTrue(all(round(w.end_ts - w.start_ts, 1) in (65.2, 122.1) for w in g.windows))

    def test_windows_are_active_inside_the_backtester(self):
        h = Harness(ttl=10.0, faults=fh(disconnects=(DisconnectSpec(BINANCE, guaranteed_per_day=1, median_s=300.0),)))
        gen_ = h.bt._disc_gens[0]
        gen_.extend(10.0)
        w = gen_.windows[0]
        self.assertIs(h.bt._active_outage(BINANCE, w.start_ts + 0.1), w)
        self.assertIsNone(h.bt._active_outage(BYBIT, w.start_ts + 0.1))
        self.assertIsNone(h.bt._active_outage(BINANCE, w.end_ts + 0.1))

    def test_a_spec_on_a_venue_outside_the_run_is_refused(self):
        with self.assertRaises(ValueError):
            Harness(faults=fh(disconnects=(DisconnectSpec("kraken", rate_per_day=1.0),)))


class UnknownStateTests(unittest.TestCase):
    def test_a_lost_ack_on_a_passive_order_holds_back_the_fill_report_until_reconciliation(self):
        h = rec_harness(ttl=30.0, faults=fh(ack_unknown_prob=1.0, ack_timeout_s=2.0, unknown_resolve_s=3.0, response="unwind"))
        boot(h)
        self.assertEqual(h.bt.hedge["unknown_orders"], 1)
        (oid,) = h.bt._maker_unknown_until
        self.assertAlmostEqual(h.bt._maker_unknown_until[oid], 0.003 + 2.0 + 3.0)
        h.sell_trade(4.0, BYBIT, volume=2.5)
        mark(h, 4.0)
        self.assertAlmostEqual(h.bt.inventory.base[BYBIT], 2.0, msg="the order is live at the exchange regardless")
        self.assertEqual(h.bt.hedge["fill_reports_deferred_unknown"], 1)
        (hedge,) = [o for o in h.bt.pending.in_flight() if o.is_hedge]
        self.assertAlmostEqual(hedge.sent_ts, 0.003 + 2.0 + 3.0, msg="the strategy does not know it filled before the reconciliation")
        walk(h, [4.5, 5.0])
        self.assertAlmostEqual(h.bt.inventory.base[BINANCE], 0.0)
        walk(h, [5.01, 5.02])
        self.assertLess(h.bt.inventory.base[BINANCE], 0.0)
        kinds = [(e.kind, e.source) for e in h.bt.events.events if e.kind == "order_unknown"]
        self.assertTrue(kinds and all(s == "injected" for _, s in kinds))

    def test_a_lost_ack_on_a_hedge_is_resolved_by_query_not_by_a_retry(self):
        h = rec_harness(ttl=30.0, faults=fh(ack_unknown_prob=1.0, ack_timeout_s=2.0, unknown_resolve_s=3.0, response="retry_unwind"))
        boot(h)
        h.sell_trade(8.0, BYBIT, volume=2.5)                # report deferred past the unknown window: hedge sent immediately
        mark(h, 8.0)
        walk(h, [8.004, 8.01])
        self.assertAlmostEqual(h.bt.inventory.base[BINANCE], -2.0, msg="the hedge executed at the exchange")
        self.assertEqual(h.bt.hedge["retries_sent"], 0)
        self.assertEqual(h.bt.hedge["unknown_resolved"], 0)
        walk(h, [12.9])
        self.assertEqual(h.bt.hedge["unknown_resolved"], 0)
        walk(h, [13.1])                                     # 8.003 + 2 + 3
        self.assertEqual(h.bt.hedge["unknown_resolved"], 1)
        self.assertEqual(h.bt.signals_fully_hedged, 1)


class ScalingAndRejectTests(unittest.TestCase):
    def test_latency_scale_multiplies_entry_and_cancel_latency(self):
        h = Harness(ttl=1.0, entry_s=0.003, cancel_s=0.05, faults=fh(latency_scale=5.0, response="unwind"))
        h.book(0.0, BYBIT); h.book(0.0, BINANCE)
        h.send_maker(0.0)
        self.assertAlmostEqual(next(iter(h.bt.pending.in_flight())).release_ts, 0.015)
        h.advance(0.02)
        h.advance(1.1)
        self.assertAlmostEqual(h.order().cancel_effective_ts - h.order().cancel_requested_ts, 0.25)

    def test_a_per_venue_reject_probability_hits_only_that_venue(self):
        h = Harness(ttl=10.0, faults=fh(reject_prob_by_venue=((BINANCE, 1.0),), response="none"))
        boot(h)
        fill(h)
        walk(h, [5.003, 5.1])
        self.assertEqual(len(h.resting) + 0, 0)
        self.assertEqual(h.bt.hedge["hedges_rejected_by_venue_prob"], 1)
        self.assertAlmostEqual(h.bt.inventory.net_base, 2.0, msg="the maker venue was not rejecting")

    def test_a_hedge_sent_in_a_fast_move_is_rejected_and_one_in_a_calm_market_is_not(self):
        for move, expect in ((True, 1), (False, 0)):
            h = Harness(ttl=10.0, faults=fh(fast_move_reject_prob=1.0, fast_move_bps=5.0, fast_move_window_s=1.0, response="none"))
            boot(h)
            h.book(4.0, BINANCE)
            if move:
                h.book(4.9, BINANCE, bids=((96.0, 5.0),), asks=((98.0, 5.0),))       # mid 100 -> 97 within a second
            else:
                h.book(4.9, BINANCE)
            fill(h)
            walk(h, [5.003])
            self.assertEqual(h.bt.hedge["fast_move_hedge_rejects"], expect, f"move={move}")

    def test_the_new_random_phenomena_do_not_move_the_hedge_draws(self):
        def decisions(**kw):
            h = Harness(ttl=10.0, faults=fh(reject_prob=0.5, delay_prob=0.5, extra_delay_s=1.0, response="unwind", **kw))
            return [(h.bt._fault_rng.random(), h.bt._fault_rng.random(), h.bt._fault_rng.random()) for _ in range(5)]
        self.assertEqual(decisions(), decisions(ack_unknown_prob=0.5, latency_scale=3.0))


class TrackingTests(unittest.TestCase):
    def test_a_hedge_failure_creates_unhedged_inventory_that_is_tracked_and_later_closed(self):
        h = rec_harness(ttl=10.0, faults=fh(reject_prob=1.0, response="unwind"))
        boot(h)
        fill(h)
        walk(h, [5.003])
        self.assertAlmostEqual(h.bt.inventory.net_base, 2.0)
        self.assertAlmostEqual(h.bt.exposure.peak_abs_net, 2.0)
        walk(h, [5.0035 + h.feed_s[BINANCE], 5.03, 5.05, 5.1])
        self.assertAlmostEqual(h.bt.inventory.net_base, 0.0)
        ev = h.bt.hedge_events[0]
        self.assertEqual((ev["outcome"], ev["unwound_qty"]), ("unwound", 2.0))
        rep = h.bt.exposure.report(5.1)
        self.assertGreater(rep["longest_net_nonzero_s"], 0.0)
        self.assertAlmostEqual(rep["peak_abs_net_base"], 2.0)


# ── presets and determinism on a synthetic replay ────────────────────────────

class PresetTests(unittest.TestCase):
    def test_clean_is_the_unmodified_backtester(self):
        self.assertEqual(fault_presets.build("clean", seed=1, calibration_path=CAL), {})

    def test_clean_managed_injects_nothing_but_manages_failures(self):
        f = fault_presets.build("clean_managed", seed=1, calibration_path=CAL)["hedge_faults"]
        self.assertFalse(f.injects)
        self.assertEqual((f.response, f.entry_guard), ("retry_unwind", True))

    def test_realistic_uses_only_what_the_calibration_measured(self):
        kw = fault_presets.build("realistic", seed=1, calibration_path=CAL)
        f = kw["hedge_faults"]
        self.assertEqual((kw["postonly_reject_model"], kw["ack_latency"].source, kw["ack_latency"].scale), ("on", "empirical", 1.0))
        self.assertEqual((f.reject_prob, f.reject_prob_by_venue, f.latency_scale, f.ack_unknown_prob), (0.0, (), 1.0, 0.0))
        (d,) = f.disconnects
        self.assertEqual((d.venue, d.kind), (BYBIT, "gateway"))
        c = fault_presets.calibration_inputs(CAL)
        self.assertAlmostEqual(d.rate_per_day, c["disconnect_rate_per_day"])
        self.assertEqual(d.durations_s, c["disconnect_durations_s"])

    def test_stressed_is_realistic_made_worse_by_the_stated_factors(self):
        kw = fault_presets.build("stressed", seed=1, calibration_path=CAL)
        f, s = kw["hedge_faults"], fault_presets.STRESS
        c = fault_presets.calibration_inputs(CAL)
        self.assertEqual((kw["ack_latency"].scale, f.latency_scale), (s["latency_x"], s["latency_x"]))
        self.assertAlmostEqual(f.reject_prob, s["reject_prob_x"] * c["reject_prob_upper_95"])
        self.assertEqual(dict(f.reject_prob_by_venue), {BYBIT: f.reject_prob, BINANCE: f.reject_prob})
        self.assertEqual({d.venue for d in f.disconnects}, {BYBIT, BINANCE})
        self.assertTrue(all(d.guaranteed_per_day >= 1 for d in f.disconnects), "at least one long disconnect per day on each venue")
        self.assertGreater(f.fast_move_reject_prob, 0.0)
        self.assertGreater(f.fast_move_bps, 0.0)
        self.assertGreater(f.ack_unknown_prob, 0.0)

    def test_every_non_clean_preset_uses_the_same_response_policy(self):
        for n in ("clean_managed", "realistic", "stressed"):
            f = fault_presets.build(n, seed=1, calibration_path=CAL)["hedge_faults"]
            self.assertEqual((f.response, f.max_retries, f.entry_guard), ("retry_unwind", 2, True), n)

    def test_unknown_preset_is_refused(self):
        with self.assertRaises(ValueError):
            fault_presets.build("nightmare", seed=1, calibration_path=CAL)

    def test_describe_lists_exact_parameters(self):
        d = fault_presets.describe("stressed", seed=3, calibration_path=CAL)
        self.assertEqual(d["hedge_faults"]["seed"], 3)
        self.assertIn("stress_factors", d)
        self.assertEqual(d["ack_latency"]["period"], "all")


class PresetReplayTests(unittest.TestCase):
    """12 synthetic days, the real strategy, hedging on."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name) / "data"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            gen(cls.root, ndays=12, seed=11, session_s=30.0)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def run_preset(self, name, seed, events=False):
        cfg = ArbBacktestConfig(
            data_root=str(self.root), venues=(BYBIT, BINANCE), pair="BTCUSDT", start_date="2026-10-01", end_date="2026-10-12",
            latency_preset="colocated_both", bybit_fee_preset="bybit_perp_mm", binance_fee_preset="binance_perp_vip0",
            maker=MakerConfig(min_queue_ahead=0.8, cancel_model="proportional"),
            arb=ArbConfig(mode=MAKER_TAKER, min_net_edge_bps=1.0, max_clip_notional=2_000.0, max_adverse_prob=0.15, max_ttf_s=8.0,
                          max_cross_venue_skew_s=0.2, fill_gate_horizon_s=5, maker_max_age_s=30.0, count_pending_exposure=True),
            record_exposure=True, record_order_events=events, **fault_presets.build(name, seed=seed, calibration_path=CAL))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            bt = CrossVenueArbBacktester(cfg)
            r = bt.run()
        return bt, r

    @staticmethod
    def key(bt, r):
        return (r.orders_sent, r.orders_filled, r.orders_missed, r.signals_generated, r.gross_pnl, r.fees_paid, r.ending_equity,
                tuple(sorted(r.final_inventory.items())), tuple((x["ts"], x["venue"], x["side"], x["qty"], x["price"], x["source"], x["chain_id"]) for x in bt.trade_log))

    def test_the_same_seed_reproduces_each_preset_exactly_and_another_seed_changes_the_stressed_run(self):
        for name in ("realistic", "stressed"):
            a, b = self.run_preset(name, 3), self.run_preset(name, 3)
            self.assertEqual(self.key(*a), self.key(*b), name)
            self.assertEqual(a[1].hedge_report["counters"], b[1].hedge_report["counters"], name)
            self.assertEqual(a[1].postonly_report["counters"], b[1].postonly_report["counters"], name)
        s3, s4 = self.run_preset("stressed", 3), self.run_preset("stressed", 4)
        self.assertNotEqual((s3[1].hedge_report["counters"], self.key(*s3)), (s4[1].hedge_report["counters"], self.key(*s4)))

    def test_clean_reproduces_the_plain_run_and_labels_nothing_as_injected(self):
        bt, r = self.run_preset("clean", 1, events=True)
        self.assertEqual(r.order_event_summary["by_source"]["injected"], 0)
        self.assertEqual(r.order_event_summary["by_source"]["historical"], 0)
        self.assertGreater(r.order_event_summary["by_source"]["modelled"], 0)
        self.assertEqual(r.hedge_report["faults"], None, "nothing was configured to inject")
        self.assertEqual(sum(r.order_event_summary["by_source"].values()), len(bt.events.events))

    def test_stressed_injects_and_every_event_in_the_run_carries_a_valid_source(self):
        bt, r = self.run_preset("stressed", 5, events=True)
        by = r.order_event_summary["by_source"]
        self.assertGreater(by["injected"], 0)
        self.assertEqual(by["historical"], 0)
        for e in bt.events.events:
            self.assertEqual(e.source == "injected", is_fault_event(e), f"{e.kind} {e.reason} labelled {e.source}")
        require_source(bt.trade_log, "trade_log row")
        self.assertEqual(sum(by.values()), len(bt.events.events))
        # The synthetic days run a 30 s session at a random time; whether a random disconnect overlaps one is chance, so only the
        # generation is asserted here (the effect of a window is tested above). A full-day replay always overlaps its windows.
        self.assertTrue(all(g.windows for g in bt._disc_gens))
        self.assertEqual(len(bt._disc_gens), 2)

    def test_the_postonly_model_on_a_replay_reports_checks_and_a_consistent_reject_count(self):
        bt, r = self.run_preset("realistic", 2)
        po = r.postonly_report
        c = po["counters"]
        self.assertEqual(po["model"], "on")
        self.assertEqual(c.get("checks", 0), c.get("accepted", 0) + c.get("rejects", 0) + bt.miss_reasons.get("not_ready", 0))
        self.assertEqual(c.get("rejects", 0), c.get("rejects_buy", 0) + c.get("rejects_sell", 0))


# ── cancel / fill race (disagreement D-03) ───────────────────────────────────

class CancelFillRaceTests(unittest.TestCase):
    """A fill that executed at the exchange at 1.02 (before the cancel took effect at 1.053) but is delivered at 1.20."""

    def _run(self, basis):
        h = Harness(ttl=1.0, cancel_s=0.05, queue=0.0, cfg_kw={"cancel_time_basis": basis})
        h.bt.latency.aligner.samples = 1
        h.book(-1.0, BYBIT); h.book(-1.0, BINANCE)
        h.send_maker(0.0)
        h.advance(0.01)                                     # rests; expiry requested at 1.01, effective 1.06
        h.sell_trade(1.20, BYBIT, volume=1.0, exchange_ts=1.02)
        h.sell_trade(1.60, BYBIT, volume=0.001, price=50.0, exchange_ts=1.60)   # a later venue event: under "exchange" the cancel lands before it
        h.advance(1.7)
        return h

    def test_receive_time_basis_drops_the_late_delivered_fill_the_legacy_behaviour(self):
        h = self._run("receive")
        self.assertEqual([r for r in rows(h, taker=False)], [])
        self.assertEqual(h.bt.lifetime["cancels_applied"], 1)

    def test_exchange_time_basis_keeps_it_and_books_it_exactly_once(self):
        h = self._run("exchange")
        maker_rows = rows(h, taker=False)
        self.assertEqual(len(maker_rows), 1)
        self.assertAlmostEqual(maker_rows[0]["qty"], 1.0)
        self.assertEqual(h.bt.lifetime["cancels_applied"], 1, "the cancel still lands, after the fill")
        h.assert_ledger_reconciles(self)
        self.assertAlmostEqual(h.bt.inventory.base[BYBIT], 1.0)

    def test_a_fill_executed_after_the_cancel_took_effect_is_never_kept(self):
        h = Harness(ttl=1.0, cancel_s=0.05, queue=0.0, cfg_kw={"cancel_time_basis": "exchange"})
        h.bt.latency.aligner.samples = 1
        h.book(-1.0, BYBIT); h.book(-1.0, BINANCE)
        h.send_maker(0.0)
        h.advance(0.01)
        h.sell_trade(1.20, BYBIT, volume=1.0, exchange_ts=1.10)       # stamped after the effective time 1.06
        self.assertEqual(rows(h, taker=False), [])


if __name__ == "__main__":
    unittest.main()
