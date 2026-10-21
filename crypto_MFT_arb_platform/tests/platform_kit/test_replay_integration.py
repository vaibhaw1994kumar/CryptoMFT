import json
from decimal import Decimal
from uuid import UUID

from trading_platform_poc.config import (
    AppConfig,
    PaperConfig,
    RiskConfig,
    StrategyConfig,
)
from trading_platform_poc.engine import TradingEngine
from trading_platform_poc.replay import ReplaySource
from trading_platform_poc.reporting import write_report


RUN_ID = UUID("00000000-0000-0000-0000-000000000001")


def envelope(timestamp_ns: int, message: dict) -> str:
    return json.dumps(
        {
            "receive_timestamp_ns": timestamp_ns,
            "message": message,
        }
    )


def replay_dataset(tmp_path):
    path = tmp_path / "raw.jsonl"
    messages = [
        envelope(
            1_000_000_000,
            {
                "source": "rest_snapshot",
                "symbol": "BTCUSDT",
                "data": {
                    "lastUpdateId": 10,
                    "bids": [["100", "2"]],
                    "asks": [["101", "2"]],
                },
            },
        ),
        envelope(
            2_000_000_000,
            {
                "data": {
                    "e": "depthUpdate",
                    "E": 2_000,
                    "s": "BTCUSDT",
                    "U": 11,
                    "u": 11,
                    "b": [["100", "2"]],
                    "a": [["101", "2"]],
                }
            },
        ),
        envelope(
            3_000_000_000,
            {
                "data": {
                    "e": "trade",
                    "E": 3_000,
                    "s": "BTCUSDT",
                    "t": 1,
                    "p": "100",
                    "q": "1",
                    "T": 3_000,
                    "m": False,
                }
            },
        ),
        envelope(
            4_000_000_000,
            {
                "data": {
                    "e": "trade",
                    "E": 4_000,
                    "s": "BTCUSDT",
                    "t": 2,
                    "p": "101",
                    "q": "1",
                    "T": 4_000,
                    "m": True,
                }
            },
        ),
    ]
    path.write_text("\n".join(messages) + "\n", encoding="utf-8")
    return path


def run_replay(path, output):
    config = AppConfig(
        risk=RiskConfig(max_net_position=Decimal("1")),
        strategy=StrategyConfig(
            spread_threshold_bps=1,
            order_notional=Decimal("10"),
            quote_cooldown_ms=1000,
        ),
    )
    engine = TradingEngine(config)
    for event in ReplaySource(path, RUN_ID).events():
        engine.process(event)
    engine.close(4_000_000_000)
    return write_report(output, RUN_ID, "replay", engine)


def test_replay_is_deterministic_and_generates_pnl(tmp_path) -> None:
    dataset = replay_dataset(tmp_path)

    first = run_replay(dataset, tmp_path / "report-1")
    second = run_replay(dataset, tmp_path / "report-2")

    assert first["result_hash"] == second["result_hash"]
    assert first["fill_count"] == 2
    assert first["portfolio"]["position"] == "0.000000"
    assert Decimal(first["portfolio"]["net_pnl"]) > 0


def test_replay_gap_skips_trades_until_embedded_snapshot(tmp_path) -> None:
    path = tmp_path / "gap-recovery.jsonl"
    messages = [
        envelope(
            1_000_000_000,
            {
                "source": "rest_snapshot",
                "symbol": "BTCUSDT",
                "data": {
                    "lastUpdateId": 10,
                    "bids": [["100", "2"]],
                    "asks": [["101", "2"]],
                },
            },
        ),
        envelope(
            2_000_000_000,
            {
                "data": {
                    "e": "depthUpdate",
                    "E": 2_000,
                    "s": "BTCUSDT",
                    "U": 11,
                    "u": 11,
                    "b": [],
                    "a": [],
                }
            },
        ),
        envelope(
            3_000_000_000,
            {
                "data": {
                    "e": "depthUpdate",
                    "E": 3_000,
                    "s": "BTCUSDT",
                    "U": 13,
                    "u": 13,
                    "b": [],
                    "a": [],
                }
            },
        ),
        envelope(
            4_000_000_000,
            {
                "data": {
                    "e": "trade",
                    "E": 4_000,
                    "s": "BTCUSDT",
                    "t": 1,
                    "p": "100",
                    "q": "1",
                    "T": 4_000,
                    "m": False,
                }
            },
        ),
        envelope(
            5_000_000_000,
            {
                "source": "rest_snapshot",
                "symbol": "BTCUSDT",
                "data": {
                    "lastUpdateId": 13,
                    "bids": [["100", "2"]],
                    "asks": [["101", "2"]],
                },
            },
        ),
        envelope(
            6_000_000_000,
            {
                "data": {
                    "e": "depthUpdate",
                    "E": 6_000,
                    "s": "BTCUSDT",
                    "U": 14,
                    "u": 14,
                    "b": [],
                    "a": [],
                }
            },
        ),
        envelope(
            7_000_000_000,
            {
                "data": {
                    "e": "trade",
                    "E": 7_000,
                    "s": "BTCUSDT",
                    "t": 2,
                    "p": "100",
                    "q": "1",
                    "T": 7_000,
                    "m": False,
                }
            },
        ),
    ]
    path.write_text("\n".join(messages) + "\n", encoding="utf-8")
    engine = TradingEngine(
        AppConfig(
            paper=PaperConfig(
                acknowledgement_latency_ms=0,
                cancellation_latency_ms=0,
            ),
            risk=RiskConfig(max_net_position=Decimal("1")),
            strategy=StrategyConfig(
                spread_threshold_bps=1,
                order_notional=Decimal("10"),
                quote_cooldown_ms=0,
            ),
        )
    )

    events = list(ReplaySource(path, RUN_ID).events())
    assert engine.process(events[0])
    assert engine.process(events[1])
    assert len(engine.oms.open_orders()) == 2
    assert not engine.process(events[2])
    assert not engine.market_data_healthy
    assert not engine.oms.open_orders()
    pnl_at_gap = engine.portfolio.net_realized_pnl

    assert not engine.process(events[3])
    assert not engine.portfolio.fills
    assert engine.portfolio.net_realized_pnl == pnl_at_gap
    assert engine.process(events[4])
    assert engine.market_data_healthy
    assert engine.process(events[5])
    assert engine.process(events[6])
    assert len(engine.portfolio.fills) == 1


async def test_replay_real_time_pacing_preserves_event_timestamps(
    tmp_path,
) -> None:
    dataset = replay_dataset(tmp_path)
    sleeps = []

    async def capture_sleep(delay: float) -> None:
        sleeps.append(delay)

    events = [
        event
        async for event in ReplaySource(dataset, RUN_ID).paced_events(
            "real-time",
            sleep=capture_sleep,
        )
    ]

    assert sleeps == [1.0, 1.0, 1.0]
    assert [event.receive_timestamp_ns for event in events] == [
        1_000_000_000,
        2_000_000_000,
        3_000_000_000,
        4_000_000_000,
    ]


async def test_replay_max_speed_does_not_sleep(tmp_path) -> None:
    dataset = replay_dataset(tmp_path)

    async def fail_sleep(delay: float) -> None:
        raise AssertionError(f"unexpected sleep: {delay}")

    events = [
        event
        async for event in ReplaySource(dataset, RUN_ID).paced_events(
            "max",
            sleep=fail_sleep,
        )
    ]
    assert len(events) == 4
