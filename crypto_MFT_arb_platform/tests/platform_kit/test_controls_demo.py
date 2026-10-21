"""End-to-end coverage for the offline execution controls demo.

The demo is what a client actually runs, so these tests assert on its
observable output rather than on internals: if the narrative claims a
rollback was complete, the numbers in the same payload must prove it.
"""

from __future__ import annotations

import json

import pytest

import hft_platform_kit
from trading_platform_poc.cli import build_parser
from trading_platform_poc.controls_demo import (
    CONTROL_SCENARIOS,
    run_controls_demo,
)
from trading_platform_poc.execution_controls import ControlKind, OrderStage


def scenario(name: str) -> dict:
    payload = run_controls_demo(scenario=name)
    assert payload["summary"]["scenarios_run"] == 1
    return payload["scenarios"][0]


def test_every_scenario_runs() -> None:
    payload = run_controls_demo()
    names = [item["scenario"] for item in payload["scenarios"]]
    assert names == list(CONTROL_SCENARIOS)
    assert payload["summary"]["matrix_cells"] == (
        len(OrderStage) * len(ControlKind)
    )


def test_the_demo_output_is_json_serialisable() -> None:
    json.dumps(run_controls_demo())


def test_the_demo_is_deterministic() -> None:
    """Identical runs must produce identical output for a client demo."""

    assert json.dumps(run_controls_demo(), sort_keys=True) == json.dumps(
        run_controls_demo(), sort_keys=True
    )


def test_an_unknown_scenario_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown scenario"):
        run_controls_demo(scenario="not-a-scenario")


def test_venue_rate_limit_demo_queues_the_overflow() -> None:
    result = scenario("venue-rate-limit")

    assert result["admitted"] == 5
    assert result["queued"] == 3
    assert result["released_after_window"] == ["q5", "q6", "q7"]
    assert result["dashboard_while_binding"]["binding"]


def test_global_throttle_demo_releases_by_priority() -> None:
    result = scenario("global-throttle")

    assert result["binding_control"] == ControlKind.GLOBAL_THROTTLE.value
    assert result["release_order"] == [
        "market_making",
        "quote_refresh",
        "research",
    ]


def test_stale_feed_demo_blocks_quoting_but_not_hedging() -> None:
    result = scenario("stale-feed")
    actions = {
        step["label"]: step["action"] for step in result["after_staleness"]
    }

    assert actions["new-order"] == "reject"
    assert actions["replace"] == "reject"
    assert actions["resting"] == "cancel"
    assert actions["cancel"] == "admit"
    assert actions["hedge"] == "admit"
    assert actions["other-venue"] == "admit"

    fresh = {
        step["label"]: step["action"] for step in result["before_staleness"]
    }
    assert fresh["new-order-fresh"] == "admit"
    assert fresh["resting-fresh"] == "admit"


def test_kill_switch_demo_still_permits_exposure_reduction() -> None:
    result = scenario("kill-switch")
    actions = {step["stage"]: step["action"] for step in result["steps"]}

    assert actions["new"] == "reject"
    assert actions["risk_approved_waiting"] == "cancel_admission"
    assert actions["resting"] == "mass_cancel"
    assert actions["partial_fill"] == "hedge_only"
    assert actions["cancel_request"] == "admit"
    assert actions["emergency_hedge"] == "admit"
    assert actions["forced_liquidation"] == "admit"

    assert result["strategy_clear_refused"]
    assert result["cleared_by_administrator"]


def test_kill_switch_demo_covers_every_order_stage() -> None:
    result = scenario("kill-switch")
    assert {step["stage"] for step in result["steps"]} == {
        stage.value for stage in OrderStage
    }


def test_atomic_reservation_demo_rolls_back_completely() -> None:
    result = scenario("atomic-reservation")

    assert result["failed_attempt"]
    assert result["rollback_was_complete"]
    assert (
        result["capacity_before_failed_attempt"]
        == result["capacity_after_failed_attempt"]
    )
    assert result["after_release"]["firm"] == 6
    assert result["granted"]["hedge_messages"] == 2


def test_control_matrix_demo_publishes_every_cell() -> None:
    result = scenario("control-matrix")

    assert set(result["matrix"]) == {stage.value for stage in OrderStage}
    for row in result["matrix"].values():
        assert set(row) == {kind.value for kind in ControlKind}


def test_the_cli_exposes_the_controls_demo() -> None:
    parser = build_parser()
    args = parser.parse_args(["controls-demo", "--scenario", "kill-switch"])
    assert args.command == "controls-demo"
    assert args.scenario == "kill-switch"

    default = parser.parse_args(["controls-demo"])
    assert default.scenario == "all"


def test_the_cli_rejects_an_unknown_scenario() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["controls-demo", "--scenario", "nope"])


def test_the_control_plane_is_exported_from_the_sdk() -> None:
    for name in (
        "ExecutionControlPlane",
        "ReservationLedger",
        "KillSwitch",
        "MessageBudget",
        "CONTROL_MATRIX",
        "OrderStage",
        "ControlAction",
    ):
        assert name in hft_platform_kit.__all__
        assert hasattr(hft_platform_kit, name)
