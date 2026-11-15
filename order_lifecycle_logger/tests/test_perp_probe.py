"""Perp probe v2 against the PAPER venue (simulated private side on a scripted public capture): placement, cancels, amends, send-time logging, inventory and loss caps, kill switch. Offline; nothing is sent anywhere."""
import asyncio
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ollog import mainnet  # noqa: E402
from ollog.models import Instrument  # noqa: E402
from ollog.probe import PerpProbeConfig, PerpProbeRunner  # noqa: E402
from ollog.recorder import Recorder  # noqa: E402
from ollog.runner import Budget  # noqa: E402
from ollog.venues import PaperClient  # noqa: E402

INST = Instrument("bybit_linear", "SOLUSDT", "SOL", "USDT", Decimal("0.01"), Decimal("0.1"), Decimal("0.1"), Decimal(5))


class Book:
    def __init__(self, bids, asks):
        self.bids, self.asks = bids, asks

    def top(self, n=20):
        return self.bids[:n], self.asks[:n]


class FakeCapture:
    def __init__(self):
        self.books = {"SOLUSDT": Book([[100.0, 7.0], [99.99, 3.0]], [[100.02, 4.0], [100.03, 2.0]])}
        self.valid = {"SOLUSDT": True}
        self.trade_hooks = []

    def print_(self, price, size, side, symbol="SOLUSDT"):
        for h in self.trade_hooks:
            h(symbol, price, size, side, 0.0)


def make(tmp_path, cap=3, client_cls=PaperClient, **kw):
    base = dict(mean_gap_s=0.02, cancel_share=1.0, cancel_after_s=(0.15,), amend_share=0.0, sharp_share=0.0, touch_share=0.5, ack_timeout_s=0.5, cancel_timeout_s=0.5, preflight=False, flatten=False, tag="v", max_open=1,
                max_notional=Decimal(25), max_net_notional=Decimal(200), unsafe_feed_s=0.3)
    base.update(kw)
    cfg = PerpProbeConfig(**base)
    cap_ = FakeCapture()
    client = client_cls("bybit_linear", cap_, {"SOLUSDT": INST}, latency_ms=(1.0, 3.0))
    rec = Recorder(tmp_path, "bybit_linear", "PAPER-simulated-orders")
    stop = asyncio.Event()
    r = PerpProbeRunner(client, rec, ["SOLUSDT"], {"SOLUSDT": INST}, cfg, Budget(cap, 5000), stop, cap_)
    return r, client, rec, cap_


async def run_until_done(r, timeout=8.0, feeder=None):
    task = asyncio.create_task(r.run())
    ft = asyncio.create_task(feeder()) if feeder else None
    try:
        await asyncio.wait_for(task, timeout)
    finally:
        if ft:
            ft.cancel()


def read(rec):
    rec.close()
    orders = [json.loads(x) for x in (rec.dir / "orders.jsonl").read_text().splitlines()]
    events = [json.loads(x) for x in (rec.dir / "events.jsonl").read_text().splitlines()]
    return orders, events


def test_v2_ceilings_and_required_caps():
    ok = dict(markets=["bybit_linear", "binance_futures"], symbols=["SOLUSDT", "ADAUSDT", "MNTUSDT"], max_orders=3600, max_notional=Decimal(10), max_net_notional=Decimal(200), loss_cap_day=Decimal(20),
              loss_cap_total=Decimal(200), max_open=2, confirm=mainnet.CONFIRM_PHRASE, env_flag="1")
    assert mainnet.violations_v2(**ok) == []
    for change, frag in [({"loss_cap_day": None}, "--loss-cap-day must be given"), ({"loss_cap_day": Decimal(21)}, "loss-cap-day"), ({"loss_cap_total": Decimal(201)}, "loss-cap-total"), ({"max_open": 3}, "--max-open"),
                         ({"max_net_notional": Decimal(201)}, "max-net-notional"), ({"symbols": ["BTCUSDT"]}, "allow-list"), ({"confirm": None}, "--confirm-mainnet"), ({"env_flag": None}, "OLLOG_MAINNET"), ({"max_orders": 5001}, "max-orders")]:
        v = mainnet.violations_v2(**{**ok, **change})
        assert v and any(frag in x for x in v), (change, v)


def test_orders_are_logged_at_send_time_and_to_their_end_never_filled_included(tmp_path):
    r, client, rec, cap = make(tmp_path, cap=6)
    asyncio.run(run_until_done(r))
    orders, events = read(rec)
    sent = [e for e in events if e["type"] == "order_sent"]
    assert len(orders) == 6 and len(sent) == 6 and r.stop_reason == "order cap reached"
    assert {o["client_id"] for o in orders} == {e["client_id"] for e in sent}
    for o in orders:
        assert o["label"] == "PAPER-simulated-orders" and o["kind"] == "perp_probe_v2" and o["outcome"] == "cancelled_unfilled" and o["filled_qty"] == "0" and o["plan"]["cancel_after_s"] == 0.15
        assert (o["side"], o["plan"]["placement"], Decimal(o["price"])) in {("Buy", "touch", Decimal("100.00")), ("Buy", "back1", Decimal("99.99")), ("Sell", "touch", Decimal("100.02")), ("Sell", "back1", Decimal("100.03"))}
        assert o["best_bid_at_send"] == 100.0 and o["t"]["send_mono_ns"] and o["t"]["csend_mono_ns"] and "pre_move_bps_1s" in o
    assert client.position.get("SOLUSDT", 0) == 0 and not r.live_orders


def test_a_print_through_the_queue_fills_and_the_account_follows(tmp_path):
    r, client, rec, cap = make(tmp_path, cap=1, cancel_after_s=(1.0,), touch_share=1.0)
    side_seen = {}

    async def feeder():
        while True:
            await asyncio.sleep(0.05)
            cap.print_(100.0, 50.0, "Sell")             # a sell aggressor at the bid price: takes the 7 ahead and fills a resting buy
            cap.print_(100.02, 50.0, "Buy")             # a buy aggressor at the ask price: fills a resting sell
    asyncio.run(run_until_done(r, feeder=feeder))
    (o,) = read(rec)[0]
    assert o["outcome"] == "filled" and Decimal(o["filled_qty"]) == Decimal("0.1") and o["execs"]
    pos = r.acct["SOLUSDT"]["pos"]
    assert pos == (Decimal("0.1") if o["side"] == "Buy" else Decimal("-0.1")) and client.position["SOLUSDT"] == pos
    assert r.acct["SOLUSDT"]["fees"] > 0 and o["net_after"] == str(pos)


def test_an_amend_moves_the_price_one_tick_and_is_recorded(tmp_path):
    r, client, rec, cap = make(tmp_path, cap=2, cancel_after_s=(0.5,), amend_share=1.0, amend_after_s=(0.1,), touch_share=1.0)
    asyncio.run(run_until_done(r))
    orders, _ = read(rec)
    for o in orders:
        (a,) = o["amends"]
        assert a["ok"] is True and abs(Decimal(a["to"]) - Decimal(a["from"])) == Decimal("0.01")
        assert a["from"] == o["price"] and a["send_mono_ns"] and a["ret_mono_ns"]
        assert o["outcome"] == "cancelled_unfilled"
    assert r.stats["amends"] == 2 and r.stats["amend_rejects"] == 0


def test_a_taker_unwind_is_reduce_only_counted_in_the_account_and_recorded(tmp_path):
    r, client, rec, cap = make(tmp_path, cap=0)

    async def go():
        await client.start_stream(r.on_event)
        r._apply_fill("SOLUSDT", "Buy", Decimal("0.3"), Decimal("100.00"), taker=False)
        client.position["SOLUSDT"] = Decimal("0.3")
        await r._taker_unwind("SOLUSDT", "inventory cap")
    asyncio.run(go())
    (o,), _ = read(rec)
    assert o["kind"] == "taker_unwind" and o["side"] == "Sell" and o["outcome"] == "filled" and r.acct["SOLUSDT"]["pos"] == 0 and client.position["SOLUSDT"] == 0
    assert r.stats["taker_unwinds"] == 1
    assert r.loss_total() > 0                                                                   # the spread and both fees are a loss


def test_feed_unsafe_trips_the_kill_switch(tmp_path):
    r, client, rec, cap = make(tmp_path, cap=50, mean_gap_s=5.0)

    async def feeder():
        await asyncio.sleep(0.3)
        cap.valid["SOLUSDT"] = False
        await asyncio.sleep(100)
    asyncio.run(run_until_done(r, feeder=feeder))
    assert r.killed and r.killed.startswith("feed UNSAFE") and r.stop_reason.startswith("kill switch: feed UNSAFE")
    _, events = read(rec)
    assert any(e["type"] == "kill_switch" for e in events)


def test_the_stop_file_is_a_manual_kill_switch(tmp_path):
    stop_file = tmp_path / "STOP"
    r, client, rec, cap = make(tmp_path, cap=50, mean_gap_s=5.0, stop_file=str(stop_file))

    async def feeder():
        await asyncio.sleep(0.3)
        stop_file.write_text("stop")
        await asyncio.sleep(100)
    asyncio.run(run_until_done(r, feeder=feeder))
    assert r.killed == "manual stop (stop file)"


def test_the_total_loss_cap_stops_the_run(tmp_path):
    r, client, rec, cap = make(tmp_path, cap=50, mean_gap_s=5.0, loss_cap_total=Decimal(200))

    async def feeder():
        await asyncio.sleep(0.2)
        r.acct["SOLUSDT"]["cash"] = Decimal(-250)
        await asyncio.sleep(100)
    asyncio.run(run_until_done(r, feeder=feeder))
    assert r.killed and r.killed.startswith("total loss cap") and r.max_loss_seen >= Decimal(250)


def test_the_daily_loss_cap_pauses_new_orders_and_flattens(tmp_path):
    r, client, rec, cap = make(tmp_path, cap=500, mean_gap_s=0.05, loss_cap_day=Decimal(20), loss_cap_total=Decimal(200))

    async def feeder():
        await asyncio.sleep(0.4)
        r.acct["SOLUSDT"]["cash"] -= Decimal(25)
        await asyncio.sleep(0.8)
        r.stop.set()
    asyncio.run(run_until_done(r, feeder=feeder))
    assert r.stats["day_pauses"] == 1 and r.paused_until_day is not None and r.killed is None
    orders, events = read(rec)
    assert any(e["type"] == "day_loss_cap" for e in events)


def test_an_order_the_venue_accepted_but_nobody_can_see_is_an_unknown_state_and_kills(tmp_path):
    class Silent(PaperClient):
        def _emit(self, *a, **k):
            return None

        async def open_orders(self, symbol):
            return []
    r, client, rec, cap = make(tmp_path, cap=5, client_cls=Silent)
    asyncio.run(run_until_done(r))
    assert r.killed and r.killed.startswith("unknown order state")


def test_the_pre_move_is_the_signed_mid_change_over_the_window(tmp_path):
    r, client, rec, cap = make(tmp_path, cap=0)
    now = time.monotonic()
    r.mids["SOLUSDT"].extend([(now - 1.2, Decimal("100.00")), (now - 0.5, Decimal("100.02")), (now, Decimal("99.97"))])
    assert r.pre_move_bps("SOLUSDT", 1.0) == pytest.approx(-3.0, abs=0.01)                      # a fall of 3 bps in the last second
    assert r.pre_move_bps("SOLUSDT", 30.0) is None


def test_a_mainnet_v2_run_is_refused_without_confirmation_caps_or_keys_and_sends_nothing(monkeypatch, tmp_path, capsys):
    import argparse
    import aiohttp
    from ollog import fillprobe
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: (_ for _ in ()).throw(AssertionError("session created")))
    monkeypatch.delenv("OLLOG_MAINNET", raising=False)
    base = dict(network="mainnet", markets="bybit_linear,binance_futures", coins_list="SOLUSDT,ADAUSDT,MNTUSDT", capture_only=False, duration_s=1, max_orders=100, hold_min_s=1, hold_max_s=2, spacing_s=1,
                max_notional=Decimal(10), max_net_notional=Decimal(200), no_flatten=False, no_preflight=False, check_every_s=0, max_consecutive_errors=5, seed=1, confirm_mainnet=None, out=str(tmp_path),
                plan="perp_v2", paper=False, max_open=2, mean_gap_s=90.0, loss_cap_day=Decimal(20), loss_cap_total=Decimal(200), stop_file=None, fast_streams=False)
    assert asyncio.run(fillprobe.run_fillprobe(argparse.Namespace(**base))) == 2
    assert "refused (nothing was sent)" in capsys.readouterr().err and not list(tmp_path.iterdir())
    monkeypatch.setenv("OLLOG_MAINNET", "1")
    for loose in (dict(loss_cap_total=Decimal(201)), dict(max_open=3), dict(coins_list="BTCUSDT")):               # inside the confirmation but outside a ceiling
        rc = asyncio.run(fillprobe.run_fillprobe(argparse.Namespace(**{**base, "confirm_mainnet": mainnet.CONFIRM_PHRASE, **loose})))
        assert rc == 2 and "refused (nothing was sent)" in capsys.readouterr().err
    for n in ("BYBIT_MAINNET_KEY", "BYBIT_MAINNET_SECRET", "BINANCE_FUT_MAINNET_KEY", "BINANCE_FUT_MAINNET_SECRET"):
        monkeypatch.delenv(n, raising=False)
    rc = asyncio.run(fillprobe.run_fillprobe(argparse.Namespace(**{**base, "confirm_mainnet": mainnet.CONFIRM_PHRASE})))
    assert rc == 2 and "missing mainnet credentials" in capsys.readouterr().err
