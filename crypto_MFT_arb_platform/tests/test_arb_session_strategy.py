"""Phase D, step D4: the arb strategy as a TradingSession strategy. Inputs come from the core (ledger positions and cash, OMS pending exposure and live maker count, float mirrors of the core books);
real March signals show that the signal-to-Opportunity conversion changes no decision. Synthetic ledger/OMS stubs prove the wiring only (no profit figure)."""
import heapq
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crypto_stat_arb.instruments import InstrumentMaster
from crypto_stat_arb.models import ChildOrder, Instrument, OrderState, Side
from crypto_stat_arb.simulation.order_book import OrderBook as FloatBook
from crypto_stat_arb.simulation.venue_state import VenueState
from crypto_stat_arb.strategies.arb_strategy import ArbConfig, ArbSessionStrategy, ArbSignal, CrossVenueArbStrategy
from new_backtest.event_loader import BOOK, DELTA, TRADE, delta_fields, iter_venue_events, snapshot_levels, trade_fields

SLICES = Path(__file__).resolve().parents[2] / "out" / "queue_v2" / "slices_mar2024"
INSTR = "SOL-USDT-PERP"
NAMES = {"bybit": "BYBIT", "binance": "BINANCE"}


def instruments(tick="0.001", step="0.1"):
    rows = [Instrument(instrument_id=INSTR, venue=v, symbol="SOLUSDT", base="SOL", quote="USDT", tick_size=Decimal(tick), quantity_step=Decimal(step), min_quantity=Decimal(step),
                       min_notional=Decimal("1"), taker_fee_bps=Decimal("5"), product_type="PERPETUAL") for v in NAMES.values()]
    return InstrumentMaster(rows)


def venues():
    return {"bybit": SimpleNamespace(fees=SimpleNamespace(maker_bps=-0.5, taker_bps=5.5)), "binance": SimpleNamespace(fees=SimpleNamespace(maker_bps=2.0, taker_bps=5.0))}


def shim(strategy=None, ledger=None, oms=None, **kw):
    strategy = strategy or CrossVenueArbStrategy("SOLUSDT", venues(), ArbConfig(mode="maker_taker", maker_venues=("bybit",)))
    return ArbSessionStrategy(strategy, ledger=ledger or SimpleNamespace(positions={}, cash_deltas={}), oms=oms or SimpleNamespace(plans={}), states={}, instruments=instruments(**kw),
                              instrument_id=INSTR, venue_names=NAMES)


def signal(**kw):
    base = dict(signal_id=7, ts=1.0, buy_venue="bybit", sell_venue="binance", qty=1.0, gross_edge_bps=9.0, fee_bps=2.7, net_edge_bps=6.3, legs=[], mode="maker_taker", buy_price=144.865, sell_price=144.9)
    base.update(kw)
    return ArbSignal(**base)


class ShimInputsFromTheCore(unittest.TestCase):
    def test_inventory_and_cash_are_read_from_the_core_ledger(self):
        ledger = SimpleNamespace(positions={("BYBIT", INSTR): Decimal("1.5"), ("BINANCE", INSTR): Decimal("-1.5")}, cash_deltas={"BYBIT": Decimal("-217.3"), "BINANCE": Decimal("217.0")})
        inv = shim(ledger=ledger).inventory()
        self.assertEqual((inv.base, inv.cash), ({"bybit": 1.5, "binance": -1.5}, {"bybit": -217.3, "binance": 217.0}))
        self.assertEqual(inv.net_base, 0.0)

    def test_pending_exposure_and_the_live_maker_count_come_from_the_oms_plans(self):
        def child(venue, side, qty, filled, state, post_only):
            return ChildOrder(order_id=f"{venue}{side}{qty}", parent_id="p", opportunity_id="o", venue=venue, instrument_id=INSTR, side=side, quantity=Decimal(qty), limit_price=Decimal("1"),
                              state=state, filled_quantity=Decimal(filled), post_only=post_only)
        plans = {"p": SimpleNamespace(children=[
            child("BYBIT", Side.BUY, "1.0", "0.4", OrderState.PARTIAL, True),                # a resting maker with 0.6 left
            child("BINANCE", Side.SELL, "0.4", "0", OrderState.SENT, False),                  # a hedge in flight
            child("BINANCE", Side.SELL, "1.0", "1.0", OrderState.FILLED, False),              # done: not pending
            child("BYBIT", Side.BUY, "1.0", "0", OrderState.CANCELLED, True)])}               # gone
        pending, makers = shim(oms=SimpleNamespace(plans=plans)).pending_and_live_makers()
        self.assertEqual((pending.buys, pending.sells, makers), ({"bybit": 0.6}, {"binance": 0.4}, 1))

    def test_a_float_book_carries_the_core_books_own_receive_time_so_staleness_is_visible(self):
        core = SimpleNamespace(bids={Decimal("144.865"): Decimal("3.5")}, asks={Decimal("144.866"): Decimal("2")}, last_receive_ts_ns=1_700_000_000_250_000_000)
        out = shim().float_books({("BYBIT", INSTR): core, ("BINANCE", INSTR): core}, 1_700_000_005.0)
        self.assertEqual(out["bybit"].last_update_ts, 1_700_000_000.25)
        self.assertEqual((out["bybit"].best_bid, out["bybit"].best_ask_size), (144.865, 2.0))
        self.assertIsNone(shim().float_books({("BYBIT", INSTR): core}, 1.0))                  # a venue with no book: no decision


class SignalToOpportunity(unittest.TestCase):
    def test_float_to_decimal_is_lossless_and_on_grid_values_are_not_touched(self):
        s = shim()
        for x in (144.865, 0.1, 0.30000000000000004, 144.9, 1e-7, 12345.678901234):
            self.assertEqual(float(ArbSessionStrategy._exact(x)), x)
        opp = s.to_opportunity(signal(), 5)
        self.assertEqual((opp.buy_venue, opp.sell_venue, opp.quantity, opp.buy_price, opp.sell_price), ("BYBIT", "BINANCE", Decimal("1.0"), Decimal("144.865"), Decimal("144.9")))
        self.assertEqual(s.grid_snaps, {"price": 0, "quantity": 0})

    def test_an_off_grid_price_rests_on_the_safe_side_and_an_off_grid_size_goes_down(self):
        s = shim(tick="0.01", step="0.1")
        opp = s.to_opportunity(signal(buy_price=144.865, sell_price=144.903, qty=1.25), 5)
        self.assertEqual((opp.buy_price, opp.sell_price, opp.quantity), (Decimal("144.86"), Decimal("144.91"), Decimal("1.2")))     # a bid rests lower, an ask higher, never through its limit
        self.assertEqual(s.grid_snaps, {"price": 2, "quantity": 1})


@unittest.skipUnless((SLICES / "bybit").is_dir() and (SLICES / "binance").is_dir(), "March slices not available")
class RealMarchSignals(unittest.TestCase):
    """Signals the real strategy produces on a real March slice (SELECT day 2024-03-11 slice, both venues, every tenth event until 300 signals or the end of the 2 h slice; the strategy's own signals, before any breaker or in-flight gate of a run): no decision (direction, venues, size, price) changes."""

    def _signals(self):
        from new_backtest.profiles import apply_profile, load_profile
        cfg = apply_profile(load_profile("perp_perp_maker_taker_v1_4"), dataset="mar2024", data_root=str(SLICES), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_d4")
        strategy = CrossVenueArbStrategy("SOLUSDT", venues(), cfg.arb)
        day = "2024-03-11"
        streams = [iter_venue_events(SLICES, v, "SOLUSDT", day, day) for v in ("bybit", "binance")]
        merged = heapq.merge(*streams, key=lambda e: e.exchange_ts)
        books = {v: FloatBook(v, "SOLUSDT", delta_semantics="hybrid", message_atomic=True) for v in NAMES}
        states = {v: VenueState(v) for v in NAMES}
        inventory = shim().inventory()
        inventory.ensure(tuple(NAMES))
        out, t0 = [], None
        for n, ev in enumerate(merged):
            t0 = t0 or ev.exchange_ts
            if ev.exchange_ts - t0 > 7200 or len(out) >= 300:
                break
            b, st = books[ev.venue], states[ev.venue]
            if ev.kind == BOOK:
                b.apply_snapshot(*snapshot_levels(ev.payload), ev.exchange_ts)
            elif ev.kind == DELTA:
                side, price, vol, et = delta_fields(ev.payload)
                b.apply_delta(side, price, vol, et, ev.exchange_ts)
                b.end_message()
            elif ev.kind == TRADE:
                price, vol, sign = trade_fields(ev.payload)
                b.on_trade(price, vol, sign, ev.exchange_ts)
                st.on_trade(ev.exchange_ts, price, vol, sign)
                continue
            if b.ready:
                st.on_book(b, ev.exchange_ts)
            if n % 10 == 0 and all(x.ready for x in books.values()):
                sig = strategy.evaluate(books, states, inventory, ev.exchange_ts)
                if sig is not None:
                    out.append((ev.exchange_ts, sig))
        return out

    def test_the_conversion_changes_no_decision_on_real_signals(self):
        signals = self._signals()
        s = shim(tick="0.001", step="0.1")
        changed = 0
        for ts, sig in signals:
            opp = s.to_opportunity(sig, int(ts * 1e9))
            same = (NAMES[sig.buy_venue], NAMES[sig.sell_venue], float(opp.quantity), float(opp.buy_price), float(opp.sell_price)) == (NAMES[sig.buy_venue], NAMES[sig.sell_venue], sig.qty, sig.buy_price, sig.sell_price)
            changed += not same
            self.assertEqual(opp.buy_venue, NAMES[sig.buy_venue])
            self.assertEqual(opp.sell_venue, NAMES[sig.sell_venue])
            self.assertEqual(float(opp.gross_edge_bps), sig.gross_edge_bps)
            self.assertEqual(float(opp.estimated_net_edge_bps), sig.net_edge_bps)
        print(f"\n[real March signals, Bybit+Binance 2024-03-11] signals {len(signals)}, changed by the conversion {changed}, grid snaps {s.grid_snaps}")
        self.assertGreater(len(signals), 0, "no real signal to test on: widen the window")
        self.assertEqual(changed, 0)


if __name__ == "__main__":
    unittest.main()
