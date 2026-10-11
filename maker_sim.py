"""Queue-aware passive (maker) simulation backed by ``sim_engine``.

``sim_engine/`` is a byte-identical copy of ``Newproject/sim_engine``
(``dry_run_core.DryRunOrderManager`` with the C++ ``mm_matching_engine`` when it
is importable, otherwise its pure-Python queue-position model). This module
feeds it this platform's reconstructed books and validated public trades:

* live (dry run / forward test): ``LivePaperSession`` calls ``on_book`` and
  ``on_trade`` with real receive times;
* replay (simulation): ``simulate_recorded_session`` streams stored canonical
  book events and trades in receive-time order, with the engine's clock driven
  by event time.

The simulator quotes post-only at the touch on each venue every
``quote_interval_ns`` within an inventory band and records every simulated
fill. It never sends an order anywhere.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from decimal import Decimal
import importlib
import io
import json
from pathlib import Path
import random
import sys
from types import SimpleNamespace
from typing import Any, Callable, Iterable
import warnings

from .config import load_config
from .fees import Liquidity
from .models import BookState, MarketEvent
from .orderbook import OrderBook


SIM_ENGINE_DIR = Path(__file__).with_name("sim_engine")


def load_sim_engine():
    if str(SIM_ENGINE_DIR) not in sys.path:
        sys.path.insert(0, str(SIM_ENGINE_DIR))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return importlib.import_module("dry_run_core")


@dataclass
class _PairState:
    inventory: float
    vol_regime: str = "calm"


@dataclass
class _VenueStats:
    quotes: int = 0
    fills: int = 0
    buy_quantity: float = 0.0
    sell_quantity: float = 0.0
    cash: float = 0.0
    fees: float = 0.0
    first_mid: float | None = None
    last_mid: float | None = None
    last_top: tuple[float, float] | None = None
    trades_seen: int = 0
    max_inventory_seen: float = 0.0
    min_inventory_seen: float = 0.0
    sanity_violations: list[str] = field(default_factory=list)


class MakerDryRunSimulator:
    def __init__(
        self,
        venues: Iterable[str],
        symbol: str,
        *,
        quote_size: float = 0.001,
        quote_interval_ns: int = 1_000_000_000,
        max_inventory: float = 0.01,
        initial_inventory: float | None = None,
        latency_ms: float = 10.0,
        maker_fee_bps: dict[str, float] | None = None,
        seed: int = 7,
        depth: int = 20,
        clock_s: Callable[[], float] | None = None,
    ) -> None:
        core = load_sim_engine()
        if clock_s is not None:
            # Replay only (dedicated process): age/latency gates follow event time.
            core.time = SimpleNamespace(monotonic=clock_s)
        random.seed(seed)
        self.engine = "cpp_mm_matching_engine" if core._SIM_FILL_AVAILABLE else "python_queue_model"
        self.symbol = symbol
        self.quote_size = quote_size
        self.quote_interval_ns = quote_interval_ns
        self.max_inventory = max_inventory
        self.initial_inventory = max_inventory / 2 if initial_inventory is None else initial_inventory
        self.depth = depth
        self.seed = seed
        self.latency_ms = latency_ms
        self.maker_fee_bps = dict(maker_fee_bps or {})
        self.states: dict[str, _PairState] = {}
        self.stats: dict[str, _VenueStats] = {}
        self.managers: dict[str, Any] = {}
        self._open: dict[str, set[str]] = {}
        self._next_quote_ns: dict[str, int] = {}
        self._sequence = 0
        for venue in venues:
            state = _PairState(inventory=self.initial_inventory)
            self.states[venue] = state
            self.stats[venue] = _VenueStats()
            self._open[venue] = set()
            self._next_quote_ns[venue] = 0
            with contextlib.redirect_stdout(io.StringIO()):
                self.managers[venue] = core.DryRunOrderManager(
                    [symbol],
                    apply_fill=lambda sym, side, qty, price, fee, venue=venue: self._apply_fill(
                        venue, side, qty, price, fee
                    ),
                    pair_state={symbol: state},
                    latency_ms=latency_ms,
                    maker_fee_rate=self.maker_fee_bps.get(venue, 0.0) / 10_000,
                )

    def on_book(self, venue: str, book: OrderBook, now_ns: int) -> None:
        manager = self.managers.get(venue)
        if manager is None or book.state != BookState.VALID:
            return
        bids = [(float(price), float(qty)) for price, qty in sorted(book.bids.items(), reverse=True)[: self.depth]]
        asks = [(float(price), float(qty)) for price, qty in sorted(book.asks.items())[: self.depth]]
        manager.update_book(self.symbol, bids, asks)
        best_bid, best_ask = bids[0][0], asks[0][0]
        stats = self.stats[venue]
        mid = (best_bid + best_ask) / 2
        stats.first_mid = stats.first_mid if stats.first_mid is not None else mid
        stats.last_mid = mid
        stats.last_top = (best_bid, best_ask)
        if now_ns < self._next_quote_ns[venue]:
            return
        self._next_quote_ns[venue] = now_ns + self.quote_interval_ns
        manager.simulate_dry_fills(self.symbol, best_bid, best_ask)
        for order_id in list(self._open[venue]):
            manager.cancel_order(self.symbol, order_id)
        self._open[venue].clear()
        inventory = self.states[venue].inventory
        if inventory + self.quote_size <= self.max_inventory + 1e-12:
            self._place(venue, "buy", best_bid)
        if inventory - self.quote_size >= -1e-12:
            self._place(venue, "sell", best_ask)

    def _place(self, venue: str, side: str, price: float) -> None:
        self._sequence += 1
        order_id = f"{venue.lower()}-{side}-{self._sequence}"
        self.managers[venue].register_order(self.symbol, order_id, side, price, self.quote_size)
        self._open[venue].add(order_id)
        self.stats[venue].quotes += 1

    def on_trade(self, venue: str, price: float, quantity: float, aggressor: str) -> None:
        manager = self.managers.get(venue)
        if manager is None:
            return
        self.stats[venue].trades_seen += 1
        manager.buffer_trade(self.symbol, price, quantity, aggressor.lower())

    def on_trade_tick(self, tick: Any) -> None:
        self.on_trade(tick.venue, float(tick.price), float(tick.quantity), str(tick.aggressor_side))

    def _apply_fill(self, venue: str, side: str, quantity: float, price: float, fee: float) -> None:
        stats = self.stats[venue]
        state = self.states[venue]
        if not (quantity > 0 and price > 0):
            stats.sanity_violations.append(f"non-positive fill {side} {quantity}@{price}")
            return
        if quantity > self.quote_size * 1.000001:
            stats.sanity_violations.append(f"fill {quantity} larger than quote {self.quote_size}")
        if stats.last_top is not None:
            best_bid, best_ask = stats.last_top
            band = 0.02 * (best_bid + best_ask) / 2
            if not best_bid - band <= price <= best_ask + band:
                stats.sanity_violations.append(
                    f"fill price {price} outside 2% of book {best_bid}/{best_ask}"
                )
        signed = quantity if side == "buy" else -quantity
        state.inventory += signed
        stats.cash -= signed * price
        stats.fees += fee
        stats.fills += 1
        if side == "buy":
            stats.buy_quantity += quantity
        else:
            stats.sell_quantity += quantity
        stats.max_inventory_seen = max(stats.max_inventory_seen, state.inventory)
        stats.min_inventory_seen = min(stats.min_inventory_seen, state.inventory)
        if state.inventory > self.max_inventory + self.quote_size + 1e-9 or state.inventory < -self.quote_size - 1e-9:
            stats.sanity_violations.append(f"inventory {state.inventory} outside band")

    def report(self) -> dict[str, Any]:
        venues = {}
        for venue, stats in sorted(self.stats.items()):
            inventory = self.states[venue].inventory
            pnl = None
            if stats.last_mid is not None and stats.first_mid is not None:
                pnl = (
                    stats.cash + inventory * stats.last_mid
                    - self.initial_inventory * stats.first_mid - stats.fees
                )
            venues[venue] = {
                "quotes": stats.quotes,
                "fills": stats.fills,
                "fill_ratio": stats.fills / stats.quotes if stats.quotes else 0.0,
                "trades_seen": stats.trades_seen,
                "buy_quantity": stats.buy_quantity,
                "sell_quantity": stats.sell_quantity,
                "inventory": inventory,
                "inventory_range": [stats.min_inventory_seen, stats.max_inventory_seen],
                "fees": stats.fees,
                "mark_to_market_pnl_quote": pnl,
                "sanity_violations": stats.sanity_violations[:20],
                "sanity_violation_count": len(stats.sanity_violations),
            }
        return {
            "schema_version": "1.0.0",
            "engine": self.engine,
            "source": "Newproject/sim_engine (vendored, byte-identical)",
            "symbol": self.symbol,
            "quote_size": self.quote_size,
            "quote_interval_ns": self.quote_interval_ns,
            "max_inventory": self.max_inventory,
            "initial_inventory": self.initial_inventory,
            "latency_ms": self.latency_ms,
            "seed": self.seed,
            "venues": venues,
            "limitations": [
                "Passive at-touch quoting harness for fill-model validation, not a strategy.",
                "Python queue model is used when the compiled C++ engine is not importable.",
            ],
        }


def maker_simulator_from_config(cfg, *, clock_s: Callable[[], float] | None = None) -> MakerDryRunSimulator | None:
    settings = (cfg.strategies or {}).get("maker_simulation") or {}
    if not settings.get("enabled", False):
        return None
    venues = tuple(settings.get("venues", cfg.venues))
    spec = cfg.instruments.get(venues[0], cfg.instrument_id)
    return MakerDryRunSimulator(
        venues, f"{spec.base}/{spec.quote}",
        quote_size=float(settings.get("quote_size", "0.001")),
        quote_interval_ns=int(settings.get("quote_interval_ms", 1_000)) * 1_000_000,
        max_inventory=float(settings.get("max_inventory", "0.01")),
        latency_ms=float(settings.get("latency_ms", 10.0)),
        maker_fee_bps={
            venue: float(cfg.instruments.fee_bps(venue, cfg.instrument_id, Liquidity.MAKER))
            for venue in venues
        },
        seed=int(settings.get("seed", 7)),
        clock_s=clock_s,
    )


def simulate_recorded_session(
    config_path: Path, canonical_paths: list[Path], trade_paths: list[Path],
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Replay stored books and trades through the sim_engine maker model."""
    cfg, _ = load_config(config_path)
    clock = [0.0]
    simulator = maker_simulator_from_config(cfg, clock_s=lambda: clock[0])
    if simulator is None:
        cfg_settings = {"enabled": True}
        cfg.strategies["maker_simulation"] = cfg_settings  # type: ignore[index]
        simulator = maker_simulator_from_config(cfg, clock_s=lambda: clock[0])
    stream: list[tuple[int, int, Any]] = []
    for path in canonical_paths:
        with path.open(encoding="utf-8") as handle:
            for ordinal, line in enumerate(handle):
                if line.strip():
                    event = MarketEvent.from_dict(json.loads(line))
                    stream.append((event.receive_ts_ns, 0, event))
    for path in trade_paths:
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    row = json.loads(line)
                    stream.append((int(row["receive_ts_ns"]), 1, row))
    stream.sort(key=lambda item: (item[0], item[1]))
    books = {
        venue: cfg.book_sanity.new_book(venue, cfg.instrument_id) for venue in cfg.venues
    }
    events = trades = 0
    for ts_ns, kind, item in stream:
        clock[0] = ts_ns / 1e9
        if kind == 0:
            book = books.get(item.venue)
            if book is None:
                continue
            book.apply(item)
            simulator.on_book(item.venue, book, ts_ns)
            events += 1
        else:
            simulator.on_trade(
                str(item["venue"]), float(item["price"]), float(item["quantity"]),
                str(item["aggressor_side"]),
            )
            trades += 1
    report = simulator.report()
    report["replay"] = {"book_events": events, "trades": trades}
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report
