"""Track B Item 1: hedge-fault injection via QueueAwareVenueAdapter.fault_config.

Tests:
- reject_prob=0  → 0 rejections
- reject_prob=0.1 → some rejections, all are a strict subset of reject_prob=0.3 (monotone subset)
- reject_prob=0.3 → superset of 0.1 rejections
- static outage window → all hedges in window are rejected regardless of reject_prob
- outage outside window → no outage rejections
- hedge_faults_injected counter tracks reasons
- IOC market / GTC PostOnly orders are not injected (only IOC LIMIT)
"""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crypto_stat_arb.execution import (
    CanonicalOrderRequest, ExecutionEventType, OrderAction, OrderType, Side, TimeInForce,
)
from crypto_stat_arb.simulation.fill_engine import MakerConfig, MakerExecutor
from crypto_stat_arb.simulation.hedge_faults import HedgeFaultConfig, Outage
from crypto_stat_arb.simulation.queue_adapter import NullOrderEventSink, QueueAwareVenueAdapter

VENUE = "BINANCE"
INSTR = "SOL-USDT-PERP"
_NS = 1_000_000_000


def _make_adapter(fault_config: HedgeFaultConfig | None = None) -> QueueAwareVenueAdapter:
    executor = MakerExecutor(NullOrderEventSink(), MakerConfig(fill_evidence="trade_confirm"))
    return QueueAwareVenueAdapter(
        VENUE, executor, tick_size=0.01, maker_fee_bps=5.0, pair=INSTR,
        fault_config=fault_config,
    )


def _ioc_limit(order_id: str, ts_ns: int) -> CanonicalOrderRequest:
    return CanonicalOrderRequest.create(
        action=OrderAction.SUBMIT,
        order_id=order_id,
        parent_opportunity_id="opp-1",
        instrument_id=INSTR,
        venue=VENUE,
        side=Side.SELL,
        price=Decimal("150.00"),
        quantity=Decimal("1.0"),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.IOC,
        post_only=False,
        created_ts_ns=ts_ns,
    )


def _gtc_postonly(order_id: str, ts_ns: int) -> CanonicalOrderRequest:
    return CanonicalOrderRequest.create(
        action=OrderAction.SUBMIT,
        order_id=order_id,
        parent_opportunity_id="opp-1",
        instrument_id=INSTR,
        venue=VENUE,
        side=Side.SELL,
        price=Decimal("155.00"),
        quantity=Decimal("1.0"),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        post_only=True,
        created_ts_ns=ts_ns,
    )


_FAULT_REASON_PREFIXES = ("fault:", "outage:")


def _is_fault_reject(cb) -> bool:
    """True only when the callback is a REJECTED produced by our fault injection (not by missing book/liquidity)."""
    return (cb.event_type == ExecutionEventType.REJECTED
            and any((cb.reason or "").startswith(p) for p in _FAULT_REASON_PREFIXES))


def _send_n(adapter: QueueAwareVenueAdapter, n: int, start_ts_ns: int = 10 * _NS) -> list[tuple[str, bool]]:
    """Send n IOC limit orders and return list of (order_id, was_fault_rejected)."""
    out = []
    for i in range(n):
        req = _ioc_limit(f"ord-{i}", start_ts_ns + i * _NS)
        cbs = adapter.handle_at(req, None, start_ts_ns + i * _NS)
        fault = any(_is_fault_reject(c) for c in cbs)
        out.append((f"ord-{i}", fault))
    return out


class TestRejectProb(unittest.TestCase):
    """reject_prob drives how many hedges are rejected, with the monotone subset property."""

    def _rejected_ids(self, prob: float, n: int = 200) -> set:
        a = _make_adapter(HedgeFaultConfig(seed=42, reject_prob=prob))
        results = _send_n(a, n)
        return {oid for oid, fault in results if fault}

    def test_zero_prob_no_rejections(self):
        rejected = self._rejected_ids(0.0)
        self.assertEqual(len(rejected), 0)

    def test_nonzero_prob_causes_rejections(self):
        rejected_01 = self._rejected_ids(0.1)
        self.assertGreater(len(rejected_01), 0, "reject_prob=0.1 must reject at least one of 200 hedges")

    def test_higher_prob_causes_more_rejections(self):
        n = 300
        rejected_01 = self._rejected_ids(0.1, n)
        rejected_03 = self._rejected_ids(0.3, n)
        self.assertGreater(len(rejected_03), len(rejected_01))

    def test_monotone_subset_same_seed(self):
        """Every order rejected at prob=0.1 is also rejected at prob=0.3 (hash-based: p0 ⊆ p1 for p0 < p1)."""
        n = 500
        rejected_01 = self._rejected_ids(0.1, n)
        rejected_03 = self._rejected_ids(0.3, n)
        self.assertTrue(
            rejected_01.issubset(rejected_03),
            f"p=0.1 rejects not ⊆ p=0.3 rejects; extra in 0.1: {rejected_01 - rejected_03}",
        )

    def test_different_seeds_independent(self):
        """Different seeds produce different rejection sets."""
        n = 300
        a1 = _make_adapter(HedgeFaultConfig(seed=1, reject_prob=0.3))
        a2 = _make_adapter(HedgeFaultConfig(seed=2, reject_prob=0.3))
        r1 = {oid for oid, fault in _send_n(a1, n) if fault}
        r2 = {oid for oid, fault in _send_n(a2, n) if fault}
        # With high probability two independent draws differ; exact same set is astronomically unlikely
        self.assertNotEqual(r1, r2, "different seeds should produce different rejection sets")

    def test_fault_counter_tracks_rejections(self):
        a = _make_adapter(HedgeFaultConfig(seed=42, reject_prob=0.3))
        results = _send_n(a, 200)
        expected = sum(1 for _, fault in results if fault)
        got = a.hedge_faults_injected.get("fault:reject_prob", 0)
        self.assertEqual(got, expected)


class TestStaticOutage(unittest.TestCase):
    """Static outage windows reject all hedges whose arrival falls inside."""

    def _adapter_with_outage(self, start: float, end: float, policy: str = "reject") -> QueueAwareVenueAdapter:
        outage = Outage(VENUE, start, end, kind="gateway")
        fc = HedgeFaultConfig(outages=(outage,), outage_policy=policy)
        return _make_adapter(fc)

    def _ts(self, s: float) -> int:
        return int(s * _NS)

    def test_inside_window_rejected(self):
        a = self._adapter_with_outage(100.0, 200.0)
        req = _ioc_limit("ord-in", self._ts(150.0))
        cbs = a.handle_at(req, None, self._ts(150.0))
        self.assertEqual(cbs[0].event_type, ExecutionEventType.REJECTED)
        self.assertIn("outage", cbs[0].reason)

    def test_before_window_not_rejected(self):
        a = self._adapter_with_outage(100.0, 200.0, policy="reject")
        req = _ioc_limit("ord-before", self._ts(50.0))
        cbs = a.handle_at(req, None, self._ts(50.0))
        self.assertFalse(any(_is_fault_reject(c) for c in cbs),
                         "order before outage window must not be fault-rejected")

    def test_after_window_not_rejected(self):
        a = self._adapter_with_outage(100.0, 200.0, policy="reject")
        req = _ioc_limit("ord-after", self._ts(250.0))
        cbs = a.handle_at(req, None, self._ts(250.0))
        self.assertFalse(any(_is_fault_reject(c) for c in cbs),
                         "order after outage window must not be fault-rejected")

    def test_outage_counter_logged(self):
        a = self._adapter_with_outage(100.0, 200.0, policy="reject")
        req = _ioc_limit("ord-cnt", self._ts(150.0))
        a.handle_at(req, None, self._ts(150.0))
        total = sum(a.hedge_faults_injected.values())
        self.assertEqual(total, 1)

    def test_all_policies_map_to_rejected(self):
        for policy in ("reject", "blackhole", "queue"):                  # "ghost" is not mapped to REJECTED any more: it is held and executed at the reconnect (test_ghost_hedge_ceiling.py)
            with self.subTest(policy=policy):
                a = self._adapter_with_outage(100.0, 200.0, policy=policy)
                req = _ioc_limit("ord-pol", self._ts(150.0))
                cbs = a.handle_at(req, None, self._ts(150.0))
                self.assertEqual(cbs[0].event_type, ExecutionEventType.REJECTED,
                                 f"outage_policy={policy!r} must yield REJECTED in session runtime (parity difference)")

    def test_outage_plus_reject_prob_independent(self):
        """An order inside the outage is rejected regardless of reject_prob draw."""
        a = self._adapter_with_outage(100.0, 200.0)
        # Use reject_prob=0 to show outage alone fires
        a._fault_config = HedgeFaultConfig(outages=a._fault_config.outages, reject_prob=0.0, outage_policy="reject")
        req = _ioc_limit("ord-combo", self._ts(150.0))
        cbs = a.handle_at(req, None, self._ts(150.0))
        self.assertEqual(cbs[0].event_type, ExecutionEventType.REJECTED)


class TestNoInjectionOnNonHedgeOrders(unittest.TestCase):
    """Fault injection fires only for IOC LIMIT orders; GTC PostOnly and no-fault config are untouched."""

    def test_no_fault_config_no_injection(self):
        a = _make_adapter(fault_config=None)
        req = _ioc_limit("ord-nofault", 10 * _NS)
        # With no book, DeterministicVenueAdapter returns REJECTED "no executable liquidity" which becomes CANCELLED
        cbs = a.handle_at(req, None, 10 * _NS)
        for c in cbs:
            self.assertNotIn("fault:", c.reason or "")
            self.assertNotIn("outage:", c.reason or "")

    def test_gtc_postonly_not_injected(self):
        """GTC PostOnly (the maker leg) must never be fault-injected."""
        a = _make_adapter(HedgeFaultConfig(seed=42, reject_prob=1.0))
        req = _gtc_postonly("ord-maker", 10 * _NS)
        # With reject_prob=1.0, every IOC would be rejected; GTC PostOnly should not be.
        # (It goes to _place() which may be REJECTED for no-book or would-cross, but NOT for a fault.)
        cbs = a.handle_at(req, None, 10 * _NS)
        for c in cbs:
            self.assertNotIn("fault:", c.reason or "")


if __name__ == "__main__":
    unittest.main()
