from __future__ import annotations

import importlib.util
from dataclasses import asdict, dataclass

from trading_platform_poc.runtime import ACCELERATED_LOOP_MODULE


@dataclass(frozen=True, slots=True)
class LibraryIntegration:
    name: str
    module: str
    active: bool
    role: str
    disposition: str

    def to_dict(self) -> dict:
        return asdict(self)


def library_integrations() -> tuple[LibraryIntegration, ...]:
    return (
        _active(
            "tardis-dev",
            "tardis_dev",
            "Exchange-native Binance historical replay ingestion.",
        ),
        _active(
            "hftbacktest",
            "hftbacktest",
            "Historical event export and execution-model calibration.",
        ),
        _active(
            "pyarrow",
            "pyarrow",
            "Normalized Parquet event storage.",
        ),
        _active(
            "FastAPI",
            "fastapi",
            "Operational status HTTP endpoint.",
        ),
        _active(
            "Prometheus client",
            "prometheus_client",
            "Runtime metrics exposition.",
        ),
        _active(
            ACCELERATED_LOOP_MODULE,
            ACCELERATED_LOOP_MODULE,
            "Low-overhead asyncio event loop.",
        ),
        LibraryIntegration(
            name="level2-order-book",
            module="level2_order_book",
            active=False,
            role="Alternative L2 book implementation.",
            disposition=(
                "Optional adapter only: its exchange/data conventions are not "
                "used for the Binance-native production path."
            ),
        ),
        LibraryIntegration(
            name="OrderBook-rs",
            module="orderbook_rs",
            active=False,
            role="Potential native high-performance order-book engine.",
            disposition=(
                "Optional future PyO3 adapter: the upstream Rust crate does "
                "not currently provide an official Python module."
            ),
        ),
    )


def _active(name: str, module: str, role: str) -> LibraryIntegration:
    installed = importlib.util.find_spec(module) is not None
    return LibraryIntegration(
        name=name,
        module=module,
        active=installed,
        role=role,
        disposition="Integrated and available." if installed else "Missing.",
    )
