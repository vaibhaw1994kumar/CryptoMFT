"""Trading platform proof-of-concept package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("hft-platform-kit")
except PackageNotFoundError:  # pragma: no cover - source checkout fallback
    __version__ = "0.2.0"
