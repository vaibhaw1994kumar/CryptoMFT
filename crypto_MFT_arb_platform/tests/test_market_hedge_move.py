"""new_backtest.market_hedge_move (v1.1 step 9a): the expected hedge move from market data only, with edge.py's sign convention."""
from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

from new_backtest import edge as edge_mod
from new_backtest.market_hedge_move import combine, day_stats

DAY = "2024-03-01"
T0 = 1709251200.0          # 2024-03-01 00:00:00 UTC


def _write(root: Path, venue: str, stream: str, rows: list[dict]) -> None:
    d = root / venue / stream / "pair=SOLUSDT" / f"date={DAY}"
    d.mkdir(parents=True, exist_ok=True)
    with gzip.open(d / "f.jsonl.gz", "wt") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _snap(t, bid, ask):
    return {"timestamp": t, "local_ts": t, "exchange": "binance", "pair": "SOLUSDT", "best_bid": bid, "best_ask": ask, "bids": [[bid, 10.0]], "asks": [[ask, 10.0]]}


def _trade(t, side, px=100.0, vol=1.0):
    return {"exchange_ts": t, "local_ts": t, "price": px, "volume": vol, "side": side}


def _root(binance_snaps, bybit_trades):
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    _write(root, "binance", "book_snapshots", binance_snaps)
    _write(root, "bybit", "trades", bybit_trades)
    return tmp, root


class MarketHedgeMoveTests(unittest.TestCase):
    def test_mid_falls_after_a_sell_print_gives_a_negative_bid_move_and_a_lower_bid(self):
        snaps = [_snap(T0 + 3.9, 99.99, 100.01), _snap(T0 + 4.45, 99.89, 99.91), _snap(T0 + 9.0, 99.89, 99.91)]   # mid 100.00 -> 99.90 = -10 bps
        tmp, root = _root(snaps, [_trade(T0 + 3.95, "sell"), _trade(T0 + 4.0, "sell"), _trade(T0 + 4.0, "sell", vol=2.0)])
        try:
            st = day_stats(root, "SOLUSDT", DAY)
        finally:
            tmp.cleanup()
        # the two prints at T0+4.0 are one sweep -> one event (-10 bps: mid 100.00 at 4.0, 99.90 at 5.0); the T0+3.95 print is a second event (-10 bps)
        self.assertEqual(st["acc"]["bid"]["n"], 2)
        out = combine([st], 1.0, "bybit", "binance", "SOLUSDT", str(root))
        self.assertAlmostEqual(out["expected_hedge_move_bps"]["bid"], -10.0, places=6)
        self.assertEqual(st["acc"]["ask"]["n"], 0)
        # the strategy's convention: a negative bid move lowers the expected hedge price, so the same quote has less edge
        book_snaps = _root_book()
        base = edge_mod.maker_taker_expected_edge_bps(100.0, "bid", book_snaps, 1.0, 0.0, 5.0, expected_hedge_move_bps=0.0)
        adverse = edge_mod.maker_taker_expected_edge_bps(100.0, "bid", book_snaps, 1.0, 0.0, 5.0, expected_hedge_move_bps=out["expected_hedge_move_bps"]["bid"])
        self.assertLess(adverse.net_bps, base.net_bps)

    def test_ask_side_is_the_negated_mid_change(self):
        snaps = [_snap(T0 + 3.9, 99.99, 100.01), _snap(T0 + 4.45, 100.09, 100.11), _snap(T0 + 9.0, 100.09, 100.11)]   # mid up 10 bps after a BUY print: adverse for a buy hedge
        tmp, root = _root(snaps, [_trade(T0 + 4.0, "buy")])
        try:
            st = day_stats(root, "SOLUSDT", DAY)
        finally:
            tmp.cleanup()
        out = combine([st], 1.0, "bybit", "binance", "SOLUSDT", str(root))
        self.assertAlmostEqual(out["expected_hedge_move_bps"]["ask"], -10.0, places=6)

    def test_stale_or_missing_hedge_book_contributes_nothing(self):
        snaps = [_snap(T0 + 0.0, 99.99, 100.01)]                                 # then silence: a print at +10 s sees a stale book
        tmp, root = _root(snaps, [_trade(T0 - 1.0, "sell"), _trade(T0 + 10.0, "sell")])
        try:
            st = day_stats(root, "SOLUSDT", DAY)
        finally:
            tmp.cleanup()
        self.assertEqual(st["acc"]["bid"]["n"], 0)
        self.assertEqual(st["counts"]["stale_at_t"], 1)
        self.assertEqual(st["counts"]["no_book_at_t"], 1)

    def test_pending_pair_needs_a_fresh_book_at_t_plus_h(self):
        snaps = [_snap(T0 + 0.0, 99.99, 100.01), _snap(T0 + 20.0, 99.89, 99.91)]   # the book is 19 s old at t+h: not a measurement
        tmp, root = _root(snaps, [_trade(T0 + 1.5, "sell")])
        try:
            st = day_stats(root, "SOLUSDT", DAY)
        finally:
            tmp.cleanup()
        self.assertEqual(st["acc"]["bid"]["n"], 0)
        self.assertEqual(st["counts"]["stale_at_t_plus_h"], 1)


def _root_book():
    from new_backtest.order_book import OrderBook
    b = OrderBook("binance", "SOLUSDT")
    b.apply_snapshot([(100.0, 10.0), (99.99, 10.0)], [(100.01, 10.0), (100.02, 10.0)], 1.0)
    return b


if __name__ == "__main__":
    unittest.main()
