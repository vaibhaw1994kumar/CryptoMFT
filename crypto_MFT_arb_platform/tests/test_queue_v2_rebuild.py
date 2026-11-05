"""Frame-atomic application in the rebuild acceptance variants (B0.1): a bid that appears and disappears inside one frame (same exchange and receive time) must not
delete the real ask it transiently crossed. Synthetic books: they prove the wiring of the variants, not a data result."""
import unittest

from crypto_stat_arb.simulation.queue_v2.rebuild import SCALE, EngineBook, EngineFrame, ExactFrame, ExactMessage, default_variants


def _px(x: float) -> int:
    return int(round(x * SCALE))


def _snapshot(v) -> None:
    v.reset([[str(100 - i), "1"] for i in range(3)], [[str(101 + i), "1"] for i in range(3)])


class FrameAtomicVariants(unittest.TestCase):
    KEY = (1.0, 1.0)

    def _run(self, v, msgs):
        _snapshot(v)
        for side, price, size in msgs:
            v.delta(side, _px(price), _px(size), self.KEY)
        v.flush()
        return v

    def test_transient_bid_inside_a_frame_keeps_the_ask_in_the_frame_variant_only(self):
        msgs = [("bid", 101.0, 1.0), ("bid", 101.0, 0.0)]            # the bid is added and removed in the same frame; the ask at 101 was never touched
        per_message = self._run(EngineBook("x", False), msgs)
        per_frame = self._run(EngineFrame("x", False), msgs)
        self.assertEqual(per_message.top("ask", 1), [(_px(102.0), _px(1.0))])      # the engine's per-message repair deleted the real 101 ask
        self.assertEqual(per_frame.top("ask", 1), [(_px(101.0), _px(1.0))])
        self.assertEqual(per_frame.top("bid", 1), [(_px(100.0), _px(1.0))])
        self.assertEqual(per_message.extra()["crossed_events"], 1)
        self.assertEqual(per_frame.extra()["crossed_events"], 0)

    def test_exact_variants_count_the_transient_cross_only_per_message(self):
        msgs = [("bid", 101.0, 1.0), ("bid", 101.0, 0.0)]
        self.assertEqual(self._run(ExactMessage(), msgs).extra()["crossed_events"], 1)
        self.assertEqual(self._run(ExactFrame(), msgs).extra()["crossed_events"], 0)

    def test_frame_variant_still_repairs_a_cross_that_survives_the_frame(self):
        msgs = [("bid", 102.0, 1.0)]                                  # a genuine cross at the end of the frame: repaired once, on the incoming side
        per_frame = self._run(EngineFrame("x", False), msgs)
        self.assertEqual(per_frame.extra()["crossed_events"], 1)
        self.assertLess(per_frame.top("bid", 1)[0][0], per_frame.top("ask", 1)[0][0])

    def test_default_variants_hold_both_engine_application_modes(self):
        self.assertEqual(set(default_variants("bybit")),
                         {"exact_message", "exact_frame", "engine_tape_repair_on", "engine_tape_repair_off", "engine_frame_tape_repair_on", "engine_frame_tape_repair_off"})


if __name__ == "__main__":
    unittest.main()
