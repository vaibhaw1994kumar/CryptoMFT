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
import math
import os
import time
from collections import deque
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
class PerpProbeConfig(ProbeConfig):
    """Perp probe v2 (owner order 15 Nov 2026, plan out/integration/cost/PERP_QUEUE_VALIDATION_PLAN.md). Used by PerpProbeRunner; the fields of ProbeConfig keep their meaning (hold_*, flatten and spacing are not used by v2)."""
    max_open: int = 2                              # orders open at the same time per coin and venue (one worker per slot)
    mean_gap_s: float = 90.0                       # mean of the exponential pause between one worker's orders
    touch_share: float = 0.5                       # share of orders AT the touch (the back of the queue); the rest rest one tick back
    cancel_share: float = 0.5                      # share cancelled after one of cancel_after_s; the rest wait for a fill up to max_hold_s
    cancel_after_s: tuple = (1.0, 3.0, 5.0, 10.0)
    amend_share: float = 0.2                       # share that gets ONE price amend (one tick: toward the touch if behind it, one tick back if at it)
    amend_after_s: tuple = (1.0, 3.0)
    max_hold_s: float = 120.0
    sharp_bps: float = 3.0                         # ASSUMED: a mid move of at least this in sharp_window_s counts as a sharp move
    sharp_window_s: float = 1.0
    sharp_share: float = 0.25                      # share of orders placed right after a sharp move (waited for up to sharp_wait_s, else placed at a random moment and tagged)
    sharp_wait_s: float = 60.0
    inventory_soft_share: float = 0.5              # above this share of max_net_notional only the reducing side is quoted (a PostOnly unwind)
    loss_cap_day: Decimal = Decimal(20)            # quote currency, per UTC day: breached = cancel all, flatten, pause until the next UTC day
    loss_cap_total: Decimal = Decimal(200)         # breached = kill switch
    unsafe_feed_s: float = 5.0                     # the capture book invalid for this long = feed UNSAFE = kill switch
    stop_file: str | None = None                   # the file's existence = a manual stop = kill switch
    maker_fee_bps: float = 2.0                     # ASSUMED, for the loss account (the exchange's own fee is in the raw events)
    taker_fee_bps: float = 5.5                     # ASSUMED


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
        prev, first_fill, last_fill = self._collect(pend, rec, t, ex)
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

    def _collect(self, pend: ProbePending, rec: dict, t: dict, ex: dict):
        """Read the order's stream events into the record: the stream stamps, every execution (cumulative differences), the exchange creation time. Returns (cumulative filled, first fill ex ms, last fill ex ms)."""
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
        return prev, first_fill, last_fill

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


class PerpProbeRunner(ProbeRunner):
    """Perp probe v2: per coin and venue up to `max_open` workers, each placing minimum-size PostOnly orders at the touch or one tick back at random moments (some right after a sharp move), with cancels after a few seconds, some price amends,
    an inventory cap per coin (PostOnly unwind above the soft share, a reduce-only taker unwind only AT the cap), a loss account with a daily and a total cap, and a kill switch (feed UNSAFE, unknown order state, private stream down, manual stop,
    total loss cap). Every order is logged at SEND time (event order_sent) and again to its terminal state, never-filled ones included. A real-money run needs mainnet.violations_v2 to pass in the CLI; this class sends only what its client sends."""

    def __init__(self, client, recorder, symbols, instruments, cfg: PerpProbeConfig, budget: Budget, stop: asyncio.Event, capture):
        super().__init__(client, recorder, symbols, instruments, cfg, budget, stop, capture)
        self.live_orders: dict[str, tuple[str, str | None]] = {}                        # client id -> (symbol, order id) while an order may rest
        self.acct = {s: {"pos": Decimal(0), "cash": Decimal(0), "fees": Decimal(0)} for s in symbols}
        self.mids = {s: deque(maxlen=700) for s in symbols}                              # (monotonic s, mid), every 0.1 s
        self.killed: str | None = None
        self.paused_until_day: int | None = None
        self.invalid_since: dict[str, float] = {}
        self.stats.update(amends=0, amend_rejects=0, taker_unwinds=0, sharp_orders=0, sharp_timeouts=0, kills=0, day_pauses=0)
        self.day_equity0: tuple[int, Decimal] | None = None                              # (UTC day number, equity at its start)
        self.max_loss_seen = Decimal(0)

    # ---- accounting ---------------------------------------------------------------------------------------------------
    def _mid_now(self, symbol: str) -> Decimal | None:
        if not self.capture.valid.get(symbol):
            return None
        bids, asks = self.capture.books[symbol].top(1)
        return Decimal(str((bids[0][0] + asks[0][0]) / 2)) if bids and asks else None

    def equity(self) -> Decimal | None:
        total = Decimal(0)
        for s, a in self.acct.items():
            if a["pos"] != 0:
                m = self._mid_now(s)
                if m is None:
                    return None                                                          # cannot mark: the monitor treats a long gap as UNSAFE
                total += a["pos"] * m
            total += a["cash"] - a["fees"]
        return total

    def _apply_fill(self, symbol: str, side: str, qty: Decimal, price: Decimal, taker: bool) -> None:
        a = self.acct[symbol]
        signed = qty if side == "Buy" else -qty
        a["pos"] += signed
        a["cash"] -= signed * price
        a["fees"] += qty * price * Decimal(str(self.cfg.taker_fee_bps if taker else self.cfg.maker_fee_bps)) / Decimal(10000)
        self.net[symbol] = a["pos"]

    def loss_total(self) -> Decimal | None:
        e = self.equity()
        return None if e is None else -e

    # ---- the book ------------------------------------------------------------------------------------------------------
    def pre_move_bps(self, symbol: str, window_s: float) -> float | None:
        h = self.mids[symbol]
        if len(h) < 2:
            return None
        now_t, now_m = h[-1]
        for t, m in reversed(h):
            if now_t - t >= window_s:
                return float((now_m - m) / m * 10000)
        return None

    async def _mid_loop(self) -> None:
        while not self.stop.is_set():
            for s in self.symbols:
                m = self._mid_now(s)
                if m is not None:
                    self.mids[s].append((time.monotonic(), m))
            await self._sleep(0.1)

    # ---- kill switch and caps ---------------------------------------------------------------------------------------------
    def kill(self, reason: str) -> None:
        if self.killed is None:
            self.killed = reason
            self.stats["kills"] += 1
            self.rec.event("kill_switch", market=self.market, reason=reason)
            self.stop.set()

    async def _monitor(self) -> None:
        while not self.stop.is_set():
            now = time.monotonic()
            for s in self.symbols:
                if self.capture.valid.get(s):
                    self.invalid_since.pop(s, None)
                else:
                    first = self.invalid_since.setdefault(s, now)
                    if now - first > self.cfg.unsafe_feed_s:
                        self.kill(f"feed UNSAFE: capture book of {s} invalid for {now - first:.1f} s")
            if self.live_orders and not self.client.stream_alive:
                self.kill("private stream down with orders resting")
            if self.cfg.stop_file and os.path.exists(self.cfg.stop_file):
                self.kill("manual stop (stop file)")
            e = self.equity()
            if e is not None:
                loss = -e
                self.max_loss_seen = max(self.max_loss_seen, loss)
                if loss >= self.cfg.loss_cap_total:
                    self.kill(f"total loss cap: {loss:.2f} >= {self.cfg.loss_cap_total}")
                day = int(time.time() // 86400)
                if self.day_equity0 is None or self.day_equity0[0] != day:
                    self.day_equity0, self.paused_until_day = (day, e), None
                else:
                    loss_today = self.day_equity0[1] - e
                    if loss_today >= self.cfg.loss_cap_day and self.paused_until_day is None:
                        self.paused_until_day = day + 1
                        self.stats["day_pauses"] += 1
                        self.rec.event("day_loss_cap", market=self.market, loss_today=str(loss_today), cap=str(self.cfg.loss_cap_day))
                        asyncio.create_task(self._flatten_now("daily loss cap"))
            await self._sleep(0.5)

    async def _flatten_now(self, why: str) -> None:
        await self._cancel_all()
        for symbol in sorted(self.symbols):
            if self.acct[symbol]["pos"] != 0:
                await self._taker_unwind(symbol, why)

    # ---- orders ----------------------------------------------------------------------------------------------------------------
    def _unknown(self, reason: str, **kw) -> None:
        self.rec.event("unknown_order_state", market=self.market, reason=reason, **kw)
        self.kill(f"unknown order state: {reason}")

    def _choose_side(self, symbol: str, preferred: str | None) -> str:
        a = self.acct[symbol]
        mid = self._mid_now(symbol) or Decimal(0)
        notional = abs(a["pos"]) * mid
        cap = self.cfg.max_net_notional
        if a["pos"] != 0 and notional >= cap * Decimal(str(self.cfg.inventory_soft_share)):
            return "Sell" if a["pos"] > 0 else "Buy"                                      # a PostOnly unwind
        return preferred or self.rng.choice(("Buy", "Sell"))

    async def _taker_unwind(self, symbol: str, why: str) -> None:
        a = self.acct[symbol]
        size = a["pos"]
        if size == 0:
            return
        side = "Sell" if size > 0 else "Buy"
        self.n_sent += 1
        cid = f"{self.prefix}u{self.n_sent:05d}"
        pend = self.pending[cid] = ProbePending(cid)
        rec = {"market": self.market, "symbol": symbol, "kind": "taker_unwind", "client_id": cid, "side": side, "qty": str(abs(size)), "why": why, "t": {}, "ex": {}, "execs": [], "errors": [], "outcome": ""}
        res = await self.client.market_close(symbol, side, abs(size), cid)
        rec["t"].update(send_wall_ns=res.t_send_wall_ns, send_mono_ns=res.t_send_mono_ns, rest_wall_ns=res.t_ret_wall_ns, rest_mono_ns=res.t_ret_mono_ns)
        if not res.ok:
            rec["outcome"] = "rejected_on_rest"
            rec["reject_code"], rec["reject_msg"] = res.code, res.msg
            self.rec.order(rec)
            self.pending.pop(cid, None)
            self._unknown("taker unwind refused", symbol=symbol, code=res.code)
            return
        await self._wait(pend.done, self.cfg.cancel_timeout_s)
        await asyncio.sleep(0.2)
        filled, first, last = self._collect(pend, rec, rec["t"], rec["ex"])
        if filled <= 0:
            self.rec.order({**rec, "outcome": "no_fill_report"})
            self.pending.pop(cid, None)
            self._unknown("taker unwind without a fill report", symbol=symbol)
            return
        px = Decimal(rec["execs"][-1]["price"]) if rec["execs"] else self._mid_now(symbol)
        self._apply_fill(symbol, side, filled, px, taker=True)
        rec["outcome"], rec["filled_qty"] = "filled", str(filled)
        self.stats["taker_unwinds"] += 1
        self.rec.order(rec)
        self.pending.pop(cid, None)

    async def _order_v2(self, symbol: str, slot: int) -> dict | None:
        cfg = self.cfg
        inst = self.inst[symbol]
        sharp = self.rng.random() < cfg.sharp_share
        preferred = None
        pre_trigger = None
        if sharp:
            t0 = time.monotonic()
            while time.monotonic() - t0 < cfg.sharp_wait_s and not self.stop.is_set():
                pm = self.pre_move_bps(symbol, cfg.sharp_window_s)
                if pm is not None and abs(pm) >= cfg.sharp_bps:
                    pre_trigger, preferred = pm, ("Buy" if pm < 0 else "Sell")           # after a fall a bid, after a rise an ask: the side a maker quotes into the move
                    break
                await self._sleep(0.1)
            if pre_trigger is None:
                self.stats["sharp_timeouts"] += 1
        if self.stop.is_set():
            return None
        a = self.acct[symbol]
        mid_now = self._mid_now(symbol)
        if mid_now is None:
            raise NoOrder("capture book not valid")
        if abs(a["pos"]) * mid_now >= cfg.max_net_notional:
            await self._taker_unwind(symbol, "inventory cap")                              # only AT the cap: counted in the loss account
            return None
        side = self._choose_side(symbol, preferred)
        bids, asks = self._touch(symbol, side)
        back = self.rng.random() >= cfg.touch_share
        tick = inst.tick
        if side == "Buy":
            raw = Decimal(str(bids[0][0]))
            price = round_down(raw, tick) - (tick if back else 0)
        else:
            raw = Decimal(str(asks[0][0]))
            price = round_up(raw, tick) + (tick if back else 0)
        if price <= 0:
            raise NoOrder("no price")
        qty = min_quantity(inst, price, cfg.max_notional)
        own_side = bids if side == "Buy" else asks
        size_at_price = next((sz for p, sz in own_side if abs(p - float(price)) < 1e-12), None)
        cancel_after = self.rng.choice(cfg.cancel_after_s) if self.rng.random() < cfg.cancel_share else None
        amend_at = self.rng.choice(cfg.amend_after_s) if self.rng.random() < cfg.amend_share else None
        if amend_at is not None and cancel_after is not None and amend_at >= cancel_after:
            amend_at = None
        self.n_sent += 1
        cid = f"{self.prefix}{self.n_sent:06d}"
        pend = self.pending[cid] = ProbePending(cid)
        t, ex = {}, {}
        plan = {"placement": "back1" if back else "touch", "cancel_after_s": cancel_after, "amend_after_s": amend_at, "sharp_target": sharp, "sharp_trigger_bps": pre_trigger, "slot": slot}
        rec = {"market": self.market, "symbol": symbol, "kind": "perp_probe_v2", "client_id": cid, "side": side, "price": str(price), "qty": str(qty), "plan": plan,
               "best_bid_at_send": bids[0][0], "best_ask_at_send": asks[0][0], "size_at_price_at_send": size_at_price, "top5_at_send": {"bids": bids[:5], "asks": asks[:5]},
               "pre_move_bps_1s": self.pre_move_bps(symbol, 1.0), "pre_move_bps_5s": self.pre_move_bps(symbol, 5.0), "net_before": str(a["pos"]),
               "t": t, "ex": ex, "clock": dict(self.clock), "errors": [], "execs": [], "amends": [], "outcome": ""}
        self.traded.add(symbol)
        self.live_orders[cid] = (symbol, None)
        self.rec.event("order_sent", **{k: v for k, v in rec.items() if k not in ("t", "ex")})                         # at SEND time: a crash or a kill later cannot lose the plan or the book
        if sharp:
            self.stats["sharp_orders"] += 1
        res = await self.client.place(symbol, price, qty, cid, side=side)
        t.update(send_wall_ns=res.t_send_wall_ns, send_mono_ns=res.t_send_mono_ns, rest_wall_ns=res.t_ret_wall_ns, rest_mono_ns=res.t_ret_mono_ns)
        ex["rest_ms"] = res.ex_time_ms
        rec["order_id"] = res.order_id
        if not res.ok:
            self.live_orders.pop(cid, None)
            rec["outcome"] = "rejected_on_rest"
            rec["reject_code"], rec["reject_msg"] = res.code, res.msg
            self.pending.pop(cid, None)
            return rec
        self.live_orders[cid] = (symbol, res.order_id)
        await self._wait(pend.ack, cfg.ack_timeout_s)
        first = pend.evs[0] if pend.evs else None
        live = True
        if first is None:
            rec["errors"].append("no_stream_ack")
            if not any(o.get("client_id") == cid for o in await self.client.open_orders(symbol)):
                self._unknown("REST accepted the order but the stream and the open-order list know nothing of it", symbol=symbol)
        elif first.kind in ("rejected", "expired") or (first.kind == "cancelled" and not any(e.kind == "new" for e in pend.evs)):
            rec["outcome"] = "rejected_on_stream"
            rec["reject_reason"] = first.reason or first.status
            live = False
        t_start = time.monotonic()
        if live:
            if amend_at is not None:
                await self._wait(pend.done, amend_at)
                if not pend.done.is_set():
                    cur_bids, cur_asks = self._touch(symbol, side)
                    behind = (side == "Buy" and float(price) < cur_bids[0][0]) or (side == "Sell" and float(price) > cur_asks[0][0])
                    new_price = (round_down(Decimal(str(cur_bids[0][0])), tick) if side == "Buy" else round_up(Decimal(str(cur_asks[0][0])), tick)) if behind else (price - tick if side == "Buy" else price + tick)
                    ares = await self.client.amend(symbol, res.order_id, cid, new_price, qty, side)
                    rec["amends"].append({"from": str(price), "to": str(new_price), "ok": ares.ok, "code": ares.code, "msg": ares.msg, "send_wall_ns": ares.t_send_wall_ns, "send_mono_ns": ares.t_send_mono_ns,
                                          "ret_wall_ns": ares.t_ret_wall_ns, "ret_mono_ns": ares.t_ret_mono_ns, "ex_ms": ares.ex_time_ms})
                    self.stats["amends"] += 1
                    if not ares.ok:
                        self.stats["amend_rejects"] += 1
                    else:
                        price = new_price
            remaining = (cancel_after if cancel_after is not None else cfg.max_hold_s) - (time.monotonic() - t_start)
            if remaining > 0:
                await self._wait(pend.done, remaining)
            rec["hold_s"] = round(time.monotonic() - t_start, 3)
            if not pend.done.is_set():
                cres = await self.client.cancel(symbol, res.order_id, cid)
                t.update(csend_wall_ns=cres.t_send_wall_ns, csend_mono_ns=cres.t_send_mono_ns, crest_wall_ns=cres.t_ret_wall_ns, crest_mono_ns=cres.t_ret_mono_ns)
                ex["crest_ms"] = cres.ex_time_ms
                rec["cancel"] = {"ok": cres.ok, "code": cres.code, "msg": cres.msg}
                if not cres.ok:
                    self.stats["cancel_after_fill"] += 1
                done = await self._wait(pend.done, cfg.cancel_timeout_s)
                if not done and not cres.ok and not any(e.kind == "trade" for e in pend.evs):
                    self._unknown("cancel refused and no terminal event for the order", symbol=symbol, code=cres.code)
        self.live_orders.pop(cid, None)
        await asyncio.sleep(0.2)
        prev, first_fill, last_fill = self._collect(pend, rec, t, ex)
        rec["filled_qty"] = str(prev)
        rec["first_fill_ex_ms"], rec["last_fill_ex_ms"] = first_fill, last_fill
        if not rec["outcome"]:
            rec["outcome"] = "filled" if prev >= qty else "partial_then_cancelled" if prev > 0 else "cancelled_unfilled"
        if prev > 0:
            for e in rec["execs"]:
                self._apply_fill(symbol, side, Decimal(e["qty"]), Decimal(e["price"]), taker=False)
            self.stats["filled_orders"] += 1
            self.stats["fills"] += len(rec["execs"])
        if rec["outcome"] == "rejected_on_stream":
            self.stats["post_only_rejects"] += 1
        rec["net_after"] = str(self.acct[symbol]["pos"])
        self.pending.pop(cid, None)
        return rec

    async def _worker(self, symbol: str, slot: int) -> None:
        skips = 0
        while not self.stop.is_set():
            await self._sleep(self.rng.expovariate(1.0 / max(self.cfg.mean_gap_s, 0.05)))
            if self.stop.is_set():
                break
            if self.paused_until_day is not None and int(time.time() // 86400) < self.paused_until_day:
                continue
            if not self.client.stream_alive:
                if self.live_orders or not await self._reconnect_stream():                     # a reconnect loses the events of an order that rests: only with none resting
                    self.kill(f"private stream down ({self.client.stream_error})")
                    break
            if not self.budget.take():
                self.stop_reason = "order cap reached"
                self.stop.set()
                break
            try:
                rec = await self._order_v2(symbol, slot)
            except NoOrder as exc:
                self.budget.used -= 1
                self.stats["skipped"] += 1
                skips += 1
                self.rec.event("skipped", market=self.market, symbol=symbol, reason=str(exc))
                if skips >= self.cfg.max_skips:
                    self.kill(f"{symbol} could not be quoted {skips} times in a row")
                continue
            except Exception as exc:
                self.rec.event("error", market=self.market, symbol=symbol, error=f"{type(exc).__name__}: {exc}")
                self.stats["errors"] += 1
                self.consecutive_errors += 1
                if self.consecutive_errors >= self.cfg.max_consecutive_errors:
                    self.kill(f"{self.consecutive_errors} consecutive errors")
                continue
            if rec is None:
                self.budget.used -= 1
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
                    self.kill(f"{self.consecutive_errors} consecutive errors")

    async def _cancel_all(self) -> None:
        for cid, (symbol, oid) in list(self.live_orders.items()):
            try:
                await self.client.cancel(symbol, oid, cid)
            except BaseException as exc:
                self.rec.event("cleanup_error", market=self.market, step="cancel_live", error=type(exc).__name__)
        self.live_orders.clear()
        self.live = None
        await super()._cancel_all()

    async def run(self) -> None:
        sync_task = tasks = None
        try:
            await self.sync_clock()
            sync_task = asyncio.create_task(self._sync_loop())
            await self.client.start_stream(self.on_event)
            self.rec.event("stream_ready", market=self.market)
            await self.client.open_orders(self.symbols[0])
            if self.cfg.preflight and not await self._preflight():
                self.stop_reason = "preflight failed: the private stream did not deliver order events (no probe order was sent)"
                return
            for s in self.symbols:
                m = self._mid_now(s)
                if m is not None:
                    self.mids[s].append((time.monotonic(), m))
            tasks = [asyncio.create_task(self._mid_loop()), asyncio.create_task(self._monitor())]
            tasks += [asyncio.create_task(self._worker(s, k)) for s in self.symbols for k in range(self.cfg.max_open)]
            await self.stop.wait()
            if self.killed:
                self.stop_reason = "kill switch: " + self.killed
            elif self.stop_reason == "completed":
                self.stop_reason = "stop requested"
        except BaseException as exc:
            self.stop_reason = f"aborted: {type(exc).__name__}: {exc}"
            raise
        finally:
            for tk in (tasks or []):
                tk.cancel()
            await asyncio.gather(*(tasks or []), return_exceptions=True)
            if sync_task is not None:
                sync_task.cancel()
            await self.cleanup()
            self.rec.event("run_end", market=self.market, reason=self.stop_reason, loss_total=str(self.loss_total()), max_loss_seen=str(self.max_loss_seen),
                           positions={s: str(a["pos"]) for s, a in self.acct.items()}, **self.stats)
