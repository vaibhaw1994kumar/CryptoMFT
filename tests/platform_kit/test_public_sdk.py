import asyncio
from decimal import Decimal

import hft_platform_kit as hft


def test_public_api_exports_versioned_typed_components() -> None:
    assert hft.__version__ == "0.3.0"
    assert hft.AppConfig is not None
    assert hft.TradingEngine is not None
    assert hft.Strategy is not None
    assert hft.ExecutionVenue is not None
    assert hft.MarketDataSource is not None
    assert hft.DataQualityValidator is not None
    assert hft.DataQualityConfig is not None
    assert hft.BinanceSpotTestnetGateway is not None
    assert hft.BinanceExecutionService is not None
    assert hft.ScheduleResult is not None
    assert hft.ScheduleState.RECOVERY_REQUIRED.value == "recovery_required"
    assert hft.ScheduleState.UNWIND_REQUIRED.value == "unwind_required"
    assert hft.ScheduleState.RECOVERED.value == "recovered"


def test_engine_accepts_custom_strategy() -> None:
    class NoOpStrategy:
        strategy_id = "no_op"
        strategy_version = "1.0.0"

        def evaluate(self, book, timestamp_ns, open_orders):
            return ()

        def should_cancel(self, book):
            return False

    strategy = NoOpStrategy()
    engine = hft.create_engine(strategy=strategy)

    assert engine.strategy is strategy


def test_engine_preserves_falsy_injected_strategy() -> None:
    class FalsyStrategy:
        strategy_id = "falsy"
        strategy_version = "1.0.0"

        def __bool__(self):
            return False

        def evaluate(self, book, timestamp_ns, open_orders):
            return ()

        def should_cancel(self, book):
            return False

    strategy = FalsyStrategy()
    engine = hft.create_engine(strategy=strategy)

    assert engine.strategy is strategy


def test_default_config_remains_usable_for_library_consumers() -> None:
    config = hft.AppConfig()

    assert config.symbol == "BTCUSDT"
    assert config.risk.max_order_notional == Decimal("1000")


def test_custom_strategy_example_executes() -> None:
    namespace: dict[str, object] = {}
    exec(
        compile(
            open("examples/custom_strategy.py", encoding="utf-8").read(),
            "examples/custom_strategy.py",
            "exec",
        ),
        namespace,
    )
    assert namespace["engine"].strategy.strategy_id == "no_op"


def test_every_declared_export_resolves() -> None:
    """A name in ``__all__`` that does not exist breaks ``import *``."""

    unresolved = [name for name in hft.__all__ if not hasattr(hft, name)]
    assert unresolved == []
    assert len(hft.__all__) == len(set(hft.__all__))


def test_control_decision_tables_are_exported_as_data() -> None:
    """The tables are the audit surface; hiding them defeats the point.

    A risk committee has to be able to reproduce a transition without
    reading the evaluator, so every table the evaluator consults is
    part of the public API.
    """

    assert hft.CONTROL_MATRIX is not None
    assert hft.CONTROL_PRECEDENCE is not None
    assert hft.EXEMPT_PRIORITIES is not None
    assert hft.RISK_REDUCING_STAGES is not None

    controls = [control for control, _ in hft.CONTROL_PRECEDENCE]
    assert controls[0] is hft.ControlKind.KILL_SWITCH
    assert set(controls) == set(hft.ControlKind)

    # Every control named in the precedence table must be answered for
    # every order state, or evaluation would fall through a hole.
    for stage, row in hft.CONTROL_MATRIX.items():
        assert set(row) == set(controls), stage
