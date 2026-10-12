"""Worst-case legging reservation, aggregate residual bound and marked plan PnL.

Live paper cycles ended holding 0.003-0.007 BTC of unhedged base against a
0.002 ``max_residual_base``: every orphan leg (0.001) was "within bound" on
its own, none was unwound, and each was booked as ~78 USDT of cash PnL.
"""

from dataclasses import replace
from decimal import Decimal
import unittest

from crypto_stat_arb.execution import ResidualPolicy, StrategyDirectedRouter
from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.models import ParentPlan, PlanState
from crypto_stat_arb.oms import OMS
from crypto_stat_arb.risk import RiskEngine, RiskLimits
from test_execution_realism import books
from test_oms import RejectingAdapter, instrument_master, opportunity


def limits(**overrides) -> RiskLimits:
    values = dict(
        max_order_notional=Decimal("5000"), max_gross_notional=Decimal("10000"),
        max_residual_base=Decimal("0.02"), max_book_age_ns=1_000_000_000,
    )
    values.update(overrides)
    return RiskLimits(**values)


def nth(n: int, qty: str = "0.01"):
    return replace(opportunity(qty), opportunity_id=f"op-{n}", source_event_ids=(f"a{n}", f"b{n}"))


class WorstCaseReservationTests(unittest.TestCase):
    def test_balanced_package_reserves_its_full_leg_quantity(self):
        engine = RiskEngine(limits(max_legging_exposure_base=Decimal("0.015")))
        first = engine.reserve(nth(1))
        self.assertEqual(first.residual_base, Decimal("0.01"))
        with self.assertRaisesRegex(ValueError, "max legging exposure exceeded"):
            engine.reserve(nth(2))
        engine.release(first)
        self.assertEqual(engine.reserve(nth(2)).residual_base, Decimal("0.01"))

    def test_carried_residual_counts_against_legging_exposure(self):
        engine = RiskEngine(limits(max_legging_exposure_base=Decimal("0.015")))
        engine.carried_residual_base = Decimal("-0.006")
        with self.assertRaisesRegex(ValueError, "max legging exposure exceeded"):
            engine.reserve(nth(1))

    def test_new_risk_is_blocked_while_carried_residual_is_at_the_limit(self):
        engine = RiskEngine(limits(max_residual_base=Decimal("0.002")))
        engine.carried_residual_base = Decimal("0.002")
        with self.assertRaisesRegex(ValueError, "carried residual at limit"):
            engine.reserve(nth(1, "0.001"))


class AggregateResidualBoundTests(unittest.TestCase):
    def oms(self, cap: str) -> OMS:
        return OMS(
            instrument_master(), RiskEngine(limits(max_residual_base=Decimal(cap))), Ledger(),
            router=StrategyDirectedRouter(instrument_master(), adapters={"BYBIT": RejectingAdapter()}),
            residual_policy=ResidualPolicy(
                max_residual_base=Decimal(cap), max_residual_age_ns=0,
                hedge_venue_priority=("BINANCE",),
            ),
        )

    def test_residuals_within_bound_one_at_a_time_cannot_pile_up(self):
        oms, current = self.oms("0.02"), books()
        first = oms.execute(nth(1), current)
        self.assertEqual((first.state, first.residual_base), (PlanState.CLOSED, Decimal("0.01")))
        self.assertEqual(oms.carried_residual_base(), Decimal("0.01"))
        # Alone, 0.01 < 0.02 would be "within bound"; on top of the carried
        # 0.01 the total would reach the cap, so this plan must unwind.
        second = oms.execute(nth(2), current)
        self.assertEqual((second.state, second.residual_base), (PlanState.CLOSED, Decimal("0")))
        self.assertTrue(any(order.order_role == "UNWIND" for order in second.children))
        self.assertEqual(oms.carried_residual_base(), Decimal("0.01"))
        self.assertEqual(oms.risk.carried_residual_base, Decimal("0.01"))
        self.assertEqual(oms.residual_risk_snapshot()["carried_residual_base"], "0.01")
        self.assertEqual(oms.residual_risk_snapshot()["total_residual_base"], "0.01")

    def test_a_residual_that_offsets_the_carried_one_is_accepted(self):
        oms, current = self.oms("0.015"), books()
        oms.execute(nth(1), current)  # carries +0.01
        same_side = ParentPlan(parent_id="x", opportunity=nth(9), children=[], residual_base=Decimal("0.01"))
        offsetting = ParentPlan(parent_id="y", opportunity=nth(9), children=[], residual_base=Decimal("-0.01"))
        self.assertFalse(oms._residual_within_bound(same_side))
        self.assertTrue(oms._residual_within_bound(offsetting))


class MarkedPlanPnlTests(unittest.TestCase):
    def test_open_residual_is_marked_not_booked_as_cash(self):
        oms = AggregateResidualBoundTests().oms("0.02")
        current = books()
        plan = oms.execute(nth(1), current)
        self.assertEqual(plan.residual_base, Decimal("0.01"))
        # Only the BINANCE buy filled: cash -600.00. Before marking, this was
        # the plan's "PnL"; now the 0.01 BTC held is valued at the mid.
        mid = sum(
            (book.best_bid + book.best_ask) / 2 for book in current.values()
        ) / len(current)
        self.assertEqual(plan.residual_mark_price, mid)
        self.assertEqual(plan.residual_mark_value, Decimal("0.01") * mid)
        self.assertEqual(plan.net_pnl, plan.gross_pnl + plan.residual_mark_value - plan.fees)
        self.assertLess(abs(plan.net_pnl), Decimal("1"))
        self.assertLess(plan.gross_pnl, Decimal("-599"))

    def test_hedged_plan_pnl_is_unchanged(self):
        oms = OMS(instrument_master(), RiskEngine(limits()), Ledger())
        plan = oms.execute(nth(1), books())
        self.assertEqual(plan.residual_base, Decimal("0"))
        self.assertEqual(plan.residual_mark_value, Decimal("0"))
        self.assertEqual(plan.net_pnl, plan.gross_pnl - plan.fees)


if __name__ == "__main__":
    unittest.main()
