"""Owner 16 Nov, Order 3: the start-up halves of the session limits. A3 the clock-offset source (server-time probe) and the real-order refusal without a source; A6 the account's own risk tiers read at start-up; A8 the
account check (one-way mode, margin mode, leverage, reduce-only derived). Every response is hand-written in the venue's documented shape (SYNTHETIC recorded messages, no account data, no network). The endpoint shapes
for the tier reads come from the public documentation and are UNVERIFIED against the live endpoints: the testnet drill confirms them."""
import asyncio
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from crypto_stat_arb.margin import AccountTierReadError, apply_account_tiers, read_account_tiers
from crypto_stat_arb.sdk import TestnetVenueAdapter
from crypto_stat_arb.testnet import (AccountVerificationError, BinanceFuturesTestnetTransport, BybitV5LinearTestnetTransport, REDUCE_ONLY_NOTE, risk_tier_rows_binance, risk_tier_rows_bybit,
                                     verify_accounts_for_start)
from crypto_stat_arb.time_integrity import parse_server_time_ns, probe_server_clock
from test_forward_testnet_calibration import FakeHttp, SECRET

PERP = "SOL-USDT-PERP"
NOW_MS = 1_700_000_100_000

BINANCE_BRACKETS = [{"symbol": "SOLUSDT", "notionalCoef": 1.0, "brackets": [
    {"bracket": 2, "initialLeverage": 50, "notionalCap": 50000, "notionalFloor": 10000, "maintMarginRatio": 0.01, "cum": 35.0},
    {"bracket": 1, "initialLeverage": 75, "notionalCap": 10000, "notionalFloor": 0, "maintMarginRatio": 0.0065, "cum": 0.0}]}]      # out of order on purpose
BYBIT_LIMITS = {"retCode": 0, "retMsg": "OK", "result": {"category": "linear", "list": [
    {"id": 1, "symbol": "SOLUSDT", "riskLimitValue": "200000", "maintenanceMargin": "0.005", "initialMargin": "0.01", "isLowestRisk": 1, "maxLeverage": "100.00", "mmDeduction": ""},
    {"id": 2, "symbol": "SOLUSDT", "riskLimitValue": "400000", "maintenanceMargin": "0.01", "initialMargin": "0.02", "isLowestRisk": 0, "maxLeverage": "50.00", "mmDeduction": "1000"}]}}


def adapter(transport):
    return TestnetVenueAdapter(transport.venue, lambda req: transport(req, SECRET))


class TierReads(unittest.TestCase):
    def test_binance_brackets_become_ordered_tiers_with_initial_rate_one_over_leverage(self):
        http = FakeHttp([(200, BINANCE_BRACKETS)])
        rows = adapter(BinanceFuturesTestnetTransport(symbols={PERP: "SOLUSDT"}, http=http, clock_ms=lambda: NOW_MS)).account_risk_tiers(PERP)
        self.assertEqual([r["max_notional"] for r in rows], ["10000", "50000"])
        self.assertEqual(Decimal(rows[0]["initial_rate"]), Decimal(1) / Decimal(75))
        self.assertEqual((rows[1]["maintenance_rate"], rows[1]["maintenance_deduction"]), ("0.01", "35.0"))
        self.assertEqual((http.calls[0][0], urlparse(http.calls[0][1]).path, parse_qs(urlparse(http.calls[0][1]).query)["symbol"]), ("GET", "/fapi/v1/leverageBracket", ["SOLUSDT"]))

    def test_bybit_risk_limit_rows_become_tiers(self):
        http = FakeHttp([(200, BYBIT_LIMITS)])
        rows = adapter(BybitV5LinearTestnetTransport(symbols={PERP: "SOLUSDT"}, http=http, clock_ms=lambda: NOW_MS)).account_risk_tiers(PERP)
        self.assertEqual([(r["max_notional"], r["maintenance_rate"], r["initial_rate"], r["maintenance_deduction"]) for r in rows], [("200000", "0.005", "0.01", "0"), ("400000", "0.01", "0.02", "1000")])
        self.assertEqual(urlparse(http.calls[0][1]).path, "/v5/market/risk-limit")

    def test_an_error_or_an_unexpected_shape_raises(self):
        for bad in ([], [{"symbol": "BTCUSDT", "brackets": []}], [{"symbol": "SOLUSDT", "brackets": []}], {"msg": "x"}):
            with self.assertRaises(ConnectionError):
                risk_tier_rows_binance(bad, "SOLUSDT")
        for bad in ({"retCode": 10005, "retMsg": "denied"}, {"retCode": 0, "result": {"list": []}}, {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "riskLimitValue": "1"}]}}):
            with self.assertRaises(ConnectionError):
                risk_tier_rows_bybit(bad, "SOLUSDT")
        transport = BinanceFuturesTestnetTransport(symbols={PERP: "SOLUSDT"}, http=FakeHttp([(400, {"code": -1, "msg": "bad"})]), clock_ms=lambda: NOW_MS)
        with self.assertRaises(ConnectionError):
            adapter(transport).account_risk_tiers(PERP)

    def test_the_read_asks_for_the_read_permission_never_the_trade_one(self):
        from crypto_stat_arb.credentials import CredentialPermission, CredentialedTestnetTransport

        class Broker:
            def inject(self, credential_id, **kw):
                self.permissions = kw["required_permissions"]
                return ({"event_type": "ACCOUNT_RISK_TIERS", "tiers": []},)
        broker = Broker()
        CredentialedTestnetTransport(broker, "cred", "BYBIT", lambda *a: ())({"action": "ACCOUNT_RISK_TIERS", "instrument_id": PERP})
        self.assertEqual(broker.permissions, (CredentialPermission.READ_ACCOUNT,))

    def test_a_spot_transport_has_no_tier_read_and_says_so(self):
        from crypto_stat_arb.testnet import BybitV5TestnetTransport
        with self.assertRaisesRegex(ValueError, "perpetual transports only"):
            BybitV5TestnetTransport(symbols={PERP: "SOLUSDT"}, http=FakeHttp([]), clock_ms=lambda: NOW_MS)({"action": "ACCOUNT_RISK_TIERS", "instrument_id": PERP}, SECRET)

    def venue_readers(self, instrument_id=PERP):
        return {"BINANCE": adapter(BinanceFuturesTestnetTransport(symbols={instrument_id: "SOLUSDT"}, http=FakeHttp([(200, BINANCE_BRACKETS)]), clock_ms=lambda: NOW_MS)),
                "BYBIT": adapter(BybitV5LinearTestnetTransport(symbols={instrument_id: "SOLUSDT"}, http=FakeHttp([(200, BYBIT_LIMITS)]), clock_ms=lambda: NOW_MS))}

    def test_read_account_tiers_builds_schedules_sourced_account_read(self):
        tiers = read_account_tiers(self.venue_readers(), ("BINANCE", "BYBIT"), PERP)
        self.assertEqual(tiers["BINANCE"].source, "ACCOUNT_READ:BINANCE")
        self.assertEqual(tiers["BYBIT"].initial(Decimal("100000")), Decimal("1000"))                # 1% of 100000
        self.assertEqual(tiers["BINANCE"].maintenance(Decimal("20000")), Decimal("165"))            # 20000 x 0.01 - 35 (the deduction)

    def test_a_missing_reader_or_a_failed_read_refuses_and_names_every_venue(self):
        class Broken:
            def account_risk_tiers(self, instrument_id):
                raise ConnectionError("unreachable")
        with self.assertRaises(AccountTierReadError) as ctx:
            read_account_tiers({"BINANCE": Broken()}, ("BINANCE", "BYBIT"), PERP)
        self.assertIn("BINANCE", str(ctx.exception)); self.assertIn("BYBIT", str(ctx.exception))

    def test_the_margin_config_takes_the_account_tiers_in_place_of_the_assumed_ones(self):
        from crypto_stat_arb.config import load_config
        cfg, _ = load_config(Path(__file__).resolve().parents[1] / "config/perp_research.json")
        tiers = read_account_tiers(self.venue_readers(cfg.instrument_id), tuple(cfg.venues), cfg.instrument_id)
        out = apply_account_tiers(cfg.margin, tiers, cfg.instrument_id)
        for venue in cfg.venues:
            self.assertEqual(out.venues[venue].tiers[cfg.instrument_id].source, f"ACCOUNT_READ:{venue}")
        self.assertNotEqual(cfg.margin.venues[cfg.venues[0]].tiers[cfg.instrument_id].source, f"ACCOUNT_READ:{cfg.venues[0]}")       # the original is untouched


def binance_verify_http(dual=False, leverage="10", margin="cross"):
    return FakeHttp([(200, {"dualSidePosition": dual}), (200, [{"symbol": "SOLUSDT", "leverage": leverage, "marginType": margin, "positionAmt": "0"}])])


def bybit_verify_http(mode="REGULAR_MARGIN", leverage="10", idx=0):
    return FakeHttp([(200, {"retCode": 0, "result": {"marginMode": mode}}), (200, {"retCode": 0, "result": {"list": [{"symbol": "SOLUSDT", "positionIdx": idx, "leverage": leverage, "size": "0"}]}})])


class VerifyAccount(unittest.TestCase):
    def test_binance_one_way_cross_leverage_ten_passes_and_says_reduce_only_is_derived(self):
        facts = adapter(BinanceFuturesTestnetTransport(symbols={PERP: "SOLUSDT"}, http=binance_verify_http(), clock_ms=lambda: NOW_MS)).verify_account(PERP, "cross", 10)
        self.assertEqual((facts["position_mode"], facts["margin_mode"], facts["leverage"]), ("ONE_WAY", "CROSS", 10))
        self.assertEqual(facts["reduce_only_accepted"], REDUCE_ONLY_NOTE)
        self.assertIn("UNVERIFIED", facts["reduce_only_accepted"])

    def test_binance_hedge_mode_another_margin_type_or_another_leverage_each_refuse(self):
        for http, text in ((binance_verify_http(dual=True), "hedge mode"), (binance_verify_http(margin="isolated"), "margin type"), (binance_verify_http(leverage="20"), "leverage")):
            with self.assertRaisesRegex(ValueError, text):
                adapter(BinanceFuturesTestnetTransport(symbols={PERP: "SOLUSDT"}, http=http, clock_ms=lambda: NOW_MS)).verify_account(PERP, "cross", 10)

    def test_bybit_one_way_regular_margin_leverage_ten_passes(self):
        facts = adapter(BybitV5LinearTestnetTransport(symbols={PERP: "SOLUSDT"}, http=bybit_verify_http(), clock_ms=lambda: NOW_MS)).verify_account(PERP, "cross", 10)
        self.assertEqual((facts["position_mode"], facts["margin_mode"], facts["leverage"]), ("ONE_WAY", "REGULAR_MARGIN", 10))

    def test_bybit_isolated_margin_hedge_mode_or_another_leverage_each_refuse(self):
        for http, text in ((bybit_verify_http(mode="ISOLATED_MARGIN"), "margin mode"), (bybit_verify_http(idx=1), "hedge"), (bybit_verify_http(leverage="5"), "leverage")):
            with self.assertRaisesRegex(ValueError, text):
                adapter(BybitV5LinearTestnetTransport(symbols={PERP: "SOLUSDT"}, http=http, clock_ms=lambda: NOW_MS)).verify_account(PERP, "cross", 10)

    def test_a_row_without_leverage_cannot_be_verified_and_refuses(self):
        http = FakeHttp([(200, {"retCode": 0, "result": {"marginMode": "REGULAR_MARGIN"}}), (200, {"retCode": 0, "result": {"list": []}})])
        with self.assertRaises(ConnectionError):
            adapter(BybitV5LinearTestnetTransport(symbols={PERP: "SOLUSDT"}, http=http, clock_ms=lambda: NOW_MS)).verify_account(PERP, "cross", 10)

    def test_the_start_up_helper_collects_every_problem(self):
        good = adapter(BinanceFuturesTestnetTransport(symbols={PERP: "SOLUSDT"}, http=binance_verify_http(), clock_ms=lambda: NOW_MS))
        bad = adapter(BybitV5LinearTestnetTransport(symbols={PERP: "SOLUSDT"}, http=bybit_verify_http(mode="ISOLATED_MARGIN"), clock_ms=lambda: NOW_MS))
        with self.assertRaises(AccountVerificationError) as ctx:
            verify_accounts_for_start({"BINANCE": good, "BYBIT": bad}, ("BINANCE", "BYBIT", "OTHER"), PERP, margin_mode="cross", leverage=10)
        self.assertIn("BYBIT", str(ctx.exception)); self.assertIn("OTHER: no account verifier", str(ctx.exception)); self.assertNotIn("BINANCE:", str(ctx.exception))
        fresh = {"BINANCE": adapter(BinanceFuturesTestnetTransport(symbols={PERP: "SOLUSDT"}, http=binance_verify_http(), clock_ms=lambda: NOW_MS)),
                 "BYBIT": adapter(BybitV5LinearTestnetTransport(symbols={PERP: "SOLUSDT"}, http=bybit_verify_http(), clock_ms=lambda: NOW_MS))}
        rows = verify_accounts_for_start(fresh, ("BINANCE", "BYBIT"), PERP, margin_mode="cross", leverage=10)
        self.assertEqual([r["venue"] for r in rows], ["BINANCE", "BYBIT"])


class ClockProbe(unittest.IsolatedAsyncioTestCase):
    def test_both_venues_server_time_bodies_parse(self):
        self.assertEqual(parse_server_time_ns("BINANCE", '{"serverTime": 1700000000123}'), 1_700_000_000_123_000_000)
        self.assertEqual(parse_server_time_ns("BYBIT", '{"retCode":0,"result":{"timeSecond":"1700000000","timeNano":"1700000000123456789"},"time":1700000000123}'), 1_700_000_000_123_456_789)
        self.assertEqual(parse_server_time_ns("BYBIT", '{"retCode":0,"result":{},"time":1700000000123}'), 1_700_000_000_123_000_000)       # the millisecond fallback
        for venue, body in (("BINANCE", "{}"), ("BYBIT", '{"retCode":10002,"retMsg":"x"}'), ("OTHER", "{}")):
            with self.assertRaises((KeyError, ValueError)):
                parse_server_time_ns(venue, body)

    async def test_five_probes_the_lowest_round_trip_wins_and_a_failure_is_dropped(self):
        ticks = iter(range(0, 10_000_000_000, 1_000_000))
        delays = [8_000_000, 3_000_000, None, 6_000_000, 4_000_000]
        state = {"now": 0, "i": 0}

        def clock():
            return state["now"]

        async def getter(url, timeout):
            d = delays[state["i"]]
            state["i"] += 1
            if d is None:
                raise ConnectionError("down")
            state["now"] += d
            return '{"serverTime": %d}' % ((state["now"] - d // 2) // 1_000_000), state["now"]

        async def run():
            return await probe_server_clock(getter, "BINANCE", clock_ns=clock)
        m = await run()
        self.assertEqual(m.samples, 4)
        self.assertEqual(m.uncertainty_ns, 1_500_000)                                               # the 3 ms probe: half of it
        self.assertTrue(m.source.startswith("server_time:BINANCE"))

    async def test_no_valid_probe_gives_none(self):
        async def getter(url, timeout):
            raise ConnectionError("down")
        self.assertIsNone(await probe_server_clock(getter, "BYBIT"))


class StartUp(unittest.TestCase):
    """LivePaperSession with oms.session_limits: the account checks run when real orders can be sent (or readers are given), before anything starts."""
    CONFIG = Path(__file__).resolve().parents[1] / "config/perp_research.json"

    def make(self, limits, readers, **kw):
        import tempfile
        from crypto_stat_arb.live_feed import LiveFeedSettings
        from crypto_stat_arb.paper_live import LivePaperSession
        settings = LiveFeedSettings(udp_enabled=False, binance_market="linear", bybit_market="linear")
        with tempfile.TemporaryDirectory() as d, mock.patch("crypto_stat_arb.paper_live.verify_account_fee_rates", lambda *a, **k: []):       # the fee check has its own tests
            return LivePaperSession(self.CONFIG, Path(d), "limits-start", settings, fee_readers=readers, session_limits=limits, **kw)

    def readers(self, venues, **verify):
        out = {}
        for v in venues:
            transport = (BinanceFuturesTestnetTransport if v == "BINANCE" else BybitV5LinearTestnetTransport)(symbols={PERP: "SOLUSDT"}, http=FakeHttp([]), clock_ms=lambda: NOW_MS)
            reader = adapter(transport)
            out[v] = reader
        return out

    def test_a_wrong_account_refuses_the_start(self):
        from crypto_stat_arb.config import load_config
        cfg, _ = load_config(self.CONFIG)

        class Reader:
            def __init__(self, ok):
                self.ok = ok

            def verify_account(self, instrument_id, margin_mode, leverage):
                if not self.ok:
                    raise ValueError("account is in hedge mode")
                return {"event_type": "ACCOUNT_VERIFIED", "position_mode": "ONE_WAY"}
        limits = {"verify_account": {"enabled": True, "margin_mode": "cross", "leverage": 10}}
        with self.assertRaisesRegex(AccountVerificationError, "hedge mode"):
            self.make(limits, {v: Reader(v != cfg.venues[0]) for v in cfg.venues})

    def test_unreadable_tiers_refuse_the_start_and_readable_ones_replace_the_assumed_tiers(self):
        from crypto_stat_arb.config import load_config
        cfg, _ = load_config(self.CONFIG)

        class NoTiers:
            def account_risk_tiers(self, instrument_id):
                raise ConnectionError("down")

        class Tiers:
            def account_risk_tiers(self, instrument_id):
                return ({"max_notional": "1000000", "maintenance_rate": "0.005", "initial_rate": "0.01", "maintenance_deduction": "0"},)
        with self.assertRaises(AccountTierReadError):
            self.make({"tier_table": "read_at_start"}, {v: NoTiers() for v in cfg.venues})
        paper = self.make({"tier_table": "read_at_start"}, {v: Tiers() for v in cfg.venues})
        for v in cfg.venues:
            self.assertEqual(paper.session.margin_config.venues[v].tiers[cfg.instrument_id].source, f"ACCOUNT_READ:{v}")
        self.assertEqual(paper.account_checks["tier_table"][cfg.venues[0]]["tiers"], 1)

    def test_a_real_order_session_with_the_clock_gate_and_no_clock_source_refuses_to_start(self):
        from crypto_stat_arb.config import load_config
        cfg, _ = load_config(self.CONFIG)
        limits = {"clock_offset": {"max_uncertainty_ms": 5.0, "max_age_s": 900.0}}
        with self.assertRaisesRegex(ValueError, "clock source"):
            self.make(limits, {v: object() for v in cfg.venues})
        paper = self.make(limits, {v: object() for v in cfg.venues}, clock_source="server_time")
        self.assertIsNotNone(paper.session.clock_gate)
        self.assertFalse(paper.session.clock_gate.replay)                                            # a live session blocks on an unknown clock

    def test_the_feed_arrival_stamp_key_turns_arrival_stamping_on_in_the_live_service(self):
        paper = self.make({"feed_arrival_stamp": True}, None)
        self.assertTrue(paper.service.settings.arrival_stamp)
        self.assertFalse(self.make({}, None).service.settings.arrival_stamp)

    def test_a_purely_simulated_session_with_no_account_starts_as_before_and_does_not_run_the_checks(self):
        import tempfile
        from crypto_stat_arb.live_feed import LiveFeedSettings
        from crypto_stat_arb.paper_live import LivePaperSession
        settings = LiveFeedSettings(udp_enabled=False, binance_market="linear", bybit_market="linear")
        limits = {"verify_account": {"enabled": True, "margin_mode": "cross", "leverage": 10}, "tier_table": "read_at_start", "clock_offset": {"max_uncertainty_ms": 5.0, "max_age_s": 900.0}}
        with tempfile.TemporaryDirectory() as d:
            paper = LivePaperSession(self.CONFIG, Path(d), "sim", settings, session_limits=limits)
        self.assertEqual(paper.account_checks, {})


class W1PaperSide(unittest.IsolatedAsyncioTestCase):
    """W1 (owner 17 Nov), the paper session: the gate block of paper_summary names the gate that acted, and the server-time probe is the host clock monitors' independent reference."""
    CONFIG = Path(__file__).resolve().parents[1] / "config" / "research.json"

    def paper(self, directory, **kw):
        import json
        from crypto_stat_arb.live_feed import LiveFeedSettings
        from crypto_stat_arb.paper_live import LivePaperSession
        from test_live_feed import FakeConnector, FakeWebSocket, StepClock
        binance = FakeWebSocket([json.dumps({"e": "depthUpdate", "E": 1000, "T": 1000, "s": "BTCUSDT", "U": 101, "u": 101, "b": [["59999.90", "2"]], "a": []})])
        bybit = FakeWebSocket([json.dumps({"topic": "orderbook.50.BTCUSDT", "type": "snapshot", "ts": 1000, "data": {"s": "BTCUSDT", "u": 10, "b": [["60060.00", "1"]], "a": [["60060.10", "1"]]}}),
                               json.dumps({"topic": "orderbook.50.BTCUSDT", "type": "delta", "ts": 1001, "data": {"s": "BTCUSDT", "u": 11, "b": [["60060.00", "2"]], "a": []}})])
        clock = StepClock()

        async def http_getter(url, timeout):
            await asyncio.sleep(0.05)
            return json.dumps({"lastUpdateId": 100, "bids": [["59999.90", "1"]], "asks": [["60000.00", "1"]]}), clock()
        return LivePaperSession(self.CONFIG, Path(directory) / "live", "w1-paper",
                                LiveFeedSettings(max_events=4, reconnect_min_seconds=0.001, reconnect_max_seconds=0.002, durable=False, udp_enabled=False, trade_streams_enabled=False),
                                websocket_connector=FakeConnector({"BINANCE": [binance], "BYBIT": [bybit]}), http_getter=http_getter, clock_ns=clock, tick_seconds=10, **kw), clock

    async def test_the_summary_names_the_gate_that_acted_and_labels_the_sessions_own_block(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            paper, _clock = self.paper(d)
            summary = await asyncio.wait_for(paper.run(), timeout=5)
        acting = summary["book_integrity_gate_acting"]
        self.assertEqual(acting["source"], "live feed service (strategy_gate)")
        self.assertEqual(acting["suspend_count"], summary["live_feed"]["service"]["strategy_gate"]["suspend_count"] if "service" in summary["live_feed"] else acting["suspend_count"])
        self.assertIn("NOT evaluated in a live session", summary["book_integrity_gate"]["label"])
        self.assertNotIn("feed_health", summary)                                             # no feed-health block was configured

    async def test_the_server_time_probe_is_the_host_clocks_reference_and_a_failed_probe_is_recorded(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            paper, clock = self.paper(d)
            rows = []
            paper._write_row = rows.append

            async def time_getter(url, timeout):
                now_ms = clock() // 1_000_000
                return (json.dumps({"serverTime": now_ms}) if "binance" in url else json.dumps({"retCode": 0, "result": {"timeNano": str(now_ms * 1_000_000)}, "time": now_ms})), clock()
            paper.service.http_getter = time_getter
            await paper._probe_clock(clock())
            for monitor in (paper.session.time_monitor, paper.service.time_monitor):
                self.assertTrue(monitor.host_clock_external)
                self.assertTrue(monitor.host_clock.source.startswith("server_time:"))
                self.assertEqual(monitor.host_clock.state, "SYNCED")
                self.assertIn("reference_cross_check", monitor.report()["host_offset_correction"])
            before = paper.session.time_monitor.host_clock.samples

            async def dead(url, timeout):
                raise ConnectionError("down")
            paper.service.http_getter = dead
            await paper._probe_clock(clock())
            self.assertEqual(paper.session.time_monitor.host_clock.samples, before)          # no measurement: nothing is fed
            self.assertEqual([r["record_type"] for r in rows], ["CLOCK_PROBE_FAILED"])
            paper._timestamps.close()




class F07AccountRules(unittest.TestCase):
    """F07 (owner 20 Nov): the account's EFFECTIVE rules parsed into a snapshot with source and time (a missing riskId is UNKNOWN, never defaulted), the start-up match against the margin engine's tier table
    at the hard cap, and the fee and risk-tier snapshots kept apart. Responses here are HAND-WRITTEN in the documented shapes; the parse is re-run on the redacted RECORDED testnet responses in
    tests/fixtures/f07_account_rules (F07RecordedFixtures) once the drill has recorded them."""

    BYBIT_POSITIONS = {"retCode": 0, "time": 1_700_000_000_123, "result": {"category": "linear", "list": [
        {"symbol": "SOLUSDT", "riskId": 2, "riskLimitValue": "400000", "leverage": "10", "tradeMode": 0, "autoAddMargin": 0, "markPrice": "150", "positionIdx": 0, "size": "0"}]}}
    BYBIT_INFO = {"retCode": 0, "result": {"marginMode": "REGULAR_MARGIN"}}
    BINANCE_RISK = [{"symbol": "SOLUSDT", "leverage": "10", "marginType": "cross", "isAutoAddMargin": "false", "maxNotionalValue": "50000", "markPrice": "150", "positionAmt": "0", "updateTime": 1_700_000_000_456}]

    def test_bybit_rules_with_and_without_a_risk_id(self):
        from crypto_stat_arb.testnet import account_rules_bybit
        r = account_rules_bybit(self.BYBIT_POSITIONS, self.BYBIT_INFO, BYBIT_LIMITS, "SOLUSDT", 1_700_000_000_123_000_000)
        self.assertEqual((r["risk_id"], r["max_notional"], r["leverage"], r["margin_mode"], r["position_trade_mode"], r["auto_add_margin"], r["maintenance_rate"], r["initial_rate"]),
                         ("2", "400000", "10", "REGULAR_MARGIN", "CROSS", "0", "0.01", "0.1"))                 # the tier's 0.02 (50x) raised to 1 / the account's leverage 10
        self.assertEqual((r["source"].split()[0], r["fetched_ts_ns"], r["unknown"]), ("BYBIT", 1_700_000_000_123_000_000, []))
        bare = {"retCode": 0, "result": {"list": [{"symbol": "SOLUSDT", "leverage": "10"}]}}
        r2 = account_rules_bybit(bare, self.BYBIT_INFO, BYBIT_LIMITS, "SOLUSDT", None)
        self.assertEqual(r2["risk_id"], "UNKNOWN")                                   # never defaulted to the lowest tier
        self.assertIn("risk_id", r2["unknown"])
        self.assertEqual(r2["maintenance_rate"], "UNKNOWN")

    def test_binance_rules_take_the_bracket_in_force_at_the_account_leverage(self):
        from crypto_stat_arb.testnet import account_rules_binance
        r = account_rules_binance(BINANCE_BRACKETS, self.BINANCE_RISK, "SOLUSDT", 1)
        self.assertEqual((r["risk_id"], r["max_notional"], r["leverage"], r["margin_mode"], r["maintenance_rate"], r["initial_rate"]), ("2", "50000", "10", "CROSS", "0.01", "0.1"))
        r2 = account_rules_binance(BINANCE_BRACKETS, [{"symbol": "SOLUSDT"}], "SOLUSDT", 1)
        self.assertEqual((r2["risk_id"], r2["leverage"], r2["initial_rate"]), ("UNKNOWN", "UNKNOWN", "UNKNOWN"))

    def schedule(self, mm="0.01", im="0.1", cap="100000"):
        from crypto_stat_arb.margin import RiskTier, TierSchedule
        return TierSchedule((RiskTier(max_notional=Decimal(cap), maintenance_rate=Decimal(mm), initial_rate=Decimal(im)),))

    def test_the_effective_tier_must_match_the_engine_at_the_hard_cap(self):
        from crypto_stat_arb.margin import check_account_tier
        from crypto_stat_arb.testnet import account_rules_binance
        rules = account_rules_binance(BINANCE_BRACKETS, self.BINANCE_RISK, "SOLUSDT", 1)
        self.assertEqual(check_account_tier(rules, self.schedule(mm="0.0065"), Decimal("5")), [])                    # 5 SOL x 150 = 750 USDT: bracket 1 (0.0065), inside the 50000 limit at 10x
        self.assertTrue(any("maintenance rate" in p for p in check_account_tier(rules, self.schedule(mm="0.005"), Decimal("5"))))
        self.assertTrue(any("maintenance rate" in p for p in check_account_tier(rules, self.schedule(mm="0.0065"), Decimal("100"))))  # 15000 USDT: the account's bracket 2 (0.01)
        self.assertTrue(any("initial rate" in p for p in check_account_tier(rules, self.schedule(mm="0.0065", im="0.02"), Decimal("5"))))   # the engine would reserve at 50x for a 10x account
        self.assertTrue(any("below the hard-cap" in p for p in check_account_tier(rules, self.schedule(mm="0.01"), Decimal("400"))))     # 60000 USDT above the account's 50000
        unknown = dict(rules, risk_id="UNKNOWN")
        self.assertTrue(any("risk_id UNKNOWN" in p for p in check_account_tier(unknown, self.schedule(mm="0.0065"), Decimal("5"))))

    def test_the_start_up_refuses_on_a_mismatch_and_keeps_the_two_snapshots_apart(self):
        from crypto_stat_arb.config import load_config
        cfg, _ = load_config(StartUp.CONFIG)
        tier = cfg.margin.venues[cfg.venues[0]].tiers[cfg.instrument_id]
        cap = Decimal(str(cfg.risk_limits.max_position_base))
        found = tier.tier(cap * Decimal("150"))[1]

        class Reader:
            def __init__(self, venue, mm):
                self.venue, self.mm = venue, mm

            def account_rules(self, instrument_id):
                return {"venue": self.venue, "symbol": "SOLUSDT", "source": "hand-written", "fetched_ts_ns": 1, "risk_id": "1", "max_notional": "1000000000", "leverage": "10",
                        "margin_mode": "CROSS", "position_trade_mode": "CROSS", "auto_add_margin": "0", "maintenance_rate": str(self.mm), "initial_rate": str(found.initial_rate), "mark_price": "150", "unknown": []}
        ok = StartUp().make({"account_tier_check": True}, {v: Reader(v, found.maintenance_rate) for v in cfg.venues})
        self.assertEqual(sorted(ok.account_checks["account_rules"]), sorted(cfg.venues))
        self.assertNotIn("mark_price", ok.account_checks["account_rules"][cfg.venues[0]])
        with self.assertRaisesRegex(ValueError, "maintenance rate"):
            StartUp().make({"account_tier_check": True}, {v: Reader(v, found.maintenance_rate + Decimal("0.001")) for v in cfg.venues})
        from crypto_stat_arb.config import session_limits_from_profile
        self.assertEqual(session_limits_from_profile({"oms": {"session_limits": {"account_tier_check": True}}}, 1.0), {"account_tier_check": True, "clip_base": 1.0})

    def test_account_read_tiers_are_raised_to_the_account_leverage_so_a_real_start_with_both_reads_passes(self):
        from crypto_stat_arb.config import load_config
        from crypto_stat_arb.testnet import account_rules_binance, risk_tier_rows_binance
        cfg, _ = load_config(StartUp.CONFIG)

        class Reader:
            def account_risk_tiers(self, instrument_id):
                return tuple(risk_tier_rows_binance(BINANCE_BRACKETS, "SOLUSDT"))

            def account_rules(self, instrument_id):
                return account_rules_binance(BINANCE_BRACKETS, F07AccountRules.BINANCE_RISK, "SOLUSDT", 1)
        paper = StartUp().make({"tier_table": "read_at_start", "account_tier_check": True}, {v: Reader() for v in cfg.venues})
        sched = paper.session.margin_config.venues[cfg.venues[0]].tiers[cfg.instrument_id]
        self.assertTrue(sched.source.endswith("ACCOUNT_LEVERAGE_10"))
        self.assertEqual({t.initial_rate for t in sched.tiers}, {Decimal("0.1")})                # 1/75 and 1/50 raised to 1/10


class F07RecordedFixtures(unittest.TestCase):
    """F07 (owner 20 Nov): the parsers on the REDACTED responses RECORDED on the testnets by the F04 drill (scripts/testnet_amend_drill.py record_fixtures; only the fields F07 parses, prices rounded, times to
    the hour, no ids, no balances). Skipped until the drill has written them."""
    DIR = Path(__file__).resolve().parent / "fixtures" / "f07_account_rules"

    def load(self, venue):
        import json
        path = self.DIR / f"{venue}.json"
        if not path.exists():
            self.skipTest(f"no recorded fixture {path.name} (the drill writes it)")
        return json.loads(path.read_text())

    def test_bybit_recorded_position_info_parses_with_a_known_risk_id(self):
        from crypto_stat_arb.testnet import account_rules_bybit
        doc = self.load("bybit")
        self.assertIn("RECORDED", doc["label"])
        rules = account_rules_bybit(doc["position_list"], doc["account_info"], doc["risk_limit"], "SOLUSDT", doc["position_list"].get("time"))
        self.assertIn("riskId", doc["position_list"]["fields_seen"])                              # the venue sends riskId in position info
        self.assertNotEqual(rules["risk_id"], "UNKNOWN")
        for k in ("max_notional", "leverage", "margin_mode", "maintenance_rate", "initial_rate"):
            self.assertNotEqual(rules[k], "UNKNOWN", k)

    def test_binance_recorded_brackets_and_position_risk_parse(self):
        from crypto_stat_arb.testnet import account_rules_binance
        doc = self.load("binance")
        self.assertIn("RECORDED", doc["label"])
        rules = account_rules_binance(doc["leverage_bracket"], doc["position_risk"], "SOLUSDT", 1)
        for k in ("risk_id", "max_notional", "leverage", "margin_mode", "maintenance_rate", "initial_rate"):
            self.assertNotEqual(rules[k], "UNKNOWN", k)
        self.assertTrue(rules["maintenance_ladder"])


CHRONY_OK = """Reference ID    : A9FEA97B (169.254.169.123)
Stratum         : 4
Ref time (UTC)  : Fri Nov 20 08:00:00 2026
System time     : 0.000250000 seconds fast of NTP time
Last offset     : -0.000001234 seconds
RMS offset      : 0.000004321 seconds
Frequency       : 12.345 ppm slow
Residual freq   : +0.001 ppm
Skew            : 0.012 ppm
Root delay      : 0.000600000 seconds
Root dispersion : 0.000300000 seconds
Update interval : 64.5 seconds
Leap status     : Normal
"""                                     # CONSTRUCTED from the documented `chronyc tracking` format: UNVERIFIED until ollog hostmeasure records real output on the production host
CHRONY_UNSYNC = """Reference ID    : 00000000 ()
Stratum         : 0
Ref time (UTC)  : Thu Jan 01 00:00:00 1970
System time     : 0.000000000 seconds slow of NTP time
Last offset     : +0.000000000 seconds
RMS offset      : 0.000000000 seconds
Frequency       : 0.000 ppm slow
Residual freq   : +0.000 ppm
Skew            : 0.000 ppm
Root delay      : 1.000000000 seconds
Root dispersion : 1.000000000 seconds
Update interval : 0.0 seconds
Leap status     : Not synchronised
"""
REF_NS = 1_795_161_600 * 1_000_000_000          # Fri Nov 20 08:00:00 2026 UTC


class F03Chrony(unittest.TestCase):
    """Phase 2 F03 a / c (owner 20 Nov): the chrony clock source. offset = local minus NTP (fast = +); uncertainty = |system offset| + root dispersion + root delay / 2; measured at chrony's Ref time.
    Unsynchronised, absent or failing: no measurement (the gate goes unknown); no fallback."""

    def test_the_tracking_fields_parse_with_the_sign_from_the_words(self):
        from crypto_stat_arb.time_integrity import parse_chronyc_tracking
        f = parse_chronyc_tracking(CHRONY_OK)
        self.assertEqual((f["reference_id"], f["reference_name"], f["stratum"], f["ref_time_ns"]), ("A9FEA97B", "169.254.169.123", 4, REF_NS))
        self.assertEqual((f["system_offset_ns"], f["root_delay_ns"], f["root_dispersion_ns"], f["update_interval_s"], f["leap_status"]), (250_000, 600_000, 300_000, 64.5, "Normal"))
        self.assertEqual(f["frequency_ppm"], -12.345)
        self.assertEqual(parse_chronyc_tracking(CHRONY_OK.replace("fast of NTP", "slow of NTP"))["system_offset_ns"], -250_000)

    def test_the_measurement_follows_the_owners_uncertainty_rule(self):
        from crypto_stat_arb.time_integrity import chrony_measurement, parse_chronyc_tracking
        m = chrony_measurement(parse_chronyc_tracking(CHRONY_OK))
        self.assertEqual((m.offset_ns, m.uncertainty_ns, m.measured_ts_ns), (250_000, 250_000 + 300_000 + 300_000, REF_NS))
        self.assertTrue(m.source.startswith("chrony:169.254.169.123"))
        self.assertEqual(m.fields["stratum"], 4)

    def test_unsynchronised_absent_or_failing_chrony_gives_no_measurement(self):
        from crypto_stat_arb.time_integrity import read_chrony
        m, fields, why = read_chrony(runner=lambda: (0, CHRONY_UNSYNC, ""))
        self.assertIsNone(m)
        self.assertIn("not synchronised", why)
        m, _, why = read_chrony(binary="/nonexistent/chronyc")
        self.assertIsNone(m)
        self.assertIn("could not be run", why)
        m, _, why = read_chrony(runner=lambda: (1, "", "506 Cannot talk to daemon"))
        self.assertIsNone(m)
        self.assertIn("exited 1", why)
        m, _, why = read_chrony(runner=lambda: (0, CHRONY_OK.replace("Root dispersion : 0.000300000 seconds\n", ""), ""))
        self.assertIsNone(m)                                                                         # a needed field missing: nothing is defaulted
        self.assertIn("root_dispersion_ns", why)

    def test_a_chrony_that_stopped_updating_is_unknown_by_age_and_the_limits_stay(self):
        from crypto_stat_arb.time_integrity import ClockGate, read_chrony
        m, _, _ = read_chrony(runner=lambda: (0, CHRONY_OK, ""))
        gate = ClockGate(5_000_000, 900_000_000_000, max_offset_ns=5_000_000)
        gate.update(m)
        self.assertEqual(gate.evaluate(REF_NS + 899 * 10**9)[0], "ok")
        self.assertEqual(gate.evaluate(REF_NS + 901 * 10**9)[0], "unknown")

    def test_the_profile_keys_parse(self):
        from crypto_stat_arb.config import session_limits_from_profile
        out = session_limits_from_profile({"oms": {"session_limits": {"clock_offset": {"max_uncertainty_ms": 5.0, "max_age_s": 900, "max_offset_ms": 5.0, "source": "chrony",
                                                                                       "monotonic_clock": {"step_threshold_ms": 50}}}}})["clock_offset"]
        self.assertEqual(out, {"max_uncertainty_ms": 5.0, "max_age_s": 900.0, "max_offset_ms": 5.0, "source": "chrony", "monotonic_clock": {"step_threshold_ms": 50.0}})
        old = session_limits_from_profile({"oms": {"session_limits": {"clock_offset": {"max_uncertainty_ms": 5.0, "max_age_s": 900}}}})["clock_offset"]
        self.assertEqual(old, {"max_uncertainty_ms": 5.0, "max_age_s": 900.0})                       # an earlier profile's block is unchanged
        for bad in ({"max_offset_ms": 0}, {"source": "ntp"}):
            with self.assertRaises(ValueError):
                session_limits_from_profile({"oms": {"session_limits": {"clock_offset": {"max_uncertainty_ms": 5.0, **bad}}}})


class F03MonotonicSessionClock(unittest.TestCase):
    """Phase 2 F03 b: intervals and deadlines on the monotonic clock; a wall-clock step is detected and recorded; the session clock never steps."""

    def test_a_wall_step_does_not_move_the_session_clock_and_is_recorded_once(self):
        from crypto_stat_arb.time_integrity import MonotonicSessionClock
        t = {"wall": 1_000_000_000_000, "mono": 5_000}
        c = MonotonicSessionClock(wall=lambda: t["wall"], mono=lambda: t["mono"], step_threshold_ns=50_000_000)
        t["wall"] += 1_000_000; t["mono"] += 1_000_000
        self.assertEqual(c(), 1_000_001_000_000)
        self.assertIsNone(c.check_step())
        t["wall"] += 1_000_000 - 2_000_000_000                                                        # 1 ms passes and the wall clock is stepped back 2 s
        t["mono"] += 1_000_000
        self.assertEqual(c(), 1_000_002_000_000)                                                      # the session clock went on
        ev = c.check_step()
        self.assertEqual((ev["step_ns"], ev["gap_after_ns"]), (-2_000_000_000, 2_000_000_000))
        self.assertIsNone(c.check_step())                                                             # once
        self.assertEqual(c.gap_ns(), 2_000_000_000)
        t["wall"] += 10_000_000; t["mono"] += 10_000_000
        self.assertIsNone(c.check_step())                                                             # ordinary time: no step

    def test_the_paper_session_uses_it_only_when_the_profile_asks_and_no_clock_is_injected(self):
        lim = {"clock_offset": {"max_uncertainty_ms": 5.0, "max_age_s": 900.0, "max_offset_ms": 5.0, "monotonic_clock": {"step_threshold_ms": 50.0}}}
        paper = StartUp().make(lim, None)
        self.assertIsNotNone(paper.session_clock)
        self.assertIs(paper.clock_ns, paper.session_clock)
        self.assertIsNotNone(paper.session.clock_gate.session_gap_of)
        self.assertIsNone(StartUp().make({"clock_offset": {"max_uncertainty_ms": 5.0, "max_age_s": 900.0}}, None).session_clock)


class F03PaperChrony(W1PaperSide):
    """Phase 2 F03 a / c in the paper session: chrony feeds the gate; an unusable chrony invalidates it at once; the venues' server time is a cross-check with one alarm per episode."""

    async def test_chrony_feeds_the_gate_an_unusable_one_invalidates_it_and_the_server_time_cross_checks(self):
        import json
        import tempfile
        from crypto_stat_arb.time_integrity import ClockGate
        with tempfile.TemporaryDirectory() as d:
            paper, clock = self.paper(d, clock_source="chrony")
            rows = []
            paper._write_row = rows.append
            gate = paper.session.clock_gate = ClockGate(5_000_000, 900_000_000_000, max_offset_ns=5_000_000)
            ok = (0, CHRONY_OK.replace("Fri Nov 20 08:00:00 2026", __import__("datetime").datetime.fromtimestamp(clock() // 10**9, __import__("datetime").timezone.utc).strftime("%a %b %d %H:%M:%S %Y")), "")
            with mock.patch("crypto_stat_arb.paper_live.read_chrony", lambda: __import__("crypto_stat_arb.time_integrity", fromlist=["read_chrony"]).read_chrony(runner=lambda: ok)):
                await paper._read_chrony(clock())
            self.assertEqual(rows[-1]["record_type"], "CLOCK_MEASUREMENT")
            self.assertEqual((rows[-1]["offset_ms"], rows[-1]["uncertainty_ms"], rows[-1]["gate_state"]), (0.25, 0.85, "ok"))
            self.assertTrue(paper.session.time_monitor.host_clock.source.startswith("chrony:"))

            async def time_getter(url, timeout):                                                     # the venues read 20 ms behind chrony's view: disagreement beyond the probe's uncertainty
                now_ms = clock() // 1_000_000 + 20
                return (json.dumps({"serverTime": now_ms}) if "binance" in url else json.dumps({"retCode": 0, "result": {"timeNano": str(now_ms * 1_000_000)}, "time": now_ms})), clock()
            paper.service.http_getter = time_getter
            await paper._probe_clock(clock())
            await paper._probe_clock(clock())
            alarms = [e for e in paper.session.ledger.events if e["event_type"] == "CLOCK_CROSS_CHECK_DISAGREE"]
            self.assertEqual(len(alarms), 1)                                                         # once per episode
            self.assertIs(gate.measurement.source.startswith("chrony:"), True)                       # the server time never replaces chrony
            with mock.patch("crypto_stat_arb.paper_live.read_chrony", lambda: (None, {}, "chronyc could not be run (FileNotFoundError)")):
                await paper._read_chrony(clock())
            self.assertEqual(rows[-1]["record_type"], "CLOCK_SOURCE_UNAVAILABLE")
            self.assertEqual(gate.evaluate(clock())[0], "unknown")                                  # no fallback: quoting stops
            paper._timestamps.close()

if __name__ == "__main__":
    unittest.main()
