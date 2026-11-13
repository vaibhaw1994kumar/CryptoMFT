# Environment-only tests (owner 13 Nov)

The Phase 0 list of 93 failing tests (`out/integration/baselines/failing_tests_phase0.txt`) is all in `tests/platform_kit/`. None is a code defect of the platform: each needs something this checkout does not have.

| Tests | Count | Needs | Why it fails here |
|---|---|---|---|
| `test_ems_wiring.py` (90 tests collected, 88 fail; 2 pass without the extension) and `test_native_ems.py` (1 collection error) | 89 | the compiled extension `hft_platform_kit._ems` | `ModuleNotFoundError: No module named 'hft_platform_kit._ems'` |
| `test_packaging.py` (2), `test_minreq_execution.py::test_build_identity_reports_the_installed_distribution_version` (1) | 3 | the package installed as a distribution (`hft-platform-kit`) and `uv build` | `PackageNotFoundError: hft-platform-kit`, version `0.0.0+local`, `uv build` fails |
| `test_opportunity_demo.py::test_cli_stdout_is_machine_readable` | 1 | `trading_platform_poc` runnable as `python -m trading_platform_poc.cli` | the subprocess exits non-zero |

## How `_ems` is built

`hft_platform_kit._ems` is a C++20 / pybind11 module compiled from `cpp/ems.cpp` by `CMakeLists.txt` (target `_ems`, installed into `hft_platform_kit/`; the same file also builds `mm_matching_engine` from `src/crypto_stat_arb/sim_engine`). The build backend is scikit-build-core (`[build-system]` in `pyproject.toml`: `scikit-build-core>=1.0.3`, `pybind11>=3.0.4`). It is built when the package is installed, not by running pytest on the working tree. Needs: CMake 3.18 or newer (found at `/opt/homebrew/bin/cmake`), a C++20 compiler (`/usr/bin/c++`), and `pybind11` (NOT installed in this Python 3.12). From `crypto_MFT_arb_platform/`:

```bash
python3.12 -m pip install "scikit-build-core>=1.0.3" "pybind11>=3.0.4"      # downloads: run it yourself
python3.12 -m pip install --no-build-isolation -e .                           # compiles _ems and mm_matching_engine
python3.12 -c "import hft_platform_kit._ems as e; print(e.__file__)"        # the check PUBLISHING.md uses
```

These steps were read from the build files; they were NOT run here (no installs, no downloads without the owner's go-ahead). Release wheels are built by `.github/workflows/wheels.yml` (cibuildwheel), see `PUBLISHING.md`.

## Listing them in another gate

```bash
PYTHONPATH=src python3.12 -m pytest tests -m native_ems --collect-only -q --ignore=tests/platform_kit/test_native_ems.py   # the 90 of test_ems_wiring.py (88 fail without the extension)
PYTHONPATH=src python3.12 -m pytest tests -m "not native_ems" --ignore=tests/platform_kit/test_native_ems.py              # everything else
```

`test_native_ems.py` fails at collection (it imports the extension at module level), so a marker cannot reach it: leave it out with `--ignore`. The marker is applied by `tests/platform_kit/conftest.py`; nothing is skipped, so the Phase 0 list is unchanged where the extension is not built.
