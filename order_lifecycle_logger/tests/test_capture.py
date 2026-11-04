"""Public capture: sequencing, resync, REST alignment and the on-disk layout the backtester loads. Offline, no network."""
import asyncio
import gzip
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ollog import capture  # noqa: E402
from ollog.capture import BinanceCapture, BybitCapture, LocalBook, TardisWriter  # noqa: E402

RECV = (1_793_000_000_000_000_000, 5_000_000_000)


def read(root: Path, venue: str, stream: str, pair: str):
    out = []
    for f in sorted((root / venue / capture.LAYOUT[stream] / f"pair={pair}").glob("date=*/*.jsonl.gz")):
        with gzip.open(f, "rt") as fh:
            out += [json.loads(x) for x in fh if x.strip()]
    return out


class FakeSession:
    def __init__(self, payloads):
        self.payloads, self.urls = list(payloads), []

    def get(self, url):
        self.urls.append(url)
        return _Ctx(self.payloads.pop(0))


class _Ctx:
    def __init__(self, obj):
        self.obj = obj

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return json.dumps(self.obj)


def mk(cls, tmp_path, symbols=("SOLUSDT",), session=None, **kw):
    notes, checks = [], []
    w = TardisWriter(tmp_path, cls.venue)
    c = cls(list(symbols), w, notes.append, checks.append, lambda u: u, "https://rest.example", "wss://ws.example", session=session, **kw)
    return c, w, notes, checks


def bybit_ob(typ, u, b=(), a=(), sym="SOLUSDT", ts=1_793_000_000_100):
    return {"topic": f"orderbook.50.{sym}", "type": typ, "ts": ts, "cts": ts - 5, "data": {"s": sym, "b": [list(x) for x in b], "a": [list(x) for x in a], "u": u, "seq": u * 10}}


def test_bybit_snapshot_then_deltas_write_absolute_levels_and_trades(tmp_path):
    c, w, notes, _ = mk(BybitCapture, tmp_path)

    async def go():
        await c.handle(bybit_ob("snapshot", 10, [("100.0", "5"), ("99.9", "3")], [("100.1", "4")]), RECV)
        await c.handle(bybit_ob("delta", 11, [("100.0", "2")], [("100.1", "0"), ("100.2", "7")]), RECV)
        await c.handle({"topic": "publicTrade.SOLUSDT", "data": [{"T": 1_793_000_000_200, "s": "SOLUSDT", "S": "Sell", "v": "1.5", "p": "100.0", "i": "x"},
                                                                  {"T": 1_793_000_000_300, "s": "SOLUSDT", "S": "Buy", "v": "0.5", "p": "100.2", "i": "y"}]}, RECV)
    asyncio.run(go())
    w.close()
    snap = read(tmp_path, "bybit", "snapshot", "SOLUSDT")
    ev = read(tmp_path, "bybit", "event", "SOLUSDT")
    tr = read(tmp_path, "bybit", "trade", "SOLUSDT")
    assert len(snap) == 1 and snap[0]["best_bid"] == 100.0 and snap[0]["best_ask"] == 100.1 and snap[0]["bids"][0] == [100.0, 5.0]
    assert [(e["side"], e["price"], e["volume"], e["event_type"]) for e in ev] == [
        ("bid", 100.0, 2.0, "modify"), ("ask", 100.1, 0.0, "delete"), ("ask", 100.2, 7.0, "modify")]
    assert ev[0]["exchange_ts"] == pytest.approx(1_793_000_000.095) and ev[0]["local_ts"] == pytest.approx(1_793_000_000.0)
    assert [(t["side"], t["price"], t["volume"]) for t in tr] == [("s", 100.0, 1.5), ("b", 100.2, 0.5)]    # S is the taker side
    assert c.books["SOLUSDT"].top() == ([[100.0, 2.0], [99.9, 3.0]], [[100.2, 7.0]]) and not notes


def test_bybit_gap_stops_the_book_until_a_fresh_snapshot_and_is_recorded(tmp_path):
    c, w, notes, _ = mk(BybitCapture, tmp_path)
    sent = []

    class WS:
        async def send(self, m):
            sent.append(json.loads(m))

    async def go():
        await c.handle(bybit_ob("snapshot", 10, [("100", "5")], [("101", "4")]), RECV)
        await c.handle(bybit_ob("delta", 12, [("100", "1")]), RECV, WS())          # u jumps 10 -> 12
        await c.handle(bybit_ob("delta", 13, [("100", "9")]), RECV, WS())          # dropped: book is not valid
        await c.handle(bybit_ob("snapshot", 14, [("100", "7")], [("101", "4")]), RECV)
        await c.handle(bybit_ob("delta", 15, [("100", "8")]), RECV)
    asyncio.run(go())
    w.close()
    assert [n["type"] for n in notes] == ["gap"] and notes[0]["expected"] == 11 and notes[0]["got"] == 12
    assert [m["op"] for m in sent] == ["unsubscribe", "subscribe"]
    ev = read(tmp_path, "bybit", "event", "SOLUSDT")
    assert [e["volume"] for e in ev] == [8.0]                                       # the gap deltas were never applied
    assert c.counts["dropped_before_snapshot"] == 1 and c.books["SOLUSDT"].bids == {100.0: 8.0}


def test_bybit_u_equals_one_is_a_service_restart_snapshot(tmp_path):
    c, w, notes, _ = mk(BybitCapture, tmp_path)

    async def go():
        await c.handle(bybit_ob("snapshot", 50, [("100", "5")], [("101", "4")]), RECV)
        await c.handle(bybit_ob("delta", 1, [("100", "2")], [("101", "3")]), RECV)
    asyncio.run(go())
    assert not notes and c.books["SOLUSDT"].bids == {100.0: 2.0} and c.last_u["SOLUSDT"] == 1


def test_compare_aligns_on_update_id_and_reports_mismatch(tmp_path):
    c, w, _, _ = mk(BybitCapture, tmp_path)

    async def go():
        await c.handle(bybit_ob("snapshot", 10, [("100", "5"), ("99", "2")], [("101", "4"), ("102", "1")]), RECV)
        await c.handle(bybit_ob("delta", 11, [("100", "6")]), RECV)
    asyncio.run(go())
    ok = c.compare("SOLUSDT", 110, [["100", "6"], ["99", "2"]], [["101", "4"], ["102", "1"]])
    assert ok["aligned"] and ok["match"] and ok["top1_match"] and ok["k"] == 2
    bad = c.compare("SOLUSDT", 110, [["100", "5"], ["99", "2"]], [["101", "4"], ["102", "1"]])
    assert bad["aligned"] and not bad["match"] and not bad["top1_match"] and bad["asks_match"] and not bad["bids_match"]
    assert c.compare("SOLUSDT", 99, [["100", "6"]], [["101", "4"]])["aligned"] is False     # no state at that id: not guessed


def bn_depth(U, u, pu, b=(), a=(), sym="SOLUSDT", T=1_793_000_000_100):
    return {"stream": f"{sym.lower()}@depth@100ms", "data": {"e": "depthUpdate", "E": T + 3, "T": T, "s": sym, "U": U, "u": u, "pu": pu,
                                                           "b": [list(x) for x in b], "a": [list(x) for x in a]}}


def test_binance_syncs_on_snapshot_and_buffered_diffs_and_checks_pu_chain(tmp_path):
    snap = {"lastUpdateId": 100, "T": 1_793_000_000_050, "bids": [["100", "5"]], "asks": [["101", "4"]]}
    c, w, notes, _ = mk(BinanceCapture, tmp_path, session=FakeSession([snap]))

    async def go():
        await c.handle(bn_depth(90, 99, 89, [("100", "1")]), RECV)                  # before the snapshot: dropped (u < 100)
        await c.handle(bn_depth(98, 102, 97, [("100", "6")]), RECV)                 # straddles: U <= 100 <= u  -> first processed
        await c.handle(bn_depth(103, 105, 102, a=[("101", "0")]), RECV)
        await asyncio.sleep(0.6)                                                    # let the sync task run
        await c.handle(bn_depth(106, 107, 105, [("102", "3")], [("103", "2")]), RECV)
    asyncio.run(go())
    w.close()
    assert c.valid["SOLUSDT"] and not notes and c.counts["syncs"] == 1
    assert c.books["SOLUSDT"].asks == {103.0: 2.0} and c.books["SOLUSDT"].bids == {100.0: 6.0, 102.0: 3.0}
    assert read(tmp_path, "binance", "snapshot", "SOLUSDT")[0]["bids"] == [[100.0, 5.0]]
    assert [e["volume"] for e in read(tmp_path, "binance", "event", "SOLUSDT")] == [6.0, 0.0, 3.0, 2.0]   # the pre-snapshot diff is not written


def test_binance_pu_break_invalidates_and_triggers_a_new_sync(tmp_path):
    s1 = {"lastUpdateId": 100, "T": 1, "bids": [["100", "5"]], "asks": [["101", "4"]]}
    s2 = {"lastUpdateId": 200, "T": 2, "bids": [["100", "9"]], "asks": [["101", "4"]]}
    c, w, notes, _ = mk(BinanceCapture, tmp_path, session=FakeSession([s1, s2]))

    async def go():
        await c.handle(bn_depth(99, 101, 98), RECV)
        await asyncio.sleep(0.6)
        assert c.valid["SOLUSDT"]
        await c.handle(bn_depth(150, 160, 120), RECV)                               # pu != last u (101): a gap
        assert not c.valid["SOLUSDT"] and notes[0]["type"] == "gap"
        await c.handle(bn_depth(195, 205, 190, [("100", "10")]), RECV)              # buffered for the new sync
        await asyncio.sleep(0.6)
    asyncio.run(go())
    assert c.valid["SOLUSDT"] and c.counts["syncs"] == 2 and c.books["SOLUSDT"].bids == {100.0: 10.0}


def test_binance_trade_side_is_the_taker_side(tmp_path):
    c, w, _, _ = mk(BinanceCapture, tmp_path)

    async def go():
        await c.handle({"data": {"e": "aggTrade", "s": "SOLUSDT", "T": 1_793_000_000_400, "p": "100.5", "q": "2", "m": True}}, RECV)    # buyer is maker -> taker sold
        await c.handle({"data": {"e": "aggTrade", "s": "SOLUSDT", "T": 1_793_000_000_500, "p": "100.6", "q": "1", "m": False}}, RECV)
    asyncio.run(go())
    w.close()
    assert [t["side"] for t in read(tmp_path, "binance", "trade", "SOLUSDT")] == ["s", "b"]


def test_every_url_goes_through_the_guard(tmp_path):
    seen = []
    snap = {"lastUpdateId": 1, "T": 1, "bids": [], "asks": []}
    c, w, _, _ = mk(BinanceCapture, tmp_path, session=FakeSession([snap]))
    c.guard = lambda u: seen.append(u) or u

    async def go():
        await c._get_json("/fapi/v1/depth", {"symbol": "SOLUSDT", "limit": 5})
    asyncio.run(go())
    assert seen and seen[0].startswith("https://rest.example/fapi/v1/depth?")
    from ollog import endpoints
    for u in endpoints.PUBLIC_WS.values():
        endpoints.assert_testnet_url(u)
    with pytest.raises(endpoints.NotTestnet):
        endpoints.assert_testnet_url("wss://fstream.binance.com/stream?streams=x")


def test_writer_layout_matches_the_loader_and_survives_a_flush_without_close(tmp_path):
    w = TardisWriter(tmp_path, "bybit")
    w.write("trade", "SOLUSDT", 1_793_000_000.5, {"price": 1.0})
    w.flush()                                                                       # no close: a killed process
    f = next((tmp_path / "bybit" / "trades" / "pair=SOLUSDT").glob("date=*/*.jsonl.gz"))
    assert f.name.endswith(".jsonl.gz") and f.parent.name.startswith("date=2026-")
    with gzip.open(f, "rt") as fh:
        try:
            assert json.loads(fh.readline())["price"] == 1.0
        except EOFError:
            pytest.fail("flushed data must be readable")


def test_local_book_top_orders_and_deletes():
    b = LocalBook()
    for s, p, z in (("bid", 1, 1), ("bid", 3, 1), ("bid", 2, 1), ("ask", 5, 1), ("ask", 4, 1)):
        b.set(s, p, z)
    b.set("bid", 3, 0)
    assert b.top(5) == ([[2, 1], [1, 1]], [[4, 1], [5, 1]])


def _ring_capture(tmp_path):
    """Binance-style ring: batch 1 covers ids 101..110, batch 2 covers 111..120 (sizes change at 100 and 101)."""
    c, w, notes, _ = mk(BinanceCapture, tmp_path)
    c.books["SOLUSDT"].reset()
    for p, q in ((100.0, 5.0), (99.0, 2.0)):
        c.books["SOLUSDT"].set("bid", p, q)
    for p, q in ((101.0, 4.0), (102.0, 1.0)):
        c.books["SOLUSDT"].set("ask", p, q)
    c._remember("SOLUSDT", 100)
    c.books["SOLUSDT"].set("bid", 100.0, 6.0)
    c._remember("SOLUSDT", 110, 101)                                             # batch 1: bid 100 5 -> 6
    c.books["SOLUSDT"].set("ask", 101.0, 9.0)
    c._remember("SOLUSDT", 120, 111)                                             # batch 2: ask 101 4 -> 9
    return c


def test_straddle_accepts_either_side_of_the_batch_but_flags_a_level_that_matches_neither(tmp_path):
    c = _ring_capture(tmp_path)
    b, a = [["100", "6"], ["99", "2"]], [["101", "4"], ["102", "1"]]
    r = c.compare("SOLUSDT", 115, b, a)                                         # inside batch 2: ask 101 may be 4 (before) or 9 (after)
    assert r["aligned"] and r["mode"] == "straddle" and r["match"] and r["levels_unexplained"] == 0
    r = c.compare("SOLUSDT", 115, b, [["101", "9"], ["102", "1"]])
    assert r["match"] and r["mode"] == "straddle"
    r = c.compare("SOLUSDT", 115, b, [["101", "7"], ["102", "1"]])               # 7 is neither the before nor the after size
    assert r["aligned"] and not r["match"] and r["levels_unexplained"] == 1 and not r["asks_match"] and r["bids_match"]
    r = c.compare("SOLUSDT", 115, [["100", "5"], ["99", "2"]], [["101", "9"], ["102", "1"]])      # an untouched-by-batch-2 level that is wrong in REST
    assert not r["match"] and not r["bids_match"]


def test_between_exact_ahead_and_older_alignment_modes(tmp_path):
    c = _ring_capture(tmp_path)
    b, a = [["100", "6"], ["99", "2"]], [["101", "4"], ["102", "1"]]
    assert c.compare("SOLUSDT", 110, b, a)["mode"] == "exact"
    r = c.compare("SOLUSDT", 111, b, a)                                         # 111 is the first id of batch 2: straddle (lo <= id < hi)
    assert r["mode"] == "straddle"
    r = c.compare("SOLUSDT", 121, b, a)
    assert r["aligned"] is False and r["reason"] == "rest_ahead_of_stream"
    assert c.compare("SOLUSDT", 50, b, a)["reason"] == "rest_older_than_ring"
    c.hist["SOLUSDT"].append((130, 140, [[100.0, 6.0]], [[101.0, 9.0]], frozenset()))        # a gap in ids between 120 and 130: state stays that of 120
    r = c.compare("SOLUSDT", 125, [["100", "6"]], [["101", "9"]])
    assert r["mode"] == "between" and r["match"]


def test_stream_url_builds_one_url_or_two_for_the_split_mainnet_paths():
    syms = ["BTCUSDT", "SOLUSDT"]
    assert capture.stream_url("bybit_linear", syms, "wss://x/v5/public/linear") == ["wss://x/v5/public/linear"]
    assert capture.stream_url("binance_futures", syms, "wss://t/stream?streams=") == [
        "wss://t/stream?streams=btcusdt@depth@100ms/solusdt@depth@100ms/btcusdt@aggTrade/solusdt@aggTrade"]
    d, t = capture.stream_url("binance_futures", syms, ("wss://m/public/stream?streams=", "wss://m/market/stream?streams="))
    assert d == "wss://m/public/stream?streams=btcusdt@depth@100ms/solusdt@depth@100ms" and t == "wss://m/market/stream?streams=btcusdt@aggTrade/solusdt@aggTrade"


def test_a_dropped_trade_connection_does_not_invalidate_the_books_but_a_dropped_depth_connection_does(tmp_path):
    c, w, _, _ = mk(BinanceCapture, tmp_path)
    c.urls = ["wss://m/public/stream?streams=a@depth@100ms", "wss://m/market/stream?streams=a@aggTrade"]
    c.valid["SOLUSDT"] = True
    c.on_disconnect(c.urls[1])
    assert c.valid["SOLUSDT"]
    c.on_disconnect(c.urls[0])
    assert not c.valid["SOLUSDT"]
    from ollog import mainnet
    for v in mainnet.PUBLIC_WS_MAINNET.values():
        for u in ((v,) if isinstance(v, str) else v):
            mainnet.assert_mainnet_url(u)


def test_levels_touched_by_the_straddling_batch_may_hold_an_intermediate_size_but_other_levels_may_not(tmp_path):
    c = _ring_capture(tmp_path)
    last = c.hist["SOLUSDT"][-1]
    c.hist["SOLUSDT"][-1] = last[:4] + (frozenset({101.0}),)                      # batch 2 touched ask 101
    b = [["100", "6"], ["99", "2"]]
    r = c.compare("SOLUSDT", 115, b, [["101", "7"], ["102", "1"]])               # 7: an intermediate value inside the batch
    assert r["match"] and r["levels_ambiguous"] == 1 and r["levels_unexplained"] == 0
    r = c.compare("SOLUSDT", 115, [["100", "7"], ["99", "2"]], [["101", "7"], ["102", "1"]])   # bid 100 was NOT touched by batch 2
    assert not r["match"] and r["levels_unexplained"] == 1
    r = c.compare("SOLUSDT", 110, b, [["101", "7"], ["102", "1"]])               # exact alignment gets no exemption
    assert r["mode"] == "exact" and not r["match"]


def test_level_writes_record_the_touched_prices_per_update(tmp_path):
    c, w, _, _ = mk(BybitCapture, tmp_path)

    async def go():
        await c.handle(bybit_ob("snapshot", 10, [("100", "5")], [("101", "4")]), RECV)
        await c.handle(bybit_ob("delta", 11, [("100", "6")], [("101", "0")]), RECV)
        await c.handle(bybit_ob("delta", 12, [("99", "1")]), RECV)
    asyncio.run(go())
    assert [e[4] for e in c.hist["SOLUSDT"]] == [frozenset(), frozenset({100.0, 101.0}), frozenset({99.0})]


def test_bybit_delta_covers_the_sequence_range_since_the_previous_message(tmp_path):
    c, w, _, _ = mk(BybitCapture, tmp_path)

    async def go():
        await c.handle(bybit_ob("snapshot", 10, [("100", "5")], [("101", "4")]), RECV)         # seq 100
        await c.handle(bybit_ob("delta", 11, [("100", "6")]), RECV)                            # seq 110, covers 101..110
    asyncio.run(go())
    assert [(e[0], e[1]) for e in c.hist["SOLUSDT"]] == [(100, 100), (101, 110)]
    r = c.compare("SOLUSDT", 105, [["100", "6"]], [["101", "4"]])                              # REST at seq 105: inside that delta
    assert r["mode"] == "straddle" and r["match"] and r["levels_ambiguous"] == 1
    r = c.compare("SOLUSDT", 105, [["100", "5"]], [["101", "4"]])                              # before-size is also acceptable
    assert r["match"]
    r = c.compare("SOLUSDT", 105, [["100", "9"]], [["101", "4"]])                              # neither: the touched level is exempt
    assert r["match"] and r["levels_ambiguous"] == 1
    r = c.compare("SOLUSDT", 105, [["100", "6"]], [["101", "8"]])                              # an untouched level that is wrong
    assert not r["match"] and r["levels_unexplained"] == 1
