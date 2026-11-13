"""Owner 13 Nov, edge calibration item 2: at live/paper start-up the account's own maker/taker rates are read on both venues and compared, exactly, with the rates the profile books; a difference, a missing reader or a failed read
refuses the start. Responses are hand-written in each venue's documented shape (SYNTHETIC, no account data, no network)."""
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from crypto_stat_arb.fees import AccountFeeMismatch, schedule_for, verify_account_fee_rates
from crypto_stat_arb.sdk import TestnetVenueAdapter
from crypto_stat_arb.testnet import BinanceFuturesTestnetTransport, BybitV5LinearTestnetTransport
from test_forward_testnet_calibration import FakeHttp, SECRET

PERP = "SOL-USDT-PERP"
NOW_MS = 1_700_000_100_000


class Reader:
    def __init__(self, maker, taker):
        self.rates = (maker, taker)

    def account_fee_rates(self, instrument_id):
        return self.rates


class Broken:
    def account_fee_rates(self, instrument_id):
        raise ConnectionError("venue unreachable")


def schedule():
    return schedule_for({"bybit": "bybit_perp_mm", "binance": "binance_perp_vip0"}, PERP)       # bybit -0.5 / 5.5 bps, binance 2.0 / 5.0 bps


class VerifyRates(unittest.TestCase):
    def test_equal_rates_pass_and_are_compared_in_decimal_bps(self):
        rows = verify_account_fee_rates(schedule(), PERP, {"BYBIT": Reader("-0.00005", "0.00055"), "BINANCE": Reader("0.0002", "0.0005")})
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(r["equal"] for r in rows))

    def test_a_difference_refuses_and_names_every_one(self):
        with self.assertRaises(AccountFeeMismatch) as ctx:
            verify_account_fee_rates(schedule(), PERP, {"BYBIT": Reader("0.0001", "0.00055"), "BINANCE": Reader("0.0002", "0.00032")})
        text = str(ctx.exception)
        self.assertIn("BYBIT MAKER", text); self.assertIn("BINANCE TAKER", text)
        self.assertNotIn("BYBIT TAKER", text); self.assertNotIn("BINANCE MAKER", text)

    def test_the_smallest_difference_is_a_difference(self):
        with self.assertRaises(AccountFeeMismatch):
            verify_account_fee_rates(schedule(), PERP, {"BYBIT": Reader("-0.00005", "0.00055"), "BINANCE": Reader("0.00020001", "0.0005")})

    def test_a_missing_reader_and_a_failed_read_both_refuse(self):
        with self.assertRaisesRegex(AccountFeeMismatch, "no account fee reader"):
            verify_account_fee_rates(schedule(), PERP, {"BYBIT": Reader("-0.00005", "0.00055")})
        with self.assertRaisesRegex(AccountFeeMismatch, "could not be read"):
            verify_account_fee_rates(schedule(), PERP, {"BYBIT": Broken(), "BINANCE": Reader("0.0002", "0.0005")})

    def test_the_public_schedule_headline_presets_are_refused_against_a_vip0_account(self):
        headline = schedule_for({"bybit": "bybit_perp_vip5", "binance": "binance_perp_vip9"}, PERP)       # the v1_9 headline: PUBLIC_SCHEDULE_UNVERIFIED
        with self.assertRaises(AccountFeeMismatch):
            verify_account_fee_rates(headline, PERP, {"BYBIT": Reader("0.0002", "0.00055"), "BINANCE": Reader("0.0002", "0.0005")})
        rows = verify_account_fee_rates(headline, PERP, {"BYBIT": Reader("0.0001", "0.00032"), "BINANCE": Reader("0", "0.00017")})
        self.assertTrue(all(r["equal"] for r in rows))


class Transports(unittest.TestCase):
    def adapter(self, transport):
        return TestnetVenueAdapter(transport.venue, lambda req: transport(req, SECRET))

    def test_binance_reads_commission_rate_with_a_signed_get_and_returns_decimal_fractions(self):
        http = FakeHttp([(200, {"symbol": "SOLUSDT", "makerCommissionRate": "0.000200", "takerCommissionRate": "0.000500"})])
        transport = BinanceFuturesTestnetTransport(symbols={PERP: "SOLUSDT"}, http=http, clock_ms=lambda: NOW_MS)
        maker, taker = self.adapter(transport).account_fee_rates(PERP)
        self.assertEqual((maker, taker), (Decimal("0.0002"), Decimal("0.0005")))
        self.assertEqual((http.calls[0][0], urlparse(http.calls[0][1]).path, parse_qs(urlparse(http.calls[0][1]).query)["symbol"]), ("GET", "/fapi/v1/commissionRate", ["SOLUSDT"]))
        self.assertIn("X-MBX-APIKEY", http.calls[0][2])

    def test_binance_an_unexpected_shape_or_status_raises(self):
        for response in ((200, {"symbol": "BTCUSDT", "makerCommissionRate": "0.0002", "takerCommissionRate": "0.0005"}), (200, {"symbol": "SOLUSDT"}), (400, {"code": -1, "msg": "bad"})):
            transport = BinanceFuturesTestnetTransport(symbols={PERP: "SOLUSDT"}, http=FakeHttp([response]), clock_ms=lambda: NOW_MS)
            with self.assertRaises(ConnectionError):
                self.adapter(transport).account_fee_rates(PERP)

    def test_bybit_reads_the_symbol_row_of_the_fee_rate_list(self):
        http = FakeHttp([(200, {"retCode": 0, "result": {"list": [{"symbol": "SOLUSDT", "takerFeeRate": "0.00055", "makerFeeRate": "-0.00005"}]}})])
        transport = BybitV5LinearTestnetTransport(symbols={PERP: "SOLUSDT"}, http=http, clock_ms=lambda: NOW_MS)
        maker, taker = self.adapter(transport).account_fee_rates(PERP)
        self.assertEqual((maker, taker), (Decimal("-0.00005"), Decimal("0.00055")))
        query = parse_qs(urlparse(http.calls[0][1]).query)
        self.assertEqual((urlparse(http.calls[0][1]).path, query["category"], query["symbol"]), ("/v5/account/fee-rate", ["linear"], ["SOLUSDT"]))

    def test_bybit_no_single_row_an_error_or_a_rate_that_is_not_a_fraction_raises(self):
        bad = [(200, {"retCode": 0, "result": {"list": []}}),
               (200, {"retCode": 0, "result": {"list": [{"symbol": "SOLUSDT", "takerFeeRate": "0.00055", "makerFeeRate": "0.00005"}, {"symbol": "SOLUSDT", "takerFeeRate": "0.00055", "makerFeeRate": "0.00005"}]}}),
               (200, {"retCode": 10005, "retMsg": "Permission denied", "result": {}}),
               (200, {"retCode": 0, "result": {"list": [{"symbol": "SOLUSDT", "takerFeeRate": "5.5", "makerFeeRate": "0.5"}]}})]       # percent, not a fraction
        for response in bad:
            transport = BybitV5LinearTestnetTransport(symbols={PERP: "SOLUSDT"}, http=FakeHttp([response]), clock_ms=lambda: NOW_MS)
            with self.assertRaises(ConnectionError):
                self.adapter(transport).account_fee_rates(PERP)

    def test_the_read_asks_for_the_read_permission_never_the_trade_one(self):
        from crypto_stat_arb.credentials import CredentialPermission, CredentialedTestnetTransport

        class Broker:
            def inject(self, credential_id, **kw):
                self.permissions = kw["required_permissions"]
                return ({"event_type": "ACCOUNT_FEE_RATE", "maker_rate": "0", "taker_rate": "0"},)
        broker = Broker()
        CredentialedTestnetTransport(broker, "cred", "BYBIT", lambda *a: ())({"action": "ACCOUNT_FEE_RATE", "instrument_id": PERP})
        self.assertEqual(broker.permissions, (CredentialPermission.READ_ACCOUNT,))

    def test_a_spot_transport_has_no_fee_read_and_says_so(self):
        from crypto_stat_arb.testnet import BybitV5TestnetTransport
        transport = BybitV5TestnetTransport(symbols={PERP: "SOLUSDT"}, http=FakeHttp([]), clock_ms=lambda: NOW_MS)
        with self.assertRaisesRegex(ValueError, "perpetual transports only"):
            transport({"action": "ACCOUNT_FEE_RATE", "instrument_id": PERP}, SECRET)


class Requirement(unittest.TestCase):
    def test_v1_9_declares_the_check_and_older_profiles_are_told_it_is_missing(self):
        from crypto_stat_arb.config import check_real_order_profile
        from new_backtest.profiles import load_profile
        self.assertEqual(check_real_order_profile(load_profile("perp_perp_maker_taker_v1_9")), [])
        self.assertTrue(any("account_fee_check" in m for m in check_real_order_profile(load_profile("perp_perp_maker_taker_v1_8"))))


class StartUp(unittest.TestCase):
    """LivePaperSession refuses to start before it builds anything when the account's rates differ, cannot be read, or the profile requires the check and no reader was given."""
    CONFIG = Path(__file__).resolve().parents[1] / "config/perp_research.json"

    def make(self, **kw):
        import tempfile
        from crypto_stat_arb.live_feed import LiveFeedSettings
        from crypto_stat_arb.paper_live import LivePaperSession
        settings = LiveFeedSettings(udp_enabled=False, binance_market="linear", bybit_market="linear")
        with tempfile.TemporaryDirectory() as d:
            return LivePaperSession(self.CONFIG, Path(d), "fee-check", settings, **kw)

    def test_a_mismatching_account_refuses_the_start(self):
        from crypto_stat_arb.config import load_config
        cfg, _ = load_config(self.CONFIG)
        readers = {v: Reader("0.9", "0.9") for v in cfg.venues}                 # nothing like any profile rate
        with self.assertRaises(AccountFeeMismatch):
            self.make(fee_readers=readers)

    def test_a_required_check_with_no_reader_refuses_the_start(self):
        with self.assertRaisesRegex(AccountFeeMismatch, "no account fee reader"):
            self.make(require_account_fees=True)

    def test_an_unreadable_account_refuses_the_start(self):
        from crypto_stat_arb.config import load_config
        cfg, _ = load_config(self.CONFIG)
        with self.assertRaisesRegex(AccountFeeMismatch, "could not be read"):
            self.make(fee_readers={v: Broken() for v in cfg.venues})


if __name__ == "__main__":
    unittest.main()
