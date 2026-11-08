"""Phase D, D5c: rules the session path needed to run the same as the backtester's loop. Wiring checks on synthetic input, never a profit figure."""
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crypto_stat_arb.hedge_lifecycle import touch_slack_price
from new_backtest.cross_venue_feed import TimedEvent
from new_backtest.event_loader import BOOK
from new_backtest.profiles import apply_profile, load_profile
from new_backtest.session_replay import SessionReplay

SLICES = Path(__file__).resolve().parents[2] / "out" / "queue_v2" / "slices_mar2024"


class HedgeCap(unittest.TestCase):
    def test_touch_slack_is_the_touch_moved_against_us_and_the_ems_layer_uses_the_same_formula(self):
        self.assertEqual(touch_slack_price("sell", Decimal("100"), Decimal("5")), Decimal("99.95"))
        self.assertEqual(touch_slack_price("buy", Decimal("100"), Decimal("5")), Decimal("100.05"))
        from new_backtest.execution.layer import ExecutionLayer          # the re-export of crypto_stat_arb.ems.layer
        from new_backtest.execution import ExecutionConfig
        layer = ExecutionLayer(ExecutionConfig(), instrument="SOLUSDT")
        self.assertAlmostEqual(layer.touch_slack_cap("sell", 100.0, 2.0), float(touch_slack_price("sell", 100.0, 5.0)))      # max(2 bps, the 5 bps floor)


def timed(t, venue, bid, ask):
    return TimedEvent(t + 0.002, t, venue, "SOLUSDT", BOOK, {"timestamp": t, "bids": [[str(bid), "50"], [str(round(bid - 0.01, 2)), "50"]], "asks": [[str(ask), "50"], [str(round(ask + 0.01, 2)), "50"]]})


@unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
class MakerLifetimeAndIocReport(unittest.TestCase):
    def _run(self, **arb):
        profile = load_profile("perp_perp_maker_taker_v1_5")
        cfg = apply_profile(profile, dataset="mar2024", data_root=str(SLICES), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_d5c")
        cfg.arb.max_position_base = 1.0
        for k, v in arb.items():
            setattr(cfg.arb, k, v)
        stream, t = [timed(1.0, "bybit", 100.00, 100.30), timed(1.0, "binance", 100.30, 100.31)], 1.05
        for _ in range(120):                                              # no print ever reaches the resting bid
            stream += [timed(t, "binance", 100.30, 100.31), timed(t + 0.01, "bybit", 100.00, 100.30)]
            t += 0.05
        replay = SessionReplay(cfg, profile, stream=sorted(stream, key=lambda e: (e.exchange_ts, e.venue)))
        return replay, replay.run()

    def test_a_resting_maker_order_is_cancelled_a_lifetime_after_the_venue_accepted_it(self):
        replay, result = self._run(maker_max_age_s=2.0)
        first = result["orders"][0]
        self.assertEqual((first["venue"], first["state"]), ("BYBIT", "CANCELLED"))
        expired = [e for e in replay.session.ledger.events if e["event_type"] == "ORDER_LIFETIME_EXPIRED"]
        ack = next(e for e in replay.session.ledger.events if e["event_type"] == "ORDER_STATE_UPDATE_APPLIED" and e["order_id"] == first["order_id"]
                   and e["update"]["state"] == "ACKNOWLEDGED")
        self.assertEqual(expired[0]["ts_ns"], ack["update"]["event_ts_ns"] + 2_000_000_000)           # at its own deadline, not at the next event

    def test_without_a_lifetime_the_order_rests_on(self):
        replay, result = self._run(maker_max_age_s=0.0)
        self.assertEqual(result["orders"][0]["state"], "ACKNOWLEDGED")
        self.assertFalse([e for e in replay.session.ledger.events if e["event_type"] == "ORDER_LIFETIME_EXPIRED"])


if __name__ == "__main__":
    unittest.main()
