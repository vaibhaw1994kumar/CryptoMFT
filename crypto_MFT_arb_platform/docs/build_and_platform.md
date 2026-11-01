# Build, platform and supply chain

This page covers requirements GAP01, GAP03 and SDK04.

## Target platform

**The validated target runtime is Linux x86_64 with CPython 3.12.** This is the base of the Docker image, `python:3.12-slim-bookworm` (glibc 2.36).

- The wheels CI job builds wheels for Linux x86_64, Linux arm64, macOS arm64 and Windows, for CPython 3.12 and 3.13.
- Only Linux x86_64 with CPython 3.12 is validated by the target-runtime job below.
- The AT-14 performance limits are also enforced on Linux only.

## Native extensions

Every wheel compiles two C++ extensions:

| Module | Source | Installed as |
|---|---|---|
| `hft_platform_kit._ems` | `cpp/ems.cpp` | `hft_platform_kit/_ems.*` |
| `mm_matching_engine` (maker queue engine) | `src/crypto_stat_arb/sim_engine/{include,src}` | `crypto_stat_arb/sim_engine/mm_matching_engine.*` |

- **Authoritative source:** `src/crypto_stat_arb/sim_engine`. The root `sim_engine/` copy is stale (it has no `types.hpp`) and is not built.
- **Python mirror:** `maker_engine.PythonQueueEngine` mirrors `mm_matching_engine`. The parity test checks the two are bit-identical over 5,000 random operations.
- **Build identity:** the build compiles `__build_info__` into `mm_matching_engine`. It records the SHA-256 of the sources it was built from, the compiler, the build type and the pybind11 version.

## Which engine ran (GAP01)

The maker report's `engine_build` (from `maker_engine.engine_identity`) records:

- for the C++ engine:
  - the binary's name and SHA-256;
  - whether it matches this interpreter's extension suffix;
  - its build info;
  - `built_from_current_source`, which compares the hash compiled into the binary with the sources on disk;
- for the Python mirror: why the C++ engine was not used.

`auto` never falls back silently, and `cpp` raises with the reason.

`tests/test_cpp_engine_loading.py` forces each failure, each in a fresh interpreter:

- the module is missing;
- the binary is corrupt;
- the module lacks the engine classes;
- the engine raises mid-run. It must propagate, and the engine never switches mid-run.

## Target-runtime job and build record (GAP03)

The `target runtime (linux x86_64, cpython 3.12)` job in `.github/workflows/wheels.yml` works like this:

1. It installs the cp312 manylinux x86_64 wheel into `python:3.12-slim-bookworm`, with the dependencies exported from `uv.lock`.
2. It runs the C++ engine tests against that installed build. `REQUIRE_CPP_ENGINE=1` turns their skip into a failure.
3. It writes `build_record.json` (`scripts/build_record.py`), which records:
   - the wheel's SHA-256;
   - the interpreter version, SOABI and extension suffix;
   - the OS, CPU and libc;
   - every native extension's SHA-256 and linked libraries;
   - the engine's build identity.
4. The job fails if any of these hold:
   - the package is imported from a source tree;
   - the engine falls back to Python;
   - the engine was not built from the shipped sources.

Releases need this job.

## SBOM (SDK04)

`scripts/sbom.py` writes a CycloneDX 1.5 SBOM next to every built wheel (`<wheel>.cdx.json`). It lists:

- the wheel and the SHA-256 of each native extension inside it;
- the runtime dependency closure from `uv.lock`, with package URLs, sdist hashes and platform markers;
- the build requirements, as unpinned build tools.

**Gaps, stated in the SBOM itself:**

- `uv.lock` records no licences.
- Dependency hashes are the sdist's.
- The build tools are version ranges resolved at build time. For example, the 2026-10-29 target build used pybind11 3.1.0 against `>=3.0.4`.

## Provenance and signing (SDK04)

- **On a version tag**, the release job attests build provenance for every release asset with `actions/attest-build-provenance`: wheels, sdist, SBOMs and the build record. The attestations are signed through Sigstore.
- **Permissions:** `id-token: write` and `attestations: write` are granted to that job only. The workflow default stays `contents: read`.
- **Branch builds** are not attested.

To check where a downloaded asset was built:

```
gh attestation verify <file> --repo vaibhaw1994kumar/CryptoMFT
```

## Rollback

Each release keeps its own versioned assets: wheels, sdist, evidence bundle, SBOMs and build record. The image keeps its version tag on GHCR. Rolling back means installing or running the previous version's assets.
