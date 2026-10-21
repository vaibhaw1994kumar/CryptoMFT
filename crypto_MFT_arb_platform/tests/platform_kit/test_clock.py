import pytest

from trading_platform_poc.clock import SimulatedClock


@pytest.mark.asyncio
async def test_simulated_clock_advances_deterministically() -> None:
    clock = SimulatedClock(100)

    await clock.sleep_until(250)

    assert clock.now_ns() == 250


@pytest.mark.asyncio
async def test_simulated_clock_rejects_backwards_time() -> None:
    clock = SimulatedClock(250)

    with pytest.raises(ValueError, match="cannot move backwards"):
        await clock.sleep_until(100)
