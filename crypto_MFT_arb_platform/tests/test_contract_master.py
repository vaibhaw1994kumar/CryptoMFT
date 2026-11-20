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


def test_perp_envelope_filters_read_the_venue_maxima_and_the_bybit_tiers(tmp_path):
    """Phase 2 F02 a (owner 20 Nov): the size-envelope filters from one public dump (constructed here in the documented shapes); a symbol not trading on a venue is None there, nothing defaulted."""
    import json
    from crypto_stat_arb.contract_master import perp_envelope_filters
    (tmp_path / "binance_usd_m_futures.json").write_text(json.dumps({"symbols": [{"symbol": "SOLUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING", "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.0100"}, {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "1000000"},
        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "80000"}, {"filterType": "MIN_NOTIONAL", "notional": "5"}, {"filterType": "MAX_NUM_ORDERS", "limit": 200}]}]}))
    (tmp_path / "bybit_linear.json").write_text(json.dumps([{"result": {"list": [
        {"symbol": "SOLUSDT", "quoteCoin": "USDT", "contractType": "LinearPerpetual", "status": "Trading", "priceFilter": {"tickSize": "0.010"}, "leverageFilter": {"maxLeverage": "100.00"},
         "lotSizeFilter": {"qtyStep": "0.1", "minOrderQty": "0.1", "maxOrderQty": "96000.0", "postOnlyMaxOrderQty": "96000.0", "maxMktOrderQty": "12000.0", "minNotionalValue": "5"}},
        {"symbol": "MNTUSDT", "quoteCoin": "USDT", "contractType": "LinearPerpetual", "status": "Trading", "priceFilter": {"tickSize": "0.0001"}, "leverageFilter": {"maxLeverage": "25"},
         "lotSizeFilter": {"qtyStep": "0.1", "minOrderQty": "0.1", "maxOrderQty": "250000", "postOnlyMaxOrderQty": "250000", "maxMktOrderQty": "50000", "minNotionalValue": "5"}}]}}]))
    (tmp_path / "bybit_linear_risk_limit.json").write_text(json.dumps([{"result": {"list": [
        {"id": 282, "symbol": "SOLUSDT", "riskLimitValue": "100000", "maintenanceMargin": "0.0056", "initialMargin": "0.0111", "mmDeduction": "30", "maxLeverage": "90.00"},
        {"id": 281, "symbol": "SOLUSDT", "riskLimitValue": "50000", "maintenanceMargin": "0.005", "initialMargin": "0.01", "mmDeduction": "", "maxLeverage": "100.00"}]}}]))
    out = perp_envelope_filters(tmp_path, ["SOLUSDT", "MNTUSDT", "XYZUSDT"])
    sol = out["SOLUSDT"]
    assert (sol["BINANCE"]["max_market_qty"], sol["BINANCE"]["max_limit_qty"], sol["BYBIT"]["max_market_qty"], sol["BYBIT"]["max_postonly_qty"]) == ("80000", "1000000", "12000.0", "96000.0")
    assert [t["id"] for t in sol["bybit_risk_tiers"]] == [281, 282] and sol["bybit_risk_tiers"][0]["mm_deduction"] == "0"
    assert out["MNTUSDT"]["BINANCE"] is None and out["MNTUSDT"]["BYBIT"]["step"] == "0.1"
    assert out["XYZUSDT"] == {"BINANCE": None, "BYBIT": None, "bybit_risk_tiers": None}
