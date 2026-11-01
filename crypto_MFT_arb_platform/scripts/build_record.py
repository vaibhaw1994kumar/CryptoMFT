"""Build and ABI record for an installed build of this package (GAP03).

Run it with the interpreter the build was installed into (not from the source tree):

    python scripts/build_record.py --wheel dist/<wheel>.whl --output build_record.json

It records the interpreter and its ABI (version, implementation, SOABI, EXT_SUFFIX),
the platform (OS, CPU, libc), every native extension shipped (path inside the package,
sha256, the shared libraries it links against) and the matching engine's own build
identity (the source hash, compiler, build type and pybind11 version compiled into it).
It exits 1 if the installed package is imported from a source tree, if the compiled
matching engine does not load, or if it was not built from the sources it ships with.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

NATIVE_MODULES = ("hft_platform_kit._ems",)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def linked_libraries(path: Path) -> list[str]:
    """The shared libraries a native extension links against (ldd / otool -L), if available."""
    for tool, args in (("ldd", [str(path)]), ("otool", ["-L", str(path)])):
        if shutil.which(tool):
            done = subprocess.run([tool, *args], capture_output=True, text=True, check=False)
            lines = [line.strip() for line in done.stdout.splitlines()[(1 if tool == "otool" else 0):]]
            return [line for line in lines if line]
    return []


def native_record(path: Path, package_root: Path) -> dict:
    return {"path": path.relative_to(package_root).as_posix(), "sha256": sha256(path),
            "bytes": path.stat().st_size, "links": linked_libraries(path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--wheel", type=Path, help="the wheel that was installed (hashed into the record)")
    parser.add_argument("--output", type=Path, default=Path("build_record.json"))
    args = parser.parse_args()

    import crypto_stat_arb
    from crypto_stat_arb import maker_engine

    package_root = Path(crypto_stat_arb.__file__).resolve().parents[1]
    problems = []
    if (package_root / "crypto_stat_arb").resolve() != Path(crypto_stat_arb.__file__).resolve().parent:
        problems.append("crypto_stat_arb resolved unexpectedly")
    if "site-packages" not in package_root.as_posix() and "dist-packages" not in package_root.as_posix():
        problems.append(f"crypto_stat_arb is imported from {package_root}, not an installed build")

    natives = []
    for name in NATIVE_MODULES:
        try:
            module = importlib.import_module(name)
            natives.append({"module": name, **native_record(Path(module.__file__), package_root)})
        except ImportError as exc:
            problems.append(f"{name} does not import: {exc}")
    engine = maker_engine.make_queue_engine("auto")
    identity = maker_engine.engine_identity(engine)
    if identity["engine"] != maker_engine.CPP_ENGINE_NAME:
        problems.append(f"matching engine fell back to Python: {identity.get('cpp_unavailable_reason')}")
    else:
        natives.append({"module": "mm_matching_engine",
                        **native_record(Path(engine._m.__file__), package_root)})
        if identity.get("built_from_current_source") is not True:
            problems.append("mm_matching_engine was not built from the sources shipped with it")

    libc = platform.libc_ver()
    record = {
        "schema_version": "1.0.0",
        "git_revision": os.environ.get("GITHUB_SHA") or os.environ.get("HFT_PLATFORM_KIT_GIT_REVISION") or "",
        "wheel": ({"file": args.wheel.name, "sha256": sha256(args.wheel)} if args.wheel else None),
        "interpreter": {
            "version": platform.python_version(), "implementation": platform.python_implementation(),
            "soabi": sysconfig.get_config_var("SOABI"), "ext_suffix": sysconfig.get_config_var("EXT_SUFFIX"),
            "executable": sys.executable,
        },
        "platform": {"system": platform.system(), "release": platform.release(), "machine": platform.machine(),
                     "libc": f"{libc[0]} {libc[1]}".strip(), "platform": platform.platform()},
        "native_extensions": natives,
        "matching_engine": identity,
        "problems": problems,
    }
    args.output.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "problems": problems}, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
