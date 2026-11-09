"""Expected hedge move estimated from MARKET DATA only (v1.1 step 9a).

The conditional edge of the v1 / v1.1 profiles needs E[hedge price | our passive fill] - quote, in bps (ArbConfig.expected_hedge_move_bps_{bid,ask}).
v1 estimated it from the executed hedges of legacy_v0 REPLAYS. Those replays contain simulated fills that no trade supports, and the price moves after
those fills were unusually good, so the number was optimistic and the quotes built on it too aggressive. This module needs no simulated fill:

    for every public print on the MAKER venue that would hit our passive quote (a sell aggressor for the bid side, a buy aggressor for the ask side),
    take the HEDGE venue's mid at the print's exchange time t and at t + horizon_s (default 1 s), both from the venue's own book rebuilt with the
    engine's OrderBook. One event per (exchange time, aggressor side): the several prints of one sweep count once.

    move_bid = (mid(t+h) - mid(t)) / mid(t) * 1e4          a passive bid filled -> the hedge SELLS; a fall of the hedge mid is adverse, so negative = adverse
    move_ask = -(mid(t+h) - mid(t)) / mid(t) * 1e4         a passive ask filled -> the hedge BUYS; a rise of the hedge mid is adverse, so negative = adverse
    (the sign convention of edge.maker_taker_expected_edge_bps: expected hedge = touch * (1 + move_bid/1e4) for a sell, touch * (1 - move_ask/1e4) for a buy)

Only TRAIN days may be passed (the caller's rule, recorded in the output). A print counts only when the hedge book is ready and fresh (its last update at most
max_book_age_s before t and before t + h), so a truncated or gapped hedge feed contributes no pairs instead of stale ones. Exchange times on both venues.
CLI: python -m new_backtest.market_hedge_move --data-root D --pair SOLUSDT --days 2024-03-01,2024-03-02 --out file.json [--jobs 3]
EXPLORATORY: it is the unconditional drift after a print, not the drift after OUR fills (those are adversely selected); the conditional number needs real fills."""
from __future__ import annotations

import argparse
import heapq
import json
import math
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

from .event_loader import BOOK, DELTA, TRADE, delta_fields, iter_venue_events, reset_truncation, snapshot_levels, trade_fields, truncation_report
from .order_book import OrderBook

HIST_BPS = 0.05           # histogram resolution for the pooled median
HIST_CLIP = 50.0


def day_stats(data_root, pair: str, day: str, maker_venue: str = "bybit", hedge_venue: str = "binance",
              horizon_s: float = 1.0, max_book_age_s: float = 2.0) -> dict:
    """Pair counts and move sums for ONE day (a day's pending tail at midnight is dropped, counted as unresolved)."""
    reset_truncation()
    book = OrderBook(hedge_venue, pair, tape_repair=False)
    hedge = iter_venue_events(data_root, hedge_venue, pair, day, day, include_snapshots=True, include_deltas=True, include_trades=False)
    maker = iter_venue_events(data_root, maker_venue, pair, day, day, include_snapshots=False, include_deltas=False, include_trades=True, trade_dedupe="multiset")
    # hedge events sort before maker trades at equal exchange time: a print at t sees the hedge book including every event up to t
    merged = heapq.merge(((e.exchange_ts, 0, i, e) for i, e in enumerate(hedge)), ((e.exchange_ts, 1, i, e) for i, e in enumerate(maker)), key=lambda x: x[:3])
    acc = {s: {"n": 0, "sum": 0.0, "sumsq": 0.0, "hist": {}} for s in ("bid", "ask")}
    cnt = {"maker_prints": 0, "events": 0, "no_book_at_t": 0, "stale_at_t": 0, "stale_at_t_plus_h": 0, "unresolved_at_end": 0, "hedge_events": 0}
    pending: deque = deque()                 # (t, side, mid0)
    last_key = None

    def resolve_until(limit_ts: float) -> None:
        while pending and pending[0][0] + horizon_s < limit_ts:
            t, side, mid0 = pending.popleft()
            t1 = t + horizon_s
            if not book.ready or book.mid <= 0 or t1 - book.last_update_ts > max_book_age_s:
                cnt["stale_at_t_plus_h"] += 1
                continue
            bps = (book.mid - mid0) / mid0 * 1e4
            if side == "ask":
                bps = -bps
            a = acc[side]
            a["n"] += 1; a["sum"] += bps; a["sumsq"] += bps * bps
            k = int(round(max(-HIST_CLIP, min(HIST_CLIP, bps)) / HIST_BPS))
            a["hist"][k] = a["hist"].get(k, 0) + 1

    for ts, kind, _, ev in merged:
        if kind == 0:
            resolve_until(ts)
            cnt["hedge_events"] += 1
            if ev.kind == BOOK:
                bids, asks = snapshot_levels(ev.payload)
                if bids and asks:
                    book.apply_snapshot(bids, asks, ts)
            elif ev.kind == DELTA:
                side, price, volume, event_type = delta_fields(ev.payload)
                if price > 0:
                    book.apply_delta(side, price, volume, event_type, ts)
            continue
        if ev.kind != TRADE:
            continue
        cnt["maker_prints"] += 1
        _, _, sign = trade_fields(ev.payload)
        key = (ts, sign)
        if key == last_key:
            continue                          # the same sweep: one event
        last_key = key
        cnt["events"] += 1
        if not book.ready or book.mid <= 0:
            cnt["no_book_at_t"] += 1
            continue
        if ts - book.last_update_ts > max_book_age_s:
            cnt["stale_at_t"] += 1
            continue
        pending.append((ts, "bid" if sign < 0 else "ask", book.mid))
    cnt["unresolved_at_end"] = len(pending)
    return {"day": day, "counts": cnt, "acc": {s: {**a, "hist": {str(k): v for k, v in a["hist"].items()}} for s, a in acc.items()},
            "truncation": truncation_report()}


def day_labels(data_root, pair: str, day: str, maker_venue: str = "bybit", hedge_venue: str = "binance", horizons: tuple = (1.0, 5.0, 30.0), max_book_age_s: float = 2.0,
               tick: float = 0.001, vol_window_s: float = 30.0, flow_window_s: float = 1.0, hook=None, tick_hook=None) -> dict:
    """S1 (owner 9 Nov): one LABEL ROW per public print event on the maker venue that would hit our passive quote, from MARKET DATA only (no strategy fill, simulated or not).
    Event, side, hedge-venue mid and the adverse sign are exactly `day_stats`'s. Each row carries the features known at the print and the hedge-mid move after each horizon (negative = adverse):
      depth_ticks   how far the DEEPEST price of the sweep is behind the maker touch just before it (0 = the touch itself): a quote that deep or shallower is filled by this print
      spread_bps    maker-venue touch spread in bps of its mid, at the print
      vol_bps       standard deviation of the hedge mid's 1 s changes over the last `vol_window_s`, in bps (None until 5 changes are known)
      flow          signed maker-venue volume of the last `flow_window_s` BEFORE the print, positive when it ran in this print's direction (the aggressor's), in base units
      move_<h>s     hedge-mid move after h seconds (None when the hedge book was stale or missing at t or t + h, or the day ended first)
    Only TRAIN days may be fitted from these rows (the caller's rule).
    S2 hedge event study (owner 9 Nov): `hook(row, hedge_book, maker_book, sweep)` is called for every event as it is created, with both books exactly as they stood BEFORE the sweep (the maker touch is the pre-sweep touch) and the sweep's total printed volume in sweep['volume'];
    `tick_hook(ts)` is called before each stream item is applied (and with infinity at the end), so a caller can resolve its own timed actions against the books as they stood strictly before `ts`."""
    reset_truncation()
    hedge_book = OrderBook(hedge_venue, pair, tape_repair=False)
    maker_book = OrderBook(maker_venue, pair, tape_repair=False)
    hedge_ev = iter_venue_events(data_root, hedge_venue, pair, day, day, include_snapshots=True, include_deltas=True, include_trades=False)
    maker_ev = iter_venue_events(data_root, maker_venue, pair, day, day, include_snapshots=True, include_deltas=True, include_trades=True, trade_dedupe="multiset")
    merged = heapq.merge(((e.exchange_ts, 0, i, e) for i, e in enumerate(hedge_ev)), ((e.exchange_ts, 1, i, e) for i, e in enumerate(maker_ev)), key=lambda x: x[:3])
    rows: list[dict] = []
    pending = {h: deque() for h in horizons}          # per horizon: (row index, t, side, mid0)
    cnt = {"prints": 0, "events": 0, "no_book": 0, "stale": 0, "unresolved_at_end": 0}
    mids: deque = deque()                              # (second, hedge mid): one per second, for the volatility
    flows: deque = deque()                             # (ts, signed volume) of recent maker prints
    last_sec = None
    group: dict | None = None                          # the sweep being assembled: same exchange time and aggressor side

    def resolve_until(limit_ts: float) -> None:
        for h, q in pending.items():
            while q and q[0][1] + h < limit_ts:
                idx, t, side, mid0 = q.popleft()
                if not hedge_book.ready or hedge_book.mid <= 0 or (t + h) - hedge_book.last_update_ts > max_book_age_s:
                    continue
                bps = (hedge_book.mid - mid0) / mid0 * 1e4
                rows[idx][f"move_{h:g}s"] = -bps if side == "ask" else bps

    def close_group() -> None:
        nonlocal group
        if group is None:
            return
        g, group = group, None
        cnt["events"] += 1
        if not (hedge_book.ready and hedge_book.mid > 0 and maker_book.ready and maker_book.mid > 0):
            cnt["no_book"] += 1
            return
        if g["ts"] - hedge_book.last_update_ts > max_book_age_s or g["ts"] - maker_book.last_update_ts > max_book_age_s:
            cnt["stale"] += 1
            return
        side = "bid" if g["sign"] < 0 else "ask"
        touch = maker_book.best_bid if side == "bid" else maker_book.best_ask
        deepest = g["min_px"] if side == "bid" else g["max_px"]
        depth = max(0, int(round(((touch - deepest) if side == "bid" else (deepest - touch)) / tick)))
        ch = [b - a for (_, a), (_, b) in zip(mids, list(mids)[1:])]
        vol = None
        if len(ch) >= 5:
            m = sum(ch) / len(ch)
            vol = math.sqrt(sum((x - m) ** 2 for x in ch) / (len(ch) - 1)) / hedge_book.mid * 1e4
        flow = sum(v for t, v in flows if g["ts"] - flow_window_s <= t < g["ts"]) * (1 if g["sign"] > 0 else -1)
        row = {"ts": g["ts"], "side": side, "depth_ticks": depth, "spread_bps": (maker_book.best_ask - maker_book.best_bid) / maker_book.mid * 1e4, "vol_bps": vol, "flow": flow,
               **{f"move_{h:g}s": None for h in horizons}}
        rows.append(row)
        for h in horizons:
            pending[h].append((len(rows) - 1, g["ts"], side, hedge_book.mid))
        if hook is not None:
            hook(row, hedge_book, maker_book, g)

    for ts, kind, _, ev in merged:
        if tick_hook is not None:
            tick_hook(ts)
        if kind == 0:
            close_group()
            resolve_until(ts)
            if ev.kind == BOOK:
                bids, asks = snapshot_levels(ev.payload)
                if bids and asks:
                    hedge_book.apply_snapshot(bids, asks, ts)
            elif ev.kind == DELTA:
                side, price, volume, event_type = delta_fields(ev.payload)
                if price > 0:
                    hedge_book.apply_delta(side, price, volume, event_type, ts)
            sec = int(ts)
            if hedge_book.ready and hedge_book.mid > 0 and sec != last_sec:
                last_sec = sec
                mids.append((sec, hedge_book.mid))
                while mids and mids[0][0] < sec - vol_window_s:
                    mids.popleft()
            continue
        if ev.kind == BOOK:
            close_group()
            bids, asks = snapshot_levels(ev.payload)
            if bids and asks:
                maker_book.apply_snapshot(bids, asks, ts)
            continue
        if ev.kind == DELTA:
            close_group()
            side, price, volume, event_type = delta_fields(ev.payload)
            if price > 0:
                maker_book.apply_delta(side, price, volume, event_type, ts)
            continue
        if ev.kind != TRADE:
            continue
        price, vol, sign = trade_fields(ev.payload)
        if price <= 0 or vol <= 0:
            continue
        cnt["prints"] += 1
        if group is not None and (group["ts"], group["sign"]) != (ts, sign):
            close_group()
        if group is None:
            group = {"ts": ts, "sign": sign, "min_px": price, "max_px": price, "volume": 0.0}
        group["min_px"], group["max_px"] = min(group["min_px"], price), max(group["max_px"], price)
        group["volume"] += vol
        flows.append((ts, vol * (1 if sign > 0 else -1)))
        while flows and flows[0][0] < ts - 5 * flow_window_s:
            flows.popleft()
    close_group()
    if tick_hook is not None:
        tick_hook(float("inf"))
    cnt["unresolved_at_end"] = sum(len(q) for q in pending.values())
    return {"day": day, "counts": cnt, "rows": rows, "truncation": truncation_report(), "horizons": list(horizons), "tick": tick, "max_book_age_s": max_book_age_s}


# ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------
# S2 hedge event study (owner 9 Nov): the Binance hedge of every event where OUR quote would have been posted and filled, simulated for each cap, on MARKET DATA only.
# ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------
STUDY_VARIANTS = ("slack2", "slack3", "slack5", "slack8", "slack12", "be0", "be1", "be2", "be4", "be8")     # touch-slack floors (bps) and break-even-plus-buffer buffers (bps; the Phase 1-lite grid 0, 1, 2, 4, 8)


def study_setup(profile_name: str = "perp_perp_maker_taker_v1_6") -> dict:
    """The profile's own numbers for the study, taken the way `session_replay.live_paper_setup` takes them: the arb config (entry rule, fees, the pinned adverse table), the venue fees, the hedge retry rule."""
    from .config import core_fee_specs, make_venues
    from .profiles import apply_profile, load_profile

    cfg = apply_profile(load_profile(profile_name), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", data_root="", output_dir="")
    fee_specs, _ = core_fee_specs(cfg.pair, cfg.bybit_fee_preset, cfg.binance_fee_preset)
    specs = make_venues(cfg.latency_preset, cfg.bybit_fee_preset, cfg.binance_fee_preset, fee_specs=fee_specs)
    arb = cfg.arb
    arb.venue_ticks = {"bybit": 0.001, "binance": 0.001}          # the March-2024 SOLUSDT grid on both venues (measured; the runner declares the same)
    hf, hedge_cfg = cfg.hedge_faults, cfg.execution.hedge
    return {"profile": profile_name, "arb": arb, "maker_bps": specs["bybit"].fees.maker_bps, "hedge_taker_bps": specs["binance"].fees.taker_bps, "maker_venue_taker_bps": specs["bybit"].fees.taker_bps,
            "retries": int(getattr(hf, "max_retries", 0) or 0), "backoff_s": float(getattr(hf, "retry_backoff_s", 0.0) or 0.0), "unwind_band_bps": float(getattr(hf, "unwind_band_bps", 15.0)),
            "slack_floor_today": float(hedge_cfg.get("slack_bps_floor", 5.0)), "max_walk_bps": float(arb.max_walk_bps), "tick": 0.001}


class HedgeStudy:
    """One event = a sweep on the maker venue that would fill a quote of the profile's own entry rule. For each cap variant a hedge chain is played on the hedge venue's best bid/ask (L1) with the profile's retry rule.
    Assumptions, all stated in the result (ASSUMED, no measured Binance delay exists): the hedge is dispatched at the print (decision time) and ARRIVES `d_send_s` later; an unfilled IOC is reported `d_report_s` after its
    arrival, the retry is dispatched `backoff_s` after that report; the cap is computed from the hedge touch at DISPATCH and the IOC is judged at ARRIVAL; an IOC fills, in full, at the arrival touch when the touch is inside its
    cap (the data is best bid/ask only: the hedge size is assumed to fit at the touch, and how often it did not is counted); a chain with no fill after 1 + retries sends is UNWOUND on the maker venue with a taker order
    banded at `unwind_band_bps` from its dispatch touch (the old backtester's unwind, `unwind_band_bps` of the profile), filled at the maker touch at its arrival when inside the band, else priced at the band edge.
    Cost (bps of the quote price, after fees, positive = a loss): -[(hedge price - q)/q (a bid fill) or (q - hedge price)/q (an ask fill)] + maker fee + the venue taker fee of whichever order closed the exposure."""

    def __init__(self, setup: dict, *, d_send_s: float = 0.045, d_report_s: float = 0.045, d_unwind_s: float = 0.035, size: float = 1.0, max_book_age_s: float = 2.0, min_gap_s: float = 1.0) -> None:
        import heapq as _hq
        from types import SimpleNamespace

        from crypto_stat_arb.ems.ioc_runtime import IocRuntime
        from crypto_stat_arb.hedge_lifecycle import touch_slack_price
        from crypto_stat_arb.strategies import edge as edge_mod
        from crypto_stat_arb.strategies.arb_strategy import CrossVenueArbStrategy

        self.s, self.arb, self.edge_mod, self.hq, self.touch_slack_price = setup, setup["arb"], edge_mod, _hq, touch_slack_price
        self.ns = SimpleNamespace(config=self.arb)
        self.quote_limit, self.quote_to_tick = CrossVenueArbStrategy._quote_limit, CrossVenueArbStrategy._quote_to_tick
        self.break_even = IocRuntime.break_even_cap
        self.d_send, self.d_report, self.d_unwind, self.size, self.max_age = d_send_s, d_report_s, d_unwind_s, size, max_book_age_s
        self.heap: list = []
        self.seq = 0
        self.limit = 0.0
        self.events: list[dict] = []
        self.counts = {"labelled_events": 0, "no_hedge_touch": 0, "stale_hedge_book": 0, "entry_rule_rejects": 0, "thinned_by_min_gap": 0, "quote_not_reached_by_sweep_volume": 0, "eligible_improving_quote": 0, "eligible": 0, "touch_size_below_hedge_size_at_decision": 0}
        self.rejects: dict[str, int] = {}
        self.hedge_book = self.maker_book = None
        self.min_gap_s = min_gap_s          # one counted event per side per `min_gap_s`: a persistent dislocation would otherwise count the same quote again and again (the strategy holds one chain at a time)
        self.last_eligible_ts: dict[str, float] = {}

    # -- the strategy's own entry rule, applied at the event ------------------------------------------------------------------------------------------------------------
    def _quote(self, row, hedge_book, maker_book):
        arb, cfg_s, edge_mod = self.arb, self.s, self.edge_mod
        side = row["side"]
        bid = side == "bid"
        hedge_px = hedge_book.best_bid if bid else hedge_book.best_ask
        touch_bid, touch_ask = maker_book.best_bid, maker_book.best_ask
        fee_bps = cfg_s["maker_bps"] + cfg_s["hedge_taker_bps"]
        table = arb.adverse_table
        if table is not None:
            move = table.expected_move_reaching_bps(side, quote_depth_ticks=0, vol_bps=row["vol_bps"], flow=row["flow"], spread_bps=row["spread_bps"], horizon_s=arb.adverse_horizon_s)
        else:
            move = arb.expected_hedge_move_bps_bid if bid else arb.expected_hedge_move_bps_ask
        limit = self.quote_limit(self.ns, side, hedge_px, fee_bps, move)
        px = self.quote_to_tick(self.ns, "bybit", limit, side)
        joins = False
        if bid:
            was_ok = touch_bid < limit < touch_ask
            if abs(px - touch_bid) < 1e-6 * arb.venue_ticks["bybit"]:
                px = touch_bid
            if was_ok and not touch_bid < px < touch_ask:
                if arb.join_touch_on_tick_rounding and px == touch_bid:
                    joins = True
                else:
                    return None, "tick_rounding_no_improve"
            if not joins and (px <= touch_bid or px >= touch_ask):
                return None, "no_edge"
        else:
            was_ok = touch_bid < limit < touch_ask
            if abs(px - touch_ask) < 1e-6 * arb.venue_ticks["bybit"]:
                px = touch_ask
            if was_ok and not touch_bid < px < touch_ask:
                if arb.join_touch_on_tick_rounding and px == touch_ask:
                    joins = True
                else:
                    return None, "tick_rounding_no_improve"
            if not joins and (px <= touch_bid or px >= touch_ask):
                return None, "no_edge"
        er = edge_mod.maker_taker_expected_edge_bps(px, side, hedge_book, self.size, cfg_s["maker_bps"], cfg_s["hedge_taker_bps"], expected_hedge_move_bps=move,
                                                    expected_hedge_slippage_bps=arb.expected_hedge_slippage_bps, funding_holding_bps=arb.funding_holding_bps, p_fail=arb.p_fail, loss_fail_bps=arb.loss_fail_bps)
        if er.net_bps is None or er.net_bps - arb.safety_buffer_bps < arb.min_net_edge_bps:
            return None, "no_edge"
        return px, "ok"

    # -- timed actions ---------------------------------------------------------------------------------------------------------------------------------------------------
    def tick(self, ts: float) -> None:
        self.limit = ts
        self._drain(ts)

    def _drain(self, limit: float) -> None:
        while self.heap and self.heap[0][0] < limit:
            t, _, kind, ch = self.hq.heappop(self.heap)
            getattr(self, "_" + kind)(t, ch)

    def _push(self, t: float, kind: str, ch: dict) -> None:
        self.seq += 1
        self.hq.heappush(self.heap, (t, self.seq, kind, ch))

    def _cap(self, ch: dict, touch: float) -> float:
        side_h = "sell" if ch["side"] == "bid" else "buy"          # a bid fill is hedged by a SELL on the hedge venue
        v = ch["variant"]
        if v.startswith("slack"):
            slack = max(self.s["max_walk_bps"], float(v[5:]))
            raw = self.touch_slack_price(side_h, touch, slack)
            tick = self.s["tick"]
            n = math.floor(raw / tick + 1e-9) if side_h == "buy" else math.ceil(raw / tick - 1e-9)          # the OMS's grid rule: a buy floors, a sell ceilings
            return round(n * tick, 10)
        ns = type("N", (), {"cap_buffer_bps": float(v[2:])})()
        return self.break_even(ns, side_h, ch["q"], self.s["maker_bps"], self.s["hedge_taker_bps"])          # ONE fixed limit for the whole chain

    def _dispatch(self, t: float, ch: dict) -> None:
        hb = self.hedge_book
        bid = ch["side"] == "bid"
        if not hb.ready or t - hb.last_update_ts > self.max_age:
            ch["dispatch_stale"] = ch.get("dispatch_stale", 0) + 1
        touch = hb.best_bid if bid else hb.best_ask
        ch["cap"] = ch["cap"] if ch["variant"].startswith("be") and "cap" in ch else self._cap(ch, touch)
        ch["submits"] += 1
        self._push(t + self.d_send, "arrive", ch)

    def _arrive(self, t: float, ch: dict) -> None:
        hb = self.hedge_book
        bid = ch["side"] == "bid"
        touch = hb.best_bid if bid else hb.best_ask
        size = hb.best_bid_size if bid else hb.best_ask_size
        inside = touch >= ch["cap"] if bid else touch <= ch["cap"]
        if touch > 0 and inside:
            ch["touch_small"] = size < self.size - 1e-12
            self._finish(ch, t, "hedged", touch, None)
            return
        if ch["submits"] <= self.s["retries"]:
            self._push(t + self.d_report + self.s["backoff_s"], "dispatch", ch)
            return
        self._push(t + self.d_report, "unwind_dispatch", ch)

    def _unwind_dispatch(self, t: float, ch: dict) -> None:
        mb = self.maker_book
        ch["maker_touch_at_fail"] = mb.best_bid if ch["side"] == "bid" else mb.best_ask          # the unwind's band is measured from the maker touch when it is dispatched
        self._push(t + self.d_unwind, "unwind", ch)

    def _unwind(self, t: float, ch: dict) -> None:
        mb = self.maker_book
        bid = ch["side"] == "bid"
        touch = mb.best_bid if bid else mb.best_ask          # a bid fill is unwound by SELLING on the maker venue at its bid
        ref = ch["maker_touch_at_fail"]
        band = self.s["unwind_band_bps"] / 1e4
        worst = ref * (1 - band) if bid else ref * (1 + band)
        price, missed = (touch, False) if (touch >= worst if bid else touch <= worst) else (worst, True)
        self._finish(ch, t, "unwound_band_miss" if missed else "unwound", price, self.s["maker_venue_taker_bps"])

    def _finish(self, ch: dict, t: float, outcome: str, price: float, closing_taker_bps) -> None:
        q, bid = ch["q"], ch["side"] == "bid"
        pnl = ((price - q) if bid else (q - price)) / q * 1e4
        fees = self.s["maker_bps"] + (self.s["hedge_taker_bps"] if closing_taker_bps is None else closing_taker_bps)
        ch["done"] = {"outcome": outcome, "submits": ch["submits"], "first_attempt_fill": outcome == "hedged" and ch["submits"] == 1, "time_to_flat_s": round(t - ch["t0"], 4),
                      "cost_bps": round(-(pnl - fees), 4), "price_drift_bps": round(-pnl, 4), "touch_small": bool(ch.get("touch_small", False)), "dispatch_stale": ch.get("dispatch_stale", 0)}
        ch["event"]["variants"][ch["variant"]] = ch["done"]

    # -- the event hook ----------------------------------------------------------------------------------------------------------------------------------------------------
    def on_event(self, row: dict, hedge_book, maker_book, sweep: dict) -> None:
        self.hedge_book, self.maker_book = hedge_book, maker_book
        c = self.counts
        c["labelled_events"] += 1
        bid = row["side"] == "bid"
        if row["ts"] - self.last_eligible_ts.get(row["side"], -1e18) < self.min_gap_s:
            c["thinned_by_min_gap"] += 1
            return
        if not (hedge_book.ready and (hedge_book.best_bid if bid else hedge_book.best_ask) > 0):
            c["no_hedge_touch"] += 1
            return
        if row["ts"] - hedge_book.last_update_ts > self.max_age:
            c["stale_hedge_book"] += 1
            return
        q, why = self._quote(row, hedge_book, maker_book)
        if q is None:
            c["entry_rule_rejects"] += 1
            self.rejects[why] = self.rejects.get(why, 0) + 1
            return
        touch_px = maker_book.best_bid if bid else maker_book.best_ask
        touch_size = maker_book.best_bid_size if bid else maker_book.best_ask_size
        improves = q > touch_px if bid else q < touch_px
        # would the sweep have filled OUR quote? an improving quote is alone at its price; a quote joining the touch sits behind the whole displayed touch size (no cancellations assumed: conservative)
        need = self.size if improves else touch_size + self.size
        if sweep["volume"] < need - 1e-12:
            c["quote_not_reached_by_sweep_volume"] += 1
            return
        self.last_eligible_ts[row["side"]] = row["ts"]
        c["eligible"] += 1
        c["eligible_improving_quote"] += int(improves)
        size_at = hedge_book.best_bid_size if bid else hedge_book.best_ask_size
        if size_at < self.size - 1e-12:
            c["touch_size_below_hedge_size_at_decision"] += 1
        ev = {"ts": row["ts"], "side": row["side"], "q": q, "hedge_touch": hedge_book.best_bid if bid else hedge_book.best_ask, "maker_touch": maker_book.best_bid if bid else maker_book.best_ask, "variants": {}}
        self.events.append(ev)
        for v in STUDY_VARIANTS:
            ch = {"variant": v, "side": row["side"], "q": q, "t0": row["ts"], "submits": 0, "event": ev}
            self._dispatch(row["ts"], ch)
        self._drain(self.limit)           # arrivals that fall before the stream item being applied now are judged against the books as they stand now


def day_hedge_study(data_root, pair: str, day: str, profile_name: str = "perp_perp_maker_taker_v1_6", maker_venue: str = "bybit", hedge_venue: str = "binance", **kw) -> dict:
    """One day of the S2 hedge event study (see `HedgeStudy`). Market data only; no simulator fill is used."""
    setup = study_setup(profile_name)
    st = HedgeStudy(setup, **kw)
    lab = day_labels(data_root, pair, day, maker_venue, hedge_venue, horizons=(0.05,), hook=st.on_event, tick_hook=st.tick)
    unresolved = len(st.heap)
    return {"day": day, "profile": profile_name, "counts": {**st.counts, "unresolved_actions_at_end": unresolved, "label_counts": lab["counts"]}, "entry_rule_reject_reasons": st.rejects, "events": st.events,
            "assumptions": {"d_send_s": st.d_send, "d_report_s": st.d_report, "d_unwind_s": st.d_unwind, "backoff_s": setup["backoff_s"], "retries": setup["retries"], "unwind_band_bps": setup["unwind_band_bps"], "size": st.size,
                            "maker_bps": setup["maker_bps"], "hedge_taker_bps": setup["hedge_taker_bps"], "maker_venue_taker_bps": setup["maker_venue_taker_bps"], "max_walk_bps": setup["max_walk_bps"], "min_gap_s": st.min_gap_s,
                            "fill_rule": "an improving quote is filled when the sweep prints at least our size; a quote joining the touch when the sweep prints at least the displayed touch size plus our size (no cancellations: conservative)",
                            "label": "ASSUMED delays (no measured Binance delay exists); Binance March-2024 is best bid/ask only: the hedge size is assumed to fit at the touch"},
            "truncation": lab["truncation"]}


def combine(per_day: list[dict], horizon_s: float, maker_venue: str, hedge_venue: str, pair: str, data_root: str) -> dict:
    out = {"label": "expected hedge move from MARKET DATA only (v1.1 step 9a); EXPLORATORY", "method": "per public print on the maker venue that hits our side (one event per exchange time and aggressor side): hedge-venue mid at t and t+horizon, bps; bid = change, ask = -change",
           "pair": pair, "maker_venue": maker_venue, "hedge_venue": hedge_venue, "horizon_s": horizon_s, "data_root": str(data_root),
           "days": [d["day"] for d in per_day], "per_day": {d["day"]: {"counts": d["counts"], "truncation": d["truncation"],
                                                                          **{s: {"n": d["acc"][s]["n"], "mean_bps": (d["acc"][s]["sum"] / d["acc"][s]["n"]) if d["acc"][s]["n"] else None}
                                                                             for s in ("bid", "ask")}} for d in per_day},
           "expected_hedge_move_bps": {}, "stats": {}}
    for s in ("bid", "ask"):
        n = sum(d["acc"][s]["n"] for d in per_day)
        sm = sum(d["acc"][s]["sum"] for d in per_day)
        sq = sum(d["acc"][s]["sumsq"] for d in per_day)
        hist: dict[int, int] = {}
        for d in per_day:
            for k, v in d["acc"][s]["hist"].items():
                hist[int(k)] = hist.get(int(k), 0) + v
        mean = sm / n if n else 0.0
        sd = math.sqrt(max(sq / n - mean * mean, 0.0)) if n else 0.0
        med = None
        if n:
            run = 0
            for k in sorted(hist):
                run += hist[k]
                if run >= n / 2:
                    med = k * HIST_BPS
                    break
        out["expected_hedge_move_bps"][s] = mean
        out["stats"][s] = {"n": n, "mean_bps": mean, "median_bps": med, "stdev_bps": sd, "se_bps": sd / math.sqrt(n) if n else None}
    return out


def _main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", required=True); ap.add_argument("--pair", required=True); ap.add_argument("--days", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--maker-venue", default="bybit"); ap.add_argument("--hedge-venue", default="binance")
    ap.add_argument("--horizon-s", type=float, default=1.0); ap.add_argument("--max-book-age-s", type=float, default=2.0)
    ap.add_argument("--jobs", type=int, default=1); ap.add_argument("--day-file", default=None, help="internal: compute one day and write its raw stats here")
    ap.add_argument("--labels-out", default=None, help="S1: write the per-print LABEL ROWS of the first day in --days to this JSON file and stop")
    ap.add_argument("--label-horizons", default="1,5,30", help="S1: the horizons in seconds of the label rows (e.g. 0.05,0.15,1,5,30)")
    ap.add_argument("--hedge-study-out", default=None, help="S2: run the hedge event study for the first day in --days and write its events to this JSON file")
    ap.add_argument("--profile", default="perp_perp_maker_taker_v1_6", help="S2: the profile whose entry rule, fees and retry rule the study uses")
    ap.add_argument("--d-send-s", type=float, default=0.045, help="S2: hedge dispatch-to-arrival delay (ASSUMED)")
    a = ap.parse_args()
    if a.hedge_study_out:
        Path(a.hedge_study_out).write_text(json.dumps(day_hedge_study(a.data_root, a.pair, a.days.split(",")[0], a.profile, a.maker_venue, a.hedge_venue, d_send_s=a.d_send_s, d_report_s=a.d_send_s, max_book_age_s=a.max_book_age_s)))
        return
    if a.labels_out:
        Path(a.labels_out).write_text(json.dumps(day_labels(a.data_root, a.pair, a.days.split(",")[0], a.maker_venue, a.hedge_venue, horizons=tuple(float(x) for x in a.label_horizons.split(",")), max_book_age_s=a.max_book_age_s)))
        return
    days = [d for d in a.days.split(",") if d]
    if a.day_file:
        Path(a.day_file).write_text(json.dumps(day_stats(a.data_root, a.pair, days[0], a.maker_venue, a.hedge_venue, a.horizon_s, a.max_book_age_s)))
        return
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    work = out.with_suffix(".days"); work.mkdir(exist_ok=True)
    running, pending = {}, [d for d in days if not (work / f"{d}.json").exists()]
    while pending or running:
        for d, p in list(running.items()):
            if p.poll() is not None:
                if p.returncode:
                    raise SystemExit(f"day {d} failed rc={p.returncode}")
                del running[d]
        while pending and len(running) < max(1, a.jobs):
            d = pending.pop(0)
            args = [sys.executable, "-m", "new_backtest.market_hedge_move", "--data-root", a.data_root, "--pair", a.pair, "--days", d, "--out", str(out),
                    "--maker-venue", a.maker_venue, "--hedge-venue", a.hedge_venue, "--horizon-s", str(a.horizon_s), "--max-book-age-s", str(a.max_book_age_s),
                    "--day-file", str(work / f"{d}.json.part")]
            running[d] = subprocess.Popen(args, env={**__import__("os").environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
        time.sleep(2)
        for d in days:
            part = work / f"{d}.json.part"
            if part.exists() and d not in running:
                part.rename(work / f"{d}.json")
    per_day = [json.loads((work / f"{d}.json").read_text()) for d in days]
    out.write_text(json.dumps(combine(per_day, a.horizon_s, a.maker_venue, a.hedge_venue, a.pair, a.data_root), indent=1))
    print(json.dumps(json.loads(out.read_text())["stats"]))


if __name__ == "__main__":
    _main()
