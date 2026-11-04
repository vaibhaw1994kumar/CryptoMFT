"""Request signing (copied in shape from FAST_MM/bybit_private_execution.py and trading_platform_poc/binance_execution.py)."""
from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlencode


def bybit_rest_signature(secret: str, timestamp_ms: str, api_key: str, recv_window: str, payload: str) -> str:
    """payload = JSON body (POST) or the exact query string (GET)."""
    return hmac.new(secret.encode(), (timestamp_ms + api_key + recv_window + payload).encode(), hashlib.sha256).hexdigest()


def bybit_ws_auth_signature(secret: str, expires_ms: int) -> str:
    return hmac.new(secret.encode(), f"GET/realtime{expires_ms}".encode(), hashlib.sha256).hexdigest()


def binance_signature(secret: str, query: str) -> str:
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


def binance_signed_query(secret: str, params: dict) -> str:
    query = urlencode([(k, str(v)) for k, v in params.items()])
    return f"{query}&signature={binance_signature(secret, query)}"


def binance_ws_api_signature(secret: str, params: dict) -> str:
    """WebSocket API signing: every param except `signature`, sorted by name, as name=value joined by '&' (no URL encoding)."""
    payload = "&".join(f"{k}={params[k]}" for k in sorted(params) if k != "signature")
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
