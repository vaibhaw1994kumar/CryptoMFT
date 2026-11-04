"""The per-market lifecycle loop: place a minimum-size post-only buy below the touch, wait for the private-stream ack, hold,
cancel, wait for the cancel confirmation, record every timestamp. One order live per market at any time.

Safety, all enforced here: a shared hard cap on orders per run, spacing between sends, a stop after K consecutive errors, a
stop event for SIGINT/SIGTERM, and a `finally` that cancels the live order and sweeps any leftover order carrying this
run's client-id prefix.
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from decimal import Decimal

from .derive import derive
from .models import Instrument, UserEvent, stamp
from .pricing import NoOrder, crossing_buy_price, is_post_only_reject, min_quantity, parse_buy_price_cap, passive_buy_price
from .recorder import Recorder

HARD_MAX_ORDERS = 2000
MIN_SPACING_S = 0.2


@dataclass
class RunConfig:
    dist_bps: Decimal = Decimal(50)
    hold_s: float = 1.0
    hold_jitter_s: float = 0.5          # hold is hold_s + uniform(0, jitter), seeded, so cancels land at varied ages
    spacing_s: float = 0.5
    ack_timeout_s: float = 5.0
    cancel_timeout_s: float = 5.0
    max_consecutive_errors: int = 5
    max_notional: Decimal = Decimal(100)
    cap_margin_bps: Decimal = Decimal(100)  # price this far under a learned buy-price limit, because the venue's reference drifts
    reject_probe_every: int = 0         # 0 = never; N = every Nth order is a crossing post-only that must be rejected
    max_invalid_probes: int = 3         # probes the venue refused for some other reason; then probing stops for this market
    sync_every_s: float = 300.0
    seed: int = 1
    tag: str = "a"


class Budget:
    """Shared across markets: the hard cap on orders sent in one run."""

    def __init__(self, total: int):
        self.total, self.used = min(int(total), HARD_MAX_ORDERS), 0

    def take(self) -> bool:
        if self.used >= self.total:
            return False
        self.used += 1
        return True


@dataclass
class Pending:
    client_id: str
    events: dict[str, UserEvent] = field(default_factory=dict)
    ack: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)


class MarketRunner:
    def __init__(self, client, recorder: Recorder, symbols: list[str], instruments: dict[str, Instrument], cfg: RunConfig,
                 budget: Budget, stop: asyncio.Event):
        self.client, self.rec, self.symbols, self.inst, self.cfg = client, recorder, symbols, instruments, cfg
        self.budget, self.stop = budget, stop
        self.market = client.market
        self.prefix = f"ollog{cfg.tag}"
        self.rng = random.Random(f"{cfg.seed}:{self.market}")
        self.pending: dict[str, Pending] = {}
        self.live: tuple[str, str | None, str] | None = None        # (symbol, order_id, client_id) while an order may rest
        self.traded: set[str] = set()
        self.clock: dict = {}
        self.n_sent = 0
        self.consecutive_errors = 0
        self.price_caps: dict[str, Decimal] = {}                    # buy-price limits learned from 170193 rejections
        self.clean_symbols: set[str] = set()                        # symbols with a full clean lifecycle in this run
        self.probe_bad: set[str] = set()                            # symbols whose probe was refused for a non-post-only reason
        self.probe_cursor = 0
        self.stats = {"orders": 0, "clean": 0, "errors": 0, "price_limit_learned": 0, "probe_post_only_rejects": 0,
                      "probe_invalid": 0, "ws_events": 0, "ws_unmatched": 0}
        self.stop_reason = "completed"

    # ---- private stream ------------------------------------------------------------------------------------
    def on_event(self, ev: UserEvent) -> None:
        self.stats["ws_events"] += 1
        p = self.pending.get(ev.client_id)
        self.rec.event("ws_event", market=self.market, kind=ev.kind, symbol=ev.symbol, order_id=ev.order_id, client_id=ev.client_id,
                       status=ev.status, reason=ev.reason, ex_event_ms=ev.ex_event_ms, ex_txn_ms=ev.ex_txn_ms,
                       recv_wall_ns=ev.recv_wall_ns, recv_mono_ns=ev.recv_mono_ns, matched=p is not None)
        if p is None:
            self.stats["ws_unmatched"] += 1
            return
        p.events.setdefault(ev.kind, ev)
        if ev.kind in ("new", "rejected", "expired", "trade", "cancelled"):
            p.ack.set()
        if ev.kind in ("cancelled", "rejected", "expired") or (ev.kind == "trade" and ev.status == "Filled") \
                or ev.status == "FILLED":
            p.done.set()

    # ---- helpers --------------------------------------------------------------------------------------------
    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def _wait(self, evt: asyncio.Event, timeout: float) -> bool:
        try:
            await asyncio.wait_for(evt.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def sync_clock(self, samples: int = 5) -> dict:
        best = None
        for _ in range(samples):
            server_ms, w0, w1 = await self.client.server_time_ms()
            rtt = (w1 - w0) / 1e6
            if best is None or rtt < best[0]:
                best = (rtt, server_ms - (w0 + w1) / 2 / 1e6)
        self.clock = {"offset_ms": best[1], "uncertainty_ms": best[0] / 2, "at_wall_ns": stamp()[0]}
        self.client.offset_ms = int(round(best[1]))
        self.rec.event("clock_sync", market=self.market, samples=samples, **self.clock)
        return self.clock

    async def _sync_loop(self) -> None:
        while True:
            await self._sleep(self.cfg.sync_every_s)
            if self.stop.is_set():
                return
            try:
                await self.sync_clock()
            except Exception as exc:
                self.rec.event("clock_sync_failed", market=self.market, error=type(exc).__name__)

    # ---- one order ------------------------------------------------------------------------------------------
    async def _lifecycle(self, symbol: str, probe: bool) -> bool | str | None:
        """Returns True for a clean lifecycle. Expected rejects on a probe count as clean. None: the venue reported a
        buy-price limit, which is now remembered for the symbol; that order is neither clean nor an error. "probe_invalid": the
        probe was refused for a reason other than taking liquidity, so it measured nothing and is not an error either."""
        inst = self.inst[symbol]
        bid, ask = await self.client.best_bid_ask(symbol)
        cap = None if probe else self.price_caps.get(symbol)
        if probe:
            if bid <= 0:
                raise NoOrder("no bid")                     # an empty or stale side: the crossing price would be meaningless
            if symbol in self.price_caps and ask > self.price_caps[symbol]:
                raise NoOrder("ask is above the learned buy-price limit")
            price = crossing_buy_price(inst, ask)
        else:
            ref = await self.client.reference_price(symbol) if inst.pct_down is not None else None
            price = passive_buy_price(inst, bid, self.cfg.dist_bps, ref, cap)
        qty = min_quantity(inst, price, self.cfg.max_notional)

        self.n_sent += 1
        cid = f"{self.prefix}{self.n_sent:06d}"
        pend = self.pending[cid] = Pending(cid)
        t: dict = {}
        ex: dict = {}
        rec = {"market": self.market, "symbol": symbol, "kind": "reject_probe" if probe else "normal", "client_id": cid,
               "side": "buy", "price": str(price), "qty": str(qty), "bid_at_send": str(bid), "ask_at_send": str(ask),
               "t": t, "ex": ex, "clock": dict(self.clock), "errors": [], "outcome": ""}
        if cap is not None:
            rec["price_cap_applied"] = str(cap)
        self.traded.add(symbol)
        self.live = (symbol, None, cid)

        res = await self.client.place(symbol, price, qty, cid)
        t.update(send_wall_ns=res.t_send_wall_ns, send_mono_ns=res.t_send_mono_ns, rest_wall_ns=res.t_ret_wall_ns,
                 rest_mono_ns=res.t_ret_mono_ns)
        ex["rest_ms"] = res.ex_time_ms
        rec["order_id"] = res.order_id
        clean = True
        if res.ok:
            self.live = (symbol, res.order_id, cid)
            got = await self._wait(pend.ack, self.cfg.ack_timeout_s)
            first = next(iter(pend.events.values()), None) if got else None
            # Bybit linear reports a post-only refusal as Cancelled (reason EC_PostOnlyWillTakeLiquidity) with no New first
            instant_cancel = probe and first is not None and first.kind == "cancelled" \
                and is_post_only_reject(self.market, "", "", first.reason)
            if instant_cancel:
                pend.events.setdefault("rejected", first)
            if first is not None and (first.kind in ("rejected", "expired") or instant_cancel):
                rec["outcome"] = "rejected_on_stream"
                rec["reject_reason"] = first.reason or first.status
                clean, self.live = probe, None
                if probe:
                    rec["post_only_reject"] = is_post_only_reject(self.market, "", "", first.reason, first.kind)
            elif first is None:
                rec["errors"].append("no_stream_ack")
                clean = False
        else:
            rec["outcome"] = "rejected_on_rest"
            rec["reject_code"], rec["reject_msg"] = res.code, res.msg
            clean = probe
            if probe:
                rec["post_only_reject"] = is_post_only_reject(self.market, res.code, res.msg)
            self.live = None
            learned = None if probe else parse_buy_price_cap(res.code, res.msg)
            if learned is not None and (cap is None or learned < cap):      # a cap that did not get tighter is a real error
                self.price_caps[symbol] = learned * (1 - self.cfg.cap_margin_bps / Decimal(10000))
                rec["outcome"], rec["learned_price_cap"] = "rejected_price_limit", str(learned)
                clean = None

        if self.live is not None:
            if probe:
                rec["errors"].append("probe_order_rested")
                clean = False
            await self._sleep(self.cfg.hold_s + self.rng.uniform(0, self.cfg.hold_jitter_s))
            cres = await self.client.cancel(symbol, res.order_id, cid)
            t.update(csend_wall_ns=cres.t_send_wall_ns, csend_mono_ns=cres.t_send_mono_ns, crest_wall_ns=cres.t_ret_wall_ns,
                     crest_mono_ns=cres.t_ret_mono_ns)
            ex["crest_ms"] = cres.ex_time_ms
            if cres.ok:
                if not await self._wait(pend.done, self.cfg.cancel_timeout_s):
                    rec["errors"].append("no_stream_cancel_confirmation")
                    clean = False
                rec["outcome"] = rec["outcome"] or "cancelled"
            else:
                rec["errors"].append("cancel_failed")
                rec["cancel_code"], rec["cancel_msg"] = cres.code, cres.msg
                rec["outcome"] = "cancel_failed"
                clean = False
            self.live = None

        for kind, key in (("new", "wsnew"), ("cancelled", "wscan"), ("rejected", "wsrej"), ("expired", "wsrej")):
            ev = pend.events.get(kind)
            if ev is not None:
                t[f"{key}_wall_ns"], t[f"{key}_mono_ns"] = ev.recv_wall_ns, ev.recv_mono_ns
                ex[f"{key}_event_ms"], ex[f"{key}_txn_ms"] = ev.ex_event_ms, ev.ex_txn_ms
        if "trade" in pend.events:
            rec["errors"].append("order_traded")
            clean = False
        invalid_probe = probe and rec["outcome"].startswith("rejected") and not rec.get("post_only_reject")
        if invalid_probe:                                   # refused, but not for taking liquidity: says nothing about post-only
            rec["outcome"], rec["clean"], clean = "probe_rejected_other", False, "probe_invalid"
            self.probe_bad.add(symbol)
        else:
            rec["clean"] = bool(clean)
            if probe and clean:
                self.stats["probe_post_only_rejects"] += 1
            elif not probe and clean and rec["outcome"] == "cancelled":
                self.clean_symbols.add(symbol)
        rec["derived"] = derive(rec)
        self.rec.order(rec)
        self.pending.pop(cid, None)
        self.stats["orders"] += 1
        return clean

    async def _probe_candidate(self) -> str | None:
        """Next symbol, in rotation independent of the normal-order cycle, that is safe to probe: it has completed a clean
        lifecycle here, has not had a probe refused for another reason, and its book has both sides (below any learned cap)."""
        pool = [s for s in self.symbols if s in self.clean_symbols and s not in self.probe_bad]
        for step in range(len(pool)):
            sym = pool[(self.probe_cursor + step) % len(pool)]
            try:
                bid, ask = await self.client.best_bid_ask(sym)
            except Exception:
                continue
            cap = self.price_caps.get(sym)
            if bid > 0 and ask > 0 and (cap is None or ask <= cap):
                self.probe_cursor = (self.probe_cursor + step + 1) % len(pool)
                return sym
        return None

    # ---- run ------------------------------------------------------------------------------------------------
    async def run(self) -> None:
        sync_task = None
        try:
            await self.sync_clock()
            sync_task = asyncio.create_task(self._sync_loop())
            await self.client.start_stream(self.on_event)
            self.rec.event("stream_ready", market=self.market)
            await self.client.open_orders(self.symbols[0])          # signed read: fails fast on bad keys
            i = k = skipped = 0
            probe_due = False
            while not self.stop.is_set():
                if not self.client.stream_alive:
                    self.stop_reason = f"private stream down ({self.client.stream_error})"
                    break
                if not self.budget.take():
                    self.stop_reason = "order cap reached"
                    break
                i += 1
                probe_due = probe_due or (bool(self.cfg.reject_probe_every) and i % self.cfg.reject_probe_every == 0
                                          and self.stats["probe_invalid"] < self.cfg.max_invalid_probes)
                symbol, probe = None, False
                if probe_due:                                       # stays due until a safe symbol is available
                    symbol = await self._probe_candidate()
                    probe = symbol is not None
                    probe_due = not probe
                if symbol is None:
                    symbol = self.symbols[k % len(self.symbols)]
                    k += 1
                try:
                    clean = await self._lifecycle(symbol, probe)
                except NoOrder as exc:                              # nothing was sent: give the slot back
                    self.rec.event("skipped", market=self.market, symbol=symbol, reason=str(exc), probe=probe)
                    self.budget.used -= 1
                    skipped += 1
                    if skipped >= len(self.symbols):
                        self.stop_reason = "no symbol could be quoted"
                        break
                    continue
                except Exception as exc:
                    self.rec.event("error", market=self.market, symbol=symbol, error=f"{type(exc).__name__}: {exc}")
                    clean = False
                skipped = 0
                if clean == "probe_invalid":
                    self.stats["probe_invalid"] += 1
                    self.rec.event("probe_invalid", market=self.market, symbol=symbol)
                elif clean is None:
                    self.stats["price_limit_learned"] += 1
                    self.rec.event("price_limit_learned", market=self.market, symbol=symbol, cap=str(self.price_caps[symbol]))
                else:
                    self.stats["clean" if clean else "errors"] += 1
                    self.consecutive_errors = 0 if clean else self.consecutive_errors + 1
                    if self.consecutive_errors >= self.cfg.max_consecutive_errors:
                        self.stop_reason = f"{self.consecutive_errors} consecutive errors"
                        break
                await self._sleep(max(self.cfg.spacing_s, MIN_SPACING_S))
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

    async def cleanup(self) -> None:
        await self._cancel_all()
        await self._close_client()

    async def _close_client(self) -> None:
        try:
            await self.client.close()
        except BaseException:
            pass

    async def _cancel_all(self) -> None:
        """Cancel the live order, then sweep open orders on every traded symbol that carry this run's prefix."""
        leftovers = 0
        if self.live is not None:
            symbol, oid, cid = self.live
            try:
                await self.client.cancel(symbol, oid, cid)
                leftovers += 1
            except BaseException as exc:
                self.rec.event("cleanup_error", market=self.market, step="cancel_live", error=type(exc).__name__)
            self.live = None
        for symbol in sorted(self.traded):
            try:
                for o in await self.client.open_orders(symbol):
                    if str(o.get("client_id", "")).startswith(self.prefix):
                        await self.client.cancel(symbol, o.get("order_id"), o["client_id"])
                        leftovers += 1
            except BaseException as exc:
                self.rec.event("cleanup_error", market=self.market, step="sweep", symbol=symbol, error=type(exc).__name__)
        self.rec.event("cleanup", market=self.market, cancelled=leftovers)
