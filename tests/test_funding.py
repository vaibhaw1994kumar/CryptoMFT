"""ECO02 perpetual funding settlement: sign, timing, data quality and reconciliation."""

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.config import load_config
from crypto_stat_arb.execution import DeterministicVenueAdapter, StrategyDirectedRouter, TimeInForce
from crypto_stat_arb.economics import (
    EconomicEventType, EventSource, Valuation, opening_balance_events, reconcile,
)
from crypto_stat_arb.funding import (
    FundingDataConflict, FundingEngine, FundingRate, FundingRateBook, FundingSpec,
    funding_amount, is_perpetual,
)
from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.models import ChildOrder, Side
from crypto_stat_arb.runner import run_backtest
from test_execution_realism import INSTRUMENT as SPOT, books, delta, submit
from test_oms import instrument_master


ROOT = Path(__file__).parents[1]
PERP = "BTC-USDT-PERP"
HOUR = 3_600 * 1_000_000_000
T = 8 * HOUR                      # a settlement time
MARK = Decimal("60000")


def perp_ledger(opening_usdt: str = "10000") -> Ledger:
    ledger = Ledger()
    ledger.asset_resolver = lambda venue, instrument_id: (
        (instrument_id, "USDT") if instrument_id == PERP else (instrument_id.split("-")[0], "USDT")
    )
    for event in opening_balance_events([(v, "USDT", Decimal(opening_usdt)) for v in ("BINANCE", "BYBIT")]):
        ledger.economics.apply(event)
    return ledger


def spec(venue="BINANCE", interval_hours=8) -> FundingSpec:
    return FundingSpec(venue, PERP, contract_asset=PERP, settlement_asset="USDT",
                       interval_ns=interval_hours * HOUR)


def rate(ts, value, venue="BINANCE", mark=MARK, **kwargs) -> FundingRate:
    return FundingRate(venue, PERP, ts, Decimal(value), mark_price=mark, **kwargs)


def engine(ledger, rates=(), specs=None, **kwargs) -> FundingEngine:
    eng = FundingEngine(ledger, specs or [spec()], FundingRateBook().extend(rates),
                        settle_lag_ns=kwargs.pop("settle_lag_ns", 0), **kwargs)
    for s in eng.specs.values():
        eng.observe(s.venue, s.instrument_id, 1, MARK)  # session start
    return eng


def trade(ledger, order_id, side, qty, *, match_ts, report_ts=None, venue="BINANCE", price="60000"):
    quantity = Decimal(qty)
    order = ChildOrder(
        order_id=order_id, parent_id="p", opportunity_id="op", venue=venue, instrument_id=PERP,
        side=side, quantity=quantity, limit_price=Decimal(price), filled_quantity=quantity,
        average_price=Decimal(price),
        state_updates=[{"event_ts_ns": match_ts, "cumulative_filled_quantity": qty}],
    )
    ledger.apply_fill(order, match_ts if report_ts is None else report_ts)


def funding_events(ledger, venue="BINANCE"):
    return [e for e in ledger.economics.events if e.event_type == EconomicEventType.FUNDING and e.venue == venue]


def usdt(ledger, venue="BINANCE"):
    return ledger.economics.balance(venue, "USDT")


class FundingAmountTests(unittest.TestCase):
    def test_long_and_short_under_positive_and_negative_rates(self):
        # (position, rate, expected cash flow): longs pay positive funding, shorts receive it.
        cases = [("0.1", "0.0001", "-0.6"), ("0.1", "-0.0001", "0.6"),
                 ("-0.1", "0.0001", "0.6"), ("-0.1", "-0.0001", "-0.6")]
        for position, value, expected in cases:
            with self.subTest(position=position, rate=value):
                ledger = perp_ledger()
                trade(ledger, "o", Side.BUY if position[0] != "-" else Side.SELL, position.lstrip("-"),
                      match_ts=T - HOUR)
                cash_before = usdt(ledger)
                eng = engine(ledger, [rate(T, value)])
                eng.settle_until(T)
                [event] = funding_events(ledger)
                self.assertEqual(event.postings[0].quantity, Decimal(expected))
                self.assertEqual(event.postings[0].asset, "USDT")
                self.assertEqual(usdt(ledger) - cash_before, Decimal(expected))
                self.assertEqual(dict(event.details)["position"], position)
                self.assertEqual(event.effective, T)
                self.assertEqual(event.source, EventSource.OBSERVED)
                state = ledger.recalculate_account_state(mark_prices={("BINANCE", PERP): MARK})
                self.assertEqual(Decimal(state["venues"]["BINANCE"]["funding"]), Decimal(expected))

    def test_linear_and_inverse_formulas(self):
        # Linear: 2 contracts x 0.001 BTC x 60000 x 0.0001 = 0.012 USDT paid by the long.
        self.assertEqual(funding_amount(Decimal("2"), Decimal("0.0001"), MARK, multiplier=Decimal("0.001")),
                         Decimal("-0.0120000"))
        # Inverse: 600 contracts x 100 USD / 60000 x 0.0001 = 0.0001 BTC paid by the long.
        self.assertEqual(funding_amount(Decimal("600"), Decimal("0.0001"), MARK,
                                        multiplier=Decimal("100"), inverse=True), Decimal("-0.0001"))
        self.assertEqual(funding_amount(Decimal("-600"), Decimal("0.0001"), MARK,
                                        multiplier=Decimal("100"), inverse=True), Decimal("0.0001"))


class FundingTimingTests(unittest.TestCase):
    def test_position_opened_immediately_before_settlement_pays(self):
        ledger = perp_ledger()
        trade(ledger, "o", Side.BUY, "0.1", match_ts=T - 1)
        engine(ledger, [rate(T, "0.0001")]).settle_until(T)
        self.assertEqual(len(funding_events(ledger)), 1)

    def test_fill_matched_exactly_at_settlement_does_not_count(self):
        ledger = perp_ledger()
        trade(ledger, "o", Side.BUY, "0.1", match_ts=T)
        eng = engine(ledger, [rate(T, "0.0001")])
        eng.settle_until(T)
        self.assertEqual(funding_events(ledger), [])
        self.assertEqual(eng.report()["status"], "COMPLETE")

    def test_position_closed_before_settlement_pays_nothing(self):
        ledger = perp_ledger()
        trade(ledger, "open", Side.BUY, "0.1", match_ts=T - HOUR)
        trade(ledger, "close", Side.SELL, "0.1", match_ts=T - 1)
        engine(ledger, [rate(T, "0.0001")]).settle_until(T)
        self.assertEqual(funding_events(ledger), [])

    def test_position_matched_before_but_reported_after_settlement_is_rebooked(self):
        ledger = perp_ledger()
        trade(ledger, "o1", Side.BUY, "0.1", match_ts=T - HOUR)
        eng = engine(ledger, [rate(T, "0.0001")])
        eng.settle_until(T)
        [first] = funding_events(ledger)
        self.assertEqual(first.postings[0].quantity, Decimal("-0.6"))
        # A second buy matched before T, its report arriving after T was settled.
        trade(ledger, "o2", Side.BUY, "0.1", match_ts=T - 5, report_ts=T + 2_000_000)
        events = funding_events(ledger)
        reversals = [e for e in ledger.economics.events if e.event_type == EconomicEventType.REVERSAL]
        self.assertEqual([e.reverses for e in reversals], [first.event_id])
        self.assertEqual(events[-1].postings[0].quantity, Decimal("-1.2"))
        self.assertTrue(events[-1].idempotency_key.endswith("|r1"))
        self.assertEqual(sum(e.postings[0].quantity for e in events) + reversals[0].postings[0].quantity,
                         Decimal("-1.2"))
        self.assertEqual(eng.report()["rebooked_after_late_fills"], 1)
        state = ledger.recalculate_account_state(mark_prices={("BINANCE", PERP): MARK})
        self.assertEqual(Decimal(state["venues"]["BINANCE"]["funding"]), Decimal("-1.2"))

    def test_partial_reduction_pays_on_the_remaining_position(self):
        # Long 0.1, reduce to 0.04 before T: 0.04 x 60000 x 0.0001 = 0.24 paid.
        ledger = perp_ledger()
        trade(ledger, "open", Side.BUY, "0.1", match_ts=T - HOUR)
        trade(ledger, "reduce", Side.SELL, "0.06", match_ts=T - 10)
        engine(ledger, [rate(T, "0.0001")]).settle_until(T)
        [event] = funding_events(ledger)
        self.assertEqual(event.postings[0].quantity, Decimal("-0.24"))
        self.assertEqual(dict(event.details)["position"], "0.04")

    def test_several_late_fills_on_one_settlement_net_to_the_true_amount(self):
        # Booked at +0.1; then three late reports (matched before T) move the
        # position to +0.1 +0.05 -0.2 +0.01 = -0.04, which receives 0.24.
        ledger = perp_ledger()
        trade(ledger, "o1", Side.BUY, "0.1", match_ts=T - HOUR)
        eng = engine(ledger, [rate(T, "0.0001")])
        eng.settle_until(T)
        for n, (side, qty) in enumerate(((Side.BUY, "0.05"), (Side.SELL, "0.2"), (Side.BUY, "0.01"))):
            trade(ledger, f"late{n}", side, qty, match_ts=T - 100 + n, report_ts=T + 1_000 + n)
        events = funding_events(ledger)
        reversals = [e for e in ledger.economics.events if e.event_type == EconomicEventType.REVERSAL]
        net = sum(e.postings[0].quantity for e in events) + sum(r.postings[0].quantity for r in reversals)
        self.assertEqual(net, Decimal("0.24"))
        self.assertEqual(len(events), 4)               # r0 .. r3
        self.assertEqual(len(reversals), 3)            # every superseded revision reversed exactly once
        self.assertEqual(len({r.reverses for r in reversals}), 3)
        self.assertTrue(events[-1].idempotency_key.endswith("|r3"))
        state = ledger.recalculate_account_state(mark_prices={("BINANCE", PERP): MARK})
        self.assertEqual(Decimal(state["venues"]["BINANCE"]["funding"]), Decimal("0.24"))
        self.assertEqual(eng.report()["rebooked_after_late_fills"], 3)

    def test_late_fill_after_a_flat_settlement_books_it(self):
        ledger = perp_ledger()
        eng = engine(ledger, [rate(T, "0.0001")])
        eng.settle_until(T)
        self.assertEqual(funding_events(ledger), [])
        trade(ledger, "o", Side.SELL, "0.1", match_ts=T - 5, report_ts=T + 1)
        [event] = funding_events(ledger)
        self.assertEqual(event.postings[0].quantity, Decimal("0.6"))

    def test_resting_maker_fill_is_stamped_with_match_time_not_placement_time(self):
        # A GTC placed at T-1h rests through T and is filled by a book update at
        # T+1s: the position did not exist at T, so it pays no funding at T.
        adapter = DeterministicVenueAdapter("BINANCE")
        router = StrategyDirectedRouter(instrument_master(), adapters={"BINANCE": adapter})
        book = books()[("BINANCE", SPOT)]
        adapter.handle(submit("m1", "59995.00", "0.1", created=T - HOUR, tif=TimeInForce.GTC), book)
        book.apply(delta("BINANCE", 2, 5, [], [["60000.00", "0"], ["59995.00", "1"]]))
        self.assertEqual(router.on_book_update("BINANCE", book, T + 1_000_000_000), 1)
        [delivery] = router._deliveries
        self.assertEqual(delivery.callback.event_ts_ns, T + 1_000_000_000)
        self.assertEqual(adapter.query_order("m1").venue_ts_ns, T + 1_000_000_000)
        ledger = perp_ledger()
        trade(ledger, "m1", Side.BUY, "0.1", match_ts=delivery.callback.event_ts_ns)
        engine(ledger, [rate(T, "0.0001")]).settle_until(T + 2_000_000_000)
        self.assertEqual(funding_events(ledger), [])

    def test_settlement_waits_for_the_report_lag(self):
        ledger = perp_ledger()
        trade(ledger, "o", Side.BUY, "0.1", match_ts=T - HOUR)
        eng = engine(ledger, [rate(T, "0.0001")], settle_lag_ns=5_000_000_000)
        eng.settle_until(T + 1_000_000_000)
        self.assertEqual(funding_events(ledger), [])
        eng.settle_until(T + 5_000_000_000)
        self.assertEqual(len(funding_events(ledger)), 1)

    def test_multiple_periods(self):
        ledger = perp_ledger()
        trade(ledger, "o", Side.BUY, "0.1", match_ts=T - HOUR)
        eng = engine(ledger, [rate(T, "0.0001"), rate(T + 8 * HOUR, "0.0002"), rate(T + 16 * HOUR, "-0.0001")])
        eng.settle_until(T + 16 * HOUR)
        amounts = [e.postings[0].quantity for e in funding_events(ledger)]
        self.assertEqual(amounts, [Decimal("-0.6"), Decimal("-1.2"), Decimal("0.6")])
        self.assertEqual(Decimal(eng.report()["realized_by_venue"]["BINANCE"]), Decimal("-1.2"))

    def test_different_intervals_per_venue_follow_their_schedules(self):
        ledger = perp_ledger()
        trade(ledger, "a", Side.BUY, "0.1", match_ts=T - HOUR, venue="BINANCE")
        trade(ledger, "b", Side.SELL, "0.1", match_ts=T - HOUR, venue="BYBIT")
        rates = [rate(T, "0.0001"), rate(T + 8 * HOUR, "0.0001")] + [
            rate(T + k * 4 * HOUR, "0.0001", venue="BYBIT") for k in range(3)
        ]
        eng = engine(ledger, rates, specs=[spec("BINANCE", 8), spec("BYBIT", 4)])
        eng.settle_until(T + 8 * HOUR)
        self.assertEqual(len(funding_events(ledger, "BINANCE")), 2)
        self.assertEqual(len(funding_events(ledger, "BYBIT")), 3)
        self.assertEqual(eng.report()["status"], "COMPLETE")

    def test_settlements_beyond_the_data_are_never_booked(self):
        ledger = perp_ledger()
        trade(ledger, "o", Side.BUY, "0.1", match_ts=T - HOUR)
        eng = engine(ledger, [rate(T, "0.0001"), rate(T + 8 * HOUR, "0.0001")])
        eng.settle_until(T + HOUR, final=True)  # last market event at T + 1h
        self.assertEqual(len(funding_events(ledger)), 1)


class FundingDataTests(unittest.TestCase):
    def test_duplicate_records_are_ignored_and_conflicts_raise(self):
        book = FundingRateBook().extend([rate(T, "0.0001"), rate(T, "0.0001")])
        self.assertEqual(book.duplicates_ignored, 1)
        with self.assertRaises(FundingDataConflict):
            book.add(rate(T, "0.0002"))

    def test_no_double_settlement_when_settlement_is_rerun_on_the_same_ledger(self):
        ledger = perp_ledger()
        trade(ledger, "o", Side.BUY, "0.1", match_ts=T - HOUR)
        eng = engine(ledger, [rate(T, "0.0001")])
        eng.settle_until(T)
        eng.settle_until(T)
        eng.settle_until(T + HOUR, final=True)
        self.assertEqual(len(funding_events(ledger)), 1)
        # A second engine on the same ledger replays the settlement: same key, ignored.
        before = usdt(ledger)
        engine(ledger, [rate(T, "0.0001")]).settle_until(T)
        self.assertEqual(len(funding_events(ledger)), 1)
        self.assertEqual(usdt(ledger), before)
        self.assertEqual(ledger.economics.duplicates_ignored, 1)

    def test_missing_rate_with_a_position_is_flagged_and_not_assumed(self):
        ledger = perp_ledger()
        trade(ledger, "o", Side.BUY, "0.1", match_ts=T - HOUR)
        eng = engine(ledger, [])                 # schedule says T, no record
        eng.settle_until(T)
        self.assertEqual(funding_events(ledger), [])
        report = eng.report()
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertEqual([f["code"] for f in report["flags"]], ["FUNDING_RATE_MISSING"])
        self.assertEqual(eng.blocking_flags_by_venue(), {"BINANCE": [f"FUNDING_RATE_MISSING@{T}"]})

    def test_perp_without_a_schedule_cannot_report_complete(self):
        ledger = perp_ledger()
        eng = engine(ledger, [rate(T, "0.0001")], specs=[spec(interval_hours=0)])
        eng.settle_until(T)
        self.assertEqual(eng.report()["status"], "INCOMPLETE")
        self.assertEqual(eng.blocking_flags_by_venue(), {"BINANCE": [f"FUNDING_SCHEDULE_UNKNOWN:{PERP}"]})

    def test_missing_rate_while_flat_is_informational(self):
        ledger = perp_ledger()
        eng = engine(ledger, [])
        eng.settle_until(T)
        report = eng.report()
        # Economically complete (no position was held), but the data has a gap.
        self.assertEqual((report["status"], report["economic_status"]), ("COMPLETE", "COMPLETE"))
        self.assertEqual((report["data_coverage"], report["data_gaps_while_flat"]), ("GAPS", 1))
        self.assertEqual(report["configuration"], "VALID")
        self.assertFalse(eng.flags[0]["blocking"])

    def test_missing_rate_with_a_position_is_economically_incomplete(self):
        ledger = perp_ledger()
        trade(ledger, "o", Side.BUY, "0.1", match_ts=T - HOUR)
        eng = engine(ledger, [])
        eng.settle_until(T)
        report = eng.report()
        self.assertEqual((report["status"], report["economic_status"], report["data_coverage"]),
                         ("INCOMPLETE", "INCOMPLETE", "GAPS"))

    def test_stale_rate_is_flagged_and_not_booked(self):
        ledger = perp_ledger()
        trade(ledger, "o", Side.BUY, "0.1", match_ts=T - HOUR)
        eng = engine(ledger, [rate(T, "0.0001", asof_ns=T - 3 * HOUR)], max_rate_age_ns=HOUR)
        eng.settle_until(T)
        self.assertEqual(funding_events(ledger), [])
        self.assertEqual(eng.flags[0]["code"], "FUNDING_RATE_STALE")

    def test_f04_rate_without_observation_time_is_flagged_when_stale_check_is_on(self):
        ledger = perp_ledger()
        trade(ledger, "o", Side.BUY, "0.1", match_ts=T - HOUR)
        eng = engine(ledger, [rate(T, "0.0001")], max_rate_age_ns=HOUR)     # no asof_ns
        eng.settle_until(T)
        self.assertEqual(funding_events(ledger), [])
        self.assertEqual(eng.flags[0]["code"], "FUNDING_RATE_ASOF_MISSING")
        self.assertEqual(eng.report()["economic_status"], "INCOMPLETE")

    def test_rate_observed_after_it_was_used_is_flagged(self):
        ledger = perp_ledger()
        trade(ledger, "o", Side.BUY, "0.1", match_ts=T - HOUR)
        eng = engine(ledger, [rate(T, "0.0001", asof_ns=T + HOUR)])
        eng.settle_until(T)
        self.assertEqual(funding_events(ledger), [])
        self.assertEqual(eng.flags[0]["code"], "FUNDING_RATE_ASOF_IN_FUTURE")

    def test_venue_settlement_history_is_exempt_from_observation_checks(self):
        ledger = perp_ledger()
        trade(ledger, "o", Side.BUY, "0.1", match_ts=T - HOUR)
        history = rate(T, "0.0001", settlement_history=True)                 # no asof, fetched later
        eng = engine(ledger, [history], max_rate_age_ns=HOUR)
        eng.settle_until(T)
        self.assertEqual(len(funding_events(ledger)), 1)
        self.assertTrue(FundingRate.from_dict({"venue": "BINANCE", "instrument_id": PERP, "funding_ts_ns": T,
                                               "rate": "0.0001", "settlement_history": True}).settlement_history)

    def test_book_mid_marks_label_the_run_estimated(self):
        ledger = perp_ledger()
        trade(ledger, "o", Side.BUY, "0.1", match_ts=T - HOUR)
        eng = engine(ledger, [rate(T, "0.0001", mark=None)], mark_fallback="book_mid")
        eng.settle_until(T)
        report = eng.report()
        self.assertEqual((report["mark_basis"], report["settlements_on_book_mid"]), ("ESTIMATED", 1))
        self.assertIn("BINANCE", eng.estimated_by_venue())
        reconciled = reconcile(ledger.economics, opening=Valuation("USDT", {("BINANCE", PERP): MARK}),
                               closing=Valuation("USDT", {("BINANCE", PERP): MARK}),
                               estimated_by_venue=eng.estimated_by_venue())
        self.assertTrue(reconciled["estimated"])
        exchange = engine(perp_ledger(), [rate(T, "0.0001")])
        self.assertEqual(exchange.report()["mark_basis"], "NONE")

    def test_missing_mark_is_flagged_unless_mid_fallback_is_enabled(self):
        ledger = perp_ledger()
        trade(ledger, "o", Side.BUY, "0.1", match_ts=T - HOUR)
        eng = engine(ledger, [rate(T, "0.0001", mark=None)])
        eng.settle_until(T)
        self.assertEqual(eng.flags[0]["code"], "FUNDING_MARK_MISSING")

        ledger = perp_ledger()
        trade(ledger, "o", Side.BUY, "0.1", match_ts=T - HOUR)
        eng = engine(ledger, [rate(T, "0.0001", mark=None)], mark_fallback="book_mid")
        eng.observe("BINANCE", PERP, T - 10, Decimal("50000"))   # mid prevailing at T
        eng.observe("BINANCE", PERP, T + 10, Decimal("70000"))   # after T: not used
        eng.settle_until(T)
        [event] = funding_events(ledger)
        self.assertEqual(event.postings[0].quantity, Decimal("-0.5"))
        self.assertIn("approximation", event.valuation_source)

    def test_predicted_funding_is_reported_and_never_booked(self):
        ledger = perp_ledger()
        trade(ledger, "o", Side.BUY, "0.1", match_ts=T - HOUR)
        eng = engine(ledger, [rate(T + 8 * HOUR, "0.0003", predicted=True, asof_ns=T)])
        eng.settle_until(T + 8 * HOUR, final=True)
        self.assertEqual(funding_events(ledger), [])
        [prediction] = eng.report()["predicted"]
        self.assertEqual(prediction["status"], "PREDICTED (not booked)")
        self.assertEqual(Decimal(prediction["predicted_amount"]), Decimal("-1.8"))

    def test_records_load_from_jsonl_with_ms_and_iso_times(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rates.jsonl"
            path.write_text(
                json.dumps({"venue": "BINANCE", "instrument_id": PERP, "funding_ts_ms": 28_800_000,
                            "rate": "0.0001", "mark_price": "60000"}) + "\n"
                + json.dumps({"venue": "BYBIT", "instrument_id": PERP, "funding_time": "1970-01-01T08:00:00Z",
                              "rate": "-0.0001", "source": "SIMULATED"}) + "\n",
                encoding="utf-8",
            )
            book = FundingRateBook.from_jsonl(path)
        self.assertEqual(set(book.realized), {("BINANCE", PERP, T), ("BYBIT", PERP, T)})
        self.assertEqual(book.realized[("BYBIT", PERP, T)].source, EventSource.SIMULATED)


class FundingRecordValidationTests(unittest.TestCase):
    def test_invalid_records_are_rejected_with_reasons_and_never_booked(self):
        capped = replace(spec(), rate_cap=Decimal("0.003"), rate_floor=Decimal("-0.003"))
        book = FundingRateBook().extend([
            rate(T, "0.004"),                                         # outside the venue cap
            rate(T, "0.0001", venue="KRAKEN"),                        # not a configured perpetual
            rate(T + 8 * HOUR, "0.0001", mark=Decimal("-1")),          # bad mark
            rate(T + 16 * HOUR, "0.0001"),                            # fine
        ])
        book.add_row({"venue": "BINANCE", "instrument_id": PERP, "rate": "0.0001"}, "row-5")  # no time
        book.add_row({"venue": "BINANCE", "instrument_id": PERP, "funding_ts_ns": T, "rate": "x"}, "row-6")
        ledger = perp_ledger()
        trade(ledger, "o", Side.BUY, "0.1", match_ts=T - HOUR)
        eng = FundingEngine(ledger, [capped], book, settle_lag_ns=0)
        eng.observe("BINANCE", PERP, 1, MARK)
        eng.settle_until(T + 16 * HOUR)
        report = eng.report()
        self.assertEqual(report["rejected_by_reason"],
                         {"INVALID_MARK": 1, "RATE_OUT_OF_BOUNDS": 1, "UNKNOWN_INSTRUMENT": 1, "UNPARSEABLE": 2})
        self.assertEqual(len(funding_events(ledger)), 1)              # only the valid 16h record
        # The rejected 0h/8h records leave those settlements without data: blocking.
        self.assertEqual(report["economic_status"], "INCOMPLETE")
        self.assertEqual(report["data_coverage"], "GAPS")
        self.assertEqual({r["origin"] for r in report["rejected_samples"] if r["origin"]}, {"row-5", "row-6"})

    def test_default_rate_bound_applies_without_venue_cap(self):
        book = FundingRateBook().extend([rate(T, "0.06"), rate(T + 8 * HOUR, "-0.05")])
        valid = book.validated({("BINANCE", PERP): spec()}, Decimal("0.05"))
        self.assertEqual([r["reason"] for r in valid.rejected], ["RATE_OUT_OF_BOUNDS"])
        self.assertEqual(set(valid.realized), {("BINANCE", PERP, T + 8 * HOUR)})

    def test_unparseable_jsonl_lines_are_kept_not_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rates.jsonl"
            path.write_text("{not json}\n" + json.dumps({"venue": "BINANCE", "instrument_id": PERP,
                                                          "funding_ts_ns": T, "rate": "0.0001",
                                                          "source": "MADE_UP"}) + "\n", encoding="utf-8")
            book = FundingRateBook.from_jsonl(path)
        self.assertEqual([(r["origin"], r["reason"]) for r in book.rejected],
                         [("rates.jsonl:1", "UNPARSEABLE"), ("rates.jsonl:2", "UNPARSEABLE")])

    def test_report_shows_provenance_revisions_and_off_schedule_records(self):
        ledger = perp_ledger()
        trade(ledger, "o1", Side.BUY, "0.1", match_ts=T - HOUR)
        eng = engine(ledger, [rate(T, "0.0001"), rate(T + 4 * HOUR, "0.0001")])   # 8h schedule
        eng.settle_until(T + 4 * HOUR)
        trade(ledger, "late", Side.BUY, "0.1", match_ts=T - 5, report_ts=T + 4 * HOUR + 1)
        report = eng.report()
        self.assertEqual(report["rate_sources"], {"OBSERVED": 2})
        self.assertEqual(report["mark_sources"], {"funding record mark price": 2})
        # The late fill (matched before both) moved both settlements to revision 1.
        self.assertEqual(report["settlement_revisions"], {"r1": 2})
        self.assertEqual(report["records_off_schedule"], [f"BINANCE:{PERP}@{T + 4 * HOUR}"])


class FundingReconciliationTests(unittest.TestCase):
    def test_funding_reconciles_and_is_kept_out_of_plan_pnl(self):
        ledger = perp_ledger()
        trade(ledger, "o", Side.BUY, "0.1", match_ts=T - HOUR)
        engine(ledger, [rate(T, "0.0001")]).settle_until(T)
        marks = {("BINANCE", PERP): MARK}
        state = ledger.recalculate_account_state(mark_prices=marks)
        prices = Valuation("USDT", {("BINANCE", PERP): MARK})
        report = reconcile(
            ledger.economics, opening=prices, closing=prices,
            trade_pnl_by_venue={v: Decimal(r["realized_pnl"]) + Decimal(r["unrealized_pnl"])
                                for v, r in state["venues"].items()},
            legacy_fees_by_venue={v: Decimal(r["fees"]) for v, r in state["venues"].items()},
            legacy_funding_by_venue={v: Decimal(r["funding"]) for v, r in state["venues"].items()},
            plan_net_pnl=Decimal("0"), product_types={"BINANCE": "PERP"},
            funding_modelled_venues=["BINANCE"],
        )
        row = report["venues"]["BINANCE"]
        self.assertEqual(report["status"], "RECONCILED", report["checks"])
        self.assertEqual(tuple(Decimal(row[k]) for k in ("funding", "strategy_equity_pnl", "identity_residual")),
                         (Decimal("-0.6"), Decimal("-0.6"), Decimal("0")))
        self.assertNotIn("FUNDING", row["not_modelled"])
        self.assertEqual(Decimal(report["consolidated"]["funding"]), Decimal("-0.6"))
        self.assertIn("BINANCE_funding_matches_trade_ledger", [c["check"] for c in report["checks"]])
        # Legacy equity includes funding (no initial capital passed): 0 trading PnL - 0.6.
        self.assertEqual(Decimal(state["venues"]["BINANCE"]["equity"]), Decimal("-0.6"))

    def test_blocking_funding_flags_make_the_reconciliation_incomplete(self):
        ledger = perp_ledger()
        report = reconcile(ledger.economics, opening=Valuation("USDT"), closing=Valuation("USDT"),
                           funding_modelled_venues=["BINANCE"],
                           funding_flags_by_venue={"BINANCE": [f"FUNDING_RATE_MISSING@{T}"]})
        self.assertEqual(report["status"], "INCOMPLETE")


class PerpetualConfigTests(unittest.TestCase):
    def write(self, directory, mutate) -> Path:
        raw = json.loads((ROOT / "config/research.json").read_text())
        raw["strategy"]["instrument_id"] = PERP
        raw["risk"]["inventory"] = {"model": "unconstrained"}
        for row in raw["instruments"]:
            row.update(instrument_id=PERP, product_type="PERP", funding_interval_hours=8)
        for row in raw.get("fees", []):
            row["instrument_id"] = PERP   # dated fee rules for the perp, not the spot instrument
        mutate(raw)
        path = Path(directory) / "perp.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path

    def test_perpetual_terms_are_parsed(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg, _ = load_config(self.write(directory, lambda raw: None))
        instrument = cfg.instruments.get("BINANCE", PERP)
        self.assertTrue(is_perpetual(instrument.product_type))
        self.assertEqual((instrument.settlement_asset, instrument.funding_interval_ns, instrument.inverse),
                         ("USDT", 8 * HOUR, False))

    def test_unsupported_terms_fail_closed(self):
        mutations = {
            "inverse": lambda raw: raw["instruments"][0].update(contract_type="INVERSE"),
            "multiplier": lambda raw: raw["instruments"][0].update(contract_multiplier="0.001"),
            "no_interval": lambda raw: raw["instruments"][0].pop("funding_interval_hours"),
            "pre_funded": lambda raw: raw["risk"].update(inventory={
                "model": "pre_funded", "initial_base_by_venue": {"BINANCE": "0.05", "BYBIT": "0.05"}}),
        }
        for name, mutate in mutations.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    load_config(self.write(directory, mutate))

    def test_perpetual_backtest_settles_funding_end_to_end(self):
        # The two-venue sample relabelled as perps: the arb buys BINANCE and
        # sells BYBIT; a settlement inside the data is booked on both venues,
        # one after the last event is not, and spot-style checks stay green.
        with tempfile.TemporaryDirectory() as directory:
            rates = [
                {"venue": v, "instrument_id": PERP, "funding_ts_ns": 1_100_000_000, "rate": "0.0001",
                 "mark_price": "60000", "source": "SIMULATED"} for v in ("BINANCE", "BYBIT")
            ] + [{"venue": "BINANCE", "instrument_id": PERP, "funding_ts_ns": 5_000_000_000,
                  "rate": "0.0001", "mark_price": "60000", "source": "SIMULATED"}]
            config = self.write(directory, lambda raw: raw.update(funding={"rates": rates, "settle_lag_ms": 0}))
            events = Path(directory) / "events.jsonl"
            events.write_text((ROOT / "data/samples/two_venue_books.jsonl").read_text().replace(
                "BTC-USDT-SPOT", PERP), encoding="utf-8")
            out = Path(directory) / "out"
            result = run_backtest(config, events, out)
            report = json.loads((out / "economic_reconciliation.json").read_text())
            funding = json.loads((out / "funding_report.json").read_text())
            state = json.loads((out / "account_state.json").read_text())
        self.assertEqual(result["economics"]["status"], "RECONCILED", report["checks"])
        self.assertEqual(funding["status"], "COMPLETE")
        self.assertEqual(funding["settlements_booked"], 2)
        realized = {v: Decimal(a) for v, a in funding["realized_by_venue"].items()}
        positions = {k.split(":")[0]: Decimal(r["quantity"]) for k, r in state["positions"].items()}
        for venue in ("BINANCE", "BYBIT"):
            self.assertEqual(realized[venue], -positions[venue] * Decimal("60000") * Decimal("0.0001"))
            self.assertEqual(Decimal(state["venues"][venue]["funding"]), realized[venue])
            self.assertNotIn("FUNDING", report["venues"][venue]["not_modelled"])
        self.assertGreater(positions["BINANCE"], 0)
        self.assertTrue(all(check["passed"] for check in report["checks"]), report["checks"])


if __name__ == "__main__":
    unittest.main()
