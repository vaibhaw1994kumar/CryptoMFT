import asyncio
import argparse
from dataclasses import replace
import hashlib
import hmac
from decimal import Decimal
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest

from trading_platform_poc.binance_execution import (
    BinanceAPIError,
    BinanceAmbiguousOrderError,
    BinanceCredentials,
    BinanceExecutionService,
    BinanceGatewayError,
    BinanceOrderResult,
    BinanceReconciliationReport,
    BinanceSpotTestnetGateway,
    BinanceSymbolRules,
    NormalizedBinanceOrder,
    sign_query,
)
from trading_platform_poc.config import BinanceExecutionConfig
from trading_platform_poc.config import (
    AppConfig,
    RiskConfig,
    StrategyConfig,
)
from trading_platform_poc.engine import TradingEngine
from trading_platform_poc.binance_cli import _print, _submit
from trading_platform_poc.models import (
    BookSnapshot,
    OrderIntent,
    OrderState,
    OrderType,
    PriceLevel,
    Side,
)
from trading_platform_poc.oms import OrderManagementSystem
from trading_platform_poc.order_book import BinanceOrderBook
from trading_platform_poc.reporting import write_report
from trading_platform_poc.strategy import SpreadCaptureStrategy


class FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self.payload = payload
        self.status = status
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def text(self):
        import json

        return json.dumps(self.payload)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, headers=None, **kwargs):
        self.requests.append((method, url, headers or {}, kwargs))
        return self.responses.pop(0)


def make_order(
    order_type: OrderType = OrderType.LIMIT,
    *,
    side: Side = Side.BUY,
    price: str | None = "100.123",
):
    oms = OrderManagementSystem()
    intent = OrderIntent(
        uuid4(),
        "test",
        "BTCUSDT",
        side,
        order_type,
        Decimal("0.123456"),
        Decimal(price) if price is not None else None,
        1,
    )
    order = oms.create(intent)
    order = oms.transition(
        order.order_id,
        OrderState.RISK_APPROVED,
        2,
    )
    return oms, order


def rules() -> BinanceSymbolRules:
    return BinanceSymbolRules(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        tick_size=Decimal("0.01"),
        minimum_price=Decimal("0.01"),
        maximum_price=Decimal("1000000"),
        step_size=Decimal("0.001"),
        minimum_quantity=Decimal("0.001"),
        maximum_quantity=Decimal("100"),
        market_step_size=Decimal("0.01"),
        market_minimum_quantity=Decimal("0.01"),
        market_maximum_quantity=Decimal("100"),
        minimum_notional=Decimal("10"),
        maximum_notional=None,
    )


def test_rejects_live_url_without_explicit_enablement() -> None:
    with pytest.raises(ValueError, match="exact"):
        BinanceExecutionConfig(
            rest_base_url="https://api.binance.com"
        )

    with pytest.raises(ValueError, match="exact"):
        BinanceExecutionConfig(
            rest_base_url=(
                "https://testnet.binance.vision.attacker.example"
            )
        )
    for unsafe_url in (
        "https://testnet.binance.vision:444",
        "https://user@testnet.binance.vision",
        "https://testnet.binance.vision/prefix",
        "https://testnet.binance.vision?redirect=1",
    ):
        with pytest.raises(ValueError, match="canonical"):
            BinanceExecutionConfig(rest_base_url=unsafe_url)


def test_testnet_gateway_refuses_live_configuration() -> None:
    live = BinanceExecutionConfig(
        environment="live",
        rest_base_url="https://api.binance.com",
        allow_live_trading=True,
    )

    with pytest.raises(ValueError, match="environment=testnet"):
        BinanceSpotTestnetGateway(
            live,
            BinanceCredentials("key", "secret"),
        )


def test_loads_credentials_from_named_environment(monkeypatch) -> None:
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "key")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "secret")

    credentials = BinanceCredentials.from_environment(
        BinanceExecutionConfig()
    )

    assert credentials == BinanceCredentials("key", "secret")


def test_sign_query_is_hmac_sha256() -> None:
    params = {
        "symbol": "BTCUSDT",
        "timestamp": 123456789,
    }

    query, signature = sign_query(params, "secret")

    assert signature == hmac.new(
        b"secret",
        query.encode(),
        hashlib.sha256,
    ).hexdigest()


@pytest.mark.asyncio
async def test_normalizes_quantity_and_side_aware_price() -> None:
    gateway = BinanceSpotTestnetGateway(
        credentials=BinanceCredentials("key", "secret")
    )
    gateway._symbol_rules["BTCUSDT"] = rules()
    _, buy = make_order(side=Side.BUY)
    _, sell = make_order(side=Side.SELL)

    normalized_buy = await gateway.normalize_order(buy)
    normalized_sell = await gateway.normalize_order(sell)

    assert normalized_buy.quantity == Decimal("0.123")
    assert normalized_buy.price == Decimal("100.12")
    assert normalized_sell.price == Decimal("100.13")


@pytest.mark.asyncio
async def test_rejects_non_finite_order_inputs() -> None:
    gateway = BinanceSpotTestnetGateway(
        credentials=BinanceCredentials("key", "secret")
    )
    gateway._symbol_rules["BTCUSDT"] = rules()
    _, order = make_order(price="NaN")

    with pytest.raises(BinanceGatewayError, match="finite"):
        await gateway.normalize_order(order)


@pytest.mark.asyncio
async def test_zero_market_step_keeps_market_quantity_limits() -> None:
    gateway = BinanceSpotTestnetGateway(
        credentials=BinanceCredentials("key", "secret")
    )
    market_rules = rules()
    gateway._symbol_rules["BTCUSDT"] = replace(
        market_rules,
        market_step_size=None,
        market_minimum_quantity=Decimal("0"),
        market_maximum_quantity=Decimal("0.2"),
    )
    _, order = make_order(OrderType.MARKET, price=None)

    normalized = await gateway.normalize_order(
        order,
        reference_price=Decimal("100"),
    )

    assert normalized.quantity == order.quantity


@pytest.mark.asyncio
async def test_submits_signed_limit_maker_order() -> None:
    payload = {
        "symbol": "BTCUSDT",
        "orderId": 123,
        "clientOrderId": "client",
        "transactTime": 1000,
        "price": "100.12",
        "origQty": "0.123",
        "executedQty": "0",
        "cummulativeQuoteQty": "0",
        "status": "NEW",
        "timeInForce": "GTC",
        "type": "LIMIT_MAKER",
        "side": "BUY",
    }
    session = FakeSession(
        [
            FakeResponse(
                payload,
                headers={
                    "X-MBX-USED-WEIGHT-1M": "2",
                    "Retry-After": "3",
                },
            )
        ]
    )
    gateway = BinanceSpotTestnetGateway(
        credentials=BinanceCredentials("key", "secret"),
        session=session,
    )
    gateway._symbol_rules["BTCUSDT"] = rules()
    _, order = make_order(OrderType.POST_ONLY)
    payload["clientOrderId"] = order.client_order_id

    result = await gateway.submit_order(order)

    method, url, headers, _ = session.requests[0]
    query = parse_qs(urlparse(url).query)
    assert method == "POST"
    assert headers["X-MBX-APIKEY"] == "key"
    assert query["type"] == ["LIMIT_MAKER"]
    assert query["price"] == ["100.12"]
    assert query["quantity"] == ["0.123"]
    assert "signature" in query
    assert result.status == "NEW"
    assert gateway.last_rate_limits["X-MBX-USED-WEIGHT-1M"] == "2"
    assert gateway.last_rate_limits["Retry-After"] == "3"


@pytest.mark.asyncio
async def test_symbol_rules_honor_market_notional_flags() -> None:
    session = FakeSession(
        [
            FakeResponse(
                {
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "status": "TRADING",
                            "baseAsset": "BTC",
                            "quoteAsset": "USDT",
                            "filters": [
                                {
                                    "filterType": "PRICE_FILTER",
                                    "minPrice": "0",
                                    "maxPrice": "0",
                                    "tickSize": "0",
                                },
                                {
                                    "filterType": "LOT_SIZE",
                                    "minQty": "0.001",
                                    "maxQty": "100",
                                    "stepSize": "0.001",
                                },
                                {
                                    "filterType": "MARKET_LOT_SIZE",
                                    "minQty": "0.001",
                                    "maxQty": "100",
                                    "stepSize": "0.001",
                                },
                                {
                                    "filterType": "NOTIONAL",
                                    "minNotional": "100",
                                    "maxNotional": "1000",
                                    "applyMinToMarket": False,
                                    "applyMaxToMarket": False,
                                },
                            ],
                        }
                    ]
                }
            )
        ]
    )
    gateway = BinanceSpotTestnetGateway(
        credentials=BinanceCredentials("key", "secret"),
        session=session,
    )
    _, market_order = make_order(OrderType.MARKET, price=None)

    parsed_rules = await gateway.symbol_rules("BTCUSDT")
    normalized = await gateway.normalize_order(
        market_order,
        reference_price=Decimal("1"),
    )

    assert parsed_rules.minimum_notional_applies_to_market is False
    assert parsed_rules.maximum_notional_applies_to_market is False
    assert normalized.quantity == Decimal("0.123")


@pytest.mark.asyncio
async def test_time_sync_uses_request_midpoint(monkeypatch) -> None:
    class Gateway(BinanceSpotTestnetGateway):
        async def _request(self, *args, **kwargs):
            return {"serverTime": 1100}

    timestamps = iter((1_000_000_000, 1_200_000_000))
    monkeypatch.setattr(
        "trading_platform_poc.binance_execution.time.time_ns",
        lambda: next(timestamps),
    )
    gateway = Gateway(credentials=BinanceCredentials("key", "secret"))

    assert await gateway.sync_time() == 0


@pytest.mark.asyncio
async def test_execution_service_updates_oms_from_binance_status() -> None:
    class Gateway:
        async def normalize_order(self, order, reference_price=None):
            return NormalizedBinanceOrder(
                quantity=order.quantity,
                price=order.price,
            )

        async def submit_order(
            self,
            order,
            *,
            reference_price=None,
            test_only=False,
        ):
            return BinanceOrderResult(
                symbol=order.symbol,
                order_id=1,
                client_order_id=order.client_order_id,
                status="FILLED",
                side="BUY",
                order_type="MARKET",
                original_quantity=order.quantity,
                executed_quantity=order.quantity,
                cumulative_quote_quantity=Decimal("12.3"),
                price=Decimal(0),
                transact_time_ms=1000,
                raw={},
            )

    oms, order = make_order(OrderType.MARKET, price=None)
    service = BinanceExecutionService(Gateway(), oms)

    await service.submit(
        order.order_id,
        reference_price=Decimal("100"),
    )

    assert oms.orders[0].state is OrderState.FILLED
    assert oms.orders[0].venue == "binance"
    assert [update.state for update in oms.updates][-3:] == [
        OrderState.SUBMITTED,
        OrderState.ACKNOWLEDGED,
        OrderState.FILLED,
    ]


@pytest.mark.asyncio
async def test_service_persists_exchange_normalized_quantity() -> None:
    class Gateway:
        async def normalize_order(self, order, reference_price=None):
            return NormalizedBinanceOrder(
                quantity=Decimal("0.123"),
                price=order.price,
            )

        async def submit_order(
            self,
            order,
            *,
            reference_price=None,
            test_only=False,
        ):
            return BinanceOrderResult(
                symbol=order.symbol,
                order_id=1,
                client_order_id=order.client_order_id,
                status="FILLED",
                side="BUY",
                order_type="LIMIT",
                original_quantity=Decimal("0.123"),
                executed_quantity=Decimal("0.123"),
                cumulative_quote_quantity=Decimal("12.3"),
                price=Decimal("100"),
                transact_time_ms=1000,
                raw={},
            )

    oms, order = make_order()
    service = BinanceExecutionService(Gateway(), oms)

    await service.submit(order.order_id)

    assert oms.orders[0].quantity == Decimal("0.123")
    assert (
        oms.updates[-1].cumulative_filled_quantity
        == Decimal("0.123")
    )


def test_cli_print_serializes_oms_struct(capsys) -> None:
    oms, order = make_order()

    _print({"order": order, "updates": oms.updates})

    output = capsys.readouterr().out
    assert str(order.order_id) in output
    assert '"quantity": "0.123456"' in output


@pytest.mark.asyncio
async def test_ambiguous_submission_queries_before_raising() -> None:
    class Gateway(BinanceSpotTestnetGateway):
        async def normalize_order(self, order, reference_price=None):
            from trading_platform_poc.binance_execution import (
                NormalizedBinanceOrder,
            )

            return NormalizedBinanceOrder(
                quantity=order.quantity,
                price=order.price,
            )

        async def _request(
            self,
            method,
            path,
            params=None,
            *,
            signed,
            _timestamp_retry=True,
        ):
            raise asyncio.TimeoutError

        async def query_order(
            self,
            symbol,
            *,
            order_id=None,
            client_order_id=None,
        ):
            raise BinanceAPIError(
                status=400,
                code=-2013,
                message="Order does not exist",
                payload={},
            )

    _, order = make_order()
    gateway = Gateway(
        credentials=BinanceCredentials("key", "secret")
    )

    with pytest.raises(BinanceAmbiguousOrderError) as error:
        await gateway.submit_order(order)

    assert error.value.client_order_id == order.client_order_id


@pytest.mark.asyncio
async def test_server_error_is_ambiguous_not_definitive_rejection() -> None:
    class Gateway(BinanceSpotTestnetGateway):
        async def normalize_order(self, order, reference_price=None):
            from trading_platform_poc.binance_execution import (
                NormalizedBinanceOrder,
            )

            return NormalizedBinanceOrder(
                quantity=order.quantity,
                price=order.price,
            )

        async def _request(
            self,
            method,
            path,
            params=None,
            *,
            signed,
            _timestamp_retry=True,
        ):
            raise BinanceAPIError(
                status=500,
                code=-1000,
                message="unknown execution status",
                payload={},
            )

        async def query_order(
            self,
            symbol,
            *,
            order_id=None,
            client_order_id=None,
        ):
            raise BinanceAPIError(
                status=503,
                code=-1000,
                message="temporarily unavailable",
                payload={},
            )

    oms, order = make_order()
    service = BinanceExecutionService(
        Gateway(credentials=BinanceCredentials("key", "secret")),
        oms,
    )

    with pytest.raises(BinanceAmbiguousOrderError):
        await service.submit(order.order_id)

    assert oms.orders[0].state is OrderState.SUBMITTED


@pytest.mark.parametrize("error_code", [-1006, -1007])
@pytest.mark.parametrize(
    ("query_status", "expected_state"),
    [
        ("NEW", OrderState.ACKNOWLEDGED),
        ("FILLED", OrderState.FILLED),
    ],
)
@pytest.mark.asyncio
async def test_http_4xx_unknown_submit_reconciles_by_client_order_id(
    error_code,
    query_status,
    expected_state,
) -> None:
    oms, order = make_order()
    submitted_quantity = Decimal("0.123")
    executed = (
        submitted_quantity
        if query_status == "FILLED"
        else Decimal(0)
    )
    session = FakeSession(
        [
            FakeResponse(
                {
                    "code": error_code,
                    "msg": "execution status unknown",
                },
                status=400,
            ),
            FakeResponse(
                {
                    "symbol": order.symbol,
                    "orderId": 123,
                    "clientOrderId": order.client_order_id,
                    "status": query_status,
                    "side": "BUY",
                    "type": "LIMIT",
                    "origQty": str(submitted_quantity),
                    "executedQty": str(executed),
                    "cummulativeQuoteQty": str(
                        executed * Decimal("100")
                    ),
                    "price": "100",
                }
            ),
        ]
    )
    gateway = BinanceSpotTestnetGateway(
        credentials=BinanceCredentials("key", "secret"),
        session=session,
    )
    gateway._symbol_rules["BTCUSDT"] = rules()
    service = BinanceExecutionService(gateway, oms)

    result = await service.submit(order.order_id)

    assert result.status == query_status
    assert oms.orders[0].state is expected_state
    assert [request[0] for request in session.requests] == ["POST", "GET"]
    query = parse_qs(urlparse(session.requests[1][1]).query)
    assert query["origClientOrderId"] == [order.client_order_id]


@pytest.mark.parametrize("initial_code", [-1006, -1007])
@pytest.mark.parametrize(
    ("query_code", "query_status"),
    [
        (-2013, 400),
        (-1007, 503),
    ],
)
@pytest.mark.asyncio
async def test_http_4xx_unknown_submit_stays_submitted_when_query_ambiguous(
    initial_code,
    query_code,
    query_status,
) -> None:
    oms, order = make_order()
    session = FakeSession(
        [
            FakeResponse(
                {
                    "code": initial_code,
                    "msg": "execution status unknown",
                },
                status=418,
            ),
            FakeResponse(
                {
                    "code": query_code,
                    "msg": "query outcome unavailable",
                },
                status=query_status,
            ),
        ]
    )
    gateway = BinanceSpotTestnetGateway(
        credentials=BinanceCredentials("key", "secret"),
        session=session,
    )
    gateway._symbol_rules["BTCUSDT"] = rules()
    service = BinanceExecutionService(gateway, oms)

    with pytest.raises(BinanceAmbiguousOrderError):
        await service.submit(order.order_id)

    assert oms.orders[0].state is OrderState.SUBMITTED
    assert oms.open_orders() == (oms.orders[0],)
    assert OrderState.REJECTED not in {
        update.state for update in oms.updates
    }
    assert [request[0] for request in session.requests] == ["POST", "GET"]


@pytest.mark.asyncio
async def test_ambiguous_cancel_is_resolved_by_order_query() -> None:
    _, order = make_order()
    response = {
        "symbol": "BTCUSDT",
        "orderId": 123,
        "clientOrderId": order.client_order_id,
        "status": "CANCELED",
        "side": "BUY",
        "type": "LIMIT",
        "origQty": "0.123",
        "executedQty": "0",
        "cummulativeQuoteQty": "0",
        "price": "100",
    }

    class Gateway(BinanceSpotTestnetGateway):
        async def _request(
            self,
            method,
            path,
            params=None,
            *,
            signed,
            _timestamp_retry=True,
        ):
            if method == "DELETE":
                raise BinanceAPIError(
                    status=503,
                    code=-1000,
                    message="execution status unknown",
                    payload={},
                )
            return response

    gateway = Gateway(credentials=BinanceCredentials("key", "secret"))

    result = await gateway.cancel_order(
        order.symbol,
        client_order_id=order.client_order_id,
    )

    assert result.status == "CANCELED"


@pytest.mark.parametrize("error_code", [-1006, -1007])
@pytest.mark.parametrize(
    ("query_status", "expected_state"),
    [
        ("NEW", OrderState.ACKNOWLEDGED),
        ("FILLED", OrderState.FILLED),
    ],
)
@pytest.mark.asyncio
async def test_http_4xx_unknown_cancel_reconciles_by_client_order_id(
    error_code,
    query_status,
    expected_state,
) -> None:
    oms, order = make_order()
    order = oms.assign_venue(order.order_id, "binance")
    order = oms.transition(order.order_id, OrderState.SUBMITTED, 3)
    order = oms.transition(order.order_id, OrderState.ACKNOWLEDGED, 4)
    executed = order.quantity if query_status == "FILLED" else Decimal(0)
    session = FakeSession(
        [
            FakeResponse(
                {
                    "code": error_code,
                    "msg": "execution status unknown",
                },
                status=400,
            ),
            FakeResponse(
                {
                    "symbol": order.symbol,
                    "orderId": 123,
                    "clientOrderId": order.client_order_id,
                    "status": query_status,
                    "side": "BUY",
                    "type": "LIMIT",
                    "origQty": str(order.quantity),
                    "executedQty": str(executed),
                    "cummulativeQuoteQty": str(
                        executed * Decimal("100")
                    ),
                    "price": "100",
                }
            ),
        ]
    )
    gateway = BinanceSpotTestnetGateway(
        credentials=BinanceCredentials("key", "secret"),
        session=session,
    )
    service = BinanceExecutionService(gateway, oms)

    result = await service.cancel(order.order_id)

    assert result.status == query_status
    assert oms.orders[0].state is expected_state
    assert [request[0] for request in session.requests] == ["DELETE", "GET"]
    query = parse_qs(urlparse(session.requests[1][1]).query)
    assert query["origClientOrderId"] == [order.client_order_id]


@pytest.mark.parametrize("initial_code", [-1006, -1007])
@pytest.mark.parametrize(
    ("query_code", "query_status"),
    [
        (-2013, 400),
        (-1007, 503),
    ],
)
@pytest.mark.asyncio
async def test_http_4xx_unknown_cancel_requires_reconciliation_when_unresolved(
    initial_code,
    query_code,
    query_status,
) -> None:
    oms, order = make_order()
    order = oms.assign_venue(order.order_id, "binance")
    order = oms.transition(order.order_id, OrderState.SUBMITTED, 3)
    order = oms.transition(order.order_id, OrderState.ACKNOWLEDGED, 4)
    session = FakeSession(
        [
            FakeResponse(
                {
                    "code": initial_code,
                    "msg": "execution status unknown",
                },
                status=429,
            ),
            FakeResponse(
                {
                    "code": query_code,
                    "msg": "query outcome unavailable",
                },
                status=query_status,
            ),
        ]
    )
    gateway = BinanceSpotTestnetGateway(
        credentials=BinanceCredentials("key", "secret"),
        session=session,
    )
    service = BinanceExecutionService(gateway, oms)

    with pytest.raises(BinanceAmbiguousOrderError):
        await service.cancel(order.order_id)

    assert oms.orders[0].state is OrderState.RECONCILIATION_REQUIRED
    assert oms.open_orders() == (oms.orders[0],)
    assert oms.updates[-1].reason == "binance_cancel_outcome_ambiguous"
    assert OrderState.REJECTED not in {
        update.state for update in oms.updates
    }
    assert [request[0] for request in session.requests] == ["DELETE", "GET"]


@pytest.mark.asyncio
async def test_ambiguous_cancel_does_not_reopen_terminal_order() -> None:
    oms, order = make_order()
    order = oms.assign_venue(order.order_id, "binance")
    order = oms.transition(order.order_id, OrderState.SUBMITTED, 3)
    order = oms.transition(order.order_id, OrderState.ACKNOWLEDGED, 4)
    order = oms.transition(
        order.order_id,
        OrderState.FILLED,
        5,
        cumulative_filled_quantity=order.quantity,
        average_fill_price=Decimal("100"),
    )
    update_count = len(oms.updates)

    class Gateway:
        async def cancel_order(self, *args, **kwargs):
            raise BinanceAmbiguousOrderError(order.client_order_id)

    service = BinanceExecutionService(Gateway(), oms)

    with pytest.raises(BinanceAmbiguousOrderError):
        await service.cancel(order.order_id)

    assert oms.orders[0].state is OrderState.FILLED
    assert len(oms.updates) == update_count


def test_cancel_response_keeps_original_client_order_id() -> None:
    result = BinanceOrderResult.from_payload(
        {
            "symbol": "BTCUSDT",
            "orderId": 1,
            "origClientOrderId": "original",
            "clientOrderId": "cancel-request",
            "status": "CANCELED",
            "side": "BUY",
            "type": "LIMIT",
        }
    )

    assert result.client_order_id == "original"


@pytest.mark.asyncio
async def test_non_json_success_response_is_treated_as_ambiguous() -> None:
    session = FakeSession(
        [FakeResponse({}, status=200)]
    )

    class InvalidResponse(FakeResponse):
        async def text(self):
            return "<html>proxy response</html>"

    session.responses = [InvalidResponse({}, status=200)]
    gateway = BinanceSpotTestnetGateway(
        credentials=BinanceCredentials("key", "secret"),
        session=session,
    )
    gateway._symbol_rules["BTCUSDT"] = rules()
    _, order = make_order()

    async def unavailable_query(*args, **kwargs):
        raise BinanceAPIError(
            status=503,
            code=-1000,
            message="unavailable",
            payload={},
        )

    gateway.query_order = unavailable_query

    with pytest.raises(BinanceAmbiguousOrderError):
        await gateway.submit_order(order)


@pytest.mark.asyncio
async def test_reconciliation_identifies_missing_and_unknown_orders() -> None:
    oms, local_order = make_order()
    local_order = oms.assign_venue(
        local_order.order_id,
        "binance",
    )
    local_order = oms.transition(
        local_order.order_id,
        OrderState.SUBMITTED,
        3,
    )

    class Gateway:
        async def open_orders(self, symbol):
            return (
                BinanceOrderResult(
                    symbol=symbol,
                    order_id=2,
                    client_order_id="unknown",
                    status="NEW",
                    side="BUY",
                    order_type="LIMIT",
                    original_quantity=Decimal("1"),
                    executed_quantity=Decimal("0"),
                    cumulative_quote_quantity=Decimal("0"),
                    price=Decimal("100"),
                    transact_time_ms=1,
                    raw={},
                ),
            )

        async def account(self):
            return {"balances": []}

        async def my_trades(self, symbol):
            return ()

    gateway = Gateway()
    report = await BinanceSpotTestnetGateway.reconcile(
        gateway,
        oms,
        "BTCUSDT",
    )

    assert report.missing_on_exchange == (
        local_order.client_order_id,
    )
    assert report.unknown_on_exchange == ("unknown",)


@pytest.mark.asyncio
async def test_mixed_venue_reconciliation_only_owns_binance_orders() -> None:
    oms = OrderManagementSystem()

    def create_owned_order(
        venue: str,
        *,
        reconciliation_required: bool = False,
    ):
        intent = OrderIntent(
            uuid4(),
            venue,
            "BTCUSDT",
            Side.BUY,
            OrderType.LIMIT,
            Decimal("0.01"),
            Decimal("100"),
            1,
        )
        order = oms.create(intent)
        order = oms.assign_venue(order.order_id, venue)
        order = oms.transition(
            order.order_id,
            OrderState.RISK_APPROVED,
            2,
        )
        order = oms.transition(
            order.order_id,
            OrderState.SUBMITTED,
            3,
        )
        if reconciliation_required:
            order = oms.transition(
                order.order_id,
                OrderState.RECONCILIATION_REQUIRED,
                4,
                reason="operator_recovery",
            )
        return order

    binance_order = create_owned_order("binance")
    okx_order = create_owned_order("okx")
    deribit_order = create_owned_order(
        "deribit",
        reconciliation_required=True,
    )

    class Gateway:
        def __init__(self):
            self.queries = []

        async def open_orders(self, symbol):
            return (
                BinanceOrderResult(
                    symbol=symbol,
                    order_id=99,
                    client_order_id=okx_order.client_order_id,
                    status="NEW",
                    side="BUY",
                    order_type="LIMIT",
                    original_quantity=okx_order.quantity,
                    executed_quantity=Decimal(0),
                    cumulative_quote_quantity=Decimal(0),
                    price=Decimal("100"),
                    transact_time_ms=1,
                    raw={},
                ),
            )

        async def account(self):
            return {"balances": []}

        async def my_trades(self, symbol):
            return ()

        async def reconcile(self, current_oms, symbol):
            return await BinanceSpotTestnetGateway.reconcile(
                self,
                current_oms,
                symbol,
            )

        async def query_order(
            self,
            symbol,
            *,
            order_id=None,
            client_order_id=None,
        ):
            self.queries.append(client_order_id)
            raise BinanceAPIError(
                status=400,
                code=-2013,
                message="Order does not exist",
                payload={},
            )

    gateway = Gateway()
    service = BinanceExecutionService(gateway, oms)

    report = await service.reconcile("BTCUSDT")

    assert report.missing_on_exchange == (
        binance_order.client_order_id,
    )
    assert report.reconciliation_required == (
        binance_order.client_order_id,
    )
    assert report.unknown_on_exchange == (
        okx_order.client_order_id,
    )
    assert gateway.queries == [binance_order.client_order_id]
    orders = {order.venue: order for order in oms.orders}
    assert orders["binance"].state is OrderState.RECONCILIATION_REQUIRED
    assert orders["okx"].state is OrderState.SUBMITTED
    assert orders["deribit"].state is OrderState.RECONCILIATION_REQUIRED
    assert okx_order.client_order_id not in report.missing_on_exchange
    assert (
        deribit_order.client_order_id
        not in report.reconciliation_required
    )


@pytest.mark.asyncio
async def test_execution_service_reconciliation_applies_known_status() -> None:
    oms, order = make_order()
    order = oms.assign_venue(order.order_id, "binance")
    order = oms.transition(
        order.order_id,
        OrderState.SUBMITTED,
        3,
    )
    exchange_result = BinanceOrderResult(
        symbol="BTCUSDT",
        order_id=5,
        client_order_id=order.client_order_id,
        status="FILLED",
        side="BUY",
        order_type="LIMIT",
        original_quantity=order.quantity,
        executed_quantity=order.quantity,
        cumulative_quote_quantity=Decimal("12.3"),
        price=Decimal("100"),
        transact_time_ms=1000,
        raw={},
    )

    class Gateway:
        async def reconcile(self, oms, symbol):
            from trading_platform_poc.binance_execution import (
                BinanceReconciliationReport,
            )

            return BinanceReconciliationReport(
                symbol=symbol,
                exchange_open_orders=(exchange_result,),
                missing_on_exchange=(),
                unknown_on_exchange=(),
                account={},
                recent_trades=(),
            )

    service = BinanceExecutionService(Gateway(), oms)

    await service.reconcile("BTCUSDT")

    assert oms.orders[0].state is OrderState.FILLED


@pytest.mark.parametrize(
    "starting_state",
    [
        OrderState.SUBMITTED,
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIALLY_FILLED,
    ],
)
@pytest.mark.asyncio
async def test_order_not_found_requires_operator_reconciliation(
    starting_state,
) -> None:
    oms, order = make_order()
    order = oms.assign_venue(order.order_id, "binance")
    order = oms.transition(
        order.order_id,
        OrderState.SUBMITTED,
        3,
    )
    if starting_state in {
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIALLY_FILLED,
    }:
        order = oms.transition(
            order.order_id,
            OrderState.ACKNOWLEDGED,
            4,
        )
    if starting_state is OrderState.PARTIALLY_FILLED:
        order = oms.transition(
            order.order_id,
            OrderState.PARTIALLY_FILLED,
            5,
            cumulative_filled_quantity=Decimal("0.02"),
            average_fill_price=Decimal("100"),
        )

    class Gateway:
        def __init__(self):
            self.query_count = 0

        async def reconcile(self, current_oms, symbol):
            unresolved = tuple(
                item.client_order_id
                for item in current_oms.open_orders()
                if (
                    item.symbol == symbol
                    and item.state
                    is OrderState.RECONCILIATION_REQUIRED
                )
            )
            missing = (
                ()
                if unresolved
                else (order.client_order_id,)
            )
            return BinanceReconciliationReport(
                symbol=symbol,
                exchange_open_orders=(),
                missing_on_exchange=missing,
                unknown_on_exchange=(),
                account={},
                recent_trades=(),
                reconciliation_required=unresolved,
            )

        async def query_order(self, *args, **kwargs):
            self.query_count += 1
            raise BinanceAPIError(
                status=400,
                code=-2013,
                message="Order does not exist",
                payload={},
            )

    gateway = Gateway()
    service = BinanceExecutionService(gateway, oms)

    first_report = await service.reconcile("BTCUSDT")
    second_report = await service.reconcile("BTCUSDT")

    assert oms.orders[0].state is OrderState.RECONCILIATION_REQUIRED
    assert oms.orders[0] in oms.open_orders()
    assert first_report.reconciliation_required == (
        order.client_order_id,
    )
    assert second_report.reconciliation_required == (
        order.client_order_id,
    )
    assert gateway.query_count == 1
    assert oms.updates[-1].reason == (
        "binance_order_not_found_requires_operator_recovery"
    )
    expected_fill = (
        Decimal("0.02")
        if starting_state is OrderState.PARTIALLY_FILLED
        else Decimal(0)
    )
    assert (
        oms.updates[-1].cumulative_filled_quantity
        == expected_fill
    )


def test_reconciliation_required_order_blocks_duplicate_side_quote() -> None:
    oms, order = make_order()
    order = oms.assign_venue(order.order_id, "binance")
    order = oms.transition(
        order.order_id,
        OrderState.SUBMITTED,
        3,
    )
    oms.transition(
        order.order_id,
        OrderState.RECONCILIATION_REQUIRED,
        4,
        reason="operator_recovery",
    )
    book = BinanceOrderBook("BTCUSDT")
    book.load_snapshot(
        BookSnapshot(
            "BTCUSDT",
            10,
            (PriceLevel(Decimal("100"), Decimal("1")),),
            (PriceLevel(Decimal("101"), Decimal("1")),),
        ),
        1,
    )
    strategy = SpreadCaptureStrategy(
        StrategyConfig(
            spread_threshold_bps=Decimal("1"),
            quote_cooldown_ms=0,
        )
    )

    intents = strategy.evaluate(book, 10, oms.open_orders())

    assert len(oms.open_orders()) == 1
    assert tuple(intent.side for intent in intents) == (Side.SELL,)


def test_run_report_counts_reconciliation_required_order(tmp_path) -> None:
    engine = TradingEngine(AppConfig())
    intent = OrderIntent(
        uuid4(),
        "binance",
        "BTCUSDT",
        Side.BUY,
        OrderType.LIMIT,
        Decimal("0.01"),
        Decimal("100"),
        1,
    )
    order = engine.oms.create(intent)
    order = engine.oms.assign_venue(order.order_id, "binance")
    order = engine.oms.transition(
        order.order_id,
        OrderState.RISK_APPROVED,
        2,
    )
    order = engine.oms.transition(
        order.order_id,
        OrderState.SUBMITTED,
        3,
    )
    engine.oms.transition(
        order.order_id,
        OrderState.RECONCILIATION_REQUIRED,
        4,
        reason="operator_recovery",
    )

    summary = write_report(
        tmp_path / "report",
        uuid4(),
        "binance",
        engine,
    )

    assert summary["open_order_count"] == 1
    assert summary["reconciliation_required_order_count"] == 1
    assert summary["orders_by_venue"] == {"binance": 1}
    assert "reconciliation_required" in (
        tmp_path / "report" / "orders.csv"
    ).read_text(encoding="utf-8")
    assert ",binance," in (
        tmp_path / "report" / "orders.csv"
    ).read_text(encoding="utf-8")


def test_repeated_partial_fills_update_cumulative_quantity() -> None:
    oms, order = make_order()
    order = oms.transition(
        order.order_id,
        OrderState.SUBMITTED,
        3,
    )
    service = BinanceExecutionService(object(), oms)

    for executed in ("0.02", "0.07"):
        service.apply_result(
            order.order_id,
            BinanceOrderResult(
                symbol="BTCUSDT",
                order_id=1,
                client_order_id=order.client_order_id,
                status="PARTIALLY_FILLED",
                side="BUY",
                order_type="LIMIT",
                original_quantity=order.quantity,
                executed_quantity=Decimal(executed),
                cumulative_quote_quantity=Decimal(executed) * Decimal("100"),
                price=Decimal("100"),
                transact_time_ms=1000,
                raw={},
            ),
        )

    assert oms.orders[0].state is OrderState.PARTIALLY_FILLED
    assert oms.updates[-1].cumulative_filled_quantity == Decimal("0.07")


def test_rejects_regressing_cumulative_partial_fill() -> None:
    oms, order = make_order()
    order = oms.transition(order.order_id, OrderState.SUBMITTED, 3)
    service = BinanceExecutionService(object(), oms)

    for executed in ("0.07", "0.02"):
        result = BinanceOrderResult(
            symbol="BTCUSDT",
            order_id=1,
            client_order_id=order.client_order_id,
            status="PARTIALLY_FILLED",
            side="BUY",
            order_type="LIMIT",
            original_quantity=order.quantity,
            executed_quantity=Decimal(executed),
            cumulative_quote_quantity=Decimal(executed) * Decimal("100"),
            price=Decimal("100"),
            transact_time_ms=1000,
            raw={},
        )
        if executed == "0.07":
            service.apply_result(order.order_id, result)
        else:
            with pytest.raises(BinanceGatewayError, match="regressed"):
                service.apply_result(order.order_id, result)


@pytest.mark.asyncio
async def test_cli_builds_risk_context_from_exchange_state() -> None:
    class Gateway:
        async def ticker_price(self, symbol):
            return Decimal("100")

        async def symbol_rules(self, symbol):
            return rules()

        async def account(self):
            return {
                "canTrade": True,
                "balances": [
                    {"asset": "BTC", "free": "0", "locked": "0"},
                    {
                        "asset": "USDT",
                        "free": "1000",
                        "locked": "0",
                    },
                ],
            }

        async def open_orders(self, symbol=None):
            return (
                BinanceOrderResult(
                    symbol="BTCUSDT",
                    order_id=1,
                    client_order_id="open",
                    status="NEW",
                    side="BUY",
                    order_type="LIMIT",
                    original_quantity=Decimal("0.1"),
                    executed_quantity=Decimal("0"),
                    cumulative_quote_quantity=Decimal("0"),
                    price=Decimal("100"),
                    transact_time_ms=1,
                    raw={},
                ),
            )

    args = argparse.Namespace(
        test_only=True,
        confirm=None,
        order_type="market",
        price=None,
        symbol="BTCUSDT",
        side="buy",
        quantity=Decimal("0.1"),
        daily_net_pnl=Decimal("0"),
    )
    config = AppConfig(
        risk=RiskConfig(
            max_open_orders=1,
            max_net_position=Decimal("1"),
        )
    )

    with pytest.raises(ValueError, match="max_open_orders"):
        await _submit(args, config, Gateway())


@pytest.mark.asyncio
async def test_cli_uses_worst_case_same_side_pending_exposure() -> None:
    def open_order(side: str, client_order_id: str):
        return BinanceOrderResult(
            symbol="BTCUSDT",
            order_id=1,
            client_order_id=client_order_id,
            status="NEW",
            side=side,
            order_type="LIMIT",
            original_quantity=Decimal("0.05"),
            executed_quantity=Decimal("0"),
            cumulative_quote_quantity=Decimal("0"),
            price=Decimal("100"),
            transact_time_ms=1,
            raw={},
        )

    class Gateway:
        async def ticker_price(self, symbol):
            return Decimal("100")

        async def symbol_rules(self, symbol):
            return rules()

        async def account(self):
            return {
                "canTrade": True,
                "balances": [
                    {"asset": "BTC", "free": "0", "locked": "0"},
                    {
                        "asset": "USDT",
                        "free": "1000",
                        "locked": "0",
                    },
                ],
            }

        async def open_orders(self, symbol=None):
            return (
                open_order("BUY", "buy"),
                open_order("SELL", "sell"),
            )

    args = argparse.Namespace(
        test_only=True,
        confirm=None,
        order_type="market",
        price=None,
        symbol="BTCUSDT",
        side="buy",
        quantity=Decimal("0.05"),
        daily_net_pnl=Decimal("0"),
    )
    config = AppConfig(
        risk=RiskConfig(
            max_open_orders=10,
            max_net_position=Decimal("0.09"),
        )
    )

    with pytest.raises(ValueError, match="max_net_position"):
        await _submit(args, config, Gateway())


@pytest.mark.asyncio
async def test_cli_does_not_spend_locked_balance() -> None:
    class Gateway:
        async def ticker_price(self, symbol):
            return Decimal("100")

        async def symbol_rules(self, symbol):
            return rules()

        async def account(self):
            return {
                "canTrade": True,
                "balances": [
                    {"asset": "BTC", "free": "0", "locked": "0"},
                    {"asset": "USDT", "free": "0", "locked": "1000"},
                ],
            }

        async def open_orders(self, symbol=None):
            return ()

    args = argparse.Namespace(
        test_only=True,
        confirm=None,
        order_type="market",
        price=None,
        symbol="BTCUSDT",
        side="buy",
        quantity=Decimal("0.1"),
        daily_net_pnl=Decimal("0"),
    )

    with pytest.raises(ValueError, match="insufficient USDT"):
        await _submit(args, AppConfig(), Gateway())


def _new_order_response(client_order_id: str, order_id: int = 991) -> dict:
    return {
        "cancelResult": "SUCCESS",
        "newOrderResult": "SUCCESS",
        "cancelResponse": {
            "symbol": "BTCUSDT",
            "orderId": 42,
            "clientOrderId": "old-client-id",
            "status": "CANCELED",
        },
        "newOrderResponse": {
            "symbol": "BTCUSDT",
            "orderId": order_id,
            "clientOrderId": client_order_id,
            "status": "NEW",
            "side": "BUY",
            "type": "LIMIT",
            "origQty": "0.123",
            "executedQty": "0",
            "cummulativeQuoteQty": "0",
            "price": "100.12",
        },
    }


def _query_response(client_order_id: str, order_id: int, status: str) -> dict:
    return {
        "symbol": "BTCUSDT",
        "orderId": order_id,
        "clientOrderId": client_order_id,
        "status": status,
        "side": "BUY",
        "type": "LIMIT",
        "origQty": "0.123",
        "executedQty": "0",
        "cummulativeQuoteQty": "0",
        "price": "100.12",
    }


def _replace_gateway(session) -> BinanceSpotTestnetGateway:
    gateway = BinanceSpotTestnetGateway(
        credentials=BinanceCredentials("key", "secret"),
        session=session,
    )
    gateway._symbol_rules["BTCUSDT"] = rules()
    return gateway


@pytest.mark.asyncio
async def test_replace_uses_the_atomic_cancel_replace_endpoint() -> None:
    _, order = make_order()
    session = FakeSession(
        [FakeResponse(_new_order_response(order.client_order_id))]
    )
    gateway = _replace_gateway(session)

    result = await gateway.replace_order(
        order,
        cancel_client_order_id="old-client-id",
    )

    assert result.client_order_id == order.client_order_id
    assert result.order_id == 991
    method, url, _headers, _kwargs = session.requests[0]
    assert method == "POST"
    assert urlparse(url).path == "/api/v3/order/cancelReplace"
    query = parse_qs(urlparse(url).query)
    assert query["cancelOrigClientOrderId"] == ["old-client-id"]
    assert query["newClientOrderId"] == [order.client_order_id]


@pytest.mark.asyncio
async def test_replace_stops_on_failure_so_two_orders_never_rest() -> None:
    """ALLOW_FAILURE could leave the replacement beside the original."""
    _, order = make_order()
    session = FakeSession(
        [FakeResponse(_new_order_response(order.client_order_id))]
    )
    gateway = _replace_gateway(session)

    await gateway.replace_order(order, cancel_client_order_id="old")

    query = parse_qs(urlparse(session.requests[0][1]).query)
    assert query["cancelReplaceMode"] == ["STOP_ON_FAILURE"]


@pytest.mark.asyncio
async def test_replace_cancels_by_exchange_order_id_when_given() -> None:
    _, order = make_order()
    session = FakeSession(
        [FakeResponse(_new_order_response(order.client_order_id))]
    )
    gateway = _replace_gateway(session)

    await gateway.replace_order(order, cancel_order_id=4242)

    query = parse_qs(urlparse(session.requests[0][1]).query)
    assert query["cancelOrderId"] == ["4242"]
    assert "cancelOrigClientOrderId" not in query


@pytest.mark.asyncio
async def test_replace_requires_the_order_being_replaced() -> None:
    _, order = make_order()
    gateway = _replace_gateway(FakeSession([]))

    with pytest.raises(BinanceGatewayError, match="cancel_order_id"):
        await gateway.replace_order(order)


@pytest.mark.asyncio
async def test_replace_reraises_a_clean_rejection_without_requerying() -> None:
    _, order = make_order()
    session = FakeSession(
        [
            FakeResponse(
                {"code": -2011, "msg": "Unknown order sent."},
                status=400,
            )
        ]
    )
    gateway = _replace_gateway(session)

    with pytest.raises(BinanceAPIError):
        await gateway.replace_order(order, cancel_client_order_id="old")

    assert len(session.requests) == 1


@pytest.mark.parametrize("unknown_code", [-1006, -1007])
@pytest.mark.asyncio
async def test_replace_resolves_an_unknown_outcome_to_the_new_order(
    unknown_code,
) -> None:
    _, order = make_order()
    session = FakeSession(
        [
            FakeResponse(
                {"code": unknown_code, "msg": "execution status unknown"},
                status=418,
            ),
            FakeResponse(
                _query_response(order.client_order_id, 991, "NEW")
            ),
        ]
    )
    gateway = _replace_gateway(session)

    result = await gateway.replace_order(
        order,
        cancel_client_order_id="old-client-id",
    )

    assert result.client_order_id == order.client_order_id
    assert [request[0] for request in session.requests] == ["POST", "GET"]


@pytest.mark.asyncio
async def test_replace_falls_back_to_the_original_order() -> None:
    """If the replacement never existed, report the surviving original."""
    _, order = make_order()
    session = FakeSession(
        [
            FakeResponse(
                {"code": -1006, "msg": "execution status unknown"},
                status=418,
            ),
            FakeResponse(
                {"code": -2013, "msg": "Order does not exist."},
                status=400,
            ),
            FakeResponse(
                _query_response("old-client-id", 42, "NEW")
            ),
        ]
    )
    gateway = _replace_gateway(session)

    result = await gateway.replace_order(
        order,
        cancel_client_order_id="old-client-id",
    )

    assert result.client_order_id == "old-client-id"
    assert [request[0] for request in session.requests] == [
        "POST",
        "GET",
        "GET",
    ]


@pytest.mark.asyncio
async def test_replace_raises_when_neither_order_can_be_resolved() -> None:
    _, order = make_order()
    session = FakeSession(
        [
            FakeResponse(
                {"code": -1006, "msg": "execution status unknown"},
                status=418,
            ),
            FakeResponse(
                {"code": -2013, "msg": "Order does not exist."},
                status=400,
            ),
            FakeResponse(
                {"code": -1007, "msg": "timeout"},
                status=503,
            ),
        ]
    )
    gateway = _replace_gateway(session)

    with pytest.raises(BinanceAmbiguousOrderError):
        await gateway.replace_order(
            order,
            cancel_client_order_id="old-client-id",
        )


@pytest.mark.asyncio
async def test_replace_requeries_when_the_response_is_malformed() -> None:
    """A 200 without newOrderResponse must not be trusted as success."""
    _, order = make_order()
    session = FakeSession(
        [
            FakeResponse({"cancelResult": "SUCCESS"}),
            FakeResponse(
                _query_response(order.client_order_id, 991, "NEW")
            ),
        ]
    )
    gateway = _replace_gateway(session)

    result = await gateway.replace_order(
        order,
        cancel_client_order_id="old-client-id",
    )

    assert result.client_order_id == order.client_order_id
    assert [request[0] for request in session.requests] == ["POST", "GET"]


@pytest.mark.asyncio
async def test_service_replace_cancels_the_replaced_oms_order() -> None:
    oms, replaced = make_order()
    replaced = oms.assign_venue(replaced.order_id, "binance")
    replaced = oms.transition(
        replaced.order_id,
        OrderState.SUBMITTED,
        3,
    )
    intent = OrderIntent(
        uuid4(),
        "test",
        "BTCUSDT",
        Side.BUY,
        OrderType.LIMIT,
        Decimal("0.123456"),
        Decimal("101.50"),
        4,
    )
    replacement = oms.create(intent)
    replacement = oms.transition(
        replacement.order_id,
        OrderState.RISK_APPROVED,
        5,
    )
    session = FakeSession(
        [FakeResponse(_new_order_response(replacement.client_order_id))]
    )
    service = BinanceExecutionService(_replace_gateway(session), oms)

    await service.replace(
        replacement.order_id,
        replaced_order_id=replaced.order_id,
    )

    states = {order.order_id: order.state for order in oms.orders}
    assert states[replaced.order_id] is OrderState.CANCELED
    assert states[replacement.order_id] is OrderState.ACKNOWLEDGED
    query = parse_qs(urlparse(session.requests[0][1]).query)
    assert query["cancelOrigClientOrderId"] == [replaced.client_order_id]


@pytest.mark.asyncio
async def test_service_replace_requires_risk_approval() -> None:
    oms, order = make_order()
    oms.transition(order.order_id, OrderState.SUBMITTED, 3)
    service = BinanceExecutionService(_replace_gateway(FakeSession([])), oms)

    with pytest.raises(BinanceGatewayError, match="risk-approved"):
        await service.replace(
            order.order_id,
            cancel_client_order_id="old",
        )


@pytest.mark.asyncio
async def test_service_replace_flags_a_replacement_that_never_went_live(
) -> None:
    oms, order = make_order()
    session = FakeSession(
        [
            FakeResponse(
                {"code": -1006, "msg": "execution status unknown"},
                status=418,
            ),
            FakeResponse(
                {"code": -2013, "msg": "Order does not exist."},
                status=400,
            ),
            FakeResponse(_query_response("old-client-id", 42, "NEW")),
        ]
    )
    service = BinanceExecutionService(_replace_gateway(session), oms)

    result = await service.replace(
        order.order_id,
        cancel_client_order_id="old-client-id",
    )

    assert result.client_order_id == "old-client-id"
    assert oms.orders[0].state is OrderState.RECONCILIATION_REQUIRED


def _account_payload() -> dict:
    return {
        "canTrade": True,
        "balances": [
            {"asset": "BTC", "free": "0.02", "locked": "0.0"},
            {"asset": "USDT", "free": "500000.0", "locked": "0.0"},
        ],
    }


@pytest.mark.asyncio
async def test_modify_command_replaces_a_resting_order() -> None:
    from trading_platform_poc.binance_cli import _modify

    args = argparse.Namespace(
        symbol="BTCUSDT",
        cancel_order_id=None,
        cancel_client_order_id="old-client-id",
        side="buy",
        order_type="limit",
        quantity=Decimal("0.020"),
        price=Decimal("1000.00"),
        daily_net_pnl=Decimal("0"),
        confirm="TESTNET",
    )
    session = FakeSession(
        [
            FakeResponse(_query_response("old-client-id", 42, "NEW")),
            FakeResponse({"symbol": "BTCUSDT", "price": "1000.00"}),
            FakeResponse(_account_payload()),
            FakeResponse([]),
            FakeResponse(_new_order_response("placeholder")),
        ]
    )
    gateway = _replace_gateway(session)
    config = AppConfig()

    captured: list = []
    await _modify_capturing(_modify, args, config, gateway, captured, session)

    paths = [urlparse(request[1]).path for request in session.requests]
    assert "/api/v3/order/cancelReplace" in paths


async def _modify_capturing(
    modify,
    args,
    config,
    gateway,
    captured,
    session,
) -> None:
    """Run _modify, letting the replacement id flow into the stub."""
    original_request = session.request

    def request(method, url, headers=None, **kwargs):
        query = parse_qs(urlparse(url).query)
        if urlparse(url).path == "/api/v3/order/cancelReplace":
            client_order_id = query["newClientOrderId"][0]
            captured.append(client_order_id)
            session.responses[0] = FakeResponse(
                _new_order_response(client_order_id)
            )
        return original_request(method, url, headers=headers, **kwargs)

    session.request = request
    await modify(args, config, gateway)


@pytest.mark.asyncio
async def test_modify_command_refuses_an_order_that_is_not_resting() -> None:
    from trading_platform_poc.binance_cli import _modify

    args = argparse.Namespace(
        symbol="BTCUSDT",
        cancel_order_id=None,
        cancel_client_order_id="old-client-id",
        side="buy",
        order_type="limit",
        quantity=Decimal("0.010"),
        price=Decimal("100.00"),
        daily_net_pnl=Decimal("0"),
        confirm="TESTNET",
    )
    session = FakeSession(
        [FakeResponse(_query_response("old-client-id", 42, "FILLED"))]
    )
    gateway = _replace_gateway(session)

    with pytest.raises(ValueError, match="resting"):
        await _modify(args, AppConfig(), gateway)


@pytest.mark.asyncio
async def test_modify_command_requires_a_price_for_limit_orders() -> None:
    from trading_platform_poc.binance_cli import _modify

    args = argparse.Namespace(
        symbol="BTCUSDT",
        cancel_order_id=None,
        cancel_client_order_id="old-client-id",
        side="buy",
        order_type="limit",
        quantity=Decimal("0.010"),
        price=None,
        daily_net_pnl=Decimal("0"),
        confirm="TESTNET",
    )
    gateway = _replace_gateway(FakeSession([]))

    with pytest.raises(ValueError, match="require --price"):
        await _modify(args, AppConfig(), gateway)


@pytest.mark.asyncio
async def test_modify_does_not_count_the_replaced_order_against_the_limit(
) -> None:
    """A replace withdraws one order as it adds one, so the net is zero."""
    from trading_platform_poc.binance_cli import _modify

    args = argparse.Namespace(
        symbol="BTCUSDT",
        cancel_order_id=None,
        cancel_client_order_id="old-client-id",
        side="buy",
        order_type="limit",
        quantity=Decimal("0.020"),
        price=Decimal("1000.00"),
        daily_net_pnl=Decimal("0"),
        confirm="TESTNET",
    )
    resting = _query_response("old-client-id", 42, "NEW")
    session = FakeSession(
        [
            FakeResponse(resting),
            FakeResponse({"symbol": "BTCUSDT", "price": "1000.00"}),
            FakeResponse(_account_payload()),
            FakeResponse([resting]),
            FakeResponse(_new_order_response("placeholder")),
        ]
    )
    gateway = _replace_gateway(session)
    config = AppConfig(risk=RiskConfig(max_open_orders=1))

    captured: list = []
    await _modify_capturing(_modify, args, config, gateway, captured, session)

    paths = [urlparse(request[1]).path for request in session.requests]
    assert "/api/v3/order/cancelReplace" in paths


def _multi_leg_args(*legs: str, test_only: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        leg=list(legs),
        daily_net_pnl=Decimal("0"),
        test_only=test_only,
        confirm=None if test_only else "TESTNET",
    )


def _eth_rules() -> BinanceSymbolRules:
    return replace(rules(), symbol="ETHUSDT", base_asset="ETH")


def _multi_leg_gateway(session) -> BinanceSpotTestnetGateway:
    gateway = BinanceSpotTestnetGateway(
        credentials=BinanceCredentials("key", "secret"),
        session=session,
    )
    gateway._symbol_rules["BTCUSDT"] = rules()
    gateway._symbol_rules["ETHUSDT"] = _eth_rules()
    return gateway


def _multi_leg_account() -> dict:
    return {
        "canTrade": True,
        "balances": [
            {"asset": "BTC", "free": "0.02", "locked": "0.0"},
            {"asset": "ETH", "free": "0.03", "locked": "0.0"},
            {"asset": "USDT", "free": "500000.0", "locked": "0.0"},
        ],
    }


def _submitted(client_order_id: str, symbol: str, order_id: int) -> dict:
    return {
        "symbol": symbol,
        "orderId": order_id,
        "clientOrderId": client_order_id,
        "status": "NEW",
        "side": "BUY",
        "type": "LIMIT",
        "origQty": "0.020",
        "executedQty": "0",
        "cummulativeQuoteQty": "0",
        "price": "1000.00",
    }


class RecordingSession(FakeSession):
    """Answers by endpoint so concurrent legs cannot depend on ordering."""

    def __init__(self, account, ticker="1000.00", submit_status=200):
        super().__init__([])
        self.account_payload = account
        self.ticker = ticker
        self.submit_status = submit_status
        self.submitted: list[str] = []

    def request(self, method, url, headers=None, **kwargs):
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        # Signed calls put parameters in the URL; unsigned ones pass them
        # as aiohttp params, so both have to be consulted.
        for key, value in (kwargs.get("params") or {}).items():
            query.setdefault(key, [str(value)])
        self.requests.append((method, url, headers or {}, kwargs))
        if parsed.path == "/api/v3/account":
            return FakeResponse(self.account_payload)
        if parsed.path == "/api/v3/openOrders":
            return FakeResponse([])
        if parsed.path == "/api/v3/ticker/price":
            return FakeResponse(
                {"symbol": query["symbol"][0], "price": self.ticker}
            )
        if parsed.path == "/api/v3/order":
            client_order_id = query["newClientOrderId"][0]
            self.submitted.append(client_order_id)
            if self.submit_status != 200:
                return FakeResponse(
                    {"code": -2010, "msg": "rejected"},
                    status=self.submit_status,
                )
            return FakeResponse(
                _submitted(
                    client_order_id,
                    query["symbol"][0],
                    len(self.submitted),
                )
            )
        raise AssertionError(f"unexpected call to {parsed.path}")


@pytest.mark.asyncio
async def test_multi_leg_dispatches_every_admitted_leg() -> None:
    from trading_platform_poc.binance_cli import _multi_leg

    session = RecordingSession(_multi_leg_account())
    gateway = _multi_leg_gateway(session)

    await _multi_leg(
        _multi_leg_args(
            "BTCUSDT:buy:limit:0.020:1000.00",
            "ETHUSDT:buy:limit:0.020:1000.00",
        ),
        AppConfig(),
        gateway,
    )

    assert len(session.submitted) == 2
    assert len(set(session.submitted)) == 2


@pytest.mark.asyncio
async def test_multi_leg_sends_nothing_when_one_leg_fails_risk() -> None:
    """Partial admission would leave one side of the trade exposed."""
    from trading_platform_poc.binance_cli import _multi_leg

    session = RecordingSession(_multi_leg_account())
    gateway = _multi_leg_gateway(session)

    with pytest.raises(ValueError, match="risk rejected leg ETHUSDT"):
        await _multi_leg(
            _multi_leg_args(
                "BTCUSDT:buy:limit:0.020:1000.00",
                # 5 ETH is far beyond the 0.05 net position limit.
                "ETHUSDT:buy:limit:5:1000.00",
            ),
            AppConfig(),
            gateway,
        )

    assert session.submitted == []


@pytest.mark.asyncio
async def test_multi_leg_sends_nothing_when_the_trade_is_unaffordable(
) -> None:
    from trading_platform_poc.binance_cli import _multi_leg

    poor = {
        "canTrade": True,
        "balances": [
            {"asset": "BTC", "free": "0.0", "locked": "0.0"},
            {"asset": "ETH", "free": "0.0", "locked": "0.0"},
            {"asset": "USDT", "free": "25.0", "locked": "0.0"},
        ],
    }
    session = RecordingSession(poor)
    gateway = _multi_leg_gateway(session)

    with pytest.raises(ValueError, match="insufficient Testnet balance"):
        await _multi_leg(
            _multi_leg_args(
                "BTCUSDT:buy:limit:0.020:1000.00",
                "ETHUSDT:buy:limit:0.020:1000.00",
            ),
            AppConfig(),
            gateway,
        )

    assert session.submitted == []


@pytest.mark.asyncio
async def test_multi_leg_sums_the_cost_of_every_leg() -> None:
    """Each leg is affordable alone; together they are not."""
    from trading_platform_poc.binance_cli import _multi_leg

    account = {
        "canTrade": True,
        "balances": [
            {"asset": "BTC", "free": "0.0", "locked": "0.0"},
            {"asset": "ETH", "free": "0.0", "locked": "0.0"},
            {"asset": "USDT", "free": "30.0", "locked": "0.0"},
        ],
    }
    session = RecordingSession(account)
    gateway = _multi_leg_gateway(session)

    with pytest.raises(ValueError, match="insufficient Testnet balance"):
        await _multi_leg(
            _multi_leg_args(
                "BTCUSDT:buy:limit:0.020:1000.00",
                "ETHUSDT:buy:limit:0.020:1000.00",
            ),
            AppConfig(),
            gateway,
        )

    assert session.submitted == []


@pytest.mark.asyncio
async def test_multi_leg_reports_a_leg_that_fails_at_dispatch() -> None:
    from trading_platform_poc.binance_cli import _multi_leg

    session = RecordingSession(_multi_leg_account(), submit_status=400)
    gateway = _multi_leg_gateway(session)

    await _multi_leg(
        _multi_leg_args(
            "BTCUSDT:buy:limit:0.020:1000.00",
            "ETHUSDT:buy:limit:0.020:1000.00",
        ),
        AppConfig(),
        gateway,
    )

    # Both legs were attempted; neither failure hid the other.
    assert len(session.submitted) == 2


@pytest.mark.asyncio
async def test_multi_leg_requires_at_least_two_legs() -> None:
    from trading_platform_poc.binance_cli import _multi_leg

    gateway = _multi_leg_gateway(RecordingSession(_multi_leg_account()))

    with pytest.raises(ValueError, match="at least two legs"):
        await _multi_leg(
            _multi_leg_args("BTCUSDT:buy:limit:0.020:1000.00"),
            AppConfig(),
            gateway,
        )


@pytest.mark.asyncio
async def test_multi_leg_requires_confirmation_for_real_orders() -> None:
    from trading_platform_poc.binance_cli import _multi_leg

    args = _multi_leg_args(
        "BTCUSDT:buy:limit:0.020:1000.00",
        "ETHUSDT:buy:limit:0.020:1000.00",
    )
    args.confirm = None
    session = RecordingSession(_multi_leg_account())

    with pytest.raises(ValueError, match="--confirm TESTNET"):
        await _multi_leg(args, AppConfig(), _multi_leg_gateway(session))

    assert session.submitted == []


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("BTCUSDT:buy:limit:0.020", "needs a price"),
        ("BTCUSDT:sideways:limit:0.020:1000", "unknown side"),
        ("BTCUSDT:buy:iceberg:0.020:1000", "unknown type"),
        ("BTCUSDT:buy:limit:zero:1000", "non-numeric"),
        ("BTCUSDT:buy:limit:-1:1000", "positive quantity"),
        ("BTCUSDT:buy:limit:0.020:-5", "positive price"),
        ("BTCUSDT:buy:limit", "SYMBOL:SIDE:TYPE:QUANTITY"),
    ],
)
def test_multi_leg_rejects_a_malformed_leg(spec, message) -> None:
    from trading_platform_poc.binance_cli import _parse_leg

    with pytest.raises(ValueError, match=message):
        _parse_leg(spec)
