"""Queue model v2, step 10: the invariants the guide requires (s9) and the independent cases. The four mutation checks (credit expiry, a vanished level that
fills, a print spent twice, an own fill that mutates the public book) are run by out/queue_v2/mutations_qv2.py against these files."""
from __future__ import annotations

import gzip
import json
import random
import tempfile
import unittest
from pathlib import Path

from qv2_support import delta, make_book, make_executor, place, sell_print

from new_backtest.event_loader import iter_venue_events
from new_backtest.models import Side

from test_maker_observations import BINANCE, BYBIT, Harness, rows, run
from test_public_book_invariant import both, differences, feed, pair

BASE = dict(fill_evidence="off", dedupe_trade_depth=True, share_trade_volume=True, exchange_time_eligibility=True)
COHORT = dict(queue_model="cohort", cohort_age="uniform")


def write(root: Path, venue: str, ch: str, day: str, recs: list[dict], name="1641340800000.jsonl.gz") -> None:
    d = root / venue / ch / "pair=SOLUSDT" / f"date={day}"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(gzip.compress(("\n".join(json.dumps(r) for r in recs) + "\n").encode()))


class FuzzInvariants(unittest.TestCase):
    """Random depth/print streams against both queue representations: nothing negative, filled + remaining = size, a print never fills more than it holds."""
    def _fuzz(self, cfg, seed):
        rng = random.Random(seed)
        ex = make_executor(**cfg)
        book = make_book(bids=[(100.0, rng.uniform(1, 20)), (99.0, 50.0)])
        orders = [place(ex, book, price=100.0, size=rng.uniform(0.5, 4.0), now=1.0 + i * 0.01) for i in range(rng.randint(1, 3))]
        level, t = book.level_size("bid", 100.0), 2.0
        for _ in range(60):
            t += rng.choice([0.001, 0.2, 1.0])
            if rng.random() < 0.5:
                level = max(0.0, level + rng.uniform(-6, 6))
                delta(ex, book, "bid", 100.0, level, t)
            else:
                v = rng.uniform(0.01, 12)
                got = sum(f.qty for f in sell_print(ex, book, 100.0, v, t))
                self.assertLessEqual(got, v + 1e-9, "a print filled more than it held")
            for o in orders:
                self.assertGreaterEqual(o.queue_ahead, -1e-9)
                self.assertGreaterEqual(o.remaining, -1e-9)
                self.assertAlmostEqual(o.filled_qty + o.remaining, o.size, places=9)

    def test_scalar_models(self):
        for model in ("proportional", "behind_first", "ahead_first", "all_ahead"):
            for seed in range(25):
                self._fuzz({**BASE, "cancel_model": model}, seed)

    def test_cohort_models(self):
        for age in ("youngest_first", "oldest_first", "uniform", "hazard"):
            for seed in range(25):
                self._fuzz({**BASE, "queue_model": "cohort", "cohort_age": age}, seed)

    def test_filled_plus_remaining_equals_the_original_after_an_amendment(self):
        ex = make_executor(**BASE)
        book = make_book(bids=[(100.0, 1.0), (99.0, 50.0)])
        o = place(ex, book, price=100.0, size=3.0)
        sell_print(ex, book, 100.0, 2.5, 2.0)                                           # 1.0 ahead + 1.5 filled
        self.assertAlmostEqual(o.filled_qty, 1.5)
        res = ex.amend(o.order_id, 99.0, 4.0, book=book, state=__import__("qv2_support").State(), regime_bucket=o.regime_bucket, now=3.0, active_ts=3.0,
                       tick_size=1.0, postonly=True)
        self.assertTrue(res.old_cancelled)
        self.assertAlmostEqual(o.filled_qty + o.remaining, 3.0)                         # the cancelled order keeps its own books
        self.assertAlmostEqual(res.order.size, 4.0)
        self.assertAlmostEqual(res.order.filled_qty + res.order.remaining, 4.0)


class IndependentCases(unittest.TestCase):
    def test_cancel_race_the_order_still_fills_until_the_cancel_takes_effect(self):
        h = Harness(cancel_s=0.5)
        h.book(0.5, BYBIT)
        h.book(0.5, BINANCE)
        h.send_maker(1.0)
        h.advance(2.0)
        o = h.order()
        h.bt._request_cancel(o, 2.0, "expired")                                          # cancel sent at 2.0, effective at 2.5
        h.sell_trade(2.2, BYBIT, volume=5.0)                                             # a print inside the race window
        self.assertGreater(o.filled_qty, 0.0)                                            # the cancel-pending order was still eligible

    def test_an_empty_level_inside_the_captured_depth_has_queue_zero(self):
        ex = make_executor(unknown_queue_model="depth_aware", unknown_queue_floor=25.0, **BASE)
        book = make_book(bids=[(100.0, 5.0), (99.0, 5.0), (98.0, 5.0)])                  # 99.5 is inside the depth and empty
        o = place(ex, book, price=99.5, size=1.0, tick=0.5)
        self.assertEqual((o.queue_ahead, o.queue_unknown), (0.0, False))

    def test_a_price_outside_the_captured_depth_is_unknown_and_flagged(self):
        ex = make_executor(unknown_queue_model="depth_aware", unknown_queue_floor=25.0, **BASE)
        book = make_book(bids=[(100.0, 5.0), (99.0, 5.0)])
        o = place(ex, book, price=90.0, size=1.0)
        self.assertTrue(o.queue_unknown)
        self.assertEqual(o.queue_ahead, 25.0)

    def test_a_fitted_unknown_queue_is_seeded_per_order_and_deterministic(self):
        with tempfile.TemporaryDirectory() as d:
            fit = Path(d) / "fit.json"
            fit.write_text(json.dumps({"venues": {"bybit": {"tick": 1.0, "buckets": [{"d_lo": 1, "d_hi": 10 ** 6, "n": 100, "median": 20.0, "sigma": 0.5}] * 7}}}))
            qs = []
            for _ in range(2):
                ex = make_executor(unknown_queue_model="depth_aware", unknown_queue_dist="fitted", unknown_queue_fit=str(fit), unknown_queue_floor=0.0,
                                   unknown_queue_seed=7, **BASE)
                book = make_book(bids=[(100.0, 5.0), (99.0, 5.0)])
                qs.append([place(ex, book, price=p, size=1.0, now=1.0 + k).queue_ahead for k, p in enumerate((90.0, 89.0, 88.0))])
            self.assertEqual(qs[0], qs[1])
            self.assertTrue(len(set(qs[0])) == 3 and all(q > 0 for q in qs[0]))        # three orders, three different draws

    def test_a_missing_venue_in_the_fit_falls_back_to_the_floor_and_says_so(self):
        with tempfile.TemporaryDirectory() as d:
            fit = Path(d) / "fit.json"
            fit.write_text(json.dumps({"venues": {"bybit": {"tick": 1.0, "buckets": [{"d_lo": 1, "d_hi": 9, "n": 0, "median": None, "sigma": None}] * 7}}}))
            ex = make_executor(unknown_queue_model="depth_aware", unknown_queue_dist="fitted", unknown_queue_fit=str(fit), unknown_queue_floor=25.0, **BASE)
            book = make_book(bids=[(100.0, 5.0), (99.0, 5.0)])
            o = place(ex, book, price=90.0, size=1.0)
            self.assertEqual(o.queue_ahead, 25.0)
            self.assertEqual(ex.diag["unknown_queue_fit_missing_venue_used_floor"], 1.0)

    def test_negative_zero_and_huge_units(self):
        ex = make_executor(**BASE)
        book = make_book(bids=[(100.0, 5.0), (99.0, 5.0)])
        o = place(ex, book, price=100.0, size=2.0)
        self.assertEqual(sell_print(ex, book, 100.0, -3.0, 2.0), [])                    # a negative print is ignored by the caller's guard, never a fill
        self.assertEqual(sell_print(ex, book, 100.0, 0.0, 2.1), [])
        fills = sell_print(ex, book, 100.0, 1e12, 3.0)                                   # a huge print fills at most the order
        self.assertAlmostEqual(sum(f.qty for f in fills), 2.0)
        self.assertGreaterEqual(o.remaining, 0.0)

    def test_iceberg_like_replenishment_the_refill_joins_behind_us(self):
        ex = make_executor(**BASE)
        book = make_book(bids=[(100.0, 6.0), (99.0, 50.0)])
        o = place(ex, book, price=100.0, size=2.0)
        sell_print(ex, book, 100.0, 4.0, 2.0)                                            # drains 4 of the 6 ahead
        delta(ex, book, "bid", 100.0, 6.0, 2.3)                                          # the displayed size is refilled to 6: 4 new units BEHIND us
        self.assertAlmostEqual(o.queue_ahead, 2.0)                                       # the refill never pushes us back

    def test_truncated_data_yields_the_records_up_to_the_cut_and_nothing_after(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            recs = [{"exchange_ts": 1641340800.0 + i, "local_ts": 1641340800.0 + i, "price": 1.0, "volume": 1.0, "side": "b"} for i in range(2000)]
            p = root / "bybit" / "trades" / "pair=SOLUSDT" / "date=2022-01-05"
            p.mkdir(parents=True)
            raw = gzip.compress(("\n".join(json.dumps(r) for r in recs) + "\n").encode(), compresslevel=1)
            (p / "1641340800000.jsonl.gz").write_bytes(raw[: len(raw) // 2])
            got = list(iter_venue_events(root, "bybit", "SOLUSDT", "2022-01-05", "2022-01-05", include_snapshots=False, include_deltas=False))
            self.assertTrue(0 < len(got) < 2000)
            self.assertEqual([e.exchange_ts for e in got], sorted(e.exchange_ts for e in got))


class EventOrderAndDedupe(unittest.TestCase):
    def _tree(self, d):
        root, t = Path(d), 1641340800.0
        write(root, "bybit", "book_snapshots", "2022-01-05", [{"timestamp": t + 1, "local_ts": t + 1.5, "bids": [[100.0, 1.0]], "asks": [[101.0, 1.0]]}])
        write(root, "bybit", "book_events", "2022-01-05", [{"exchange_ts": t + 1, "local_ts": t + 1.4, "side": "bid", "price": 100.0, "volume": 2.0, "event_type": "modify"}])
        write(root, "bybit", "trades", "2022-01-05", [{"exchange_ts": t + 1, "local_ts": t + 1.3, "price": 100.0, "volume": 1.0, "side": "s"}])
        return root

    def test_legacy_policy_is_snapshot_then_delta_then_trade_and_it_is_pinned(self):
        with tempfile.TemporaryDirectory() as d:
            kinds = [e.kind for e in iter_venue_events(self._tree(d), "bybit", "SOLUSDT", "2022-01-05", "2022-01-05")]
            self.assertEqual(kinds, ["book", "book_event", "trade"])

    def test_capture_policy_orders_by_exchange_then_local_time_and_not_by_kind(self):
        with tempfile.TemporaryDirectory() as d:
            kinds = [e.kind for e in iter_venue_events(self._tree(d), "bybit", "SOLUSDT", "2022-01-05", "2022-01-05", event_order="capture")]
            self.assertEqual(kinds, ["trade", "book_event", "book"])                      # local receive times 1.3 < 1.4 < 1.5

    def test_capture_policy_keeps_file_order_inside_a_stream_when_both_clocks_tie(self):
        with tempfile.TemporaryDirectory() as d:
            root, t = Path(d), 1641340800.0
            recs = [{"exchange_ts": t, "local_ts": t, "price": 100.0 + i, "volume": 1.0, "side": "b"} for i in range(5)]
            write(root, "bybit", "trades", "2022-01-05", recs)
            prices = [e.payload["price"] for e in iter_venue_events(root, "bybit", "SOLUSDT", "2022-01-05", "2022-01-05", include_snapshots=False,
                                                                     include_deltas=False, event_order="capture", trade_dedupe="multiset")]
            self.assertEqual(prices, [100.0, 101.0, 102.0, 103.0, 104.0])

    def test_an_unknown_event_order_is_an_error(self):
        with self.assertRaises(ValueError):
            list(iter_venue_events("/nonexistent", "bybit", "SOLUSDT", "2022-01-05", "2022-01-05", event_order="random"))

    def test_dedupe_keeps_identical_prints_with_different_ids_and_drops_a_repeated_delivery_of_the_same_id(self):
        with tempfile.TemporaryDirectory() as d:
            root, t = Path(d), 1641340800.0
            same = {"exchange_ts": t, "local_ts": t, "price": 100.0, "volume": 1.0, "side": "b"}
            a = [{**same, "trade_id": 1}, {**same, "trade_id": 2}]                          # two genuine prints, identical content, different ids
            b = [{**same, "trade_id": 1}, {**same, "trade_id": 2}, {**same, "trade_id": 2}]  # a second collector delivers them again (id 2 twice in this file)
            write(root, "bybit", "trades", "2022-01-05", a, name="a.jsonl.gz")
            write(root, "bybit", "trades", "2022-01-05", b, name="b.jsonl.gz")
            got = [e.payload["trade_id"] for e in iter_venue_events(root, "bybit", "SOLUSDT", "2022-01-05", "2022-01-05", include_snapshots=False, include_deltas=False)]
            self.assertEqual(sorted(got), [1, 2])                                           # each id once; the identical content with a different id is NOT merged


class OwnFillsAndTheOverlay(unittest.TestCase):
    def _run(self, impact):
        a, b = Harness(cfg_kw={"own_impact": impact}), Harness(cfg_kw={"own_impact": impact})
        feed(a, b)
        a.send_maker(1.0, qty=2.0)
        both(a, b, lambda h: h.sell_trade(5.0, BYBIT, volume=2.5))
        both(a, b, lambda h: h.advance(6.0))
        return a, b

    def test_with_the_overlay_an_own_taker_fill_never_mutates_the_public_book(self):
        a, b = self._run("overlay")
        self.assertEqual(a.bt.hedge["hedges_filled"], 1)
        self.assertEqual(differences(a, b), [])                                           # both venues, both sides: equal to the public reconstruction
        self.assertAlmostEqual(a.bt.taker.consumed_qty, 2.0)
        self.assertAlmostEqual(sum(a.bt.books[BINANCE].overlay_bid.values()), 2.0)         # our consumption lives in the overlay

    def test_the_overlay_hides_what_we_took_from_our_next_walk_and_is_cleared_by_the_next_public_update(self):
        a, _ = self._run("overlay")
        book = a.bt.books[BINANCE]
        top = book.best_bid
        full = dict(book.levels("bid", 10))[top]
        self.assertAlmostEqual(book.walk("bid", 100.0).filled_qty, sum(dict(book.levels("bid", 10)).values()) - 2.0)   # 2 already taken
        book.apply_delta("bid", top, full, "modify", 7.0)                                  # a public update of that price level
        self.assertEqual(book.overlay_bid.get(top, 0.0), 0.0)
        self.assertAlmostEqual(book.walk("bid", 100.0).filled_qty, sum(dict(book.levels("bid", 10)).values()))

    def test_the_legacy_default_still_mutates_the_view_book_and_says_so(self):
        a, b = self._run("legacy")
        self.assertNotEqual(differences(a, b, venues=(BINANCE,)), [])

    def test_the_maker_print_budget_is_recorded_in_the_overlay_and_cleared_at_the_next_update(self):
        h = Harness(cfg_kw={"own_impact": "overlay"})
        h.book(0.5, BYBIT)
        h.book(0.5, BINANCE)
        h.send_maker(1.0, qty=2.0)
        h.sell_trade(5.0, BYBIT, volume=2.5)
        self.assertAlmostEqual(sum(h.bt.maker.print_overlay.values()), 2.0)
        h.event(6.0, "book_event", BYBIT, {"side": "bid", "price": 100.0, "volume": 3.0, "event_type": "modify"})
        self.assertEqual(h.bt.maker.print_overlay, {})

    def test_provenance_ids_are_model_labels_never_native_ids(self):
        h = run()
        (f,) = rows(h, "maker_fill")
        self.assertTrue(f["fill"]["order_model_id"].startswith("qv2:"))
        self.assertEqual(f["fill"]["id_kind"], "model_label_not_native")
        self.assertEqual(len(f["fill"]["model_settings_sha256"]), 64)
        self.assertTrue({"arrival_ts", "match_ts", "report_ts", "source_type", "queue_before", "volume_offered", "netting_credit", "flags"} <= set(f["fill"]))


class ThroughPrints(unittest.TestCase):
    def _fill(self, **cfg):
        ex = make_executor(fill_through_prints=True, **BASE, **cfg)
        book = make_book(bids=[(100.0, 5.0), (99.0, 5.0)])
        a = place(ex, book, price=100.0, size=3.0, now=1.0)
        b = place(ex, book, price=100.0, size=3.0, now=1.1)
        fills = ex.on_trade(book.venue, 99.0, 4.0, -1, 2.0, 0.0, event_ts=2.0)           # a sell at 99 trades THROUGH our bids at 100
        return fills, a, b

    def test_a_through_print_fills_in_full_by_default(self):
        fills, a, b = self._fill()
        self.assertEqual(sorted(f.cause for f in fills), ["trade_through", "trade_through"])
        self.assertAlmostEqual(sum(f.qty for f in fills), 6.0)                            # more than the 4 the print held: the optimistic rule

    def test_the_volume_limited_rule_never_fills_more_than_the_print_and_tags_the_cause(self):
        fills, a, b = self._fill(through_print_volume_limited=True)
        self.assertLessEqual(sum(f.qty for f in fills), 4.0 + 1e-9)
        self.assertTrue(all(f.cause == "trade_through" for f in fills))


class ThroughPrintSpentOnce(unittest.TestCase):
    """Step 8 re-check (owner 7 Nov): one print is spent ONCE across the through rule, the cross rule and the ordinary trade rule, in both arrival orders, and a print that a depth
    decrease already explained still reaches the through rule. Settings: the v1_2 maker block with the volume-limited through rule switched on (the profile has it off for now).
    The four tests that were OPEN (7 Nov re-check) pass since the owner's 7 Nov decision and the engine change: one print budget across the cross, through and ordinary rules; a netted print still reaches the through rule."""
    CFG = dict(BASE, cancel_model="proportional", fill_through_prints=True, through_print_volume_limited=True, opposite_cross_fills=True, opposite_cross_confirm_ms=50.0,
               opposite_cross_cap_to_print=True, dedupe_window_s=0.05, dedupe_time_basis="exchange", min_queue_ahead=0.0)

    def _ex(self, bids, orders):
        ex = make_executor(**self.CFG)
        book = make_book(bids=bids)
        placed = [place(ex, book, price=p, size=sz, now=1.0) for p, sz in orders]
        return ex, book, placed

    def _cross_to(self, ex, book, price, now):
        book.apply_delta("ask", 101.0, 0.0, "modify", now)
        book.apply_delta("ask", price, 5.0, "modify", now)                              # the best ask is now at `price`: every bid at or above it is crossed
        return ex.on_opposite_cross(book.venue, book, now, 0.0, event_ts=now)

    # -- a print the book already explained still reaches the through rule --
    def _netted(self, first):
        # our bid A at 100 (through candidate) and our bid B at 99, behind 5 on the level the print hits; a sell of 4 at 99 and the depth decrease 5 -> 1 at 99 are the same execution
        ex, book, (a, b) = self._ex([(100.0, 5.0), (99.0, 5.0)], [(100.0, 3.0), (99.0, 3.0)])
        if first == "book":
            fills = delta(ex, book, "bid", 99.0, 1.0, 2.000)
            fills += ex.on_trade(book.venue, 99.0, 4.0, -1, 2.010, 0.0, event_ts=2.010)
        else:
            fills = ex.on_trade(book.venue, 99.0, 4.0, -1, 2.000, 0.0, event_ts=2.000)
            fills += delta(ex, book, "bid", 99.0, 1.0, 2.010)
        return sum(f.qty for f in fills if f.order_id == a.order_id), a, b

    def test_print_first_the_through_rule_fills_the_order_through_which_the_print_went(self):
        got, a, b = self._netted("trade")
        self.assertAlmostEqual(got, 3.0)                                                 # a sell at 99 went through our bid at 100
        self.assertEqual(b.filled_qty, 0.0)                                              # B only drained 4 of its 5 ahead

    def test_book_first_a_print_netted_against_the_depth_decrease_is_still_seen_by_the_through_rule(self):
        got, a, b = self._netted("book")
        self.assertAlmostEqual(got, 3.0)                                                 # today 0: the netted print returns before the through rule runs, so the two arrival orders differ

    # -- the through rule and the ordinary rule share one print --
    def _through_and_ordinary(self, first):
        # bid A at 100 (through candidate) and bid B at 99 on an empty level (at the print's price): a sell of 4 at 99
        ex, book, (a, b) = self._ex([(98.0, 50.0)], [(100.0, 3.0), (99.0, 3.0)])
        if first == "book":
            fills = delta(ex, book, "bid", 99.0, 0.0, 2.000)                              # a decrease at 99 that shrinks nothing of ours (the level was absent)
            fills += ex.on_trade(book.venue, 99.0, 4.0, -1, 2.010, 0.0, event_ts=2.010)
        else:
            fills = ex.on_trade(book.venue, 99.0, 4.0, -1, 2.000, 0.0, event_ts=2.000)
        return fills

    def test_one_print_is_spent_once_across_the_through_rule_and_the_ordinary_rule_print_first(self):
        self.assertLessEqual(sum(f.qty for f in self._through_and_ordinary("trade")), 4.0 + 1e-9)       # today 6: the through budget is the gross print, B's ordinary fill spends it again

    def test_one_print_is_spent_once_across_the_through_rule_and_the_ordinary_rule_after_a_decrease(self):
        self.assertLessEqual(sum(f.qty for f in self._through_and_ordinary("book")), 4.0 + 1e-9)

    # -- the through rule and the cross rule share one print --
    def _through_and_cross(self, first):
        # A at 100 is crossed by the opposite quote; C at 99.5 is not. A sell of 4 at 99 is through both.
        ex, book, (a, c) = self._ex([(98.0, 50.0)], [(100.0, 3.0), (99.5, 3.0)])
        fills = []
        if first == "cross":
            fills += self._cross_to(ex, book, 100.0, 1.990)                                # armed: waits for a reaching print
            fills += ex.on_trade(book.venue, 99.0, 4.0, -1, 2.000, 0.0, event_ts=2.000)
        else:
            fills += ex.on_trade(book.venue, 99.0, 4.0, -1, 1.980, 0.0, event_ts=1.980)
            fills += self._cross_to(ex, book, 100.0, 1.990)
        return fills, a, c

    def test_print_first_then_cross_the_print_is_spent_once(self):
        fills, a, c = self._through_and_cross("trade")
        self.assertLessEqual(sum(f.qty for f in fills), 4.0 + 1e-9)

    def test_cross_first_then_print_the_print_is_spent_once(self):
        fills, a, c = self._through_and_cross("cross")
        self.assertLessEqual(sum(f.qty for f in fills), 4.0 + 1e-9)                      # today 6: the cross cap is spent on A, the through budget for C starts again at the gross print

    def test_a_cross_pending_order_is_filled_once_by_the_print_that_confirms_it(self):
        fills, a, c = self._through_and_cross("cross")
        self.assertEqual([f.order_id for f in fills].count(a.order_id), 1)
        self.assertAlmostEqual(a.filled_qty, 3.0)

if __name__ == "__main__":  # pragma: no cover
    unittest.main()
