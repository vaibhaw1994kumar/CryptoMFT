"""CycloneDX 1.5 SBOM for each built wheel (SDK04), from uv.lock and the wheel itself.

    python scripts/sbom.py --wheelhouse wheelhouse          # one <wheel>.cdx.json per wheel
    python scripts/sbom.py --wheel dist/x.whl --output x.cdx.json

Each SBOM describes what that wheel ships:
- the wheel itself (sha256) and every native extension inside it (sha256 of the file);
- the runtime dependency closure from uv.lock (the dev group is excluded), each with
  its package URL, the sdist sha256 recorded in the lock, and its environment marker
  when it is platform-specific (e.g. uvloop off Windows, winloop on Windows);
- the build tools from pyproject's [build-system] requires. They are version ranges
  resolved at build time, not locked; the SBOM says so.

Known gaps, stated in the SBOM's properties rather than invented:
- uv.lock records no licences;
- dependency hashes are the sdist's, not the platform wheel pip installs.

Output is deterministic: the serial number is derived from the wheel and lock hashes,
and the timestamp comes from SOURCE_DATE_EPOCH when set.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tomllib
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NATIVE_SUFFIXES = (".so", ".pyd", ".dylib")
NAMESPACE = uuid.UUID("6f1e9a52-4c1b-4c87-9a8a-5d2b1f0d6c11")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _purl(name: str, version: str) -> str:
    return f"pkg:pypi/{name.lower().replace('_', '-')}@{version}"


def runtime_closure(lock: dict, root_name: str) -> tuple[list[dict], dict[str, set[str]], dict[str, str]]:
    """Packages reachable from the root's runtime dependencies, their edges, and markers."""
    packages = {p["name"]: p for p in lock["package"]}
    root = packages[root_name]
    markers: dict[str, str] = {}
    edges: dict[str, set[str]] = {}
    seen: set[str] = set()
    pending = []
    for dep in root.get("dependencies", []):
        pending.append(dep["name"])
        if dep.get("marker"):
            markers[dep["name"]] = dep["marker"]
    edges[root_name] = {dep["name"] for dep in root.get("dependencies", [])}
    while pending:
        name = pending.pop()
        if name in seen or name not in packages:
            continue
        seen.add(name)
        deps = packages[name].get("dependencies", [])
        edges[name] = {dep["name"] for dep in deps}
        pending.extend(dep["name"] for dep in deps)
    return [packages[name] for name in sorted(seen)], edges, markers


def native_extensions(wheel: Path) -> list[dict]:
    with zipfile.ZipFile(wheel) as archive:
        return [{"path": info.filename, "sha256": _sha256(archive.read(info))}
                for info in sorted(archive.infolist(), key=lambda i: i.filename)
                if info.filename.endswith(NATIVE_SUFFIXES)]


def build_sbom(wheel: Path, lock_path: Path = ROOT / "uv.lock", pyproject_path: Path = ROOT / "pyproject.toml") -> dict:
    lock_bytes = lock_path.read_bytes()
    lock = tomllib.loads(lock_bytes.decode("utf-8"))
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = pyproject["project"]
    root_name, version = project["name"], project["version"]
    wheel_sha = _sha256(wheel.read_bytes())
    packages, edges, markers = runtime_closure(lock, root_name)

    components = []
    for package in packages:
        component = {"type": "library", "bom-ref": _purl(package["name"], package["version"]),
                     "name": package["name"], "version": package["version"],
                     "purl": _purl(package["name"], package["version"])}
        sdist = package.get("sdist") or {}
        if sdist.get("hash", "").startswith("sha256:"):
            component["hashes"] = [{"alg": "SHA-256", "content": sdist["hash"].split(":", 1)[1]}]
            component["externalReferences"] = [{"type": "distribution", "url": sdist["url"]}]
        properties = [{"name": "uv.lock:wheels", "value": str(len(package.get("wheels", [])))}]
        if package["name"] in markers:
            properties.append({"name": "environment-marker", "value": markers[package["name"]]})
        component["properties"] = properties
        components.append(component)
    root_ref = _purl(root_name, version)
    for native in native_extensions(wheel):
        components.append({"type": "library", "bom-ref": f"{root_ref}#{native['path']}",
                           "name": Path(native["path"]).name, "version": version,
                           "description": "native extension compiled into this wheel",
                           "hashes": [{"alg": "SHA-256", "content": native["sha256"]}],
                           "properties": [{"name": "path-in-wheel", "value": native["path"]}]})
    for requirement in pyproject.get("build-system", {}).get("requires", []):
        name = requirement.split(">")[0].split("=")[0].split("<")[0].strip()
        components.append({"type": "library", "bom-ref": f"build:{name}", "name": name, "scope": "excluded",
                           "description": "build tool (not shipped)",
                           "properties": [{"name": "build-requirement", "value": requirement},
                                          {"name": "pinned", "value": "false: resolved at build time"}]})

    sbom = {
        "bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
        "serialNumber": f"urn:uuid:{uuid.uuid5(NAMESPACE, wheel_sha + _sha256(lock_bytes))}",
        "metadata": {
            "tools": {"components": [{"type": "application", "name": "scripts/sbom.py"}]},
            "component": {"type": "library", "bom-ref": root_ref, "name": root_name, "version": version,
                          "purl": root_ref, "hashes": [{"alg": "SHA-256", "content": wheel_sha}],
                          "properties": [{"name": "wheel", "value": wheel.name}]},
            "properties": [
                {"name": "source", "value": "uv.lock (runtime dependency closure; dev group excluded)"},
                {"name": "uv.lock:sha256", "value": _sha256(lock_bytes)},
                {"name": "gap:licenses", "value": "uv.lock records no licences; none are listed"},
                {"name": "gap:dependency-hashes", "value": "sdist sha256 from uv.lock, not the installed platform wheel"},
            ],
        },
        "components": components,
        "dependencies": [{"ref": _purl(name, next(p["version"] for p in lock["package"] if p["name"] == name)),
                          "dependsOn": sorted(_purl(dep, next(p["version"] for p in lock["package"] if p["name"] == dep))
                                              for dep in deps if any(p["name"] == dep for p in lock["package"]))}
                         for name, deps in sorted(edges.items())],
    }
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch:
        sbom["metadata"]["timestamp"] = datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return sbom


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--wheel", type=Path)
    group.add_argument("--wheelhouse", type=Path, help="write <wheel>.cdx.json next to every wheel in it")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    wheels = [args.wheel] if args.wheel else sorted(args.wheelhouse.glob("*.whl"))
    if not wheels:
        raise SystemExit("no wheels found")
    for wheel in wheels:
        target = args.output if args.wheel and args.output else wheel.with_name(wheel.name + ".cdx.json")
        target.write_text(json.dumps(build_sbom(wheel), indent=2, sort_keys=True), encoding="utf-8")
        print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
