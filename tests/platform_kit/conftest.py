"""Shared pytest fixtures.

The default suite must be hermetic: exchange venues such as Binance reject
requests from restricted regions, so any test that reaches a live endpoint
passes or fails depending on where it runs. Outbound sockets are therefore
blocked for every test except the credential-gated ``binance_testnet`` smoke
tests, which are explicitly opt-in.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", ""})
_LIVE_NETWORK_MARKERS = frozenset({"binance_testnet"})


class OutboundNetworkBlocked(RuntimeError):
    """Raised when a hermetic test attempts to reach a remote host."""


def _is_loopback(host: object) -> bool:
    return host is None or host in _LOOPBACK_HOSTS


@pytest.fixture(autouse=True)
def block_outbound_network(request: pytest.FixtureRequest) -> Iterator[None]:
    if any(
        request.node.get_closest_marker(marker)
        for marker in _LIVE_NETWORK_MARKERS
    ):
        yield
        return

    real_getaddrinfo = socket.getaddrinfo
    real_create_connection = socket.create_connection

    def guarded_getaddrinfo(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        if not _is_loopback(host):
            raise OutboundNetworkBlocked(
                f"test attempted to resolve remote host {host!r}; "
                "inject a fake adapter or mark the test with "
                "@pytest.mark.binance_testnet"
            )
        return real_getaddrinfo(host, *args, **kwargs)

    def guarded_create_connection(address, *args, **kwargs):  # type: ignore[no-untyped-def]
        host = address[0] if isinstance(address, tuple) else address
        if not _is_loopback(host):
            raise OutboundNetworkBlocked(
                f"test attempted to connect to remote host {host!r}; "
                "inject a fake adapter or mark the test with "
                "@pytest.mark.binance_testnet"
            )
        return real_create_connection(address, *args, **kwargs)

    socket.getaddrinfo = guarded_getaddrinfo  # type: ignore[assignment]
    socket.create_connection = guarded_create_connection  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.getaddrinfo = real_getaddrinfo  # type: ignore[assignment]
        socket.create_connection = real_create_connection  # type: ignore[assignment]
