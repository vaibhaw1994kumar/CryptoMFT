"""7b: perpetuals are polled on their own endpoints, so settling or delisting is seen."""

from decimal import Decimal
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from crypto_stat_arb import cli

from crypto_stat_arb.config import load_config
from crypto_stat_arb.instrument_status import InstrumentStatusPoller

ROOT = Path(__file__).parents[1]
PERP = "BTC-USDT-PERP"
SPOT = "BTC-USDT-SPOT"


def binance_futures(status="TRADING", tick="0.10", min_notional="100"):
    return {"symbols": [
        {"symbol": "ETHUSDT", "status": "TRADING", "filters": []},
        {"symbol": "BTCUSDT", "status": status, "contractType": "PERPETUAL", "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": tick},
            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
            {"filterType": "MIN_NOTIONAL", "notional": min_notional},
        ]},
    ]}


def bybit_linear(status="Trading"):
    return {"retCode": 0, "result": {"category": "linear", "list": [{
        "symbol": "BTCUSDT", "status": status, "contractType": "LinearPerpetual",
        "priceFilter": {"tickSize": "0.10"},
        "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "minNotionalValue": "5"},
    }]}}


class Endpoints:
    def __init__(self, binance=None, bybit=None):
        self.binance, self.bybit, self.urls = binance or binance_futures(), bybit or bybit_linear(), []

    def __call__(self, url):
        self.urls.append(url)
        return self.binance if "binance" in url else self.bybit


class PerpetualStatusPollTests(unittest.TestCase):
    def poller(self, endpoints):
        cfg, _ = load_config(ROOT / "config/perp_research.json")
        poller = InstrumentStatusPoller(cfg.instruments, {"BINANCE": (PERP, "BTCUSDT"), "BYBIT": (PERP, "BTCUSDT")},
                                        http_get=endpoints)
        return cfg, poller

    def test_perpetuals_use_the_futures_and_linear_endpoints(self):
        endpoints = Endpoints()
        _, poller = self.poller(endpoints)
        fetched = poller.fetch()
        self.assertEqual(endpoints.urls, ["https://fapi.binance.com/fapi/v1/exchangeInfo",
                                          "https://api.bybit.com/v5/market/instruments-info?category=linear&symbol=BTCUSDT"])
        self.assertEqual(fetched["BINANCE"]["min_notional"], "100")       # USD-M MIN_NOTIONAL "notional"
        self.assertEqual(fetched["BYBIT"]["quantity_step"], "0.001")      # linear qtyStep
        self.assertEqual({row["status"] for row in fetched.values()}, {"TRADING"})

    def test_a_settling_or_delivering_contract_changes_the_instrument_status(self):
        cfg, poller = self.poller(Endpoints(binance_futures(status="SETTLING"), bybit_linear(status="Delivering")))
        changes = poller.poll(5_000)
        self.assertEqual(sorted(change["venue"] for change in changes), ["BINANCE", "BYBIT"])
        self.assertEqual(cfg.instruments.get("BINANCE", PERP, 5_000).status, "SETTLING")
        self.assertEqual(cfg.instruments.get("BYBIT", PERP, 5_000).status, "DELIVERING")
        self.assertEqual(cfg.instruments.get("BINANCE", PERP, 4_999).status, "TRADING")     # history kept

    def test_a_rule_change_on_the_futures_endpoint_is_applied(self):
        cfg, poller = self.poller(Endpoints(binance_futures(tick="0.20", min_notional="50")))
        poller.poll(5_000)
        spec = cfg.instruments.get("BINANCE", PERP, 5_000)
        self.assertEqual((spec.tick_size, spec.min_notional), (Decimal("0.20"), Decimal("50")))

    def test_a_contract_missing_from_exchange_info_is_a_poll_failure(self):
        _, poller = self.poller(Endpoints(binance={"symbols": [{"symbol": "ETHUSDT", "status": "TRADING"}]}))
        with self.assertRaisesRegex(ValueError, "does not list BTCUSDT"):
            poller.fetch()

    def test_spot_instruments_still_use_the_spot_endpoints(self):
        cfg, _ = load_config(ROOT / "config/research.json")
        urls = []

        def http_get(url):
            urls.append(url)
            if "binance" in url:
                return {"symbols": [{"status": "TRADING", "filters": []}]}
            return {"retCode": 0, "result": {"list": [{"status": "Trading", "priceFilter": {}, "lotSizeFilter": {}}]}}

        InstrumentStatusPoller(cfg.instruments, {"BINANCE": (SPOT, "BTCUSDT"), "BYBIT": (SPOT, "BTCUSDT")},
                               http_get=http_get).fetch()
        self.assertEqual(urls, ["https://api.binance.com/api/v3/exchangeInfo?symbol=BTCUSDT",
                                "https://api.bybit.com/v5/market/instruments-info?category=spot&symbol=BTCUSDT"])


class LivePaperCliTests(unittest.TestCase):
    def paper_session(self, *extra):
        captured = {}

        async def capture(service, runner):
            captured["paper"] = runner.__self__          # the LivePaperSession, not run
            return {}

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(cli, "_run_with_signals", capture), \
                mock.patch.object(cli, "_print_live_result", lambda result: None), \
                mock.patch.object(sys, "argv", ["cli", "live-paper", "--config", str(ROOT / "config/perp_research.json"),
                                                "--output-dir", directory, "--binance-market", "linear",
                                                "--bybit-market", "linear", *extra]):
            cli.main()
            paper = captured["paper"]
            # Never run: release what the session opened so Windows can remove the directory.
            paper._timestamps.close()
            paper.service.recorder.abort()
            paper.service.ohlc_writer.close()
            paper.service.ltp_writer.close()
        return paper

    def test_perpetual_paper_runs_poll_instrument_status(self):
        poller = self.paper_session().status_poller
        self.assertIsNotNone(poller)                    # used to be spot-only
        self.assertEqual(poller.symbols, {"BINANCE": (PERP, "BTCUSDT"), "BYBIT": (PERP, "BTCUSDT")})

    def test_polling_can_still_be_disabled(self):
        self.assertIsNone(getattr(self.paper_session("--status-poll-seconds", "0"), "status_poller", None))


if __name__ == "__main__":
    unittest.main()
