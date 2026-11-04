"""Execution-layer brief step 8: a simulated own fill never changes the reconstructed public book.

The reference engine B is fed exactly the public events engine A is fed, but sends no orders: its books ARE the public reconstruction.
A rests a maker bid, gets filled by a public print, and hedges. Maker fills must leave A's books equal to B's. Taker fills do too when
`consume_liquidity` is off; with it on (the engine default) a taker fill removes the liquidity it walked from the VIEW book — the one
documented exception, counted in maker_observations.own_taker_book_consumption — and the test pins exactly what it removes."""
from __future__ import annotations

import unittest
from unittest import mock

from test_maker_order_lifetime import BINANCE, BYBIT, Harness


def snapshot(h):
    return {v: (h.bt.books[v].levels("bid", 50), h.bt.books[v].levels("ask", 50)) for v in (BYBIT, BINANCE)}


def differences(a: Harness, b: Harness, venues=(BYBIT, BINANCE)) -> list[str]:
    out = []
    for v in venues:
        for side in ("bid", "ask"):
            if a.bt.books[v].levels(side, 50) != b.bt.books[v].levels(side, 50):
                out.append(f"{v}/{side}")
    return out


def pair(consume: bool):
    a, b = Harness(cfg_kw={"consume_liquidity": consume}), Harness(cfg_kw={"consume_liquidity": consume})
    return a, b


def both(a, b, fn):
    fn(a)
    fn(b)


def feed(a, b):
    both(a, b, lambda h: h.book(0.5, BYBIT))
    both(a, b, lambda h: h.book(0.5, BINANCE))


class OwnFillsAndThePublicBook(unittest.TestCase):
    def test_a_resting_order_and_a_maker_fill_leave_the_book_equal_to_the_public_reconstruction(self):
        a, b = pair(True)
        feed(a, b)
        a.send_maker(1.0, qty=2.0)                                   # only A has an order
        both(a, b, lambda h: h.advance(2.0))
        self.assertEqual(differences(a, b), [])
        both(a, b, lambda h: h.sell_trade(5.0, BYBIT, volume=2.5))   # the public print that fills A's order
        self.assertEqual(a.bt.maker.orders_filled, 1)               # the order really was filled in A ...
        self.assertEqual(differences(a, b, venues=(BYBIT,)), [])     # ... and the maker venue's public book is untouched
        self.assertEqual(differences(a, b, venues=(BINANCE,)), [])   # the hedge is still in flight

    def test_a_taker_fill_leaves_the_book_equal_when_consume_liquidity_is_off(self):
        a, b = pair(False)
        feed(a, b)
        a.send_maker(1.0, qty=2.0)
        both(a, b, lambda h: h.sell_trade(5.0, BYBIT, volume=2.5))
        both(a, b, lambda h: h.advance(6.0))                         # the hedge lands on Binance and fills
        self.assertEqual(a.bt.hedge["hedges_filled"], 1)
        self.assertEqual(differences(a, b), [])
        self.assertEqual(a.bt.taker.consumed_qty, 0.0)

    def test_the_documented_exception_a_taker_fill_removes_what_it_walked_when_consume_liquidity_is_on(self):
        a, b = pair(True)
        feed(a, b)
        a.send_maker(1.0, qty=2.0)
        both(a, b, lambda h: h.sell_trade(5.0, BYBIT, volume=2.5))
        both(a, b, lambda h: h.advance(6.0))
        self.assertEqual(differences(a, b, venues=(BYBIT,)), [])                    # never the maker venue
        self.assertEqual(differences(a, b, venues=(BINANCE,)), [f"{BINANCE}/bid"])               # only the side the hedge sold into
        # exactly the hedge's quantity left the bid side of the hedge venue, nothing else moved
        pub = dict(b.bt.books[BINANCE].levels("bid", 50))
        own = dict(a.bt.books[BINANCE].levels("bid", 50))
        self.assertAlmostEqual(sum(pub.values()) - sum(own.values()), 2.0)
        self.assertEqual(a.bt.books[BINANCE].levels("ask", 50), b.bt.books[BINANCE].levels("ask", 50))
        self.assertAlmostEqual(a.bt.taker.consumed_qty, 2.0)
        self.assertEqual(a.bt._obs_summary()["own_taker_book_consumption"]["fills_that_removed_liquidity"], 1)

    def test_mutation_an_own_maker_fill_that_mutates_the_public_book_is_caught(self):
        a, b = pair(False)
        feed(a, b)
        a.send_maker(1.0, qty=2.0)
        real = a.bt._on_fill

        def mutant(fill, *args, **kw):                                # the broken engine also takes the fill out of the displayed level
            if not fill.is_taker:
                bk = a.bt.books[fill.venue]
                bk.apply_delta("bid", fill.price, max(0.0, bk.level_size("bid", fill.price) - fill.qty), "modify", fill.ts)
                bk.apply_delta("bid", 99.0, 1.0, "modify", fill.ts)                 # (and moves the touch, as a book walk would)
            return real(fill, *args, **kw)

        with mock.patch.object(a.bt, "_on_fill", mutant):
            both(a, b, lambda h: h.sell_trade(5.0, BYBIT, volume=2.5))
        self.assertNotEqual(differences(a, b, venues=(BYBIT,)), [])                  # the invariant check sees it

    def test_mutation_a_taker_that_consumes_while_the_flag_is_off_is_caught(self):
        a, b = pair(False)
        feed(a, b)
        a.send_maker(1.0, qty=2.0)
        both(a, b, lambda h: h.sell_trade(5.0, BYBIT, volume=2.5))
        a.bt.taker.consume_liquidity = True                                          # the mutant: the guard `if self.consume_liquidity` is gone
        both(a, b, lambda h: h.advance(6.0))
        self.assertNotEqual(differences(a, b), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
