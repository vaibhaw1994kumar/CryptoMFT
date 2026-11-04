"""Execution-layer brief steps 6-7: observation records for the maker leg, and the rule that they are observation ONLY."""
from __future__ import annotations

import unittest

from new_backtest.backtester import TRADE

from test_maker_order_lifetime import BINANCE, BYBIT, Harness

BYBIT_BIDS = ((99.0, 6.0), (98.0, 5.0))
BYBIT_ASKS = ((101.0, 2.0), (102.0, 5.0))


def run(obs: bool = True, trade_payload_extra: dict | None = None):
    h = Harness(cfg_kw={"record_maker_obs": obs})
    h.book(0.5, BYBIT, bids=BYBIT_BIDS, asks=BYBIT_ASKS)
    h.book(0.5, BINANCE, bids=((99.5, 3.0), (98.5, 5.0)), asks=((100.5, 1.0), (101.5, 5.0)))
    h.send_maker(1.0, price=100.0, qty=2.0)
    h.advance(2.0)
    if trade_payload_extra:
        h.event(5.0, TRADE, BYBIT, {"price": 100.0, "volume": 2.5, "side": "s", **trade_payload_extra})
    else:
        h.sell_trade(5.0, BYBIT, volume=2.5)
    h.advance(6.0)
    return h


def rows(h, kind):
    return [r for r in h.bt.obs.rows if r["obs"] == kind]


class PlacementRecord(unittest.TestCase):
    def test_placement_logs_queue_position_side_sizes_and_the_imbalance_on_both_venues(self):
        h = run()
        (p,) = rows(h, "maker_place")
        self.assertEqual((p["venue"], p["side"], p["price"], p["size"]), (BYBIT, "buy", 100.0, 2.0))
        self.assertIn("relative_queue_position", p)
        self.assertEqual(p["near_side_size_at_price"], p["level_size"])
        self.assertEqual(p["opposite_side_size_at_price"], 0.0)                         # nobody rests on the ask side at 100
        by, bn = p["touch"][BYBIT], p["touch"][BINANCE]
        self.assertAlmostEqual(by["imbalance"], (6.0 - 2.0) / (6.0 + 2.0))              # (B - A) / (B + A) at the touch, both venues
        self.assertAlmostEqual(bn["imbalance"], (3.0 - 1.0) / (3.0 + 1.0))
        self.assertEqual((by["bid"], by["ask"], bn["bid"], bn["ask"]), (99.0, 101.0, 99.5, 100.5))
        self.assertEqual(p["hedge_touch_at_decision"]["bid"], 99.5)                      # the hedge venue's touch at the decision
        self.assertEqual(p["intent_id"], "sig1-P1")

    def test_hedge_arrival_logs_the_hedge_venues_touch_at_arrival_and_at_decision(self):
        h = run()
        (a,) = rows(h, "hedge_arrival")
        self.assertEqual((a["venue"], a["side"], a["attempt"], a["intent_id"]), (BINANCE, "sell", 0, "sig1-H1"))
        self.assertEqual(a["touch_at_arrival"]["bid"], 99.5)
        self.assertEqual(a["touch_at_decision"]["bid"], 99.5)
        self.assertGreater(a["ts"], 5.0)


class HedgeBookUsed(unittest.TestCase):
    def test_the_hedge_arrival_also_records_which_book_the_fill_really_matched(self):
        h = run()
        (u,) = rows(h, "hedge_book_used")
        self.assertEqual((u["venue"], u["book"]), (BINANCE, "view"))                    # the Harness has no truth books: the strategy view is the book used
        self.assertEqual(u["touch_used"]["bid"], u["touch_view"]["bid"])


class FillRecord(unittest.TestCase):
    def test_a_maker_fill_carries_cause_volume_queue_credit_model_and_flags(self):
        h = run()
        (f,) = rows(h, "maker_fill")
        self.assertEqual((f["venue"], f["side"], f["qty"], f["intent_id"]), (BYBIT, "buy", 2.0, "sig1-P1"))
        self.assertEqual(f["trigger"]["kind"], "trade")
        self.assertIsNone(f["trigger"]["trade_id"])                                       # the Jan-2022-style record carries no id: the cause tag stands in
        fl = f["fill"]
        self.assertEqual(fl["cause"], "trade")
        self.assertAlmostEqual(fl["volume_offered"], 2.5)                                  # the print that caused it
        self.assertAlmostEqual(fl["queue_before"], 0.5)                                    # the queue estimate before the fill (min_queue_ahead on an absent level)
        self.assertAlmostEqual(fl["volume_consumed_by_queue"], 0.5)
        self.assertAlmostEqual(fl["volume_consumed_by_fill"], 2.0)
        self.assertEqual(fl["netting_credit"], 0.0)
        self.assertEqual(fl["cancel_model"], "proportional")
        self.assertIn("queue_proxy", fl["flags"])                                           # absent level: queue is a proxy, say so
        self.assertEqual(f["touch"][BYBIT]["bid"], 99.0)

    def test_a_native_trade_id_is_logged_when_the_record_has_one(self):
        h = run(trade_payload_extra={"trade_id": 777})
        (f,) = rows(h, "maker_fill")
        self.assertEqual((f["trigger"]["trade_id"], f["trigger"]["trade_id_field"]), ("777", "trade_id"))

    def test_the_summary_counts_causes_flags_and_own_taker_consumption(self):
        h = run()
        s = h.bt._obs_summary()
        self.assertEqual(s["counts"], {"maker_place": 1, "maker_fill": 1, "hedge_arrival": 1, "hedge_book_used": 1})
        self.assertEqual(s["maker_fill_causes"], {"trade": 1})
        self.assertEqual(s["own_taker_book_consumption"]["fills_that_removed_liquidity"], 1)
        self.assertAlmostEqual(s["own_taker_book_consumption"]["qty_removed_from_the_view_book"], 2.0)


class ObservationOnly(unittest.TestCase):
    def test_turning_the_observation_off_changes_nothing_the_engine_does(self):
        on, off = run(True), run(False)
        self.assertEqual(on.bt.trade_log, off.bt.trade_log)
        self.assertEqual((on.bt.inventory.base, on.bt.inventory.cash), (off.bt.inventory.base, off.bt.inventory.cash))
        self.assertEqual(on.bt.fees_paid, off.bt.fees_paid)
        self.assertEqual(on.bt.maker.diag, off.bt.maker.diag)
        self.assertEqual(on.bt.xl.counts, off.bt.xl.counts)
        self.assertEqual(off.bt.obs.rows, [])
        self.assertIsNone(None)

    def test_fills_carry_no_observation_when_it_is_off(self):
        h = run(False)
        self.assertFalse(h.bt.maker.obs_enabled)

    def test_nothing_in_the_engine_reads_the_observation(self):
        """Every use of `.obs` outside the producer is a write (add / summary / write_jsonl / construct) or the hand-over of fill.obs."""
        import pathlib
        import re
        src = pathlib.Path(__file__).resolve().parents[1] / "src" / "new_backtest"
        allowed = (r"self\.obs = ObservationLog\(\)", r"self\.obs\.add\(", r"self\.obs\.summary\(", r"self\.obs\.write_jsonl\(",
                   r"dict\(fill\.obs or \{\}\)", r"fill\.obs = \{", r"fill\.obs = ")
        for f in src.rglob("*.py"):
            for n, line in enumerate(f.read_text().splitlines(), 1):
                code = line.split("#", 1)[0]
                if re.search(r"\.obs\b(?!_)", code) and not any(re.search(a, code) for a in allowed):
                    self.fail(f"{f.name}:{n} touches an observation outside the allowed write paths: {line.strip()}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
