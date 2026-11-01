"""PostOnly acceptance in the backtester (postonly_reject_model), modelled from market data at the EXCHANGE time of arrival.

Rule: a passive order that reaches the exchange is checked against the book the matching engine held at that moment. Buy at or
above the best ask, or sell at or below the best bid: rejected (no fill, no queue position, no conversion to a taker order).
The strategy hears of it one more latency later and the slot stays occupied until then.

Exchange time vs receive time: within one venue the feed arrives in exchange order, so the book at the arrival time is the book
just before the first venue event whose (host-aligned) exchange time is >= the arrival. A feed that lags therefore cannot make
the check see a book that is too old or too new.
"""
from __future__ import annotations

import warnings
from types import SimpleNamespace

import pytest

from new_backtest.event_loader import BOOK, DELTA, TRADE
from new_backtest.fill_engine import MakerConfig, MakerExecutor
from new_backtest.fill_model import EmpiricalFillModel
from new_backtest.models import LambdaBucket, Side
from new_backtest.order_book import OrderBook
from new_backtest.postonly import AckLatencyConfig, AckLatencySource, would_cross

from test_maker_order_lifetime import BINANCE, BYBIT, Harness

warnings.simplefilter("ignore")
ON = {"postonly_reject_model": "on", "postonly_notice_s": 0.05}


def harness(model="on", **kw):
    cfg = dict(ON if model == "on" else {})
    cfg.update(kw.pop("cfg_kw", {}))
    h = Harness(ttl=0.0, entry_s=0.003, queue=0.0, max_live=1, cfg_kw=cfg, **kw)
    h.bt.latency.aligner.samples = 1            # aligner ready, offset 0: exchange times are usable
    return h


def two_books(h, bids, asks, t=0.0):
    for v in (BYBIT, BINANCE):
        h.event(t, BOOK, v, {"bids": [list(b) for b in bids], "asks": [list(a) for a in asks]}, exchange_ts=t)
        h.bt.tick_size[v] = 1.0


def poke(h, t, venue=BYBIT, ets=None):
    """An unrelated trade, stamped `ets` at the exchange: the first event whose exchange time reaches the arrival."""
    h.event(t, TRADE, venue, {"price": 50.0, "volume": 0.001, "side": "s"}, exchange_ts=t if ets is None else ets)


def send(h, side, price, t=0.01):
    from new_backtest.arb_strategy import MAKER_TAKER, ArbLeg, ArbSignal
    h._sid += 1
    leg = ArbLeg(venue=BYBIT, side=side, qty=1.0, limit_price=price, is_taker=False, expected_price=price, fee_bps=0.0)
    buy, sell = (BYBIT, BINANCE) if side == "buy" else (BINANCE, BYBIT)
    sig = ArbSignal(signal_id=h._sid, ts=t, buy_venue=buy, sell_venue=sell, qty=1.0, gross_edge_bps=5.0, fee_bps=1.0, net_edge_bps=4.0,
                    legs=[leg], mode=MAKER_TAKER, buy_price=price, sell_price=price + 1)
    h.bt._fire(sig, t)
    return sig


# ── the rule itself ──────────────────────────────────────────────────────────

def test_bad_side_is_an_error():
    with pytest.raises(ValueError):
        would_cross("hold", 1.0, 1.0, 2.0)


@pytest.mark.parametrize("side,price,bid,ask,expect", [
    ("buy", 101.0, 100.0, 101.0, True), ("buy", 101.5, 100.0, 101.0, True), ("buy", 100.5, 100.0, 102.0, False),
    ("buy", 100.0, 100.0, 101.0, False), ("sell", 100.0, 100.0, 101.0, True), ("sell", 99.5, 100.0, 101.0, True),
    ("sell", 100.5, 100.0, 102.0, False), ("sell", 101.0, 100.0, 101.0, False), ("buy", 5.0, 0.0, 0.0, False), ("sell", 5.0, 0.0, 0.0, False),
])
def test_rule(side, price, bid, ask, expect):
    assert would_cross(side, price, bid, ask) is expect


@pytest.mark.parametrize("side,price,bid,ask,expect", [
    ("buy", 101.0, 100.0, 101.0, True), ("buy", 100.0, 100.0, 101.0, False),
    ("sell", 100.0, 100.0, 101.0, True), ("sell", 101.0, 100.0, 101.0, False),
    ("buy", 101.0, 100.0, 102.0, False), ("sell", 100.0, 99.0, 101.0, False),
])
def test_would_cross_boundary_and_improvement(side, price, bid, ask, expect):
    assert would_cross(side, price, bid, ask) is expect


# ── in the backtester ────────────────────────────────────────────────────────

@pytest.mark.parametrize("side,price,reason", [("buy", 101.0, "buy at the best ask"), ("sell", 99.0, "sell at the best bid")])
def test_crossing_on_arrival_is_rejected_on_both_sides(side, price, reason):
    h = harness()
    two_books(h, bids=((99.0, 5.0), (98.0, 5.0)), asks=((101.0, 5.0), (102.0, 5.0)))
    send(h, side, price)
    h.advance(0.02)                                     # landed at the exchange (t=0.013) ...
    assert h.resting == [], "... but not resting: the check runs when the venue's feed reaches the arrival time"
    poke(h, 0.03)                                       # first venue event stamped after the arrival
    assert h.resting == [], reason
    assert h.bt.po["rejects"] == 1 and h.bt.po["rejects_" + side] == 1
    assert h.bt.trade_log == [], "no fill and no conversion into a taker order"
    assert not any(True for _ in h.bt.maker.live_orders())


def test_the_strategy_hears_of_the_reject_one_latency_later_and_the_slot_stays_occupied_until_then():
    h = harness()
    two_books(h, bids=((99.0, 5.0),), asks=((101.0, 5.0),))
    send(h, "buy", 101.0)
    poke(h, 0.03)                                       # rejected at the exchange at t = 0.013 (the arrival)
    assert h.live(0.03) == 1, "still counted live: the notice has not arrived"
    assert h.bt.orders_missed == 0
    h.advance(0.0629)                                   # notice due at 0.013 + 0.05 = 0.063
    assert h.live(0.0629) == 1
    h.advance(0.07)
    assert h.live(0.07) == 0 and h.bt.orders_missed == 1 and h.bt.miss_reasons["postonly_reject"] == 1


def test_one_tick_inside_the_spread_rests_and_the_exact_touch_is_rejected():
    for side, inside, touch in (("buy", 101.0, 102.0), ("sell", 101.0, 100.0)):
        h = harness()
        two_books(h, bids=((100.0, 5.0),), asks=((102.0, 5.0),))
        send(h, side, inside)
        poke(h, 0.03)
        assert len(h.resting) == 1 and h.bt.po["rejects"] == 0, f"{side} one tick inside the spread improves it and rests"
        h2 = harness()
        two_books(h2, bids=((100.0, 5.0),), asks=((102.0, 5.0),))
        send(h2, side, touch)
        poke(h2, 0.03)
        assert h2.resting == [] and h2.bt.po["rejects"] == 1, f"{side} at the opposite touch takes liquidity"


def test_no_reject_when_the_book_does_not_cross_and_the_order_then_fills_as_before():
    h = harness()
    two_books(h, bids=((99.0, 5.0), (98.0, 5.0)), asks=((101.0, 5.0),))
    send(h, "buy", 100.0)
    poke(h, 0.03)
    assert len(h.resting) == 1 and h.bt.po["rejects"] == 0 and h.bt.po["accepted"] == 1
    h.sell_trade(0.5, BYBIT, volume=3.0, price=100.0, exchange_ts=0.5)
    assert sum(r["qty"] for r in h.bt.trade_log if r["venue"] == BYBIT) == pytest.approx(1.0)


def test_model_off_places_every_arriving_order_exactly_as_before():
    h = harness(model="off")
    two_books(h, bids=((99.0, 5.0),), asks=((101.0, 5.0),))
    send(h, "buy", 101.0)                               # would cross, but the model is off
    h.advance(0.02)
    assert len(h.resting) == 1 and h.bt.po["checks"] == 0


def test_stale_feed_receive_time_book_looks_fine_but_the_exchange_time_book_crosses():
    """The ask drops to 100 at exchange time 0.001; the update is delivered late, at receive time 0.040. The buy at 100 arrives
    at 0.013: the book the strategy holds then (ask 101) says 'fine', the matching engine's book (ask 100) says 'cross'."""
    h = harness()
    two_books(h, bids=((99.0, 5.0),), asks=((101.0, 5.0),))
    send(h, "buy", 100.0, t=0.010)                       # arrives at 0.013
    h.advance(0.02)
    assert h.bt.books[BYBIT].best_ask == 101.0, "receive-time book still shows the old ask"
    h.event(0.040, BOOK, BYBIT, {"bids": [[99.0, 5.0]], "asks": [[100.0, 5.0], [101.0, 5.0]]}, exchange_ts=0.001)   # stamped BEFORE the arrival
    assert h.bt.po["checks"] == 0, "an update stamped before the arrival does not by itself resolve the check"
    poke(h, 0.041, ets=0.020)                            # first event stamped after the arrival: check against the book as it is now
    assert h.bt.po["rejects"] == 1 and h.resting == []


def test_an_update_stamped_after_the_arrival_does_not_reject():
    """The ask drops to 100 at exchange time 0.020, after the order arrived at 0.013: the order was accepted by then."""
    h = harness()
    two_books(h, bids=((99.0, 5.0),), asks=((101.0, 5.0),))
    send(h, "buy", 100.0, t=0.010)
    h.event(0.030, BOOK, BYBIT, {"bids": [[99.0, 5.0]], "asks": [[100.0, 5.0], [101.0, 5.0]]}, exchange_ts=0.020)
    assert h.bt.po["rejects"] == 0 and h.bt.po["accepted"] == 1 and len(h.resting) == 1


def test_without_exchange_times_the_receive_time_is_used_and_counted():
    h = harness()
    h.bt.latency.aligner.samples = 0                    # aligner not ready: no host-aligned exchange time
    for v in (BYBIT, BINANCE):
        h.event(0.0, BOOK, v, {"bids": [[99.0, 5.0]], "asks": [[101.0, 5.0]]})
        h.bt.tick_size[v] = 1.0
    send(h, "buy", 101.0)
    h.event(0.030, TRADE, BYBIT, {"price": 50.0, "volume": 0.001, "side": "s"})
    assert h.bt.po["rejects"] == 1 and h.bt.po["checks_receive_time_fallback"] == 1


def test_the_order_cannot_fill_before_the_check_resolves():
    h = harness()
    two_books(h, bids=((99.0, 5.0),), asks=((101.0, 5.0),))
    send(h, "buy", 100.0)
    h.advance(0.02)
    assert h.resting == [] and h.bt.trade_log == []


def test_orders_still_waiting_at_the_end_of_data_are_counted_not_placed():
    h = harness()
    two_books(h, bids=((99.0, 5.0),), asks=((101.0, 5.0),))
    send(h, "buy", 100.0)
    h.advance(0.02)
    h.bt._close_out(0.1)
    assert h.bt.po["unresolved_at_data_end"] == 1 and h.resting == []


def test_a_waiting_order_counts_as_live_and_as_pending_exposure():
    h = harness()
    two_books(h, bids=((99.0, 5.0),), asks=((101.0, 5.0),))
    send(h, "buy", 100.0)
    h.advance(0.02)
    assert h.live(0.02) == 1
    assert h.bt._pending_exposure().buys.get(BYBIT, 0.0) == pytest.approx(1.0)


# ── amend (cancel/replace) ───────────────────────────────────────────────────

def _executor_with_order(side=Side.BID, price=100.0):
    ex = MakerExecutor(EmpiricalFillModel(), MakerConfig(min_queue_ahead=0.0))
    book = OrderBook(BYBIT, "BTCUSDT", "absolute")
    book.apply_snapshot([(99.0, 5.0)], [(102.0, 5.0)], 0.0)
    state = SimpleNamespace(top_depth=1.0, top_depth_baseline=1.0, spread_pct=0.01, short_vol=0.0, recent_trade_rate=0.0, trade_sign=0.0)
    o = ex.place(BYBIT, "BTCUSDT", "BTCUSDT", side, price, 1.0, book, state, LambdaBucket.CALM, 0.0, 0.0, 1.0)
    return ex, book, state, o


def test_an_amend_that_would_cross_is_rejected_and_the_old_queue_position_is_lost():
    ex, book, state, o = _executor_with_order()
    res = ex.amend(o.order_id, 102.0, 1.0, book=book, state=state, regime_bucket=LambdaBucket.CALM, now=1.0, active_ts=1.0, tick_size=1.0)
    assert res.rejected and res.old_cancelled and res.order is None and res.reason == "postonly_would_take_liquidity"
    assert ex.orders == {}, "the original order is gone and nothing replaced it"
    assert ex.amends_rejected_postonly == 1


def test_an_amend_that_does_not_cross_replaces_the_order_with_a_fresh_queue_position():
    ex, book, state, o = _executor_with_order()
    o.queue_ahead = 0.0                                  # the old order had worked its way to the front
    res = ex.amend(o.order_id, 100.5, 1.0, book=book, state=state, regime_bucket=LambdaBucket.CALM, now=1.0, active_ts=1.0, tick_size=1.0)
    assert not res.rejected and res.order is not None and res.order.order_id != o.order_id
    assert list(ex.orders) == [res.order.order_id]
    assert res.order.price == 100.5


def test_an_amend_of_an_unknown_order_changes_nothing():
    ex, book, state, o = _executor_with_order()
    res = ex.amend(999, 100.5, 1.0, book=book, state=state, regime_bucket=LambdaBucket.CALM, now=1.0, active_ts=1.0, tick_size=1.0)
    assert res.reason == "unknown_order" and list(ex.orders) == [o.order_id]


# ── latency source ───────────────────────────────────────────────────────────

def test_fixed_ack_latency_sets_the_arrival_time_and_leaves_the_venue_latency_stream_alone():
    a = harness(model="off", cfg_kw={"ack_latency": AckLatencyConfig("fixed", fixed_ms=40.0)})
    b = harness(model="off")
    for h in (a, b):
        two_books(h, bids=((99.0, 5.0),), asks=((101.0, 5.0),))
        h.bt.latency.entry_delay = h.bt.latency.__class__.entry_delay.__get__(h.bt.latency)    # unpin the harness override: use the real sampler
    send(a, "buy", 100.0, t=1.0)
    send(b, "buy", 100.0, t=1.0)
    arrivals = [o.release_ts for o in a.bt.pending.in_flight()]
    assert arrivals == [pytest.approx(1.04)]
    assert a.bt.latency["bybit"].entry.sample() == b.bt.latency["bybit"].entry.sample(), "the venue entry sampler drew the same number either way"


def test_empirical_ack_latency_reproduces_the_calibrated_quantiles(tmp_path):
    import json
    q = [i / 100 for i in range(100)] + [1.0]
    ms = [10.0 + i for i in range(100)] + [200.0]
    doc = {"ack_latency_proxy": {"empirical_quantile_grids": {"p": {"n": 1000, "q": q, "ms": ms}}}}
    path = tmp_path / "cal.json"
    path.write_text(json.dumps(doc))
    src = AckLatencySource(AckLatencyConfig("empirical", calibration_path=str(path), period="p"))
    xs = sorted(src.sample_s() * 1000 for _ in range(20000))
    assert xs[len(xs) // 2] == pytest.approx(60.0, abs=2.0) and xs[int(0.9 * len(xs))] == pytest.approx(100.0, abs=2.0)
    assert src.n_calibration == 1000


def test_same_seed_same_ack_samples_and_scale_applies():
    a = AckLatencySource(AckLatencyConfig("fixed", fixed_ms=10.0, scale=5.0))
    assert a.sample_s() == pytest.approx(0.05)
    with pytest.raises(ValueError):
        AckLatencyConfig("fixed")                       # fixed needs a value
    with pytest.raises(ValueError):
        AckLatencyConfig("empirical")                   # empirical needs a file
    with pytest.raises(ValueError):
        AckLatencyConfig("bogus")


def test_config_validation():
    from new_backtest.backtester import ArbBacktestConfig, CrossVenueArbBacktester
    for kw in ({"postonly_reject_model": "maybe"}, {"cancel_time_basis": "never"}, {"postonly_notice_s": -1.0}):
        with pytest.raises(ValueError):
            Harness(cfg_kw=kw)
