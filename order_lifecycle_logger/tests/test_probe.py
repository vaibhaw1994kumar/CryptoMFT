"""At-touch fill probes against a scripted fake venue: fills are data, races are recorded, flatten is reduce-only and scoped."""
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ollog import runner as runner_mod  # noqa: E402
from ollog.models import Instrument, RestResult, UserEvent  # noqa: E402
from ollog.probe import ProbeConfig, ProbeRunner, execution_from_event  # noqa: E402
from ollog.recorder import Recorder  # noqa: E402
from ollog.runner import Budget  # noqa: E402

MS = 1_000_000
INST = Instrument("bybit_linear", "SOLUSDT", "SOL", "USDT", Decimal("0.01"), Decimal("0.1"), Decimal("0.1"), Decimal(5))


class Book:
    def __init__(self, bids, asks):
        self.bids, self.asks = bids, asks

    def top(self, n=20):
        return self.bids[:n], self.asks[:n]


class FakeCapture:
    def __init__(self, bids=((100.0, 7.0), (99.99, 3.0)), asks=((100.02, 4.0), (100.03, 2.0)), valid=True):
        self.books = {"SOLUSDT": Book([list(x) for x in bids], [list(x) for x in asks]), "ETHUSDT": Book([list(x) for x in bids], [list(x) for x in asks])}
        self.valid = {"SOLUSDT": valid, "ETHUSDT": valid}


class FakeClient:
    """script: list of per-order scripts (dict). Keys: fills=[(delay_s, cum_qty, price, status)], cancel_ok, stream_reject."""
    market = "bybit_linear"

    def __init__(self, script=None):
        self.script = list(script or [])
        self.placed, self.cancels, self.closes = [], [], []
        self.stream_alive, self.stream_error, self.offset_ms = False, None, 0
        self.position = {"SOLUSDT": Decimal(0), "ETHUSDT": Decimal(0)}
        self.on_event = None
        self.closed = False
        self.cur = {}

    async def server_time_ms(self):
        return 1_000_100, 1_000_000 * MS, 1_000_020 * MS

    async def start_stream(self, on_event):
        self.on_event, self.stream_alive = on_event, True

    async def open_orders(self, symbol):
        return []

    stream_variants = 1
    stream_variant = 0
    silent_variants: set = set()

    def _emit(self, kind, cid, status, raw, ms=1_793_000_000_500, delay=0.0):
        if self.stream_variant in self.silent_variants:
            return                                                                 # this stream variant delivers nothing

        async def later():
            await asyncio.sleep(delay)
            self.on_event(UserEvent("bybit_linear", kind, "SOLUSDT", "o", cid, ms, ms, status, "", recv_wall_ns=ms * MS, recv_mono_ns=ms * MS, raw=raw))
        asyncio.get_event_loop().create_task(later())

    async def place(self, symbol, price, qty, cid, side="Buy"):
        sc = self.script.pop(0) if self.script else {}
        self.placed.append({"symbol": symbol, "price": price, "qty": qty, "side": side, "cid": cid})
        self.cur[cid] = sc
        base = len(self.placed) * 1000
        if sc.get("rest_reject"):
            return RestResult(ok=False, code="110072", msg="x", t_send_wall_ns=1, t_send_mono_ns=base * MS, t_ret_wall_ns=2, t_ret_mono_ns=(base + 5) * MS)
        res = RestResult(ok=True, order_id=f"o{len(self.placed)}", ex_time_ms=1_793_000_000_400, http_status=200,
                         t_send_wall_ns=1_793_000_000_300 * MS, t_send_mono_ns=base * MS, t_ret_wall_ns=1_793_000_000_340 * MS, t_ret_mono_ns=(base + 40) * MS)
        if sc.get("stream_reject"):
            self._emit("rejected", cid, "Rejected", {}, delay=0.01)
            self.cur[cid]["_rejected"] = True
            return res
        self._emit("new", cid, "New", {"createdTime": "1793000000410", "updatedTime": "1793000000410"}, ms=1_793_000_000_410)
        cum = Decimal(0)
        for delay, cum_qty, px, status in sc.get("fills", []):
            self._emit("trade", cid, status, {"cumExecQty": str(cum_qty), "avgPrice": str(px), "updatedTime": str(1_793_000_001_000 + int(delay * 1000))},
                       ms=1_793_000_001_000, delay=delay)
        return res

    async def cancel(self, symbol, order_id, cid):
        sc = self.cur.get(cid, {})
        self.cancels.append(cid)
        ok = sc.get("cancel_ok", True)
        if ok:
            self._emit("cancelled", cid, "Cancelled", {}, ms=1_793_000_002_000)
        return RestResult(ok=ok, code="" if ok else "110001", msg="" if ok else "order not exists or too late to cancel",
                          t_send_wall_ns=1, t_send_mono_ns=1, t_ret_wall_ns=2, t_ret_mono_ns=2)

    async def position_size(self, symbol):
        return self.position[symbol]

    async def market_close(self, symbol, side, qty, cid):
        self.closes.append((symbol, side, qty))
        return RestResult(ok=True, order_id="c")

    async def close(self):
        self.closed = True

    async def restart_stream(self, on_event):
        self.restarts = getattr(self, "restarts", 0) + 1
        if getattr(self, "restart_fails", False):
            raise ConnectionError("down")
        self.on_event, self.stream_alive = on_event, True


def make(tmp_path, client=None, capture=None, cap=2, symbols=("SOLUSDT",), **kw):
    runner_mod.MIN_SPACING_S = 0.0
    kw.setdefault("preflight", False)
    cfg = ProbeConfig(hold_min_s=0.3, hold_max_s=0.3, spacing_s=0.0, ack_timeout_s=0.5, cancel_timeout_s=0.5, tag="p", **kw)
    rec = Recorder(tmp_path, "bybit_linear")
    client = client or FakeClient()
    stop = asyncio.Event()
    r = ProbeRunner(client, rec, list(symbols), {s: INST for s in symbols}, cfg, Budget(cap), stop, capture or FakeCapture())
    return r, client, rec


def orders(rec):
    rec.close()
    return [json.loads(x) for x in (rec.dir / "orders.jsonl").read_text().splitlines()]


def test_buy_joins_the_best_bid_and_sell_joins_the_best_ask_alternating(tmp_path):
    r, client, rec = make(tmp_path, cap=3, flatten=False)
    asyncio.run(r.run())
    assert [(p["side"], p["price"]) for p in client.placed] == [("Buy", Decimal("100.00")), ("Sell", Decimal("100.02")), ("Buy", Decimal("100.00"))]
    o = orders(rec)[0]
    assert o["best_bid_at_send"] == 100.0 and o["size_at_price_at_send"] == 7.0 and o["kind"] == "fill_probe"      # displayed queue at our level


def test_a_full_fill_is_data_not_an_error_and_no_cancel_is_sent(tmp_path):
    client = FakeClient([{"fills": [(0.05, "0.1", "100.0", "Filled")]}])
    r, client, rec = make(tmp_path, client, cap=1, flatten=False)
    asyncio.run(r.run())
    (o,) = orders(rec)
    assert o["outcome"] == "filled" and o["clean"] and o["errors"] == [] and client.cancels == []
    assert o["execs"][0]["qty"] == "0.1" and o["first_fill_ex_ms"] == 1_793_000_001_050 and o["filled_qty"] == "0.1"
    assert r.stats["filled_orders"] == 1 and r.stats["errors"] == 0 and r.net["SOLUSDT"] == Decimal("0.1")
    assert o["ex"]["created_ms"] == 1_793_000_000_410                              # Bybit createdTime, the order's arrival at the exchange


def test_partial_fills_accumulate_then_the_order_is_cancelled(tmp_path):
    client = FakeClient([{"fills": [(0.02, "0.04", "100.0", "PartiallyFilled"), (0.08, "0.07", "100.0", "PartiallyFilled")]}])
    r, client, rec = make(tmp_path, client, cap=1, flatten=False)
    asyncio.run(r.run())
    (o,) = orders(rec)
    assert o["outcome"] == "partial_then_cancelled" and client.cancels and o["filled_qty"] == "0.07"
    assert [e["qty"] for e in o["execs"]] == ["0.04", "0.03"]                       # increments of cumExecQty
    assert o["cancel"]["ok"] is True and o["clean"]


def test_a_cancel_refused_because_the_order_filled_is_recorded_and_the_fill_kept(tmp_path):
    client = FakeClient([{"fills": [(0.31, "0.1", "100.0", "Filled")], "cancel_ok": False}])     # fill lands just after the hold
    r, client, rec = make(tmp_path, client, cap=1, flatten=False)
    asyncio.run(r.run())
    (o,) = orders(rec)
    assert o["cancel"] == {"ok": False, "code": "110001", "msg": "order not exists or too late to cancel"}
    assert o["outcome"] == "filled" and o["filled_qty"] == "0.1" and r.stats["cancel_after_fill"] == 1 and r.stats["errors"] == 0


def test_unfilled_order_is_cancelled_and_labelled(tmp_path):
    r, client, rec = make(tmp_path, cap=1, flatten=False)
    asyncio.run(r.run())
    (o,) = orders(rec)
    assert o["outcome"] == "cancelled_unfilled" and o["filled_qty"] == "0" and o["first_fill_ex_ms"] is None and o["clean"]


def test_post_only_rejection_on_the_stream_is_recorded_not_cancelled(tmp_path):
    client = FakeClient([{"stream_reject": True}])
    r, client, rec = make(tmp_path, client, cap=1, flatten=False)
    asyncio.run(r.run())
    (o,) = orders(rec)
    assert o["outcome"] == "rejected_on_stream" and client.cancels == [] and r.stats["post_only_rejects"] == 1


def test_net_position_cap_forces_the_reducing_side(tmp_path):
    r, client, rec = make(tmp_path, cap=1, flatten=False, max_net_notional=Decimal(10))
    r.net["SOLUSDT"] = Decimal("0.5")                                              # 0.5 * 100 = 50 >= 10: long, so only sells
    r.side_n["SOLUSDT"] = 0                                                         # alternation alone would say Buy
    asyncio.run(r.run())
    assert client.placed[0]["side"] == "Sell"


def test_invalid_capture_book_sends_nothing_and_gives_up(tmp_path):
    r, client, rec = make(tmp_path, capture=FakeCapture(valid=False), cap=5, flatten=False, max_skips=3)
    asyncio.run(r.run())
    assert client.placed == [] and r.stop_reason == "no symbol could be quoted" and r.budget.used == 0


def test_flatten_is_reduce_only_on_traded_symbols_and_uses_the_venue_position(tmp_path):
    client = FakeClient([{"fills": [(0.02, "0.1", "100.0", "Filled")]}])
    client.position["SOLUSDT"] = Decimal("0.1")
    client.position["ETHUSDT"] = Decimal("3")                                      # not traded by this run: must be left alone
    r, client, rec = make(tmp_path, client, cap=1, symbols=("SOLUSDT", "ETHUSDT"))
    asyncio.run(r.run())
    assert client.closes == [("SOLUSDT", "Sell", Decimal("0.1"))] and client.closed
    events = [json.loads(x) for x in (rec.dir / "events.jsonl").read_text().splitlines()]
    assert [e["symbol"] for e in events if e["type"] == "flatten"] == ["SOLUSDT"]


def test_no_flatten_when_disabled_and_no_close_when_flat(tmp_path):
    client = FakeClient()
    r, client, rec = make(tmp_path, client, cap=1, flatten=False)
    asyncio.run(r.run())
    assert client.closes == []
    r2, c2, rec2 = make(tmp_path / "b", cap=1)
    asyncio.run(r2.run())
    assert c2.closes == []                                                          # position 0: nothing to close


def test_execution_parsing_for_both_venues():
    ev = UserEvent("bybit_linear", "trade", "S", "o", "c", 1, 1, "PartiallyFilled", raw={"cumExecQty": "0.5", "avgPrice": "100.1", "updatedTime": "5"})
    assert execution_from_event(ev, Decimal("0.2")) == (Decimal("0.3"), Decimal("100.1"), 5, Decimal("0.5"))
    assert execution_from_event(ev, Decimal("0.5")) is None                         # no new quantity: not an execution
    bn = UserEvent("binance_futures", "trade", "S", "o", "c", 1, 1, "PARTIALLY_FILLED",
                   raw={"e": "ORDER_TRADE_UPDATE", "o": {"x": "TRADE", "l": "0.4", "L": "99.9", "z": "0.4", "T": 7}})
    assert execution_from_event(bn, Decimal(0)) == (Decimal("0.4"), Decimal("99.9"), 7, Decimal("0.4"))
    new = UserEvent("binance_futures", "new", "S", "o", "c", 1, 1, "NEW", raw={"o": {"x": "NEW", "l": "0"}})
    assert execution_from_event(new, Decimal(0)) is None


def test_prices_round_toward_the_book_side_so_an_order_always_joins_and_never_crosses(tmp_path):
    cap = FakeCapture(bids=((100.004, 7.0),), asks=((100.025, 4.0),))               # both off the 0.01 tick
    r, client, rec = make(tmp_path, capture=cap, cap=2, flatten=False)
    asyncio.run(r.run())
    assert [(p["side"], p["price"]) for p in client.placed] == [("Buy", Decimal("100.00")), ("Sell", Decimal("100.03"))]    # buy rounds down, sell rounds up


def test_a_dropped_private_stream_is_resubscribed_between_orders_and_the_run_continues(tmp_path):
    client = FakeClient()
    r, client, rec = make(tmp_path, client, cap=3, flatten=False)
    real = client.place

    async def drop_after_first(symbol, price, qty, cid, side="Buy"):
        res = await real(symbol, price, qty, cid, side=side)
        if len(client.placed) == 1:
            asyncio.get_event_loop().call_later(0.5, lambda: setattr(client, "stream_alive", False))     # drops after the first order is done
        return res
    client.place = drop_after_first
    asyncio.run(r.run())
    assert client.restarts == 1 and len(client.placed) == 3 and r.stop_reason == "order cap reached" and r.stats["stream_reconnects"] == 1
    events = [json.loads(x) for x in (rec.dir / "events.jsonl").read_text().splitlines()]
    assert [e["type"] for e in events if e["type"].startswith("stream_reconnect")] == ["stream_reconnected"]


def test_reconnects_are_bounded_and_a_failed_resubscribe_stops_the_market(tmp_path):
    client = FakeClient()
    client.restart_fails = True
    r, client, rec = make(tmp_path, client, cap=5, flatten=False, max_stream_reconnects=2)
    client.stream_alive = True
    real_start = client.start_stream

    async def start_then_die(on_event):
        await real_start(on_event)
        client.stream_alive = False
    client.start_stream = start_then_die
    asyncio.run(r.run())
    assert client.placed == [] and r.stop_reason.startswith("private stream down") and client.restarts == 1


def test_reconnect_budget_is_enforced_when_the_stream_keeps_dropping(tmp_path):
    client = FakeClient()
    r, client, rec = make(tmp_path, client, cap=10, flatten=False, max_stream_reconnects=2)
    real = client.place

    async def drop_each(symbol, price, qty, cid, side="Buy"):
        res = await real(symbol, price, qty, cid, side=side)
        asyncio.get_event_loop().call_later(0.5, lambda: setattr(client, "stream_alive", False))
        return res
    client.place = drop_each
    asyncio.run(r.run())
    assert len(client.placed) == 3 and client.restarts == 2 and r.stop_reason.startswith("private stream down")     # 1 original + 2 reconnects


def test_preflight_places_one_far_order_proves_the_stream_and_then_the_run_proceeds(tmp_path):
    r, client, rec = make(tmp_path, cap=1, flatten=False, preflight=True)
    asyncio.run(r.run())
    far, probe = client.placed
    assert far["side"] == "Buy" and far["price"] < Decimal("99.6") and far["cid"].startswith("ollogpz") and probe["price"] == Decimal("100.00")
    assert len(client.cancels) == 2 and r.stop_reason == "order cap reached" and r.budget.used == 1        # the preflight order is outside the budget
    events = [json.loads(x) for x in (rec.dir / "events.jsonl").read_text().splitlines()]
    assert [e for e in events if e["type"] == "preflight"][0]["ack"] is True


def test_preflight_failure_stops_the_market_before_any_probe_order_and_cancels_the_far_order(tmp_path):
    client = FakeClient()
    client.silent_variants = {0}
    r, client, rec = make(tmp_path, client, cap=5, flatten=False, preflight=True)
    asyncio.run(r.run())
    assert len(client.placed) == 1 and client.placed[0]["cid"].startswith("ollogpz") and client.cancels            # only the far order; it was cancelled
    assert r.stop_reason.startswith("preflight failed") and r.stats["orders"] == 0


def test_preflight_tries_the_next_stream_variant_when_the_first_is_silent(tmp_path):
    client = FakeClient()
    client.stream_variants = 2
    client.silent_variants = {0}
    r, client, rec = make(tmp_path, client, cap=1, flatten=False, preflight=True)
    asyncio.run(r.run())
    assert client.stream_variant == 1 and client.restarts == 1 and r.stop_reason == "order cap reached" and len(client.placed) == 3   # far, far (retry), probe
    events = [json.loads(x) for x in (rec.dir / "events.jsonl").read_text().splitlines()]
    pf = [e for e in events if e["type"].startswith("preflight")]
    assert [(e["type"], e.get("variant"), e.get("ack")) for e in pf] == [("preflight", 0, False), ("preflight_failed", 0, None), ("preflight", 1, True)]


def test_binance_cumulative_execution_ignores_duplicated_updates():
    ev = UserEvent("binance_futures", "trade", "S", "o", "c", 1, 1, "PARTIALLY_FILLED", raw={"o": {"x": "TRADE", "l": "0.4", "L": "99.9", "z": "0.4", "T": 7}})
    q, p, t, cum = execution_from_event(ev, Decimal(0))
    assert q == Decimal("0.4")
    assert execution_from_event(ev, cum) is None                                    # the same update delivered twice adds nothing


def test_preflight_requires_the_cancel_confirmation_too(tmp_path):
    client = FakeClient([{"cancel_ok": False}])                                    # the far order is acked but its cancel is never confirmed on the stream
    r, client, rec = make(tmp_path, client, cap=3, flatten=False, preflight=True)
    asyncio.run(r.run())
    assert r.stop_reason.startswith("preflight failed") and len(client.placed) == 1
    events = [json.loads(x) for x in (rec.dir / "events.jsonl").read_text().splitlines()]
    assert [e["reason"] for e in events if e["type"] == "preflight_failed"] == ["no stream cancel confirmation"]
