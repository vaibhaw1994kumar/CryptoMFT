"""Public order-book and trade capture for a perpetual market, written in the backtester's own Tardis layout:

    <root>/<venue>/book_snapshots|book_events|trades/pair=<PAIR>/date=YYYY-MM-DD/<hour_ms>.jsonl.gz

so the capture replays through new_backtest.event_loader without a converter. Book events carry the level's NEW ABSOLUTE size
("delete" when 0, else "modify"). Public data only: no credentials, no orders. Venue-side sequencing is enforced (Bybit `u`,
Binance `U`/`u`/`pu` with a REST snapshot sync); a gap is recorded in capture_notes.jsonl and the book is rebuilt, never
patched over. Every `check_every_s`, the reconstructed top-N is compared with an independent REST depth snapshot aligned on
the venue's update id (recon_checks.jsonl).
"""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import ssl
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .models import stamp

LAYOUT = {"snapshot": "book_snapshots", "event": "book_events", "trade": "trades"}
TOP_N = 20


def _utc(ts_s: float) -> datetime:
    return datetime.fromtimestamp(ts_s, timezone.utc)


class TardisWriter:
    """Append-only gz files per (stream, pair, hour). `flush()` sync-flushes so a killed process loses at most one interval."""

    def __init__(self, root: Path, venue: str, raw: bool = True):
        self.root, self.venue, self.raw = Path(root), venue, raw
        self._files: dict[tuple, gzip.GzipFile] = {}

    def _open(self, key: tuple, path: Path) -> gzip.GzipFile:
        f = self._files.get(key)
        if f is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            f = self._files[key] = gzip.GzipFile(str(path), "ab")
        return f

    def write(self, stream: str, pair: str, ts_s: float, rec: dict) -> None:
        t = _utc(ts_s)
        hour_ms = int(t.replace(minute=0, second=0, microsecond=0).timestamp() * 1000)
        path = self.root / self.venue / LAYOUT[stream] / f"pair={pair}" / f"date={t:%Y-%m-%d}" / f"{hour_ms}.jsonl.gz"
        self._open((stream, pair, hour_ms), path).write((json.dumps(rec, separators=(",", ":")) + "\n").encode())

    def write_raw(self, recv_wall_ns: int, text: str) -> None:
        if not self.raw:
            return
        hour_ms = int(_utc(recv_wall_ns / 1e9).replace(minute=0, second=0, microsecond=0).timestamp() * 1000)
        path = self.root / "_raw" / self.venue / f"{hour_ms}.jsonl.gz"
        self._open(("raw", hour_ms), path).write((json.dumps([recv_wall_ns, text], separators=(",", ":")) + "\n").encode())

    def flush(self) -> None:
        for f in self._files.values():
            f.flush()

    def close(self) -> None:
        for f in self._files.values():
            f.close()
        self._files.clear()


class LocalBook:
    def __init__(self):
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}

    def reset(self) -> None:
        self.bids.clear()
        self.asks.clear()

    def set(self, side: str, price: float, size: float) -> None:
        d = self.bids if side == "bid" else self.asks
        if size <= 0:
            d.pop(price, None)
        else:
            d[price] = size

    def top(self, n: int = TOP_N) -> tuple[list, list]:
        return ([[p, self.bids[p]] for p in sorted(self.bids, reverse=True)[:n]],
                [[p, self.asks[p]] for p in sorted(self.asks)[:n]])


def _f(x) -> float:
    return float(x)


class _Capture:
    venue: str
    market: str

    def __init__(self, symbols: list[str], writer: TardisWriter, notes: Callable[[dict], None], checks: Callable[[dict], None],
                 guard: Callable[[str], str], rest_base: str, ws_url, session=None, ws_connect=None, keep_history: bool = True):
        self.symbols, self.writer, self._note, self._check, self.guard = symbols, writer, notes, checks, guard
        self.urls = [ws_url] if isinstance(ws_url, str) else list(ws_url)
        self.rest_base, self.session, self._ws_connect = rest_base, session, ws_connect
        self.books = {s: LocalBook() for s in symbols}
        self.valid = {s: False for s in symbols}
        self.hist = {s: deque(maxlen=800) for s in symbols} if keep_history else None      # (update id, bids, asks) after each applied update
        self._touched = {s: set() for s in symbols}              # prices changed since the last remembered update
        self.counts = Counter()
        self.stop = asyncio.Event()

    # ---- shared record writers --------------------------------------------------------------------------------
    def note(self, kind: str, **kw) -> None:
        self.counts[f"note_{kind}"] += 1
        self._note({"t_wall": time.time(), "venue": self.venue, "type": kind, **kw})

    def _snapshot_record(self, sym: str, ts_s: float, recv: tuple) -> None:
        bids, asks = self.books[sym].top(50)
        rec = {"timestamp": ts_s, "local_ts": recv[0] / 1e9, "local_mono_ns": recv[1], "exchange": self.venue, "pair": sym,
               "best_bid": bids[0][0] if bids else None, "best_ask": asks[0][0] if asks else None, "bids": bids, "asks": asks}
        self.writer.write("snapshot", sym, ts_s, rec)
        self.counts["snapshots"] += 1

    def _level(self, sym: str, side: str, price: float, size: float, ts_s: float, recv: tuple) -> None:
        self.writer.write("event", sym, ts_s, {"exchange": self.venue, "pair": sym, "exchange_ts": ts_s, "local_ts": recv[0] / 1e9,
                                               "local_mono_ns": recv[1], "side": side, "price": price, "volume": size,
                                               "event_type": "delete" if size <= 0 else "modify"})
        self.books[sym].set(side, price, size)
        self._touched[sym].add(price)
        self.counts["levels"] += 1

    def _trade(self, sym: str, ts_s: float, price: float, size: float, side: str, recv: tuple) -> None:
        self.writer.write("trade", sym, ts_s, {"exchange": self.venue, "pair": sym, "exchange_ts": ts_s, "local_ts": recv[0] / 1e9,
                                               "local_mono_ns": recv[1], "price": price, "volume": size, "side": side})
        self.counts["trades"] += 1

    def _remember(self, sym: str, update_id: int, first_id: int | None = None) -> None:
        """Keep the top of book after each applied update, keyed by the update-id range [first_id, update_id] it covers."""
        if self.hist is not None:
            b, a = self.books[sym].top()
            self.hist[sym].append((update_id if first_id is None else first_id, update_id, b, a, frozenset(self._touched[sym])))
        self._touched[sym].clear()

    # ---- REST reconciliation ----------------------------------------------------------------------------------------
    def compare(self, sym: str, rest_id: int, rest_bids: list, rest_asks: list, k: int = TOP_N) -> dict:
        """Compare the reconstructed top-k with a REST snapshot taken at update id `rest_id`. Pure; used by tests.

        exact:    an update ends exactly at rest_id  -> compare with the state after it.
        straddle: rest_id falls inside one batch (the 100 ms diff covers many ids) -> a level matches if the REST size equals the
                  size before OR after that batch; only a level that equals neither is an unexplained difference.
        between:  rest_id falls between two updates -> the state after the earlier one (every change inside the compared depth is a
                  message, so nothing could have changed in between).
        Unaligned (not guessed): the id is ahead of everything received, or older than the history kept."""
        out = {"t_wall": time.time(), "venue": self.venue, "symbol": sym, "rest_update_id": rest_id}
        h = list(self.hist[sym]) if self.hist is not None else []
        i = next((j for j, e in enumerate(h) if e[1] >= rest_id), None)
        reason = None
        if i is None:
            reason = "rest_ahead_of_stream"
        elif h[i][0] <= rest_id:
            if h[i][1] == rest_id:
                before = after = h[i]
                out["mode"] = "exact"
            elif i == 0:
                reason = "rest_older_than_ring"
            else:
                before, after = h[i - 1], h[i]
                out["mode"] = "straddle"
        elif i == 0:
            reason = "rest_older_than_ring"
        else:
            before = after = h[i - 1]
            out["mode"] = "between"
        if reason:
            out.update(aligned=False, reason=reason, ring_first_u=h[0][0] if h else None, ring_last_u=h[-1][1] if h else None)
            return out
        n = min(k, len(rest_bids), len(rest_asks))
        rb = {_f(p): _f(q) for p, q in rest_bids[:n]}
        ra = {_f(p): _f(q) for p, q in rest_asks[:n]}

        exempt = after[4] if out["mode"] == "straddle" else frozenset()   # a batch can move one price several times: its levels may hold any intermediate size

        def unexplained(rest: dict, b_side: list, a_side: list, bid: bool) -> tuple[int, int]:
            if not rest:
                return 0, 0
            bound = min(rest) if bid else max(rest)
            inside = (lambda p: p >= bound) if bid else (lambda p: p <= bound)
            bd, ad = dict((p, q) for p, q in b_side), dict((p, q) for p, q in a_side)
            prices = (set(rest) | {p for p in bd if inside(p)} | {p for p in ad if inside(p)}) - exempt
            bad = sum(1 for p in prices if rest.get(p, 0.0) != bd.get(p, 0.0) and rest.get(p, 0.0) != ad.get(p, 0.0))
            return bad, len(prices)
        bb, nb_ = unexplained(rb, before[2], after[2], True)
        ba, na_ = unexplained(ra, before[3], after[3], False)
        top1 = lambda st: bool(st[2] and st[3] and n and st[2][0] == [max(rb), rb[max(rb)]] and st[3][0] == [min(ra), ra[min(ra)]])
        out.update(aligned=True, k=n, levels_compared=nb_ + na_, levels_unexplained=bb + ba, levels_ambiguous=len(exempt), bids_match=bb == 0, asks_match=ba == 0,
                   top1_match=top1(before) or top1(after), match=bb + ba == 0)
        return out

    async def _await_stream(self, sym: str, rest_id: int, wait_s: float = 3.0) -> None:
        """The REST reply can be fresher than the frames received so far: give the stream a moment to reach its update id."""
        t0 = time.monotonic()
        while self.hist is not None and (not self.hist[sym] or self.hist[sym][-1][1] < rest_id) and time.monotonic() - t0 < wait_s:
            await asyncio.sleep(0.05)

    async def _get_json(self, path: str, params: dict):
        from urllib.parse import urlencode
        url = self.guard(f"{self.rest_base}{path}?{urlencode(params)}")
        async with self.session.get(url) as resp:
            return json.loads(await resp.text())

    async def _checker(self, every_s: float) -> None:
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), every_s)
                return
            except asyncio.TimeoutError:
                pass
            for sym in self.symbols:
                if self.valid[sym]:
                    try:
                        res = await self.rest_check(sym)
                        if res:
                            self.counts["checks"] += 1
                            self.counts["checks_match"] += bool(res.get("match"))
                            self._check(res)
                    except Exception as exc:
                        self.note("check_failed", symbol=sym, error=type(exc).__name__)

    # ---- websocket plumbing ---------------------------------------------------------------------------------------------
    async def _connect(self, url: str):
        self.guard(url)
        if self._ws_connect is not None:
            return await self._ws_connect(url)
        import certifi
        import websockets
        return await websockets.connect(url, ssl=ssl.create_default_context(cafile=certifi.where()), ping_interval=20, max_size=2 ** 23)

    async def run(self, check_every_s: float = 30.0) -> None:
        tasks = [asyncio.create_task(self._conn_loop(u)) for u in self.urls]
        if check_every_s and self.session is not None:
            tasks.append(asyncio.create_task(self._checker(check_every_s)))
        try:
            await self.stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.writer.flush()

    async def _conn_loop(self, url: str) -> None:
        backoff = 0.5
        while not self.stop.is_set():
            try:
                ws = await self._connect(url)
                backoff = 0.5
                await self.after_connect(ws, url)
                while not self.stop.is_set():
                    try:
                        text = await asyncio.wait_for(ws.recv(), 1.0)
                    except asyncio.TimeoutError:
                        continue
                    recv = stamp()
                    self.writer.write_raw(recv[0], text if isinstance(text, str) else text.decode())
                    self.counts["frames"] += 1
                    try:
                        msg = json.loads(text)
                    except ValueError:
                        self.counts["bad_json"] += 1
                        continue
                    await self.handle(msg, recv, ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.note("reconnect", error=type(exc).__name__, url=url.split("?")[0])
                self.on_disconnect(url)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)

    def on_disconnect(self, url: str) -> None:
        for s in self.symbols:
            self.valid[s] = False

    async def after_connect(self, ws, url: str) -> None: ...
    async def handle(self, msg: dict, recv: tuple, ws) -> None: ...
    async def rest_check(self, sym: str) -> dict | None: ...


class BybitCapture(_Capture):
    venue = "bybit"
    market = "bybit_linear"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.last_u: dict[str, int] = {}
        self.last_seq: dict[str, int] = {}

    async def after_connect(self, ws, url: str) -> None:
        for s in self.symbols:
            self.valid[s] = False
        await ws.send(json.dumps({"op": "subscribe", "args": [f"orderbook.50.{s}" for s in self.symbols] + [f"publicTrade.{s}" for s in self.symbols]}))

    async def handle(self, msg: dict, recv: tuple, ws=None) -> None:
        topic = msg.get("topic", "")
        if topic.startswith("orderbook."):
            d = msg["data"]
            sym = d["s"]
            if sym not in self.books:
                return
            ts = (msg.get("cts") or msg.get("ts") or recv[0] / 1e6) / 1000
            u = int(d["u"])
            if msg.get("type") == "snapshot" or u == 1:
                self.books[sym].reset()
                for p, s in d.get("b", []):
                    self.books[sym].set("bid", _f(p), _f(s))
                for p, s in d.get("a", []):
                    self.books[sym].set("ask", _f(p), _f(s))
                self.valid[sym] = True
                self.last_u[sym] = u
                self._snapshot_record(sym, ts, recv)
                self.last_seq[sym] = int(d.get("seq") or u)
                self._remember(sym, self.last_seq[sym])            # keyed on the cross sequence: the only id REST and WS share
                return
            if not self.valid[sym]:
                self.counts["dropped_before_snapshot"] += 1
                return
            if u != self.last_u[sym] + 1:
                self.note("gap", symbol=sym, expected=self.last_u[sym] + 1, got=u)
                self.valid[sym] = False
                if ws is not None:                                         # resubscribe to get a fresh snapshot
                    await ws.send(json.dumps({"op": "unsubscribe", "args": [f"orderbook.50.{sym}"]}))
                    await ws.send(json.dumps({"op": "subscribe", "args": [f"orderbook.50.{sym}"]}))
                return
            self.last_u[sym] = u
            for p, s in d.get("b", []):
                self._level(sym, "bid", _f(p), _f(s), ts, recv)
            for p, s in d.get("a", []):
                self._level(sym, "ask", _f(p), _f(s), ts, recv)
            seq = int(d.get("seq") or u)
            self._remember(sym, seq, self.last_seq[sym] + 1)       # a delta covers every change since the previous message
            self.last_seq[sym] = seq
        elif topic.startswith("publicTrade."):
            for t in msg.get("data", []):
                if t["s"] in self.books:
                    self._trade(t["s"], t["T"] / 1000, _f(t["p"]), _f(t["v"]), "b" if t["S"] == "Buy" else "s", recv)

    async def rest_check(self, sym: str) -> dict | None:
        obj = await self._get_json("/v5/market/orderbook", {"category": "linear", "symbol": sym, "limit": 50})
        r = obj.get("result") or {}
        if obj.get("retCode") != 0 or "seq" not in r:
            return None
        await self._await_stream(sym, int(r["seq"]))
        return self.compare(sym, int(r["seq"]), r.get("b", []), r.get("a", []))


class BinanceCapture(_Capture):
    venue = "binance"
    market = "binance_futures"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.buf: dict[str, list] = {s: [] for s in self.symbols}
        self.last_u: dict[str, int] = {}
        self.syncing: set[str] = set()
        self._sync_tasks: set = set()

    def _is_depth_url(self, url: str) -> bool:
        return len(self.urls) == 1 or "/public/" in url or "depth" in url

    async def after_connect(self, ws, url: str) -> None:
        if self._is_depth_url(url):                                # a trade-only connection dropping must not invalidate the books
            for s in self.symbols:
                self.valid[s] = False
                self.buf[s].clear()

    def on_disconnect(self, url: str) -> None:
        if self._is_depth_url(url):
            super().on_disconnect(url)

    async def handle(self, msg: dict, recv: tuple, ws=None) -> None:
        d = msg.get("data", msg)
        e = d.get("e")
        if e == "depthUpdate":
            sym = d["s"]
            if sym not in self.books:
                return
            if not self.valid[sym]:
                self.buf[sym].append((d, recv))
                del self.buf[sym][:-5000]
                if sym not in self.syncing and self.session is not None:
                    self.syncing.add(sym)
                    t = asyncio.create_task(self._sync(sym))
                    self._sync_tasks.add(t)
                    t.add_done_callback(self._sync_tasks.discard)
                return
            self._apply(sym, d, recv)
        elif e == "aggTrade" and d["s"] in self.books:
            self._trade(d["s"], d["T"] / 1000, _f(d["p"]), _f(d["q"]), "s" if d["m"] else "b", recv)

    def _apply(self, sym: str, d: dict, recv: tuple) -> bool:
        if int(d["pu"]) != self.last_u[sym]:
            self.note("gap", symbol=sym, expected_pu=self.last_u[sym], got_pu=int(d["pu"]))
            self.valid[sym] = False
            self.books[sym].reset()
            return False
        ts = d["T"] / 1000
        for p, q in d.get("b", []):
            self._level(sym, "bid", _f(p), _f(q), ts, recv)
        for p, q in d.get("a", []):
            self._level(sym, "ask", _f(p), _f(q), ts, recv)
        self.last_u[sym] = int(d["u"])
        self._remember(sym, int(d["u"]), int(d["U"]))
        return True

    async def _sync(self, sym: str) -> None:
        """REST snapshot + buffered diffs, per Binance's documented procedure."""
        try:
            for attempt in range(5):
                await asyncio.sleep(0.3)
                obj = await self._get_json("/fapi/v1/depth", {"symbol": sym, "limit": 1000})
                recv = stamp()
                last = int(obj["lastUpdateId"])
                ev = [x for x in self.buf[sym] if int(x[0]["u"]) >= last]
                if not ev:
                    continue
                if int(ev[0][0]["U"]) > last:                              # snapshot older than the first buffered event: take a newer one
                    self.note("snapshot_too_old", symbol=sym, last=last, first_U=int(ev[0][0]["U"]))
                    continue
                self.books[sym].reset()
                for p, q in obj["bids"]:
                    self.books[sym].set("bid", _f(p), _f(q))
                for p, q in obj["asks"]:
                    self.books[sym].set("ask", _f(p), _f(q))
                ts = (obj.get("T") or obj.get("E") or recv[0] / 1e6) / 1000
                self._snapshot_record(sym, ts, recv)
                self.last_u[sym] = last
                first = True
                ok = True
                for d, r in ev:
                    if first:                                              # first event: U <= lastUpdateId <= u
                        self.last_u[sym] = int(d["pu"])
                        first = False
                    if not self._apply(sym, d, r):
                        ok = False
                        break
                if ok:
                    self.buf[sym].clear()
                    self.valid[sym] = True
                    self.counts["syncs"] += 1
                    return
            self.note("sync_failed", symbol=sym)
        finally:
            self.syncing.discard(sym)

    async def rest_check(self, sym: str) -> dict | None:
        obj = await self._get_json("/fapi/v1/depth", {"symbol": sym, "limit": 50})
        if "lastUpdateId" not in obj:
            return None
        await self._await_stream(sym, int(obj["lastUpdateId"]))
        return self.compare(sym, int(obj["lastUpdateId"]), obj.get("bids", []), obj.get("asks", []))


def stream_url(market: str, symbols: list[str], base) -> list[str]:
    """Public stream URL(s). Bybit subscribes after connecting. Binance puts the streams in the URL; a (depth_base, trade_base) pair
    means the venue serves them on separate paths (mainnet USD-M: /public/ for depth, /market/ for aggTrade), so two connections."""
    if market == "bybit_linear":
        return [base]
    depth = "/".join(f"{x.lower()}@depth@100ms" for x in symbols)
    trade = "/".join(f"{x.lower()}@aggTrade" for x in symbols)
    if isinstance(base, tuple):
        return [base[0] + depth, base[1] + trade]
    return [base + depth + "/" + trade]
