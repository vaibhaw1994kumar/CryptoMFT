from dataclasses import replace
from decimal import Decimal
import unittest

from crypto_stat_arb.instruments import InstrumentMaster
from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.models import (
    ChildOrder, Instrument, MarketEvent, Opportunity, ParentPlan, Side,
)
from crypto_stat_arb.orderbook import OrderBook
from crypto_stat_arb.risk import (
    ExposureControlState, PreTradeDecision, RiskEngine, RiskLimits,
)


def limits(**changes) -> RiskLimits:
    return replace(RiskLimits(
        max_order_notional=Decimal("5000"),
        max_gross_notional=Decimal("10000"),
        max_residual_base=Decimal("1"), max_book_age_ns=1_000_000,
    ), **changes)


def opportunity() -> Opportunity:
    return Opportunity(
        opportunity_id="exposure-op", strategy_id="strategy-1",
        instrument_id="BTC-USDT-SPOT", observed_ts_ns=2,
        buy_venue="BINANCE", sell_venue="BYBIT",
        buy_price=Decimal("60000.00"), sell_price=Decimal("60060.00"),
        quantity=Decimal("0.010"), gross_edge_bps=Decimal("10"),
        estimated_net_edge_bps=Decimal("2"), source_event_ids=("a", "b"),
    )


def orders(item: Opportunity) -> tuple[ChildOrder, ChildOrder]:
    return (
        ChildOrder(
            order_id="buy", parent_id="parent", opportunity_id=item.opportunity_id,
            venue="BINANCE", instrument_id=item.instrument_id, side=Side.BUY,
            quantity=item.quantity, limit_price=item.buy_price,
        ),
        ChildOrder(
            order_id="sell", parent_id="parent", opportunity_id=item.opportunity_id,
            venue="BYBIT", instrument_id=item.instrument_id, side=Side.SELL,
            quantity=item.quantity, limit_price=item.sell_price,
        ),
    )


def books() -> dict[tuple[str, str], OrderBook]:
    result = {}
    for venue, bid, ask in (
        ("BINANCE", "59999.90", "60000.00"),
        ("BYBIT", "60060.00", "60060.10"),
    ):
        book = OrderBook(venue, "BTC-USDT-SPOT")
        book.apply(MarketEvent.from_dict({
            "event_type": "BOOK_SNAPSHOT", "venue": venue,
            "instrument_id": "BTC-USDT-SPOT", "exchange_ts_ns": 1,
            "receive_ts_ns": 1, "sequence": 1,
            "bids": [[bid, "1"]], "asks": [[ask, "1"]],
        }))
        result[(venue, "BTC-USDT-SPOT")] = book
    return result


def instruments() -> InstrumentMaster:
    return InstrumentMaster([
        Instrument(
            instrument_id="BTC-USDT-SPOT", venue=venue, symbol="BTCUSDT",
            base="BTC", quote="USDT", tick_size=Decimal("0.10"),
            quantity_step=Decimal("0.001"), min_quantity=Decimal("0.001"),
            min_notional=Decimal("5"), taker_fee_bps=Decimal("4"),
        )
        for venue in ("BINANCE", "BYBIT")
    ])


class ExposureLimitTests(unittest.TestCase):
    def validate(self, risk_limits: RiskLimits):
        item = opportunity()
        return RiskEngine(risk_limits).validate_pretrade(
            opportunity=item, orders=orders(item), books=books(),
            instruments=instruments(), route_status={}, now_ns=2,
            plans={}, ledger=Ledger(),
        )

    def assert_limit_rejects(self, code: str, risk_limits: RiskLimits) -> None:
        report = self.validate(risk_limits)
        self.assertEqual(report.decision, PreTradeDecision.REJECTED)
        self.assertIn(code, {check.code for check in report.failures})

    def test_strategy_gross_limit_blocks_complete_package(self):
        self.assert_limit_rejects(
            "STRATEGY_GROSS_EXPOSURE",
            limits(max_strategy_gross_notional=Decimal("1200")),
        )

    def test_strategy_net_limit_tracks_directional_notional(self):
        self.assert_limit_rejects(
            "STRATEGY_NET_EXPOSURE",
            limits(max_strategy_net_notional=Decimal("0.50")),
        )

    def test_instrument_gross_override_binds(self):
        self.assert_limit_rejects(
            "INSTRUMENT_GROSS_EXPOSURE",
            limits(instrument_gross_limits={"BTC-USDT-SPOT": Decimal("1200")}),
        )

    def test_venue_gross_override_binds(self):
        self.assert_limit_rejects(
            "VENUE_GROSS_EXPOSURE",
            limits(venue_gross_limits={"BINANCE": Decimal("599")}),
        )

    def test_snapshot_exposes_utilization_for_every_scope(self):
        item = opportunity()
        snapshot = RiskEngine(limits(
            max_strategy_gross_notional=Decimal("2000"),
            max_strategy_net_notional=Decimal("100"),
            max_instrument_gross_notional=Decimal("2000"),
            max_venue_gross_notional=Decimal("1000"),
        )).exposure_snapshot(
            plans={}, ledger=Ledger(), now_ns=2,
            projected_strategy_id=item.strategy_id,
            projected_orders=orders(item),
        )
        self.assertIn("strategy-1", snapshot["strategies"])
        self.assertIn("BTC-USDT-SPOT", snapshot["instruments"])
        self.assertEqual(set(snapshot["venues"]), {"BINANCE", "BYBIT"})
        self.assertGreater(
            Decimal(snapshot["strategies"]["strategy-1"]["gross_utilization"]),
            Decimal("0.5"),
        )

    def test_breach_and_recovery_are_durable_state_transitions(self):
        item = opportunity()
        buy, _ = orders(item)
        buy.filled_quantity = buy.quantity
        buy.average_price = buy.limit_price
        plan = ParentPlan("parent", item, [buy])
        ledger = Ledger()
        ledger.apply_fill(buy, 2)
        engine = RiskEngine(limits(
            max_strategy_gross_notional=Decimal("500"),
            max_strategy_net_notional=Decimal("500"),
        ))
        snapshot = engine.update_exposure_state(
            plans={plan.parent_id: plan}, ledger=ledger, now_ns=3,
        )
        self.assertEqual(engine.exposure_state, ExposureControlState.BREACHED)
        self.assertTrue(engine.kill_switch)
        self.assertEqual(len(snapshot["binding_events"]), 2)
        self.assertEqual(len(engine.exposure_transitions), 1)
        engine.update_exposure_state(
            plans={plan.parent_id: plan}, ledger=ledger, now_ns=4,
        )
        self.assertEqual(len(engine.exposure_transitions), 1)

        engine.limits = replace(
            engine.limits,
            max_strategy_gross_notional=Decimal("1000"),
            max_strategy_net_notional=Decimal("1000"),
        )
        engine.update_exposure_state(
            plans={plan.parent_id: plan}, ledger=ledger, now_ns=5,
        )
        self.assertEqual(engine.exposure_state, ExposureControlState.NORMAL)
        self.assertEqual(len(engine.exposure_transitions), 2)
        transitions = [
            event for event in ledger.events
            if event["event_type"] == "EXPOSURE_LIMIT_STATE_TRANSITION"
        ]
        self.assertEqual([event["state"] for event in transitions], [
            "BREACHED", "NORMAL",
        ])
        self.assertTrue(engine.kill_switch)  # operator reset remains explicit


if __name__ == "__main__":
    unittest.main()
