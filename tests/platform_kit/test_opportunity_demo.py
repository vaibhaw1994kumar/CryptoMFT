"""End-to-end coverage for the offline opportunity demo and simulator."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

import hft_platform_kit
from trading_platform_poc.cli import build_parser, run_opportunity_demo
from trading_platform_poc.exchange_adapter import ExchangeAdapter
from trading_platform_poc.opportunity_store import OpportunityStore
from trading_platform_poc.simulated_venue import (
    FillStep,
    SimulatedVenue,
    VenueScript,
)


def test_simulated_venue_satisfies_the_adapter_protocol() -> None:
    venue = SimulatedVenue("binance")
    assert isinstance(venue, ExchangeAdapter)
    assert venue.venue == "binance"


@pytest.mark.asyncio
async def test_resubmitting_the_same_order_never_duplicates() -> None:
    venue = SimulatedVenue(
        "binance",
        script=VenueScript(fills=(FillStep(Decimal("1"), Decimal("100")),)),
    )
    order = _order("dup-1")

    first = await venue.submit_order(order)
    second = await venue.submit_order(order)

    assert first.exchange_order_id == second.exchange_order_id
    assert len(await venue.open_orders()) == 1


@pytest.mark.asyncio
async def test_scripted_fills_progress_one_poll_at_a_time() -> None:
    venue = SimulatedVenue(
        "okx",
        script=VenueScript(
            fills=(
                FillStep(Decimal("0.4"), Decimal("100")),
                FillStep(Decimal("0.6"), Decimal("110")),
            )
        ),
    )
    submitted = await venue.submit_order(_order("progress-1"))

    partial = await venue.query_order(
        "BTCUSDT",
        client_order_id=submitted.client_order_id,
    )
    assert partial.status == "PARTIALLY_FILLED"
    assert partial.filled_quantity == Decimal("0.4")

    complete = await venue.query_order(
        "BTCUSDT",
        client_order_id=submitted.client_order_id,
    )
    assert complete.status == "FILLED"
    assert complete.filled_quantity == Decimal("1")
    # Volume weighted, not last price.
    assert complete.average_fill_price == Decimal("106")
    assert venue.positions() == {"BTCUSDT": Decimal("1")}


@pytest.mark.asyncio
async def test_cancel_stops_further_fills() -> None:
    venue = SimulatedVenue(
        "okx",
        script=VenueScript(fills=(FillStep(Decimal("1"), Decimal("100")),)),
    )
    submitted = await venue.submit_order(_order("cancel-1"))
    await venue.cancel_order(
        "BTCUSDT",
        client_order_id=submitted.client_order_id,
    )

    polled = await venue.query_order(
        "BTCUSDT",
        client_order_id=submitted.client_order_id,
    )
    assert polled.status == "CANCELED"
    assert polled.filled_quantity == Decimal(0)


@pytest.mark.asyncio
async def test_demo_happy_path_closes_flat_and_balanced() -> None:
    payload = await run_opportunity_demo(scenario="filled")
    scenario = payload["scenarios"][0]

    assert scenario["state"] == "closed"
    assert scenario["residual_quantity"] == "0"
    assert scenario["duplicate_free"] is True
    assert scenario["replay_is_idempotent"] is True
    assert scenario["unwinds"] == []

    reconciliation = scenario["reconciliation"]
    assert reconciliation["balanced"] is True
    assert reconciliation["net_position"] == "0"
    assert reconciliation["open_order_count"] == 0
    assert reconciliation["risk_released"] is True
    assert Decimal(reconciliation["realized_pnl"]) > 0

    # Every position view must agree, which is the point of the check.
    assert (
        reconciliation["exchange_positions"]
        == reconciliation["local_positions"]
        == reconciliation["risk_positions"]
    )
    assert scenario["transitions"][0] == "created"
    assert scenario["transitions"][-1] == "closed"


@pytest.mark.asyncio
async def test_demo_partial_fill_hedges_the_residual() -> None:
    payload = await run_opportunity_demo(scenario="partial")
    scenario = payload["scenarios"][0]

    assert scenario["state"] == "closed"
    assert "partially_filled" in scenario["transitions"]
    assert "hedge_required" in scenario["transitions"]
    assert "unwinding" in scenario["transitions"]

    unwind = scenario["unwinds"][0]
    assert unwind["reason"] == "residual_exposure"
    assert Decimal(unwind["quantity"]) == Decimal("0.6")
    # Venue priority puts okx first, so selection must be deterministic.
    assert unwind["venue"] == "okx"

    reconciliation = scenario["reconciliation"]
    assert Decimal(reconciliation["net_position"]) == 0
    assert reconciliation["balanced"] is True


@pytest.mark.asyncio
async def test_demo_reject_is_classified_and_never_retried() -> None:
    payload = await run_opportunity_demo(scenario="reject")
    scenario = payload["scenarios"][0]

    assert "rejected" in scenario["transitions"]
    rejected = next(
        leg for leg in scenario["legs"] if leg["state"] == "rejected"
    )
    assert rejected["venue"] == "binance"
    assert rejected["reject_reason"] == "invalid_order"
    assert rejected["reject_action"] == "manual_investigation"
    # An invalid order must not be resubmitted.
    assert rejected["attempts"] == 1
    assert Decimal(scenario["reconciliation"]["net_position"]) == 0


@pytest.mark.asyncio
async def test_demo_runs_every_scenario_by_default() -> None:
    payload = await run_opportunity_demo()
    names = {scenario["scenario"] for scenario in payload["scenarios"]}
    assert names == {"filled", "partial", "reject"}
    assert payload["mode"] == "simulated_venues"


@pytest.mark.asyncio
async def test_demo_output_is_json_serialisable() -> None:
    payload = await run_opportunity_demo(scenario="filled")
    assert json.loads(json.dumps(payload)) == payload


@pytest.mark.asyncio
async def test_demo_persists_a_durable_audit_trail(tmp_path: Path) -> None:
    store_path = tmp_path / "ledger.db"
    payload = await run_opportunity_demo(
        scenario="filled",
        store_path=store_path,
    )
    scenario = payload["scenarios"][0]
    opportunity_id = scenario["opportunity_id"]

    written = tmp_path / "ledger-filled.db"
    assert written.is_file()

    with OpportunityStore(str(written)) as store:
        record = store.load_opportunity(opportunity_id)
        assert record is not None
        assert record.terminal is True
        assert store.open_opportunity_ids() == ()

        events = store.load_events(opportunity_id)
        types = [event.event_type for event in events]
        assert types[0] == "opportunity_created"
        assert "reconciliation" in types
        # Write ahead: intent is durable before the venue is contacted.
        assert types.index("leg_submitting") < types.index(
            "leg_acknowledged"
        )
        sequences = [event.sequence for event in events]
        assert sequences == sorted(sequences)


def test_cli_exposes_the_opportunity_demo() -> None:
    args = build_parser().parse_args(
        ["opportunity-demo", "--scenario", "partial"]
    )
    assert args.command == "opportunity-demo"
    assert args.scenario == "partial"
    assert args.store is None


def test_cli_rejects_an_unknown_scenario() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["opportunity-demo", "--scenario", "nope"])


def test_sdk_exports_the_state_machine_surface() -> None:
    for name in (
        "OpportunityCoordinator",
        "OpportunityStore",
        "OpportunityRequest",
        "LegRequest",
        "MarketQuote",
        "ResidualPolicy",
        "OpportunityTimeouts",
        "SimulatedVenue",
        "classify_reject",
    ):
        assert name in hft_platform_kit.__all__
        assert hasattr(hft_platform_kit, name)


def _order(client_order_id: str):
    from uuid import uuid4

    from trading_platform_poc.models import (
        Order,
        OrderState,
        OrderType,
        Side,
    )

    return Order(
        order_id=uuid4(),
        intent_id=uuid4(),
        client_order_id=client_order_id,
        symbol="BTCUSDT",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("1"),
        price=Decimal("100"),
        state=OrderState.RISK_APPROVED,
        created_timestamp_ns=1,
    )


@pytest.mark.asyncio
async def test_lifecycle_runs_with_native_dependencies_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduce a locked-down Windows host and prove the machine still runs.

    A consumer hit ``ImportError: DLL load failed ... An Application Control
    policy has blocked this file`` when pyarrow's native library was blocked.
    The opportunity state machine must never depend on such a module.
    """

    import builtins

    blocked = {"pyarrow", "numpy", "hftbacktest", "uvloop"}
    real_import = builtins.__import__

    def guard(name: str, *args: object, **kwargs: object):
        root = name.split(".")[0]
        if root in blocked or name.endswith("._ems"):
            raise ImportError(
                f"DLL load failed while importing {root}: "
                "An Application Control policy has blocked this file."
            )
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)

    # Force a genuine re-import rather than a cache hit.
    for module in (
        "trading_platform_poc.opportunity",
        "trading_platform_poc.opportunity_store",
        "trading_platform_poc.simulated_venue",
    ):
        monkeypatch.delitem(__import__("sys").modules, module, raising=False)

    from trading_platform_poc.opportunity import (
        LegRequest as Leg,
        MarketQuote as Quote,
        OpportunityCoordinator as Coordinator,
        OpportunityRequest as Request,
    )
    from trading_platform_poc.opportunity_store import (
        OpportunityStore as Store,
    )
    from trading_platform_poc.simulated_venue import (
        FillStep as Step,
        SimulatedVenue as Venue,
        VenueScript as Script,
    )
    from trading_platform_poc.models import Side as OrderSide

    adapters = {
        "binance": Venue(
            "binance",
            script=Script(fills=(Step(Decimal("1"), Decimal("100000")),)),
        ),
        "okx": Venue(
            "okx",
            script=Script(fills=(Step(Decimal("1"), Decimal("100040")),)),
        ),
    }
    with Store(":memory:") as store:
        coordinator = Coordinator(adapters, store)
        for venue in adapters:
            coordinator.update_quote(
                Quote(
                    venue=venue,
                    symbol="BTCUSDT",
                    bid_price=Decimal("99990"),
                    ask_price=Decimal("100010"),
                    fee_bps=Decimal("1"),
                )
            )
        result = await coordinator.execute(
            Request(
                strategy_id="portability",
                strategy_version="v1",
                signal_id="blocked-dll",
                canonical_symbol="BTCUSDT",
                legs=(
                    Leg(
                        "binance",
                        "BTCUSDT",
                        OrderSide.BUY,
                        Decimal("1"),
                        Decimal("100000"),
                    ),
                    Leg(
                        "okx",
                        "BTCUSDT",
                        OrderSide.SELL,
                        Decimal("1"),
                        Decimal("100040"),
                    ),
                ),
            )
        )

    assert result.state.value == "closed"
    assert result.reconciliation is not None
    assert result.reconciliation.balanced is True


def test_cli_stdout_is_machine_readable(tmp_path: Path) -> None:
    """Diagnostics belong on stderr so results can be piped."""

    import subprocess
    import sys

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "trading_platform_poc.cli",
            "opportunity-demo",
            "--scenario",
            "filled",
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=True,
    )

    payload = json.loads(completed.stdout)
    assert payload["scenarios"][0]["state"] == "closed"
    # The lifecycle logs must still be emitted, just not on stdout.
    assert "opportunity_state" in completed.stderr
