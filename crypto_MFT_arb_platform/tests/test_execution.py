from decimal import Decimal
from pathlib import Path
import json
import tempfile
import unittest

from crypto_stat_arb.execution import (
    CanonicalOrderRequest,
    ExecutionEventType,
    OrderAction,
    OrderType,
    MakerTakerIntent,
    RateLimitPolicy,
    RoutingInstruction,
    StrategyDirectedRouter,
    TimeInForce,
    ThrottleAction,
    VenueRouteStatus,
)
from crypto_stat_arb.models import Instrument, MarketEvent, Side
from crypto_stat_arb.instruments import InstrumentMaster
from crypto_stat_arb.orderbook import OrderBook


def instruments() -> InstrumentMaster:
    return InstrumentMaster([
        Instrument(
            instrument_id="BTC-USDT-SPOT",
            venue=venue,
            symbol="BTCUSDT",
            base="BTC",
            quote="USDT",
            tick_size=Decimal("0.10"),
            quantity_step=Decimal("0.001"),
            min_quantity=Decimal("0.001"),
            min_notional=Decimal("5"),
            taker_fee_bps=Decimal("4"),
        )
        for venue in ("BINANCE", "BYBIT")
    ])


def book(venue: str = "BINANCE") -> OrderBook:
    result = OrderBook(venue, "BTC-USDT-SPOT")
    result.apply(MarketEvent.from_dict({
        "event_type": "BOOK_SNAPSHOT",
        "venue": venue,
        "instrument_id": "BTC-USDT-SPOT",
        "exchange_ts_ns": 1,
        "receive_ts_ns": 1,
        "sequence": 1,
        "bids": [["59999.90", "1"]],
        "asks": [["60000.00", "1"]],
    }))
    return result


def request(
    action: OrderAction,
    order_id: str,
    *,
    price: str = "59000.00",
    target: str | None = None,
    idempotency_key: str | None = None,
    post_only: bool = True,
) -> CanonicalOrderRequest:
    return CanonicalOrderRequest.create(
        action=action,
        order_id=order_id,
        target_order_id=target,
        parent_opportunity_id="opportunity-1",
        parent_order_id="parent-1",
        instrument_id="BTC-USDT-SPOT",
        venue="BINANCE",
        side=Side.BUY,
        price=Decimal(price),
        quantity=Decimal("0.010"),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        post_only=post_only,
        reduce_only=False,
        created_ts_ns=100,
        idempotency_key=idempotency_key,
    )


class ExecutionApiTests(unittest.TestCase):
    @staticmethod
    def routing_instruction(**overrides) -> RoutingInstruction:
        values = {
            "strategy_id": "strategy-1",
            "eligible_venues": ("BINANCE", "BYBIT"),
            "instrument_id": "BTC-USDT-SPOT",
            "side": Side.BUY,
            "order_type": OrderType.LIMIT,
            "price": Decimal("60000.00"),
            "quantity": Decimal("0.010"),
            "route_priority": ("BINANCE", "BYBIT"),
            "maker_taker_intent": MakerTakerIntent.TAKER,
            "time_in_force": TimeInForce.IOC,
            "post_only": False,
            "reduce_only": False,
            "parent_opportunity_id": "opportunity-1",
            "parent_order_id": "parent-1",
            "order_id": "child-1",
            "created_ts_ns": 100,
        }
        values.update(overrides)
        return RoutingInstruction(**values)

    def test_route_override_across_two_venues(self):
        router = StrategyDirectedRouter(instruments())
        books = {
            ("BINANCE", "BTC-USDT-SPOT"): book("BINANCE"),
            ("BYBIT", "BTC-USDT-SPOT"): book("BYBIT"),
        }
        result = router.route_controlled(
            self.routing_instruction(route_override="BYBIT"), books
        )
        self.assertEqual(result.selected_venue, "BYBIT")
        self.assertTrue(result.override_applied)
        self.assertEqual(result.request.venue, "BYBIT")
        self.assertEqual(result.checks[0].venue, "BYBIT")
        self.assertEqual(router.stats_snapshot()["route_overrides"], 1)

    def test_router_skips_disabled_account_and_uses_next_priority(self):
        router = StrategyDirectedRouter(
            instruments(),
            route_status={
                "BINANCE": VenueRouteStatus(account_enabled=False),
                "BYBIT": VenueRouteStatus(),
            },
        )
        books = {
            ("BINANCE", "BTC-USDT-SPOT"): book("BINANCE"),
            ("BYBIT", "BTC-USDT-SPOT"): book("BYBIT"),
        }
        result = router.route_controlled(self.routing_instruction(), books)
        self.assertEqual(result.selected_venue, "BYBIT")
        self.assertEqual(result.checks[0].reasons, ("account_disabled",))

    def test_router_rejects_when_rate_limit_or_order_support_is_unavailable(self):
        router = StrategyDirectedRouter(
            instruments(),
            route_status={
                "BINANCE": VenueRouteStatus(rate_limit_capacity=0),
                "BYBIT": VenueRouteStatus(
                    supported_order_types=frozenset((OrderType.MARKET,))
                ),
            },
        )
        books = {
            ("BINANCE", "BTC-USDT-SPOT"): book("BINANCE"),
            ("BYBIT", "BTC-USDT-SPOT"): book("BYBIT"),
        }
        result = router.route_controlled(self.routing_instruction(), books)
        self.assertIsNone(result.selected_venue)
        self.assertIn("rate_limit_exhausted", result.checks[0].reasons)
        self.assertIn("order_type_unsupported", result.checks[1].reasons)
        self.assertEqual(result.checks[0].throttle_action, ThrottleAction.REJECT)
        self.assertEqual(result.checks[0].rate_limit_used, 0)
        self.assertEqual(result.checks[0].rate_limit_headroom, 0)

    def test_burst_queue_is_bounded_and_releases_fifo_by_window(self):
        class CountingAdapter:
            def __init__(self):
                self.order_ids = []

            def handle(self, request, order_book):
                self.order_ids.append(request.order_id)
                return ()

        adapter = CountingAdapter()
        status = VenueRouteStatus(
            rate_limit_capacity=1,
            rate_limit_used=1,
            rate_limit_window_ns=1_000,
            rate_limit_policy=RateLimitPolicy.QUEUE,
            max_throttle_queue=2,
        )
        router = StrategyDirectedRouter(
            instruments(), adapters={"BINANCE": adapter},
            route_status={"BINANCE": status},
        )
        books = {("BINANCE", "BTC-USDT-SPOT"): book("BINANCE")}
        first_instruction = self.routing_instruction(
            eligible_venues=("BINANCE",), route_priority=("BINANCE",),
            order_id="burst-1", created_ts_ns=100, idempotency_key="burst-1",
        )
        first = router.route_controlled(first_instruction, books)
        duplicate = router.route_controlled(first_instruction, books)
        self.assertEqual(duplicate.request.request_id, first.request.request_id)
        self.assertEqual(router.rate_limit_snapshot()["pending_requests"], 1)
        second = router.route_controlled(self.routing_instruction(
            eligible_venues=("BINANCE",), route_priority=("BINANCE",),
            order_id="burst-2", created_ts_ns=101, idempotency_key="burst-2",
        ), books)
        rejected = router.route_controlled(self.routing_instruction(
            eligible_venues=("BINANCE",), route_priority=("BINANCE",),
            order_id="burst-3", created_ts_ns=102, idempotency_key="burst-3",
        ), books)
        self.assertEqual(first.throttle_action, ThrottleAction.QUEUE)
        self.assertEqual(second.throttle_action, ThrottleAction.QUEUE)
        self.assertEqual(first.release_ts_ns, 1_000)
        self.assertIsNone(rejected.selected_venue)
        self.assertIn("throttle_queue_full", rejected.checks[0].reasons)
        self.assertEqual(adapter.order_ids, [])
        snapshot = router.rate_limit_snapshot()
        self.assertEqual(snapshot["venues"]["BINANCE"]["used"], 1)
        self.assertEqual(snapshot["venues"]["BINANCE"]["headroom"], 0)
        self.assertEqual(snapshot["venues"]["BINANCE"]["queue_depth"], 2)
        self.assertEqual(router.process_throttled(999), ())
        released_first = router.process_throttled(1_000)
        self.assertEqual([x.request.order_id for x in released_first], ["burst-1"])
        self.assertEqual(adapter.order_ids, ["burst-1"])
        self.assertEqual(router.rate_limit_snapshot()["pending_requests"], 1)
        released_second = router.process_throttled(2_000)
        self.assertEqual([x.request.order_id for x in released_second], ["burst-2"])
        self.assertEqual(adapter.order_ids, ["burst-1", "burst-2"])
        stats = router.stats_snapshot()
        self.assertEqual(stats["rate_limit_queued"], 2)
        self.assertEqual(stats["rate_limit_released"], 2)
        self.assertEqual(stats["rate_limit_rejections"], 1)

    def test_delay_policy_holds_request_until_exact_simulated_release(self):
        class CountingAdapter:
            def __init__(self):
                self.calls = 0

            def handle(self, request, order_book):
                self.calls += 1
                return ()

        adapter = CountingAdapter()
        router = StrategyDirectedRouter(
            instruments(), adapters={"BINANCE": adapter},
            route_status={"BINANCE": VenueRouteStatus(
                rate_limit_capacity=1, rate_limit_used=1,
                rate_limit_window_ns=1_000,
                rate_limit_policy=RateLimitPolicy.DELAY,
                throttle_delay_ns=250,
            )},
        )
        books = {("BINANCE", "BTC-USDT-SPOT"): book("BINANCE")}
        result = router.route_controlled(self.routing_instruction(
            eligible_venues=("BINANCE",), route_priority=("BINANCE",),
            order_id="delayed-1", created_ts_ns=100,
            idempotency_key="delayed-1",
        ), books)
        self.assertEqual(result.throttle_action, ThrottleAction.DELAY)
        self.assertEqual(result.release_ts_ns, 1_250)
        self.assertEqual(router.process_throttled(1_249), ())
        self.assertEqual(adapter.calls, 0)
        released = router.process_throttled(1_250)
        self.assertEqual(len(released), 1)
        self.assertEqual(released[0].throttle_action, ThrottleAction.RELEASED)
        self.assertEqual(adapter.calls, 1)
        release_trace = next(
            row for row in router.trace if row["record_type"] == "THROTTLE_RELEASED"
        )
        self.assertEqual(release_trace["actual_delay_ns"], 1_150)

    def test_disabled_control_state_rejects_instead_of_queueing(self):
        router = StrategyDirectedRouter(
            instruments(),
            route_status={"BINANCE": VenueRouteStatus(
                account_enabled=False, rate_limit_capacity=1, rate_limit_used=1,
                rate_limit_policy=RateLimitPolicy.QUEUE,
            )},
        )
        result = router.route_controlled(self.routing_instruction(
            eligible_venues=("BINANCE",), route_priority=("BINANCE",),
            idempotency_key="disabled-control",
        ), {("BINANCE", "BTC-USDT-SPOT"): book("BINANCE")})
        self.assertIsNone(result.selected_venue)
        self.assertEqual(result.throttle_action, ThrottleAction.REJECT)
        self.assertEqual(result.checks[0].throttle_action, ThrottleAction.REJECT)
        self.assertIn("account_disabled", result.checks[0].reasons)
        self.assertEqual(router.rate_limit_snapshot()["pending_requests"], 0)

    def test_common_request_contains_required_order_semantics(self):
        row = request(OrderAction.SUBMIT, "order-1").to_dict()
        required = {
            "instrument_id", "venue", "side", "price", "quantity",
            "order_type", "time_in_force", "post_only", "reduce_only",
            "parent_opportunity_id",
        }
        self.assertTrue(required.issubset(row))
        self.assertEqual(row["action"], "SUBMIT")

    def test_submit_and_cancel_use_same_router_and_callback_contract(self):
        observed = []
        router = StrategyDirectedRouter(instruments(), callback_handler=observed.append)
        submit = request(OrderAction.SUBMIT, "order-1")
        submit_callbacks = router.route(submit, book())
        self.assertEqual([x.event_type for x in submit_callbacks], [
            ExecutionEventType.ACKNOWLEDGED
        ])
        cancel = request(
            OrderAction.CANCEL, "order-1", target="order-1",
            idempotency_key="cancel-order-1",
        )
        cancel_callbacks = router.route(cancel)
        self.assertEqual(cancel_callbacks[-1].event_type, ExecutionEventType.CANCELLED)
        self.assertEqual(observed, [*submit_callbacks, *cancel_callbacks])

    def test_replace_resting_order_can_fill_at_new_price(self):
        router = StrategyDirectedRouter(instruments())
        router.route(request(OrderAction.SUBMIT, "old-order"), book())
        replacement = request(
            OrderAction.REPLACE,
            "new-order",
            price="60000.00",
            target="old-order",
            idempotency_key="replace-old-order",
            post_only=False,
        )
        callbacks = router.route(replacement, book())
        self.assertEqual([x.event_type for x in callbacks], [
            ExecutionEventType.REPLACED,
            ExecutionEventType.ACKNOWLEDGED,
            ExecutionEventType.FILLED,
        ])
        self.assertEqual(callbacks[-1].cumulative_filled_quantity, Decimal("0.010"))

    def test_duplicate_idempotency_key_suppresses_second_adapter_action(self):
        router = StrategyDirectedRouter(instruments())
        first_request = request(
            OrderAction.SUBMIT, "order-1", idempotency_key="same-logical-request"
        )
        first = router.route(first_request, book())
        second = router.route(first_request, book())
        self.assertIs(first, second)
        self.assertEqual(router.stats_snapshot()["duplicates_suppressed"], 1)

    def test_trace_correlates_requests_and_callbacks(self):
        router = StrategyDirectedRouter(instruments())
        submit = request(OrderAction.SUBMIT, "order-1")
        callbacks = router.route(submit, book())
        with tempfile.TemporaryDirectory() as directory:
            result = router.write_trace(Path(directory) / "order_trace.jsonl")
            rows = [json.loads(x) for x in Path(result["path"]).read_text().splitlines()]
        self.assertEqual(rows[0]["request"]["request_id"], submit.request_id)
        self.assertEqual(rows[1]["callback"]["request_id"], submit.request_id)
        self.assertEqual(rows[1]["callback"]["callback_id"], callbacks[0].callback_id)


if __name__ == "__main__":
    unittest.main()
