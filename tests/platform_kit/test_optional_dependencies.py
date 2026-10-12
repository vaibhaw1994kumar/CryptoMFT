"""Regression tests for optional native dependency handling.

A consumer on Windows hit ``ImportError: DLL load failed while importing lib:
An Application Control policy has blocked this file`` when importing
``hft_platform_kit``, because ``pyarrow`` was imported at module scope. The
whole SDK became unusable even though only the Parquet paths need pyarrow.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from trading_platform_poc.optional_dependencies import (
    OptionalDependencyError,
    require_hftbacktest,
    require_hftbacktest_binance_futures,
    require_hftbacktest_snapshot,
    require_hftbacktest_stats,
    require_hftbacktest_tardis,
    require_numba,
    require_numpy,
    require_pyarrow,
    require_pyarrow_parquet,
)

_BLOCKED_ROOTS = ("pyarrow", "hftbacktest", "numba", "numpy")

_BLOCK_PREAMBLE = textwrap.dedent(
    """
    import builtins
    import sys

    _blocked = {blocked!r}
    _real_import = builtins.__import__


    def _blocked_import(name, *args, **kwargs):
        if name.split(".")[0] in _blocked:
            raise ImportError(
                "DLL load failed while importing lib: "
                "An Application Control policy has blocked this file."
            )
        return _real_import(name, *args, **kwargs)


    builtins.__import__ = _blocked_import
    for _module in list(sys.modules):
        if _module.split(".")[0] in _blocked:
            del sys.modules[_module]
    """
).format(blocked=_BLOCKED_ROOTS)


def _run_with_blocked_natives(body: str) -> subprocess.CompletedProcess[str]:
    """Run ``body`` in a fresh interpreter where native imports all fail."""

    return subprocess.run(
        [sys.executable, "-c", _BLOCK_PREAMBLE + textwrap.dedent(body)],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_public_sdk_imports_when_native_dependencies_are_blocked() -> None:
    result = _run_with_blocked_natives(
        """
        import hft_platform_kit
        from hft_platform_kit import (
            AppConfig,
            OrderManagementSystem,
            RiskEngine,
            Side,
        )

        assert hft_platform_kit.__version__
        assert AppConfig().symbol
        assert Side.BUY.value == "buy"
        print("IMPORT_OK")
        """
    )

    assert result.returncode == 0, result.stderr
    assert "IMPORT_OK" in result.stdout


def test_trading_platform_package_imports_when_natives_are_blocked() -> None:
    result = _run_with_blocked_natives(
        """
        import trading_platform_poc
        from trading_platform_poc.recorder import JsonlRecorder
        from trading_platform_poc.replay import ReplaySource
        from trading_platform_poc import hftbacktest_integration

        assert JsonlRecorder is not None
        assert ReplaySource is not None
        assert hftbacktest_integration is not None
        print("IMPORT_OK")
        """
    )

    assert result.returncode == 0, result.stderr
    assert "IMPORT_OK" in result.stdout


def test_blocked_parquet_feature_reports_actionable_error() -> None:
    result = _run_with_blocked_natives(
        """
        import tempfile
        from pathlib import Path

        from trading_platform_poc.optional_dependencies import (
            OptionalDependencyError,
        )
        from trading_platform_poc.recorder import JsonlRecorder

        with tempfile.TemporaryDirectory() as directory:
            try:
                with JsonlRecorder(Path(directory) / "dataset"):
                    pass
            except OptionalDependencyError as error:
                message = str(error)
                assert "pyarrow" in message
                assert "Application Control policy" in message
                print("ACTIONABLE_OK")
            else:
                raise AssertionError("expected OptionalDependencyError")
        """
    )

    assert result.returncode == 0, result.stderr
    assert "ACTIONABLE_OK" in result.stdout


def test_blocked_hftbacktest_feature_reports_actionable_error() -> None:
    result = _run_with_blocked_natives(
        """
        from trading_platform_poc.hftbacktest_integration import (
            hftbacktest_health,
        )
        from trading_platform_poc.optional_dependencies import (
            OptionalDependencyError,
        )

        try:
            hftbacktest_health()
        except OptionalDependencyError as error:
            assert "hftbacktest" in str(error)
            print("ACTIONABLE_OK")
        else:
            raise AssertionError("expected OptionalDependencyError")
        """
    )

    assert result.returncode == 0, result.stderr
    assert "ACTIONABLE_OK" in result.stdout


@pytest.mark.parametrize(
    "loader",
    [
        require_pyarrow,
        require_pyarrow_parquet,
        require_hftbacktest,
        require_hftbacktest_binance_futures,
        require_hftbacktest_snapshot,
        require_hftbacktest_stats,
        require_hftbacktest_tardis,
        require_numba,
        require_numpy,
    ],
)
def test_loaders_return_modules_when_dependencies_are_installed(
    loader,
) -> None:
    assert loader() is not None


def test_optional_dependency_error_is_an_import_error() -> None:
    assert issubclass(OptionalDependencyError, ImportError)


def test_missing_module_raises_optional_dependency_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trading_platform_poc import optional_dependencies

    def fail(name: str) -> None:
        raise ImportError("DLL load failed while importing lib")

    monkeypatch.setattr(optional_dependencies.importlib, "import_module", fail)

    with pytest.raises(OptionalDependencyError) as error:
        optional_dependencies.require_pyarrow()

    message = str(error.value)
    assert "pyarrow" in message
    assert "Application Control policy" in message
