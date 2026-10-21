"""Cross-venue arbitrage replay engine.

Event loop, and the one ordering rule that makes it honest:

    for ev in receive_time_ordered_feed:
        clock.set_now(ev.receive_ts)
        release_in_flight_orders(now)      # fill against the book AS IT IS NOW
        apply_event_to_book(ev)
        if touch_changed: evaluate_strategy()

Orders are never filled at the price that triggered them. A signal computed at
t fires legs that arrive at t + entry_latency, and they execute against whatever
the book holds at arrival. When the edge has closed in that window the order
misses, or fills worse. That gap — measured per signal as expected vs realised
edge — is the difference between a cross-exchange arb backtest and a fantasy.

PnL is tracked as cash per venue plus base inventory per venue, marked at the
final mid. Attribution splits the result into:

    gross edge   what the matched legs actually earned
    fees         maker/taker, sign-correct (negative = rebate earned)
    slippage     realised fill VWAP vs the expected price
    leg risk     cost of flattening a signal where one leg filled and one missed
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from . import virtual_clock
from .arb_strategy import (
    MAKER_TAKER, TAKER_TAKER, ArbConfig, ArbLeg, ArbSignal,
    CrossVenueArbStrategy, InventoryBook,
)
from .config import (
    BINANCE, BYBIT, DEFAULT_END_DATE, DEFAULT_LATENCY_PRESET, DEFAULT_START_DATE,
    LATENCY_PRESETS, VenueSpec, make_venues, max_position_for,
)
from .cross_venue_feed import CrossVenueFeed, check_coverage
from .event_loader import BOOK, DELTA, TRADE, delta_fields, snapshot_levels, trade_fields
from .fill_engine import Fill, MakerConfig, MakerExecutor, TakerExecutor
from .fill_model import EmpiricalFillModel
from .latency_model import InFlightOrder, LatencyBook, MarkoutTracker, PendingOrderQueue
from .models import Side
from .order_book import OrderBook
from .venue_state import VenueState, VenueStateConfig


@dataclass
class ArbBacktestConfig:
    pair: str = "BTCUSDT"
    data_root: str = "data"
    venues: tuple[str, ...] = (BYBIT, BINANCE)
    start_date: str = DEFAULT_START_DATE
    end_date: str = DEFAULT_END_DATE
    max_events: int | None = None

    latency_preset: str = DEFAULT_LATENCY_PRESET
    bybit_fee_preset: str = "bybit_spot_mm"
    binance_fee_preset: str = "binance_perp_vip0"

    arb: ArbConfig = field(default_factory=ArbConfig)
    maker: MakerConfig = field(default_factory=MakerConfig)
    venue_state: VenueStateConfig = field(default_factory=VenueStateConfig)

    consume_liquidity: bool = True
    include_deltas: bool = True
    seed: int = 7

    output_dir: str | None = None
    warm_start_fill_events: str | None = None
    markout_horizons_s: tuple[float, ...] = (1.0, 5.0, 30.0)
    progress_every: int = 0          # print every N events; 0 = silent
    max_trade_log: int = 200_000     # cap in-memory fill log on long runs


@dataclass
class ArbBacktestResult:
    pair: str
    venues: list[str]
    start_date: str
    end_date: str
    mode: str
    latency_preset: str

    events_processed: int
    events_by_venue: dict[str, int]
    events_by_kind: dict[str, int]
    wall_seconds: float

    signals_generated: int
    signals_completed: int
    signals_fully_hedged: int
    signals_unhedged: int
    signals_both_missed: int
    orders_sent: int
    orders_filled: int
    orders_missed: int

    gross_pnl: float
    fees_paid: float
    unhedged_loss: float
    realized_pnl: float
    mark_to_market_pnl: float

    # Gross-to-gross: what share of the spread visible at fire time did the
    # matched legs actually collect. Latency and slippage push it below 1.
    mean_expected_gross_edge_bps: float
    mean_realized_gross_edge_bps: float
    edge_capture_ratio: float
    # After the fees actually charged on the matched legs.
    mean_realized_net_edge_bps: float
    mean_slippage_bps: float

    final_inventory: dict[str, float]
    final_cash: dict[str, float]
    final_mids: dict[str, float]
    net_base_position: float
    traded_notional: float

    strategy_report: dict[str, float]
    feed_report: dict[str, dict[str, float]]
    latency_report: dict[str, dict[str, float]]
    book_stats: dict[str, dict[str, float]]
    maker_stats: dict[str, float]
    markout_summary: dict[str, dict[str, float]]
    miss_reasons: dict[str, int]
    coverage: dict[str, list[str]]

    fill_log_path: str | None = None
    bucket_stats_path: str | None = None
    trades_path: str | None = None


@dataclass
class _SignalTracker:
    """Resolves a signal once every leg has either filled or missed."""
    signal: ArbSignal
    legs_outstanding: int
    buy_qty: float = 0.0
    sell_qty: float = 0.0
    buy_notional: float = 0.0
    sell_notional: float = 0.0
    fees: float = 0.0
    missed: int = 0
    hedged_qty: float = 0.0   # cumulative qty for which a hedge leg was dispatched
    maker_resting: bool = False


class CrossVenueArbBacktester:
    def __init__(self, config: ArbBacktestConfig) -> None:
        self.config = config
        all_specs = make_venues(
            config.latency_preset, config.bybit_fee_preset, config.binance_fee_preset,
        )
        self.venue_specs: dict[str, VenueSpec] = {v: all_specs[v] for v in config.venues}

        self.latency = LatencyBook(
            {v: s.latency for v, s in self.venue_specs.items()}, seed=config.seed,
        )
        self.books: dict[str, OrderBook] = {
            v: OrderBook(v, config.pair, self.venue_specs[v].delta_semantics)
            for v in config.venues
        }
        self.states: dict[str, VenueState] = {
            v: VenueState(v, config.venue_state) for v in config.venues
        }
        self.tick_size: dict[str, float] = {v: 0.0 for v in config.venues}

        self.fill_model = EmpiricalFillModel()
        if config.warm_start_fill_events:
            self.fill_model.load_event_log(config.warm_start_fill_events)

        if config.arb.max_position_base <= 0:
            config.arb.max_position_base = max_position_for(config.pair)

        self.strategy = CrossVenueArbStrategy(config.pair, self.venue_specs, config.arb)
        self.taker = TakerExecutor(consume_liquidity=config.consume_liquidity)
        self.maker = MakerExecutor(self.fill_model, config.maker)
        self.pending = PendingOrderQueue()
        self.markouts = MarkoutTracker(config.markout_horizons_s)
        self.inventory = InventoryBook()
        self.inventory.ensure(config.venues)

        self._order_ids = 0
        self._trackers: dict[int, _SignalTracker] = {}
        self._maker_to_signal: dict[int, int] = {}
        self._last_touch: dict[str, tuple[float, float]] = {v: (0.0, 0.0) for v in config.venues}

        self.events_processed = 0
        self.events_by_venue: Counter[str] = Counter()
        self.events_by_kind: Counter[str] = Counter()
        self.orders_sent = 0
        self.orders_filled = 0
        self.orders_missed = 0
        self.miss_reasons: Counter[str] = Counter()
        self.signals_completed = 0
        self.signals_fully_hedged = 0
        self.signals_unhedged = 0
        self.signals_both_missed = 0
        self.fees_paid = 0.0
        self.gross_pnl = 0.0
        self.unhedged_loss = 0.0
        self.traded_notional = 0.0
        self.expected_gross_sum = 0.0
        self.realized_gross_sum = 0.0
        self.realized_net_sum = 0.0
        self.edge_samples = 0
        self.slippage_sum = 0.0
        self.slippage_count = 0
        self.trade_log: list[dict] = []

    # ── Event application ────────────────────────────────────────────────────

    def _apply_event(self, ev) -> None:
        book = self.books[ev.venue]
        state = self.states[ev.venue]
        spec = self.venue_specs[ev.venue]
        now = ev.receive_ts

        if ev.kind == BOOK:
            bids, asks = snapshot_levels(ev.payload)
            if not bids or not asks:
                return
            if self.tick_size[ev.venue] <= 0:
                self.tick_size[ev.venue] = _infer_tick(bids, asks)
            book.apply_snapshot(bids, asks, now)
            state.on_book(book, now)
            self.maker.mark_adverse(ev.venue, book, now, self.tick_size[ev.venue])
            for f in self.maker.on_snapshot(
                ev.venue, book, now, self.tick_size[ev.venue], spec.fees.maker_bps
            ):
                self._on_fill(f)

        elif ev.kind == DELTA:
            side, price, volume, event_type = delta_fields(ev.payload)
            if price <= 0:
                return
            book.apply_delta(side, price, volume, event_type, now)
            state.on_book(book, now)
            for f in self.maker.on_delta(
                ev.venue, side, price, volume, now,
                self.tick_size[ev.venue], spec.fees.maker_bps,
            ):
                self._on_fill(f)

        elif ev.kind == TRADE:
            price, volume, sign = trade_fields(ev.payload)
            if price <= 0 or volume <= 0:
                return
            book.on_trade(price, volume, sign, now)
            state.on_trade(now, price, volume, sign)
            for f in self.maker.on_trade(
                ev.venue, price, volume, sign, now, spec.fees.maker_bps
            ):
                self._on_fill(f)

    def _touch_changed(self, venue: str) -> bool:
        book = self.books[venue]
        if not book.ready:
            return False
        touch = (book.best_bid, book.best_ask)
        if touch != self._last_touch[venue]:
            self._last_touch[venue] = touch
            return True
        return False

    # ── Order dispatch ───────────────────────────────────────────────────────

    def _send_leg(self, leg: ArbLeg, signal: ArbSignal, now: float) -> None:
        self._order_ids += 1
        self.pending.enqueue(InFlightOrder(
            release_ts=now + self.latency.entry_delay(leg.venue),
            order_id=self._order_ids,
            venue=leg.venue,
            side=leg.side,
            qty=leg.qty,
            limit_price=leg.limit_price,
            is_taker=leg.is_taker,
            sent_ts=now,
            signal_id=signal.signal_id,
            expected_price=leg.expected_price,
            expected_edge_bps=signal.net_edge_bps,
        ))
        self.orders_sent += 1

    def _fire(self, signal: ArbSignal, now: float) -> None:
        self._trackers[signal.signal_id] = _SignalTracker(signal, len(signal.legs))
        for leg in signal.legs:
            self._send_leg(leg, signal, now)

    def _execute_arrival(self, order: InFlightOrder, now: float) -> None:
        """An in-flight order reached the exchange. Fill against the live book."""
        tracker = self._trackers.get(order.signal_id)
        book = self.books[order.venue]
        spec = self.venue_specs[order.venue]

        if order.is_taker:
            outcome = self.taker.execute(
                book=book, side=order.side, qty=order.qty,
                limit_price=order.limit_price, fee_bps=spec.fees.taker_bps,
                now=now, expected_price=order.expected_price,
                signal_id=order.signal_id,
            )
            if outcome.filled and outcome.fill is not None:
                self.orders_filled += 1
                self._on_fill(outcome.fill, tracker)
            else:
                self.orders_missed += 1
                self.miss_reasons[outcome.reason] += 1
                if tracker is not None:
                    tracker.missed += 1
            if tracker is not None:
                tracker.legs_outstanding -= 1
                self._maybe_resolve(order.signal_id, now)
            return

        # Passive leg joins the book and waits for the market to come to it.
        if not book.ready:
            self.orders_missed += 1
            self.miss_reasons["not_ready"] += 1
            if tracker is not None:
                tracker.legs_outstanding -= 1
                self._maybe_resolve(order.signal_id, now)
            return

        state = self.states[order.venue]
        maker_order = self.maker.place(
            venue=order.venue, pair=self.config.pair, symbol=self.config.pair,
            side=Side.BID if order.side == "buy" else Side.ASK,
            price=order.limit_price, size=order.qty, book=book,
            state=state.to_market_state(book, now),
            regime_bucket=state.regime().bucket, now=now, active_ts=now,
            tick_size=self.tick_size[order.venue], signal_id=order.signal_id,
        )
        self._maker_to_signal[maker_order.order_id] = order.signal_id
        if tracker is not None:
            tracker.legs_outstanding -= 1
            tracker.maker_resting = True

    # ── Fill handling ────────────────────────────────────────────────────────

    def _on_fill(self, fill: Fill, tracker: _SignalTracker | None = None) -> None:
        if tracker is None:
            tracker = self._trackers.get(fill.signal_id)

        self.inventory.apply(fill.venue, fill.signed_qty, fill.cash_delta)
        self.fees_paid += fill.fee
        self.traded_notional += fill.notional
        self.markouts.record_fill(fill.ts, fill.venue, fill.side, fill.price, fill.qty)

        if fill.slippage_bps:
            self.slippage_sum += fill.slippage_bps
            self.slippage_count += 1

        if tracker is not None:
            if fill.side == "buy":
                tracker.buy_qty += fill.qty
                tracker.buy_notional += fill.notional
            else:
                tracker.sell_qty += fill.qty
                tracker.sell_notional += fill.notional
            tracker.fees += fill.fee

            # Passive leg filled: fire the hedge now, not before. Sending it
            # earlier would leave a naked position against an uncertain fill.
            sig = tracker.signal
            if sig.mode == MAKER_TAKER and not fill.is_taker:
                hedge = self.strategy.hedge_leg(sig, fill.qty, self.books[sig.sell_venue])
                if hedge is not None:
                    tracker.hedged_qty += fill.qty
                    tracker.legs_outstanding += 1
                    self._send_leg(hedge, sig, fill.ts)
                # maker_resting stays True until cancel_all clears it

        if len(self.trade_log) < self.config.max_trade_log:
            self.trade_log.append({
                "ts": fill.ts, "venue": fill.venue, "side": fill.side,
                "qty": fill.qty, "price": fill.price, "fee": fill.fee,
                "taker": fill.is_taker, "signal_id": fill.signal_id,
                "slippage_bps": fill.slippage_bps,
            })

    def _maybe_resolve(self, signal_id: int, now: float) -> None:
        tracker = self._trackers.get(signal_id)
        if tracker is None or tracker.legs_outstanding > 0:
            return
        # A maker signal whose passive leg is still resting is not finished.
        if tracker.maker_resting:
            return

        self._trackers.pop(signal_id, None)
        self.signals_completed += 1

        matched = min(tracker.buy_qty, tracker.sell_qty)
        imbalance = tracker.buy_qty - tracker.sell_qty

        if matched > 0:
            buy_vwap = tracker.buy_notional / max(tracker.buy_qty, 1e-12)
            sell_vwap = tracker.sell_notional / max(tracker.sell_qty, 1e-12)
            self.gross_pnl += (sell_vwap - buy_vwap) * matched
            if buy_vwap > 0:
                gross_bps = (sell_vwap - buy_vwap) / buy_vwap * 10_000.0
                fee_bps = tracker.fees / max(matched * buy_vwap, 1e-12) * 10_000.0
                self.realized_gross_sum += gross_bps
                self.realized_net_sum += gross_bps - fee_bps
                self.expected_gross_sum += tracker.signal.gross_edge_bps
                self.edge_samples += 1

        if abs(imbalance) > 1e-9:
            self.signals_unhedged += 1
            self._unwind(tracker, imbalance, now)
        elif matched > 0:
            self.signals_fully_hedged += 1
        else:
            # Both legs missed: the edge closed during flight on both venues.
            self.signals_both_missed += 1

    def _unwind(self, tracker: _SignalTracker, imbalance: float, now: float) -> None:
        """Flatten the leftover from a half-filled signal at market.

        Charged to unhedged_loss rather than buried in PnL: leg risk is the
        characteristic failure mode of cross-exchange arb and belongs on its own
        line. The unwind uses an unbounded limit because it must complete —
        carrying the exposure forward would silently turn an arb backtest into
        a directional one.
        """
        sig = tracker.signal
        # imbalance = buy_qty - sell_qty; positive → long on buy_venue → sell there
        venue = sig.buy_venue if imbalance > 0 else sig.sell_venue
        book = self.books[venue]
        if not book.ready:
            return
        side = "sell" if imbalance > 0 else "buy"
        qty = abs(imbalance)
        ref = book.mid

        outcome = self.taker.execute(
            book=book, side=side, qty=qty,
            limit_price=float("inf") if side == "buy" else 0.0,
            fee_bps=self.venue_specs[venue].fees.taker_bps,
            now=now, expected_price=ref, signal_id=sig.signal_id,
        )
        if outcome.filled and outcome.fill is not None:
            self.orders_filled += 1
            f = outcome.fill
            self.unhedged_loss += abs(f.price - ref) * f.qty  # fee goes to fees_paid via _on_fill
            self._on_fill(f)

    # ── Main loop ────────────────────────────────────────────────────────────

    def run(self) -> ArbBacktestResult:
        cfg = self.config
        virtual_clock.install_virtual_clock(0.0)
        virtual_clock.reset(0.0)
        wall_start = virtual_clock.real_time()

        coverage = check_coverage(
            cfg.data_root, cfg.pair, cfg.venues, cfg.start_date, cfg.end_date,
        )
        feed = CrossVenueFeed(
            data_root=cfg.data_root, pair=cfg.pair, venues=cfg.venues,
            start_day=cfg.start_date, end_day=cfg.end_date, latency=self.latency,
            trust_local_ts={v: s.trust_local_ts for v, s in self.venue_specs.items()},
            include_deltas=cfg.include_deltas,
        )

        now = 0.0
        try:
            for ev in feed:
                if cfg.max_events is not None and self.events_processed >= cfg.max_events:
                    break
                now = ev.receive_ts
                virtual_clock.set_now(now)
                self.events_processed += 1
                self.events_by_venue[ev.venue] += 1
                self.events_by_kind[ev.kind] += 1

                # In-flight orders land before the current event is applied: an order
                # sent at t-latency cannot see the book update arriving right now.
                for order in self.pending.release(now):
                    self._execute_arrival(order, now)

                self._apply_event(ev)

                # Trades can move the touch too, via trade-tape repair.
                if self._touch_changed(ev.venue):
                    self.markouts.resolve(now, {v: b.mid for v, b in self.books.items()})
                    signal = self.strategy.evaluate(self.books, self.states, self.inventory, now)
                    if signal is not None:
                        self._fire(signal, now)

                if cfg.progress_every and self.events_processed % cfg.progress_every == 0:
                    print(
                        f"  {self.events_processed:>12,} ev  "
                        f"sig={self.strategy.signals:<7,} fill={self.orders_filled:<7,} "
                        f"miss={self.orders_missed:<7,} "
                        f"pnl={self.gross_pnl - self.fees_paid - self.unhedged_loss:,.2f}",
                        flush=True,
                    )

            # Drain: everything in flight lands, resting orders are cancelled.
            for order in self.pending.drain():
                self._execute_arrival(order, now)
            for mo in self.maker.cancel_all(now):
                sid = self._maker_to_signal.get(mo.order_id, -1)
                t = self._trackers.get(sid)
                if t is not None:
                    t.maker_resting = False
                self._maybe_resolve(sid, now)
            for sid in list(self._trackers):
                self._trackers[sid].legs_outstanding = 0
                self._trackers[sid].maker_resting = False
                self._maybe_resolve(sid, now)

            self.markouts.resolve(now, {v: b.mid for v, b in self.books.items()}, finalize=True)
            return self._build_result(feed, coverage, virtual_clock.real_time() - wall_start)
        finally:
            virtual_clock.uninstall()

    # ── Result ───────────────────────────────────────────────────────────────

    def _build_result(self, feed: CrossVenueFeed, coverage, wall_s: float) -> ArbBacktestResult:
        cfg = self.config
        mids = {v: b.mid for v, b in self.books.items()}
        mtm = self.inventory.total_cash + sum(
            self.inventory.base.get(v, 0.0) * mids.get(v, 0.0) for v in cfg.venues
        )

        n = max(1, self.edge_samples)
        mean_exp = self.expected_gross_sum / n
        mean_real = self.realized_gross_sum / n
        mean_net = self.realized_net_sum / n
        capture = (mean_real / mean_exp) if abs(mean_exp) > 1e-12 else 0.0
        mean_slip = self.slippage_sum / self.slippage_count if self.slippage_count else 0.0

        fill_log_path = bucket_stats_path = trades_path = None
        if cfg.output_dir:
            out = Path(cfg.output_dir)
            out.mkdir(parents=True, exist_ok=True)
            fill_log_path = str(out / "fill_events.jsonl")
            bucket_stats_path = str(out / "fill_bucket_stats.json")
            trades_path = str(out / "trades.jsonl")
            self.fill_model.save_event_log(fill_log_path)
            self.fill_model.save_bucket_stats(bucket_stats_path)
            with open(trades_path, "w", encoding="utf-8") as fh:
                for row in self.trade_log:
                    fh.write(json.dumps(row, sort_keys=True) + "\n")

        return ArbBacktestResult(
            pair=cfg.pair, venues=list(cfg.venues),
            start_date=cfg.start_date, end_date=cfg.end_date,
            mode=cfg.arb.mode, latency_preset=cfg.latency_preset,
            events_processed=self.events_processed,
            events_by_venue=dict(self.events_by_venue),
            events_by_kind=dict(self.events_by_kind),
            wall_seconds=wall_s,
            signals_generated=self.strategy.signals,
            signals_completed=self.signals_completed,
            signals_fully_hedged=self.signals_fully_hedged,
            signals_unhedged=self.signals_unhedged,
            signals_both_missed=self.signals_both_missed,
            orders_sent=self.orders_sent,
            orders_filled=self.orders_filled,
            orders_missed=self.orders_missed,
            gross_pnl=self.gross_pnl,
            fees_paid=self.fees_paid,
            unhedged_loss=self.unhedged_loss,
            realized_pnl=self.gross_pnl - self.fees_paid - self.unhedged_loss,
            mark_to_market_pnl=mtm,
            mean_expected_gross_edge_bps=mean_exp,
            mean_realized_gross_edge_bps=mean_real,
            edge_capture_ratio=capture,
            mean_realized_net_edge_bps=mean_net,
            mean_slippage_bps=mean_slip,
            final_inventory=dict(self.inventory.base),
            final_cash=dict(self.inventory.cash),
            final_mids=mids,
            net_base_position=self.inventory.net_base,
            traded_notional=self.traded_notional,
            strategy_report=self.strategy.report(),
            feed_report=feed.report(),
            latency_report=self.latency.report(),
            book_stats={v: b.stats() for v, b in self.books.items()},
            maker_stats=self.maker.stats(),
            markout_summary=self.markouts.summary(),
            miss_reasons=dict(self.miss_reasons),
            coverage={k: list(v) for k, v in coverage.items()},
            fill_log_path=fill_log_path,
            bucket_stats_path=bucket_stats_path,
            trades_path=trades_path,
        )


def _infer_tick(bids, asks) -> float:
    prices = sorted({float(l[0]) for l in list(bids) + list(asks) if len(l) >= 2})
    diffs = [b - a for a, b in zip(prices, prices[1:]) if b > a]
    if diffs:
        return min(diffs)
    return (prices[0] if prices else 1.0) * 1e-6


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Cross-venue (Bybit x Binance) arbitrage backtester",
    )
    p.add_argument("--pair", default="BTCUSDT")
    p.add_argument("--data-root", default="data")
    p.add_argument("--start", dest="start_date", default=DEFAULT_START_DATE)
    p.add_argument("--end", dest="end_date", default=DEFAULT_END_DATE)
    p.add_argument("--venues", nargs="+", default=[BYBIT, BINANCE])
    p.add_argument("--max-events", type=int)
    p.add_argument("--mode", choices=[TAKER_TAKER, MAKER_TAKER], default=TAKER_TAKER)
    p.add_argument("--latency-preset", choices=sorted(LATENCY_PRESETS),
                   default=DEFAULT_LATENCY_PRESET)
    p.add_argument("--bybit-fees", default="bybit_spot_mm")
    p.add_argument("--binance-fees", default="binance_perp_vip0")
    p.add_argument("--min-edge-bps", type=float, default=1.0)
    p.add_argument("--safety-buffer-bps", type=float, default=0.5)
    p.add_argument("--max-book-age-ms", type=float, default=500.0)
    p.add_argument("--max-touch-age-ms", type=float, default=300.0,
                   help="refuse to hit a best level not confirmed within this window")
    p.add_argument("--max-walk-bps", type=float, default=2.0)
    p.add_argument("--max-clip-notional", type=float, default=25_000.0)
    p.add_argument("--max-position-base", type=float, default=0.0,
                   help="0 => per-pair default from config.MAX_POSITION_BASE")
    p.add_argument("--no-toxic-gate", action="store_true")
    p.add_argument("--no-consume", action="store_true",
                   help="do not remove liquidity our own taker orders consumed")
    p.add_argument("--no-deltas", action="store_true",
                   help="snapshots only — much faster, much coarser")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--output-dir")
    p.add_argument("--warm-start", dest="warm_start_fill_events")
    p.add_argument("--progress-every", type=int, default=0)
    return p


def config_from_args(args) -> ArbBacktestConfig:
    arb = ArbConfig(
        mode=args.mode,
        min_net_edge_bps=args.min_edge_bps,
        safety_buffer_bps=args.safety_buffer_bps,
        max_book_age_s=args.max_book_age_ms / 1000.0,
        max_touch_age_s=args.max_touch_age_ms / 1000.0,
        max_walk_bps=args.max_walk_bps,
        max_clip_notional=args.max_clip_notional,
        max_position_base=args.max_position_base,
        block_on_toxic=not args.no_toxic_gate,
    )
    return ArbBacktestConfig(
        pair=args.pair, data_root=args.data_root, venues=tuple(args.venues),
        start_date=args.start_date, end_date=args.end_date,
        max_events=args.max_events, latency_preset=args.latency_preset,
        bybit_fee_preset=args.bybit_fees, binance_fee_preset=args.binance_fees,
        arb=arb, consume_liquidity=not args.no_consume,
        include_deltas=not args.no_deltas, seed=args.seed,
        output_dir=args.output_dir,
        warm_start_fill_events=args.warm_start_fill_events,
        progress_every=args.progress_every,
    )


def main(argv: Iterable[str] | None = None) -> int:
    args = build_arg_parser().parse_args(list(argv) if argv is not None else None)
    cfg = config_from_args(args)
    result = CrossVenueArbBacktester(cfg).run()
    payload = json.dumps(asdict(result), indent=2, sort_keys=True, default=str)
    print(payload)
    if cfg.output_dir:
        Path(cfg.output_dir, "result.json").write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
