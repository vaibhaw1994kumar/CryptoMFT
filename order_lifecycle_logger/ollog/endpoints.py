"""Testnet endpoints and the guard that keeps every request on them.

The hosts are hard-coded. There is no option, environment variable or argument that changes them, and every REST call and
WebSocket connect passes through `assert_testnet_url`, which accepts an exact host from ALLOWED_HOSTS and nothing else.
"""
from __future__ import annotations

from urllib.parse import urlsplit

LABEL = "testnet-measured"

ALLOWED_HOSTS = frozenset({
    "api-testnet.bybit.com",
    "stream-testnet.bybit.com",
    "testnet.binance.vision",
    "stream.testnet.binance.vision",
    "ws-api.testnet.binance.vision",
    "testnet.binancefuture.com",
    "fstream.binancefuture.com",
})

# market -> endpoints. Bybit uses one testnet key pair for spot and linear; the two Binance testnets need separate pairs.
MARKETS: dict[str, dict] = {
    "bybit_spot": {"venue": "bybit", "category": "spot", "rest": "https://api-testnet.bybit.com",
                   "ws": "wss://stream-testnet.bybit.com/v5/private", "env": ("BYBIT_TESTNET_KEY", "BYBIT_TESTNET_SECRET")},
    "bybit_linear": {"venue": "bybit", "category": "linear", "rest": "https://api-testnet.bybit.com",
                     "ws": "wss://stream-testnet.bybit.com/v5/private", "env": ("BYBIT_TESTNET_KEY", "BYBIT_TESTNET_SECRET")},
    "binance_spot": {"venue": "binance", "rest": "https://testnet.binance.vision",
                     "ws": "wss://stream.testnet.binance.vision/ws/", "env": ("BINANCE_SPOT_TESTNET_KEY", "BINANCE_SPOT_TESTNET_SECRET"),
                     # the REST listenKey endpoints answer HTTP 410; order events come from the WebSocket API subscription
                     "ws_api": "wss://ws-api.testnet.binance.vision/ws-api/v3"},
    "binance_futures": {"venue": "binance", "rest": "https://testnet.binancefuture.com",
                        "ws": "wss://fstream.binancefuture.com/ws/", "env": ("BINANCE_FUT_TESTNET_KEY", "BINANCE_FUT_TESTNET_SECRET")},
}


# Public market-data streams for the perpetual markets (no credentials). Binance streams are appended to the URL by capture.stream_url.
PUBLIC_WS = {"bybit_linear": "wss://stream-testnet.bybit.com/v5/public/linear",
             "binance_futures": "wss://fstream.binancefuture.com/stream?streams="}


class NotTestnet(RuntimeError):
    pass


def assert_testnet_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("https", "wss"):
        raise NotTestnet(f"refusing non-TLS url scheme {parts.scheme!r}")
    if parts.username or parts.password or parts.port not in (None, 443):
        raise NotTestnet("refusing url with userinfo or a non-default port")
    host = (parts.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise NotTestnet(f"refusing host {host!r}: not one of the testnet hosts {sorted(ALLOWED_HOSTS)}")
    return url


for _u in PUBLIC_WS.values():
    assert_testnet_url(_u)
for _m in MARKETS.values():     # import-time self-check: the table itself must pass the guard
    assert_testnet_url(_m["rest"])
    assert_testnet_url(_m["ws"])
    if "ws_api" in _m:
        assert_testnet_url(_m["ws_api"])
