"""OMS unwind exhaustion: when every unwind attempt is spent and the residual is still out of bound, the OMS raises a persistent
UNWIND_EXHAUSTED_ALARM (once per plan, ledger + plan store), refuses new risk until an operator resets it, and never closes the plan
as a success. The alarm survives a restart."""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.execution import ResidualPolicy, StrategyDirectedRouter, UnwindUrgency
from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.models import PlanState
from crypto_stat_arb.oms import OMS
from crypto_stat_arb.plan_store import ParentPlanStore
from test_oms import RejectingAdapter, instrument_master, make_book, opportunity, risk


def books():
    return {("BINANCE", "BTC-USDT-SPOT"): make_book("BINANCE", "59999.90", "60000.00"),
            ("BYBIT", "BTC-USDT-SPOT"): make_book("BYBIT", "60060.00", "60060.10")}


def make_oms(ledger, attempts=1, plan_store=None, hold=False):
    return OMS(instrument_master(), risk(), ledger, plan_store=plan_store,
               router=StrategyDirectedRouter(instrument_master(), adapters={"BYBIT": RejectingAdapter()}),
               residual_policy=ResidualPolicy(max_residual_base=Decimal("0.002"), max_residual_age_ns=1,
                                              hedge_venue_priority=("BYBIT",), unwind_urgency=UnwindUrgency.PASSIVE,
                                              max_unwind_attempts=attempts, hold_new_risk_on_breach=hold))


def kinds(ledger, kind):
    return [e for e in ledger.events if e["event_type"] == kind]


class UnwindExhaustedAlarmTests(unittest.TestCase):
    def test_exhausted_unwinds_raise_one_persistent_alarm_and_the_plan_is_not_a_success(self):
        ledger = Ledger()
        oms = make_oms(ledger, attempts=2)
        plan = oms.execute(opportunity(), books())                     # one unwind venue: one attempt per residual check
        self.assertEqual(plan.unwind_attempts, 1)
        self.assertEqual(kinds(ledger, "UNWIND_EXHAUSTED_ALARM"), [])   # an attempt is left: no alarm yet
        oms.enforce_residual_limits(5_000, books())
        self.assertEqual(plan.unwind_attempts, 2)
        self.assertEqual(plan.state, PlanState.HEDGE_UNWIND)            # not CLOSED: the residual is still open
        self.assertGreater(abs(plan.residual_base), Decimal("0.002"))
        alarms = kinds(ledger, "UNWIND_EXHAUSTED_ALARM")
        self.assertEqual(len(alarms), 1)
        self.assertEqual((alarms[0]["unwind_attempts"], alarms[0]["attempts_allowed"]), (2, 2))
        oms.enforce_residual_limits(10_000, books())                   # later checks do not raise it again
        oms.enforce_residual_limits(20_000, books())
        self.assertEqual(len(kinds(ledger, "UNWIND_EXHAUSTED_ALARM")), 1)
        self.assertIn(plan.parent_id, oms.unwind_exhausted_alarms)

    def test_the_alarm_refuses_new_risk_until_an_operator_resets_it(self):
        ledger = Ledger()
        oms = make_oms(ledger)
        first = oms.execute(opportunity(), books())
        held = oms.execute(replace(opportunity(), opportunity_id="op-2"), books())
        self.assertEqual((held.state, held.terminal_outcome), (PlanState.CLOSED, "REJECTED"))
        self.assertEqual(len(kinds(ledger, "NEW_RISK_HELD_UNWIND_EXHAUSTED")), 1)
        with self.assertRaises(ValueError):
            oms.reset_unwind_exhausted_alarm(30_000, operator="")
        self.assertEqual(oms.reset_unwind_exhausted_alarm(30_000, operator="desk"), [first.parent_id])
        self.assertEqual(oms.unwind_exhausted_alarms, {})
        self.assertEqual(kinds(ledger, "UNWIND_EXHAUSTED_ALARM_RESET")[0]["operator"], "desk")
        oms.execute(replace(opportunity(), opportunity_id="op-3"), books())
        # the alarm no longer holds new risk (pre-trade limits may still refuse it: the first residual is still open)
        self.assertEqual(len(kinds(ledger, "NEW_RISK_HELD_UNWIND_EXHAUSTED")), 1)

    def test_no_alarm_while_attempts_remain_or_when_the_unwind_succeeds(self):
        ledger = Ledger()
        ok = OMS(instrument_master(), risk(), ledger, residual_policy=ResidualPolicy(
            max_residual_base=Decimal("0"), max_residual_age_ns=100, hedge_venue_priority=("BINANCE",),
            unwind_urgency=UnwindUrgency.PASSIVE, max_unwind_attempts=2),
            router=StrategyDirectedRouter(instrument_master(), adapters={"BYBIT": RejectingAdapter()}))
        ok.execute(opportunity(), books())
        ok.enforce_residual_limits(1_000, books())
        self.assertEqual(kinds(ledger, "UNWIND_EXHAUSTED_ALARM"), [])
        self.assertEqual(ok.unwind_exhausted_alarms, {})

    def test_the_alarm_survives_a_restart_and_its_reset_does_too(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "plans.sqlite"
            store = ParentPlanStore(path)
            oms = make_oms(Ledger(), plan_store=store)
            plan = oms.execute(opportunity(), books())
            store.close()
            store = ParentPlanStore(path)
            recovered = make_oms(Ledger(), plan_store=store)
            self.assertIn(plan.parent_id, recovered.unwind_exhausted_alarms)
            recovered.reset_unwind_exhausted_alarm(50_000, operator="desk")
            store.close()
            store = ParentPlanStore(path)
            self.assertEqual(make_oms(Ledger(), plan_store=store).unwind_exhausted_alarms, {})
            store.close()


if __name__ == "__main__":
    unittest.main()
