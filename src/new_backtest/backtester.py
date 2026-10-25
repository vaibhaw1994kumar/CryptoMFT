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
import warnings

from . import virtual_clock
from .arb_strategy import (
    MAKER_TAKER, TAKER_TAKER, ArbConfig, ArbLeg, ArbSignal,
    CrossVenueArbStrategy, InventoryBook, PendingExposure, validate_maker_lifetime,
)
from .background_agent_model import AgentFlowConfig, BackgroundAgentModel
from .config import (
    BINANCE, BYBIT, DEFAULT_END_DATE, DEFAULT_LATENCY_PRESET, DEFAULT_START_DATE,
    LATENCY_PRESETS, VenueSpec, make_venues, max_position_for,
)
from .cross_venue_feed import CrossVenueFeed, check_coverage
from .event_loader import BOOK, DELTA, TRADE, delta_fields, snapshot_levels, trade_fields
from .fill_engine import Fill, MakerConfig, MakerExecutor, MakerOrder, TakerExecutor
from .fill_model import EmpiricalFillModel
from .latency_model import InFlightOrder, LatencyBook, MarkoutTracker, PendingOrderQueue
from .models import Side
from .order_book import OrderBook
from .venue_state import VenueState, VenueStateConfig


CLOCK_OFFSET_WARN_MS = 5.0   # a venue whose receive times average this far before its exchange stamps is flagged


@dataclass
class ArbBacktestConfig:
    pair: str = "BTCUSDT"
    data_root: str = "data"
    venues: tuple[str, ...] = (BYBIT, BINANCE)
    start_date: str = DEFAULT_START_DATE
    end_date: str = DEFAULT_END_DATE
    max_events: int | None = None

    latency_preset: str = DEFAULT_LATENCY_PRESET
    bybit_fee_preset: str = "bybit_perp_mm"
    binance_fee_preset: str = "binance_perp_vip0"

    arb: ArbConfig = field(default_factory=ArbConfig)
    maker: MakerConfig = field(default_factory=MakerConfig)
    venue_state: VenueStateConfig = field(default_factory=VenueStateConfig)

    consume_liquidity: bool = True
    include_deltas: bool = True
    seed: int = 7

    agent_flow: AgentFlowConfig = field(default_factory=AgentFlowConfig)

    output_dir: str | None = None
    warm_start_fill_events: str | None = None
    # If set, only load fill-log events whose label has matured by this epoch second.
    # Prevents temporal leakage when the calibration window overlaps the eval window.
    warm_start_until_ts: float | None = None
    # Path to a FillModelV1 JSON checkpoint.  When set, the strategy gates maker
    # quotes on P(fill) >= arb.min_fill_prob.  The model's trained_until_ts must
    # be <= epoch(start_date); the backtester raises ValueError otherwise to
    # prevent forward-looking model use.
    fill_model_v1_path: str | None = None
    markout_horizons_s: tuple[float, ...] = (1.0, 5.0, 30.0)
    progress_every: int = 0          # print every N events; 0 = silent
    max_trade_log: int = 200_000     # cap in-memory fill log on long runs
    # Directory of funding-rate history JSONL files (e.g. data/funding/).
    # When set, perpetual funding payments are settled during replay and
    # reported as funding_paid in ArbBacktestResult.  None = not modelled.
    funding_rate_dir: str | None = None


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
    funding_paid: float
    realized_pnl: float
    open_position_value: float   # A10: unrealised – base inventory marked at end-of-run mids
    ending_equity: float         # A10: total_cash + open_position_value (does not assume zero start)
    mark_to_market_pnl: float    # legacy alias == ending_equity; kept for back-compat

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
    # Maker-order lifetime / cancel accounting (all zero when maker_max_age_s == 0). See _expire_maker_orders.
    maker_lifetime: dict[str, float] = field(default_factory=dict)


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


def _fee_preset_family(preset: str) -> str:
    """Return 'spot', 'perp', or 'unknown' from the preset name."""
    if "_spot_" in preset:
        return "spot"
    if "_perp_" in preset:
        return "perp"
    return "unknown"


class CrossVenueArbBacktester:
    def __init__(self, config: ArbBacktestConfig) -> None:
        self.config = config
        validate_maker_lifetime(config.arb.maker_max_age_s)   # the arb config may have been mutated after construction
        bybit_family = _fee_preset_family(config.bybit_fee_preset)
        binance_family = _fee_preset_family(config.binance_fee_preset)
        if bybit_family != "unknown" and binance_family != "unknown" and bybit_family != binance_family:
            warnings.warn(
                f"Fee preset family mismatch: bybit={config.bybit_fee_preset!r} ({bybit_family}) "
                f"vs binance={config.binance_fee_preset!r} ({binance_family}). "
                "Use matching product types (both spot or both perp) unless intentional.",
                stacklevel=2,
            )
        if config.arb.mode == "maker_taker" and config.maker.min_queue_ahead <= 0.0:
            warnings.warn(
                "MakerConfig.min_queue_ahead is 0 in maker_taker mode: a quote that joins a price level absent from "
                "the book starts at the front of the queue, so fill rates and edge are optimistic. Set it to a "
                "typical level depth for the instrument (--min-queue-ahead).",
                RuntimeWarning, stacklevel=2,
            )
        if config.arb.mode == "maker_taker" and config.arb.maker_max_age_s <= 0.0:
            warnings.warn(
                "ArbConfig.maker_max_age_s is 0 in maker_taker mode: a resting maker order is only ever removed by a "
                "fill, so one that stops being marketable keeps its place in the queue, blocks the live-order limit "
                "and can fill much later at a stale price. The engine has no cancel/replace on price moves "
                "(maker_requote_bps is not used); set maker_max_age_s as a time-in-force.",
                RuntimeWarning, stacklevel=2,
            )
        if (config.arb.mode == "maker_taker" and config.arb.maker_max_live_orders > 1
                and not config.arb.count_pending_exposure):
            warnings.warn(
                f"maker_max_live_orders={config.arb.maker_max_live_orders} with count_pending_exposure=False: position "
                "limits look at filled inventory only, so several resting or in-flight orders that all fill can exceed "
                "max_position_base / the venue imbalance cap. Set ArbConfig.count_pending_exposure=True.",
                RuntimeWarning, stacklevel=2,
            )
        if config.arb.mode == "maker_taker" and not config.arb.maker_count_inflight:
            warnings.warn(
                "ArbConfig.maker_count_inflight is False in maker_taker mode: orders still in flight are not counted "
                "toward maker_max_live_orders, so one signal can be repeated until the first order lands (21 identical "
                "orders in the original audit). Legacy behaviour, kept for comparison only.",
                RuntimeWarning, stacklevel=2,
            )
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
            self.fill_model.load_event_log(
                config.warm_start_fill_events,
                until_ts=config.warm_start_until_ts,
            )

        if config.arb.max_position_base <= 0:
            config.arb.max_position_base = max_position_for(config.pair)

        fill_model_v1 = None
        if config.fill_model_v1_path:
            from .fill_model_v1 import load_fill_model  # noqa: PLC0415
            import datetime as _dt                       # noqa: PLC0415
            fill_model_v1 = load_fill_model(config.fill_model_v1_path)
            # Leakage guard: model must not have been trained on data from the
            # eval window.  Convert start_date "YYYY-MM-DD" to midnight UTC.
            start_epoch = _dt.datetime.fromisoformat(config.start_date).replace(
                tzinfo=_dt.timezone.utc
            ).timestamp()
            if fill_model_v1.trained_until_ts > start_epoch:
                raise ValueError(
                    f"Fill model trained_until_ts={fill_model_v1.trained_until_ts:.0f} "
                    f"is after start_date epoch {start_epoch:.0f} ({config.start_date!r}). "
                    "Using a model trained on future data leaks labels into the backtest."
                )

        self.strategy = CrossVenueArbStrategy(
            config.pair, self.venue_specs, config.arb,
            fill_model_v1=fill_model_v1,
        )
        self.taker = TakerExecutor(consume_liquidity=config.consume_liquidity)
        self.maker = MakerExecutor(self.fill_model, config.maker)
        self.pending = PendingOrderQueue()
        self.markouts = MarkoutTracker(config.markout_horizons_s)
        self.inventory = InventoryBook()
        self.inventory.ensure(config.venues)

        self.settler: "FundingSettler | None" = None
        if config.funding_rate_dir:
            from .funding_settler import FundingSettler  # noqa: PLC0415
            self.settler = FundingSettler(
                config.funding_rate_dir,
                tuple(config.venues),
                start_date=config.start_date,
                end_date=config.end_date,
            )

        self.agent = BackgroundAgentModel(config.agent_flow)
        self._last_trade_ts: dict[str, float] = {}

        self._order_ids = 0
        self._trackers: dict[int, _SignalTracker] = {}
        self._maker_to_signal: dict[int, int] = {}
        # Exchange-side cancel has landed but the strategy cannot know yet: slot is released at effective_ts + feed delay.
        self._cancel_acks: list[float] = []
        self._cancel_watch: dict[int, "MakerOrder"] = {}   # orders with a cancel requested and not yet resolved
        self.lifetime: Counter[str] = Counter()
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
            # Inject synthetic background taker flow in the gap since the last
            # real trade on this venue.  Gate on live orders: with nothing
            # resting the synthetic trades have no effect on queue state.
            if (self.agent.config.enabled
                    and book.ready
                    and self.maker.live_orders(ev.venue)):
                last = self._last_trade_ts.get(ev.venue, now)
                if last < now:
                    for syn in self.agent.generate(
                        last, now, book.best_bid, book.best_ask,
                    ):
                        for f in self.maker.on_trade(
                            ev.venue, syn.price, syn.volume, syn.sign,
                            syn.timestamp, spec.fees.maker_bps, is_synthetic=True,
                        ):
                            self._on_fill(f)
            self._last_trade_ts[ev.venue] = now
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
                # Maker order fully consumed: order was removed from self.maker.orders
                # in _consume(). Resolve immediately instead of waiting for cancel_all.
                if fill.order_id not in self.maker.orders:
                    tracker.maker_resting = False

        if len(self.trade_log) < self.config.max_trade_log:
            self.trade_log.append({
                "ts": fill.ts, "venue": fill.venue, "side": fill.side,
                "qty": fill.qty, "price": fill.price, "fee": fill.fee,
                "taker": fill.is_taker, "signal_id": fill.signal_id,
                "slippage_bps": fill.slippage_bps,
                "synthetic": fill.is_synthetic,
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

    def _expire_maker_orders(self, now: float) -> None:
        """Time-in-force for resting maker orders (ArbConfig.maker_max_age_s, seconds; 0 disables).

        Semantics (all times are replay receive time):
          * The lifetime clock starts when the order is placed at the exchange (`placed_ts`, the first replay event at or
            after its release_ts). An order still in flight therefore cannot expire; it is counted toward the live-order
            limit until it lands.
          * At `placed_ts + ttl` the strategy requests a cancel. The cancel takes effect after one sampled cancel latency,
            drawn once and stored on the order, so scanning again can neither move the deadline nor cancel twice.
          * Between the request and the effective time the order is 'cancel_pending': still resting, still fillable, and
            still counted as live. Fills in that window are kept.
          * Tie rule: expiry is processed before the market event it coincides with, so an event stamped exactly at the
            effective time does not fill the order.
          * The slot is released to the strategy one feed delay after the cancel lands, not at the instant it lands.
        """
        ttl = self.config.arb.maker_max_age_s
        # A cancel that lost the race to a full fill: the order left the book on its own, the cancel finds nothing.
        for oid, gone in list(self._cancel_watch.items()):
            if oid not in self.maker.orders:
                del self._cancel_watch[oid]
                self.lifetime["cancels_lost_to_fill"] += 1
                during = gone.filled_qty - gone.filled_at_cancel_request
                if during > 1e-12:
                    self.lifetime["fills_while_cancel_pending"] += 1
                    self.lifetime["qty_filled_while_cancel_pending"] += during
        for order in list(self.maker.orders.values()):
            if order.cancel_requested_ts is None:
                requested = order.placed_ts + ttl
                if now < requested:
                    continue
                order.cancel_requested_ts = requested
                order.cancel_effective_ts = requested + self.latency.cancel_delay(order.venue)
                order.cancel_reason = "expired"
                order.filled_at_cancel_request = order.filled_qty
                self._cancel_watch[order.order_id] = order
                self.lifetime["expiries_triggered"] += 1
            if now < order.cancel_effective_ts:
                continue
            effective = max(order.cancel_effective_ts, order.placed_ts)
            cancelled = self.maker.cancel(order.order_id, effective)
            if cancelled is None:
                continue
            self._cancel_watch.pop(cancelled.order_id, None)
            self.lifetime["cancels_applied"] += 1
            self.lifetime["age_at_cancel_sum_s"] += effective - cancelled.placed_ts
            self.lifetime["cancel_pending_time_sum_s"] += effective - cancelled.cancel_requested_ts
            if cancelled.filled_qty > 1e-12:
                self.lifetime["expired_after_partial_fill"] += 1
            during = cancelled.filled_qty - cancelled.filled_at_cancel_request
            if during > 1e-12:
                self.lifetime["fills_while_cancel_pending"] += 1
                self.lifetime["qty_filled_while_cancel_pending"] += during
            self._cancel_acks.append(effective + self.venue_specs[cancelled.venue].latency.feed_ms / 1000.0)
            sid = self._maker_to_signal.get(cancelled.order_id, -1)
            tracker = self._trackers.get(sid)
            if tracker is not None:
                tracker.maker_resting = False
            self._maybe_resolve(sid, now)

    def _pending_exposure(self) -> PendingExposure:
        """Worst-case base not in the inventory yet: resting maker remainders plus every order still in flight."""
        pend = PendingExposure()
        for o in self.maker.orders.values():
            pend.add(o.venue, o.side_str, o.remaining)
        for o in self.pending.in_flight():
            pend.add(o.venue, o.side, o.qty)
        return pend

    def _live_maker_orders(self, now: float) -> int:
        """Maker orders the strategy must treat as possibly executable: resting (incl. cancel-pending), in flight,
        and cancelled at the exchange but not yet acknowledged to the strategy."""
        n = len(self.maker.orders)
        if self.config.arb.maker_count_inflight:
            n += self.pending.maker_legs_in_flight()
        if self._cancel_acks:
            self._cancel_acks = [t for t in self._cancel_acks if t > now]
            n += len(self._cancel_acks)
        return n

    def _advance_to(self, now: float) -> None:
        """Bring order state up to `now`, before the market event stamped `now` is applied.

        Order: in-flight orders land, then expiries/cancels take effect, then the caller applies the event. An order
        sent at t-latency therefore cannot see the book update arriving right now, and a cancel effective at or before
        `now` wins a tie against that event.
        """
        for order in self.pending.release(now):
            self._execute_arrival(order, now)
        if self.config.arb.maker_max_age_s > 0:
            self._expire_maker_orders(now)

    def _close_out(self, now: float) -> None:
        """End of data. Orders still in flight land on the last book, resting orders are closed.

        Neither is a real outcome: the data simply stopped. They are counted separately (`maker_landed_after_data_end`,
        `taker_landed_after_data_end`, `resting_at_data_end`, `cancel_pending_at_data_end`) and never as an applied
        cancel. The closes only censor the fill-model observations; they add no fills.
        """
        self.lifetime["resting_at_data_end"] = len(self.maker.orders)
        self.lifetime["cancel_pending_at_data_end"] = sum(
            1 for o in self.maker.orders.values() if o.cancel_requested_ts is not None)
        for order in self.pending.drain():
            self.lifetime["taker_landed_after_data_end" if order.is_taker else "maker_landed_after_data_end"] += 1
            self._execute_arrival(order, now)
        for mo in self.maker.orders.values():
            if mo.cancel_requested_ts is None:
                mo.cancel_reason = "end_of_data"
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
        self._cancel_watch.clear()
        self._cancel_acks.clear()

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
            quality_preflight=True,
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

                self._advance_to(now)
                self._apply_event(ev)

                if self.settler is not None:
                    self.settler.settle(now, self.inventory, self.books)

                # Trades can move the touch too, via trade-tape repair.
                if self._touch_changed(ev.venue):
                    self.markouts.resolve(now, {v: b.mid for v, b in self.books.items()})
                    blocked_before = self.strategy.rejects["maker_limit"]
                    signal = self.strategy.evaluate(
                        self.books, self.states, self.inventory, now,
                        live_maker_orders=self._live_maker_orders(now),
                        pending=self._pending_exposure() if cfg.arb.count_pending_exposure else None,
                    )
                    if (self.strategy.rejects["maker_limit"] > blocked_before
                            and (self._cancel_acks or any(o.cancel_requested_ts is not None for o in self.maker.orders.values()))):
                        self.lifetime["quotes_blocked_by_unresolved_cancel"] += 1
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

            self._close_out(now)

            self.markouts.resolve(now, {v: b.mid for v, b in self.books.items()}, finalize=True)
            return self._build_result(feed, coverage, virtual_clock.real_time() - wall_start)
        finally:
            virtual_clock.uninstall()

    # ── Result ───────────────────────────────────────────────────────────────

    def _build_result(self, feed: CrossVenueFeed, coverage, wall_s: float) -> ArbBacktestResult:
        cfg = self.config
        mids = {v: b.mid for v, b in self.books.items()}
        open_pos_value = sum(
            self.inventory.base.get(v, 0.0) * mids.get(v, 0.0) for v in cfg.venues
        )
        ending_equity = self.inventory.total_cash + open_pos_value
        mtm = ending_equity  # legacy alias

        n = max(1, self.edge_samples)
        mean_exp = self.expected_gross_sum / n
        mean_real = self.realized_gross_sum / n
        mean_net = self.realized_net_sum / n
        capture = (mean_real / mean_exp) if abs(mean_exp) > 1e-12 else 0.0
        mean_slip = self.slippage_sum / self.slippage_count if self.slippage_count else 0.0

        feed_report = feed.report()
        for venue, rep in feed_report.items():
            lat = None if venue.startswith("_") else rep.get("mean_feed_latency_vs_host_ms")
            if lat is not None and lat < -CLOCK_OFFSET_WARN_MS:
                warnings.warn(
                    f"{venue}: recorded receive times average {-lat:.1f} ms BEFORE the exchange timestamps after "
                    "host-clock correction. The collector clock for this venue is probably offset; the replay then "
                    "shows this venue's updates early and results can be look-ahead biased.",
                    RuntimeWarning, stacklevel=2)

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
            funding_paid=self.settler.funding_paid if self.settler else 0.0,
            realized_pnl=(self.gross_pnl - self.fees_paid - self.unhedged_loss
                          - (self.settler.funding_paid if self.settler else 0.0)),
            open_position_value=open_pos_value,
            ending_equity=ending_equity,
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
            feed_report=feed_report,
            latency_report=self.latency.report(),
            book_stats={v: b.stats() for v, b in self.books.items()},
            maker_stats=self.maker.stats(),
            markout_summary=self.markouts.summary(),
            miss_reasons=dict(self.miss_reasons),
            coverage={k: list(v) for k, v in coverage.items()},
            fill_log_path=fill_log_path,
            bucket_stats_path=bucket_stats_path,
            trades_path=trades_path,
            maker_lifetime=self._lifetime_report(),
        )

    def _lifetime_report(self) -> dict[str, float]:
        lt = self.lifetime
        cancels = max(1, lt["cancels_applied"])
        keys = ("expiries_triggered", "cancels_applied", "cancels_lost_to_fill", "expired_after_partial_fill",
                "fills_while_cancel_pending", "qty_filled_while_cancel_pending", "quotes_blocked_by_unresolved_cancel",
                "resting_at_data_end", "cancel_pending_at_data_end", "maker_landed_after_data_end",
                "taker_landed_after_data_end")
        out = {k: float(lt[k]) for k in keys}
        out["mean_age_at_cancel_s"] = lt["age_at_cancel_sum_s"] / cancels
        out["mean_cancel_pending_s"] = lt["cancel_pending_time_sum_s"] / cancels
        return out


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
    p.add_argument("--bybit-fees", default="bybit_perp_mm")
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
    p.add_argument("--min-queue-ahead", type=float, default=0.0,
                   dest="min_queue_ahead",
                   help="MakerConfig.min_queue_ahead: queue-depth floor for absent "
                        "price levels (base-asset units). 0 = no floor (default). "
                        "Calibrate from the p25 of observed present-level depths.")
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
        arb=arb, maker=MakerConfig(min_queue_ahead=args.min_queue_ahead),
        consume_liquidity=not args.no_consume,
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
