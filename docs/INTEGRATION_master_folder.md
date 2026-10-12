# Master folder layout (crypto_stat_arb_platform_phase1 (10))

This folder is the single master. Everything from `unified_trading_platform`
and `sim_engine` was copied in on 2026-09-13; the original folders were not
modified. Existing files in (10) were never overwritten.

| Source | Location here | Notes |
| --- | --- | --- |
| `unified_trading_platform/src/hft_platform_kit` | `src/hft_platform_kit/` | byte-identical |
| `unified_trading_platform/src/trading_platform_poc` (incl. `minreq/`) | `src/trading_platform_poc/` | byte-identical; minreq stays the reference implementation that was ported into `crypto_stat_arb` |
| `unified_trading_platform/src/new_backtest` | `src/new_backtest/` | byte-identical; `crypto_stat_arb/advanced_backtest` is the same engine already bundled |
| `unified_trading_platform/src/crypto_stat_arb` | not copied | older copy of this folder's own package |
| `unified_trading_platform/cpp`, `CMakeLists.txt`, `Dockerfile`, `.github`, `uv.lock`, `config*.yaml`, `LICENSE` | same paths | native `_ems` build and CI as in unified |
| `unified_trading_platform/tests/*.py` (kit + minreq tests) | `tests/platform_kit/` | `test_oms.py` renamed `test_kit_oms.py`; three tests had their repo-root path moved one level up |
| `unified_trading_platform/tests/stat_arb` | not copied | older duplicate of `tests/` |
| `unified_trading_platform/{README,INTEGRATION}.md` | `README_HFT_PLATFORM_KIT.md`, `docs/INTEGRATION_unified_trading_platform.md` | |
| all other unified root docs (`MINIMUM_REQUIREMENTS.md`, `CHANGES_SUMMARY.md`, client/demo docs, …) | same names | |
| `unified_trading_platform/{config,data,docs,examples,scripts}` | merged, missing files only | |
| `unified_trading_platform/pyproject.toml` | `pyproject.toml` (+ `requests`) | previous (10) file kept as `docs/original_pyproject_crypto_stat_arb_phase1_10.toml` |
| `Newproject/sim_engine` | `src/crypto_stat_arb/sim_engine/` | byte-identical; used by `maker_sim.py` |

Run: `PYTHONPATH=src python -m unittest discover -s tests` (crypto_stat_arb,
187 tests) and `PYTHONPATH=src python -m pytest tests/platform_kit -m "not slow"`
(kit + minreq; needs `tardis_dev`, `hftbacktest`, `pyarrow` and the native
`_ems` build for the modules that fail to collect without them).
