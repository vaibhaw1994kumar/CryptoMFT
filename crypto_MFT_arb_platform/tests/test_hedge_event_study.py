"""S2 hedge event study (owner 9 Nov): the hedge chain of `new_backtest.market_hedge_move.HedgeStudy` on hand-built books: the touch-slack cap is re-taken from the touch at each dispatch, the break-even-plus-buffer cap is ONE fixed limit,
a retry follows the profile's backoff, a chain with no fill is unwound on the maker venue and its cost is in the figures (a failed chain cannot vanish from the mean)."""
import unittest
from types import SimpleNamespace

from new_backtest.market_hedge_move import HedgeStudy, STUDY_VARIANTS


class Book:
    ready = True
    last_update_ts = 0.0

    def __init__(self, bid, ask, bid_size=5.0, ask_size=5.0):
        self.best_bid, self.best_ask, self.best_bid_size, self.best_ask_size = bid, ask, bid_size, ask_size


def study():
    setup = {"arb": SimpleNamespace(), "tick": 0.001, "max_walk_bps": 2.0, "retries": 2, "backoff_s": 0.25, "maker_bps": 1.0, "hedge_taker_bps": 1.7, "maker_venue_taker_bps": 5.0, "unwind_band_bps": 15.0}
    return HedgeStudy(setup)


def chain(st, variant, side="bid", q=100.0):
    ev = {"variants": {}}
    return {"variant": variant, "side": side, "q": q, "t0": 0.0, "submits": 0, "event": ev}, ev


def play(st, ch, books):
    """books: one (hedge_book, maker_book) per action, in the order the chain asks for them."""
    st.hedge_book, st.maker_book = books[0]
    st._dispatch(0.0, ch)
    i = 0
    while st.heap and "done" not in ch:
        t, _, kind, c = st.hq.heappop(st.heap)
        i = min(i + 1, len(books) - 1)
        st.hedge_book, st.maker_book = books[i]
        getattr(st, "_" + kind)(t, c)


class HedgeChainTests(unittest.TestCase):
    def test_slack_cap_fills_on_the_first_attempt_and_costs_the_drift_plus_fees(self):
        st = study()
        ch, ev = chain(st, "slack5")
        play(st, ch, [(Book(100.00, 100.01), Book(99.99, 100.0)), (Book(99.96, 99.97), Book(99.99, 100.0))])
        d = ev["variants"]["slack5"]
        self.assertEqual((d["outcome"], d["submits"], d["first_attempt_fill"]), ("hedged", 1, True))
        self.assertAlmostEqual(d["time_to_flat_s"], 0.045)
        self.assertAlmostEqual(d["cost_bps"], 4.0 + 2.7, places=3)          # the hedge sold 4 bps below the quote, plus maker 1.0 and taker 1.7 bps

    def test_slack_cap_is_retaken_from_the_touch_at_each_dispatch(self):
        st = study()
        ch, ev = chain(st, "slack3")                                       # effective slack max(2, 3) = 3 bps
        play(st, ch, [(Book(100.00, 100.01), Book(99.99, 100.0)), (Book(99.90, 99.91), Book(99.99, 100.0)),   # arrival: touch 10 bps lower, outside the cap
                      (Book(99.90, 99.91), Book(99.99, 100.0)), (Book(99.89, 99.90), Book(99.99, 100.0))])   # retry: cap from 99.90, the touch 99.89 is inside 3 bps
        d = ev["variants"]["slack3"]
        self.assertEqual((d["outcome"], d["submits"], d["first_attempt_fill"]), ("hedged", 2, False))
        self.assertAlmostEqual(d["time_to_flat_s"], 0.045 + 0.045 + 0.25 + 0.045)

    def test_break_even_cap_is_one_fixed_limit_and_the_failed_chain_is_unwound_and_counted(self):
        st = study()
        ch, ev = chain(st, "be0")                                          # limit = 100 x (1 + 2.7 bps): a SELL below it is refused on every attempt
        flat = (Book(100.00, 100.01), Book(99.99, 100.0))
        play(st, ch, [flat] * 4 + [(Book(100.00, 100.01), Book(99.98, 99.99))] * 3)
        d = ev["variants"]["be0"]
        self.assertEqual((d["outcome"], d["submits"], d["first_attempt_fill"]), ("unwound", 3, False))
        self.assertAlmostEqual(d["cost_bps"], 2.0 + 1.0 + 5.0, places=2)    # sold on the maker venue 2 bps under the quote, plus maker 1.0 and that venue's taker 5.0 bps

    def test_variants_are_the_slack_grid_and_the_phase_1_lite_buffers(self):
        self.assertEqual(STUDY_VARIANTS, ("slack2", "slack3", "slack5", "slack8", "slack12", "be0", "be1", "be2", "be4", "be8"))


if __name__ == "__main__":
    unittest.main()
