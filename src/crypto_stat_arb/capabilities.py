"""Venue / product / adapter capability matrix (requirement REF05).

For each venue and product the platform trades, this records what the **venue**
supports (order types, time in force, post-only, reduce-only, amend, self-trade
prevention, rate-limit budgets and their scope) and what **our adapter** for it
actually implements. The router is configured from the intersection, so an
instruction the route cannot carry out is refused before it is sent instead of being
silently mistranslated.

Every venue fact carries where it came from:

* ``DOCUMENTED``: checked against the venue's public API documentation (URL in ``sources``);
* ``ASSUMED``: believed correct but not confirmed from a primary source. Verify before
  relying on it (e.g. rate-limit numbers before sizing a quoting strategy).

Notable consequences:

* **Reduce-only on spot is platform-only.** Spot venues have no reduce-only flag; the
  OMS sizes unwinds to the residual, but the venue does not enforce it.
* **Our testnet transports do not implement replace/amend.** A ``REPLACE`` there would
  have placed a second order and left the first live, so testnet routes refuse it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .execution import OrderType, TimeInForce, VenueRouteStatus

AS_OF = "2026-10-25"
BYBIT_DOCS = ("https://bybit-exchange.github.io/docs/v5/order/create-order",
              "https://bybit-exchange.github.io/docs/v5/order/amend-order",
              "https://bybit-exchange.github.io/docs/v5/rate-limit")
BINANCE_DOCS = ("https://developers.binance.com/docs/binance-spot-api-docs/faqs/stp_faq",
                "https://developers.binance.com/docs/derivatives/usds-margined-futures/faq/stp-faq",
                "https://developers.binance.com/docs/derivatives/change-log")


@dataclass(frozen=True)
class RateLimit:
    scope: str              # IP | ACCOUNT | UID_ENDPOINT
    name: str
    limit: int
    interval_seconds: float
    unit: str               # requests | weight | orders
    status: str             # DOCUMENTED | ASSUMED


@dataclass(frozen=True)
class VenueCapabilities:
    venue: str
    product: str                                    # SPOT | PERP
    order_types: tuple[str, ...]
    time_in_force: tuple[str, ...]
    post_only: str                                  # how it is expressed
    reduce_only: str                                # VENUE | NONE (platform-only)
    amend: str                                      # SUPPORTED | NOT_SUPPORTED
    self_trade_prevention: tuple[str, ...]
    rate_limits: tuple[RateLimit, ...]
    sources: tuple[str, ...]
    assumed: tuple[str, ...] = ()                    # fields not confirmed from a primary source
    notes: str = ""


@dataclass(frozen=True)
class AdapterCapabilities:
    name: str
    order_types: tuple[str, ...]
    time_in_force: tuple[str, ...]
    post_only: bool
    reduce_only_sent: bool
    replace: bool
    self_trade_prevention: bool = False


VENUES: dict[tuple[str, str], VenueCapabilities] = {
    ("BINANCE", "SPOT"): VenueCapabilities(
        "BINANCE", "SPOT",
        order_types=("LIMIT", "MARKET", "LIMIT_MAKER", "STOP_LOSS", "STOP_LOSS_LIMIT", "TAKE_PROFIT",
                     "TAKE_PROFIT_LIMIT"),
        time_in_force=("GTC", "IOC", "FOK"),
        post_only="order type LIMIT_MAKER",
        reduce_only="NONE",
        amend="SUPPORTED",
        self_trade_prevention=("NONE", "EXPIRE_TAKER", "EXPIRE_MAKER", "EXPIRE_BOTH"),
        rate_limits=(RateLimit("IP", "REQUEST_WEIGHT", 6_000, 60, "weight", "ASSUMED"),
                     RateLimit("ACCOUNT", "ORDERS", 100, 10, "orders", "ASSUMED"),
                     RateLimit("ACCOUNT", "ORDERS", 200_000, 86_400, "orders", "ASSUMED")),
        sources=BINANCE_DOCS[:1],
        assumed=("order_types", "amend", "rate_limits"),
        notes="amend = order amend keep-priority (quantity decrease only) and cancel-replace",
    ),
    ("BINANCE", "PERP"): VenueCapabilities(
        "BINANCE", "PERP",
        order_types=("LIMIT", "MARKET", "STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET",
                     "TRAILING_STOP_MARKET"),
        time_in_force=("GTC", "IOC", "FOK", "GTX", "GTD"),
        post_only="time in force GTX",
        reduce_only="VENUE",
        amend="SUPPORTED",
        self_trade_prevention=("EXPIRE_TAKER", "EXPIRE_MAKER", "EXPIRE_BOTH"),
        rate_limits=(RateLimit("IP", "REQUEST_WEIGHT", 2_400, 60, "weight", "ASSUMED"),
                     RateLimit("ACCOUNT", "ORDERS", 300, 10, "orders", "ASSUMED"),
                     RateLimit("ACCOUNT", "ORDERS", 1_200, 60, "orders", "ASSUMED")),
        sources=BINANCE_DOCS[1:],
        assumed=("order_types", "rate_limits"),
        notes="amend = PUT /fapi/v1/order (price/quantity), keeps selfTradePreventionMode; "
              "STP only applies with IOC, GTC or GTD",
    ),
    ("BYBIT", "SPOT"): VenueCapabilities(
        "BYBIT", "SPOT",
        order_types=("Limit", "Market"),
        time_in_force=("GTC", "IOC", "FOK", "PostOnly"),
        post_only="time in force PostOnly",
        reduce_only="NONE",
        amend="SUPPORTED",
        self_trade_prevention=("CancelMaker", "CancelTaker", "CancelBoth"),
        rate_limits=(RateLimit("IP", "HTTP", 600, 5, "requests", "DOCUMENTED"),
                     RateLimit("UID_ENDPOINT", "/v5/order/create", 20, 1, "requests", "ASSUMED"),
                     RateLimit("UID_ENDPOINT", "/v5/order/amend", 10, 1, "requests", "ASSUMED"),
                     RateLimit("UID_ENDPOINT", "/v5/order/cancel", 20, 1, "requests", "ASSUMED")),
        sources=BYBIT_DOCS,
        assumed=("self_trade_prevention", "rate_limits:UID_ENDPOINT"),
        notes="reduceOnly is not valid for spot; per-UID limits are per endpoint (spot/linear mapping "
              "of the documented 10/s and 20/s values not confirmed)",
    ),
    ("BYBIT", "PERP"): VenueCapabilities(
        "BYBIT", "PERP",
        order_types=("Limit", "Market"),
        time_in_force=("GTC", "IOC", "FOK", "PostOnly"),
        post_only="time in force PostOnly",
        reduce_only="VENUE",
        amend="SUPPORTED",
        self_trade_prevention=("CancelMaker", "CancelTaker", "CancelBoth"),
        rate_limits=(RateLimit("IP", "HTTP", 600, 5, "requests", "DOCUMENTED"),
                     RateLimit("UID_ENDPOINT", "/v5/order/create", 10, 1, "requests", "ASSUMED"),
                     RateLimit("UID_ENDPOINT", "/v5/order/amend", 10, 1, "requests", "ASSUMED"),
                     RateLimit("UID_ENDPOINT", "/v5/order/cancel", 10, 1, "requests", "ASSUMED")),
        sources=BYBIT_DOCS,
        assumed=("self_trade_prevention", "rate_limits:UID_ENDPOINT"),
        notes="category=linear; positionIdx 0 = one-way mode (the adapter requires one-way)",
    ),
}

# What each of our adapters implements (from crypto_stat_arb.testnet / execution).
INTERNAL = AdapterCapabilities("internal simulation / paper", ("LIMIT", "MARKET"), ("GTC", "IOC", "FOK"),
                               post_only=True, reduce_only_sent=True, replace=True)
ADAPTERS: dict[tuple[str, str], AdapterCapabilities] = {
    ("BINANCE", "SPOT"): AdapterCapabilities("BinanceSpotTestnetTransport", ("LIMIT", "MARKET"),
                                             ("GTC", "IOC", "FOK"), post_only=True,
                                             reduce_only_sent=False, replace=False),
    ("BINANCE", "PERP"): AdapterCapabilities("BinanceFuturesTestnetTransport", ("LIMIT", "MARKET"),
                                             ("GTC", "IOC", "FOK"), post_only=True,
                                             reduce_only_sent=True, replace=False),
    ("BYBIT", "SPOT"): AdapterCapabilities("BybitV5TestnetTransport", ("LIMIT", "MARKET"),
                                           ("GTC", "IOC", "FOK"), post_only=True,
                                           reduce_only_sent=False, replace=False),
    ("BYBIT", "PERP"): AdapterCapabilities("BybitV5LinearTestnetTransport", ("LIMIT", "MARKET"),
                                           ("GTC", "IOC", "FOK"), post_only=True,
                                           reduce_only_sent=True, replace=False),
}

_VENUE_ORDER_TYPE = {"Limit": "LIMIT", "Market": "MARKET", "LIMIT_MAKER": "LIMIT"}


def product_of(product_type: str) -> str:
    from .funding import is_perpetual

    return "PERP" if is_perpetual(product_type) else "SPOT"


def capabilities(venue: str, product_type: str) -> VenueCapabilities:
    key = (venue, product_of(product_type))
    if key not in VENUES:
        raise ValueError(f"no capability entry for {venue} {key[1]}: add it to crypto_stat_arb.capabilities")
    return VENUES[key]


def effective(venue: str, product_type: str, *, testnet: bool) -> dict[str, Any]:
    """What the platform can actually use on this route: venue ∩ adapter."""
    venue_caps = capabilities(venue, product_type)
    adapter = ADAPTERS[(venue, venue_caps.product)] if testnet else INTERNAL
    venue_types = {_VENUE_ORDER_TYPE.get(t, t) for t in venue_caps.order_types}
    return {
        "venue": venue, "product": venue_caps.product, "adapter": adapter.name,
        "order_types": sorted(venue_types & set(adapter.order_types)),
        "time_in_force": sorted(set(venue_caps.time_in_force) & set(adapter.time_in_force)),
        "post_only": adapter.post_only and bool(venue_caps.post_only),
        # Reduce-only stays accepted on spot (the OMS sizes unwinds to the residual), but it
        # is platform-enforced only: the venue cannot reject an order that would flip the position.
        "reduce_only": ("VENUE" if venue_caps.reduce_only == "VENUE" and (adapter.reduce_only_sent or not testnet)
                        else "PLATFORM_ONLY"),
        "replace": adapter.replace and venue_caps.amend == "SUPPORTED",
        "self_trade_prevention": adapter.self_trade_prevention and bool(venue_caps.self_trade_prevention),
    }


def configure_route(status: VenueRouteStatus, venue: str, product_type: str, *, testnet: bool) -> VenueRouteStatus:
    caps = effective(venue, product_type, testnet=testnet)
    status.supported_order_types = frozenset(OrderType(t) for t in caps["order_types"])
    status.supported_time_in_force = frozenset(TimeInForce(t) for t in caps["time_in_force"])
    status.supports_post_only = caps["post_only"]
    status.supports_reduce_only = True                       # VENUE or PLATFORM_ONLY: both accepted
    status.supports_replace = caps["replace"]
    return status


def report(venues: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    rows = []
    for (venue, product), caps in sorted(VENUES.items()):
        if venues and venue not in venues:
            continue
        rows.append({"venue_capabilities": asdict(caps),
                     "adapter_testnet": asdict(ADAPTERS[(venue, product)]),
                     "effective_internal": effective(venue, product, testnet=False),
                     "effective_testnet": effective(venue, product, testnet=True),
                     "as_of": AS_OF})
    return rows
