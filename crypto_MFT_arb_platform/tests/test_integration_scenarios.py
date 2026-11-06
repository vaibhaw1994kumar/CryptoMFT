"""Integration scenarios, Phase 0 of the shared-platform work (out/integration/core_backtest_integration_prompt.md).

Fifteen small deterministic two-venue scripts. The expected orders, fills, positions, fees, funding and cash of every scenario were worked
out BY HAND and written in the docstring of its class BEFORE the scenario was first run; the asserts are those numbers, nothing else.
A scenario the current code cannot express is marked NOT_EXPRESSIBLE; a scenario whose requirement-correct answer differs from what the
current code does is split in two: `test_target_*` (the requirement, `expectedFailure` today) and `test_today_*` (pins today's behaviour so
a later phase that changes it shows up as a failing test to update on purpose).

Book (tests/test_maker_order_lifetime.Harness): both venues bid 99/98, ask 101/102, size 5; latency preset colocated_both, entry 3 ms, cancel
50 ms, feed 1 ms. Fee presets bybit_perp_mm (maker -0.5 bps, taker 5.5) and binance_perp_vip0 (maker 2.0, taker 5.0). The maker bid is 2.0 @ 100.0
on bybit with 0.5 ahead in the queue (min_queue_ahead), so a sell print of 2.5 at 100 fills all 2.0. Hedge: sell on binance at the bid, 99.0.

Common arithmetic (clean fill and hedge, scenario 1):
    maker  buy 2.0 @ 100.0 bybit    fee  2.0 x 100 x -0.5 / 1e4 = -0.01          cash bybit   = -200.00 + 0.01  = -199.99
    hedge  sell 2.0 @ 99.0 binance  fee  2.0 x  99 x  5.0 / 1e4 = +0.099         cash binance = +198.00 - 0.099 = +197.901
    fees_paid = 0.089     total cash = -2.089     gross pnl = (99 - 100) x 2 = -2.0     funding 0     net position 0
These synthetic runs prove wiring only. No figure here is a profit figure.

First run (5 Nov, before any change to the code under test): 5 of the 24 tests did not match what was written; the rest passed as written. Four of the five were my errors,
corrected below and marked "FIRST-RUN CORRECTION" in their class; the fifth (S13 today) was a placeholder to be filled from the observation. Separately, the first form of
3b passed in both modes and proved nothing; I rebuilt it. None of the five was a code defect except where the class says so:
  S10  my driver omitted the breaker poll / trip handler that run() calls after every event (the expectation stands, the driver was fixed);
  S11  I assumed the liquidation fee replaces the venue taker fee; the code charges both on the liquidation fill, by design (backtester._margin_step comment);
  S12  I named the recovered plan state DISPATCHED; a plan with one filled child is PARTIAL;
  S15  I expected a gap (no venue query after a restart); there is none in this window: both children are re-armed and queried, nothing is re-sent.
S05 and S13 are real findings (the ghost over-hedge; the executor's answer depends on the order of print and depth update).
"""
from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
import warnings
from decimal import Decimal
from pathlib import Path

from new_backtest.arb_strategy import InventoryBook
from new_backtest.circuit_breaker import CircuitBreakerConfig
from new_backtest.event_loader import BOOK, DELTA, TRADE
from new_backtest.execution import ExecutionConfig
from new_backtest.funding_settler import FundingSettler
from new_backtest.hedge_faults import HedgeFaultConfig, Outage
from new_backtest.timing import TimelineSpec, TimingConfig, VenueTimelines

from test_hedge_failures import fh, mark, rows, walk
from test_margin_model import hedged_position, margin_cfg
from test_maker_executor_engine_parity import ExecutorAdapter
from test_maker_order_lifetime import BINANCE, BYBIT, Harness, started
from test_postonly_reject import harness as po_harness, poke, send as po_send, two_books

warnings.simplefilter("ignore")
EPS = 1e-9


def fills(h, taker=None):
    return [(r["venue"], r["side"], round(r["qty"], 9), round(r["price"], 9)) for r in h.bt.trade_log if taker is None or r["taker"] == taker]


class Base(unittest.TestCase):
    def assertPosition(self, h, bybit, binance):
        self.assertAlmostEqual(h.bt.inventory.base[BYBIT], bybit, places=9)
        self.assertAlmostEqual(h.bt.inventory.base[BINANCE], binance, places=9)

    def assertCash(self, h, bybit, binance, fees):
        self.assertAlmostEqual(h.bt.inventory.cash[BYBIT], bybit, places=9)
        self.assertAlmostEqual(h.bt.inventory.cash[BINANCE], binance, places=9)
        self.assertAlmostEqual(h.bt.fees_paid, fees, places=9)


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
class S01CleanFillAndHedge(Base):
    """1. A clean maker fill; the hedge fills in full.
    Orders: maker buy 2.0 @100 bybit (sent 0, lands 0.003); hedge sell 2.0 binance (sent 5.0, lands 5.003). Fills: (bybit buy 2.0 @100.0 maker),
    (binance sell 2.0 @99.0 taker). Position bybit +2.0, binance -2.0. fees 0.089, cash bybit -199.99, binance +197.901, funding 0."""

    def test_scenario(self):
        h = started()
        h.sell_trade(5.0, BYBIT, volume=2.5)
        h.advance(5.01)
        self.assertEqual(fills(h), [(BYBIT, "buy", 2.0, 100.0), (BINANCE, "sell", 2.0, 99.0)])
        self.assertEqual((h.bt.orders_sent, h.bt.hedge["hedges_sent"], h.bt.hedge["hedges_filled"]), (2, 1, 1))
        self.assertPosition(h, 2.0, -2.0)
        self.assertCash(h, -199.99, 197.901, 0.089)
        self.assertAlmostEqual(h.bt.inventory.total_cash, -2.089, places=9)
        self.assertAlmostEqual(h.bt.gross_pnl, -2.0, places=9)
        self.assertEqual((h.bt.signals_completed, h.bt.signals_unhedged), (1, 0))
        h.assert_ledger_reconciles(self)


class S02MakerPartialFill(Base):
    """2. Maker partial fill; the hedge is for the filled quantity only.
    Print 1.7 at t=5.0: 0.5 burns the queue, 1.2 fills. Hedge sell 1.2 @99.0 (lands 5.003). fees: -1.2x100x0.5/1e4 = -0.006; 1.2x99x5/1e4 = +0.0594
    -> 0.0534. cash bybit -120 + 0.006 = -119.994; binance +118.8 - 0.0594 = +118.7406. Position +1.2 / -1.2. The other 0.8 rests until its lifetime
    ends: placed 0.003 + ttl 10 = cancel requested 10.003, effective 10.053. At t=12: nothing resting, still exactly one hedge, position unchanged."""

    def test_scenario(self):
        h = started()
        h.sell_trade(5.0, BYBIT, volume=1.7)
        h.advance(5.01)
        self.assertEqual(fills(h), [(BYBIT, "buy", 1.2, 100.0), (BINANCE, "sell", 1.2, 99.0)])
        self.assertPosition(h, 1.2, -1.2)
        self.assertCash(h, -119.994, 118.7406, 0.0534)
        self.assertAlmostEqual(h.order().remaining, 0.8, places=9)               # still resting
        h.advance(12.0)
        self.assertEqual(h.resting, [])
        self.assertEqual(h.bt.hedge["hedges_sent"], 1)
        self.assertEqual(len(fills(h)), 2)
        self.assertPosition(h, 1.2, -1.2)
        h.assert_ledger_reconciles(self)


class S03MakerNeverFills(Base):
    """3. The maker never fills: no hedge, no position.
    A sell print of 0.4 at 100 burns 0.4 of the 0.5 queue: no fill. Expiry at 10.003 / 10.053. At t=12: no fills, no hedge sent, position 0 / 0,
    fees 0, cash 0 / 0, orders sent 1."""

    def test_scenario(self):
        h = started()
        h.sell_trade(5.0, BYBIT, volume=0.4)
        h.advance(12.0)
        self.assertEqual(fills(h), [])
        self.assertEqual((h.bt.orders_sent, h.bt.hedge["hedges_sent"]), (1, 0))
        self.assertEqual(h.resting, [])
        self.assertPosition(h, 0.0, 0.0)
        self.assertCash(h, 0.0, 0.0, 0.0)

    def _vanish(self, evidence):
        """3b. Depth only, NO print at all: the level we rest on (size 3 at 100, nothing ahead of us beyond it) goes 3 -> 0 -> 2 -> 0. Rule (CLAUDE.md): a fill needs a real
        trade, so expected: no fill, no hedge, position 0 / 0, whatever the evidence setting. FIRST-RUN FINDING (C9): in the legacy setting (fill_evidence "off", legacy_v0
        and, for snapshot shrinks, v1) the second removal fills 0.15 x 2.0 = 0.3 and the hedge follows. (A simple 3 -> 0 did not reproduce it: the first draft of this
        scenario passed in both modes and proved nothing.)"""
        h = Harness(ttl=0.0, queue=0.0)
        h.bt.maker.config = dataclasses.replace(h.bt.maker.config, fill_evidence=evidence)
        h.book(0.0, BYBIT, bids=((100.0, 3.0), (99.0, 5.0)), asks=((101.0, 5.0), (102.0, 5.0)))
        h.book(0.0, BINANCE)
        h.send_maker(0.0)
        h.advance(0.003)
        for t, size in ((5.0, 0.0), (5.5, 2.0), (6.0, 0.0)):
            h.event(t, DELTA, BYBIT, {"side": "bid", "price": 100.0, "volume": size, "event_type": "update"})
        h.advance(7.0)
        return h

    def test_3b_depth_only_with_trade_confirm_does_not_fill(self):
        h = self._vanish("trade_confirm")                                         # the v1.1 setting
        self.assertEqual(fills(h), [])
        self.assertPosition(h, 0.0, 0.0)

    @unittest.expectedFailure
    def test_3b_target_depth_only_never_fills_in_the_legacy_setting_either(self):
        h = self._vanish("off")                                                   # the legacy_v0 setting
        self.assertEqual(fills(h), [])

    def test_3b_today_the_legacy_setting_fills_from_depth_alone(self):
        h = self._vanish("off")
        self.assertEqual(fills(h), [(BYBIT, "buy", 0.3, 100.0), (BINANCE, "sell", 0.3, 99.0)])
        self.assertPosition(h, 0.3, -0.3)


class S04HedgeRejectedRetryThenUnwind(Base):
    """4. The hedge is rejected, retried twice (backoff 0.25 s), then unwound.
    Fill at 5.0 (maker buy 2.0 @100, fee -0.01). Hedge sent 5.0, lands 5.003, rejected, known +1 ms (5.004). Retry 1 sent 5.254, lands 5.257, rejected,
    known 5.258. Retry 2 sent 5.508, lands 5.511, rejected, known 5.512. Unwind (sell 2.0 on bybit) sent 5.512, lands 5.515: walks bybit's bid 99.0.
    Counters: retries 2, rejected hedge attempts 3, unwinds 1. Fills: maker buy 2.0 @100 bybit; unwind sell 2.0 @99.0 bybit (taker 5.5 bps: 2x99x5.5/1e4 =
    0.1089). Position 0 / 0. fees -0.01 + 0.1089 = 0.0989. cash bybit -200 + 0.01 + 198 - 0.1089 = -2.0989; binance 0. No hedge fill on binance."""

    def test_scenario(self):
        h = Harness(ttl=10.0, faults=fh(reject_prob=1.0, response="retry_unwind", max_retries=2, retry_backoff_s=0.25))
        h.book(0.0, BYBIT); h.book(0.0, BINANCE); h.send_maker(0.0); h.advance(0.003)
        h.sell_trade(5.0, BYBIT, volume=2.5)
        mark(h, 5.0)
        t = 5.003
        while t < 6.0:
            walk(h, [t]); t += 0.01
        c = h.bt.hedge
        self.assertEqual((c["retries_sent"], c["failed_fault_rejected"], c["unwinds_sent"]), (2, 3, 1))
        self.assertEqual(fills(h), [(BYBIT, "buy", 2.0, 100.0), (BYBIT, "sell", 2.0, 99.0)])
        self.assertPosition(h, 0.0, 0.0)
        self.assertCash(h, -2.0989, 0.0, 0.0989)
        self.assertEqual(h.bt.hedge_events[0]["outcome"], "unwound")
        h.assert_ledger_reconciles(self)


class S05LostAckThenGhostFillAtReconnect(Base):
    """5. Demonstration 1 (hedge recovery under ambiguity): the maker partly fills (1.2 of 2.0), the hedge goes to binance during a gateway outage
    4.0-10.0 with outage_policy "ghost": the strategy hears nothing and gives up after ack_timeout 2.0 s, but the order is delivered at reconnect.
    Hand timeline: maker fill 5.0 (bybit +1.2); hedge sent 5.0 into the outage; written off 7.0; reconnect 10.0 (+3 ms), the hedge executes at 99.0.
    REQUIREMENT-CORRECT answer (OMS03 / Demonstration 1): the strategy must query before replacing, so exactly ONE hedge order ever exists. Final: bybit +1.2,
    binance -1.2, net 0; no unwind sent; no ghost over-hedge; and from 5.0 to 10.0 the pending exposure counts the 1.2 on binance (no understated reserve). The run uses the
    two exposure switches the v1 and v1.1 profiles make MANDATORY (count_pending_exposure, count_unknown_exposure): with the Harness defaults (both off, the legacy setting) the
    reserve at 6.0 and 7.5 is EMPTY, i.e. understated; with them on it holds 1.2 sells on binance until the ghost lands (checked on the first run).
    TODAY the code writes the hedge off at 7.0, unwinds the maker fill (unwinds_sent 1, sell 1.2 on bybit @99), then the ghost lands and sells 1.2 on
    binance: net -1.2, a short nobody asked for (the open ghost defect). `test_today_*` pins that; `test_target_*` is the requirement."""

    def _run(self, **arb_kw):
        arb_kw = {"count_pending_exposure": True, "count_unknown_exposure": True, **arb_kw}        # the profile's mandatory switches
        h = Harness(ttl=10.0, faults=fh(outages=(Outage(BINANCE, 4.0, 10.0),), outage_policy="ghost", response="unwind", ack_timeout_s=2.0), **arb_kw)
        h.book(0.0, BYBIT); h.book(0.0, BINANCE); h.send_maker(0.0); h.advance(0.003)
        mark(h, 0.003)
        h.sell_trade(5.0, BYBIT, volume=1.7)
        mark(h, 5.0)
        walk(h, [5.003, 7.5])
        mid = (h.bt.inventory.net_base, h.bt.hedge["unwinds_sent"], h.bt._pending_exposure().buys, h.bt._pending_exposure().sells)
        walk(h, [10.003, 10.01])
        return h, mid

    @unittest.expectedFailure
    def test_target_one_hedge_flat_after_reconnect(self):
        h, mid = self._run()
        self.assertAlmostEqual(dict(mid[3]).get(BINANCE, 0.0), 1.2, places=9)           # reserve not understated while the hedge is unresolved
        self.assertEqual(h.bt.hedge["unwinds_sent"], 0)
        self.assertPosition(h, 1.2, -1.2)
        self.assertAlmostEqual(h.bt.inventory.net_base, 0.0, places=9)
        self.assertEqual(h.bt.hedge["ghost_fill_qty"], 0.0)

    def test_today_with_the_legacy_exposure_settings_the_reserve_is_understated(self):
        h, mid = self._run(count_pending_exposure=False, count_unknown_exposure=False)
        self.assertEqual(dict(mid[3]), {})                                          # 1.2 live on binance, reserve shows nothing

    def test_today_the_ghost_over_hedges(self):
        h, mid = self._run()
        self.assertAlmostEqual(dict(mid[3]).get(BINANCE, 0.0), 1.2, places=9)           # with the mandatory switches the reserve IS held (the half that works today)
        self.assertEqual(h.bt.hedge["unwinds_sent"], 1)
        self.assertAlmostEqual(mid[0], 0.0, places=9)                              # unwound at 7.0: flat before the ghost lands
        self.assertPosition(h, 0.0, -1.2)
        self.assertEqual((h.bt.hedge["ghost_fills"], round(h.bt.hedge["ghost_fill_qty"], 9)), (1, 1.2))
        self.assertAlmostEqual(h.bt.inventory.net_base, -1.2, places=9)


class S06CancelRace(Base):
    """6. Cancel race: a fill arrives after the cancel request but before it takes effect.
    Resting since 0.003, ttl 10: cancel requested 10.003, effective 10.053. A sell print 2.5 at 10.03 (inside that 50 ms) fills all 2.0 @100 (kept, the
    order is still live); hedge sell 2.0 @99 binance sent 10.03, lands 10.033. A second print 2.5 at 10.06 (after the cancel took effect) fills nothing.
    Final: bybit +2.0, binance -2.0; fills 2 (not 3); fees 0.089; cash as scenario 1; lifetime counters: fills_while_cancel_pending 1, qty 2.0."""

    def test_scenario(self):
        h = started()
        h.advance(10.003)
        self.assertIsNotNone(h.order().cancel_requested_ts)
        h.sell_trade(10.03, BYBIT, volume=2.5)
        h.advance(10.04)
        h.sell_trade(10.06, BYBIT, volume=2.5)
        h.advance(10.1)
        self.assertEqual(fills(h), [(BYBIT, "buy", 2.0, 100.0), (BINANCE, "sell", 2.0, 99.0)])
        self.assertPosition(h, 2.0, -2.0)
        self.assertCash(h, -199.99, 197.901, 0.089)
        lt = h.lifetime()
        self.assertEqual((lt["fills_while_cancel_pending"], lt["qty_filled_while_cancel_pending"]), (1, 2.0))


class S07PostOnlyRejectedAtArrival(Base):
    """7. PostOnly rejected at arrival: a buy at 101.0 reaches the exchange (t=0.013) when bybit's best ask is 101.0 -> rejected, never rests.
    The strategy hears one notice later (0.05 s): slot occupied until 0.063. Expected: no fill, no queue position, no hedge, orders sent 1, position 0/0,
    fees 0, cash 0; rejects counted 1 (buy side); a trade at the rejected price afterwards still fills nothing."""

    def test_scenario(self):
        h = po_harness()
        two_books(h, bids=((99.0, 5.0), (98.0, 5.0)), asks=((101.0, 5.0), (102.0, 5.0)))
        po_send(h, "buy", 101.0)
        poke(h, 0.03)
        self.assertEqual(h.resting, [])
        self.assertEqual((h.bt.po["rejects"], h.bt.po["rejects_buy"]), (1, 1))
        self.assertEqual(h.live(0.03), 1)                                          # slot held until the notice
        h.advance(0.07)
        self.assertEqual(h.live(0.07), 0)
        h.event(1.0, TRADE, BYBIT, {"price": 101.0, "volume": 5.0, "side": "b"})
        h.advance(1.1)
        self.assertEqual(fills(h), [])
        self.assertEqual((h.bt.orders_sent, h.bt.hedge["hedges_sent"]), (1, 0))
        self.assertPosition(h, 0.0, 0.0)
        self.assertCash(h, 0.0, 0.0, 0.0)


class S08FundingAcrossSettlement(Base):
    """8. Funding across a settlement with a position open on both legs (scenario 1 position: bybit +2.0, binance -2.0).
    Records at t=6.0: bybit rate 0.0001 mark 100 -> payment = +2.0 x 100 x 0.0001 = +0.02 (the long pays: cash -0.02); binance rate 0.0003 mark 100 ->
    payment = -2.0 x 100 x 0.0003 = -0.06 (the short receives: cash +0.06). funding_paid = 0.02 - 0.06 = -0.04 (a net receipt).
    cash bybit -199.99 - 0.02 = -200.01; binance 197.901 + 0.06 = 197.961; total cash -2.089 + 0.04 = -2.049. Settled once: a second call at 7.0 adds nothing.
    A flat account at the same settlement would pay nothing."""

    def test_scenario(self):
        h = started()
        h.sell_trade(5.0, BYBIT, volume=2.5)
        h.advance(5.01)
        d = tempfile.mkdtemp()
        (Path(d) / "f.jsonl").write_text("\n".join(json.dumps(r) for r in (
            {"venue": "BYBIT", "funding_ts_ms": 6000, "rate": "0.0001", "mark_price": "100.0", "settlement_history": True},
            {"venue": "BINANCE", "funding_ts_ms": 6000, "rate": "0.0003", "mark_price": "100.0", "settlement_history": True})) + "\n")
        s = FundingSettler(d, (BYBIT, BINANCE))
        s.settle(6.0, h.bt.inventory, h.bt.books)
        s.settle(7.0, h.bt.inventory, h.bt.books)
        self.assertAlmostEqual(s.funding_paid, -0.04, places=9)
        self.assertAlmostEqual(h.bt.inventory.cash[BYBIT], -200.01, places=9)
        self.assertAlmostEqual(h.bt.inventory.cash[BINANCE], 197.961, places=9)
        self.assertAlmostEqual(h.bt.inventory.total_cash, -2.049, places=9)
        self.assertEqual(len(s.settlements), 2)
        self.assertPosition(h, 2.0, -2.0)


class S09RiskLimitRefusesASignal(Base):
    """9. A risk limit refuses a signal: nothing changes. Venue cap = net_ceiling_multiple 1.0 x max_position_base 1.0 = 1.0 base; the signal is 2.0 with
    risk_reservation "enforce" -> refused as venue_cap:bybit. Expected: signals_refused_by_risk 1; orders sent 0; no maker order; no reservation held;
    positions 0/0; fees 0; cash 0; the layer counted one refusal and opened no intent."""

    def test_scenario(self):
        h = Harness(ttl=10.0, max_position_base=1.0, net_ceiling_multiple=1.0, cfg_kw={"execution": ExecutionConfig(risk_reservation="enforce")})
        h.book(0.0, BYBIT); h.book(0.0, BINANCE)
        h.send_maker(1.0)
        h.advance(2.0)
        self.assertEqual(h.bt.hedge["signals_refused_by_risk"], 1)
        self.assertEqual(h.bt.orders_sent, 0)
        self.assertEqual(h.resting, [])
        self.assertEqual(h.bt.xl.reservations.open_total(), 0.0)
        self.assertEqual(h.bt.xl.counts["risk_refusals"], 1)
        self.assertEqual(h.bt.xl.intents, {})
        self.assertPosition(h, 0.0, 0.0)
        self.assertCash(h, 0.0, 0.0, 0.0)


class S10BreakerTripsWithPositionOpen(Base):
    """10. The breaker trips with a position open. 2 maker orders may rest (max_live 2); the first fills 2.0 @100 at 5.0; the hedge is rejected (response "none":
    carried). consecutive_failures 1 -> the breaker trips when the failure is known (5.004). Expected TODAY: tripped, cause names the failure; the second
    resting maker (bid @99.5, still resting) gets a cancel request from the trip handler (counter 1); no new signal is accepted; the position is NOT
    flattened (bybit +2.0, binance 0, net +2.0 carried): `flatten on trip` and `pause vs trip` are not built (Phase E). Fees -0.01 (maker only); cash bybit -199.99."""

    def test_today(self):
        h = Harness(ttl=100.0, max_live=2, faults=fh(reject_prob=1.0, response="none"), cfg_kw={"circuit_breaker": CircuitBreakerConfig(consecutive_failures=1)})
        h.book(0.0, BYBIT); h.book(0.0, BINANCE)
        h.send_maker(0.0, price=100.0)
        h.send_maker(0.0, price=99.5)
        h.advance(0.003)
        mark(h, 0.003)
        h.sell_trade(5.0, BYBIT, volume=2.5)
        mark(h, 5.0)
        for t in (5.003, 5.01, 5.02, 5.5):                                         # what run() does after every event: advance, poll the breaker, handle a trip
            h.advance(t); mark(h, t); h.bt._breaker_poll(t); h.bt._breaker_trip_check(t)
        self.assertTrue(h.bt._breaker.is_tripped())
        self.assertEqual(len(h.bt._breaker.trips), 1)
        self.assertEqual(h.bt.hedge["resting_orders_cancel_requested_on_trip"], 1)
        self.assertFalse(h.bt._breaker.allow_signal(6.0, abs(h.bt.inventory.net_base)))
        self.assertEqual(fills(h), [(BYBIT, "buy", 2.0, 100.0)])
        self.assertPosition(h, 2.0, 0.0)
        self.assertCash(h, -199.99, 0.0, -0.01)

    @unittest.skip("NOT_EXPRESSIBLE today: flatten on trip and breaker pause are built in Phase E (plan section 4)")
    def test_target_trip_flattens_the_open_position(self):
        raise AssertionError


class S11MarginStressToLiquidation(Base):
    """11. Margin stress to liquidation (tiers: <=1000 notional 10% initial / 5% maintenance; bybit capital 30, binance 1000; liquidation fee 10 bps).
    Position as scenario 1. At t=10 bybit's book falls to bids 80/79, asks 81/82: mark = mid 80.5; bybit equity = 30 + (-199.99) + 2.0 x 80.5 = -8.99 < maintenance
    161 x 5% = 8.05 -> liquidate: sell 2.0 on bybit at its bid 80.0 (+160), liquidation fee 2 x 80 x 10/1e4 = 0.16; the matching quantity is flattened on binance:
    buy 2.0 @ ask 101.0 (-202) with taker fee 2 x 101 x 5/1e4 = 0.101. Positions 0 / 0. cash bybit -199.99 + 160 - 0.16 = -40.15; binance 197.901 - 202 - 0.101 = -4.2.
    One liquidation, one flatten; liquidation_fees 0.16. The account is left with total cash -44.35 (a realised loss, and bybit alone is insolvent).
    FIRST-RUN CORRECTION: the liquidation fill ALSO pays the venue taker fee (5.5 bps x 160 = 0.088), by design: "the liquidation fee rides on the fill itself (taker fee +
    liquidation fee)" (backtester._margin_step). So cash bybit = -199.99 + 160 - 0.16 - 0.088 = -40.238 and total cash = -40.238 - 4.2 = -44.438. Whether a real venue charges both
    is a Phase B question (matrix A10): the core's LiquidationEngine charges "a liquidation fee on the notional"."""

    def test_scenario(self):
        h = Harness(cfg_kw={"margin": margin_cfg(bybit_capital=30.0, binance_capital=1_000.0, fee_bps=10.0)})
        hedged_position(h)
        h.book(10.0, BYBIT, bids=((80.0, 5.0), (79.0, 5.0)), asks=((81.0, 5.0), (82.0, 5.0)))
        h.bt._margin_step(10.0)
        m = h.bt.margin_model
        self.assertEqual((m.stats["liquidations"], m.stats["flattened_after_liquidation"]), (1, 1))
        self.assertEqual(fills(h)[-2:], [(BYBIT, "sell", 2.0, 80.0), (BINANCE, "buy", 2.0, 101.0)])
        self.assertAlmostEqual(m.stats["liquidation_fees"], 0.16, places=9)
        self.assertPosition(h, 0.0, 0.0)
        self.assertAlmostEqual(h.bt.inventory.cash[BYBIT], -40.238, places=9)      # first-run correction: taker fee 0.088 on top of the liquidation fee
        self.assertAlmostEqual(h.bt.inventory.cash[BINANCE], -4.2, places=9)
        self.assertAlmostEqual(h.bt.inventory.total_cash, -44.438, places=9)

    @unittest.skip("NOT_EXPRESSIBLE today: a position above the last tier is a BREACH in the core and is simply kept by the backtester (A9, Phase B)")
    def test_target_position_above_the_last_tier_breaches(self):
        raise AssertionError


# ── Phase 0, second part: hedge-failure cases from the five reports that existing tests pin only partly (position and counters, not fees and cash) ──────
class S16DelayedHedgeMissesWhenTheBookMoves(Base):
    """16. A hedge sent late (hedge_failure_study D1/D5). Every hedge takes 1.0 s extra flight (delay_prob 1, response unwind). Maker fill 2.0 @100 at 5.0 (fee -0.01).
    (a) Book unchanged: the hedge lands at 5.0 + 0.003 + 1.0 = 6.003 and sells 2.0 @99.0 (fee 0.099): flat, fees 0.089, cash -2.089, net open for 1.003 s.
    (b) Binance's bids fall to 98/97 at 5.5, below the hedge limit 99.0 less 5 bps = 98.9505: at 6.003 the hedge finds nothing (counted failed_price_moved 1), the strategy
    learns 1 ms later and unwinds: sell 2.0 on bybit @99.0 (taker 5.5 bps: 2 x 99 x 5.5/1e4 = 0.1089). Final: position 0/0; fills maker buy 2.0 @100 and unwind sell 2.0 @99
    on bybit; fees -0.01 + 0.1089 = 0.0989; cash bybit -199.99 + 198 - 0.1089 = -2.0989, binance 0."""

    def _run(self, move):
        h = Harness(ttl=10.0, faults=fh(delay_prob=1.0, extra_delay_s=1.0, response="unwind"))
        h.book(0.0, BYBIT); h.book(0.0, BINANCE); h.send_maker(0.0); h.advance(0.003)
        mark(h, 0.003)
        h.sell_trade(5.0, BYBIT, volume=2.5)
        mark(h, 5.0)
        walk(h, [5.003])
        if move:
            h.book(5.5, BINANCE, bids=((98.0, 5.0), (97.0, 5.0)), asks=((101.0, 5.0), (102.0, 5.0)))
        t = 6.003
        while t < 6.4:
            walk(h, [t]); t += 0.002
        return h

    def test_a_book_unchanged(self):
        h = self._run(False)
        self.assertEqual(fills(h), [(BYBIT, "buy", 2.0, 100.0), (BINANCE, "sell", 2.0, 99.0)])
        self.assertPosition(h, 2.0, -2.0)
        self.assertCash(h, -199.99, 197.901, 0.089)
        self.assertAlmostEqual(h.bt.exposure.longest_net_nonzero_s, 1.003, places=3)

    def test_b_book_moves_through_the_limit(self):
        h = self._run(True)
        self.assertEqual(h.bt.hedge["failed_price_moved"], 1)
        self.assertEqual(fills(h), [(BYBIT, "buy", 2.0, 100.0), (BYBIT, "sell", 2.0, 99.0)])
        self.assertPosition(h, 0.0, 0.0)
        self.assertCash(h, -2.0989, 0.0, 0.0989)
        self.assertEqual(h.bt.hedge_events[0]["outcome"], "unwound")


class S17PartialHedgeFill(Base):
    """17. The hedge itself fills only half (partial_prob 1, fraction 0.5), the rest fails like a rejection; response unwind. Maker fill 2.0 @100 (fee -0.01). Hedge sell 1.0 @99.0
    on binance (fee 1 x 99 x 5/1e4 = 0.0495); the unhedged 1.0 is unwound by selling 1.0 on bybit @99.0 (fee 1 x 99 x 5.5/1e4 = 0.05445). Position bybit 2.0 - 1.0 = +1.0, binance -1.0,
    net 0. Fees -0.01 + 0.0495 + 0.05445 = 0.09395. Cash bybit -199.99 + 99 - 0.05445 = -101.04445; binance +99 - 0.0495 = 98.9505; total -2.09395."""

    def test_scenario(self):
        h = Harness(ttl=10.0, faults=fh(partial_prob=1.0, partial_fraction=0.5, response="unwind"))
        h.book(0.0, BYBIT); h.book(0.0, BINANCE); h.send_maker(0.0); h.advance(0.003)
        mark(h, 0.003)
        h.sell_trade(5.0, BYBIT, volume=2.5)
        mark(h, 5.0)
        t = 5.003
        while t < 5.3:
            walk(h, [t]); t += 0.002
        self.assertEqual(fills(h), [(BYBIT, "buy", 2.0, 100.0), (BINANCE, "sell", 1.0, 99.0), (BYBIT, "sell", 1.0, 99.0)])
        self.assertPosition(h, 1.0, -1.0)
        self.assertCash(h, -101.04445, 98.9505, 0.09395)
        self.assertAlmostEqual(h.bt.hedge_events[0]["unwound_qty"], 1.0, places=9)
        h.assert_ledger_reconciles(self)


class S18OutageBlackholeThenUnwind(Base):
    """18. The hedge is sent into a binance gateway outage 4.0-10.0 with policy blackhole: the order is lost, the strategy hears nothing and acts after ack_timeout 2.0 (at 7.0),
    response unwind. Maker fill 2.0 @100 at 5.0.
    TODAY: unwind sent 7.0, lands 7.003: sell 2.0 on bybit @99.0 (fee 0.1089). Net open from 5.0 to 7.003 = 2.003 s. The hedge never traded. Final: position 0/0, fills maker buy
    2.0 @100 and unwind sell 2.0 @99 (bybit); fees 0.0989; cash bybit -2.0989, binance 0. Pinned by test_today_*.
    REQUIREMENT (OMS03, matrix H08): on a timeout, query the existing order before issuing a replacement. A blackholed order and a ghost look the same to the strategy at 7.0 (no
    answer), and the unwind at 7.0 is exactly what S05 shows going wrong when the order was only delayed. So the requirement-correct state at 7.5 is: no unwind sent yet, the
    maker fill still open (bybit +2.0, binance 0), the 2.0 still reserved. `test_target_*` asserts that; it fails today. (Added after review: the first form of this scenario
    asserted the blind unwind as the expected result, which contradicted the split rule above.)"""

    def _run(self, until):
        h = Harness(ttl=10.0, faults=fh(outages=(Outage(BINANCE, 4.0, 10.0),), outage_policy="blackhole", response="unwind", ack_timeout_s=2.0))
        h.book(0.0, BYBIT); h.book(0.0, BINANCE); h.send_maker(0.0); h.advance(0.003)
        mark(h, 0.003)
        h.sell_trade(5.0, BYBIT, volume=2.5)
        mark(h, 5.0)
        walk(h, [5.003, 6.9])
        self.assertAlmostEqual(h.bt.inventory.net_base, 2.0, places=9)
        self.assertEqual(h.bt.hedge["unwinds_sent"], 0)                            # still waiting for the acknowledgement
        walk(h, until)
        return h

    def test_today_unwind_after_the_ack_timeout_without_a_query(self):
        h = self._run([7.0, 7.003, 7.01])
        self.assertEqual(h.bt.hedge["unwinds_sent"], 1)
        self.assertEqual(fills(h), [(BYBIT, "buy", 2.0, 100.0), (BYBIT, "sell", 2.0, 99.0)])
        self.assertPosition(h, 0.0, 0.0)
        self.assertCash(h, -2.0989, 0.0, 0.0989)
        self.assertAlmostEqual(h.bt.exposure.longest_net_nonzero_s, 2.003, places=3)

    @unittest.expectedFailure
    def test_target_no_unwind_before_the_hedge_state_has_been_queried(self):
        h = self._run([7.0, 7.003, 7.01, 7.5])
        self.assertEqual(h.bt.hedge["unwinds_sent"], 0)
        self.assertPosition(h, 2.0, 0.0)


class HedgeOnANonValidBook(Base):
    """Phase B data check 6: does the hedge path check the book state? TODAY, pinned. enforce_book_state is on. The maker fills 2.0 at 5.0; neither book has been updated since 0.0, so
    the hedge venue's book is 5 s old (max_book_age_s 0.5: stale) when the hedge arrives at 5.003.
    WITHOUT the fault layer (faults None) the taker arrival checks the book: it misses (reason book_not_valid) and the legacy instant unwind flattens the maker fill on bybit at once
    (sell 2.0 @99): net 0, but the hedge never traded, and nothing waited or retried.
    WITH the fault layer (the fault path returns before that check) the hedge executes on the stale book (sell 2.0 on binance @99). The rule for a hedge refused on a bad book
    (wait, retry or unwind) is Phase C; this pins the gap."""

    def _run(self, faults):
        h = Harness(ttl=10.0, faults=faults, cfg_kw={"enforce_book_state": True})
        h.book(0.0, BYBIT); h.book(0.0, BINANCE); h.send_maker(0.0); h.advance(0.003)
        h.sell_trade(5.0, BYBIT, volume=2.5)
        walk(h, [5.003, 5.01, 5.5])
        return h

    def test_today_without_faults_the_hedge_is_refused_on_a_stale_book_and_the_fill_is_flattened_at_once(self):
        h = self._run(None)
        self.assertEqual(dict(h.bt.miss_reasons), {"book_not_valid": 1})
        self.assertEqual([(r["venue"], r["side"], r["qty"]) for r in h.bt.trade_log if r["taker"]], [(BYBIT, "sell", 2.0)])        # the instant unwind, not a hedge
        self.assertAlmostEqual(h.bt.inventory.net_base, 0.0, places=9)

    def test_today_with_faults_the_hedge_does_not_look_at_the_book_state(self):
        h = self._run(fh())
        self.assertEqual(dict(h.bt.miss_reasons), {})
        self.assertEqual([(r["venue"], r["side"], r["qty"]) for r in h.bt.trade_log if r["taker"]], [(BINANCE, "sell", 2.0)])      # executed on the 5 s old book
        self.assertPosition(h, 2.0, -2.0)


class S05S18QueryBeforeReplace(Base):
    """Phase B, v1_2 (HedgeFaultConfig.query_before_replace, OMS03): the two targets S05 and S18 pin as expected failures, with the switch on. A hedge sent into a gateway outage
    4.0-10.0 gets no answer; at the ack timeout (7.0) the strategy queries instead of replacing. The query is answered unknown_resolve_s (3.0) later and not before the gateway
    is back (10.0 + 3 ms) and, for a ghost, not before it has landed.
    GHOST (S05 data, maker fill 1.2 at 5.0): no unwind at 7.0; the ghost executes at 10.003 and the query at 10.004 finds it done: bybit +1.2, binance -1.2, net 0, unwinds 0.
    BLACKHOLE (S18 data, maker fill 2.0): no unwind at 7.5 (position +2.0 / 0, the 2.0 still reserved); the query at 10.004 finds nothing and the normal ladder runs: one unwind."""

    def _ghost(self):
        h = Harness(ttl=10.0, faults=fh(outages=(Outage(BINANCE, 4.0, 10.0),), outage_policy="ghost", response="unwind", ack_timeout_s=2.0, unknown_resolve_s=3.0,
                                        query_before_replace=True), count_pending_exposure=True, count_unknown_exposure=True)
        h.book(0.0, BYBIT); h.book(0.0, BINANCE); h.send_maker(0.0); h.advance(0.003)
        mark(h, 0.003)
        h.sell_trade(5.0, BYBIT, volume=1.7)
        mark(h, 5.0)
        walk(h, [5.003, 7.5])
        return h

    def test_ghost_is_found_executed_by_the_query_and_nothing_is_replaced(self):
        h = self._ghost()
        self.assertEqual(h.bt.hedge["unwinds_sent"], 0)
        self.assertAlmostEqual(h.bt.inventory.net_base, 1.2, places=9)                           # still waiting for the query
        walk(h, [10.003, 10.01, 10.5])
        self.assertEqual((h.bt.hedge["unwinds_sent"], h.bt.hedge["query_found_executed"], h.bt.hedge["query_not_found"]), (0, 1, 0))
        self.assertPosition(h, 1.2, -1.2)
        self.assertAlmostEqual(h.bt.inventory.net_base, 0.0, places=9)

    def test_blackhole_is_not_unwound_before_the_query_and_is_after_it_finds_nothing(self):
        h = Harness(ttl=10.0, faults=fh(outages=(Outage(BINANCE, 4.0, 10.0),), outage_policy="blackhole", response="unwind", ack_timeout_s=2.0, unknown_resolve_s=3.0,
                                        query_before_replace=True))
        h.book(0.0, BYBIT); h.book(0.0, BINANCE); h.send_maker(0.0); h.advance(0.003)
        mark(h, 0.003)
        h.sell_trade(5.0, BYBIT, volume=2.5)
        mark(h, 5.0)
        walk(h, [5.003, 6.9, 7.0, 7.003, 7.01, 7.5])
        self.assertEqual(h.bt.hedge["unwinds_sent"], 0)
        self.assertPosition(h, 2.0, 0.0)
        walk(h, [10.003, 10.01, 10.5])
        self.assertEqual((h.bt.hedge["query_not_found"], h.bt.hedge["unwinds_sent"]), (1, 1))
        self.assertPosition(h, 0.0, 0.0)

    def test_the_switch_is_off_by_default_and_then_nothing_changes(self):
        self.assertFalse(fh().query_before_replace)
        self.assertNotIn("query_before_replace", fh().as_dict())


class S19LostAckOnTheHedgeIsResolvedByQueryNotRetry(Base):
    """19. The hedge reaches binance and fills, but its acknowledgement is lost (ack_unknown_prob 1, scope hedge, ack_timeout 2.0, unknown_resolve 3.0, response retry_unwind).
    Maker fill at 5.0; the hedge lands and fills at 5.003: sell 2.0 @99.0 (fee 0.099). The strategy cannot tell: the order is UNKNOWN until 5.003 + 2.0 + 3.0 = 10.003. Requirement
    (OMS03): query, never resend. Expected: no retry, no unwind, exactly 2 orders ever sent (maker + hedge), unknown_resolved 0 at 9.9 and 1 at 10.1, signal fully hedged.
    Position +2.0 / -2.0; fees 0.089; cash -199.99 / +197.901. This one the backtester already gets right (its UNKNOWN path queries); S05 is the case it gets wrong."""

    def test_scenario(self):
        h = Harness(ttl=30.0, faults=fh(ack_unknown_prob=1.0, ack_unknown_scope="hedge", ack_timeout_s=2.0, unknown_resolve_s=3.0, response="retry_unwind"))
        h.book(0.0, BYBIT); h.book(0.0, BINANCE); h.send_maker(0.0); h.advance(0.003)
        mark(h, 0.003)
        h.sell_trade(5.0, BYBIT, volume=2.5)
        mark(h, 5.0)
        walk(h, [5.003, 5.01, 9.9])
        self.assertEqual((h.bt.hedge["retries_sent"], h.bt.hedge["unwinds_sent"], h.bt.hedge["unknown_resolved"]), (0, 0, 0))
        self.assertEqual(fills(h), [(BYBIT, "buy", 2.0, 100.0), (BINANCE, "sell", 2.0, 99.0)])
        walk(h, [10.1])
        self.assertEqual(h.bt.hedge["unknown_resolved"], 1)
        self.assertEqual((h.bt.orders_sent, h.bt.signals_fully_hedged), (2, 1))
        self.assertPosition(h, 2.0, -2.0)
        self.assertCash(h, -199.99, 197.901, 0.089)


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
class S12RestartWithAnOpenPosition(unittest.TestCase):
    """12. Restart with an open position. NOT_EXPRESSIBLE in the backtester: it keeps all state in memory and has no store or restart (SIM_ONLY). Expressed in the
    core: BINANCE fills its child (buy 0.010 BTC @ 60000.00 = 600.00 notional) at once; BYBIT's adapter stays silent (the sell child is SENT, no answer). The process
    ends. A new OMS opens the same plan store. Requirement (SDK05 / OMS03): the plan is recovered open (PARTIAL: one child filled; my first draft said DISPATCHED), the filled child keeps 0.010 @ 60000, the
    unanswered child is not re-sent (unique requests 0 on the new router), and the plan's reservation is held again (reserved gross > 0). The recovered ledger
    shows the BINANCE position 0.010 base (BUY fill rebuilt from the store)."""

    def test_recovery_without_duplicate_send(self):
        from crypto_stat_arb.execution import DeterministicVenueAdapter, StrategyDirectedRouter
        from crypto_stat_arb.ledger import Ledger
        from crypto_stat_arb.models import OrderState, PlanState
        from crypto_stat_arb.oms import OMS
        from crypto_stat_arb.plan_store import ParentPlanStore
        from test_restart_resilience import books, master, opportunity, risk

        class Silent:
            def handle(self, request, book):
                return ()

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "plans.sqlite"
            store = ParentPlanStore(path)
            oms1 = OMS(master(), risk(), Ledger(), router=StrategyDirectedRouter(master(), adapters={"BINANCE": DeterministicVenueAdapter("BINANCE"), "BYBIT": Silent()}),
                       plan_store=store)
            plan = oms1.execute(opportunity(), books())
            parent_id = plan.parent_id
            binance_child = next(c for c in plan.children if c.venue == "BINANCE")
            self.assertEqual((binance_child.state, binance_child.filled_quantity), (OrderState.FILLED, Decimal("0.010")))
            store.close()                                                        # the process ends here

            store2 = ParentPlanStore(path)
            router2 = StrategyDirectedRouter(master(), adapters={"BINANCE": Silent(), "BYBIT": Silent()})
            ledger2 = Ledger()
            risk2 = risk()
            oms2 = OMS(master(), risk2, ledger2, router=router2, plan_store=store2)
            rec = oms2.plans[parent_id]
            self.assertEqual(rec.state, PlanState.PARTIAL)                              # first-run correction: one child filled
            child = next(c for c in rec.children if c.venue == "BINANCE")
            self.assertEqual((child.state, child.filled_quantity, child.average_price), (OrderState.FILLED, Decimal("0.010"), Decimal("60000.00")))
            again = oms2.execute(opportunity(), books())
            self.assertEqual(again.parent_id, parent_id)
            self.assertEqual(router2.stats_snapshot()["unique_requests"], 0)       # nothing re-sent
            self.assertGreater(risk2.reserved_gross, Decimal("0"))                  # the package is reserved again
            fills_seen = [e for e in ledger2.events if e["event_type"] == "FILL"]
            self.assertEqual([(e["venue"], e["side"], str(e["fill_quantity"])) for e in fills_seen], [("BINANCE", "BUY", "0.010")])
            store2.close()


class S13TradeDepthOrderIndependence(unittest.TestCase):
    """13. Demonstration 3: a print and the matching depth update arrive in either order, with two own orders at one level.
    Level 100 shows 10 (external). A (size 5) and B (size 5) join the same level. The feed then shows 20 (10 external + 10 ours). A sell print of 20 at 100 and the depth
    update to 0 arrive: (i) print then depth, (ii) depth then print. One trade budget: 20 = 10 external burned + 5 (A) + 5 (B) -> A and B fill 5.0 each, total 10.0,
    queue ahead 0 for both, remaining 0. The depth update must add no fill and no second charge, so (i) and (ii) give identical fills and queue state.
    Core Python engine, used as the reference for 'one budget'. Today (D2) the executor charges the common queue to each order: see test_today. The C++ engine loaded in the
    parity tests (checked on review: load_cpp_module() returns the .so, no error), so those tests compare against it too."""

    @staticmethod
    def _run(adapter, order):
        adapter.set_level("bid", 100.0, 10.0)
        adapter.place("A", "bid", 100.0, 5.0)
        adapter.place("B", "bid", 100.0, 5.0)
        adapter.set_level("bid", 100.0, 20.0)
        f = []
        if order == "trade_first":
            f += adapter.on_trade(100.0, 20.0, "SELL")
            adapter.set_level("bid", 100.0, 0.0)
        else:
            adapter.set_level("bid", 100.0, 0.0)
            f += adapter.on_trade(100.0, 20.0, "SELL")
        return sorted((o, round(q, 9)) for o, q in f), round(adapter.remaining("A"), 9), round(adapter.remaining("B"), 9)

    def test_target_core_engine_is_order_independent_and_uses_one_budget(self):
        from crypto_stat_arb.maker_engine import PythonQueueEngine
        from test_maker_executor_engine_parity import CoreAdapter
        a = self._run(CoreAdapter(PythonQueueEngine()), "trade_first")
        b = self._run(CoreAdapter(PythonQueueEngine()), "depth_first")
        self.assertEqual(a, b)
        self.assertEqual(a, ([("A", 5.0), ("B", 5.0)], 0.0, 0.0))

    @unittest.expectedFailure
    def test_target_executor_matches_the_one_budget_answer_in_both_orders(self):
        a = self._run(ExecutorAdapter(cancel_model="all_ahead"), "trade_first")
        b = self._run(ExecutorAdapter(cancel_model="all_ahead"), "depth_first")
        self.assertEqual(a, b)
        self.assertEqual(a, ([("A", 5.0), ("B", 5.0)], 0.0, 0.0))

    def test_today_the_v1_1_maker_block_is_order_dependent_too(self):
        """Checked on review: not only the parity-test configuration. With the maker block of perp_perp_maker_taker_v1_1 (proportional cancels, trade_confirm, dedupe of print
        against depth, 50 ms credits) the answer is the same two observations as TODAY_S13. Latent in v1.1 runs: maker_max_live_orders is 1 there, so two own orders never share a
        level; it becomes reachable as soon as more than one live order is allowed."""
        from new_backtest.fill_engine import MakerExecutor
        from new_backtest.fill_model import EmpiricalFillModel
        from new_backtest.profiles import apply_profile, load_profile
        doc = load_profile(Path(__file__).resolve().parents[1] / "profiles" / "perp_perp_maker_taker_v1_1.yaml")
        cfg = apply_profile(doc, data_root="/unused", pair="SOLUSDT", start_date="2024-03-08", end_date="2024-03-08", output_dir="/unused", funding_rate_dir=None)
        self.assertEqual(cfg.arb.maker_max_live_orders, 1)
        got = []
        for order in ("trade_first", "depth_first"):
            a = ExecutorAdapter(cancel_model="all_ahead")
            a.ex = MakerExecutor(EmpiricalFillModel(), dataclasses.replace(cfg.maker, min_queue_ahead=0.0))
            got.append(self._run(a, order))
        self.assertEqual(tuple(got), TODAY_S13)

    def test_today_executor_observation_is_recorded(self):
        """Not an assertion about correctness: pins what the executor does today so Phase B/D changes show up (recorded on the first run)."""
        a = self._run(ExecutorAdapter(cancel_model="all_ahead"), "trade_first")
        b = self._run(ExecutorAdapter(cancel_model="all_ahead"), "depth_first")
        self.assertEqual((a, b), TODAY_S13)


# First run, executor: print first -> A fills 5.0 and B gets nothing (B charged the common queue again); depth first -> both fill 5.0. The answer depends on the order of the two events.
TODAY_S13 = ((([("A", 5.0)], 0.0, 5.0)), (([("A", 5.0), ("B", 5.0)], 0.0, 0.0)))


class S14StaleBooksAndTimers(Base):
    """14. Demonstration 4: both books stop updating with no relative skew while a risk/cancel deadline expires between market messages.
    Timing model on (fixed 6 ms entry, 6 ms cancel, 2 ms reports), ttl 10. Both books updated at 0.5 and then silent. Maker sent 1.0 (lands 1.006). max_book_age 1.0
    by default -> at t=2.0 both books are 1.5 s old: stale on both, ENTRY BLOCKED (entry_blocked_both_stale counted), independent of relative skew (0). The
    lifetime deadline is a timer: with the next market event not before t=100, the cancel is requested at 11.006 and effective at 11.012 (not at 100) and the order is gone;
    timers_fired_between_events counts arrival, request and effective (3). Position 0/0, no fills."""

    def test_scenario(self):
        tc = TimingConfig(venues={v: VenueTimelines(entry=TimelineSpec(6.0), ack_report=TimelineSpec(2.0), fill_report=TimelineSpec(2.0), cancel=TimelineSpec(6.0),
                                                   cancel_report=TimelineSpec(2.0), feed=TimelineSpec(2.0)) for v in (BYBIT, BINANCE)}, equal_time_policy="market_first")
        h = Harness(ttl=10.0, cfg_kw={"timing": tc})
        h.book(0.5, BYBIT); h.book(0.5, BINANCE)
        h.send_maker(1.0)
        h.bt._fire_timers_before(2.0)
        self.assertTrue(h.bt._dead_feed_blocks(2.0))
        self.assertEqual(h.bt.timing_stats["entry_blocked_both_stale"], 1)
        order = h.order()
        h.bt._fire_timers_before(100.0)
        self.assertAlmostEqual(order.cancel_requested_ts, 1.006 + 10.0, places=9)
        self.assertAlmostEqual(order.cancel_effective_ts, 11.006 + 0.006, places=9)
        self.assertEqual(h.resting, [])
        self.assertEqual(h.bt.timing_stats["timers_fired_between_events"], 3)
        self.assertEqual(fills(h), [])
        self.assertPosition(h, 0.0, 0.0)


class S15CrashBetweenPersistedAndSent(unittest.TestCase):
    """15. OMS03: the process dies after the dispatch intent of a child is durable but before the order reaches the venue. (An existing test,
    tests/test_restart_resilience.py::test_process_restart_recovers_open_orders_without_duplicate_dispatch, raises an ordinary exception from the adapter; the
    process survives and the failure is recorded as ambiguous. This scenario raises a BaseException the OMS does not catch, so no ambiguity record is written: a kill.)
    Script: BINANCE's adapter raises Kill on the first request. Hand expectation after the kill: the store holds the plan, state DISPATCHED, with the BINANCE child
    dispatch-started and not sent, the BYBIT child never dispatched; no fill; the reservation is held.
    After a restart (new OMS, same store) the requirement is: (1) the plan is recovered open; (2) nothing is sent by merely starting or by re-executing the same
    opportunity (unique requests 0); (3) before any replacement the OMS queries the venue for each non-terminal child (query_order called for the BINANCE child, with the venue
    answering 'unknown to venue'); (4) the reservation stays held while the child is unresolved; (5) a replacement send is allowed only after the query answered. (5) needs
    the deferred send path of Phase C and is NOT_EXPRESSIBLE today. Items 1, 2 and 4 are asserted; 3 is recorded as the target.
    Open behaviour noted for Phase C: after the restart the BYBIT child was never sent and re-executing does not send it, so the package stays half-dispatched (BINANCE
    unresolved, BYBIT never sent) until the queries time out; nothing decides what to do with the second leg."""

    def _kill_and_restart(self):
        from crypto_stat_arb.execution import StrategyDirectedRouter
        from crypto_stat_arb.ledger import Ledger
        from crypto_stat_arb.oms import OMS
        from crypto_stat_arb.plan_store import ParentPlanStore
        from test_restart_resilience import books, master, opportunity, risk

        class Kill(BaseException):
            pass

        class Dies:
            def handle(self, request, book):
                raise Kill()

        class Counting:
            def __init__(self):
                self.calls = 0

            def handle(self, request, book):
                self.calls += 1
                return ()

        d = tempfile.TemporaryDirectory()
        path = Path(d.name) / "plans.sqlite"
        store = ParentPlanStore(path)
        oms1 = OMS(master(), risk(), Ledger(), router=StrategyDirectedRouter(master(), adapters={"BINANCE": Dies(), "BYBIT": Counting()}), plan_store=store)
        with self.assertRaises(Kill):
            oms1.execute(opportunity(), books())
        parent_id = next(iter(oms1.plans))
        events = [e["event_type"] for e in store.events(parent_id)]
        store.close()
        store2 = ParentPlanStore(path)
        b, y = Counting(), Counting()
        router2 = StrategyDirectedRouter(master(), adapters={"BINANCE": b, "BYBIT": y})
        risk2 = risk()
        oms2 = OMS(master(), risk2, Ledger(), router=router2, plan_store=store2)
        return d, store2, oms2, router2, risk2, parent_id, events, (b, y)

    def test_today_kill_between_persist_and_send(self):
        from crypto_stat_arb.models import PlanState
        from test_restart_resilience import books, opportunity
        d, store2, oms2, router2, risk2, parent_id, events, (b, y) = self._kill_and_restart()
        try:
            self.assertIn("CHILD_DISPATCH_STARTED", events)                          # the intent was durable
            self.assertNotIn("CHILD_DISPATCH_AMBIGUOUS", [e for e in events])         # a kill leaves no ambiguity record
            rec = oms2.plans[parent_id]
            self.assertEqual(rec.state, PlanState.DISPATCHED)                         # (1) recovered open
            self.assertEqual(sum(c.filled_quantity for c in rec.children), Decimal("0"))
            again = oms2.execute(opportunity(), books())                              # (2) no send on re-execution
            self.assertEqual(again.parent_id, parent_id)
            self.assertEqual((b.calls, y.calls, router2.stats_snapshot()["unique_requests"]), (0, 0, 0))
        finally:
            store2.close(); d.cleanup()

    def test_reservation_held_and_venue_queried_before_any_replacement(self):
        """FIRST-RUN CORRECTION: I expected this to fail. It passes: after the restart both children are re-armed (2 deadlines), check_order_deadlines queries the venue for each,
        nothing is re-sent (adapter calls 0), the reservation of 1200.60 stays held and the kill switch is not tripped."""
        from test_restart_resilience import books
        d, store2, oms2, router2, risk2, parent_id, events, _ = self._kill_and_restart()
        try:
            self.assertGreater(risk2.reserved_gross, Decimal("0"))                    # (4)
            queried = []
            original = router2.query_order
            router2.query_order = lambda venue, order_id, ts: (queried.append((venue, order_id)) or original(venue, order_id, ts))
            self.assertEqual(len(oms2._report_deadlines), 2)                          # re-armed from the store, no event needed
            n = oms2.check_order_deadlines(10 ** 12)                                  # (3)
            self.assertEqual(sorted(v for v, _ in queried), ["BINANCE", "BYBIT"])
            self.assertEqual(n, 2)
            self.assertEqual(risk2.reserved_gross, Decimal("1200.60000"))             # still held: the children are unresolved
            self.assertFalse(risk2.kill_switch)
        finally:
            store2.close(); d.cleanup()


if __name__ == "__main__":
    unittest.main()
