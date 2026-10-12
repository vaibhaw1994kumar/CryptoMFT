# Integration of three projects

This repository combines three previously separate code bases into one
installable project. Every source file from the three originals is present;
nothing was rewritten apart from the small wiring changes listed below.

| Original folder | Where it lives now |
| --- | --- |
| `hft-platform-kit-main/` | Repository root (unchanged layout): `src/hft_platform_kit`, `src/trading_platform_poc`, `cpp/`, `tests/`, `examples/`, `scripts/`, `.github/`, all root `*.md` docs, `config*.yaml`, `Dockerfile`, `uv.lock` |
| `crypto_stat_arb_platform_phase1 (7)/src/crypto_stat_arb` | `src/crypto_stat_arb/` |
| `crypto_stat_arb_platform_phase1 (7)/tests/` | `tests/stat_arb/` |
| `crypto_stat_arb_platform_phase1 (7)/examples/` | `examples/crypto_stat_arb/` |
| `crypto_stat_arb_platform_phase1 (7)/config/`, `data/`, `docs/` | `config/`, `data/`, `docs/` (same relative paths, so its README commands still work from the repo root) |
| `crypto_stat_arb_platform_phase1 (7)/README.md` | `README_CRYPTO_STAT_ARB.md` |
| `crypto_stat_arb_platform_phase1 (7)/BUILD_STATUS.md` | `BUILD_STATUS.md` |
| `crypto_stat_arb_platform_phase1 (7)/pyproject.toml`, `.gitignore` | Merged into the root files; originals kept verbatim as `docs/original_pyproject_crypto_stat_arb.toml` and `docs/original_gitignore_crypto_stat_arb` |
| `new_backtest/` | `src/new_backtest/` |

Not copied (regenerable caches/build output only): `__pycache__/`,
`.pytest_cache/`, `*.egg-info/`, and `hft-platform-kit-main/build/` (CMake
scratch directory).

## Wiring changes

- **`pyproject.toml`**: `crypto_stat_arb` and `new_backtest` added to
  `wheel.packages`; `websockets>=12,<17` added to dependencies; the
  `crypto-stat-arb` console script added; optional extra `backtest`
  (`requests`, used by `new_backtest.fetch_tardis`); new docs/config added to
  `sdist.include`. The distribution name and version stay
  `hft-platform-kit` / `0.3.0` because the packaging tests pin them.
- **`.gitignore`**: `data/` became `data/*` with exceptions for
  `data/samples/`, `data/evidence/` and `data/contract_master/`, which the
  stat-arb tests and docs depend on. `runs/` added.
- **`tests/stat_arb/`**: added an empty `__init__.py` so its `test_oms.py`
  does not collide with `tests/test_oms.py` under pytest. `ROOT =
  Path(__file__).parents[1]` became `parents[2]` in the nine test files that
  read `config/`, `data/` or `docs/`, because they are one directory deeper.
- `uv.lock` was not regenerated (uv is not installed on this machine). It
  currently pins `websockets 17.0`, which violates the new `<17` constraint;
  run `uv lock` once.

## Running

```bash
pip install -e .                        # or: uv sync
hft-platform-kit --help
crypto-stat-arb --help
python -m new_backtest.backtester --help
pytest                                  # all suites, including tests/stat_arb
python -m unittest discover -s tests/stat_arb -t .   # stat-arb suite alone
```

## Wiring between the packages

The three code bases no longer only sit side by side; the minimum-requirements
stack (`trading_platform_poc.minreq`) now uses the other two directly:

| Used by | Comes from | For |
| --- | --- | --- |
| `minreq/quality.py` | `crypto_stat_arb.time_integrity.TimeIntegrityMonitor` | Negative and impossible receive latency, cross-venue receive skew |
| `minreq/quality.py` | `new_backtest.latency_model.HostClockAligner` | Host-clock offset estimate in every quality report |
| `minreq/latency.py` | `new_backtest.config.LATENCY_PRESETS` | `--latency-preset` lognormal co-location profiles |
| `minreq/backtest.py` | `new_backtest.latency_model.MarkoutTracker` | 1 s / 5 s / 30 s fill markouts in `execution_quality.json` |
| `minreq/matching.py` | design of `new_backtest.backtester` and `sim_engine/dry_run_core.py` | Orders execute on arrival; queue-aware maker fills |

New minreq modules: `checksums.py` (OKX CRC32, verified whenever a non-zero checksum is published), `ingest.py` (live capture and
`mr-record`), `live.py` (live paper trading), `testnet.py` (venue testnet
execution behind the matcher interface). `tests/test_minreq_hardening.py`
covers them.

Other folders found under `Newproject/` but **not** merged, pending a decision:
`altcoin_mm_v1/` (current Kraken market-making bot, with three older copies in
`AMM/Kraken/`, `AMM/Kraken/altcoin_mm_v1_snapshot_/` and `ec2_kraken_files/`),
`backtest/` (Bybit FAST_MM backtester) and `sim_engine/` (FAST_MM dry-run fill
engine with a C++ matching core; identical copy in `~/Downloads/sim_engine`).
