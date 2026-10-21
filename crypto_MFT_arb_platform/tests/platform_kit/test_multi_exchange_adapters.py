import asyncio
import json
from decimal import Decimal
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import pytest

from trading_platform_poc.deribit_test import (
    DeribitCredentials,
    DeribitTestAdapter,
    DeribitTestConfig,
    sign_deribit_client_auth,
    sign_deribit_http_request,
)
from trading_platform_poc.config import (
    DeribitTestExecutionConfig,
    OKXDemoExecutionConfig,
)
from trading_platform_poc.exchange_adapter import (
    AmbiguousOrderError,
    ExchangeAdapter,
    ExchangeAdapterError,
    ExchangeAPIError,
    ExchangeEndpointError,
    ExchangeInvalidResponseError,
    InstrumentRules,
    QuantityUnit,
    local_open_client_ids,
)
from trading_platform_poc.models import (
    OrderIntent,
    OrderState,
    OrderType,
    Side,
)
from trading_platform_poc.okx_demo import (
    OKXCredentials,
    OKXDemoAdapter,
    OKXDemoConfig,
    sign_okx_request,
)
from trading_platform_poc.oms import OrderManagementSystem


class FakeResponse:
    def __init__(self, payload=None, *, status=200, headers=None, text=None):
        self.payload = payload
        self.status = status
        self.headers = headers or {}
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def text(self):
        if self._text is not None:
            return self._text
        return json.dumps(self.payload)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, headers=None, **kwargs):
        self.requests.append((method, url, headers or {}, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def make_order(
    *,
    symbol="BTC-USDT",
    side=Side.BUY,
    order_type=OrderType.LIMIT,
    quantity="0.123456",
    price="100.127",
):
    oms = OrderManagementSystem()
    intent = OrderIntent(
        UUID("11111111-1111-1111-1111-111111111111"),
        "test",
        symbol,
        side,
        order_type,
        Decimal(quantity),
        Decimal(price) if price is not None else None,
        1,
    )
    order = oms.create(intent)
    order = oms.transition(order.order_id, OrderState.RISK_APPROVED, 2)
    return oms, order


def add_order(
    oms,
    *,
    intent_id,
    symbol,
    state,
    venue,
):
    order = oms.create(
        OrderIntent(
            intent_id,
            "test",
            symbol,
            Side.BUY,
            OrderType.LIMIT,
            Decimal("1"),
            Decimal("100"),
            1,
        )
    )
    if venue is not None:
        order = oms.assign_venue(order.order_id, venue)
    if state is OrderState.CREATED:
        return order
    if state is OrderState.RISK_REJECTED:
        return oms.transition(order.order_id, state, 2)
    order = oms.transition(order.order_id, OrderState.RISK_APPROVED, 2)
    if state is OrderState.RISK_APPROVED:
        return order
    order = oms.transition(order.order_id, OrderState.SUBMITTED, 3)
    if state is OrderState.SUBMITTED:
        return order
    if state in {
        OrderState.RECONCILIATION_REQUIRED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
    }:
        return oms.transition(order.order_id, state, 4)
    order = oms.transition(order.order_id, OrderState.ACKNOWLEDGED, 4)
    if state is OrderState.ACKNOWLEDGED:
        return order
    return oms.transition(order.order_id, state, 5)


def okx_rules():
    return InstrumentRules(
        venue="okx",
        symbol="BTC-USDT",
        tick_size=Decimal("0.01"),
        quantity_step=Decimal("0.001"),
        minimum_quantity=Decimal("0.001"),
        maximum_limit_quantity=Decimal("100"),
        maximum_market_quantity=Decimal("50"),
        base_currency="BTC",
        quote_currency="USDT",
    )


def deribit_rules():
    return InstrumentRules(
        venue="deribit",
        symbol="BTC-PERPETUAL",
        tick_size=Decimal("0.5"),
        quantity_step=Decimal("10"),
        minimum_quantity=Decimal("10"),
        base_currency="BTC",
        quote_currency="USD",
        settlement_currency="BTC",
        quantity_unit=QuantityUnit.QUOTE,
        instrument_type="reversed",
        contract_size=Decimal("10"),
        raw={
            "tick_size_steps": [
                {"above_price": 100000, "tick_size": 1},
            ]
        },
    )


def okx_order_row(
    *,
    client_id="okx-client",
    state="live",
    order_id="123",
):
    return {
        "instId": "BTC-USDT",
        "ordId": order_id,
        "clOrdId": client_id,
        "state": state,
        "side": "buy",
        "ordType": "limit",
        "sz": "0.123",
        "accFillSz": "0",
        "px": "100.12",
        "avgPx": "",
        "uTime": "1700000000000",
    }


def deribit_order_row(
    *,
    label="deribit-client",
    state="open",
    order_id="BTC-123",
    instrument_name="BTC-PERPETUAL",
    amount=20,
    filled_amount=0,
    direction="buy",
):
    return {
        "instrument_name": instrument_name,
        "order_id": order_id,
        "label": label,
        "order_state": state,
        "direction": direction,
        "order_type": "limit",
        "amount": amount,
        "filled_amount": filled_amount,
        "price": 100000,
        "average_price": 0,
        "creation_timestamp": 1700000000000,
        "last_update_timestamp": 1700000000001,
    }


def okx_payload(data, **extra):
    return {"code": "0", "msg": "", "data": data, **extra}


def deribit_payload(result, **extra):
    return {"jsonrpc": "2.0", "id": 1, "result": result, **extra}


def test_adapters_satisfy_shared_runtime_contract():
    okx = OKXDemoAdapter(credentials=OKXCredentials("key", "secret", "pass"))
    deribit = DeribitTestAdapter(
        credentials=DeribitCredentials("id", "secret")
    )

    assert isinstance(okx, ExchangeAdapter)
    assert isinstance(deribit, ExchangeAdapter)


@pytest.mark.parametrize(
    ("adapter", "venue", "symbol", "other_venue"),
    [
        (
            OKXDemoAdapter(
                credentials=OKXCredentials("key", "secret", "pass")
            ),
            "okx",
            "BTC-USDT",
            "deribit",
        ),
        (
            DeribitTestAdapter(
                credentials=DeribitCredentials("id", "secret")
            ),
            "deribit",
            "BTC-PERPETUAL",
            "okx",
        ),
    ],
    ids=["okx", "deribit"],
)
def test_local_open_client_ids_filters_mixed_oms_by_ownership_and_state(
    adapter,
    venue,
    symbol,
    other_venue,
):
    oms = OrderManagementSystem()
    exchange_owned_states = {
        OrderState.SUBMITTED,
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIALLY_FILLED,
        OrderState.RECONCILIATION_REQUIRED,
    }
    expected = set()

    for index, state in enumerate(OrderState, start=1):
        order = add_order(
            oms,
            intent_id=UUID(int=index),
            symbol=symbol,
            state=state,
            venue=venue,
        )
        if state in exchange_owned_states:
            expected.add(adapter.client_order_id(order))

    add_order(
        oms,
        intent_id=UUID(int=100),
        symbol=symbol,
        state=OrderState.SUBMITTED,
        venue=None,
    )
    add_order(
        oms,
        intent_id=UUID(int=101),
        symbol=symbol,
        state=OrderState.SUBMITTED,
        venue=other_venue,
    )
    add_order(
        oms,
        intent_id=UUID(int=102),
        symbol="ETH-USDT",
        state=OrderState.SUBMITTED,
        venue=venue,
    )

    assert local_open_client_ids(adapter, oms, symbol) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://www.okx.com",
        "https://api.okx.com",
        "https://www.okx.com.attacker.example",
        "https://www.okx.com:443",
        "https://www.okx.com/api/v5",
        "http://www.okx.com",
    ],
)
def test_okx_rejects_noncanonical_or_live_urls(url):
    with pytest.raises(ValueError, match="US-targeted"):
        OKXDemoConfig(rest_base_url=url)


def test_okx_us_origin_is_the_only_adapter_and_app_config_default():
    assert OKXDemoConfig().rest_base_url == "https://us.okx.com"
    assert (
        OKXDemoExecutionConfig().rest_base_url
        == "https://us.okx.com"
    )
    with pytest.raises(ValueError):
        OKXDemoExecutionConfig(rest_base_url="https://www.okx.com")


@pytest.mark.parametrize(
    "url",
    [
        "https://www.deribit.com/api/v2",
        "https://test.deribit.com",
        "https://test.deribit.com/api/v2/",
        "https://test.deribit.com:443/api/v2",
        "https://test.deribit.com.attacker.example/api/v2",
    ],
)
def test_deribit_rejects_live_or_noncanonical_urls(url):
    with pytest.raises(ValueError, match="exact official test endpoint"):
        DeribitTestConfig(rest_base_url=url)


@pytest.mark.parametrize("kind", ["future_combo", "option_combo"])
def test_deribit_rejects_combo_instrument_config(kind):
    with pytest.raises(ValueError, match="does not track leg-level exposure"):
        DeribitTestConfig(instrument_kind=kind)
    with pytest.raises(ValueError):
        DeribitTestExecutionConfig(instrument_kind=kind)


def test_credentials_load_from_demo_and_test_environment(monkeypatch):
    monkeypatch.setenv("OKX_DEMO_API_KEY", "key")
    monkeypatch.setenv("OKX_DEMO_API_SECRET", "secret")
    monkeypatch.setenv("OKX_DEMO_PASSPHRASE", "pass")
    monkeypatch.setenv("DERIBIT_TEST_CLIENT_ID", "id")
    monkeypatch.setenv("DERIBIT_TEST_CLIENT_SECRET", "dsecret")

    assert OKXCredentials.from_environment(OKXDemoConfig()) == OKXCredentials(
        "key", "secret", "pass"
    )
    assert DeribitCredentials.from_environment(
        DeribitTestConfig()
    ) == DeribitCredentials("id", "dsecret")


def test_okx_official_signing_structure_vector():
    signature = sign_okx_request(
        "2020-12-08T09:08:57.715Z",
        "GET",
        "/api/v5/account/balance?ccy=BTC",
        "",
        "22582BD0CFF14C41EDBF1AB98506286D",
    )

    assert signature == "HiZhvSfMtWJA3uUIVXV3a/bSXNPCWvYFXoGCVS8V4zY="


def test_deribit_official_client_signature_example_and_http_structure():
    assert sign_deribit_client_auth(
        1576074319000,
        "1iqt2wls",
        "",
        "AMANDASECRECT",
    ) == "56590594f97921b09b18f166befe0d1319b198bbcdad7ca73382de2f88fe9aa1"
    assert sign_deribit_http_request(
        "GET",
        "/api/v2/private/get_account_summary?currency=BTC&extended=true",
        "",
        1576074319000,
        "1iqt2wls",
        "AMANDASECRECT",
    ) == "91e6193100e8cbf118d55d485e822fc5f2c594b192e97309aa882b21bd65378a"


def test_client_labels_are_deterministic_and_venue_safe():
    _, order = make_order()
    okx = OKXDemoAdapter(credentials=OKXCredentials("key", "secret", "pass"))
    deribit = DeribitTestAdapter(
        credentials=DeribitCredentials("id", "secret")
    )

    assert okx.client_order_id(order) == okx.client_order_id(order)
    assert okx.client_order_id(order).isalnum()
    assert len(okx.client_order_id(order)) <= 32
    assert deribit.client_order_id(order) == deribit.client_order_id(order)
    assert len(deribit.client_order_id(order)) <= 64


@pytest.mark.asyncio
async def test_okx_normalizes_quantity_and_side_aware_price():
    adapter = OKXDemoAdapter(
        credentials=OKXCredentials("key", "secret", "pass")
    )
    adapter._rules["BTC-USDT"] = okx_rules()
    _, buy = make_order(side=Side.BUY)
    _, sell = make_order(side=Side.SELL)

    normalized_buy = await adapter.normalize_order(buy)
    normalized_sell = await adapter.normalize_order(sell)

    assert normalized_buy.quantity == Decimal("0.123")
    assert normalized_buy.price == Decimal("100.12")
    assert normalized_sell.price == Decimal("100.13")


@pytest.mark.asyncio
async def test_deribit_normalizes_dynamic_tick_and_quantity():
    adapter = DeribitTestAdapter(
        credentials=DeribitCredentials("id", "secret")
    )
    adapter._rules["BTC-PERPETUAL"] = deribit_rules()
    _, buy = make_order(
        symbol="BTC-PERPETUAL",
        quantity="29",
        price="100000.7",
    )
    _, sell = make_order(
        symbol="BTC-PERPETUAL",
        side=Side.SELL,
        quantity="29",
        price="100000.2",
    )

    normalized_buy = await adapter.normalize_order(buy)
    normalized_sell = await adapter.normalize_order(sell)

    assert normalized_buy.quantity == Decimal("20")
    assert normalized_buy.price == Decimal("100000")
    assert normalized_sell.price == Decimal("100001")


@pytest.mark.asyncio
async def test_deribit_usd_amount_minimum_and_step_are_normalized():
    adapter = DeribitTestAdapter(
        credentials=DeribitCredentials("id", "secret")
    )
    adapter._rules["BTC-PERPETUAL"] = deribit_rules()
    _, stepped = make_order(
        symbol="BTC-PERPETUAL",
        quantity="109",
        price="100000",
    )
    _, below_minimum = make_order(
        symbol="BTC-PERPETUAL",
        quantity="9",
        price="100000",
    )

    assert (await adapter.normalize_order(stepped)).quantity == Decimal("100")
    with pytest.raises(ExchangeAdapterError, match="below minimum"):
        await adapter.normalize_order(below_minimum)


@pytest.mark.asyncio
async def test_okx_instrument_rules_reject_malformed_payload():
    session = FakeSession(
        [FakeResponse(okx_payload([{"instId": "BTC-USDT", "tickSz": "0"}]))]
    )
    adapter = OKXDemoAdapter(
        credentials=OKXCredentials("key", "secret", "pass"),
        session=session,
    )

    with pytest.raises(ExchangeInvalidResponseError):
        await adapter.instrument_rules("BTC-USDT")


@pytest.mark.asyncio
async def test_deribit_instrument_rules_parse_official_filter_fields():
    session = FakeSession(
        [
            FakeResponse(
                deribit_payload(
                    [
                        {
                            "instrument_id": 124972,
                            "instrument_name": "BTC-PERPETUAL",
                            "kind": "future",
                            "settlement_period": "perpetual",
                            "instrument_type": "reversed",
                            "future_type": "reversed",
                            "is_active": True,
                            "tick_size": 0.5,
                            "tick_size_steps": [],
                            "min_trade_amount": 10,
                            "contract_size": 10,
                            "lot_size": 10,
                            "base_currency": "BTC",
                            "quote_currency": "USD",
                            "counter_currency": "USD",
                            "settlement_currency": "BTC",
                            "price_index": "btc_usd",
                            "state": "open",
                        }
                    ]
                )
            )
        ]
    )
    adapter = DeribitTestAdapter(
        credentials=DeribitCredentials("id", "secret"),
        session=session,
    )

    rules = await adapter.instrument_rules("BTC-PERPETUAL")

    assert rules.tick_size == Decimal("0.5")
    assert rules.quantity_step == Decimal("10")
    assert rules.minimum_quantity == Decimal("10")
    assert rules.quantity_unit is QuantityUnit.QUOTE
    assert rules.quote_currency == "USD"
    assert rules.settlement_currency == "BTC"
    method, url, headers, kwargs = session.requests[0]
    assert method == "GET"
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "test.deribit.com"
    assert parsed.path == "/api/v2/public/get_instruments"
    assert parse_qs(parsed.query) == {
        "currency": ["any"],
        "kind": ["future"],
        "expired": ["false"],
    }
    assert headers == {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    assert kwargs == {}


@pytest.mark.asyncio
async def test_deribit_linear_perpetual_uses_base_amount_unit():
    """Linear perpetuals are base-denominated and settled in USDC.

    Metadata shape captured from the live Deribit test API. Classifying this
    instrument as QUOTE would multiply the order amount by price.
    """
    session = FakeSession(
        [
            FakeResponse(
                deribit_payload(
                    [
                        {
                            "instrument_name": "ETH_USDC-PERPETUAL",
                            "kind": "future",
                            "settlement_period": "perpetual",
                            "instrument_type": "linear",
                            "is_active": True,
                            "tick_size": 0.05,
                            "tick_size_steps": [],
                            "min_trade_amount": 0.001,
                            "contract_size": 0.001,
                            "base_currency": "ETH",
                            "quote_currency": "USDC",
                            "counter_currency": "USDC",
                            "settlement_currency": "USDC",
                        }
                    ]
                )
            )
        ]
    )
    adapter = DeribitTestAdapter(
        credentials=DeribitCredentials("id", "secret"),
        session=session,
    )

    rules = await adapter.instrument_rules("ETH_USDC-PERPETUAL")

    assert rules.quantity_unit is QuantityUnit.BASE
    assert rules.minimum_quantity == Decimal("0.001")
    assert rules.settlement_currency == "USDC"
    parsed = urlparse(session.requests[0][1])
    assert parse_qs(parsed.query)["currency"] == ["any"]


@pytest.mark.asyncio
async def test_deribit_future_without_instrument_type_is_rejected():
    """An unknown denomination must fail closed, never be guessed."""
    session = FakeSession(
        [
            FakeResponse(
                deribit_payload(
                    [
                        {
                            "instrument_name": "BTC-PERPETUAL",
                            "kind": "future",
                            "settlement_period": "perpetual",
                            "is_active": True,
                            "tick_size": 0.5,
                            "tick_size_steps": [],
                            "min_trade_amount": 10,
                            "contract_size": 10,
                            "base_currency": "BTC",
                            "quote_currency": "USD",
                            "counter_currency": "USD",
                            "settlement_currency": "BTC",
                        }
                    ]
                )
            )
        ]
    )
    adapter = DeribitTestAdapter(
        credentials=DeribitCredentials("id", "secret"),
        session=session,
    )

    with pytest.raises(ExchangeInvalidResponseError):
        await adapter.instrument_rules("BTC-PERPETUAL")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("symbol", "metadata"),
    [
        (
            "BTC-FS-1AUG26_PERP",
            {
                "instrument_name": "BTC-FS-1AUG26_PERP",
                "kind": "future_combo",
                "settlement_period": "day",
                "instrument_type": "reversed",
                "is_active": True,
                "tick_size": 0.5,
                "tick_size_steps": [],
                "min_trade_amount": 10,
                "contract_size": 10,
                "base_currency": "BTC",
                "quote_currency": "USD",
                "counter_currency": "USD",
                "settlement_currency": "BTC",
            },
        ),
        (
            "BTC-PS-1AUG26-63500_63000",
            {
                "instrument_name": "BTC-PS-1AUG26-63500_63000",
                "kind": "option_combo",
                "settlement_period": "day",
                "instrument_type": "reversed",
                "is_active": True,
                "tick_size": 0.0001,
                "tick_size_steps": [],
                "min_trade_amount": 0.1,
                "contract_size": 1,
                "base_currency": "BTC",
                "quote_currency": "BTC",
                "counter_currency": "USD",
                "settlement_currency": "BTC",
            },
        ),
    ],
)
async def test_deribit_rejects_real_combo_metadata_shape(symbol, metadata):
    adapter = DeribitTestAdapter(
        credentials=DeribitCredentials("id", "secret"),
        session=FakeSession([FakeResponse(deribit_payload([metadata]))]),
    )

    with pytest.raises(ExchangeInvalidResponseError):
        await adapter.instrument_rules(symbol)


@pytest.mark.asyncio
async def test_okx_submits_signed_post_only_with_demo_and_expiry_headers():
    session = FakeSession(
        [
            FakeResponse(
                okx_payload(
                    [
                        {
                            "ordId": "123",
                            "clOrdId": "",
                            "sCode": "0",
                            "sMsg": "",
                            "ts": "1700000000001",
                        }
                    ],
                    inTime="1700000000001000",
                    outTime="1700000000002000",
                ),
                headers={"x-ratelimit-remaining": "59"},
            )
        ]
    )
    adapter = OKXDemoAdapter(
        credentials=OKXCredentials("key", "secret", "pass"),
        session=session,
        clock_ms=lambda: 1700000000000,
    )
    adapter._rules["BTC-USDT"] = okx_rules()
    _, order = make_order(order_type=OrderType.POST_ONLY)

    result = await adapter.submit_order(order)

    method, url, headers, kwargs = session.requests[0]
    body = json.loads(kwargs["data"])
    assert method == "POST"
    assert url == "https://us.okx.com/api/v5/trade/order"
    assert headers["x-simulated-trading"] == "1"
    assert headers["OK-ACCESS-KEY"] == "key"
    assert headers["expTime"] == "1700000005000"
    assert body["ordType"] == "post_only"
    assert body["px"] == "100.12"
    assert body["sz"] == "0.123"
    assert body["clOrdId"] == adapter.client_order_id(order)
    assert result.status == "accepted"
    assert adapter.last_request.rate_limit_headers == {
        "x-ratelimit-remaining": "59"
    }


@pytest.mark.asyncio
async def test_okx_market_order_uses_base_quantity_and_disables_auto_amend():
    session = FakeSession(
        [
            FakeResponse(
                okx_payload(
                    [{"ordId": "1", "clOrdId": "x", "sCode": "0"}]
                )
            )
        ]
    )
    adapter = OKXDemoAdapter(
        credentials=OKXCredentials("key", "secret", "pass"),
        session=session,
    )
    adapter._rules["BTC-USDT"] = okx_rules()
    _, order = make_order(order_type=OrderType.MARKET, price=None)

    await adapter.submit_order(order)

    body = json.loads(session.requests[0][3]["data"])
    assert body["ordType"] == "market"
    assert body["tgtCcy"] == "base_ccy"
    assert body["banAmend"] is True
    assert "px" not in body


@pytest.mark.asyncio
async def test_deribit_submits_http_hmac_post_only_with_reject_flag():
    session = FakeSession(
        [
            FakeResponse(
                deribit_payload(
                    {
                        "order": deribit_order_row(
                            label="ignored",
                            amount=100,
                        )
                    }
                ),
                headers={"x-rate-limit-credits": "49900"},
            )
        ]
    )
    adapter = DeribitTestAdapter(
        credentials=DeribitCredentials("AMANDA", "AMANDASECRECT"),
        session=session,
        clock_ms=lambda: 1576074319000,
        nonce_factory=lambda: "1iqt2wls",
    )
    adapter._rules["BTC-PERPETUAL"] = deribit_rules()
    _, order = make_order(
        symbol="BTC-PERPETUAL",
        order_type=OrderType.POST_ONLY,
        quantity="100",
        price="100000",
    )
    session.responses[0].payload["result"]["order"]["label"] = (
        adapter.client_order_id(order)
    )

    await adapter.submit_order(order)

    method, url, headers, kwargs = session.requests[0]
    query = parse_qs(urlparse(url).query)
    # https://docs.deribit.com/api-reference/trading/private-buy
    # defines inverse perpetual `amount` in USD, separately from `contracts`.
    assert query["amount"] == ["100"]
    assert method == "GET"
    assert kwargs == {}
    assert query["type"] == ["limit"]
    assert query["post_only"] == ["true"]
    assert query["reject_post_only"] == ["true"]
    assert query["valid_until"] == ["1576074324000"]
    assert query["label"] == [adapter.client_order_id(order)]
    assert headers["Authorization"].startswith(
        "deri-hmac-sha256 id=AMANDA,ts=1576074319000,"
        "nonce=1iqt2wls,sig="
    )
    uri = f"/api/v2/private/buy?{urlparse(url).query}"
    expected = sign_deribit_http_request(
        "GET",
        uri,
        "",
        1576074319000,
        "1iqt2wls",
        "AMANDASECRECT",
    )
    assert headers["Authorization"].endswith(f"sig={expected}")
    assert adapter.last_request.rate_limit_headers == {
        "x-rate-limit-credits": "49900"
    }


@pytest.mark.asyncio
async def test_deribit_market_order_omits_price_and_post_only():
    session = FakeSession(
        [
            FakeResponse(
                deribit_payload({"order": deribit_order_row()})
            )
        ]
    )
    adapter = DeribitTestAdapter(
        credentials=DeribitCredentials("id", "secret"),
        session=session,
    )
    adapter._rules["BTC-PERPETUAL"] = deribit_rules()
    _, order = make_order(
        symbol="BTC-PERPETUAL",
        order_type=OrderType.MARKET,
        quantity="20",
        price=None,
    )
    session.responses[0].payload["result"]["order"]["label"] = (
        adapter.client_order_id(order)
    )

    await adapter.submit_order(order)

    query = parse_qs(urlparse(session.requests[0][1]).query)
    assert query["type"] == ["market"]
    assert query["post_only"] == ["false"]
    assert "price" not in query


@pytest.mark.asyncio
async def test_deribit_sell_submits_and_parses_usd_amounts():
    session = FakeSession(
        [
            FakeResponse(
                deribit_payload(
                    {
                        "order": deribit_order_row(
                            amount=100,
                            filled_amount=50,
                            direction="sell",
                        )
                    }
                )
            )
        ]
    )
    adapter = DeribitTestAdapter(
        credentials=DeribitCredentials("id", "secret"),
        session=session,
    )
    adapter._rules["BTC-PERPETUAL"] = deribit_rules()
    _, order = make_order(
        symbol="BTC-PERPETUAL",
        side=Side.SELL,
        order_type=OrderType.MARKET,
        quantity="100",
        price=None,
    )
    session.responses[0].payload["result"]["order"]["label"] = (
        adapter.client_order_id(order)
    )

    exchange_order = await adapter.submit_order(order)

    parsed = urlparse(session.requests[0][1])
    query = parse_qs(parsed.query)
    # https://docs.deribit.com/api-reference/trading/private-sell
    assert parsed.path.endswith("/private/sell")
    assert query["amount"] == ["100"]
    assert exchange_order.quantity == Decimal("100")
    assert exchange_order.filled_quantity == Decimal("50")


@pytest.mark.asyncio
async def test_okx_server_error_reconciles_by_client_id_without_retry():
    _, order = make_order()
    adapter_for_id = OKXDemoAdapter(
        credentials=OKXCredentials("key", "secret", "pass")
    )
    client_id = adapter_for_id.client_order_id(order)
    session = FakeSession(
        [
            FakeResponse(
                {"code": "50000", "msg": "internal", "data": []},
                status=500,
            ),
            FakeResponse(okx_payload([okx_order_row(client_id=client_id)])),
        ]
    )
    adapter = OKXDemoAdapter(
        credentials=OKXCredentials("key", "secret", "pass"),
        session=session,
    )
    adapter._rules["BTC-USDT"] = okx_rules()

    result = await adapter.submit_order(order)

    assert result.client_order_id == client_id
    assert [request[0] for request in session.requests] == ["POST", "GET"]
    assert parse_qs(urlparse(session.requests[1][1]).query)["clOrdId"] == [
        client_id
    ]


@pytest.mark.asyncio
async def test_okx_http_400_50004_reconciles_by_client_id_without_retry():
    _, order = make_order()
    adapter_for_id = OKXDemoAdapter(
        credentials=OKXCredentials("key", "secret", "pass")
    )
    client_id = adapter_for_id.client_order_id(order)
    session = FakeSession(
        [
            FakeResponse(
                {
                    "code": "50004",
                    "msg": "API endpoint request timeout",
                    "data": [],
                },
                status=400,
            ),
            FakeResponse(okx_payload([okx_order_row(client_id=client_id)])),
        ]
    )
    adapter = OKXDemoAdapter(
        credentials=OKXCredentials("key", "secret", "pass"),
        session=session,
    )
    adapter._rules["BTC-USDT"] = okx_rules()

    result = await adapter.submit_order(order)

    assert result.status == "live"
    assert result.client_order_id == client_id
    assert [request[0] for request in session.requests] == ["POST", "GET"]
    assert parse_qs(urlparse(session.requests[1][1]).query)["clOrdId"] == [
        client_id
    ]
    assert sum(request[0] == "POST" for request in session.requests) == 1


@pytest.mark.asyncio
async def test_okx_http_400_50004_failed_query_remains_ambiguous():
    session = FakeSession(
        [
            FakeResponse(
                {
                    "code": "50004",
                    "msg": "API endpoint request timeout",
                    "data": [],
                },
                status=400,
            ),
            FakeResponse(
                {"code": "51603", "msg": "order does not exist", "data": []},
                status=400,
            ),
        ]
    )
    adapter = OKXDemoAdapter(
        credentials=OKXCredentials("key", "secret", "pass"),
        session=session,
    )
    adapter._rules["BTC-USDT"] = okx_rules()
    _, order = make_order()

    with pytest.raises(AmbiguousOrderError):
        await adapter.submit_order(order)

    assert [request[0] for request in session.requests] == ["POST", "GET"]
    assert sum(request[0] == "POST" for request in session.requests) == 1


@pytest.mark.asyncio
async def test_okx_transport_then_failed_reconciliation_is_ambiguous():
    session = FakeSession(
        [
            asyncio.TimeoutError(),
            FakeResponse(
                {"code": "51603", "msg": "order does not exist", "data": []},
                status=400,
            ),
        ]
    )
    adapter = OKXDemoAdapter(
        credentials=OKXCredentials("key", "secret", "pass"),
        session=session,
    )
    adapter._rules["BTC-USDT"] = okx_rules()
    _, order = make_order()

    with pytest.raises(AmbiguousOrderError):
        await adapter.submit_order(order)

    assert len(session.requests) == 2


@pytest.mark.asyncio
async def test_deribit_server_error_reconciles_open_order_by_label():
    _, order = make_order(
        symbol="BTC-PERPETUAL",
        quantity="20",
        price="100000",
    )
    helper = DeribitTestAdapter(
        credentials=DeribitCredentials("id", "secret")
    )
    label = helper.client_order_id(order)
    session = FakeSession(
        [
            FakeResponse(
                {
                    "jsonrpc": "2.0",
                    "error": {"code": 10001, "message": "error"},
                },
                status=500,
            ),
            FakeResponse(
                deribit_payload([deribit_order_row(label=label)])
            ),
        ]
    )
    adapter = DeribitTestAdapter(
        credentials=DeribitCredentials("id", "secret"),
        session=session,
    )
    adapter._rules["BTC-PERPETUAL"] = deribit_rules()

    result = await adapter.submit_order(order)

    assert result.client_order_id == label
    assert len(session.requests) == 2
    assert "/private/get_open_orders_by_label" in session.requests[1][1]


@pytest.mark.asyncio
async def test_deribit_ambiguous_fill_falls_back_to_recent_order_history():
    _, order = make_order(
        symbol="BTC-PERPETUAL",
        quantity="20",
        price="100000",
    )
    helper = DeribitTestAdapter(
        credentials=DeribitCredentials("id", "secret")
    )
    label = helper.client_order_id(order)
    session = FakeSession(
        [
            asyncio.TimeoutError(),
            FakeResponse(deribit_payload([])),
            FakeResponse(
                deribit_payload(
                    [
                        deribit_order_row(
                            label=label,
                            state="filled",
                        )
                    ]
                )
            ),
        ]
    )
    adapter = DeribitTestAdapter(
        credentials=DeribitCredentials("id", "secret"),
        session=session,
    )
    adapter._rules["BTC-PERPETUAL"] = deribit_rules()

    result = await adapter.submit_order(order)

    assert result.status == "filled"
    assert len(session.requests) == 3


@pytest.mark.asyncio
async def test_deribit_label_lookup_selects_exact_instrument_match():
    session = FakeSession(
        [
            FakeResponse(
                deribit_payload(
                    [
                        deribit_order_row(
                            label="shared-label",
                            order_id="BTC-dated",
                            instrument_name="BTC-25DEC26",
                        ),
                        deribit_order_row(
                            label="shared-label",
                            order_id="BTC-perpetual",
                        ),
                    ]
                )
            )
        ]
    )
    adapter = DeribitTestAdapter(
        credentials=DeribitCredentials("id", "secret"),
        session=session,
    )

    adapter._rules["BTC-PERPETUAL"] = deribit_rules()

    result = await adapter.query_order(
        "BTC-PERPETUAL",
        client_order_id="shared-label",
    )

    assert result.exchange_order_id == "BTC-perpetual"
    assert result.symbol == "BTC-PERPETUAL"
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_deribit_query_rejects_mismatched_instrument_response():
    session = FakeSession(
        [
            FakeResponse(
                deribit_payload(
                    deribit_order_row(instrument_name="BTC-25DEC26")
                )
            )
        ]
    )
    adapter = DeribitTestAdapter(
        credentials=DeribitCredentials("id", "secret"),
        session=session,
    )

    with pytest.raises(ExchangeInvalidResponseError):
        await adapter.query_order(
            "BTC-PERPETUAL",
            exchange_order_id="BTC-123",
        )


@pytest.mark.asyncio
async def test_deribit_query_accepts_exact_instrument_response():
    session = FakeSession(
        [FakeResponse(deribit_payload(deribit_order_row()))]
    )
    adapter = DeribitTestAdapter(
        credentials=DeribitCredentials("id", "secret"),
        session=session,
    )

    result = await adapter.query_order(
        "BTC-PERPETUAL",
        exchange_order_id="BTC-123",
    )

    assert result.symbol == "BTC-PERPETUAL"
    assert result.exchange_order_id == "BTC-123"


@pytest.mark.asyncio
async def test_deribit_unresolved_transport_is_ambiguous_not_retried():
    session = FakeSession(
        [
            asyncio.TimeoutError(),
            FakeResponse(deribit_payload([])),
            FakeResponse(deribit_payload([])),
            FakeResponse(deribit_payload([])),
        ]
    )
    adapter = DeribitTestAdapter(
        credentials=DeribitCredentials("id", "secret"),
        session=session,
    )
    adapter._rules["BTC-PERPETUAL"] = deribit_rules()
    _, order = make_order(
        symbol="BTC-PERPETUAL",
        quantity="20",
        price="100000",
    )

    with pytest.raises(AmbiguousOrderError):
        await adapter.submit_order(order)

    assert len(session.requests) == 4


@pytest.mark.asyncio
async def test_okx_ambiguous_cancel_polls_until_documented_terminal_state():
    session = FakeSession(
        [
            asyncio.TimeoutError(),
            FakeResponse(okx_payload([okx_order_row(state="live")])),
            FakeResponse(okx_payload([okx_order_row(state="canceled")])),
        ]
    )
    adapter = OKXDemoAdapter(
        config=OKXDemoConfig(
            cancel_reconciliation_attempts=2,
            cancel_reconciliation_interval_seconds=0,
        ),
        credentials=OKXCredentials("key", "secret", "pass"),
        session=session,
    )

    result = await adapter.cancel_order(
        "BTC-USDT",
        exchange_order_id="123",
    )

    assert result.status == "canceled"
    assert len(session.requests) == 3


@pytest.mark.asyncio
async def test_okx_ambiguous_cancel_does_not_treat_live_as_resolved():
    session = FakeSession(
        [
            asyncio.TimeoutError(),
            FakeResponse(okx_payload([okx_order_row(state="live")])),
            FakeResponse(
                okx_payload([okx_order_row(state="partially_filled")])
            ),
        ]
    )
    adapter = OKXDemoAdapter(
        config=OKXDemoConfig(
            cancel_reconciliation_attempts=2,
            cancel_reconciliation_interval_seconds=0,
        ),
        credentials=OKXCredentials("key", "secret", "pass"),
        session=session,
    )

    with pytest.raises(AmbiguousOrderError):
        await adapter.cancel_order(
            "BTC-USDT",
            exchange_order_id="123",
        )

    assert len(session.requests) == 3


@pytest.mark.asyncio
async def test_okx_http_400_50004_cancel_polls_to_terminal_without_retry():
    session = FakeSession(
        [
            FakeResponse(
                {
                    "code": "50004",
                    "msg": "API endpoint request timeout",
                    "data": [],
                },
                status=400,
            ),
            FakeResponse(okx_payload([okx_order_row(state="live")])),
            FakeResponse(okx_payload([okx_order_row(state="canceled")])),
        ]
    )
    adapter = OKXDemoAdapter(
        config=OKXDemoConfig(
            cancel_reconciliation_attempts=2,
            cancel_reconciliation_interval_seconds=0,
        ),
        credentials=OKXCredentials("key", "secret", "pass"),
        session=session,
    )

    result = await adapter.cancel_order(
        "BTC-USDT",
        client_order_id="okx-client",
    )

    assert result.status == "canceled"
    assert [request[0] for request in session.requests] == [
        "POST",
        "GET",
        "GET",
    ]
    assert sum(request[0] == "POST" for request in session.requests) == 1
    for request in session.requests[1:]:
        assert parse_qs(urlparse(request[1]).query)["clOrdId"] == [
            "okx-client"
        ]


@pytest.mark.asyncio
async def test_okx_http_400_50004_cancel_still_live_remains_ambiguous():
    session = FakeSession(
        [
            FakeResponse(
                {
                    "code": "50004",
                    "msg": "API endpoint request timeout",
                    "data": [],
                },
                status=400,
            ),
            FakeResponse(okx_payload([okx_order_row(state="live")])),
            FakeResponse(okx_payload([okx_order_row(state="live")])),
        ]
    )
    adapter = OKXDemoAdapter(
        config=OKXDemoConfig(
            cancel_reconciliation_attempts=2,
            cancel_reconciliation_interval_seconds=0,
        ),
        credentials=OKXCredentials("key", "secret", "pass"),
        session=session,
    )

    with pytest.raises(AmbiguousOrderError):
        await adapter.cancel_order(
            "BTC-USDT",
            client_order_id="okx-client",
        )

    assert [request[0] for request in session.requests] == [
        "POST",
        "GET",
        "GET",
    ]
    assert sum(request[0] == "POST" for request in session.requests) == 1


@pytest.mark.asyncio
async def test_deribit_ambiguous_cancel_polls_until_cancelled():
    session = FakeSession(
        [
            asyncio.TimeoutError(),
            FakeResponse(
                deribit_payload(deribit_order_row(state="open"))
            ),
            FakeResponse(
                deribit_payload(deribit_order_row(state="cancelled"))
            ),
        ]
    )
    adapter = DeribitTestAdapter(
        config=DeribitTestConfig(
            cancel_reconciliation_attempts=2,
            cancel_reconciliation_interval_seconds=0,
        ),
        credentials=DeribitCredentials("id", "secret"),
        session=session,
    )

    result = await adapter.cancel_order(
        "BTC-PERPETUAL",
        exchange_order_id="BTC-123",
    )

    assert result.status == "cancelled"
    assert len(session.requests) == 3


@pytest.mark.asyncio
async def test_deribit_ambiguous_cancel_does_not_treat_open_as_resolved():
    session = FakeSession(
        [
            asyncio.TimeoutError(),
            FakeResponse(
                deribit_payload(deribit_order_row(state="open"))
            ),
            FakeResponse(
                deribit_payload(deribit_order_row(state="open"))
            ),
        ]
    )
    adapter = DeribitTestAdapter(
        config=DeribitTestConfig(
            cancel_reconciliation_attempts=2,
            cancel_reconciliation_interval_seconds=0,
        ),
        credentials=DeribitCredentials("id", "secret"),
        session=session,
    )

    with pytest.raises(AmbiguousOrderError):
        await adapter.cancel_order(
            "BTC-PERPETUAL",
            exchange_order_id="BTC-123",
        )

    assert len(session.requests) == 3


@pytest.mark.asyncio
async def test_deribit_cancel_mismatched_instrument_remains_ambiguous():
    mismatched = deribit_order_row(
        state="cancelled",
        instrument_name="BTC-25DEC26",
    )
    session = FakeSession(
        [
            FakeResponse(deribit_payload(mismatched)),
            FakeResponse(deribit_payload(mismatched)),
        ]
    )
    adapter = DeribitTestAdapter(
        config=DeribitTestConfig(
            cancel_reconciliation_attempts=1,
            cancel_reconciliation_interval_seconds=0,
        ),
        credentials=DeribitCredentials("id", "secret"),
        session=session,
    )

    with pytest.raises(AmbiguousOrderError):
        await adapter.cancel_order(
            "BTC-PERPETUAL",
            exchange_order_id="BTC-123",
        )

    assert "/private/cancel" in session.requests[0][1]
    assert "/private/get_order_state" in session.requests[1][1]


@pytest.mark.asyncio
async def test_deribit_cancel_by_label_never_cancels_other_instrument():
    session = FakeSession(
        [
            FakeResponse(
                deribit_payload(
                    [
                        deribit_order_row(
                            label="shared-label",
                            instrument_name="BTC-25DEC26",
                        )
                    ]
                )
            ),
            FakeResponse(deribit_payload([])),
            FakeResponse(deribit_payload([])),
        ]
    )
    adapter = DeribitTestAdapter(
        credentials=DeribitCredentials("id", "secret"),
        session=session,
    )

    adapter._rules["BTC-PERPETUAL"] = deribit_rules()

    with pytest.raises(ExchangeAPIError) as error:
        await adapter.cancel_order(
            "BTC-PERPETUAL",
            client_order_id="shared-label",
        )

    assert error.value.code == 10004
    assert all(
        "/private/cancel" not in request[1] for request in session.requests
    )


@pytest.mark.asyncio
async def test_deribit_label_lookup_exhausts_recent_history_pages():
    target = deribit_order_row(
        label="target-label",
        state="filled",
        order_id="BTC-target",
    )
    first_page = [
        deribit_order_row(
            label="target-label",
            order_id="BTC-dated",
            instrument_name="BTC-25DEC26",
        ),
        deribit_order_row(label="other-2", order_id="BTC-2"),
    ]
    session = FakeSession(
        [
            FakeResponse(deribit_payload([])),
            FakeResponse(deribit_payload(first_page)),
            FakeResponse(deribit_payload([target])),
        ]
    )
    adapter = DeribitTestAdapter(
        config=DeribitTestConfig(history_page_size=2),
        credentials=DeribitCredentials("id", "secret"),
        session=session,
    )

    adapter._rules["BTC-PERPETUAL"] = deribit_rules()

    result = await adapter.query_order(
        "BTC-PERPETUAL",
        client_order_id="target-label",
    )

    assert result.exchange_order_id == "BTC-target"
    first_query = parse_qs(urlparse(session.requests[1][1]).query)
    second_query = parse_qs(urlparse(session.requests[2][1]).query)
    assert first_query["offset"] == ["0"]
    assert second_query["offset"] == ["2"]
    assert second_query["include_old"] == ["true"]
    assert second_query["historical"] == ["false"]


@pytest.mark.asyncio
async def test_deribit_label_lookup_falls_back_to_exhaustive_historical_mode():
    target = deribit_order_row(
        label="old-label",
        state="filled",
        order_id="BTC-old",
    )
    session = FakeSession(
        [
            FakeResponse(deribit_payload([])),
            FakeResponse(deribit_payload([])),
            FakeResponse(deribit_payload([target])),
        ]
    )
    adapter = DeribitTestAdapter(
        config=DeribitTestConfig(history_page_size=2),
        credentials=DeribitCredentials("id", "secret"),
        session=session,
    )

    adapter._rules["BTC-PERPETUAL"] = deribit_rules()

    result = await adapter.query_order(
        "BTC-PERPETUAL",
        client_order_id="old-label",
    )

    assert result.exchange_order_id == "BTC-old"
    historical_query = parse_qs(urlparse(session.requests[2][1]).query)
    assert historical_query["include_old"] == ["true"]
    assert historical_query["include_unfilled"] == ["true"]
    assert historical_query["historical"] == ["true"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "adapter,responses,rules,symbol",
    [
        (
            "okx",
            [FakeResponse(text="<html>proxy</html>")],
            okx_rules(),
            "BTC-USDT",
        ),
        (
            "deribit",
            [FakeResponse({"jsonrpc": "2.0", "result": {"unexpected": []}})],
            deribit_rules(),
            "BTC-PERPETUAL",
        ),
    ],
)
async def test_malformed_success_responses_are_visible(
    adapter,
    responses,
    rules,
    symbol,
):
    session = FakeSession(responses)
    if adapter == "okx":
        gateway = OKXDemoAdapter(
            credentials=OKXCredentials("key", "secret", "pass"),
            session=session,
        )
        with pytest.raises(ExchangeInvalidResponseError):
            await gateway.account()
    else:
        gateway = DeribitTestAdapter(
            credentials=DeribitCredentials("id", "secret"),
            session=session,
        )
        with pytest.raises(ExchangeInvalidResponseError):
            await gateway.trades(symbol)


@pytest.mark.asyncio
@pytest.mark.parametrize("venue", ["okx", "deribit"])
async def test_non_json_response_records_telemetry_before_parsing(venue):
    if venue == "okx":
        session = FakeSession(
            [
                FakeResponse(
                    status=502,
                    headers={
                        "x-ratelimit-remaining": "0",
                        "Retry-After": "1",
                    },
                    text="<html>bad gateway</html>",
                )
            ]
        )
        adapter = OKXDemoAdapter(
            credentials=OKXCredentials("key", "secret", "pass"),
            session=session,
        )
        operation = adapter.account()
        expected_path = "/api/v5/account/balance"
    else:
        session = FakeSession(
            [
                FakeResponse(
                    status=502,
                    headers={
                        "x-rate-limit-credits": "0",
                        "Retry-After": "1",
                    },
                    text="<html>bad gateway</html>",
                )
            ]
        )
        adapter = DeribitTestAdapter(
            credentials=DeribitCredentials("id", "secret"),
            session=session,
        )
        operation = adapter.account("BTC")
        expected_path = "/private/get_account_summary"

    with pytest.raises(ExchangeInvalidResponseError):
        await operation

    assert adapter.last_request.status == 502
    assert adapter.last_request.path == expected_path
    assert adapter.last_request.retry_after == "1"
    assert adapter.last_request.rate_limit_headers


@pytest.mark.asyncio
async def test_api_errors_preserve_status_code_payload_and_retry_after():
    okx = OKXDemoAdapter(
        credentials=OKXCredentials("key", "secret", "pass"),
        session=FakeSession(
            [
                FakeResponse(
                    {"code": "50011", "msg": "rate limit", "data": []},
                    status=429,
                    headers={"Retry-After": "2"},
                )
            ]
        ),
    )

    with pytest.raises(ExchangeAPIError) as error:
        await okx.account()

    assert error.value.status == 429
    assert error.value.code == "50011"
    assert error.value.retry_after == "2"


@pytest.mark.asyncio
async def test_endpoint_allowlists_reject_unknown_paths_before_network():
    okx = OKXDemoAdapter(
        credentials=OKXCredentials("key", "secret", "pass"),
        session=FakeSession([]),
    )
    deribit = DeribitTestAdapter(
        credentials=DeribitCredentials("id", "secret"),
        session=FakeSession([]),
    )

    with pytest.raises(ExchangeEndpointError):
        await okx._request("POST", "/api/v5/asset/withdrawal", signed=True)
    with pytest.raises(ExchangeEndpointError):
        await deribit._request("GET", "/private/withdraw", signed=True)


@pytest.mark.asyncio
async def test_okx_query_cancel_open_trades_and_account_contracts():
    session = FakeSession(
        [
            FakeResponse(okx_payload([okx_order_row()])),
            FakeResponse(
                okx_payload(
                    [
                        {
                            "ordId": "123",
                            "clOrdId": "okx-client",
                            "sCode": "0",
                            "sMsg": "",
                            "ts": "1700000000002",
                        }
                    ]
                )
            ),
            FakeResponse(okx_payload([okx_order_row()])),
            FakeResponse(
                okx_payload(
                    [
                        {
                            "instId": "BTC-USDT",
                            "tradeId": "t1",
                            "ordId": "123",
                            "side": "buy",
                            "fillPx": "100.12",
                            "fillSz": "0.1",
                            "fee": "-0.001",
                            "feeCcy": "USDT",
                            "ts": "1700000000003",
                        }
                    ]
                )
            ),
            FakeResponse(
                okx_payload(
                    [{"uTime": "1700000000004", "details": []}]
                )
            ),
        ]
    )
    adapter = OKXDemoAdapter(
        credentials=OKXCredentials("key", "secret", "pass"),
        session=session,
    )

    queried = await adapter.query_order(
        "BTC-USDT",
        client_order_id="okx-client",
    )
    canceled = await adapter.cancel_order(
        "BTC-USDT",
        exchange_order_id="123",
    )
    opened = await adapter.open_orders("BTC-USDT")
    trades = await adapter.trades("BTC-USDT")
    account = await adapter.account("USDT")

    assert queried.exchange_order_id == "123"
    assert canceled.status == "cancel_requested"
    assert opened[0].status == "live"
    assert trades[0].fee == Decimal("-0.001")
    assert account.currency == "USDT"


@pytest.mark.asyncio
async def test_deribit_query_cancel_open_trades_and_account_contracts():
    session = FakeSession(
        [
            FakeResponse(deribit_payload(deribit_order_row())),
            FakeResponse(
                deribit_payload(
                    deribit_order_row(state="cancelled")
                )
            ),
            FakeResponse(deribit_payload([deribit_order_row()])),
            FakeResponse(
                deribit_payload(
                    {
                        "trades": [
                            {
                                "instrument_name": "BTC-PERPETUAL",
                                "trade_id": "t1",
                                "order_id": "BTC-123",
                                "direction": "buy",
                                "price": 100000,
                                "amount": 10,
                                "fee": 0.0001,
                                "fee_currency": "BTC",
                                "timestamp": 1700000000002,
                            }
                        ],
                        "has_more": False,
                    }
                )
            ),
            FakeResponse(
                deribit_payload(
                    {
                        "currency": "BTC",
                        "balance": 1,
                        "limits": {
                            "matching_engine": {
                                "trading": {"total": {"burst": 20}}
                            }
                        },
                    }
                )
            ),
        ]
    )
    adapter = DeribitTestAdapter(
        credentials=DeribitCredentials("id", "secret"),
        session=session,
    )

    queried = await adapter.query_order(
        "BTC-PERPETUAL",
        exchange_order_id="BTC-123",
    )
    canceled = await adapter.cancel_order(
        "BTC-PERPETUAL",
        exchange_order_id="BTC-123",
    )
    opened = await adapter.open_orders("BTC-PERPETUAL")
    trades = await adapter.trades("BTC-PERPETUAL")
    account = await adapter.account("BTC")

    assert queried.exchange_order_id == "BTC-123"
    assert canceled.status == "cancelled"
    assert opened[0].status == "open"
    assert trades[0].fee_currency == "BTC"
    assert account.raw["limits"]["matching_engine"]


@pytest.mark.asyncio
async def test_okx_reconciliation_snapshot_identifies_drift():
    oms, order = make_order()
    order = oms.assign_venue(order.order_id, "okx")
    order = oms.transition(order.order_id, OrderState.SUBMITTED, 3)
    adapter = OKXDemoAdapter(
        credentials=OKXCredentials("key", "secret", "pass"),
        clock_ms=lambda: 1700000000000,
    )
    adapter._rules["BTC-USDT"] = okx_rules()
    local_id = adapter.client_order_id(order)
    other_venue_order = add_order(
        oms,
        intent_id=UUID(int=200),
        symbol="BTC-USDT",
        state=OrderState.SUBMITTED,
        venue="deribit",
    )
    other_venue_id = adapter.client_order_id(other_venue_order)

    async def open_orders(symbol=None):
        return (
            await _okx_parsed_order(other_venue_id),
        )

    async def trades(symbol, limit=100):
        return ()

    async def account(currency=None):
        from trading_platform_poc.exchange_adapter import AccountSnapshot

        return AccountSnapshot("okx", currency, 1, {})

    adapter.open_orders = open_orders
    adapter.trades = trades
    adapter.account = account

    snapshot = await adapter.reconcile(oms, "BTC-USDT")

    assert snapshot.missing_on_exchange == (local_id,)
    assert snapshot.unknown_on_exchange == (other_venue_id,)
    assert snapshot.captured_at_ms == 1700000000000


async def _okx_parsed_order(client_id):
    from trading_platform_poc.exchange_adapter import ExchangeOrder

    return ExchangeOrder(
        venue="okx",
        symbol="BTC-USDT",
        exchange_order_id="1",
        client_order_id=client_id,
        status="live",
        side="buy",
        order_type="limit",
        quantity=Decimal("1"),
        filled_quantity=Decimal("0"),
        price=Decimal("100"),
        average_fill_price=None,
        timestamp_ms=1,
        raw={},
    )


@pytest.mark.asyncio
async def test_deribit_reconciliation_snapshot_identifies_drift():
    oms, order = make_order(
        symbol="BTC-PERPETUAL",
        quantity="20",
        price="100000",
    )
    order = oms.assign_venue(order.order_id, "deribit")
    order = oms.transition(order.order_id, OrderState.SUBMITTED, 3)
    adapter = DeribitTestAdapter(
        credentials=DeribitCredentials("id", "secret"),
        clock_ms=lambda: 1700000000000,
    )
    adapter._rules["BTC-PERPETUAL"] = deribit_rules()
    local_id = adapter.client_order_id(order)
    other_venue_order = add_order(
        oms,
        intent_id=UUID(int=201),
        symbol="BTC-PERPETUAL",
        state=OrderState.SUBMITTED,
        venue="okx",
    )
    other_venue_id = adapter.client_order_id(other_venue_order)

    async def open_orders(symbol=None):
        from trading_platform_poc.exchange_adapter import ExchangeOrder

        return (
            ExchangeOrder(
                "deribit",
                "BTC-PERPETUAL",
                "BTC-2",
                other_venue_id,
                "open",
                "buy",
                "limit",
                Decimal("10"),
                Decimal("0"),
                Decimal("100000"),
                None,
                1,
                {},
            ),
        )

    async def trades(symbol, limit=100):
        return ()

    async def account(currency=None):
        from trading_platform_poc.exchange_adapter import AccountSnapshot

        return AccountSnapshot("deribit", currency, 1, {})

    adapter.open_orders = open_orders
    adapter.trades = trades
    adapter.account = account

    snapshot = await adapter.reconcile(oms, "BTC-PERPETUAL")

    assert snapshot.missing_on_exchange == (local_id,)
    assert snapshot.unknown_on_exchange == (other_venue_id,)
    assert snapshot.account.currency == "BTC"


@pytest.mark.asyncio
async def test_time_sync_uses_official_server_time_shapes():
    okx = OKXDemoAdapter(
        credentials=OKXCredentials("key", "secret", "pass"),
        session=FakeSession(
            [FakeResponse(okx_payload([{"ts": "1700000000100"}]))]
        ),
        clock_ms=lambda: 1700000000000,
    )
    deribit = DeribitTestAdapter(
        credentials=DeribitCredentials("id", "secret"),
        session=FakeSession(
            [FakeResponse(deribit_payload(1700000000200))]
        ),
        clock_ms=lambda: 1700000000000,
    )

    assert await okx.sync_time() == 100
    assert await deribit.sync_time() == 200
