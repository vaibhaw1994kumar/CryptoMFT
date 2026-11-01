from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict, dataclass, is_dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

import msgspec

from trading_platform_poc.binance_execution import (
    BinanceExecutionService,
    BinanceSpotTestnetGateway,
)
from trading_platform_poc.config import AppConfig
from trading_platform_poc.models import (
    OrderIntent,
    OrderState,
    OrderType,
    Side,
)
from trading_platform_poc.oms import OrderManagementSystem
from trading_platform_poc.risk import RiskContext, RiskEngine


def configure_binance_parser(
    subparsers: argparse._SubParsersAction,
) -> None:
    parser = subparsers.add_parser(
        "binance-testnet",
        help="operate the guarded Binance Spot Testnet gateway",
    )
    actions = parser.add_subparsers(
        dest="binance_action",
        required=True,
    )
    actions.add_parser("ping", help="check public Testnet connectivity")
    rules = actions.add_parser(
        "rules",
        help="show Binance symbol filters",
    )
    rules.add_argument("--symbol", default="BTCUSDT")
    ticker = actions.add_parser(
        "ticker",
        help="show the current public Testnet symbol price",
    )
    ticker.add_argument("--symbol", default="BTCUSDT")
    actions.add_parser(
        "account",
        help="show the authenticated Testnet account",
    )
    open_orders = actions.add_parser(
        "open-orders",
        help="list Testnet open orders",
    )
    open_orders.add_argument("--symbol")

    query = actions.add_parser("query", help="query one Testnet order")
    _add_order_lookup_arguments(query)

    cancel = actions.add_parser("cancel", help="cancel one Testnet order")
    _add_order_lookup_arguments(cancel)
    cancel.add_argument(
        "--confirm",
        required=True,
        choices=["TESTNET"],
    )

    submit = actions.add_parser(
        "submit",
        help="risk-check and submit a Testnet order through the OMS",
    )
    submit.add_argument("--symbol", default="BTCUSDT")
    submit.add_argument(
        "--side",
        required=True,
        choices=["buy", "sell"],
    )
    submit.add_argument(
        "--type",
        dest="order_type",
        required=True,
        choices=["market", "limit", "post-only"],
    )
    submit.add_argument("--quantity", type=Decimal, required=True)
    submit.add_argument("--price", type=Decimal)
    submit.add_argument(
        "--daily-net-pnl",
        type=Decimal,
        required=True,
        help="caller-supplied current daily net PnL for risk evaluation",
    )
    submit.add_argument(
        "--test-only",
        action="store_true",
        help="use Binance /api/v3/order/test without creating an order",
    )
    submit.add_argument(
        "--confirm",
        choices=["TESTNET"],
        help="required when creating an actual Testnet order",
    )

    modify = actions.add_parser(
        "modify",
        help="cancel and replace a resting Testnet order atomically",
    )
    modify.add_argument("--symbol", default="BTCUSDT")
    replaced = modify.add_mutually_exclusive_group(required=True)
    replaced.add_argument("--cancel-order-id", type=int)
    replaced.add_argument("--cancel-client-order-id")
    modify.add_argument(
        "--side",
        required=True,
        choices=["buy", "sell"],
    )
    modify.add_argument(
        "--type",
        dest="order_type",
        required=True,
        choices=["market", "limit", "post-only"],
    )
    modify.add_argument("--quantity", type=Decimal, required=True)
    modify.add_argument("--price", type=Decimal)
    modify.add_argument(
        "--daily-net-pnl",
        type=Decimal,
        required=True,
        help="caller-supplied current daily net PnL for risk evaluation",
    )
    modify.add_argument(
        "--confirm",
        required=True,
        choices=["TESTNET"],
    )

    multi_leg = actions.add_parser(
        "multi-leg",
        help="admit and dispatch several Testnet legs concurrently",
    )
    multi_leg.add_argument(
        "--leg",
        action="append",
        required=True,
        metavar="SYMBOL:SIDE:TYPE:QUANTITY[:PRICE]",
        help=(
            "one leg, repeatable; e.g. BTCUSDT:buy:limit:0.001:60000 "
            "or BTCUSDT:sell:market:0.001"
        ),
    )
    multi_leg.add_argument(
        "--daily-net-pnl",
        type=Decimal,
        required=True,
        help="caller-supplied current daily net PnL for risk evaluation",
    )
    multi_leg.add_argument(
        "--test-only",
        action="store_true",
        help="validate every leg without creating Testnet orders",
    )
    multi_leg.add_argument(
        "--confirm",
        choices=["TESTNET"],
        help="required when creating actual Testnet orders",
    )

    reconcile = actions.add_parser(
        "reconcile",
        help="capture account, open-order, and recent-trade state",
    )
    reconcile.add_argument("--symbol", default="BTCUSDT")


async def run_binance_command(
    args: argparse.Namespace,
    config: AppConfig,
) -> None:
    async with BinanceSpotTestnetGateway(
        config.binance_execution
    ) as gateway:
        action = args.binance_action
        if action == "ping":
            await gateway.ping()
            offset = await gateway.sync_time()
            _print({"status": "ok", "time_offset_ms": offset})
        elif action == "rules":
            _print(await gateway.symbol_rules(args.symbol))
        elif action == "ticker":
            _print(
                {
                    "symbol": args.symbol.upper(),
                    "price": await gateway.ticker_price(args.symbol),
                }
            )
        elif action == "account":
            _print(await gateway.account())
        elif action == "open-orders":
            _print(await gateway.open_orders(args.symbol))
        elif action == "query":
            _print(
                await gateway.query_order(
                    args.symbol,
                    order_id=args.order_id,
                    client_order_id=args.client_order_id,
                )
            )
        elif action == "cancel":
            _print(
                await gateway.cancel_order(
                    args.symbol,
                    order_id=args.order_id,
                    client_order_id=args.client_order_id,
                )
            )
        elif action == "submit":
            await _submit(args, config, gateway)
        elif action == "modify":
            await _modify(args, config, gateway)
        elif action == "multi-leg":
            await _multi_leg(args, config, gateway)
        elif action == "reconcile":
            report = await gateway.reconcile(
                OrderManagementSystem(),
                args.symbol,
            )
            _print(report)
        else:
            raise ValueError(f"unsupported Binance action: {action}")


async def _submit(
    args: argparse.Namespace,
    config: AppConfig,
    gateway: BinanceSpotTestnetGateway,
) -> None:
    if not args.test_only and args.confirm != "TESTNET":
        raise ValueError(
            "actual Testnet submission requires --confirm TESTNET"
        )
    order_type = {
        "market": OrderType.MARKET,
        "limit": OrderType.LIMIT,
        "post-only": OrderType.POST_ONLY,
    }[args.order_type]
    if order_type is not OrderType.MARKET and args.price is None:
        raise ValueError("limit and post-only orders require --price")
    reference_price = await gateway.ticker_price(args.symbol)
    rules = await gateway.symbol_rules(args.symbol)
    account = await gateway.account()
    if not account.get("canTrade", False):
        raise ValueError("Binance Testnet account is not allowed to trade")
    free_balances = {
        balance["asset"]: Decimal(balance["free"])
        for balance in account.get("balances", [])
    }
    total_balances = {
        balance["asset"]: Decimal(balance["free"])
        + Decimal(balance["locked"])
        for balance in account.get("balances", [])
    }
    position = total_balances.get(rules.base_asset, Decimal(0))
    exchange_open_orders = await gateway.open_orders()
    open_order_count = len(exchange_open_orders)
    pending_same_side_quantity = Decimal(0)
    for open_order in exchange_open_orders:
        open_order_rules = await gateway.symbol_rules(
            open_order.symbol
        )
        if open_order_rules.base_asset != rules.base_asset:
            continue
        if open_order.side.upper() != args.side.upper():
            continue
        remaining = (
            open_order.original_quantity
            - open_order.executed_quantity
        )
        pending_same_side_quantity += remaining
    effective_price = args.price or reference_price
    if Side(args.side) is Side.BUY:
        required_quote = args.quantity * effective_price
        if (
            free_balances.get(rules.quote_asset, Decimal(0))
            < required_quote
        ):
            raise ValueError(
                f"insufficient {rules.quote_asset} Testnet balance"
            )
    elif (
        free_balances.get(rules.base_asset, Decimal(0))
        < args.quantity
    ):
        raise ValueError(
            f"insufficient {rules.base_asset} Testnet balance"
        )
    intent = OrderIntent(
        intent_id=uuid4(),
        strategy_id="binance_testnet_cli",
        symbol=args.symbol.upper(),
        side=Side(args.side),
        order_type=order_type,
        quantity=args.quantity,
        price=args.price,
        created_timestamp_ns=__import__("time").time_ns(),
    )
    risk = RiskEngine(config.risk)
    decision = risk.evaluate(
        intent,
        RiskContext(
            position=(
                position + pending_same_side_quantity
                if Side(args.side) is Side.BUY
                else position - pending_same_side_quantity
            ),
            open_order_count=open_order_count,
            daily_net_pnl=args.daily_net_pnl,
            feed_stale=False,
            mid_price=reference_price,
        ),
        intent.created_timestamp_ns,
    )
    if not decision.approved:
        raise ValueError(f"risk rejected order: {decision.reason}")
    oms = OrderManagementSystem()
    order = oms.create(intent)
    order = oms.transition(
        order.order_id,
        OrderState.RISK_APPROVED,
        decision.decision_timestamp_ns,
        reason=decision.reason,
    )
    service = BinanceExecutionService(gateway, oms)
    result = await service.submit(
        order.order_id,
        reference_price=reference_price,
        test_only=args.test_only,
    )
    _print(
        {
            "binance": result,
            "oms_order": oms.orders[0],
            "oms_updates": oms.updates,
            "rate_limits": gateway.last_rate_limits,
        }
    )


async def _modify(
    args: argparse.Namespace,
    config: AppConfig,
    gateway: BinanceSpotTestnetGateway,
) -> None:
    """Replace a resting order with a fresh, risk-checked order."""
    order_type = {
        "market": OrderType.MARKET,
        "limit": OrderType.LIMIT,
        "post-only": OrderType.POST_ONLY,
    }[args.order_type]
    if order_type is not OrderType.MARKET and args.price is None:
        raise ValueError("limit and post-only orders require --price")
    existing = await gateway.query_order(
        args.symbol,
        order_id=args.cancel_order_id,
        client_order_id=args.cancel_client_order_id,
    )
    if existing.status not in {"NEW", "PARTIALLY_FILLED"}:
        raise ValueError(
            f"only a resting order can be replaced; it is {existing.status}"
        )
    reference_price = await gateway.ticker_price(args.symbol)
    rules = await gateway.symbol_rules(args.symbol)
    account = await gateway.account()
    if not account.get("canTrade", False):
        raise ValueError("Binance Testnet account is not allowed to trade")
    total_balances = {
        balance["asset"]: Decimal(balance["free"])
        + Decimal(balance["locked"])
        for balance in account.get("balances", [])
    }
    intent = OrderIntent(
        intent_id=uuid4(),
        strategy_id="binance_testnet_cli",
        symbol=args.symbol.upper(),
        side=Side(args.side),
        order_type=order_type,
        quantity=args.quantity,
        price=args.price,
        created_timestamp_ns=time.time_ns(),
    )
    risk = RiskEngine(config.risk)
    decision = risk.evaluate(
        intent,
        RiskContext(
            position=total_balances.get(rules.base_asset, Decimal(0)),
            # The order being replaced is withdrawn by the same call, so
            # it must not be counted against the open-order limit.
            open_order_count=max(
                0,
                len(await gateway.open_orders()) - 1,
            ),
            daily_net_pnl=args.daily_net_pnl,
            feed_stale=False,
            mid_price=reference_price,
        ),
        intent.created_timestamp_ns,
    )
    if not decision.approved:
        raise ValueError(f"risk rejected replacement: {decision.reason}")
    oms = OrderManagementSystem()
    order = oms.create(intent)
    order = oms.transition(
        order.order_id,
        OrderState.RISK_APPROVED,
        decision.decision_timestamp_ns,
        reason=decision.reason,
    )
    service = BinanceExecutionService(gateway, oms)
    result = await service.replace(
        order.order_id,
        cancel_order_id=args.cancel_order_id,
        cancel_client_order_id=args.cancel_client_order_id,
        reference_price=reference_price,
    )
    _print(
        {
            "replaced": {
                "client_order_id": existing.client_order_id,
                "order_id": existing.order_id,
                "status_before": existing.status,
            },
            "binance": result,
            "replacement_is_live": (
                result.client_order_id == order.client_order_id
            ),
            "oms_order": oms.orders[0],
            "oms_updates": oms.updates,
            "rate_limits": gateway.last_rate_limits,
        }
    )


@dataclass(frozen=True)
class _LegSpec:
    symbol: str
    side: Side
    order_type: OrderType
    quantity: Decimal
    price: Decimal | None


def _parse_leg(raw: str) -> _LegSpec:
    fields = raw.split(":")
    if len(fields) not in {4, 5}:
        raise ValueError(
            f"leg {raw!r} must be SYMBOL:SIDE:TYPE:QUANTITY[:PRICE]"
        )
    symbol, side, order_type, quantity = fields[:4]
    price = fields[4] if len(fields) == 5 else None
    if side.lower() not in {"buy", "sell"}:
        raise ValueError(f"leg {raw!r} has an unknown side {side!r}")
    types = {
        "market": OrderType.MARKET,
        "limit": OrderType.LIMIT,
        "post-only": OrderType.POST_ONLY,
    }
    if order_type.lower() not in types:
        raise ValueError(f"leg {raw!r} has an unknown type {order_type!r}")
    resolved_type = types[order_type.lower()]
    if resolved_type is not OrderType.MARKET and price is None:
        raise ValueError(f"leg {raw!r} is a limit order and needs a price")
    try:
        parsed_quantity = Decimal(quantity)
        parsed_price = None if price is None else Decimal(price)
    except InvalidOperation as error:
        raise ValueError(f"leg {raw!r} has a non-numeric field") from error
    if parsed_quantity <= 0:
        raise ValueError(f"leg {raw!r} must have a positive quantity")
    if parsed_price is not None and parsed_price <= 0:
        raise ValueError(f"leg {raw!r} must have a positive price")
    return _LegSpec(
        symbol=symbol.upper(),
        side=Side(side.lower()),
        order_type=resolved_type,
        quantity=parsed_quantity,
        price=parsed_price,
    )


async def _multi_leg(
    args: argparse.Namespace,
    config: AppConfig,
    gateway: BinanceSpotTestnetGateway,
) -> None:
    """Admit every leg before any of them reaches the exchange.

    A multi-leg trade is only safe if the whole set is affordable and
    within risk before the first message goes out. Admitting legs one at a
    time can leave the account holding one side of a spread when a later
    leg is refused, so every check below runs against all legs first and
    dispatch happens only once they all pass.
    """
    if not args.test_only and args.confirm != "TESTNET":
        raise ValueError(
            "actual Testnet submission requires --confirm TESTNET"
        )
    legs = [_parse_leg(raw) for raw in args.leg]
    if len(legs) < 2:
        raise ValueError("a multi-leg trade needs at least two legs")
    client_order_ids = {leg.symbol for leg in legs}
    if len(client_order_ids) != len(legs):
        raise ValueError("each leg must use a distinct symbol")

    account = await gateway.account()
    if not account.get("canTrade", False):
        raise ValueError("Binance Testnet account is not allowed to trade")
    free_balances = {
        balance["asset"]: Decimal(balance["free"])
        for balance in account.get("balances", [])
    }
    total_balances = {
        balance["asset"]: Decimal(balance["free"])
        + Decimal(balance["locked"])
        for balance in account.get("balances", [])
    }
    open_order_count = len(await gateway.open_orders())

    risk = RiskEngine(config.risk)
    oms = OrderManagementSystem()
    required: dict[str, Decimal] = {}
    admitted: list[tuple[_LegSpec, Any]] = []
    plan: list[dict[str, Any]] = []

    for index, leg in enumerate(legs):
        rules = await gateway.symbol_rules(leg.symbol)
        reference_price = await gateway.ticker_price(leg.symbol)
        effective_price = leg.price or reference_price
        asset = rules.quote_asset if leg.side is Side.BUY else rules.base_asset
        amount = (
            leg.quantity * effective_price
            if leg.side is Side.BUY
            else leg.quantity
        )
        required[asset] = required.get(asset, Decimal(0)) + amount
        intent = OrderIntent(
            intent_id=uuid4(),
            strategy_id="binance_testnet_multi_leg",
            symbol=leg.symbol,
            side=leg.side,
            order_type=leg.order_type,
            quantity=leg.quantity,
            price=leg.price,
            created_timestamp_ns=time.time_ns(),
        )
        decision = risk.evaluate(
            intent,
            RiskContext(
                position=total_balances.get(rules.base_asset, Decimal(0)),
                # Every earlier leg in this trade is also about to rest.
                open_order_count=open_order_count + index,
                daily_net_pnl=args.daily_net_pnl,
                feed_stale=False,
                mid_price=reference_price,
            ),
            intent.created_timestamp_ns,
        )
        if not decision.approved:
            raise ValueError(
                f"risk rejected leg {leg.symbol}: {decision.reason}"
            )
        order = oms.create(intent)
        order = oms.transition(
            order.order_id,
            OrderState.RISK_APPROVED,
            decision.decision_timestamp_ns,
            reason=decision.reason,
        )
        # Normalising now surfaces tick, lot and notional violations while
        # the trade can still be abandoned without exposure.
        normalized = await gateway.normalize_order(
            order,
            reference_price=reference_price,
        )
        admitted.append((leg, order))
        plan.append(
            {
                "symbol": leg.symbol,
                "side": leg.side.value,
                "type": leg.order_type.value,
                "client_order_id": order.client_order_id,
                "quantity": normalized.quantity,
                "price": normalized.price,
                "reference_price": reference_price,
            }
        )

    shortfalls = {
        asset: {"required": amount, "free": free_balances.get(asset, Decimal(0))}
        for asset, amount in required.items()
        if free_balances.get(asset, Decimal(0)) < amount
    }
    if shortfalls:
        raise ValueError(
            "insufficient Testnet balance for the whole trade: "
            f"{ {asset: str(values['required']) for asset, values in shortfalls.items()} }"
        )

    service = BinanceExecutionService(gateway, oms)
    dispatched = await asyncio.gather(
        *(
            service.submit(order.order_id, test_only=args.test_only)
            for _leg, order in admitted
        ),
        return_exceptions=True,
    )

    results: list[dict[str, Any]] = []
    failures = 0
    for (leg, order), outcome in zip(admitted, dispatched):
        if isinstance(outcome, BaseException):
            failures += 1
            results.append(
                {
                    "symbol": leg.symbol,
                    "client_order_id": order.client_order_id,
                    "dispatched": False,
                    "error": f"{type(outcome).__name__}: {outcome}",
                }
            )
            continue
        results.append(
            {
                "symbol": leg.symbol,
                "client_order_id": order.client_order_id,
                "dispatched": True,
                "binance": outcome,
            }
        )
    _print(
        {
            "admission": {
                "legs": len(legs),
                "all_legs_admitted": True,
                "required_balances": {
                    asset: str(amount) for asset, amount in required.items()
                },
                "plan": plan,
            },
            "dispatch": {
                "concurrent": True,
                "failed_legs": failures,
                "results": results,
            },
            "oms_orders": oms.orders,
            "oms_updates": oms.updates,
            "rate_limits": gateway.last_rate_limits,
        }
    )


def _add_order_lookup_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--symbol", default="BTCUSDT")
    lookup = parser.add_mutually_exclusive_group(required=True)
    lookup.add_argument("--order-id", type=int)
    lookup.add_argument("--client-order-id")


def _print(value) -> None:
    print(
        json.dumps(
            value,
            default=_json_default,
            indent=2,
            sort_keys=True,
        )
    )


def _json_default(value):
    if isinstance(value, msgspec.Struct):
        return msgspec.to_builtins(value)
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, tuple):
        return list(value)
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"cannot serialize {type(value).__name__}")
