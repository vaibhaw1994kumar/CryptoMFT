"""The lifecycle loop against a scripted fake client: derived numbers, safety caps, cancel-on-exit."""
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ollog import runner as runner_mod  # noqa: E402
from ollog.models import Instrument, RestResult, UserEvent  # noqa: E402
from ollog.recorder import Recorder  # noqa: E402
from ollog.runner import Budget, MarketRunner, RunConfig  # noqa: E402
from ollog.summary import summarize  # noqa: E402

MS = 1_000_000
INST = Instrument("fake", "SOLUSDT", "SOL", "USDT", Decimal("0.01"), Decimal("0.001"), Decimal("0.001"), Decimal(5))


class FakeClient:
    """Scripted venue. place/cancel return fixed stamps; the stream callback fires right after each REST call."""
    market = "fake"

    def __init__(self, place_ok=True, cancel_ok=True, probe_rejects=True, cancel_raises=None, stream_after=None):
        self.place_ok, self.cancel_ok, self.probe_rejects, self.cancel_raises = place_ok, cancel_ok, probe_rejects, cancel_raises
        self.stream_alive, self.stream_error, self.offset_ms = False, None, 0
        self.on_event = None
        self.live, self.max_live, self.placed, self.cancelled, self.closed = {}, 0, [], [], False
        self.other_open = [{"order_id": "foreign", "client_id": "someone-else"}]
        self.sweep_calls = 0
        self.books: dict[str, tuple[Decimal, Decimal]] = {}
        self.crossing_reject = ("170213", "post only would take")
        self.probe_symbols: list[str] = []

    async def server_time_ms(self):
        return 1_000_100, 1_000_000 * MS, 1_000_020 * MS      # offset = 1_000_100 - 1_000_010 = +90 ms, rtt 20 ms

    async def start_stream(self, on_event):
        self.on_event, self.stream_alive = on_event, True

    async def best_bid_ask(self, symbol):
        return self.books.get(symbol, (Decimal("100.00"), Decimal("100.01")))

    async def reference_price(self, symbol):
        return None

    def _emit(self, kind, cid, oid, status, mono, ev_ms):
        self.on_event(UserEvent("fake", kind, "SOLUSDT", oid, cid, ev_ms, ev_ms, status, recv_wall_ns=ev_ms * MS, recv_mono_ns=mono))

    async def place(self, symbol, price, qty, cid):
        self.placed.append(cid)
        crossing = price >= Decimal("100.01")
        if crossing:
            self.probe_symbols.append(symbol)
        base = len(self.placed) * 10_000
        res = RestResult(ok=self.place_ok and not (crossing and self.probe_rejects), order_id=f"o{len(self.placed)}",
                         ex_time_ms=1_000_150, code="" if self.place_ok else "10001", msg="" if self.place_ok else "boom",
                         http_status=200, t_send_wall_ns=1_000_100 * MS, t_send_mono_ns=base * MS, t_ret_wall_ns=1_000_140 * MS,
                         t_ret_mono_ns=(base + 40) * MS)
        if res.ok:
            self.live[cid] = res.order_id
            self.max_live = max(self.max_live, len(self.live))
            self._emit("new", cid, res.order_id, "New", (base + 90) * MS, 1_000_160)
        elif crossing and self.place_ok:
            res.code, res.msg = self.crossing_reject
        return res

    async def cancel(self, symbol, order_id, cid):
        if self.cancel_raises is not None and cid not in self.cancelled:
            exc, self.cancel_raises = self.cancel_raises, None
            raise exc
        self.cancelled.append(cid)
        was_live = self.live.pop(cid, None) is not None
        base = len(self.placed) * 10_000 + 2_000
        if self.cancel_ok and was_live:
            self._emit("cancelled", cid, order_id or "", "Cancelled", (base + 80) * MS, 1_002_140)
        return RestResult(ok=self.cancel_ok and was_live, order_id=order_id, ex_time_ms=1_002_120, http_status=200,
                          t_send_wall_ns=1_002_100 * MS, t_send_mono_ns=base * MS, t_ret_wall_ns=1_002_130 * MS, t_ret_mono_ns=(base + 30) * MS)

    async def open_orders(self, symbol):
        self.sweep_calls += 1
        return [{"order_id": o, "client_id": c} for c, o in self.live.items()] + self.other_open

    async def close(self):
        self.closed = True


def make(tmp_path, client=None, cap=3, symbols=("SOLUSDT",), **cfg_kw):
    runner_mod.MIN_SPACING_S = 0.0
    cfg = RunConfig(hold_s=0.0, hold_jitter_s=0.0, spacing_s=0.0, ack_timeout_s=0.5, cancel_timeout_s=0.5, tag="t", **cfg_kw)
    rec = Recorder(tmp_path, "fake")
    client = client or FakeClient()
    stop = asyncio.Event()
    r = MarketRunner(client, rec, list(symbols), {x: INST for x in symbols}, cfg, Budget(cap), stop)
    return r, client, rec, stop


def orders(rec):
    rec.close()
    return [json.loads(x) for x in (rec.dir / "orders.jsonl").read_text().splitlines()]


def test_clean_lifecycle_records_every_timestamp_and_the_derived_latencies(tmp_path):
    r, client, rec, _ = make(tmp_path, cap=1)
    asyncio.run(r.run())
    (o,) = orders(rec)
    assert o["outcome"] == "cancelled" and o["clean"] and o["errors"] == [] and o["label"] == "testnet-measured"
    assert o["clock"]["offset_ms"] == 90.0 and o["clock"]["uncertainty_ms"] == 10.0
    d = o["derived"]["local_ms"]
    assert (d["place_rest_rtt"], d["place_send_to_ws_new"], d["ws_new_after_rest_return"]) == (40, 90, 50)
    assert (d["cancel_rest_rtt"], d["cancel_send_to_ws_cancelled"], d["ws_cancelled_after_cancel_rest_return"]) == (30, 80, 50)
    assert o["derived"]["exchange_referenced_ms"]["place_send_to_exchange_new_event"] == 1_000_160 - (1_000_100 + 90)
    assert r.stop_reason == "order cap reached" and client.closed and not client.live


def test_hard_cap_is_shared_and_exact(tmp_path):
    b = Budget(4)
    r1, c1, rec1, _ = make(tmp_path / "a", cap=1)
    r2, c2, rec2, stop = make(tmp_path / "b", cap=1)
    r1.budget = r2.budget = b

    async def go():
        await asyncio.gather(r1.run(), r2.run())
    asyncio.run(go())
    assert len(c1.placed) + len(c2.placed) == 4
    assert Budget(10 ** 6).total == runner_mod.HARD_MAX_ORDERS


def test_one_order_live_at_a_time_and_cleanup_leaves_nothing(tmp_path):
    r, client, rec, _ = make(tmp_path, cap=5)
    asyncio.run(r.run())
    assert client.max_live == 1 and len(client.placed) == 5 and not client.live


def test_stops_after_k_consecutive_errors(tmp_path):
    r, client, rec, _ = make(tmp_path, FakeClient(place_ok=False), cap=50, max_consecutive_errors=3)
    asyncio.run(r.run())
    os_ = orders(rec)
    assert len(os_) == 3 and r.stop_reason == "3 consecutive errors"
    assert all(o["outcome"] == "rejected_on_rest" and o["reject_code"] == "10001" for o in os_)


def test_a_clean_order_resets_the_error_count(tmp_path):
    r, client, rec, _ = make(tmp_path, cap=6, max_consecutive_errors=3)
    flip = {"n": 0}
    real = client.place

    async def flaky(symbol, price, qty, cid):
        flip["n"] += 1
        client.place_ok = flip["n"] % 3 != 0                    # every third order is refused
        return await real(symbol, price, qty, cid)
    client.place = flaky
    asyncio.run(r.run())
    assert len(client.placed) == 6 and r.stop_reason == "order cap reached"


def test_reject_probe_is_expected_to_be_rejected_and_counts_as_clean(tmp_path):
    r, client, rec, _ = make(tmp_path, cap=4, reject_probe_every=2)
    asyncio.run(r.run())
    os_ = orders(rec)
    probes = [o for o in os_ if o["kind"] == "reject_probe"]
    assert len(os_) == 4 and len(probes) == 2
    assert all(p["outcome"] == "rejected_on_rest" and p["clean"] and p["reject_code"] == "170213" for p in probes)
    assert r.stats["errors"] == 0


def test_cleanup_cancels_the_live_order_and_sweeps_only_our_prefix_when_the_loop_is_interrupted(tmp_path):
    client = FakeClient(cancel_raises=asyncio.CancelledError())
    r, client, rec, _ = make(tmp_path, client, cap=3)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(r.run())
    assert client.cancelled == ["ollogt000001"] and not client.live         # the interrupted cancel was retried by cleanup
    assert "foreign" not in " ".join(client.cancelled) and client.closed
    events = [json.loads(x) for x in (rec.dir / "events.jsonl").read_text().splitlines()]
    assert any(e["type"] == "cleanup" for e in events) and any(e["type"] == "run_end" and e["reason"].startswith("aborted") for e in events)


def test_stop_event_ends_the_loop_and_still_cleans_up(tmp_path):
    r, client, rec, stop = make(tmp_path, cap=1000)
    real = client.place

    async def place_then_stop(symbol, price, qty, cid):
        res = await real(symbol, price, qty, cid)
        if len(client.placed) == 2:
            stop.set()
        return res
    client.place = place_then_stop
    asyncio.run(r.run())
    assert len(client.placed) == 2 and r.stop_reason == "stop requested" and not client.live and client.sweep_calls >= 1


def test_a_dead_stream_stops_the_market_before_another_order(tmp_path):
    r, client, rec, _ = make(tmp_path, cap=10)
    real = client.place

    async def place_then_drop(symbol, price, qty, cid):
        res = await real(symbol, price, qty, cid)
        client.stream_alive, client.stream_error = False, "ConnectionClosed"
        return res
    client.place = place_then_drop
    asyncio.run(r.run())
    assert len(client.placed) == 1 and r.stop_reason.startswith("private stream down")


def test_symbol_that_cannot_be_quoted_is_skipped_without_sending_or_spending_the_cap(tmp_path):
    r, client, rec, _ = make(tmp_path, cap=3)
    r.inst = {"SOLUSDT": Instrument("fake", "SOLUSDT", "SOL", "USDT", Decimal("0.01"), Decimal("0.001"), Decimal("0.001"), Decimal(10 ** 6))}
    asyncio.run(r.run())
    assert client.placed == [] and r.budget.used == 0 and r.stop_reason == "no symbol could be quoted"


def test_missing_stream_confirmation_is_flagged_not_hidden(tmp_path):
    r, client, rec, _ = make(tmp_path, cap=1)
    client.cancel_ok = False
    asyncio.run(r.run())
    (o,) = orders(rec)
    assert "cancel_failed" in o["errors"] and not o["clean"] and o["outcome"] == "cancel_failed"


def test_summary_has_no_ids_and_labels_the_data_as_testnet(tmp_path):
    r, client, rec, _ = make(tmp_path, cap=3)
    asyncio.run(r.run())
    rec.close()
    doc = summarize(rec.dir)
    text = (rec.dir / "summary.json").read_text()
    assert doc["label"] == "testnet-measured" and doc["orders"] == 3 and doc["clean_orders"] == 3
    assert "ollog" not in text and "o1" not in text.replace("outcomes", "") and "client_id" not in text
    assert doc["latency_local_ms"]["place_rest_rtt"]["n"] == 3 and doc["latency_local_ms"]["place_rest_rtt"]["p50"] is None
    assert "TESTNET" in doc["caveat"] and "no race rate" in doc["caveat"]


def test_sweep_cancels_an_orphan_with_our_prefix_that_the_loop_never_tracked(tmp_path):
    client = FakeClient()
    client.live["ollogt999999"] = "orphan"                       # e.g. a place whose reply was lost after the exchange accepted it
    r, client, rec, _ = make(tmp_path, client, cap=1)
    asyncio.run(r.run())
    assert "ollogt999999" in client.cancelled and "foreign" not in client.cancelled and "ollogt999999" not in client.live


def test_cancel_live_works_without_the_sweep(tmp_path):
    client = FakeClient(cancel_raises=asyncio.CancelledError())
    r, client, rec, _ = make(tmp_path, client, cap=3)
    real = client.open_orders
    calls = {"n": 0}

    async def blind_sweep(symbol):                              # the exchange's open-orders list shows nothing (lag / outage)
        calls["n"] += 1
        return []
    client.open_orders = blind_sweep
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(r.run())
    assert client.cancelled == ["ollogt000001"] and not client.live and calls["n"] >= 2


def test_spacing_between_sends_is_enforced_with_a_floor(tmp_path):
    r, client, rec, _ = make(tmp_path, cap=3, )
    saved = runner_mod.MIN_SPACING_S
    runner_mod.MIN_SPACING_S = 0.2
    r.cfg.spacing_s = 0.01
    waits = []

    async def record(seconds):
        waits.append(seconds)
    r._sleep = record
    try:
        asyncio.run(r.run())
    finally:
        runner_mod.MIN_SPACING_S = saved
    assert len(client.placed) == 3 and waits.count(0.2) == 3 and all(w >= 0.2 for w in waits if w)


class CapClient(FakeClient):
    """Refuses any buy above `limit` with Bybit's 170193 reply, like a stale testnet book whose bid is above the venue's limit."""

    def __init__(self, limit, **kw):
        super().__init__(**kw)
        self.limit, self.prices = Decimal(limit), []

    async def place(self, symbol, price, qty, cid):
        self.prices.append(price)
        if price > self.limit:
            return RestResult(ok=False, code="170193", msg=f"Buy order price cannot be higher than {self.limit}USDT.", http_status=200,
                              t_send_wall_ns=1_000_100 * MS, t_send_mono_ns=10 * MS, t_ret_wall_ns=1_000_140 * MS, t_ret_mono_ns=50 * MS)
        return await super().place(symbol, price, qty, cid)


def test_price_limit_rejection_is_learned_then_orders_are_placed_under_it_without_counting_as_errors(tmp_path):
    client = CapClient("90.00")                                 # bid is 100.00, so the first order (99.50) is refused
    r, client, rec, _ = make(tmp_path, client, cap=3, max_consecutive_errors=1, cap_margin_bps=Decimal(0))       # any real error would stop the run at once
    asyncio.run(r.run())
    o = orders(rec)
    assert [x["outcome"] for x in o] == ["rejected_price_limit", "cancelled", "cancelled"]
    assert o[0]["learned_price_cap"] == "90.00" and "price_cap_applied" not in o[0]
    assert all(Decimal(x["price"]) <= Decimal("90.00") for x in o[1:]) and o[1]["price_cap_applied"] == "90.00"
    assert r.stats["errors"] == 0 and r.stats["price_limit_learned"] == 1 and r.stats["clean"] == 2
    assert r.stop_reason == "order cap reached"                 # the learning order spent a slot of the hard cap
    assert client.live == {}


def test_a_cap_that_does_not_get_tighter_is_a_real_error(tmp_path):
    class Stubborn(CapClient):
        async def place(self, symbol, price, qty, cid):
            res = await super().place(symbol, Decimal("999"), qty, cid)          # always sends a price above the limit
            self.prices[-1] = price
            return res

    r, client, rec, _ = make(tmp_path, Stubborn("90.00"), cap=6, max_consecutive_errors=2, cap_margin_bps=Decimal(0))
    asyncio.run(r.run())
    o = orders(rec)
    assert [x["outcome"] for x in o] == ["rejected_price_limit", "rejected_on_rest", "rejected_on_rest"]
    assert r.stats["errors"] == 2 and r.stop_reason == "2 consecutive errors"


def test_summary_keeps_price_limit_rejections_out_of_the_latency_figures(tmp_path):
    r, _, rec, _ = make(tmp_path, CapClient("90.00"), cap=3, cap_margin_bps=Decimal(0))
    asyncio.run(r.run())
    orders(rec)
    doc = summarize(rec.dir)
    assert doc["outcomes"] == {"rejected_price_limit": 1, "cancelled": 2}
    assert doc["price_limit"] == {"learned": 1, "orders_at_learned_cap": 2}
    assert doc["latency_local_ms"]["place_rest_rtt"]["n"] == 2                  # the refused order's RTT is not mixed in


def test_margin_under_the_cap_absorbs_a_drifting_venue_limit(tmp_path):
    class Drifting(CapClient):
        """The venue's limit falls 0.05% after every order, like a reference price that moves while we run."""
        async def place(self, symbol, price, qty, cid):
            res = await super().place(symbol, price, qty, cid)
            self.limit = self.limit * Decimal("0.9995")
            return res

    r, client, rec, _ = make(tmp_path, Drifting("90.00"), cap=12, max_consecutive_errors=1)       # default 100 bps margin
    asyncio.run(r.run())
    o = orders(rec)
    assert r.stats["price_limit_learned"] == 1 and r.stats["errors"] == 0 and r.stats["clean"] == 11
    assert o[1]["price_cap_applied"] == "89.1000" and Decimal(o[1]["price"]) == Decimal("89.10")       # 90.00 less 100 bps, on the tick

    r2, _, rec2, _ = make(tmp_path / "nomargin", Drifting("90.00"), cap=12, max_consecutive_errors=1, cap_margin_bps=Decimal(0))
    asyncio.run(r2.run())
    assert r2.stats["price_limit_learned"] > 1 or r2.stats["errors"] > 0          # without the margin the same drift keeps rejecting


FIVE = ("AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT", "EEEUSDT")


def test_probes_rotate_over_symbols_instead_of_aligning_with_the_normal_cycle(tmp_path):
    r, client, rec, _ = make(tmp_path, cap=40, symbols=FIVE, reject_probe_every=5)      # 5 symbols and every 5th: the old trap
    asyncio.run(r.run())
    probes = [o for o in orders(rec) if o["kind"] == "reject_probe"]
    assert len(probes) >= 6 and len({p["symbol"] for p in probes}) == 5
    assert all(p["post_only_reject"] and p["clean"] for p in probes)
    normal = [o["symbol"] for o in orders(rec) if o["kind"] == "normal"]
    counts = [normal.count(x) for x in FIVE]
    assert max(counts) - min(counts) <= 1                                    # probes do not starve any symbol of normal orders


def test_probe_waits_for_a_symbol_that_has_traded_cleanly(tmp_path):
    r, client, rec, _ = make(tmp_path, cap=3, reject_probe_every=1)
    asyncio.run(r.run())
    kinds = [o["kind"] for o in orders(rec)]
    assert kinds == ["normal", "reject_probe", "reject_probe"]              # due from order 1, sent once a symbol is clean


def test_probe_skips_a_symbol_with_no_bid_or_a_book_above_the_learned_cap(tmp_path):
    client = FakeClient()
    r, client, rec, _ = make(tmp_path, client, cap=12, symbols=("AAAUSDT", "BBBUSDT", "CCCUSDT"), reject_probe_every=3)
    client.books["AAAUSDT"] = (Decimal(0), Decimal("100.01"))               # one-sided book: never probed
    r.price_caps["BBBUSDT"] = Decimal("90")                                  # ask 100.01 is above the learned limit
    r.clean_symbols.update({"AAAUSDT", "BBBUSDT", "CCCUSDT"})
    asyncio.run(r.run())
    assert client.probe_symbols and set(client.probe_symbols) == {"CCCUSDT"}
    rec.close()
    events = [json.loads(x) for x in (rec.dir / "events.jsonl").read_text().splitlines()]
    assert not [e for e in events if e["type"] == "skipped" and e["probe"]]  # the picker never spends a probe slot on them


def test_probe_refused_for_another_reason_is_not_a_post_only_reject_and_is_not_an_error(tmp_path):
    client = FakeClient()
    client.crossing_reject = ("110094", "Order does not meet minimum order value 5USDT")
    r, client, rec, _ = make(tmp_path, client, cap=20, symbols=FIVE, reject_probe_every=2, max_consecutive_errors=2)
    asyncio.run(r.run())
    os_ = orders(rec)
    probes = [o for o in os_ if o["kind"] == "reject_probe"]
    assert len(probes) == 3 and r.stats["probe_invalid"] == 3 and r.stats["probe_post_only_rejects"] == 0     # then probing stops
    assert all(p["outcome"] == "probe_rejected_other" and not p["clean"] for p in probes)
    assert len({p["symbol"] for p in probes}) == 3                           # a refused symbol is not probed again
    assert r.stats["errors"] == 0 and r.stop_reason == "order cap reached"   # invalid probes do not trip the error stop
    doc = summarize(rec.dir)
    assert doc["reject_probes"]["post_only_rejected"] == 0 and doc["reject_probes"]["refused_for_other_reason"] == 3
    assert doc["reject_probes"]["other_reasons"] == {"110094": 3}


def test_summary_keeps_probes_out_of_the_lifecycle_latency_and_reports_them_apart(tmp_path):
    r, client, rec, _ = make(tmp_path, cap=6, reject_probe_every=3)
    asyncio.run(r.run())
    rec.close()
    doc = summarize(rec.dir)
    p = doc["reject_probes"]
    assert p["sent"] == 2 and p["post_only_rejected"] == 2 and p["post_only_reject_rest_rtt_ms"]["n"] == 2
    assert doc["latency_local_ms"]["place_rest_rtt"]["n"] == 4


def test_a_stream_side_probe_reject_is_timed_from_send_to_the_reject_event(tmp_path):
    class StreamReject(FakeClient):
        async def place(self, symbol, price, qty, cid):
            res = await super().place(symbol, price, qty, cid)
            if res.code == "170213":
                res.ok, res.code, res.msg, res.order_id = True, "0", "", "oX"
                self._emit("rejected", cid, "oX", "Rejected", res.t_ret_mono_ns + 70 * MS, 1_000_170)
                self.on_event_reason = "EC_PostOnlyWillTakeLiquidity"
            return res
    client = StreamReject()
    r, client, rec, _ = make(tmp_path, client, cap=2, reject_probe_every=2)
    orig = r.on_event

    def tag(ev):
        if ev.kind == "rejected":
            ev.reason = "EC_PostOnlyWillTakeLiquidity"
        orig(ev)
    client.on_event = tag
    r.on_event = tag
    asyncio.run(r.run())
    (probe,) = [o for o in orders(rec) if o["kind"] == "reject_probe"]
    assert probe["outcome"] == "rejected_on_stream" and probe["post_only_reject"] and probe["clean"]
    assert probe["derived"]["local_ms"]["place_send_to_ws_reject"] is not None


def test_bybit_linear_style_post_only_refusal_arrives_as_cancelled_and_is_not_cancelled_again(tmp_path):
    class InstantCancel(FakeClient):
        async def place(self, symbol, price, qty, cid):
            res = await super().place(symbol, price, qty, cid)
            if res.code == "170213":
                res.ok, res.code, res.msg, res.order_id = True, "0", "", "oY"
                self._emit("cancelled", cid, "oY", "Cancelled", res.t_ret_mono_ns + 60 * MS, 1_000_170)
            return res
    client = InstantCancel()
    r, client, rec, _ = make(tmp_path, client, cap=2, reject_probe_every=2)
    orig = r.on_event
    r.on_event = client.on_event = lambda ev: orig(_with_reason(ev))
    asyncio.run(r.run())
    (probe,) = [o for o in orders(rec) if o["kind"] == "reject_probe"]
    assert probe["outcome"] == "rejected_on_stream" and probe["post_only_reject"] and probe["clean"] and not probe["errors"]
    assert client.cancelled == ["ollogt000001"]                              # only the normal order was cancelled
    assert probe["derived"]["local_ms"]["place_send_to_ws_reject"] is not None
    assert r.stats["probe_post_only_rejects"] == 1 and r.stats["errors"] == 0


def _with_reason(ev):
    if ev.kind == "cancelled" and ev.order_id == "oY":
        ev.reason = "EC_PostOnlyWillTakeLiquidity"
    return ev
