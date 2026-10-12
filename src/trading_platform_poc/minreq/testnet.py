"""Venue testnet execution behind the router's matcher interface (MR-07, MR-08).

``execution_mode: venue_testnet`` replaces the internal matcher with
:class:`VenueTestnetExecution`. The router talks to both through the same small
surface (``send``, ``release``, ``request_cancel``, ``query_order``,
``available_depth``, ``venue_snapshot`` ...), so a testnet paper run exercises
exactly the order state machine a backtest does, but orders go to a real
exchange testnet and every fill is labelled ``venue_testnet``.

Venue calls are asynchronous and the router is not. Each call runs as a task on
the live event loop; its result is stamped with the wall-clock time it came
back and handed to the router by :meth:`VenueTestnetExecution.release`. A call
that raises produces *no* report: the router's acknowledgement timeout moves
the order to ``unknown`` and the venue is queried, the same path a lost
acknowledgement takes in simulation. An order is never resent while unknown.

Adapters reuse the existing guarded gateways, which read credentials only from
the process environment and refuse non-testnet endpoints:

* ``binance`` -> :class:`~trading_platform_poc.binance_execution.BinanceSpotTestnetGateway`
* ``okx``     -> :class:`~trading_platform_poc.okx_demo.OKXDemoAdapter` (simulated trading)

Neither gateway exposes IOC for these order types, so IOC is emulated by
cancelling whatever is still open immediately after the submit returns.
Limitation: resting maker orders learn about fills only through queries and
the cancel at their time-to-live; there is no private fill stream yet.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import math
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping, Protocol, Sequence

from trading_platform_poc.minreq.canonical import Liquidity, PriceLevel
from trading_platform_poc.minreq.latency import LatencyModel
from trading_platform_poc.minreq.matching import (
    ExecutionMode,
    MatchingConfig,
    MatchOutcome,
    OrderRequest,
    RejectReason,
    SimulatedFill,
    VenueOrderView,
)
from trading_platform_poc.minreq.quality import CanonicalBook
from trading_platform_poc.minreq.securitymaster import SecurityMaster

SUPPORTED_TESTNET_VENUES = ("binance", "okx")


@dataclass(frozen=True, slots=True)
class VenueOrderReport:
    """A venue's answer about one order, with cumulative fill quantities."""

    client_order_id: str
    state: str
    quantity: Decimal
    filled_quantity: Decimal
    average_price: Decimal | None
    reject_reason: str = ""
    venue_order_id: str = ""
    timestamp_ns: int = 0


class VenueGateway(Protocol):
    venue: str

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def submit(
        self, request: OrderRequest, venue_symbol: str
    ) -> VenueOrderReport: ...

    async def query(
        self, request: OrderRequest, venue_symbol: str
    ) -> VenueOrderReport | None: ...

    async def cancel(
        self, request: OrderRequest, venue_symbol: str
    ) -> VenueOrderReport | None: ...


def _to_order(request: OrderRequest, venue_symbol: str, venue: str) -> Any:
    from trading_platform_poc.models import Order, OrderState, OrderType, Side

    if request.post_only:
        order_type = OrderType.POST_ONLY
    elif request.order_type.lower() == "market":
        order_type = OrderType.MARKET
    else:
        order_type = OrderType.LIMIT
    return Order(
        order_id=uuid.uuid5(uuid.NAMESPACE_URL, f"order|{request.client_order_id}"),
        intent_id=uuid.uuid5(uuid.NAMESPACE_URL, f"plan|{request.plan_id}"),
        client_order_id=request.client_order_id,
        symbol=venue_symbol,
        side=Side(request.side.lower()),
        order_type=order_type,
        quantity=request.quantity,
        price=request.price,
        state=OrderState.CREATED,
        created_timestamp_ns=request.timestamp_ns,
        venue=venue,
    )


def _wants_ioc(request: OrderRequest) -> bool:
    return request.time_in_force.lower() in {"ioc", "fok"} and not request.post_only


_BINANCE_STATES = {
    "NEW": "acknowledged",
    "PARTIALLY_FILLED": "partially_filled",
    "FILLED": "filled",
    "CANCELED": "cancelled",
    "EXPIRED": "cancelled",
    "EXPIRED_IN_MATCH": "cancelled",
    "PENDING_CANCEL": "partially_filled",
    "REJECTED": "rejected",
}

_OKX_STATES = {
    "accepted": "acknowledged",
    "live": "acknowledged",
    "partially_filled": "partially_filled",
    "filled": "filled",
    "canceled": "cancelled",
    "cancelled": "cancelled",
    "mmp_canceled": "cancelled",
}


class BinanceTestnetGateway:
    """Binance Spot Testnet through the existing signed, guarded gateway."""

    venue = "binance"
    ORDER_DOES_NOT_EXIST = -2013

    def __init__(self, gateway: Any) -> None:
        self._gateway = gateway

    @classmethod
    def from_config(cls, config: Any) -> BinanceTestnetGateway:
        from trading_platform_poc.binance_execution import (
            BinanceCredentials,
            BinanceSpotTestnetGateway,
        )

        credentials = BinanceCredentials.from_environment(config)
        return cls(BinanceSpotTestnetGateway(config, credentials))

    async def start(self) -> None:
        await self._gateway.__aenter__()
        await self._gateway.sync_time()

    async def close(self) -> None:
        await self._gateway.close()

    @staticmethod
    def _report(result: Any) -> VenueOrderReport:
        return VenueOrderReport(
            client_order_id=result.client_order_id,
            state=_BINANCE_STATES.get(str(result.status).upper(), "acknowledged"),
            quantity=result.original_quantity,
            filled_quantity=result.executed_quantity,
            average_price=result.average_fill_price,
            reject_reason="" if str(result.status).upper() != "REJECTED" else "venue_rejected",
            venue_order_id=str(result.order_id),
            timestamp_ns=int(result.transact_time_ms or 0) * 1_000_000,
        )

    async def submit(self, request: OrderRequest, venue_symbol: str) -> VenueOrderReport:
        order = _to_order(request, venue_symbol, self.venue)
        result = await self._gateway.submit_order(order, reference_price=request.price)
        report = self._report(result)
        if _wants_ioc(request) and report.state in {"acknowledged", "partially_filled"}:
            cancelled = await self.cancel(request, venue_symbol)
            if cancelled is not None:
                report = cancelled
        return report

    async def query(
        self, request: OrderRequest, venue_symbol: str
    ) -> VenueOrderReport | None:
        from trading_platform_poc.binance_execution import BinanceAPIError

        try:
            result = await self._gateway.query_order(
                venue_symbol, client_order_id=request.client_order_id
            )
        except BinanceAPIError as error:
            if getattr(error, "code", None) == self.ORDER_DOES_NOT_EXIST:
                return None
            raise
        return self._report(result)

    async def cancel(
        self, request: OrderRequest, venue_symbol: str
    ) -> VenueOrderReport | None:
        from trading_platform_poc.binance_execution import BinanceAPIError

        try:
            result = await self._gateway.cancel_order(
                venue_symbol, client_order_id=request.client_order_id
            )
        except BinanceAPIError:
            return await self.query(request, venue_symbol)
        return self._report(result)


class OkxDemoGateway:
    """OKX simulated trading through the existing guarded demo adapter."""

    venue = "okx"
    ORDER_DOES_NOT_EXIST = "51603"

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    @classmethod
    def from_config(cls, config: Any) -> OkxDemoGateway:
        from trading_platform_poc.okx_demo import (
            OKXCredentials,
            OKXDemoAdapter,
            OKXDemoConfig,
        )

        okx_config = OKXDemoConfig(
            rest_base_url=config.rest_base_url,
            api_key_environment_variable=config.api_key_environment_variable,
            api_secret_environment_variable=config.api_secret_environment_variable,
            passphrase_environment_variable=config.passphrase_environment_variable,
            request_timeout_seconds=float(config.request_timeout_seconds),
            transaction_expiry_ms=int(config.transaction_expiry_ms),
            instrument_type=config.instrument_type,
            trade_mode=config.trade_mode,
        )
        credentials = OKXCredentials.from_environment(okx_config)
        return cls(OKXDemoAdapter(okx_config, credentials))

    async def start(self) -> None:
        await self._adapter.__aenter__()
        await self._adapter.sync_time()

    async def close(self) -> None:
        await self._adapter.close()

    @staticmethod
    def _report(result: Any, client_order_id: str) -> VenueOrderReport:
        return VenueOrderReport(
            client_order_id=client_order_id,
            state=_OKX_STATES.get(str(result.status).lower(), "acknowledged"),
            quantity=result.quantity,
            filled_quantity=result.filled_quantity,
            average_price=result.average_fill_price,
            venue_order_id=str(result.exchange_order_id),
            timestamp_ns=int(result.timestamp_ms or 0) * 1_000_000,
        )

    def _venue_client_id(self, request: OrderRequest, venue_symbol: str) -> str:
        return self._adapter.client_order_id(_to_order(request, venue_symbol, self.venue))

    async def submit(self, request: OrderRequest, venue_symbol: str) -> VenueOrderReport:
        order = _to_order(request, venue_symbol, self.venue)
        await self._adapter.submit_order(order)
        report = await self.query(request, venue_symbol)
        if report is None:
            raise RuntimeError("okx accepted the order but cannot find it")
        if _wants_ioc(request) and report.state in {"acknowledged", "partially_filled"}:
            cancelled = await self.cancel(request, venue_symbol)
            if cancelled is not None:
                report = cancelled
        return report

    async def query(
        self, request: OrderRequest, venue_symbol: str
    ) -> VenueOrderReport | None:
        from trading_platform_poc.exchange_adapter import ExchangeAPIError

        try:
            result = await self._adapter.query_order(
                venue_symbol,
                client_order_id=self._venue_client_id(request, venue_symbol),
            )
        except ExchangeAPIError as error:
            if self.ORDER_DOES_NOT_EXIST in str(error):
                return None
            raise
        return self._report(result, request.client_order_id)

    async def cancel(
        self, request: OrderRequest, venue_symbol: str
    ) -> VenueOrderReport | None:
        from trading_platform_poc.exchange_adapter import ExchangeAdapterError

        try:
            await self._adapter.cancel_order(
                venue_symbol,
                client_order_id=self._venue_client_id(request, venue_symbol),
            )
        except ExchangeAdapterError:
            pass
        return await self.query(request, venue_symbol)


class VenueTestnetExecution:
    """Routes the router's child orders to venue testnets, asynchronously."""

    mode = ExecutionMode.VENUE_TESTNET

    def __init__(
        self,
        gateways: Mapping[str, VenueGateway],
        *,
        books: Mapping[str, CanonicalBook],
        latency: LatencyModel,
        config: MatchingConfig,
        security_master: SecurityMaster,
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        if config.mode is not ExecutionMode.VENUE_TESTNET:
            raise ValueError(
                "VenueTestnetExecution requires MatchingConfig(mode=venue_testnet)"
            )
        self.config = config
        self._gateways = dict(gateways)
        self._books = books
        self._latency = latency
        self._security_master = security_master
        self._clock = clock
        self._tasks: set[asyncio.Task[Any]] = set()
        self._completed: list[tuple[int, int, MatchOutcome]] = []
        self._sequence = itertools.count()
        self._requests: dict[str, OrderRequest] = {}
        self._reports: dict[str, VenueOrderReport] = {}
        self._reported_fills: dict[str, tuple[Decimal, Decimal]] = {}
        self._queries_running: set[str] = set()
        self.unavailable_venues: set[str] = set()
        self.rejects: list[dict[str, Any]] = []
        self.positions: dict[tuple[str, str], Decimal] = {}
        self.fills: list[SimulatedFill] = []
        self.errors: list[str] = []
        self.orders_received = 0

    # --------------------------------------------------------------- lifecycle
    def assert_ready(self, venues: Sequence[str]) -> None:
        missing = sorted(set(venues) - set(self._gateways))
        if missing:
            raise ValueError(
                f"venue_testnet has no adapter for {missing}; supported venues "
                f"are {list(SUPPORTED_TESTNET_VENUES)}"
            )

    async def start(self) -> None:
        for gateway in self._gateways.values():
            await gateway.start()

    async def settle(self, timeout_seconds: float = 5.0) -> None:
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout_seconds)

    async def close(self) -> None:
        await self.settle()
        for gateway in self._gateways.values():
            try:
                await gateway.close()
            except Exception as error:  # closing must not mask the run result
                self.errors.append(f"close {gateway.venue}: {error}")

    # ----------------------------------------------------------------- orders
    def _venue_symbol(self, request: OrderRequest) -> str:
        record = self._security_master.resolve(
            request.instrument_id, request.timestamp_ns
        )
        return record.venue_symbol

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def send(self, request: OrderRequest) -> int:
        self.orders_received += 1
        self._requests[request.client_order_id] = request
        gateway = self._gateways.get(request.venue)
        if gateway is None or request.venue in self.unavailable_venues:
            self._reject(request, RejectReason.VENUE_UNAVAILABLE)
            return request.timestamp_ns
        self._spawn(self._run_submit(gateway, request))
        return request.timestamp_ns

    async def _run_submit(self, gateway: VenueGateway, request: OrderRequest) -> None:
        try:
            report = await gateway.submit(request, self._venue_symbol(request))
        except Exception as error:
            # No report: the router times the order out, marks it unknown and
            # asks the venue what happened instead of assuming anything.
            self.errors.append(
                f"submit {request.client_order_id}: {type(error).__name__}: {error}"
            )
            return
        self._complete(request, report, "order")

    def request_cancel(self, client_order_id: str, timestamp_ns: int) -> int | None:
        request = self._requests.get(client_order_id)
        gateway = None if request is None else self._gateways.get(request.venue)
        if request is None or gateway is None:
            return None
        self._spawn(self._run_cancel(gateway, request))
        return timestamp_ns

    async def _run_cancel(self, gateway: VenueGateway, request: OrderRequest) -> None:
        try:
            report = await gateway.cancel(request, self._venue_symbol(request))
        except Exception as error:
            self.errors.append(
                f"cancel {request.client_order_id}: {type(error).__name__}: {error}"
            )
            return
        if report is not None:
            self._complete(request, report, "cancel")

    def query_order(self, client_order_id: str, as_of_ns: int) -> VenueOrderView | None:
        """Answer from the last venue report, or start a real query.

        The router asks synchronously; a live query cannot answer in the same
        call, so ``None`` (unknown) is returned and the venue's answer arrives
        later as an ordinary report that resolves the order.
        """

        report = self._reports.get(client_order_id)
        request = self._requests.get(client_order_id)
        if report is not None and request is not None:
            return VenueOrderView(
                client_order_id=client_order_id,
                state=report.state,
                quantity=report.quantity,
                filled_quantity=report.filled_quantity,
                average_price=report.average_price,
                fills=tuple(
                    fill for fill in self.fills if fill.client_order_id == client_order_id
                ),
                reject_reason=report.reject_reason,
                resting_quantity=(
                    report.quantity - report.filled_quantity
                    if report.state in {"acknowledged", "partially_filled"}
                    else Decimal(0)
                ),
                arrival_timestamp_ns=report.timestamp_ns,
            )
        if request is not None and client_order_id not in self._queries_running:
            gateway = self._gateways.get(request.venue)
            if gateway is not None:
                self._queries_running.add(client_order_id)
                self._spawn(self._run_query(gateway, request))
        return None

    async def _run_query(self, gateway: VenueGateway, request: OrderRequest) -> None:
        try:
            report = await gateway.query(request, self._venue_symbol(request))
        except Exception as error:
            self.errors.append(
                f"query {request.client_order_id}: {type(error).__name__}: {error}"
            )
            report = None
        finally:
            self._queries_running.discard(request.client_order_id)
        if report is not None:
            self._complete(request, report, "order")

    # ---------------------------------------------------------------- reports
    def _reject(self, request: OrderRequest, reason: RejectReason) -> None:
        now = self._clock()
        self.rejects.append(
            {
                "timestamp_ns": now,
                "venue": request.venue,
                "instrument_id": request.instrument_id,
                "client_order_id": request.client_order_id,
                "reason": reason.value,
            }
        )
        outcome = MatchOutcome(
            request=request,
            accepted=False,
            state="rejected",
            ack_timestamp_ns=now,
            fills=(),
            reject_reason=reason.value,
            source=ExecutionMode.VENUE_TESTNET,
            arrival_timestamp_ns=now,
        )
        heapq.heappush(self._completed, (now, next(self._sequence), outcome))

    def _complete(self, request: OrderRequest, report: VenueOrderReport, kind: str) -> None:
        now = self._clock()
        self._reports[request.client_order_id] = report
        fills = self._incremental_fills(request, report, now)
        if report.state == "rejected":
            self.rejects.append(
                {
                    "timestamp_ns": now,
                    "venue": request.venue,
                    "instrument_id": request.instrument_id,
                    "client_order_id": request.client_order_id,
                    "reason": report.reject_reason or "venue_rejected",
                }
            )
        resting = (
            report.quantity - report.filled_quantity
            if report.state in {"acknowledged", "partially_filled"}
            else Decimal(0)
        )
        outcome = MatchOutcome(
            request=request,
            accepted=report.state != "rejected",
            state=report.state,
            ack_timestamp_ns=now,
            fills=tuple(fills),
            reject_reason=report.reject_reason,
            venue_order_id=report.venue_order_id,
            resting_quantity=resting,
            source=ExecutionMode.VENUE_TESTNET,
            arrival_timestamp_ns=report.timestamp_ns or now,
            expired_quantity=(
                report.quantity - report.filled_quantity
                if report.state == "cancelled"
                else Decimal(0)
            ),
            kind=kind,
        )
        heapq.heappush(self._completed, (now, next(self._sequence), outcome))

    def _incremental_fills(
        self, request: OrderRequest, report: VenueOrderReport, now: int
    ) -> list[SimulatedFill]:
        """Venues report cumulative fills; the ledger needs each increment once."""

        previous_quantity, previous_notional = self._reported_fills.get(
            request.client_order_id, (Decimal(0), Decimal(0))
        )
        if report.filled_quantity <= previous_quantity or report.average_price is None:
            return []
        notional = report.filled_quantity * report.average_price
        increment = report.filled_quantity - previous_quantity
        price = (notional - previous_notional) / increment
        self._reported_fills[request.client_order_id] = (report.filled_quantity, notional)
        fill = SimulatedFill(
            client_order_id=request.client_order_id,
            child_order_id=request.child_order_id,
            plan_id=request.plan_id,
            venue=request.venue,
            instrument_id=request.instrument_id,
            side=request.side,
            quantity=increment,
            price=price,
            timestamp_ns=now,
            liquidity=Liquidity.MAKER if request.post_only else Liquidity.TAKER,
            source=ExecutionMode.VENUE_TESTNET,
            levels_consumed=0,
            fill_id=f"{request.client_order_id}:{report.filled_quantity}",
            execution_timestamp_ns=report.timestamp_ns or now,
        )
        key = (request.instrument_id, request.venue)
        signed = increment if request.side == "buy" else -increment
        self.positions[key] = self.positions.get(key, Decimal(0)) + signed
        self.fills.append(fill)
        return [fill]

    def release(self, now_ns: int) -> list[MatchOutcome]:
        outcomes: list[MatchOutcome] = []
        while self._completed and self._completed[0][0] <= now_ns:
            outcomes.append(heapq.heappop(self._completed)[2])
        return outcomes

    def next_event_ns(self) -> int | None:
        return self._completed[0][0] if self._completed else None

    @property
    def in_flight_count(self) -> int:
        return len(self._tasks) + len(self._completed)

    # ------------------------------------------------------------- estimates
    def available_depth(self, venue: str, side: str) -> tuple[PriceLevel, ...]:
        book = self._books.get(venue)
        if book is None or not book.initialized:
            return ()
        return book.depth(side)

    def expected_fill_probability(
        self,
        venue: str,
        side: str,
        quantity: Decimal,
        limit_price: Decimal | None,
    ) -> Decimal:
        if quantity <= 0:
            return Decimal(0)
        levels = self.available_depth(venue, side)
        if limit_price is not None:
            levels = tuple(
                level
                for level in levels
                if (level.price <= limit_price if side == "buy" else level.price >= limit_price)
            )
        coverage = min(
            Decimal(1), sum((level.quantity for level in levels), Decimal(0)) / quantity
        )
        half_life = max(1, self.config.liquidity_half_life_ns)
        decay = math.pow(0.5, self._latency.expected_ns(venue, "entry") / half_life)
        return (coverage * Decimal(repr(round(decay, 6)))).quantize(Decimal("0.000001"))

    # ------------------------------------------------------- market callbacks
    def on_book_update(self, venue: str) -> None:
        return None

    def on_trade(self, *args: Any, **kwargs: Any) -> list[SimulatedFill]:
        return []

    def set_venue_available(self, venue: str, available: bool) -> None:
        if available:
            self.unavailable_venues.discard(venue)
        else:
            self.unavailable_venues.add(venue)

    def venue_snapshot(self) -> dict[str, Any]:
        return {
            "orders": {
                client_order_id: {
                    "state": report.state,
                    "filled_quantity": str(report.filled_quantity),
                    "leaves_quantity": str(report.quantity - report.filled_quantity),
                }
                for client_order_id, report in sorted(self._reports.items())
                if report.state in {"acknowledged", "partially_filled"}
            },
            "positions": {
                f"{instrument_id}@{venue}": str(quantity)
                for (instrument_id, venue), quantity in sorted(self.positions.items())
            },
        }


def build_testnet_gateways(app_config: Any, venues: Sequence[str]) -> dict[str, VenueGateway]:
    """Construct gateways from the application config and the environment."""

    unsupported = sorted(set(venues) - set(SUPPORTED_TESTNET_VENUES))
    if unsupported:
        raise ValueError(
            f"venue_testnet has no adapter for {unsupported}; supported venues "
            f"are {list(SUPPORTED_TESTNET_VENUES)}"
        )
    gateways: dict[str, VenueGateway] = {}
    if "binance" in venues:
        gateways["binance"] = BinanceTestnetGateway.from_config(app_config.binance_execution)
    if "okx" in venues:
        gateways["okx"] = OkxDemoGateway.from_config(app_config.okx_demo_execution)
    return gateways


def venue_testnet_matcher_factory(
    gateways: Mapping[str, VenueGateway],
    venues: Sequence[str],
    *,
    security_master: SecurityMaster,
    clock: Callable[[], int] = time.time_ns,
) -> Callable[..., VenueTestnetExecution]:
    """Factory for ``TradingSession(matcher_factory=...)`` that fails fast."""

    missing = sorted(set(venues) - set(gateways))
    if missing:
        raise ValueError(
            f"venue_testnet has no adapter for {missing}; supported venues "
            f"are {list(SUPPORTED_TESTNET_VENUES)}"
        )

    def factory(
        *, books: Mapping[str, CanonicalBook], latency: LatencyModel, config: MatchingConfig
    ) -> VenueTestnetExecution:
        execution = VenueTestnetExecution(
            gateways,
            books=books,
            latency=latency,
            config=config,
            security_master=security_master,
            clock=clock,
        )
        execution.assert_ready(venues)
        return execution

    return factory
