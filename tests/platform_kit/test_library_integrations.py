from trading_platform_poc.library_integrations import library_integrations
from trading_platform_poc.runtime import ACCELERATED_LOOP_MODULE


def test_required_libraries_are_active_and_optional_adapters_are_explicit() -> None:
    integrations = {
        integration.name: integration
        for integration in library_integrations()
    }

    for required in (
        "tardis-dev",
        "hftbacktest",
        "pyarrow",
        "FastAPI",
        "Prometheus client",
        ACCELERATED_LOOP_MODULE,
    ):
        assert integrations[required].active is True

    assert integrations["level2-order-book"].active is False
    assert "Optional adapter" in integrations["level2-order-book"].disposition
    assert integrations["OrderBook-rs"].active is False
    assert "PyO3" in integrations["OrderBook-rs"].disposition
