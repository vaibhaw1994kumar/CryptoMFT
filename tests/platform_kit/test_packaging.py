from __future__ import annotations

import subprocess
import tarfile
import tomllib
from pathlib import Path, PurePosixPath
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[2]
REQUIRED_SDIST_PATHS = {
    "CMakeLists.txt",
    "README.md",
    "PUBLISHING.md",
    "config.yaml",
    "config.testnet.example.yaml",
    "cpp/ems.cpp",
    "examples/custom_strategy.py",
    "examples/native_ems.py",
    "examples/sdk_replay.py",
    "src/hft_platform_kit/__init__.py",
    "src/trading_platform_poc/__init__.py",
}
REQUIRED_SDIST_EXCLUDES = {
    "/.venv/**",
    "/build/**",
    "/data/**",
    "/dist/**",
    "/reports/**",
    "/.tardis-cache/**",
    "/.pytest_cache/**",
    "**/__pycache__/**",
    "**/*.egg-info/**",
    "**/*.log",
    "**/*.py[cod]",
    "**/.DS_Store",
    "**/.env",
    "**/.env.*",
    "**/~$*",
}
FORBIDDEN_TOP_LEVEL = {
    ".idea",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tardis-cache",
    ".tox",
    ".venv",
    ".vscode",
    "build",
    "data",
    "dist",
    "htmlcov",
    "reports",
    "wheels",
}
FORBIDDEN_COMPONENTS = FORBIDDEN_TOP_LEVEL | {
    "__pycache__",
    "site-packages",
}


def _sdist_relative_paths(sdist: Path) -> set[str]:
    with tarfile.open(sdist, "r:gz") as archive:
        paths = [
            PurePosixPath(member.name)
            for member in archive.getmembers()
            if member.isfile()
        ]

    roots = {path.parts[0] for path in paths}
    assert len(roots) == 1, f"sdist should have one archive root, found: {roots}"
    return {
        PurePosixPath(*path.parts[1:]).as_posix()
        for path in paths
        if len(path.parts) > 1
    }


def _forbidden_sdist_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1]
    return (
        (parts and parts[0] in FORBIDDEN_TOP_LEVEL)
        or any(part in FORBIDDEN_COMPONENTS for part in parts)
        or name == "PCOC.MD"
        or name == ".coverage"
        or name == ".DS_Store"
        or name.startswith(".env")
        or name.startswith("~$")
        or name.endswith((".log", ".pyc", ".pyo"))
    )


def test_release_artifacts_are_clean_and_native(tmp_path: Path) -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    excludes = set(config["tool"]["scikit-build"]["sdist"]["exclude"])
    assert REQUIRED_SDIST_EXCLUDES <= excludes

    subprocess.run(
        ["uv", "build", "--clear", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    sdists = list(tmp_path.glob("hft_platform_kit-*.tar.gz"))
    wheels = list(tmp_path.glob("hft_platform_kit-*.whl"))
    assert len(sdists) == 1, f"expected one sdist, found: {sdists}"
    assert len(wheels) == 1, f"expected one wheel, found: {wheels}"

    sdist_paths = _sdist_relative_paths(sdists[0])
    assert REQUIRED_SDIST_PATHS <= sdist_paths
    forbidden = sorted(path for path in sdist_paths if _forbidden_sdist_path(path))
    assert not forbidden, f"forbidden paths in sdist: {forbidden}"

    wheel = wheels[0]
    assert not wheel.name.endswith("-py3-none-any.whl"), (
        f"native package produced a pure wheel: {wheel.name}"
    )
    with ZipFile(wheel) as archive:
        native_extensions = [
            name
            for name in archive.namelist()
            if name.startswith("hft_platform_kit/_ems")
            and name.endswith((".so", ".pyd"))
        ]
    assert native_extensions, f"native _ems extension missing from {wheel.name}"


def test_public_packages_report_the_distribution_version() -> None:
    """Both public packages must agree with pyproject and each other."""
    import hft_platform_kit
    import trading_platform_poc
    from importlib.metadata import version

    declared = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    installed = version("hft-platform-kit")

    assert installed == declared
    assert hft_platform_kit.__version__ == declared
    assert trading_platform_poc.__version__ == declared
