from datetime import datetime, timezone
from uuid import UUID

from tardis_dev import Response

from trading_platform_poc.models import EventType
from trading_platform_poc.tardis_integration import TardisBinanceSource


async def test_tardis_source_normalizes_mocked_async_replay(tmp_path) -> None:
    captured = {}

    async def replay_fn(**kwargs):
        captured.update(kwargs)
        yield Response(
            datetime(2025, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc),
            {
                "e": "trade",
                "E": 1_735_787_045_123,
                "s": "BTCUSDT",
                "t": 77,
                "p": "100.25",
                "q": "0.5",
                "T": 1_735_787_045_123,
                "m": False,
            },
        )

    source = TardisBinanceSource(
        UUID("00000000-0000-0000-0000-000000000001"),
        from_date="2025-01-02",
        to_date="2025-01-03",
        symbols=["BTCUSDT"],
        api_key="",
        cache_dir=tmp_path / "cache",
        replay_fn=replay_fn,
    )

    results = [item async for item in source.events()]

    assert len(results) == 1
    raw, event = results[0]
    assert raw["t"] == 77
    assert event.event_type is EventType.TRADE
    assert event.receive_timestamp_ns == 1_735_787_045_123_456_000
    assert captured["api_key"] == ""
    assert captured["cache_dir"] == str(tmp_path / "cache")
    assert [item.name for item in captured["filters"]] == ["depth", "trade"]
    assert list(captured["filters"][0].symbols) == ["btcusdt"]
