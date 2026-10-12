from dataclasses import replace
from decimal import Decimal
import unittest

from crypto_stat_arb.execution import StrategyDirectedRouter, VenueRouteStatus
from crypto_stat_arb.instruments import InstrumentMaster
from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.models import (
    ChildOrder, Instrument, MarketEvent, Opportunity, Side,
)
from crypto_stat_arb.oms import OMS
from crypto_stat_arb.orderbook import OrderBook
from crypto_stat_arb.risk import (
    AccountRiskSnapshot, PreTradeDecision, RiskEngine, RiskLimits,
)


def instruments(*, status: str = "TRADING", min_notional: str = "5") -> InstrumentMaster:
    return InstrumentMaster([
        Instrument(
            instrument_id="BTC-USDT-SPOT", venue=venue, symbol="BTCUSDT",
            base="BTC", quote="USDT", tick_size=Decimal("0.10"),
            quantity_step=Decimal("0.001"), min_quantity=Decimal("0.001"),
            min_notional=Decimal(min_notional), taker_fee_bps=Decimal("4"),
            status=status,
        )
        for venue in ("BINANCE", "BYBIT")
    ])


def opportunity(**changes) -> Opportunity:
    base = Opportunity(
        opportunity_id="pretrade-opportunity", strategy_id="strategy-1",
        instrument_id="BTC-USDT-SPOT", observed_ts_ns=2,
        buy_venue="BINANCE", sell_venue="BYBIT",
        buy_price=Decimal("60000.00"), sell_price=Decimal("60060.00"),
        quantity=Decimal("0.010"), gross_edge_bps=Decimal("10"),
        estimated_net_edge_bps=Decimal("2"), source_event_ids=("a", "b"),
    )
    return replace(base, **changes)


def books(*, receive_ts_ns: int = 1) -> dict[tuple[str, str], OrderBook]:
    result = {}
    for venue, bid, ask in (
        ("BINANCE", "59999.90", "60000.00"),
        ("BYBIT", "60060.00", "60060.10"),
    ):
        book = OrderBook(venue, "BTC-USDT-SPOT")
        book.apply(MarketEvent.from_dict({
            "event_type": "BOOK_SNAPSHOT", "venue": venue,
            "instrument_id": "BTC-USDT-SPOT", "exchange_ts_ns": receive_ts_ns,
            "receive_ts_ns": receive_ts_ns, "sequence": 1,
            "bids": [[bid, "1"]], "asks": [[ask, "1"]],
        }))
        result[(venue, "BTC-USDT-SPOT")] = book
    return result


def limits(**changes) -> RiskLimits:
    base = RiskLimits(
        max_order_notional=Decimal("5000"),
        max_gross_notional=Decimal("10000"),
        max_residual_base=Decimal("0.002"),
        max_book_age_ns=1_000_000_000,
    )
    return replace(base, **changes)


def orders(item: Opportunity) -> tuple[ChildOrder, ChildOrder]:
    return (
        ChildOrder(
            order_id="buy", parent_id="parent", opportunity_id=item.opportunity_id,
            venue=item.buy_venue, instrument_id=item.instrument_id, side=Side.BUY,
            quantity=item.quantity, limit_price=item.buy_price,
        ),
        ChildOrder(
            order_id="sell", parent_id="parent", opportunity_id=item.opportunity_id,
            venue=item.sell_venue, instrument_id=item.instrument_id, side=Side.SELL,
            quantity=item.quantity, limit_price=item.sell_price,
        ),
    )


class PreTradeValidationTests(unittest.TestCase):
    def validate(
        self, *, item: Opportunity | None = None,
        master: InstrumentMaster | None = None,
        engine: RiskEngine | None = None,
        market_books: dict[tuple[str, str], OrderBook] | None = None,
        route_status: dict[str, VenueRouteStatus] | None = None,
    ):
        item = item or opportunity()
        engine = engine or RiskEngine(limits())
        return engine.validate_pretrade(
            opportunity=item, orders=orders(item), books=(
                books() if market_books is None else market_books
            ), instruments=master or instruments(),
            route_status=route_status or {}, now_ns=item.observed_ts_ns,
        )

    def assert_rejected_by(self, code: str, **kwargs) -> None:
        report = self.validate(**kwargs)
        self.assertEqual(report.decision, PreTradeDecision.REJECTED)
        self.assertIn(code, {check.code for check in report.failures})

    def test_instrument_must_exist(self):
        self.assert_rejected_by("INSTRUMENT_EXISTS", master=InstrumentMaster([]))

    def test_instrument_status_must_be_trading(self):
        self.assert_rejected_by(
            "INSTRUMENT_STATUS", master=instruments(status="SUSPENDED")
        )

    def test_price_must_be_inside_configured_band(self):
        self.assert_rejected_by(
            "PRICE_BAND",
            item=opportunity(buy_price=Decimal("61000.00")),
            engine=RiskEngine(limits(max_price_deviation_bps=Decimal("10"))),
        )

    def test_price_must_match_tick_size(self):
        self.assert_rejected_by(
            "TICK_SIZE", item=opportunity(buy_price=Decimal("60000.05"))
        )

    def test_quantity_must_match_lot_size(self):
        self.assert_rejected_by(
            "LOT_SIZE", item=opportunity(quantity=Decimal("0.0105"))
        )

    def test_quantity_must_meet_minimum(self):
        self.assert_rejected_by(
            "MINIMUM_QUANTITY", item=opportunity(quantity=Decimal("0.0005"))
        )

    def test_notional_must_meet_instrument_minimum(self):
        self.assert_rejected_by(
            "MINIMUM_NOTIONAL", master=instruments(min_notional="1000")
        )

    def test_order_must_not_exceed_platform_maximum(self):
        self.assert_rejected_by(
            "MAXIMUM_ORDER_NOTIONAL",
            engine=RiskEngine(limits(max_order_notional=Decimal("500"))),
        )

    def test_order_must_not_exceed_venue_maximum(self):
        self.assert_rejected_by(
            "VENUE_ORDER_NOTIONAL",
            engine=RiskEngine(limits(), {
                "BINANCE": AccountRiskSnapshot(
                    venue="BINANCE", max_order_notional=Decimal("500")
                )
            }),
        )

    def test_package_must_not_exceed_strategy_limit(self):
        self.assert_rejected_by(
            "STRATEGY_PACKAGE_NOTIONAL",
            engine=RiskEngine(limits(
                max_strategy_package_notional=Decimal("1000")
            )),
        )

    def test_live_market_data_must_be_fresh(self):
        self.assert_rejected_by(
            "LIVE_DATA_AGE",
            item=opportunity(observed_ts_ns=2_000_000_001),
        )

    def test_live_book_must_be_valid(self):
        invalid = books()
        invalid[("BINANCE", "BTC-USDT-SPOT")].start_resync("injected")
        self.assert_rejected_by("LIVE_BOOK_VALID", market_books=invalid)

    def test_venue_and_account_must_be_enabled(self):
        self.assert_rejected_by(
            "VENUE_STATUS",
            route_status={"BINANCE": VenueRouteStatus(venue_enabled=False)},
        )
        self.assert_rejected_by(
            "ACCOUNT_STATUS",
            engine=RiskEngine(limits(), {
                "BINANCE": AccountRiskSnapshot(
                    venue="BINANCE", account_enabled=False
                )
            }),
        )

    def test_strategy_must_be_enabled_for_account(self):
        self.assert_rejected_by(
            "STRATEGY_STATUS",
            engine=RiskEngine(limits(), {
                "BINANCE": AccountRiskSnapshot(
                    venue="BINANCE", strategy_enabled=False
                )
            }),
        )

    def test_paper_cash_must_cover_buy_leg(self):
        self.assert_rejected_by(
            "AVAILABLE_CASH",
            engine=RiskEngine(limits(), {
                "BINANCE": AccountRiskSnapshot(
                    venue="BINANCE", available_cash=Decimal("100")
                )
            }),
        )

    def test_paper_margin_must_cover_required_margin(self):
        self.assert_rejected_by(
            "AVAILABLE_MARGIN",
            engine=RiskEngine(limits(), {
                "BYBIT": AccountRiskSnapshot(
                    venue="BYBIT", available_margin=Decimal("1"),
                    initial_margin_rate=Decimal("0.10"),
                )
            }),
        )

    def test_oms_records_matrix_and_dispatches_nothing_on_failure(self):
        engine = RiskEngine(limits(max_order_notional=Decimal("500")))
        ledger = Ledger()
        router = StrategyDirectedRouter(instruments())
        plan = OMS(instruments(), engine, ledger, router=router).execute(
            opportunity(), books()
        )
        self.assertEqual(plan.terminal_outcome, "REJECTED")
        self.assertEqual(router.stats_snapshot()["unique_requests"], 0)
        event = next(
            row for row in ledger.events
            if row["event_type"] == "PRE_TRADE_VALIDATION"
        )
        self.assertEqual(event["decision"], "REJECTED")
        self.assertIn(
            "MAXIMUM_ORDER_NOTIONAL", event["validation"]["failure_codes"]
        )


if __name__ == "__main__":
    unittest.main()
