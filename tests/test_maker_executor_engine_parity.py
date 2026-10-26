"""Parity of new_backtest.MakerExecutor with the core queue engines (C++ mm_matching_engine and its Python mirror).

Three implementations of "a passive order queues behind the visible level, public prints burn the queue, only then do we fill":
  * crypto_stat_arb.maker_engine.CppQueueEngine      (compiled, used by `simulate-maker`)
  * crypto_stat_arb.maker_engine.PythonQueueEngine   (bit-identical mirror, checked against the C++ one here)
  * new_backtest.fill_engine.MakerExecutor           (the cross-venue backtester), configured trades-only
    (delta_fill_share = snapshot_fill_share = 0) so that only prints can fill, as in the core engines.

They are separate implementations, so exact equality is only expected where their rules coincide. The scenarios below
assert agreement there and pin the three places where they deliberately or accidentally differ:
  D1 a print strictly THROUGH our price: the core engines fill the order in full, MakerExecutor only burns queue.
  D2 two of our orders at one level sharing one print: the core engines burn the common queue once; MakerExecutor charges
     it to each order, so the later order under-fills (conservative; only reachable with maker_max_live_orders > 1).
  D3 a depth decrease followed much later by a print: the core engines credit the decrease to the next print without a time
     limit; MakerExecutor's credit expires after dedupe_window_s, so the later print drains again.
"""
from __future__ import annotations

import unittest

from crypto_stat_arb.maker_engine import CppQueueEngine, PythonQueueEngine, load_cpp_module
from new_backtest.fill_engine import MakerConfig, MakerExecutor
from new_backtest.fill_model import EmpiricalFillModel
from new_backtest.models import LambdaBucket, Side
from new_backtest.order_book import OrderBook


class _State:
    top_depth = top_depth_baseline = 100.0
    spread_pct = 0.001
    short_vol = recent_trade_rate = trade_sign = 0.0


class ExecutorAdapter:
    """MakerExecutor behind the core QueueEngine contract. Time advances 1 ms per call unless wait() is used."""
    name = "maker_executor"

    def __init__(self, **maker_kw):
        self.ex = MakerExecutor(EmpiricalFillModel(), MakerConfig(delta_fill_share=0.0, snapshot_fill_share=0.0, min_queue_ahead=0.0,
                                                                   **maker_kw))
        self.levels = {"bid": {}, "ask": {}}
        self.t = 1.0
        self.auto_advance = True            # +1 ms per call in scripted scenarios; replay drivers set t themselves
        self.tick = 1.0                     # price increment: MakerExecutor matches levels to within half a tick
        self.orders = {}

    def wait(self, seconds):
        self.t += seconds

    def _book(self, side):
        """A book whose `side` carries our known levels; the other side is a far sentinel so the library's crossed-book
        trimming can never remove a level (MakerExecutor only reads the side the order rests on)."""
        book = OrderBook("bybit", "TEST", delta_semantics="absolute")
        if side == "bid":
            bids = sorted(self.levels["bid"].items(), reverse=True) or [(1.0, 1.0)]
            asks = [(1e12, 1.0)]
        else:
            bids = [(1e-12, 1.0)]
            asks = sorted(self.levels["ask"].items()) or [(1e12, 1.0)]
        book.apply_snapshot(bids, asks, ts=0.0)
        return book

    def _tick(self):
        if self.auto_advance:
            self.t += 0.001

    def cancel(self, oid):
        order = self.orders.get(oid)
        return order is not None and self.ex.cancel(order.order_id, self.t) is not None

    def set_level(self, side, price, size):
        self._tick()
        if size > 0:
            self.levels[side][price] = size
        else:
            self.levels[side].pop(price, None)
        self.ex.on_delta("bybit", side, price, size, self.t, self.tick, 0.0)

    def place(self, oid, side, price, size, ts_ns=0):
        self._tick()
        order = self.ex.place("bybit", "TEST", "TEST", Side.BID if side == "bid" else Side.ASK, price, size, self._book(side),
                              _State(), LambdaBucket.CALM, self.t, self.t, self.tick, 1)
        self.orders[oid] = order
        return order.queue_ahead

    def on_trade(self, price, quantity, aggressor_side):
        self._tick()
        by_id = {o.order_id: oid for oid, o in self.orders.items()}
        fills = self.ex.on_trade("bybit", price, quantity, 1 if aggressor_side == "BUY" else -1, self.t, 0.0)
        return [(by_id[f.order_id], f.qty) for f in fills]

    def queue_ahead(self, oid):
        return self.orders[oid].queue_ahead

    def remaining(self, oid):
        return self.orders[oid].remaining


class CoreAdapter:
    def __init__(self, engine):
        self.e, self.name = engine, engine.name

    def wait(self, seconds):
        pass                                   # the core engines have no clock

    def set_level(self, side, price, size):
        self.e.set_level(side, price, size)

    def cancel(self, oid):
        return self.e.cancel(oid)

    def place(self, oid, side, price, size, ts_ns=0):
        return self.e.place(oid, side, price, size, ts_ns)

    def on_trade(self, price, quantity, aggressor_side):
        return [(f.client_order_id, f.quantity) for f in self.e.on_trade(price, quantity, aggressor_side)]

    def queue_ahead(self, oid):
        return self.e.queue_ahead(oid)

    def remaining(self, oid):
        return self.e.remaining(oid)


def adapters():
    out = {"python": CoreAdapter(PythonQueueEngine()), "executor": ExecutorAdapter()}
    module = load_cpp_module()
    if module is not None:
        out["cpp"] = CoreAdapter(CppQueueEngine(module))
    return out


# ── scenarios: each returns an observation the engines can be compared on ──

def s_touch_prints(a):
    a.set_level("bid", 100.0, 10.0); a.place("A", "bid", 100.0, 5.0)
    return [(a.on_trade(100.0, 4.0, "SELL"), round(a.queue_ahead("A"), 9)) for _ in range(3)] + [round(a.remaining("A"), 9)]

def s_trade_then_depth(a):
    a.set_level("bid", 100.0, 10.0); a.place("A", "bid", 100.0, 5.0)
    f = a.on_trade(100.0, 3.0, "SELL"); a.set_level("bid", 100.0, 7.0)
    return f, round(a.queue_ahead("A"), 9)

def s_depth_then_trade(a):
    a.set_level("bid", 100.0, 10.0); a.place("A", "bid", 100.0, 5.0)
    a.set_level("bid", 100.0, 7.0); f = a.on_trade(100.0, 3.0, "SELL")
    return f, round(a.queue_ahead("A"), 9)

def s_cancel_shrink(a):
    a.set_level("bid", 100.0, 10.0); a.place("A", "bid", 100.0, 5.0); a.set_level("bid", 100.0, 6.0)
    return round(a.queue_ahead("A"), 9)

def s_growth_behind_us(a):
    a.set_level("bid", 100.0, 10.0); a.place("A", "bid", 100.0, 5.0); a.set_level("bid", 100.0, 25.0)
    return round(a.queue_ahead("A"), 9)

def s_grow_then_shrink_below_queue(a):
    a.set_level("bid", 100.0, 10.0); a.place("A", "bid", 100.0, 5.0)
    a.set_level("bid", 100.0, 20.0); a.set_level("bid", 100.0, 8.0)
    return round(a.queue_ahead("A"), 9)

def s_ask_side_mirror(a):
    a.set_level("ask", 101.0, 10.0); a.place("A", "ask", 101.0, 5.0)
    return a.on_trade(101.0, 12.0, "BUY"), round(a.remaining("A"), 9)

def s_print_through(a):                                        # D1
    a.set_level("bid", 100.0, 10.0); a.place("A", "bid", 100.0, 5.0)
    return a.on_trade(99.0, 1.0, "SELL"), round(a.remaining("A"), 9)

def s_two_orders_one_print(a):                                 # D2
    a.set_level("bid", 100.0, 10.0); a.place("A", "bid", 100.0, 5.0); a.place("B", "bid", 100.0, 5.0)
    return sorted((o, round(q, 9)) for o, q in a.on_trade(100.0, 20.0, "SELL"))

def s_depth_then_late_trade(a):                                # D3
    a.set_level("bid", 100.0, 10.0); a.place("A", "bid", 100.0, 5.0)
    a.set_level("bid", 100.0, 7.0); a.wait(60.0); f = a.on_trade(100.0, 3.0, "SELL")
    return f, round(a.queue_ahead("A"), 9)


class AgreementTests(unittest.TestCase):
    """Where the rules coincide, all engines give identical observations."""

    def _run(self, scenario):
        return {name: scenario(a) for name, a in adapters().items()}

    def test_core_engines_agree_with_each_other_on_every_scenario(self):
        if "cpp" not in adapters():
            self.skipTest("compiled mm_matching_engine is not importable here")
        for sc in (s_touch_prints, s_trade_then_depth, s_depth_then_trade, s_cancel_shrink, s_growth_behind_us,
                   s_grow_then_shrink_below_queue, s_ask_side_mirror, s_print_through, s_two_orders_one_print, s_depth_then_late_trade):
            with self.subTest(sc.__name__):
                r = self._run(sc)
                self.assertEqual(r["cpp"], r["python"])

    def test_executor_matches_the_core_engines_where_the_rules_coincide(self):
        expected = {
            s_touch_prints: [([], 6.0), ([], 2.0), ([("A", 2.0)], 0.0), 3.0],
            s_trade_then_depth: ([], 7.0),
            s_depth_then_trade: ([], 7.0),
            s_cancel_shrink: 6.0,
            s_growth_behind_us: 10.0,
            s_grow_then_shrink_below_queue: 8.0,
            s_ask_side_mirror: ([("A", 2.0)], 3.0),
        }
        for sc, want in expected.items():
            with self.subTest(sc.__name__):
                r = self._run(sc)
                for name, got in r.items():
                    self.assertEqual(got, want, f"{name} on {sc.__name__}")


class DocumentedDifferenceTests(unittest.TestCase):
    def _run(self, scenario):
        return {name: scenario(a) for name, a in adapters().items()}

    def test_D1_a_print_through_our_price_fills_the_core_engines_but_only_burns_queue_in_the_executor(self):
        r = self._run(s_print_through)
        for name in set(r) - {"executor"}:
            self.assertEqual(r[name], ([("A", 5.0)], 0.0), name)
        self.assertEqual(r["executor"], ([], 5.0))                 # queue 10 -> 9, no fill: conservative

    def test_D1_with_the_opt_in_through_print_rule_the_executor_matches_the_core_engines(self):
        r = s_print_through(ExecutorAdapter(fill_through_prints=True))
        self.assertEqual(r, ([("A", 5.0)], 0.0))
        core = s_print_through(CoreAdapter(PythonQueueEngine()))
        self.assertEqual(r, core)

    def test_D2_two_orders_sharing_one_print_the_executor_double_charges_the_common_queue(self):
        r = self._run(s_two_orders_one_print)
        for name in set(r) - {"executor"}:
            self.assertEqual(r[name], [("A", 5.0), ("B", 5.0)], name)
        self.assertEqual(r["executor"], [("A", 5.0)])              # B still has 5 of the queue to burn: conservative

    def test_D3_a_late_print_after_a_depth_decrease_drains_again_in_the_executor(self):
        r = self._run(s_depth_then_late_trade)
        for name in set(r) - {"executor"}:
            self.assertEqual(r[name], ([], 7.0), name)             # the decrease is credited to the next print
        self.assertEqual(r["executor"], ([], 4.0))                 # credit expired after 50 ms: 3 more drained

    def test_D3_inside_the_window_the_executor_agrees_with_the_core_engines(self):
        a = ExecutorAdapter()
        a.set_level("bid", 100.0, 10.0); a.place("A", "bid", 100.0, 5.0)
        a.set_level("bid", 100.0, 7.0); a.wait(0.04)               # 40 ms: still the same execution
        self.assertEqual(a.on_trade(100.0, 3.0, "SELL"), [])
        self.assertAlmostEqual(a.queue_ahead("A"), 7.0, places=9)


if __name__ == "__main__":
    unittest.main()
