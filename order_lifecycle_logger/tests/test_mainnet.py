"""Mainnet is opt-in and fenced: typed confirmation, env flag, perp markets and two symbols only, hard ceilings, own hosts and keys."""
import argparse
import asyncio
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ollog import endpoints, fillprobe, mainnet, venues  # noqa: E402

OK = dict(markets=["bybit_linear", "binance_futures"], symbols=["BTCUSDT", "SOLUSDT"], max_orders=100, max_notional=Decimal(120),
          max_net_notional=Decimal(120), confirm=mainnet.CONFIRM_PHRASE, env_flag="1")


def test_a_fully_specified_request_inside_the_limits_is_allowed():
    assert mainnet.violations(**OK) == []


@pytest.mark.parametrize("change,fragment", [
    ({"confirm": None}, "--confirm-mainnet"), ({"confirm": "yes"}, "--confirm-mainnet"), ({"env_flag": None}, "OLLOG_MAINNET"),
    ({"env_flag": "0"}, "OLLOG_MAINNET"), ({"markets": ["bybit_spot"]}, "not available on mainnet"), ({"symbols": ["BTCUSDT", "DOGEUSDT"]}, "allow-list"),
    ({"max_orders": 201}, "max-orders"), ({"max_notional": Decimal(151)}, "max-notional"), ({"max_net_notional": Decimal(151)}, "max-net-notional"),
])
def test_each_condition_alone_refuses_the_run(change, fragment):
    v = mainnet.violations(**{**OK, **change})
    assert v and any(fragment in x for x in v)


def test_mainnet_guard_accepts_only_exact_mainnet_hosts_and_the_testnet_guard_refuses_them():
    for u in ("https://api.bybit.com/v5/order/create", "wss://stream.bybit.com/v5/private", "https://fapi.binance.com/fapi/v1/order", "wss://fstream.binance.com/ws/x"):
        mainnet.assert_mainnet_url(u)
        with pytest.raises(endpoints.NotTestnet):
            endpoints.assert_testnet_url(u)
    for u in ("https://api-testnet.bybit.com/v5/order/create", "https://api.bybit.com.evil.com/x", "http://api.bybit.com/x", "https://api.bybit.com:8443/x",
              "https://user@api.bybit.com/x", "https://api.binance.com/api/v3/order", "wss://stream.binance.com:9443/ws/x"):
        with pytest.raises(mainnet.NotMainnetAllowed):
            mainnet.assert_mainnet_url(u)


def test_mainnet_credentials_never_reuse_the_testnet_variable_names():
    mn = {n for m in mainnet.MAINNET_MARKETS.values() for n in m["env"]}
    tn = {n for m in endpoints.MARKETS.values() for n in m["env"]}
    assert not (mn & tn) and all("MAINNET" in n for n in mn)


class Sess:
    def __init__(self):
        self.calls = []

    def request(self, method, url, headers=None, data=None):
        self.calls.append(url)
        raise RuntimeError("no network in tests")


def test_clients_default_to_testnet_and_mainnet_clients_use_the_mainnet_guard():
    s = Sess()
    t = venues.make_client("bybit_linear", s, "K", "S")
    assert t.network == "testnet" and t.cfg["rest"] == "https://api-testnet.bybit.com"
    m = venues.make_client("bybit_linear", s, "K", "S", network="mainnet")
    assert m.network == "mainnet" and m.cfg["rest"] == "https://api.bybit.com"
    with pytest.raises(endpoints.NotTestnet):
        asyncio.run(t._http("GET", "https://api.bybit.com/v5/market/time"))              # a testnet client cannot reach mainnet
    with pytest.raises(mainnet.NotMainnetAllowed):
        asyncio.run(m._http("GET", "https://api-testnet.bybit.com/v5/market/time"))      # and vice versa
    assert s.calls == []
    with pytest.raises(ValueError):
        venues.make_client("bybit_linear", s, network="staging")


def _args(**kw):
    base = dict(network="mainnet", markets="bybit_linear,binance_futures", coins_list="BTCUSDT", capture_only=False, duration_s=1, max_orders=10, hold_min_s=1,
                hold_max_s=2, spacing_s=1, max_notional=Decimal(100), max_net_notional=Decimal(100), no_flatten=False, check_every_s=0, max_consecutive_errors=5,
                seed=1, confirm_mainnet=None, out="/nonexistent")
    base.update(kw)
    return argparse.Namespace(**base)


def test_mainnet_order_run_without_confirmation_exits_before_any_network_object_exists(monkeypatch, tmp_path, capsys):
    import aiohttp

    def boom(*a, **k):
        raise AssertionError("a session was created: something could have been sent")
    monkeypatch.setattr(aiohttp, "ClientSession", boom)
    monkeypatch.delenv("OLLOG_MAINNET", raising=False)
    assert asyncio.run(fillprobe.run_fillprobe(_args(out=str(tmp_path)))) == 2
    assert "refused (nothing was sent)" in capsys.readouterr().err and not list(tmp_path.iterdir())


def test_mainnet_run_with_confirmation_but_no_mainnet_keys_still_sends_nothing(monkeypatch, tmp_path, capsys):
    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: (_ for _ in ()).throw(AssertionError("session created")))
    monkeypatch.setenv("OLLOG_MAINNET", "1")
    for n in ("BYBIT_MAINNET_KEY", "BYBIT_MAINNET_SECRET", "BINANCE_FUT_MAINNET_KEY", "BINANCE_FUT_MAINNET_SECRET"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("BYBIT_TESTNET_KEY", "testnet-key-must-not-be-used")
    rc = asyncio.run(fillprobe.run_fillprobe(_args(confirm_mainnet=mainnet.CONFIRM_PHRASE, out=str(tmp_path))))
    assert rc == 2 and "missing mainnet credentials" in capsys.readouterr().err


class _Resp:
    status = 200

    def __init__(self, text):
        self._t = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return self._t


class ListenKeySession:
    def request(self, method, url, headers=None, data=None):
        return _Resp('{"listenKey":"abc"}')


class _WS:
    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(3600)

    async def close(self):
        pass


def _stream_url(network, variant):
    urls = []

    async def connect(u):
        urls.append(u)
        return _WS()

    async def go():
        c = venues.make_client("binance_futures", ListenKeySession(), "K", "S", ws_connect=connect, network=network)
        c.stream_variant = variant
        await c.start_stream(lambda e: None)
        await c.close()
        return c.stream_variants
    n = asyncio.run(go())
    return urls[0], n


def test_binance_futures_private_stream_urls_by_network_and_variant():
    assert _stream_url("testnet", 0) == ("wss://fstream.binancefuture.com/ws/abc", 1)                   # testnet is unchanged
    assert _stream_url("mainnet", 0) == ("wss://fstream.binance.com/private/ws?listenKey=abc", 2)       # new path first
    assert _stream_url("mainnet", 1) == ("wss://fstream.binance.com/ws/abc", 2)                         # legacy path as the fallback
