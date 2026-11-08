"""Phase D, step D5: the session replay end to end on a synthetic stream (wiring only, never a profit figure). The strategy signals through the shim, the core router sends a PostOnly maker
bid after its entry latency, a trade print through the bid fills it (queue adapter, evidence from a print), FILL_TRIGGERED hedges on the other venue, and the ledger carries both legs."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from new_backtest.cross_venue_feed import TimedEvent
from new_backtest.event_loader import BOOK, TRADE
from new_backtest.profiles import apply_profile, load_profile
from new_backtest.session_replay import SessionReplay

SLICES = Path(__file__).resolve().parents[2] / "out" / "queue_v2" / "slices_mar2024"


def timed(t, venue, kind, payload):
    return TimedEvent(t + 0.002, t, venue, "SOLUSDT", kind, payload)


def snap(t, venue, bid, ask):
    return timed(t, venue, BOOK, {"timestamp": t, "bids": [[str(bid), "50"], [str(round(bid - 0.01, 2)), "50"]], "asks": [[str(ask), "50"], [str(round(ask + 0.01, 2)), "50"]]})


def stream():
    ev = [snap(1.0, "bybit", 100.00, 100.30), snap(1.0, "binance", 100.30, 100.31)]
    t = 1.1
    for _ in range(40):                                                      # both books fresh (the gate marks a book stale after 0.5 s)
        ev += [snap(t, "binance", 100.30, 100.31), snap(t + 0.01, "bybit", 100.00, 100.30)]
        t += 0.1
    ev.append(timed(3.0, "bybit", TRADE, {"exchange_ts": 3.0, "price": "100.24", "volume": "30", "side": "sell"}))     # a sell print through the resting bid at 100.247
    for _ in range(40):
        ev += [snap(t, "binance", 100.30, 100.31), snap(t + 0.01, "bybit", 100.00, 100.30)]
        t += 0.1
    return sorted(ev, key=lambda e: (e.exchange_ts, e.venue))


@unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
class SessionReplayEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = load_profile("perp_perp_maker_taker_v1_5")
        cfg = apply_profile(cls.profile, dataset="mar2024", data_root=str(SLICES), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_d5_syn")
        cfg.arb.max_position_base = 1.0
        cls.replay = SessionReplay(cfg, cls.profile, stream=stream())
        cls.result = cls.replay.run()

    def test_the_maker_bid_is_filled_by_a_print_and_hedged_on_the_other_venue(self):
        r = self.result
        by_venue = {o["venue"]: o for o in r["orders"] if o["state"] == "FILLED"}
        self.assertEqual((by_venue["BYBIT"]["side"], by_venue["BYBIT"]["post_only"], by_venue["BYBIT"]["limit_price"]), ("BUY", True, "100.247"))
        self.assertEqual((by_venue["BINANCE"]["side"], by_venue["BINANCE"]["post_only"]), ("SELL", False))
        self.assertEqual(r["fills_by_source"]["BYBIT"], {"trade": 1})                       # a fill needs a real print
        self.assertEqual(r["positions"], {"BYBIT:SOL-USDT-PERP": "1.0", "BINANCE:SOL-USDT-PERP": "-1.0"})
        self.assertEqual(len(r["fills"]), 2)

    def test_the_opportunity_carries_the_books_and_eligibility_the_router_checks(self):
        """Regression (found wiring D5): an opportunity with no source event ids or eligibility fingerprint is REJECTED by the router as 'market state changed'."""
        plans = [p for p in self.replay.session.oms.plans.values()]
        self.assertTrue(plans)
        self.assertNotIn("REJECTED", {p.state.value for p in plans})

    def test_the_manifest_labels_the_policy_the_sequence_and_the_differences(self):
        m = self.result["manifest"]
        self.assertEqual((m["invalid_book_policy"]["resync_delay_label"], m["feed"]["sequence_numbers"], m["feed"]["sequence_gaps"]["status"]), ("ASSUMED", "SYNTHETIC", "NOT_MEASURED"))
        self.assertIn("suspension_intervals_ns", m["invalid_book_policy"])
        self.assertTrue(m["differences_from_backtester_loop"])
        self.assertTrue(self.result["label"].startswith("EXPLORATORY"))


if __name__ == "__main__":
    unittest.main()
