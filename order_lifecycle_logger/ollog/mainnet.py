"""MAINNET configuration for the fill probe, kept apart from endpoints.py so the testnet guard is unchanged.

Mainnet orders are REAL orders with real money. This module only declares what is allowed; the CLI refuses to use it unless the
operator passes the typed confirmation, sets OLLOG_MAINNET=1, and stays inside LIMITS. It has its own host allow-list and its own
credential variable names (never the testnet ones), and covers the two perpetual markets only. Public market-data capture on
mainnet (`--capture-only`) needs no credentials and sends no orders.
"""
from __future__ import annotations

from decimal import Decimal
from urllib.parse import urlsplit

CONFIRM_PHRASE = "I-ACCEPT-REAL-ORDERS"

MAINNET_HOSTS = frozenset({"api.bybit.com", "stream.bybit.com", "fapi.binance.com", "fstream.binance.com"})

MAINNET_MARKETS: dict[str, dict] = {
    "bybit_linear": {"venue": "bybit", "category": "linear", "rest": "https://api.bybit.com", "ws": "wss://stream.bybit.com/v5/private",
                     "env": ("BYBIT_MAINNET_KEY", "BYBIT_MAINNET_SECRET")},
    "binance_futures": {"venue": "binance", "rest": "https://fapi.binance.com", "ws": "wss://fstream.binance.com/ws/",
                        "env": ("BINANCE_FUT_MAINNET_KEY", "BINANCE_FUT_MAINNET_SECRET")},
}
# Binance USD-M mainnet serves depth under /public/ and aggTrade under /market/ (the legacy combined URL delivers depth only), so two
# connections: (depth_base, trade_base).
PUBLIC_WS_MAINNET = {"bybit_linear": "wss://stream.bybit.com/v5/public/linear",
                     "binance_futures": ("wss://fstream.binance.com/public/stream?streams=", "wss://fstream.binance.com/market/stream?streams=")}

# Hard ceilings for orders. The recorder on the VM already archives these two symbols, so probes are limited to them.
LIMITS = {"symbols": frozenset({"BTCUSDT", "SOLUSDT"}), "max_orders": 200, "max_notional": Decimal(150), "max_net_notional": Decimal(150)}


class NotMainnetAllowed(RuntimeError):
    pass


def assert_mainnet_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("https", "wss"):
        raise NotMainnetAllowed(f"refusing non-TLS url scheme {parts.scheme!r}")
    if parts.username or parts.password or parts.port not in (None, 443):
        raise NotMainnetAllowed("refusing url with userinfo or a non-default port")
    host = (parts.hostname or "").lower()
    if host not in MAINNET_HOSTS:
        raise NotMainnetAllowed(f"refusing host {host!r}: not one of the mainnet hosts {sorted(MAINNET_HOSTS)}")
    return url


for _m in MAINNET_MARKETS.values():
    assert_mainnet_url(_m["rest"])
    assert_mainnet_url(_m["ws"])
for _u in PUBLIC_WS_MAINNET.values():
    for _x in ((_u,) if isinstance(_u, str) else _u):
        assert_mainnet_url(_x)


def violations(markets, symbols, max_orders, max_notional, max_net_notional, confirm: str | None, env_flag: str | None) -> list[str]:
    """Every reason a mainnet ORDER run is refused. Empty list = allowed."""
    out = []
    if confirm != CONFIRM_PHRASE:
        out.append(f"--confirm-mainnet must be exactly {CONFIRM_PHRASE}")
    if env_flag != "1":
        out.append("environment variable OLLOG_MAINNET=1 is not set")
    bad = [m for m in markets if m not in MAINNET_MARKETS]
    if bad:
        out.append(f"market(s) {bad} are not available on mainnet (perpetual markets only)")
    extra = sorted(set(symbols) - LIMITS["symbols"])
    if extra:
        out.append(f"symbol(s) {extra} are outside the mainnet allow-list {sorted(LIMITS['symbols'])}")
    if max_orders > LIMITS["max_orders"]:
        out.append(f"--max-orders {max_orders} exceeds the mainnet ceiling {LIMITS['max_orders']}")
    if max_notional > LIMITS["max_notional"]:
        out.append(f"--max-notional {max_notional} exceeds the mainnet ceiling {LIMITS['max_notional']}")
    if max_net_notional > LIMITS["max_net_notional"]:
        out.append(f"--max-net-notional {max_net_notional} exceeds the mainnet ceiling {LIMITS['max_net_notional']}")
    return out
