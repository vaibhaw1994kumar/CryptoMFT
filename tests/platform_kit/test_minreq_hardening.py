"""Regression coverage for the minimum-requirements hardening work.

Each test pins one defect that an audit found and a probe reproduced: data that
was sequence-valid but economically impossible, execution that ignored latency
and liquidity, plans that closed while exposed, controls that never fired, and
paper/testnet wiring that did not exist. All tests are hermetic.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import zlib
from decimal import Decimal
from pathlib import Path

import pytest

from trading_platform_poc.minreq.acceptance import (
    BTC_BINANCE,
    BTC_OKX,
    MicroEnvironment,
    book_event,
)
from trading_platform_poc.minreq.backtest import (
    BacktestConfig,
    TradingSession,
    build_dataset,
)
from trading_platform_poc.minreq.canonical import (
    BookValidity,
    CanonicalEvent,
    CanonicalEventType,
    QualityFlag,
    TradePayload,
    make_event_id,
)
from trading_platform_poc.minreq.checksums import (
    okx_book_checksum,
    okx_checksum_from_levels,
)
from trading_platform_poc.minreq.cli import resolve_execution_mode
from trading_platform_poc.minreq.features import FeatureConfig
from trading_platform_poc.minreq.ingest import MonotonicWallClock, record_live
from trading_platform_poc.minreq.latency import latency_model_from_preset
from trading_platform_poc.minreq.ledger import EventLedger
from trading_platform_poc.minreq.live import run_live_paper
from trading_platform_poc.minreq.matching import ExecutionMode, MatchingConfig
from trading_platform_poc.minreq.quality import CanonicalBook, QualityMonitor
from trading_platform_poc.minreq.riskcontrols import (
    AutoKillPolicy,
    KillSwitchSupervisor,
    LimitId,
    RiskLimits,
)
from trading_platform_poc.minreq.router import (
    ExecutionStyle,
    LegDispatch,
    PlanState,
    RouterConfig,
    StateTransitionError,
)
from trading_platform_poc.minreq.statarb import StatArbConfig
from trading_platform_poc.minreq.synthetic import (
    FaultInjection,
    SyntheticConfig,
    generate,
)
from trading_platform_poc.minreq.testnet import (
    VenueOrderReport,
    VenueTestnetExecution,
    build_testnet_gateways,
    venue_testnet_matcher_factory,
)

T = 1_800_000_000_000_000_000
MS = 1_000_000
GROUP = "BTC/USDT.SPOT"


def _positions(env: MicroEnvironment, run_id: str) -> dict[str, Decimal]:
    return {
        venue: position.quantity
        for (_, venue), position in env.ledger.derive(run_id=run_id).positions.items()
        if position.quantity
    }


# ----------------------------------------------------------------- data plane
def test_okx_checksum_interleaves_bids_and_asks() -> None:
    bids = [(Decimal("3366.1"), Decimal("7")), (Decimal("3366"), Decimal("6"))]
    asks = [(Decimal("3366.8"), Decimal("9")), (Decimal("3368"), Decimal("8"))]
    expected = zlib.crc32(b"3366.1:7:3366.8:9:3366:6:3368:8") & 0xFFFFFFFF
    if expected >= 1 << 31:
        expected -= 1 << 32
    assert okx_checksum_from_levels(bids, asks) == expected
    assert okx_book_checksum(dict(bids), dict(asks)) == expected


@pytest.mark.parametrize(
    ("faults", "code", "failure"),
    [
        (FaultInjection(checksum_mismatch_at=40), "checksum_mismatch", "checksum_mismatches"),
        (FaultInjection(price_jump_at=40), "price_jump", "price_jumps"),
        (FaultInjection(invalid_trade_at=40), "schema_exception", "invalid_trades"),
        (FaultInjection(schema_exception_at=40), "schema_exception", "schema_exceptions"),
        (FaultInjection(silent_venue_after=60), "stale", "silent_streams"),
    ],
)
def test_each_data_defect_is_flagged_and_blocks_approval(
    tmp_path: Path, faults: FaultInjection, code: str, failure: str
) -> None:
    _, manifest = build_dataset(
        tmp_path / "ds", synthetic=SyntheticConfig(seed=5, steps=120, faults=faults)
    )
    quality = manifest["quality"]
    assert manifest["approved"] is False
    assert code in quality["by_code"]
    assert any(item.startswith(failure) for item in quality["failures"]), quality["failures"]
    if faults.schema_exception_at is not None:
        quarantine = tmp_path / "ds" / "quarantine" / "quarantine.jsonl"
        row = json.loads(quarantine.read_text(encoding="utf-8").splitlines()[0])
        assert row["raw_sha256"] and "NormalizationError" in row["error"]


def test_clean_synthetic_dataset_is_labelled_synthetic_without_anomalies(
    tmp_path: Path,
) -> None:
    _, manifest = build_dataset(tmp_path / "ds", synthetic=SyntheticConfig(seed=5, steps=80))
    quality = manifest["quality"]
    assert manifest["approved"] is True
    assert quality["by_code"] == {}
    assert quality["provenance"]["synthetic"] == quality["event_count"]
    assert quality["time_integrity"]["blocked_events"] == 0
    assert "created_at" not in quality["time_integrity"]


def test_out_of_order_update_is_not_reported_as_a_duplicate() -> None:
    book = CanonicalBook("okx", BTC_OKX, verify_checksums=False)
    book.apply(book_event("okx", BTC_OKX, "BTC-USDT", 10, [("100", "1")], [("101", "1")], T))
    delta = lambda seq: book_event(  # noqa: E731
        "okx", BTC_OKX, "BTC-USDT", seq, [("100.5", "1")], [], T + seq, snapshot=False
    )
    assert book.apply(delta(11)).applied
    assert book.apply(delta(12)).applied
    assert book.apply(delta(12)).flags == (QualityFlag.DUPLICATE,)
    assert book.apply(delta(9)).flags == (QualityFlag.OUT_OF_ORDER,)


def test_price_jump_and_wide_spread_invalidate_the_book() -> None:
    book = CanonicalBook("binance", BTC_BINANCE)
    book.apply(book_event("binance", BTC_BINANCE, "BTCUSDT", 1, [("60000", "1")], [("60001", "1")], T))
    jumped = book.apply(
        book_event("binance", BTC_BINANCE, "BTCUSDT", 2, [("6000000", "1")], [("6000001", "1")], T + 1)
    )
    assert jumped.validity is BookValidity.INVALID
    assert QualityFlag.PRICE_JUMP in jumped.flags
    wide = book.apply(
        book_event("binance", BTC_BINANCE, "BTCUSDT", 3, [("30000", "1")], [("90000", "1")], T + 2)
    )
    assert QualityFlag.WIDE_SPREAD in wide.flags
    assert wide.validity is BookValidity.INVALID


def test_trades_with_impossible_values_are_rejected() -> None:
    monitor = QualityMonitor()
    monitor.process(book_event("okx", BTC_OKX, "BTC-USDT", 1, [("60000", "1")], [("60001", "1")], T))

    def trade(price: str, quantity: str, ts: int) -> CanonicalEvent:
        return CanonicalEvent(
            event_id=make_event_id("okx", BTC_OKX, CanonicalEventType.TRADE, ts, ts),
            event_type=CanonicalEventType.TRADE,
            venue="okx",
            instrument_id=BTC_OKX,
            venue_symbol="BTC-USDT",
            exchange_timestamp_ns=ts,
            receive_timestamp_ns=ts,
            sequence=ts,
            payload=TradePayload(trade_id=str(ts), price=Decimal(price), quantity=Decimal(quantity)),
        )

    assert QualityFlag.SCHEMA_EXCEPTION in monitor.process(trade("-5", "1", T + 1))[0].quality_flags
    assert QualityFlag.SCHEMA_EXCEPTION in monitor.process(trade("60000", "0", T + 2))[0].quality_flags
    assert QualityFlag.TRADE_OUTSIDE_BOOK in monitor.process(trade("70000", "1", T + 3))[0].quality_flags
    assert monitor.process(trade("60000.5", "1", T + 4))[0].quality_flags == ()
    assert monitor.report().approved is False


def test_a_feed_that_dies_is_detected_without_waiting_for_it() -> None:
    monitor = QualityMonitor()
    monitor.process(book_event("binance", BTC_BINANCE, "BTCUSDT", 1, [("60000", "1")], [("60001", "1")], T))
    emitted = []
    for step in range(1, 40):
        emitted.extend(
            monitor.process(
                book_event("okx", BTC_OKX, "BTC-USDT", step, [("60000", "1")], [("60001", "1")], T + step * 100 * MS)
            )[1:]
        )
    assert any(event.payload.details.get("detected_by") == "heartbeat" for event in emitted)
    report = monitor.report()
    assert report.silent_streams == ("binance:BTC-USDT.BINANCE.SPOT",)
    assert not report.approved


def test_updates_before_the_first_snapshot_are_not_gaps() -> None:
    monitor = QualityMonitor()
    early = book_event("binance", BTC_BINANCE, "BTCUSDT", 5, [("60000", "1")], [], T, snapshot=False)
    assert monitor.process(early)[0].quality_flags == ()
    monitor.process(book_event("binance", BTC_BINANCE, "BTCUSDT", 6, [("60000", "1")], [("60001", "1")], T + 1))
    assert "sequence_gap" not in monitor.report().by_code


# ------------------------------------------------------------------ execution
def test_orders_execute_against_the_book_when_they_arrive(tmp_path: Path) -> None:
    env = MicroEnvironment(tmp_path / "arrive", run_id="arrive")
    env.seed_books(T)
    env.router.submit_opportunity(env.intent(T, opportunity_id="arrive"))
    # The best ask is lifted while the orders are on the wire.
    env.engine.observe(
        book_event("binance", BTC_BINANCE, "BTCUSDT", 2, [], [("60000.00", "0"), ("60010.00", "5")], T + 1, snapshot=False)
    )
    env.drain()
    binance = next(item for item in env.router.execution_quality if item["venue"] == "binance")
    assert Decimal(binance["fill_price"]) > Decimal(binance["expected_price"])
    assert Decimal(binance["latency_slippage_bps"]) > 0
    assert binance["execution_ns"] > binance["sent_ns"]
    env.close()


def test_ack_timeout_moves_orders_to_unknown_and_trusts_the_venue(tmp_path: Path) -> None:
    env = MicroEnvironment(tmp_path / "unknown", run_id="unknown", router=RouterConfig(ack_timeout_ns=0))
    env.seed_books(T)
    result = env.router.submit_opportunity(env.intent(T, opportunity_id="unknown"))
    env.drain()
    states = [
        event.payload["state"]
        for event in env.ledger.events(run_id="unknown")
        if event.event_type == "order_state"
    ]
    assert states.count("unknown") == 2
    assert result.plan.state is PlanState.CLOSED
    assert env.ledger.reconcile(env.matcher.venue_snapshot(), env.router.now_ns, run_id="unknown").clean
    assert env.matcher.orders_received == 2
    env.close()


def test_a_plan_cannot_close_while_exposed_and_unwinds_when_the_venue_admits_it(
    tmp_path: Path,
) -> None:
    env = MicroEnvironment(
        tmp_path / "residual",
        run_id="residual",
        matching=MatchingConfig(max_orders_per_second=2),
        risk=RiskLimits(max_residual_duration_ns=150 * MS),
    )
    env.seed_books(T, okx_depth=[("60120.0", "0.05"), ("60119.0", "0.01")])
    first = env.router.submit_opportunity(env.intent(T, opportunity_id="residual-a"))
    second = env.router.submit_opportunity(env.intent(T, opportunity_id="residual-b"))
    env.router.advance(T + 100 * MS)
    exposed = next(result.plan for result in (first, second) if result.plan.net_exposure)
    assert exposed.state is not PlanState.CLOSED
    with pytest.raises(StateTransitionError):
        env.router._set_plan_state(exposed, PlanState.RECONCILED, T + 100 * MS)

    sequence, now = 1, T + 100 * MS
    while exposed.state is not PlanState.CLOSED and now < T + 10_000 * MS:
        now += 100 * MS
        env.router.advance(now)  # arrivals land before the next market update
        sequence += 1
        env.engine.observe(book_event("binance", BTC_BINANCE, "BTCUSDT", sequence, [("59999.00", "5")], [("60000.00", "5")], now))
        env.engine.observe(book_event("okx", BTC_OKX, "BTC-USDT", sequence, [("60120.0", "5")], [("60121.0", "5")], now))
    assert exposed.state is PlanState.CLOSED
    assert exposed.net_exposure == 0
    assert exposed.residual_breached
    assert any(order.reject_reason == "rate_limit" for order in exposed.children)
    assert env.router.residual_breaches[0]["limit_id"] == LimitId.MAX_RESIDUAL_DURATION.value
    assert env.risk_engine.risk_holds == {}
    assert sum(_positions(env, "residual").values()) == 0
    env.close()


def test_consumed_liquidity_is_not_sold_twice(tmp_path: Path) -> None:
    env = MicroEnvironment(tmp_path / "reuse", run_id="reuse")
    env.seed_books(T, binance_depth=[("60000.00", "0.05"), ("60000.50", "5")])
    env.router.submit_opportunity(env.intent(T, opportunity_id="reuse-a"))
    env.drain()
    assert [level.price for level in env.matcher.available_depth("binance", "buy")][0] == Decimal("60000.50")
    env.engine.observe(book_event("binance", BTC_BINANCE, "BTCUSDT", 2, [("59990.00", "1")], [], env.router.now_ns + 1, snapshot=False))
    env.router.submit_opportunity(env.intent(env.router.now_ns + 2, opportunity_id="reuse-b"))
    env.drain()
    prices = [Decimal(item["fill_price"]) for item in env.router.execution_quality if item["venue"] == "binance"]
    assert prices == [Decimal("60000.00"), Decimal("60000.50")]
    # Once the venue's own update shows the level consumed, nothing is withheld twice.
    env.engine.observe(book_event("binance", BTC_BINANCE, "BTCUSDT", 3, [], [("60000.50", "4.95")], env.router.now_ns + 3, snapshot=False))
    assert env.matcher.available_depth("binance", "buy")[0].quantity == Decimal("4.95")
    env.close()


def test_ioc_remainder_is_cancelled_explicitly(tmp_path: Path) -> None:
    env = MicroEnvironment(tmp_path / "ioc", run_id="ioc")
    env.seed_books(T, binance_depth=[("60000.00", "0.05"), ("60000.50", "0.01")])
    env.router.submit_opportunity(env.intent(T, opportunity_id="ioc-a"))
    env.router.submit_opportunity(env.intent(T, opportunity_id="ioc-b"))
    env.drain()
    expired = [event for event in env.ledger.events(run_id="ioc") if event.event_type == "order_expired"]
    assert expired and Decimal(expired[0].payload["expired_quantity"]) == Decimal("0.04")
    cancelled = [
        order
        for plan in env.router.plans.values()
        for order in plan.children
        if order.reject_reason == "ioc_remainder_expired"
    ]
    assert cancelled and cancelled[0].state.value == "cancelled"
    env.close()


def test_hardest_first_releases_the_dependent_leg_after_the_first_fill(tmp_path: Path) -> None:
    env = MicroEnvironment(tmp_path / "legs", run_id="legs", router=RouterConfig(leg_dispatch=LegDispatch.HARDEST_FIRST))
    env.seed_books(T, okx_depth=[("60120.0", "0.06")])
    result = env.router.submit_opportunity(env.intent(T, quantity="0.02", opportunity_id="legs"))
    assert result.plan.dispatch_policy == "hardest_first"
    assert len(result.plan.children) == 1
    env.drain()
    first, second = result.plan.children
    assert result.plan.state is PlanState.CLOSED
    assert second.sent_ns > first.sent_ns
    assert second.quantity == first.filled_quantity


def test_maker_leg_rests_in_queue_and_is_hedged_as_taker(tmp_path: Path) -> None:
    env = MicroEnvironment(tmp_path / "maker", run_id="maker", router=RouterConfig(execution_style=ExecutionStyle.MAKER_TAKER))
    env.seed_books(T)
    result = env.router.submit_opportunity(env.intent(T, opportunity_id="maker"))
    env.router.advance(T + 50 * MS)
    maker = result.plan.children[0]
    assert maker.state.value == "acknowledged" and maker.post_only
    # Five units rest ahead at the bid; a smaller print does not reach us.
    assert env.matcher.on_trade("binance", BTC_BINANCE, Decimal("59999.00"), Decimal("4"), "sell", T + 60 * MS) == []
    fills = env.matcher.on_trade("binance", BTC_BINANCE, Decimal("59999.00"), Decimal("2"), "sell", T + 70 * MS)
    env.router.on_maker_fills(fills)
    env.drain()
    assert result.plan.state is PlanState.CLOSED
    assert [(order.liquidity, order.state.value) for order in result.plan.children] == [
        ("maker", "filled"),
        ("taker", "filled"),
    ]


def test_mark_to_market_daily_loss_trips_the_kill_switch_and_flattens(tmp_path: Path) -> None:
    env = MicroEnvironment(tmp_path / "loss", run_id="loss", risk=RiskLimits(max_daily_loss=Decimal("40")))
    env.seed_books(T)
    env.router.submit_opportunity(env.intent(T, opportunity_id="loss"))
    env.drain()
    now = env.router.now_ns
    env.risk_engine.mark(BTC_BINANCE, "binance", Decimal("59000"))
    env.risk_engine.mark(BTC_OKX, "okx", Decimal("61000"))
    assert env.risk_engine.realized_pnl < 0  # fees only so far
    assert env.risk_engine.unrealized_pnl < -Decimal("40")
    fired = KillSwitchSupervisor(env.risk_engine).evaluate(now)
    assert [item["trigger"] for item in fired] == ["daily_loss"]
    assert env.risk_engine.kill_switch.active
    assert env.router.flatten_positions(now, "daily_loss") == 2
    env.drain()
    assert _positions(env, "loss") == {}
    blocked = env.router.submit_opportunity(env.intent(env.router.now_ns, opportunity_id="after-kill"))
    assert not blocked.accepted and blocked.reason == "kill switch is active"
    env.close()


def test_residual_breach_holds_new_risk_before_escalating(tmp_path: Path) -> None:
    env = MicroEnvironment(tmp_path / "hold", run_id="hold")
    env.risk_engine.hold_new_risk("plan-x", LimitId.MAX_RESIDUAL_NOTIONAL, "open residual", T)
    env.seed_books(T)
    held = env.router.submit_opportunity(env.intent(T, opportunity_id="held"))
    assert not held.accepted and "new risk held" in held.reason
    supervisor = KillSwitchSupervisor(env.risk_engine, AutoKillPolicy(residual_kill_after_ns=5_000 * MS))
    breach = {"plan_id": "plan-x", "breached_ns": T, "net_exposure": "0.05"}
    assert supervisor.evaluate(T + 1_000 * MS, open_residual_breaches=[breach]) == []
    fired = supervisor.evaluate(T + 6_000 * MS, open_residual_breaches=[breach])
    assert fired[0]["trigger"] == "residual_breach_unresolved:plan-x"
    env.close()


def test_reduce_only_intents_can_never_open_exposure(tmp_path: Path) -> None:
    env = MicroEnvironment(tmp_path / "reduce", run_id="reduce")
    env.seed_books(T)
    result = env.router.submit_opportunity(env.intent(T, opportunity_id="reduce", risk_reducing=True))
    assert not result.accepted and result.reason == "nothing_to_reduce"
    env.close()


def test_supervisor_fires_on_every_automatic_trigger(tmp_path: Path) -> None:
    env = MicroEnvironment(tmp_path / "triggers", run_id="triggers")
    supervisor = KillSwitchSupervisor(env.risk_engine)
    supervisor.venue_down("okx", T)
    fired = supervisor.evaluate(
        T + 31_000 * MS,
        feed_ages_ns={"binance": 31_000 * MS},
        venue_reject_streaks={"okx": 5},
        unresolved_orders=[{"client_order_id": "mr-x", "venue": "okx"}],
        reconciliation_breaks=1,
    )
    triggers = {item["trigger"] for item in fired}
    assert triggers == {
        "stale_feed:binance",
        "venue_down:okx",
        "venue_rejects:okx",
        "unresolved_order:mr-x",
        "reconciliation_break",
    }
    assert sum(1 for item in fired if item["engaged_kill_switch"]) == 1
    env.close()


def test_execution_modes_are_resolved_and_enforced_at_startup(tmp_path: Path) -> None:
    assert resolve_execution_mode("backtest", "internal_simulation") is ExecutionMode.INTERNAL_SIMULATION
    assert resolve_execution_mode("paper", "internal_simulation") is ExecutionMode.INTERNAL_PAPER
    assert resolve_execution_mode("paper", "venue_testnet") is ExecutionMode.VENUE_TESTNET
    with pytest.raises(ValueError):
        resolve_execution_mode("backtest", "venue_testnet")
    for config in (
        BacktestConfig(run_id="x", matching=MatchingConfig(mode=ExecutionMode.INTERNAL_PAPER)),
        BacktestConfig(run_id="x", mode="paper"),
        BacktestConfig(run_id="x", mode="paper", matching=MatchingConfig(mode=ExecutionMode.VENUE_TESTNET)),
    ):
        with pytest.raises(ValueError):
            TradingSession(config, tmp_path / "refused")


def test_latency_presets_come_from_new_backtest() -> None:
    model = latency_model_from_preset("singapore", ("binance", "okx", "bybit"), seed=1)
    binance = model.profile("binance")
    assert binance.entry.model == "lognormal"
    assert binance.entry.base_ns == 40 * MS
    assert model.profile("bybit").entry.base_ns == 6 * MS
    assert model.profile("okx").entry.model == "uniform"
    with pytest.raises(ValueError):
        latency_model_from_preset("mars", ("binance",))


# ------------------------------------------------------------- live and testnet
class _FakeFeed:
    """Replays generator frames as if they arrived on a socket."""

    finite = True

    def __init__(self, records, clock, cursor) -> None:
        self._records = records
        self._clock = clock
        self._cursor = cursor

    async def snapshot(self):
        return None

    async def stream(self, **_: object):
        for record in self._records:
            self._cursor["t"] = max(self._cursor["t"], record.receive_timestamp_ns)
            yield dataclasses.replace(record, receive_timestamp_ns=self._clock())
            await asyncio.sleep(0)


def _fake_feeds(steps: int = 250, seed: int = 42):
    records = generate(SyntheticConfig(seed=seed, steps=steps))
    cursor = {"t": records[0].receive_timestamp_ns}
    clock = MonotonicWallClock(source=lambda: cursor["t"])
    by_venue: dict[str, list] = {}
    for record in records:
        by_venue.setdefault(record.venue, []).append(record)

    def factory(venue: str, symbol: str, feed_clock):
        return _FakeFeed(by_venue[venue], feed_clock, cursor)

    return factory, clock


async def test_record_live_publishes_a_replayable_dataset(tmp_path: Path) -> None:
    factory, clock = _fake_feeds(steps=60)
    outcome = await record_live(
        tmp_path / "live-ds", venues=("binance", "okx"), symbol="BTCUSDT", feed_factory=factory, clock=clock
    )
    manifest = outcome["manifest"]
    assert manifest["raw_records"] > 0 and manifest["canonical_events"] > 0
    assert set(manifest["venues"]) == {"binance", "okx"}
    assert outcome["capture"]["records"]["okx"] > 0
    assert (tmp_path / "live-ds" / "dataset_manifest.json").is_file()


async def test_live_paper_session_trades_on_feeds_with_internal_paper_fills(tmp_path: Path) -> None:
    factory, clock = _fake_feeds()
    config = BacktestConfig(
        run_id="live-paper",
        mode="paper",
        seed=42,
        matching=MatchingConfig(mode=ExecutionMode.INTERNAL_PAPER),
        strategy=StatArbConfig(
            economic_group=GROUP, entry_z=Decimal("1.2"), exit_z=Decimal("0.3"),
            target_quantity=Decimal("0.05"), min_samples=10, cooldown_ns=0,
        ),
        feature=FeatureConfig(economic_group=GROUP, min_samples=10),
    )
    result = await run_live_paper(
        config, tmp_path / "run", duration_seconds=30, feed_factory=factory, clock=clock,
        record_dataset=tmp_path / "recorded", heartbeat_seconds=0.001, settle_seconds=0.05,
    )
    assert int(result.tca.totals["fills"]) > 0
    assert result.reconciliation["breaks"] == []
    ledger = EventLedger(result.ledger_path, run_id="live-paper")
    try:
        sources = {event.source for event in ledger.events(run_id="live-paper", event_type="fill")}
    finally:
        ledger.close()
    assert sources == {"internal_paper"}
    assert result.manifest.component_versions["execution_mode"] == "internal_paper"
    assert json.loads((result.output_dir / "recorded_dataset.json").read_text())["checksum"]


class _FakeGateway:
    def __init__(self, venue: str, *, fill_ratio: Decimal = Decimal(1), lose_submit: bool = False) -> None:
        self.venue = venue
        self.fill_ratio = fill_ratio
        self.lose_submit = lose_submit
        self.submits = 0
        self.orders: dict[str, VenueOrderReport] = {}

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def submit(self, request, venue_symbol):
        self.submits += 1
        filled = (request.quantity * self.fill_ratio).quantize(Decimal("0.00001"))
        report = VenueOrderReport(
            client_order_id=request.client_order_id,
            state="filled" if filled == request.quantity else "cancelled",
            quantity=request.quantity,
            filled_quantity=filled,
            average_price=request.price,
            venue_order_id=f"{self.venue}-{self.submits}",
        )
        self.orders[request.client_order_id] = report
        if self.lose_submit:
            raise TimeoutError("connection dropped after the venue accepted the order")
        return report

    async def query(self, request, venue_symbol):
        return self.orders.get(request.client_order_id)

    async def cancel(self, request, venue_symbol):
        return self.orders.get(request.client_order_id)


async def _run_testnet(tmp_path: Path, gateways) -> MicroEnvironment:
    env = MicroEnvironment(tmp_path / "testnet", run_id="testnet")
    now = {"t": T}
    env.router.matcher = VenueTestnetExecution(
        gateways,
        books=env.engine.books,
        latency=env.latency,
        config=MatchingConfig(mode=ExecutionMode.VENUE_TESTNET),
        security_master=env.security_master,
        clock=lambda: now["t"],
    )
    env.seed_books(T)
    result = env.router.submit_opportunity(env.intent(T, opportunity_id="testnet"))
    for _ in range(40):
        await asyncio.sleep(0)
        now["t"] += 50 * MS
        env.router.advance(now["t"])
        if result.plan.state is PlanState.CLOSED:
            break
    env.result = result  # type: ignore[attr-defined]
    return env


async def test_venue_testnet_fills_are_labelled_and_ioc_remainders_cancelled(tmp_path: Path) -> None:
    gateways = {"binance": _FakeGateway("binance", fill_ratio=Decimal("0.6")), "okx": _FakeGateway("okx")}
    env = await _run_testnet(tmp_path, gateways)
    fills = [event for event in env.ledger.events(run_id="testnet") if event.event_type == "fill"]
    assert fills and {event.source for event in fills} == {"venue_testnet"}
    binance_child = next(order for order in env.result.plan.children if order.venue == "binance")
    assert binance_child.filled_quantity == Decimal("0.03")
    assert binance_child.reject_reason == "ioc_remainder_expired"
    env.close()


async def test_venue_testnet_lost_submit_is_resolved_by_query_without_resending(tmp_path: Path) -> None:
    gateways = {"binance": _FakeGateway("binance", lose_submit=True), "okx": _FakeGateway("okx", lose_submit=True)}
    env = await _run_testnet(tmp_path, gateways)
    assert env.result.plan.state is PlanState.CLOSED
    assert all(gateway.submits == 1 for gateway in gateways.values())
    states = [event.payload["state"] for event in env.ledger.events(run_id="testnet") if event.event_type == "order_state"]
    assert states.count("unknown") == 2
    env.close()


def test_testnet_wiring_refuses_venues_without_an_adapter() -> None:
    with pytest.raises(ValueError):
        build_testnet_gateways(object(), ("binance", "bybit"))
    with pytest.raises(ValueError):
        venue_testnet_matcher_factory({"binance": _FakeGateway("binance")}, ("binance", "okx"), security_master=None)  # type: ignore[arg-type]
