"""Shared fixtures for the queue model v2 tests: a MakerExecutor, a populated book and an order placed at venue arrival."""
from __future__ import annotations

from new_backtest.fill_engine import MakerConfig, MakerExecutor
from new_backtest.fill_model import EmpiricalFillModel
from new_backtest.models import LambdaBucket, Side
from new_backtest.order_book import OrderBook


class State:
    top_depth = 100.0
    top_depth_baseline = 100.0
    spread_pct = 0.001
    short_vol = 0.0
    recent_trade_rate = 0.0
    trade_sign = 0.0


def make_book(venue="bybit", bids=None, asks=None, **kw) -> OrderBook:
    b = OrderBook(venue, "SOLUSDT", delta_semantics="absolute", **kw)
    b.apply_snapshot(bids or [(100.0, 50.0), (99.0, 100.0)], asks or [(101.0, 50.0), (102.0, 100.0)], 0.0)
    return b


def make_executor(**cfg) -> MakerExecutor:
    return MakerExecutor(EmpiricalFillModel(), MakerConfig(**cfg))


def place(ex: MakerExecutor, book: OrderBook, side=Side.BID, price=100.0, size=10.0, now=1.0, active_ts=None, arrival_ts=None, tick=1.0):
    return ex.place(venue=book.venue, pair="SOLUSDT", symbol="SOLUSDT", side=side, price=price, size=size, book=book, state=State(),
                    regime_bucket=LambdaBucket.CALM, now=now, active_ts=now if active_ts is None else active_ts, tick_size=tick, signal_id=1,
                    arrival_ts=arrival_ts)


def sell_print(ex, book, price, volume, now, event_ts=None, fee=0.0):
    """A sell aggressor print (hits bids): the maker executor's trade path with sign -1."""
    return ex.on_trade(book.venue, price, volume, -1, now, fee, event_ts=event_ts if event_ts is not None else now)


def delta(ex, book, side, price, new_size, now, event_ts=None, fee=0.0, tick=1.0, apply_to_book=True):
    """A public depth message: the book is updated first (as the engine does), then the maker executor is told the level's new size."""
    if apply_to_book:
        book.apply_delta(side, price, new_size, "modify", now)
    return ex.on_delta(book.venue, side, price, new_size, now, tick, fee, event_ts=event_ts if event_ts is not None else now)
