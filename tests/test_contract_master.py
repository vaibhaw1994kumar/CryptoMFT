from datetime import datetime, timezone
import csv
import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.contract_master import dump_contract_master


class FakeClient:
    def get_json(self, url, params=None):
        if "fundingInfo" in url:
            symbol = "BTCUSD_PERP" if "dapi" in url else "BTCUSDT"
            return [{
                "symbol": symbol,
                "adjustedFundingRateCap": "0.025",
                "adjustedFundingRateFloor": "-0.025",
                "fundingIntervalHours": 8,
            }]
        if "premiumIndex" in url:
            symbol = "BTCUSD_PERP" if "dapi" in url else "BTCUSDT"
            return [{
                "symbol": symbol, "lastFundingRate": "0.0001",
                "nextFundingTime": 1790000000000, "time": 1789990000000,
            }]
        if "fapi" in url:
            return {"symbols": [{
                "symbol": "BTCUSDT", "pair": "BTCUSDT", "contractType": "PERPETUAL",
                "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDT",
                "marginAsset": "USDT", "onboardDate": 1, "deliveryDate": 0,
                "pricePrecision": 2, "quantityPrecision": 3,
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                    {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "100", "stepSize": "0.001"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                ],
            }]}
        if "dapi" in url:
            return {"symbols": [{
                "symbol": "BTCUSD_PERP", "pair": "BTCUSD", "contractType": "PERPETUAL",
                "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USD",
                "marginAsset": "BTC", "contractSize": 100,
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                    {"filterType": "LOT_SIZE", "minQty": "1", "maxQty": "100000", "stepSize": "1"},
                ],
            }]}
        if "eapi" in url:
            return {"optionSymbols": [{
                "symbol": "BTC-260925-100000-C", "underlying": "BTCUSDT",
                "quoteAsset": "USDT", "side": "CALL", "strikePrice": "100000",
                "expiryDate": 1790294400000, "status": "TRADING", "unit": "0.01",
                "makerFeeRate": "0.0002", "takerFeeRate": "0.0003",
                "initialMargin": "0.15", "maintenanceMargin": "0.075",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                    {"filterType": "LOT_SIZE", "minQty": "0.01", "maxQty": "100", "stepSize": "0.01"},
                ],
            }]}
        category = params["category"]
        if "tickers" in url:
            return {
                "retCode": 0, "retMsg": "OK",
                "result": {"list": [{
                    "symbol": f"BTC{category.upper()}",
                    "fundingRate": "0.0002", "nextFundingTime": "1790000000000",
                }]},
            }
        item = {
            "symbol": f"BTC{category.upper()}", "contractType": "LinearPerpetual",
            "status": "Trading", "baseCoin": "BTC", "quoteCoin": "USDT",
            "settleCoin": "USDT", "launchTime": "1", "deliveryTime": "0",
            "priceScale": "2", "priceFilter": {"tickSize": "0.10"},
            "fundingInterval": 480, "upperFundingRate": "0.02",
            "lowerFundingRate": "-0.02",
            "leverageFilter": {"minLeverage": "1", "maxLeverage": "100", "leverageStep": "0.01"},
            "lotSizeFilter": {"minOrderQty": "0.001", "maxOrderQty": "100", "qtyStep": "0.001", "minNotionalValue": "5"},
        }
        if category == "option":
            item.update({"optionsType": "Call", "deliveryTime": "1790294400000"})
        return {"retCode": 0, "retMsg": "OK", "result": {"list": [item], "nextPageCursor": ""}}


class ContractMasterTests(unittest.TestCase):
    def test_derivatives_dump_is_normalized_and_published(self):
        fixed = datetime(2026, 9, 4, 9, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            manifest = dump_contract_master(
                output, client=FakeClient(), now=lambda: fixed
            )
            self.assertEqual(manifest["row_count"], 6)
            latest = output / "contract_master_binance_bybit_latest.csv"
            self.assertTrue(latest.exists())
            with latest.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 6)
            self.assertEqual({row["exchange"] for row in rows}, {"BINANCE", "BYBIT"})
            inverse = next(row for row in rows if row["native_symbol"] == "BTCUSD_PERP")
            self.assertEqual(inverse["contract_multiplier"], "100")
            self.assertEqual(inverse["is_inverse"], "true")
            self.assertEqual(inverse["current_funding_rate"], "0.0001")
            bybit_linear = next(row for row in rows if row["market_category"] == "linear")
            self.assertEqual(bybit_linear["funding_interval_minutes"], "480")
            self.assertEqual(bybit_linear["max_leverage"], "100")
            option = next(row for row in rows if row["market_category"] == "options")
            self.assertEqual(option["maker_fee_rate"], "0.0002")
            self.assertEqual(option["initial_margin_rate"], "0.15")
            self.assertTrue((output / "raw" / "20260904T093000Z").is_dir())

    def test_strict_mode_does_not_publish_incomplete_csv(self):
        class BrokenClient:
            def get_json(self, url, params=None):
                raise RuntimeError("offline")

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with self.assertRaises(RuntimeError):
                dump_contract_master(output, client=BrokenClient())
            self.assertFalse((output / "contract_master_binance_bybit_latest.csv").exists())


if __name__ == "__main__":
    unittest.main()
