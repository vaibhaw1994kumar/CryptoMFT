"""Unit coverage for the minimum-requirements execution plane (MR-07 to MR-14).

Every component here is deterministic, so these tests assert exact states and
exact numbers rather than tolerances.
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from trading_platform_poc.minreq.acceptance import (
    CHECKS,
    MicroEnvironment,
    run_acceptance,
)
from trading_platform_poc.minreq.backtest import (
    BacktestConfig,
    BacktestRunner,
    build_dataset,
)
from trading_platform_poc.minreq.canonical import Liquidity
from trading_platform_poc.minreq.features import FeatureConfig
from trading_platform_poc.minreq.ledger import EventLedger, LedgerError
from trading_platform_poc.minreq.manifest import (
    REVISION_ENV_VAR,
    RunManifest,
    build_identity,
    compare_manifests,
)
from trading_platform_poc.minreq.matching import OrderRequest, RejectReason
from trading_platform_poc.minreq.monitoring import (
    MetricRegistry,
    monitoring_document,
)
from trading_platform_poc.minreq.riskcontrols import (
    LimitId,
    RiskCheckRequest,
    RiskEngine,
    RiskLimits,
    risk_limit_matrix,
)
from trading_platform_poc.minreq.router import (
    ORDER_STATE_MACHINE_DOC,
    PlanState,
    StateTransitionError,
    assert_plan_transition,
    client_order_id,
)
from trading_platform_poc.minreq.securitymaster import default_security_master
from trading_platform_poc.minreq.statarb import StatArbConfig
from trading_platform_poc.minreq.synthetic import SyntheticConfig

AT_NS = 1_800_000_000_000_000_000
BTC_BINANCE = "BTC-USDT.BINANCE.SPOT"
BTC_OKX = "BTC-USDT.OKX.SPOT"
GROUP = "BTC/USDT.SPOT"


@pytest.fixture()
def stack(tmp_path: Path):
    env = MicroEnvironment(tmp_path / "micro", run_id="unit")
    env.seed_books(AT_NS)
    try:
        yield env
    finally:
        env.close()


def _order(**overrides) -> OrderRequest:
    defaults = dict(
        client_order_id="unit-1",
        child_order_id="child-1",
        plan_id="plan-1",
        venue="binance",
        instrument_id=BTC_BINANCE,
        side="buy",
        order_type="market",
        quantity=Decimal("0.01"),
        price=None,
        time_in_force="ioc",
        timestamp_ns=AT_NS,
    )
    defaults.update(overrides)
    return OrderRequest(**defaults)


def _risk_request(**overrides) -> RiskCheckRequest:
    defaults = dict(
        instrument_id=BTC_BINANCE,
        venue="binance",
        side="buy",
        quantity=Decimal("0.01"),
        price=Decimal("60000"),
        notional=Decimal("600"),
        reference_price=Decimal("60000"),
        data_age_ns=0,
        book_valid=True,
    )
    defaults.update(overrides)
    return RiskCheckRequest(**defaults)


# ---------------------------------------------------------------------- MR-07
def test_normal_opportunity_fills_both_legs_and_flattens(stack) -> None:
    result = stack.router.submit_opportunity(stack.intent(AT_NS))
    stack.drain()
    plan = result.plan
    assert result.accepted, result.reason
    assert plan.state is PlanState.CLOSED
    assert len(plan.children) == 2
    assert {child.state.value for child in plan.children} == {"filled"}

    derived = stack.ledger.derive(run_id="unit")
    assert derived.net_position(BTC_BINANCE) + derived.net_position(BTC_OKX) == 0
    fees = sum(
        (position.fees for position in derived.positions.values()), Decimal(0)
    )
    assert fees > 0


def test_matcher_walks_depth_and_charges_taker_fees(stack) -> None:
    outcome = stack.matcher.submit(_order(quantity=Decimal("7")))
    assert outcome.accepted, outcome.reject_reason
    assert outcome.fills
    fill = outcome.fills[0]
    assert fill.liquidity is Liquidity.TAKER
    # Seven units cannot come from the five-unit top level alone.
    assert fill.levels_consumed >= 2
    assert fill.price > Decimal("60000.00")


def test_matcher_rejects_a_duplicate_client_order_id(stack) -> None:
    request = _order(client_order_id="unit-dup")
    assert stack.matcher.submit(request).accepted
    repeat = stack.matcher.submit(request)
    assert not repeat.accepted
    assert repeat.reject_reason == RejectReason.DUPLICATE_CLIENT_ORDER_ID.value


def test_matcher_rejects_orders_on_an_unavailable_venue(stack) -> None:
    stack.matcher.set_venue_available("binance", False)
    outcome = stack.matcher.submit(_order(client_order_id="unit-down"))
    assert not outcome.accepted
    assert outcome.reject_reason == RejectReason.VENUE_UNAVAILABLE.value


def test_client_order_ids_are_deterministic_across_restart() -> None:
    first = client_order_id("run", "plan", "binance", "buy", 0)
    second = client_order_id("run", "plan", "binance", "buy", 0)
    third = client_order_id("run", "plan", "binance", "buy", 1)
    assert first == second
    assert first != third


def test_plan_state_machine_rejects_illegal_transitions() -> None:
    with pytest.raises(StateTransitionError):
        assert_plan_transition(PlanState.CLOSED, PlanState.DISPATCHED)
    assert_plan_transition(PlanState.CREATED, PlanState.RISK_RESERVED)
    assert set(ORDER_STATE_MACHINE_DOC["parent_plan"]) == {
        state.value for state in PlanState
    }
    assert ORDER_STATE_MACHINE_DOC["child_order"]
    assert ORDER_STATE_MACHINE_DOC["notes"]


# ---------------------------------------------------------------------- MR-10
def test_risk_reservation_is_all_or_nothing() -> None:
    engine = RiskEngine(
        RiskLimits(max_opportunity_notional=Decimal("4000")),
        default_security_master(),
    )
    legs = [
        _risk_request(notional=Decimal("3000")),
        _risk_request(
            instrument_id=BTC_OKX,
            venue="okx",
            side="sell",
            notional=Decimal("3000"),
            price=Decimal("60100"),
            reference_price=Decimal("60100"),
        ),
    ]
    reservation, decisions = engine.reserve("plan-1", legs, AT_NS)
    assert reservation is None
    assert any(not decision.approved for decision in decisions)
    assert engine.utilization()["gross_notional"] == "0"
    assert engine.active_reservations() == ()


def test_risk_blocks_stale_data_and_invalid_books() -> None:
    engine = RiskEngine(RiskLimits(), default_security_master())
    stale = engine.check(_risk_request(data_age_ns=10_000_000_000), AT_NS)
    assert not stale.approved
    assert stale.limit_id == LimitId.STALE_MARKET_DATA.value

    invalid = engine.check(_risk_request(book_valid=False), AT_NS)
    assert not invalid.approved
    assert invalid.limit_id == LimitId.INVALID_BOOK.value


def test_kill_switch_blocks_new_risk_but_allows_reduction() -> None:
    engine = RiskEngine(RiskLimits(), default_security_master())
    engine.kill_switch.activate(AT_NS, "drill")
    blocked = engine.check(_risk_request(), AT_NS)
    assert not blocked.approved
    assert blocked.limit_id == LimitId.KILL_SWITCH.value
    assert engine.check(_risk_request(risk_reducing=True), AT_NS).approved


def test_message_rate_limit_binds_and_releases() -> None:
    engine = RiskEngine(
        RiskLimits(max_messages_per_second=2), default_security_master()
    )
    for _ in range(2):
        engine.record_message(AT_NS)
    assert engine.message_headroom(AT_NS) == 0
    blocked = engine.check(_risk_request(), AT_NS)
    assert not blocked.approved
    assert blocked.limit_id == LimitId.MAX_MESSAGE_RATE.value
    assert engine.message_headroom(AT_NS + 1_000_000_001) == 2


def test_risk_limit_matrix_documents_every_limit() -> None:
    matrix = risk_limit_matrix()
    assert {row["limit_id"] for row in matrix} == {
        limit.value for limit in LimitId
    }
    for row in matrix:
        assert row["owner"]
        assert row["control"]
        assert row["order_state_effect"]
        assert row["scope"]
        assert row["severity"]


# ---------------------------------------------------------------------- MR-11
def test_ledger_is_append_only_and_rebuildable(stack) -> None:
    stack.router.submit_opportunity(stack.intent(AT_NS))
    stack.drain()
    digest = stack.ledger.chain_digest(run_id="unit")
    state_hash = stack.ledger.derive(run_id="unit").state_hash()
    stored = next(iter(stack.ledger.events(run_id="unit")))

    assert stack.ledger.append(stored) is False
    tampered = replace(stored, payload={"tampered": True})
    with pytest.raises(LedgerError):
        stack.ledger.append(tampered)

    reopened = EventLedger(stack.ledger_path, run_id="unit")
    try:
        assert reopened.chain_digest(run_id="unit") == digest
        assert reopened.derive(run_id="unit").state_hash() == state_hash
    finally:
        reopened.close()


def test_reconciliation_detects_an_injected_break(stack) -> None:
    stack.router.submit_opportunity(stack.intent(AT_NS))
    stack.drain()
    clean = stack.ledger.reconcile(
        stack.matcher.venue_snapshot(), AT_NS, run_id="unit"
    )
    assert clean.clean, [item.to_dict() for item in clean.breaks]

    stack.matcher.inject_position_break(BTC_BINANCE, "binance", Decimal("0.01"))
    broken = stack.ledger.reconcile(
        stack.matcher.venue_snapshot(), AT_NS, run_id="unit"
    )
    assert not broken.clean
    assert broken.breaks[0].scope == "position"


# ---------------------------------------------------------------------- MR-12
def test_monitoring_document_assigns_an_owner_to_every_alert() -> None:
    document = monitoring_document()
    assert document["metrics"]
    assert document["alerts"]
    for metric in document["metrics"]:
        assert metric["owner"], metric["name"]
        assert metric["description"]
        assert metric["source"]
    for alert in document["alerts"]:
        assert alert["owner"], alert["alert"]
        assert alert["severity"]
        assert alert["response"]


def test_metric_registry_escalates_on_threshold_breach() -> None:
    registry = MetricRegistry()
    assert registry.observe("feed_gap_count", 0).status == "ok"
    registry.observe("feed_gap_count", 99)
    assert registry.worst_status() == "critical"
    alerts = registry.alerts()
    assert any(alert["metric"] == "feed_gap_count" for alert in alerts)
    assert all(alert["owner"] for alert in alerts)


# ---------------------------------------------------------------------- MR-13
def test_run_manifest_identity_ignores_wall_clock() -> None:
    def build(started: int) -> RunManifest:
        return RunManifest(
            run_id="run",
            mode="backtest",
            started_ns=started,
            seed=1,
            build={"package_version": "1", "git_revision": "abc"},
            config={"a": 1},
            component_versions={"schema": "2"},
            dataset={"checksum": "x", "created_at": f"t{started}"},
            results={"pnl": "1"},
            finished_ns=started + 10,
        )

    first, second = build(1), build(999)
    assert first.identity() == second.identity()
    assert compare_manifests(first.to_dict(), second.to_dict())["identical"]


# ------------------------------------------------------------- end-to-end run
def test_backtest_is_reproducible_and_reconciles(tmp_path: Path) -> None:
    def once(tag: str):
        root = tmp_path / tag
        pipeline, dataset = build_dataset(
            root / "dataset", synthetic=SyntheticConfig(seed=42, steps=220)
        )
        config = BacktestConfig(
            run_id="repro",
            seed=42,
            strategy=StatArbConfig(
                economic_group=GROUP,
                entry_z=Decimal("1.2"),
                exit_z=Decimal("0.3"),
                target_quantity=Decimal("0.05"),
                min_samples=10,
                cooldown_ns=0,
            ),
            feature=FeatureConfig(economic_group=GROUP, min_samples=10),
        )
        runner = BacktestRunner(
            config,
            pipeline.venue_streams(),
            root / "run",
            dataset_info=dataset,
        )
        return runner.run()

    first, second = once("a"), once("b")
    assert int(first.tca.totals["plans"]) > 0
    assert first.manifest.identity() == second.manifest.identity()
    assert first.state_hash == second.state_hash
    assert first.reconciliation["breaks"] == []

    for name in ("run_manifest.json", "tca.json", "tca.csv", "tca.md"):
        assert (first.output_dir / name).is_file()
    payload = json.loads((first.output_dir / "tca.json").read_text("utf-8"))
    assert payload["totals"]["plans"] == first.tca.totals["plans"]


def test_acceptance_suite_registers_all_fourteen_tests() -> None:
    assert [test_id for test_id, _ in CHECKS] == [
        f"AT-{index:02d}" for index in range(1, 15)
    ]


def test_build_identity_reports_the_installed_distribution_version() -> None:
    """A report that cannot name its own build is not reproducible evidence."""

    identity = build_identity()
    assert identity["package_version"] != "0.0.0+local"
    assert identity["package_version"][0].isdigit()


def test_build_identity_accepts_a_stamped_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Container and wheel installs have no git checkout to interrogate."""

    monkeypatch.setenv(REVISION_ENV_VAR, "0123456789abcdef")
    assert build_identity()["git_revision"] == "0123456789abcdef"

    monkeypatch.setenv(REVISION_ENV_VAR, "   ")
    assert build_identity()["git_revision"] != "   "


def test_acceptance_report_records_defects_limitations_and_sign_off(
    tmp_path: Path,
) -> None:
    """Section 10 requires more than results: owners, limitations, remediation."""

    report = run_acceptance(tmp_path / "acceptance", only=["AT-14"])
    payload = report.to_dict()

    assert payload["defects"] == []
    assert {item["limitation_id"] for item in payload["limitations"]} == {
        "LIM-01",
        "LIM-02",
    }
    for item in payload["limitations"]:
        assert item["owner"]
        assert item["remediation"]
        assert item["remediation_date"]
    assert [item["release_label"] for item in payload["sign_off"]] == [
        "Backtest-ready",
        "Paper-ready",
        "Live-ready",
    ]

    markdown = (tmp_path / "acceptance" / "acceptance_report.md").read_text("utf-8")
    assert "## Defect log" in markdown
    assert "## Accepted limitations" in markdown
    assert "## Sign-off" in markdown
    assert "2026-09-30" in markdown


def test_paper_mode_checks_carry_the_feed_limitation(tmp_path: Path) -> None:
    report = run_acceptance(tmp_path / "acceptance", only=["AT-12"])
    check = next(item for item in report.checks if item.test_id == "AT-12")
    assert check.limitations
    assert "deterministic synthetic generator" in check.limitations


@pytest.mark.slow
def test_full_acceptance_suite_passes(tmp_path: Path) -> None:
    report = run_acceptance(tmp_path / "acceptance")
    failed = [check.test_id for check in report.checks if not check.passed]
    assert not failed, json.dumps(failed)
