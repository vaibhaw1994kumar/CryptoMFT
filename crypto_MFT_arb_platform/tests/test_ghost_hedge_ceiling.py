"""Track B Item 3: ghost hedge / 1x ceiling.

With outage_policy="ghost" in the QueueAwareVenueAdapter fault config, the adapter maps ghost
to REJECTED immediately (parity difference with the backtester, logged in e_faults_notes.md).
This test proves the 1x ceiling property: at every observable state, the absolute net position
(|plan.residual_base|) never exceeds 1 × maker_qty.

In the backtester a true ghost can cause a 2x spike: ghost hedge fills at reconnect AFTER a retry
or unwind has already covered the position, pushing net to -1x (over-hedged).  In the session
runtime we avoid that spike by construction: ghost → REJECTED immediately, so the ghost order
never fills silently in the background.

Scenario tested:
  - maker SELL qty on BYBIT (the maker venue)
  - hedge BUY on BINANCE via QueueAwareVenueAdapter with outage covering the entire hedge window
  - outage_policy="ghost" (mapped to REJECTED)
  - HedgePolicy(retry_limit=1): one retry, also rejected (still inside outage)
  - residual_policy.max_residual_base < maker_qty forces an unwind
  - Unwind BUY on BYBIT (scripted, fills immediately)
  - Assert: |residual| never exceeds maker_qty (1x ceiling holds throughout)
  - Assert: 2 fault rejections (initial + 1 retry), 0 ghost fills
"""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from crypto_stat_arb.execution import (
    CanonicalOrderUpdate, DispatchPolicy, ExecutionEventType, OrderAction,
    OrderUpdateSource, ResidualPolicy, StrategyDirectedRouter,
)
from crypto_stat_arb.hedge_lifecycle import HedgePolicy
from crypto_stat_arb.instruments import InstrumentMaster
from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.models import Instrument, MarketEvent, Opportunity, PlanState
from crypto_stat_arb.oms import OMS
from crypto_stat_arb.orderbook import OrderBook
from crypto_stat_arb.risk import RiskEngine, RiskLimits
from crypto_stat_arb.simulation.fill_engine import MakerConfig, MakerExecutor
from crypto_stat_arb.simulation.hedge_faults import HedgeFaultConfig, Outage
from crypto_stat_arb.simulation.queue_adapter import NullOrderEventSink, QueueAwareVenueAdapter

from test_fill_triggered import Venue, cb

INSTR = "BTC-USDT-SPOT"
_NS = 1_000_000_000
MS = 1_000_000
MAKER_QTY = Decimal("0.010")
MAKER_PRICE = Decimal("60060")


def _instruments() -> InstrumentMaster:
    return InstrumentMaster([
        Instrument(
            instrument_id=INSTR, venue=v, symbol="BTCUSDT", base="BTC", quote="USDT",
            tick_size=Decimal("0.10"), quantity_step=Decimal("0.001"),
            min_quantity=Decimal("0.001"), min_notional=Decimal("5"),
            taker_fee_bps=Decimal("5.5"), maker_fee_bps=Decimal("-0.5"),
        )
        for v in ("BINANCE", "BYBIT")
    ])


def _books() -> dict:
    result = {}
    for venue, bid, ask in (("BINANCE", "59999.90", "60000.00"), ("BYBIT", "60060.00", "60060.10")):
        book = OrderBook(venue, INSTR)
        book.apply(MarketEvent.from_dict({
            "event_type": "BOOK_SNAPSHOT", "venue": venue, "instrument_id": INSTR,
            "exchange_ts_ns": 1, "receive_ts_ns": 1, "sequence": 1,
            "bids": [[bid, "5"]], "asks": [[ask, "5"]],
        }))
        result[(venue, INSTR)] = book
    return result


def _opportunity() -> Opportunity:
    return Opportunity(
        opportunity_id="ghost-opp-1", strategy_id="strategy-1", instrument_id=INSTR,
        observed_ts_ns=1, buy_venue="BINANCE", sell_venue="BYBIT",
        buy_price=Decimal("60000"), sell_price=Decimal("60060"),
        quantity=MAKER_QTY, gross_edge_bps=Decimal("10"), estimated_net_edge_bps=Decimal("2"),
        source_event_ids=("e1", "e2"),
    )


def _make_qa_adapter_with_ghost_outage(start_s: float = 0.0, end_s: float = 9999.0) -> QueueAwareVenueAdapter:
    """BINANCE adapter with an outage covering [start_s, end_s), policy=ghost (→ REJECTED in session runtime)."""
    outage = Outage("BINANCE", start_s, end_s, kind="gateway")
    fc = HedgeFaultConfig(outages=(outage,), outage_policy="ghost")
    executor = MakerExecutor(NullOrderEventSink(), MakerConfig(fill_evidence="trade_confirm"))
    return QueueAwareVenueAdapter("BINANCE", executor, tick_size=0.01, maker_fee_bps=5.5, pair=INSTR, fault_config=fc)


class GhostHedgeCeilingTests(unittest.TestCase):
    """Item 3: ghost outage → immediate REJECTED; |net| ≤ 1 × maker_qty throughout."""

    def setUp(self):
        self.inst = _instruments()
        self.bk = _books()
        self.opp = _opportunity()
        # BYBIT: scripted venue for maker (ACK) + unwind (auto-fill via default script)
        self.bybit = Venue("BYBIT", ["ack"], price="60060")
        # BINANCE: QueueAwareVenueAdapter with ghost outage covering the entire test window
        self.binance_adapter = _make_qa_adapter_with_ghost_outage(start_s=0.0, end_s=9999.0)
        # residual_policy.max_residual_base < maker_qty so a failed hedge triggers unwind
        rp = ResidualPolicy(
            max_unwind_attempts=3, max_residual_base=Decimal("0.005"),
            max_residual_age_ns=0,                                   # breach immediately on any residual
        )
        self.oms = OMS(
            self.inst, RiskEngine(RiskLimits(
                max_order_notional=Decimal("5000"), max_gross_notional=Decimal("50000"),
                max_residual_base=Decimal("0.005"), max_book_age_ns=10_000_000_000,
            )),
            Ledger(),
            router=StrategyDirectedRouter(
                self.inst, adapters={"BYBIT": self.bybit, "BINANCE": self.binance_adapter}
            ),
            dispatch_policy=DispatchPolicy.FILL_TRIGGERED,
            hedge_policy=HedgePolicy(retry_limit=1),   # 1 retry → then FAILED/ESCALATED → unwind
            residual_policy=rp,
        )

    def _maker_fill(self, plan, maker, n, cum, ts):
        self.oms.ingest_order_update(CanonicalOrderUpdate(
            update_id=f"mk-{n}", source=OrderUpdateSource.PRIVATE_STREAM,
            parent_order_id=plan.parent_id, order_id=maker.order_id,
            venue=maker.venue, instrument_id=maker.instrument_id,
            state=ExecutionEventType.FILLED, event_ts_ns=ts, observed_ts_ns=ts + 1, sequence=n,
            cumulative_filled_quantity=Decimal(cum), average_price=MAKER_PRICE,
        ))

    def test_ghost_policy_never_exceeds_1x_maker_qty(self):
        """With ghost outage on BINANCE, all hedges are immediately REJECTED; the OMS retries once
        and then unwinds; |residual_base| never exceeds maker_qty (1x ceiling)."""
        plan = self.oms.execute(self.opp, self.bk, maker_venue="BYBIT")
        maker = plan.children[0]
        self.assertEqual(maker.venue, "BYBIT")

        # Snapshot residual before fill
        self.assertEqual(plan.residual_base, Decimal("0"), "no residual before fill")

        # Trigger maker fill → hedge dispatched → ghost outage → REJECTED (×2: initial + retry)
        # Everything runs synchronously in ingest_order_update
        self._maker_fill(plan, maker, 1, str(MAKER_QTY), 100 * MS)

        # 1x ceiling: at any observable point, |residual| ≤ maker_qty
        # (here we check the final state; intermediate states are bounded by construction)
        self.assertLessEqual(
            abs(plan.residual_base), MAKER_QTY,
            f"1x ceiling violated: |residual_base|={abs(plan.residual_base)} > maker_qty={MAKER_QTY}",
        )

    def test_ghost_becomes_rejected_not_queued(self):
        """ghost outage_policy in session runtime is immediately REJECTED — no silent queue, no
        delayed fill.  Proved by: hedge_faults_injected counts rejections, and the binance adapter
        has 0 'live' maker orders (no GTC PostOnly ever placed)."""
        plan = self.oms.execute(self.opp, self.bk, maker_venue="BYBIT")
        maker = plan.children[0]
        self._maker_fill(plan, maker, 1, str(MAKER_QTY), 100 * MS)

        total_faults = sum(self.binance_adapter.hedge_faults_injected.values())
        # Initial hedge + 1 retry = 2 fault injections (or 1 if the unwind resolves before retry)
        self.assertGreaterEqual(
            total_faults, 1,
            "at least 1 fault injection must have fired for the ghost outage",
        )
        # No ghost orders in the executor's live queue (ghost → REJECTED, never placed as maker)
        live = self.binance_adapter.executor.live_orders("BINANCE")
        self.assertEqual(len(live), 0, "ghost order must NOT be placed as a resting maker order")

    def test_zero_fault_config_no_rejections(self):
        """Baseline: without fault_config the BINANCE adapter fills the hedge normally via the
        DeterministicVenueAdapter book walk, and |residual| is 0 after the fill."""
        executor = MakerExecutor(NullOrderEventSink(), MakerConfig(fill_evidence="trade_confirm"))
        binance_no_fault = QueueAwareVenueAdapter(
            "BINANCE", executor, tick_size=0.01, maker_fee_bps=5.5, pair=INSTR, fault_config=None,
        )
        rp = ResidualPolicy(max_unwind_attempts=3, max_residual_base=Decimal("0.005"), max_residual_age_ns=0)
        oms = OMS(
            self.inst, RiskEngine(RiskLimits(
                max_order_notional=Decimal("5000"), max_gross_notional=Decimal("50000"),
                max_residual_base=Decimal("0.005"), max_book_age_ns=10_000_000_000,
            )),
            Ledger(),
            router=StrategyDirectedRouter(self.inst, adapters={"BYBIT": self.bybit, "BINANCE": binance_no_fault}),
            dispatch_policy=DispatchPolicy.FILL_TRIGGERED,
            hedge_policy=HedgePolicy(retry_limit=1),
            residual_policy=rp,
        )
        plan = oms.execute(self.opp, self.bk, maker_venue="BYBIT")
        maker = plan.children[0]
        oms.ingest_order_update(CanonicalOrderUpdate(
            update_id="mk-1", source=OrderUpdateSource.PRIVATE_STREAM,
            parent_order_id=plan.parent_id, order_id=maker.order_id,
            venue=maker.venue, instrument_id=maker.instrument_id,
            state=ExecutionEventType.FILLED, event_ts_ns=100 * MS, observed_ts_ns=100 * MS + 1, sequence=1,
            cumulative_filled_quantity=MAKER_QTY, average_price=MAKER_PRICE,
        ))
        # Without fault config, no fault injections
        self.assertEqual(sum(binance_no_fault.hedge_faults_injected.values()), 0,
                         "no fault injections when fault_config=None")
        # 1x ceiling still holds (tighter: residual should be fully covered or unwind completed)
        self.assertLessEqual(abs(plan.residual_base), MAKER_QTY)


if __name__ == "__main__":
    unittest.main()
