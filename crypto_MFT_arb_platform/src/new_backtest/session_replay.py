"""Session replay (Phase D, step D5): the replay driver and result adapter for `runtime: session`.

The arb strategy runs on the core `TradingSession` behind the `QueueAwareVenueAdapter`, fed by the canonical feed converter. This file only DRIVES (feed, per-venue market state,
adapters, result files); every rule it uses lives in `crypto_stat_arb` (config from the profile, the session, the gate, the invalid-book policy, the queue adapter, the strategy
shim). Its replay loop retires with the backtester's in Phase F.

Order of work for one market event (the same for every venue):
  1. a book whose resync wait is over is resynced FIRST (session.resync_if_due), from the feed's own state at that time;
  2. the core book applies the event;
  3. the per-venue float `VenueState` (owned here, shared with the strategy and the adapters) reads the float mirror of the core book;
  4. `TradingSession.on_event(apply_book=False)`: gate, OMS, strategy, dispatch.
A trade print reaches the venue state first and then the session's trade hook (queue fills come from prints only).

Differences from the backtester's loop, recorded in the manifest (nothing here pretends to be the same loop): no timing timelines or venue-truth books (the core latency model draws the
order latencies), no maker-lifetime timer, hedge faults, breaker, requote or margin step of the backtester (the core OMS, risk engine and book gate replace them), a book
that ends a message crossed is invalidated by the core (the backtester repairs it), and the sequence numbers are SYNTHETIC. Parity is explained, not forced (prompt, PHASE D).
"""
from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from crypto_stat_arb.config import config_from_raw, raw_config_from_profile
from crypto_stat_arb.execution import DispatchPolicy
from crypto_stat_arb.hedge_lifecycle import HedgePolicy
from crypto_stat_arb.models import EventType, MarketEvent, TradeEvent
from crypto_stat_arb.session import ExecutionMode, TradingSession
from crypto_stat_arb.simulation.order_book import float_mirror
from crypto_stat_arb.simulation.queue_adapter import QueueAwareVenueAdapter
from crypto_stat_arb.simulation.venue_state import VenueState
from crypto_stat_arb.strategies.arb_strategy import ArbSessionStrategy, CrossVenueArbStrategy

from .canonical_feed import CanonicalFeed
from .config import core_fee_specs, make_venues
from .cross_venue_feed import CrossVenueFeed
from .backtester import _preflight_override
from .fill_engine import MakerExecutor
from .fill_model import EmpiricalFillModel
from .latency_model import LatencyBook
from .timing import FixedOffsetAligner, Timelines, TimelinesLatencyModel, TruthBooks
from collections import Counter
from .event_loader import BOOK, DELTA, delta_fields, snapshot_levels
from .order_book import OrderBook

# The dated price grid per dataset (the Phase B data checks' ticks) and the quantity step. The step is ASSUMED: no instrument master is stored for these datasets (lot NOT_MEASURED).
DATED_TICKS = {"2022": {"bybit": 0.005, "binance": 0.001}, "2024": {"bybit": 0.001, "binance": 0.001}}
QUANTITY_STEP = {"bybit": 0.1, "binance": 0.01}
_NS = 1_000_000_000


def dated_ticks(start_date: str) -> dict[str, float]:
    return dict(DATED_TICKS[start_date[:4]])


class SessionReplay:
    """One replay on the session. `stream` is any iterable of `TimedEvent` built with `decimal_numbers=True` (the real feed or a synthetic list); consumed lazily."""

    def __init__(self, config, profile: dict, *, stream: Iterable | None = None, ticks: dict[str, float] | None = None) -> None:
        if config.runtime != "session":
            raise ValueError("SessionReplay runs the session runtime only")
        self.config, self.profile = config, profile
        venues = tuple(config.venues)
        self.venues = venues
        self.names = {v: v.upper() for v in venues}
        fee_specs, self.fee_rows = core_fee_specs(config.pair, config.bybit_fee_preset, config.binance_fee_preset)
        specs = make_venues(config.latency_preset, config.bybit_fee_preset, config.binance_fee_preset, fee_specs=fee_specs)
        self.venue_specs = {v: specs[v] for v in venues}
        for v, trust in (config.trust_local_ts or {}).items():          # the profile's run-level setting, applied exactly as the backtester does
            if v in self.venue_specs:
                self.venue_specs[v] = replace(self.venue_specs[v], trust_local_ts=bool(trust))
        self.ticks = ticks or dated_ticks(config.start_date)
        size = float(config.arb.max_position_base)
        raw, self.config_notes = raw_config_from_profile(profile, pair=config.pair, venues=venues, ticks=self.ticks, quantity_steps={v: QUANTITY_STEP[v] for v in venues}, size_base=size,
                                                         seed=config.seed)
        self.app_config, self.raw_config = config_from_raw(raw)
        self.instrument_id = self.app_config.instrument_id
        self.feed = CanonicalFeed(self.instrument_id, venue_names=self.names, delta_semantics={v: s.delta_semantics for v, s in self.venue_specs.items()})
        self.states = {v: VenueState(v, config.venue_state) for v in venues}
        self.timing_stats: Counter = Counter()
        self.truth = None
        if config.timing is not None and config.timing.truth_books:       # WP3 truth books, the shared rule (timing.TruthBooks): taker arrivals and PostOnly checks use the venue-truth book at the arrival's exchange time
            truth_books = {v: OrderBook(v, config.pair, self.venue_specs[v].delta_semantics, tape_repair=config.book_tape_repair, own_overlay=config.own_impact == "overlay",
                                        message_atomic=config.message_atomic_book) for v in venues}
            self.truth = TruthBooks(truth_books, self._truth_apply, message_atomic=config.message_atomic_book, aligner_of=lambda: self.latency.aligner, stats=self.timing_stats)
        # The float VIEW books the strategy reads and the maker executor is judged against: persistent, message-atomic, touch ages kept, a cross repaired at message end: the backtester's own books,
        # fed here from the converted events. The core books (below, in the session) serve the core's gates, OMS and risk.
        self.views = {v: OrderBook(v, config.pair, self.venue_specs[v].delta_semantics, tape_repair=config.book_tape_repair, own_overlay=config.own_impact == "overlay",
                                   message_atomic=config.message_atomic_book) for v in venues}
        executor = MakerExecutor(EmpiricalFillModel(), config.maker)
        executor.own_overlay = config.own_impact == "overlay"
        self.adapters = {self.names[v]: QueueAwareVenueAdapter(self.names[v], executor, tick_size=self.ticks[v], maker_fee_bps=self.venue_specs[v].fees.maker_bps, pair=config.pair,
                                                               state=self.states[v], truth=self.truth, feed_venue=v, view_book=(lambda v=v: self.views[v]), driver_fed=True) for v in venues}
        self.resync_delay_s = float(getattr(config, "resync_delay_s", 5.0))
        # Timing is a RULE, the same on both runtimes (owner 9 Nov): the profile's timing block (latency preset, floors, jitter) drives the feed delay, the clock and every order delay.
        self.latency = LatencyBook({v: s.latency for v, s in self.venue_specs.items()}, seed=config.seed)
        self.timelines = Timelines(config.timing) if config.timing is not None else None
        if self.timelines is not None:
            aligner = FixedOffsetAligner(config.timing.clock)          # what the backtester's _install_timing does
            self.latency.aligner = aligner
            for v, vl in self.latency.venues.items():
                vl.aligner = aligner
                vl.feed = self.timelines.sampler(v, "feed")
        self.latency_model = TimelinesLatencyModel(self.timelines) if self.timelines is not None else None
        hf, hedge_cfg = config.hedge_faults, (config.execution.hedge if getattr(config, "execution", None) is not None else {})
        hedge_policy = HedgePolicy(retry_limit=int(getattr(hf, "max_retries", 0) or 0), retry_backoff_ns=int(float(getattr(hf, "retry_backoff_s", 0.0) or 0.0) * _NS),
                                   query_attempt_limit=int(getattr(hf, "unknown_query_max_attempts", 3) or 3),
                                   touch_slack_bps=max(float(config.arb.max_walk_bps), float(hedge_cfg.get("slack_bps_floor", 5.0))) if hedge_cfg.get("cap") == "touch_slack" else None)
        self.session = TradingSession(
            self.app_config, execution_mode=ExecutionMode.INTERNAL_SIMULATION, adapters=self.adapters, strategies=[],
            dispatch_policy=DispatchPolicy.FILL_TRIGGERED, maker_venues=tuple(self.names[v] for v in config.arb.maker_venues),
            resync_delay_ns=int(self.resync_delay_s * _NS), resync_source=self.feed.resync_snapshot, latency_model=self.latency_model, hedge_policy=hedge_policy,
        )
        if config.arb.maker_max_age_s > 0:
            self.session.oms.order_lifetime_ns = int(config.arb.maker_max_age_s * _NS)     # the profile's MANDATORY maker order lifetime (a rule, the same on both runtimes)
        self.strategy = CrossVenueArbStrategy(config.pair, self.venue_specs, config.arb)
        self.shim = ArbSessionStrategy(self.strategy, ledger=self.session.ledger, oms=self.session.oms, states=self.states, instruments=self.app_config.instruments,
                                       instrument_id=self.instrument_id, venue_names=self.names, eligibility_policy=self.session.alignment_policy, view_books=lambda: self.views)
        self.session.strategies.append(self.shim)
        self.session.strategy = self.shim
        self._stream = stream
        self.events = {"book": 0, "trade": 0}
        self.last_ns = 0

    # -- feed --------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _truth_apply(ev, tb) -> None:
        """One raw research event into a venue-truth book (the same reader as the backtester's)."""
        if ev.kind == BOOK:
            bids, asks = snapshot_levels(ev.payload)
            if bids and asks:
                tb.apply_snapshot(bids, asks, getattr(ev, "exchange_ts", None) or ev.receive_ts)
        elif ev.kind == DELTA:
            side, price, volume, event_type = delta_fields(ev.payload)
            if price > 0:
                tb.apply_delta(side, price, volume, event_type, getattr(ev, "exchange_ts", None) or ev.receive_ts)

    def _with_truth(self, raw):
        """Hold a look-ahead window of events per venue for the truth books, and bring the truth book along with every event the view receives (the backtester's `_with_lookahead` and `_truth_sync_event`)."""
        if self.truth is None:
            return raw
        window = max(t.feed.max_ms for t in self.config.timing.venues.values()) / 1000.0

        def synced():
            for ev in self.truth.with_lookahead(raw, window):
                self.truth.sync_event(ev)
                yield ev
        return synced()

    def _timed_stream(self) -> Iterable:
        if self._stream is not None:
            return self._stream
        c = self.config
        return CrossVenueFeed(
            data_root=c.data_root, pair=c.pair, venues=c.venues, start_day=c.start_date, end_day=c.end_date, latency=self.latency,
            trust_local_ts={v: s.trust_local_ts for v, s in self.venue_specs.items()}, include_deltas=c.include_deltas, quality_preflight=True, quality_report_only=_preflight_override(c) is not None,
            trade_dedupe=c.trade_dedupe, event_order=c.event_order, skip_snapshots=tuple(c.skip_snapshot_venues), decimal_numbers=True,
        )

    # -- loop --------------------------------------------------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        limit_ns = None if self.config.max_event_ts is None else int(self.config.max_event_ts * _NS)
        for event in self.feed.convert(self._with_truth(self._timed_stream())):
            if self.config.max_events is not None and sum(self.events.values()) >= self.config.max_events:
                break
            if limit_ns is not None and event.receive_ts_ns > limit_ns:
                break
            self.last_ns = event.receive_ts_ns
            if isinstance(event, TradeEvent):
                self._trade(event)
            else:
                self._book(event)
        self.session.drain()
        return self.result()

    def _feed_venue(self, core_name: str) -> str:
        return next(v for v, n in self.names.items() if n == core_name)

    def _ets(self, exchange_ts_ns: int) -> float | None:
        """When the event executed at the exchange, on the host clock (the backtester's `_event_host_ts`); None until the aligner has a sample."""
        aligner = self.latency.aligner
        return aligner.to_host(exchange_ts_ns / _NS) if aligner.ready else None

    def _deliver(self, adapter, produced, now_ns: int, exchange_ts_ns: int) -> None:
        if produced:
            self.session.router._deliver_resting_fills(adapter.venue, produced, now_ns, exchange_ts_ns=exchange_ts_ns)

    def _book(self, event: MarketEvent) -> None:
        """The backtester's `_apply_event` for a book event: the view book and venue state, then the maker executor (snapshot, or each level of the message, then the cross check), then the core session."""
        now = event.receive_ts_ns
        now_s = now / _NS
        self.events["book"] += 1
        self.session.resync_if_due(now)
        self.session.oms.advance_time(now, self.session.books)         # in-flight orders land and timers fire BEFORE this event is applied (the backtester's _advance_to): an order sent at t - latency never sees an event received at t
        self.session.books[(event.venue, event.instrument_id)].apply(event)
        fv = self._feed_venue(event.venue)
        view, state, adapter, ets = self.views[fv], self.states[fv], self.adapters[event.venue], self._ets(event.exchange_ts_ns)
        produced: list = []
        if event.event_type == EventType.BOOK_SNAPSHOT:
            bids, asks = [[float(l.price), float(l.quantity)] for l in event.bids], [[float(l.price), float(l.quantity)] for l in event.asks]
            if bids and asks:
                view.apply_snapshot(bids, asks, now_s)
                state.on_book(view, now_s)
                produced += adapter.feed_snapshot(view, now_s, ets)
        else:
            levels = [("bid", float(l.price), float(l.quantity)) for l in event.bids] + [("ask", float(l.price), float(l.quantity)) for l in event.asks]
            for side, price, size in levels:
                view.apply_delta(side, price, size, "modify", now_s)
            view.end_message()
            state.on_book(view, now_s)
            for side, price, size in levels:
                produced += adapter.feed_delta(side, price, view.level_size(side, price) if self.config.delta_uses_book_size else size, now_s, ets)
        produced += adapter.feed_cross(view, now_s, ets)
        self._deliver(adapter, produced, now, event.exchange_ts_ns)
        self.session.on_event(event, apply_book=False)

    def _trade(self, trade: TradeEvent) -> None:
        now = trade.receive_ts_ns
        now_s = now / _NS
        price, volume = float(trade.price), float(trade.quantity)
        if price <= 0 or volume <= 0:
            return
        self.events["trade"] += 1
        self.session.oms.advance_time(now, self.session.books)
        sign = 1 if trade.aggressor_side == "BUY" else -1
        fv = self._feed_venue(trade.venue)
        view, state, adapter, ets = self.views[fv], self.states[fv], self.adapters[trade.venue], self._ets(trade.exchange_ts_ns)
        view.on_trade(price, volume, sign, now_s)
        state.on_trade(now_s, price, volume, sign)
        produced = list(adapter.feed_trade(price, volume, sign, now_s, ets)) + list(adapter.feed_cross(view, now_s, ets))
        self._deliver(adapter, produced, now, trade.exchange_ts_ns)

    # -- result ------------------------------------------------------------------------------------------------------------

    def result(self) -> dict[str, Any]:
        s = self.session
        orders, fills = [], []
        for plan in s.oms.plans.values():
            for child in plan.children:
                orders.append({"order_id": child.order_id, "venue": child.venue, "side": child.side.value, "quantity": str(child.quantity), "limit_price": None if child.limit_price is None else str(child.limit_price),
                               "post_only": bool(child.post_only), "state": child.state.value, "filled_quantity": str(child.filled_quantity), "average_price": str(child.average_price), "fee": str(child.fee)})
        for e in s.ledger.events:
            if e.get("event_type") == "FILL":
                fills.append({k: (str(v) if isinstance(v, Decimal) else v) for k, v in e.items()})
        mids = {}
        for (venue, instrument_id), book in s.books.items():
            if book.bids and book.asks:
                mids[venue] = (book.best_bid + book.best_ask) / 2
        positions = {f"{v}:{i}": str(q) for (v, i), q in s.ledger.positions.items()}
        cash = {v: str(c) for v, c in s.ledger.cash_deltas.items()}
        equity = sum((s.ledger.cash_deltas.get(v, Decimal(0)) for v in mids), Decimal(0)) + sum((q * mids[v] for (v, i), q in s.ledger.positions.items() if v in mids), Decimal(0))
        signals = [{"opportunity_id": k, "ts": sig.ts, "buy_venue": sig.buy_venue, "sell_venue": sig.sell_venue, "qty": sig.qty, "buy_price": sig.buy_price, "sell_price": sig.sell_price,
                    "net_edge_bps": sig.net_edge_bps} for k, sig in self.shim.signals.items()]
        return {
            "label": "EXPLORATORY: session runtime, not a result to quote", "runtime": "session", "profile": self.profile.get("name"), "pair": self.config.pair,
            "days": [self.config.start_date, self.config.end_date], "size_base": float(self.config.arb.max_position_base), "fee_presets": self.profile.get("fee_presets"),
            "events": dict(self.events), "signals": signals, "orders": orders, "fills": fills, "positions": positions, "cash_by_venue": cash, "equity_at_book_mids": str(equity),
            "grid_snaps": dict(self.shim.grid_snaps), "fills_by_source": {n: dict(a.fills_by_source) for n, a in self.adapters.items()},
            "manifest": {
                "invalid_book_policy": s.book_policy_report(self.last_ns), "feed": self.feed.manifest(), "ticks": self.ticks, "quantity_step": f"{QUANTITY_STEP} ASSUMED",
                "config_notes": self.config_notes, "timing": {"latency_model": None if self.latency_model is None else self.latency_model.report(), "truth_books": dict(self.timing_stats)}, "release_identity": self.config.release_identity,
                "differences_from_backtester_loop": ["no timing timelines or venue-truth books", "core latency model", "no maker-lifetime timer, hedge faults, breaker, requote, margin step of the backtester",
                                                     "message-end cross invalidates the core book", "sequence SYNTHETIC, gaps NOT_MEASURED"],
            },
        }


def write_result(result: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("signals", "orders", "fills"):
        with (out_dir / f"session_{name}.jsonl").open("w") as fh:
            for row in result[name]:
                fh.write(json.dumps(row, default=str) + "\n")
    (out_dir / "session_manifest.json").write_text(json.dumps({k: v for k, v in result.items() if k not in ("signals", "orders", "fills")}, indent=1, default=str))


# ---- parity (D5): v1_5 on the session against v1_4 on the backtester's loop ----------------------------------------------------------------------------------------------------
# Tolerances, written in the prompt before any run: signals equal in count and within 1 ms; orders equal in count, side and quantity with the limit price within 1 tick of the venue;
# fills equal in count, within 1 ms and 1 tick; P&L within 0.05 USDT per slice at size 1 SOL. A difference above a tolerance is a finding to classify (rounding, BUG_BACKTEST,
# BUG_CORE, BY_DESIGN), never something to tune away. Fills are compared per (venue, side) because the two paths split one fill differently (the backtester reports partial chunks).
TOLERANCES = {"signal_time_s": 1e-3, "fill_time_s": 1e-3, "price_ticks": 1.0, "pnl_usdt": 0.05}


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def _by_leg(fills: list[dict]) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for f in fills:
        leg = out.setdefault((f["venue"], f["side"]), {"qty": 0.0, "notional": 0.0, "first_ts": f["ts"], "last_ts": f["ts"], "rows": 0, "fee": 0.0})
        leg["qty"] += f["qty"]
        leg["notional"] += f["qty"] * f["price"]
        leg["first_ts"], leg["last_ts"] = min(leg["first_ts"], f["ts"]), max(leg["last_ts"], f["ts"])
        leg["rows"] += 1
        leg["fee"] += f.get("fee", 0.0)
    for leg in out.values():
        leg["vwap"] = leg["notional"] / leg["qty"] if leg["qty"] else 0.0
    return out


def old_fills(run_dir: Path) -> list[dict]:
    """The backtester's trades.jsonl as fills: venue, side, qty, price, time (seconds), fee."""
    return [{"venue": r["venue"], "side": r["side"], "qty": float(r["qty"]), "price": float(r["price"]), "ts": float(r["ts"]), "fee": float(r.get("fee", 0.0))} for r in _rows(run_dir / "trades.jsonl")]


def new_fills(run_dir: Path) -> list[dict]:
    """The session's ledger FILL events as fills (venue names lower-cased to the feed's)."""
    out = []
    for e in _rows(run_dir / "session_fills.jsonl"):
        out.append({"venue": str(e["venue"]).lower(), "side": str(e["side"]).lower(), "qty": float(e["fill_quantity"]), "price": float(e["fill_price"]), "ts": int(e["ts_ns"]) / _NS,
                    "fee": float(e.get("fee_delta", e.get("fee", 0.0)) or 0.0)})
    return out


def compare_parity(old_dir: Path, new_dir: Path, ticks: dict[str, float], windows_ns: list[list] | None = None) -> dict:
    """Per (venue, side) leg: quantity, vwap price in ticks, first and last fill time, row counts; the signal count; every difference above a tolerance, each with whether it lies inside a
    suspension window of the session run. Classification is done by a person from this output and recorded in the run manifest (a machine cannot tell BUG_CORE from BY_DESIGN)."""
    o, n = _by_leg(old_fills(old_dir)), _by_leg(new_fills(new_dir))
    windows = [(w[0] / _NS, w[1] / _NS) for w in (windows_ns or [])]
    inside = lambda t: any(a <= t <= b for a, b in windows)
    diffs = []
    for key in sorted(set(o) | set(n)):
        a, b = o.get(key), n.get(key)
        if a is None or b is None:
            diffs.append({"leg": key, "what": "leg present in only one run", "old": a, "new": b})
            continue
        tick = ticks[key[0]]
        if abs(a["qty"] - b["qty"]) > 1e-9:
            diffs.append({"leg": key, "what": "quantity", "old": a["qty"], "new": b["qty"]})
        if abs(a["vwap"] - b["vwap"]) > TOLERANCES["price_ticks"] * tick:
            diffs.append({"leg": key, "what": "price (ticks)", "old": a["vwap"], "new": b["vwap"], "ticks": (b["vwap"] - a["vwap"]) / tick})
        for edge in ("first_ts", "last_ts"):
            if abs(a[edge] - b[edge]) > TOLERANCES["fill_time_s"]:
                diffs.append({"leg": key, "what": f"time {edge}", "old": a[edge], "new": b[edge], "seconds": b[edge] - a[edge], "in_session_window": inside(a[edge]) or inside(b[edge])})
    old_summary = json.loads((old_dir / "summary.json").read_text()) if (old_dir / "summary.json").exists() else {}
    new_summary = json.loads((new_dir / "summary.json").read_text()) if (new_dir / "summary.json").exists() else {}
    return {"tolerances": TOLERANCES, "old_legs": {f"{k[0]}/{k[1]}": v for k, v in o.items()}, "new_legs": {f"{k[0]}/{k[1]}": v for k, v in n.items()},
            "signals": {"old": old_summary.get("signals"), "new": new_summary.get("signals")}, "differences": diffs, "windows": len(windows),
            "classification": "UNCLASSIFIED: to be classified by hand (rounding, BUG_BACKTEST, BUG_CORE, BY_DESIGN) and recorded in the run manifest"}


def order_times_old(events) -> list[dict]:
    """Per order on the backtester's loop, in send order: decision (= send; the decision is the send there), send, arrival (a passive order's `order_resting`, a taker's fill), first and last fill."""
    from types import SimpleNamespace
    rows, by_id = [], {}
    for e in events:
        if isinstance(e, dict):                                     # a row of order_events.jsonl
            e = SimpleNamespace(**e)
        if e.kind == "order_sent":
            row = {"venue": e.venue, "side": e.side, "decision": e.ts, "send": e.ts, "arrival": None, "fills": []}
            by_id[e.order_id] = row
            rows.append(row)
        elif e.kind == "order_resting" and e.order_id in by_id:
            by_id[e.order_id]["arrival"] = e.ts
        elif e.kind == "fill":
            owner = next((r for r in reversed(rows) if r["venue"] == e.venue and r["side"] == e.side and r["send"] <= e.ts), None)     # a fill belongs to the latest earlier order on its venue and side
            if owner is not None:
                owner["fills"].append(e.ts)
    return rows


def order_times_new(replay: "SessionReplay") -> list[dict]:
    """The same table from the session: decision (the opportunity's time), send (ORDER_IN_FLIGHT), arrival (ORDER_ARRIVED), fills (ledger FILL events), in send order."""
    s = replay.session
    decision = {}
    for e in s.ledger.events:
        if e["event_type"] == "OPPORTUNITY":
            decision[e.get("opportunity", {}).get("opportunity_id")] = e["ts_ns"] / _NS
    side = {c.order_id: (c.side.value.lower(), c.opportunity_id) for p in s.oms.plans.values() for c in p.children}
    rows, by_id = [], {}
    for t in s.router.trace:
        if t["record_type"] == "ORDER_IN_FLIGHT" and t["action"] == "SUBMIT":
            sd, opp = side.get(t["order_id"], ("", None))
            row = {"venue": t["venue"].lower(), "side": sd, "decision": decision.get(opp), "send": t["ts_ns"] / _NS, "arrival": t["arrival_ts_ns"] / _NS, "fills": []}
            by_id[t["order_id"]] = row
            rows.append(row)
    for e in s.ledger.events:
        if e["event_type"] == "FILL" and e.get("order_id") in by_id:
            by_id[e["order_id"]]["fills"].append(e["ts_ns"] / _NS)
    first_of: dict = {}
    for t in s.router.trace:                                  # a hedge's decision is the maker fill that triggered it (FILL_TRIGGERED), as on the backtester's loop
        if t["record_type"] == "ORDER_IN_FLIGHT" and t["action"] == "SUBMIT" and t["order_id"] in by_id:
            opp = side.get(t["order_id"], ("", None))[1]
            if opp in first_of and first_of[opp]["fills"]:
                by_id[t["order_id"]]["decision"] = first_of[opp]["fills"][0]
            first_of.setdefault(opp, by_id[t["order_id"]])
    return rows


def compare_order_times(old_rows: list[dict], new_rows: list[dict], tolerance_s: float = 1e-3) -> list[dict]:
    """Order by order (send order): each of decision, send, arrival, first and last fill with its difference and whether it is within the stated tolerance (1 ms)."""
    out = []
    for i in range(max(len(old_rows), len(new_rows))):
        a = old_rows[i] if i < len(old_rows) else None
        b = new_rows[i] if i < len(new_rows) else None
        if a is None or b is None:
            out.append({"order": i, "what": "order present in only one run", "old": a, "new": b})
            continue
        row = {"order": i, "venue": (a["venue"], b["venue"]), "side": (a["side"], b["side"])}
        for name, x, y in (("decision", a["decision"], b["decision"]), ("send", a["send"], b["send"]), ("arrival", a["arrival"] if a["arrival"] is not None else (a["fills"] or [None])[0], b["arrival"]),
                           ("first_fill", (a["fills"] or [None])[0], (b["fills"] or [None])[0]), ("last_fill", (a["fills"] or [None])[-1], (b["fills"] or [None])[-1])):
            row[name] = {"old": x, "new": y, "diff_s": None if x is None or y is None else y - x, "within_tolerance": None if x is None or y is None else abs(y - x) <= tolerance_s}
        out.append(row)
    return out
