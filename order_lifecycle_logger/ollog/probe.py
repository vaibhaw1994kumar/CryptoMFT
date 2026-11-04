"""At-touch fill probes: minimum-size post-only orders that JOIN the best bid (buy) or best ask (sell) so they can really queue and
fill, alternating sides to keep the net position near flat. Each order rests until it fills or `hold` elapses, then is cancelled.
Every execution is DATA (quantity, exchange time, price), not an error; a cancel refused because the order already filled is
recorded, not hidden. The book the orders are placed against is the live capture's own reconstruction, so the capture and the
orders share clocks and sequencing. One order live per market at a time.

Safety (all enforced here): shared order cap, per-order notional cap, net-position notional cap, stop after K consecutive errors,
cleanup cancels the live order and sweeps this run's client-id prefix, then (optionally) flattens any leftover position with a
reduce-only market order on the traded symbols only.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal

from .capture import _Capture
from .derive import _ms
from .models import UserEvent, round_down, round_up, stamp
from .pricing import NoOrder, min_quantity
from .runner import Budget, MarketRunner, RunConfig


@dataclass
class ProbeConfig(RunConfig):
    hold_min_s: float = 15.0
    hold_max_s: float = 45.0
    max_net_notional: Decimal = Decimal(150)       # |net position| in quote currency before only the reducing side is quoted
    flatten: bool = True
    max_skips: int = 40                            # consecutive "could not quote" before the market gives up
    max_stream_reconnects: int = 5                 # private-stream drops survived between orders before the market gives up
    preflight: bool = True                         # one far-from-touch order first: the private stream must deliver its ack and cancel
    preflight_dist_bps: Decimal = Decimal(50)


@dataclass
class ProbePending:
    client_id: str
    evs: list = field(default_factory=list)
    ack: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)


def execution_from_event(ev: UserEvent, prev_cum: Decimal) -> tuple[Decimal, Decimal, int | None, Decimal] | None:
    """(executed qty in this update, price, exchange ms, cumulative qty) for a fill update, else None."""
    r = ev.raw or {}
    if ev.market.startswith("bybit"):
        cum = Decimal(str(r.get("cumExecQty") or 0))
        qty = cum - prev_cum
        if qty <= 0:
            return None
        price = Decimal(str(r.get("avgPrice") or r.get("price") or 0))      # the order's average fill price so far
        return qty, price, int(r["updatedTime"]) if r.get("updatedTime") else ev.ex_event_ms, cum
    o = r.get("o") if "o" in r else r                        # futures wraps in o; spot is flat
    if o.get("x") != "TRADE":
        return None
    cum = Decimal(str(o.get("z"))) if o.get("z") not in (None, "") else prev_cum + Decimal(str(o.get("l") or 0))
    qty = cum - prev_cum                                     # cumulative difference: a duplicated update adds nothing
    if qty <= 0:
        return None
    price = Decimal(str(o.get("L") or 0))
    return qty, price, int(o["T"]) if o.get("T") else ev.ex_txn_ms, cum


def created_ms(ev: UserEvent) -> int | None:
    """Exchange time the order was created: Bybit's createdTime, Binance's transaction time of the NEW update."""
    r = ev.raw or {}
    if ev.market.startswith("bybit") and r.get("createdTime"):
        return int(r["createdTime"])
    return ev.ex_txn_ms or ev.ex_event_ms


class ProbeRunner(MarketRunner):
    def __init__(self, client, recorder, symbols, instruments, cfg: ProbeConfig, budget: Budget, stop: asyncio.Event, capture: _Capture):
        super().__init__(client, recorder, symbols, instruments, cfg, budget, stop)
        self.capture = capture
        self.net: dict[str, Decimal] = {s: Decimal(0) for s in symbols}
        self.side_n: dict[str, int] = {s: 0 for s in symbols}
        self.stats.update(fills=0, filled_orders=0, post_only_rejects=0, cancel_after_fill=0, skipped=0, flattened=0)
        self.flatten_results: list[dict] = []
        self.reconnects = 0

    # ---- private stream -----------------------------------------------------------------------------------------
    def on_event(self, ev: UserEvent) -> None:
        self.stats["ws_events"] += 1
        p = self.pending.get(ev.client_id)
        self.rec.event("ws_event", market=self.market, kind=ev.kind, symbol=ev.symbol, order_id=ev.order_id, client_id=ev.client_id,
                       status=ev.status, reason=ev.reason, ex_event_ms=ev.ex_event_ms, ex_txn_ms=ev.ex_txn_ms,
                       recv_wall_ns=ev.recv_wall_ns, recv_mono_ns=ev.recv_mono_ns, matched=p is not None)
        if p is None:
            self.stats["ws_unmatched"] += 1
            return
        p.evs.append(ev)
        if ev.kind in ("new", "rejected", "expired", "trade", "cancelled"):
            p.ack.set()
        if ev.kind in ("cancelled", "rejected", "expired") or (ev.kind == "trade" and ev.status in ("Filled", "FILLED")):
            p.done.set()

    # ---- one order ------------------------------------------------------------------------------------------------
    def _touch(self, symbol: str, side: str):
        if not self.capture.valid.get(symbol):
            raise NoOrder("capture book not valid")
        bids, asks = self.capture.books[symbol].top(5)
        if not bids or not asks:
            raise NoOrder("empty side")
        if bids[0][0] >= asks[0][0]:
            raise NoOrder("crossed capture book")
        return bids, asks

    async def _probe(self, symbol: str, side: str) -> dict:
        inst = self.inst[symbol]
        bids, asks = self._touch(symbol, side)
        raw_px = Decimal(str(bids[0][0] if side == "Buy" else asks[0][0]))
        price = round_down(raw_px, inst.tick) if side == "Buy" else round_up(raw_px, inst.tick)
        qty = min_quantity(inst, price, self.cfg.max_notional)
        own_side = bids if side == "Buy" else asks
        size_at_price = next((s for p, s in own_side if abs(p - float(price)) < 1e-12), None)

        self.n_sent += 1
        cid = f"{self.prefix}{self.n_sent:06d}"
        pend = self.pending[cid] = ProbePending(cid)
        t: dict = {}
        ex: dict = {}
        rec = {"market": self.market, "symbol": symbol, "kind": "fill_probe", "client_id": cid, "side": side, "price": str(price), "qty": str(qty),
               "best_bid_at_send": bids[0][0], "best_ask_at_send": asks[0][0], "size_at_price_at_send": size_at_price,
               "top5_at_send": {"bids": bids, "asks": asks}, "t": t, "ex": ex, "clock": dict(self.clock), "errors": [], "execs": [], "outcome": ""}
        self.traded.add(symbol)
        self.live = (symbol, None, cid)
        res = await self.client.place(symbol, price, qty, cid, side=side)
        t.update(send_wall_ns=res.t_send_wall_ns, send_mono_ns=res.t_send_mono_ns, rest_wall_ns=res.t_ret_wall_ns, rest_mono_ns=res.t_ret_mono_ns)
        ex["rest_ms"] = res.ex_time_ms
        rec["order_id"] = res.order_id
        if not res.ok:
            self.live = None
            rec["outcome"] = "rejected_on_rest"
            rec["reject_code"], rec["reject_msg"] = res.code, res.msg
            self.pending.pop(cid, None)
            return rec
        self.live = (symbol, res.order_id, cid)
        await self._wait(pend.ack, self.cfg.ack_timeout_s)
        first = pend.evs[0] if pend.evs else None
        if first is None:
            rec["errors"].append("no_stream_ack")
        elif first.kind in ("rejected", "expired") or (first.kind == "cancelled" and not any(e.kind == "new" for e in pend.evs)):
            rec["outcome"] = "rejected_on_stream"
            rec["reject_reason"] = first.reason or first.status
            self.live = None
        if self.live is not None:
            hold = self.rng.uniform(self.cfg.hold_min_s, self.cfg.hold_max_s)
            rec["hold_s"] = round(hold, 3)
            await self._wait(pend.done, hold)
            if not pend.done.is_set():
                cres = await self.client.cancel(symbol, res.order_id, cid)
                t.update(csend_wall_ns=cres.t_send_wall_ns, csend_mono_ns=cres.t_send_mono_ns, crest_wall_ns=cres.t_ret_wall_ns, crest_mono_ns=cres.t_ret_mono_ns)
                ex["crest_ms"] = cres.ex_time_ms
                rec["cancel"] = {"ok": cres.ok, "code": cres.code, "msg": cres.msg}
                if not cres.ok:
                    self.stats["cancel_after_fill"] += 1                     # raced a fill, or a real failure: the order's own events say which
                await self._wait(pend.done, self.cfg.cancel_timeout_s)
            self.live = None
        await asyncio.sleep(0.2)                                             # late fill / cancel updates for this order
        prev, first_fill, last_fill = Decimal(0), None, None
        for ev in pend.evs:
            if ev.kind == "new" and "wsnew_wall_ns" not in t:
                t["wsnew_wall_ns"], t["wsnew_mono_ns"] = ev.recv_wall_ns, ev.recv_mono_ns
                ex["created_ms"], ex["wsnew_event_ms"] = created_ms(ev), ev.ex_event_ms
            elif ev.kind == "cancelled" and "wscan_wall_ns" not in t:
                t["wscan_wall_ns"], t["wscan_mono_ns"] = ev.recv_wall_ns, ev.recv_mono_ns
                ex["wscan_event_ms"] = ev.ex_event_ms
            elif ev.kind in ("rejected", "expired") and "wsrej_wall_ns" not in t:
                t["wsrej_wall_ns"], t["wsrej_mono_ns"] = ev.recv_wall_ns, ev.recv_mono_ns
            if ev.kind == "trade":
                e = execution_from_event(ev, prev)
                if e is not None:
                    qty_e, px_e, ex_ms, prev = e
                    rec["execs"].append({"qty": str(qty_e), "price": str(px_e), "ex_ms": ex_ms, "recv_wall_ns": ev.recv_wall_ns, "recv_mono_ns": ev.recv_mono_ns})
                    first_fill = first_fill if first_fill is not None else ex_ms
                    last_fill = ex_ms
        if "created_ms" not in ex:                                           # no usable NEW event: fall back to the REST time
            ex["created_ms"] = ex.get("rest_ms")
        filled = prev
        rec["filled_qty"] = str(filled)
        rec["first_fill_ex_ms"], rec["last_fill_ex_ms"] = first_fill, last_fill
        if not rec["outcome"]:
            rec["outcome"] = ("filled" if filled >= qty else "partial_then_cancelled" if filled > 0 else "cancelled_unfilled")
        if filled > 0:
            self.net[symbol] += filled if side == "Buy" else -filled
            self.stats["filled_orders"] += 1
            self.stats["fills"] += len(rec["execs"])
        if rec["outcome"] == "rejected_on_stream":
            self.stats["post_only_rejects"] += 1
        self.pending.pop(cid, None)
        return rec

    def _pick_side(self, symbol: str) -> str:
        """Alternate Buy/Sell per symbol; once |net| reaches the cap only the reducing side is quoted."""
        n = self.side_n[symbol]
        self.side_n[symbol] += 1
        bids, _ = self.capture.books[symbol].top(1) if self.capture.valid.get(symbol) else ([], [])
        net = self.net[symbol]
        if bids and abs(net) * Decimal(str(bids[0][0])) >= self.cfg.max_net_notional:
            return "Sell" if net > 0 else "Buy"
        return "Buy" if n % 2 == 0 else "Sell"

    async def _preflight_once(self, symbol: str) -> str | None:
        """None if the stream delivered the ack and the cancel confirmation for a resting far order, else the reason it did not."""
        bids, asks = self._touch(symbol, "Buy")
        inst = self.inst[symbol]
        price = round_down(Decimal(str(bids[0][0])) * (1 - self.cfg.preflight_dist_bps / Decimal(10000)), inst.tick)
        if price <= 0:
            return "no price"
        qty = min_quantity(inst, price, self.cfg.max_notional)
        self.n_sent += 1
        cid = f"{self.prefix}z{self.n_sent:05d}"
        pend = self.pending[cid] = ProbePending(cid)
        self.traded.add(symbol)
        self.live = (symbol, None, cid)
        res = await self.client.place(symbol, price, qty, cid, side="Buy")
        if not res.ok:
            self.live = None
            return f"preflight order refused: {res.code} {res.msg}"
        self.live = (symbol, res.order_id, cid)
        got_ack = await self._wait(pend.ack, self.cfg.ack_timeout_s)
        await self.client.cancel(symbol, res.order_id, cid)
        got_cancel = await self._wait(pend.done, self.cfg.cancel_timeout_s)
        self.live = None
        self.pending.pop(cid, None)
        self.rec.event("preflight", market=self.market, symbol=symbol, variant=getattr(self.client, "stream_variant", 0), ack=got_ack, cancel_confirmed=got_cancel)
        if not got_ack:
            return "no stream ack"
        return None if got_cancel else "no stream cancel confirmation"

    async def _preflight(self) -> bool:
        """Try each private-stream variant the venue offers until one is proven to deliver this account's order events."""
        symbol = self.symbols[0]
        for variant in range(getattr(self.client, "stream_variants", 1)):
            if variant:
                self.client.stream_variant = variant
                await self.client.restart_stream(self.on_event)
            why = await self._preflight_once(symbol)
            if why is None:
                return True
            self.rec.event("preflight_failed", market=self.market, variant=variant, reason=why)
        return False

    # ---- run -------------------------------------------------------------------------------------------------------------
    async def run(self) -> None:
        sync_task = None
        try:
            await self.sync_clock()
            sync_task = asyncio.create_task(self._sync_loop())
            await self.client.start_stream(self.on_event)
            self.rec.event("stream_ready", market=self.market)
            await self.client.open_orders(self.symbols[0])
            if self.cfg.preflight and not await self._preflight():
                self.stop_reason = "preflight failed: the private stream did not deliver order events (no probe order was sent)"
                return
            k = skips = 0
            while not self.stop.is_set():
                if not self.client.stream_alive and not await self._reconnect_stream():
                    self.stop_reason = f"private stream down ({self.client.stream_error})"
                    break
                if not self.budget.take():
                    self.stop_reason = "order cap reached"
                    break
                symbol = self.symbols[k % len(self.symbols)]
                k += 1
                side = self._pick_side(symbol)
                try:
                    rec = await self._probe(symbol, side)
                except NoOrder as exc:
                    self.budget.used -= 1
                    self.stats["skipped"] += 1
                    skips += 1
                    self.rec.event("skipped", market=self.market, symbol=symbol, reason=str(exc))
                    if skips >= self.cfg.max_skips:
                        self.stop_reason = "no symbol could be quoted"
                        break
                    await self._sleep(1.0)
                    continue
                except Exception as exc:
                    self.rec.event("error", market=self.market, symbol=symbol, error=f"{type(exc).__name__}: {exc}")
                    self.stats["errors"] += 1
                    self.consecutive_errors += 1
                    if self.consecutive_errors >= self.cfg.max_consecutive_errors:
                        self.stop_reason = f"{self.consecutive_errors} consecutive errors"
                        break
                    await self._sleep(1.0)
                    continue
                skips = 0
                rec["clean"] = not rec["errors"] and not rec["outcome"].startswith("rejected_on_rest")
                self.rec.order(rec)
                self.stats["orders"] += 1
                if rec["clean"]:
                    self.consecutive_errors = 0
                else:
                    self.stats["errors"] += 1
                    self.consecutive_errors += 1
                    if self.consecutive_errors >= self.cfg.max_consecutive_errors:
                        self.stop_reason = f"{self.consecutive_errors} consecutive errors"
                        break
                await self._sleep(max(self.cfg.spacing_s, 0.2))
            if self.stop.is_set():
                self.stop_reason = "stop requested"
        except BaseException as exc:
            self.stop_reason = f"aborted: {type(exc).__name__}: {exc}"
            raise
        finally:
            if sync_task is not None:
                sync_task.cancel()
            await self.cleanup()
            self.rec.event("run_end", market=self.market, reason=self.stop_reason, **self.stats)

    async def _reconnect_stream(self) -> bool:
        """Between orders only (nothing rests, so no event can be missed for a live order). Bounded; every attempt is recorded."""
        if self.reconnects >= self.cfg.max_stream_reconnects:
            return False
        self.reconnects += 1
        err = self.client.stream_error
        try:
            await self.client.restart_stream(self.on_event)
            self.rec.event("stream_reconnected", market=self.market, attempt=self.reconnects, after=err)
            self.stats["stream_reconnects"] = self.reconnects
            return self.client.stream_alive
        except Exception as exc:
            self.rec.event("stream_reconnect_failed", market=self.market, attempt=self.reconnects, error=type(exc).__name__)
            return False

    async def cleanup(self) -> None:
        await self._cancel_all()
        if self.cfg.flatten:
            await self._flatten()
        await self._close_client()

    async def _flatten(self) -> None:
        """Reduce-only market orders for whatever position the probes left, on the traded symbols only."""
        for symbol in sorted(self.traded):
            try:
                size = await self.client.position_size(symbol)
                out = {"symbol": symbol, "position": str(size), "tracked_net": str(self.net[symbol])}
                if size != 0:
                    side = "Sell" if size > 0 else "Buy"
                    res = await self.client.market_close(symbol, side, abs(size), f"{self.prefix}f{self.stats['flattened']:05d}")
                    out.update(side=side, ok=res.ok, code=res.code, msg=res.msg)
                    self.stats["flattened"] += 1
                self.flatten_results.append(out)
                self.rec.event("flatten", market=self.market, **out)
            except BaseException as exc:
                self.rec.event("flatten_error", market=self.market, symbol=symbol, error=type(exc).__name__)
