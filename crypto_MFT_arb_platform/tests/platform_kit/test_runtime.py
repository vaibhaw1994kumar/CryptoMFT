import asyncio
import sys

import pytest

from trading_platform_poc import runtime


def test_accelerated_loop_module_matches_platform() -> None:
    """uvloop has no Windows support; winloop is its Windows port."""
    if sys.platform == "win32":
        assert runtime.ACCELERATED_LOOP_MODULE == "winloop"
    else:
        assert runtime.ACCELERATED_LOOP_MODULE == "uvloop"


def test_run_uses_accelerated_loop_when_available() -> None:
    async def value() -> int:
        return 7

    assert runtime.run(value()) == 7


def test_run_falls_back_to_stdlib_when_accelerated_loop_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing or broken accelerated loop must not break the CLI."""
    monkeypatch.setattr(runtime, "accelerated_runner", lambda: None)
    used: dict[str, str] = {}

    async def value() -> int:
        loop = asyncio.get_running_loop()
        used["loop"] = type(loop).__module__
        return 11

    assert runtime.run(value()) == 11
    assert used["loop"].startswith("asyncio")


def test_accelerated_runner_returns_none_when_import_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _missing(name: str):
        raise ImportError(name)

    monkeypatch.setattr(runtime.importlib, "import_module", _missing)

    assert runtime.accelerated_runner() is None


def test_cli_does_not_import_uvloop_at_module_scope() -> None:
    """The CLI entry point must import on Windows, where uvloop is absent."""
    from pathlib import Path

    source = Path(runtime.__file__).with_name("cli.py").read_text(
        encoding="utf-8"
    )

    assert "import uvloop" not in source
