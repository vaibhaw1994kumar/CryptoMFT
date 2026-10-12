# Summary of changes

Date: 2026-09-13. All work is in this folder
(`Newproject/unified_trading_platform`). The original folders were not
modified, and nothing has been committed.

## 1. Integration of the three projects

| Original | Location here |
| --- | --- |
| `hft-platform-kit-main` | Repository root (layout unchanged) |
| `crypto_stat_arb_platform_phase1 (7)` | `src/crypto_stat_arb/`, tests in `tests/stat_arb/`, `config/`, `data/`, `docs/`, `README_CRYPTO_STAT_ARB.md`, `BUILD_STATUS.md` |
| `new_backtest` | `src/new_backtest/` |

- All 324 source files were copied and verified byte-identical. Only caches and CMake build output were left out.
- `pyproject.toml` builds all four packages, adds the `websockets` dependency, and adds the `crypto-stat-arb` command.
- `.gitignore` now keeps the stat-arb sample and evidence data tracked.
- The stat-arb tests got an `__init__.py` (to avoid the `test_oms.py` name clash) and their data paths now point one folder deeper.
- `INTEGRATION.md` maps every original file to its new location.

**Wiring between the packages** (minreq layer, `src/trading_platform_poc/minreq/`):

| Uses | From | For |
| --- | --- | --- |
| `quality.py` | `crypto_stat_arb.time_integrity` | Negative and impossible latency, cross-venue receive skew |
| `quality.py` | `new_backtest.latency_model.HostClockAligner` | Host-clock offset estimate |
| `latency.py` | `new_backtest.config.LATENCY_PRESETS` | `--latency-preset` lognormal venue profiles |
| `backtest.py` | `new_backtest.latency_model.MarkoutTracker` | 1 s / 5 s / 30 s fill markouts |
| `matching.py` | Designs from `new_backtest` and `sim_engine` | Fill on arrival; queue-aware maker fills |

**Not merged (awaiting your decision):**
- `altcoin_mm_v1`: the current copy plus three older copies.
- `backtest/`
- `sim_engine/`

## 2. Data sanity (MR-02, MR-04)

| Check | Before | After |
| --- | --- | --- |
| Price jump / wide spread | Not detected | `price_jump` (> 500 bps) and `wide_spread` (> 500 bps) mark the book invalid |
| Bad trades | Never checked | Non-positive price or size and unknown side raise `schema_exception`; prints far from the mid raise `trade_outside_book`; the simulator ignores flagged trades |
| Feed goes silent | Approved | Silence is detected while other venues keep trading; streams still silent at the end fail approval; stale ratio is also measured by time |
| Out-of-order vs duplicate | Both reported as duplicate | Redeliveries are `duplicate`; older, never-applied updates are `out_of_order` |
| Checksums | Never verified | OKX CRC32 verified whenever a non-zero checksum is published (`checksums.py`) |
| Unparseable frames | Crashed normalization | Kept in `quarantine/quarantine.jsonl` and graded `schema_exception` |
| Synthetic data | Not labelled | Tagged `synthetic`, reported as provenance rather than as an anomaly |
| Time integrity | Normalizer flag only | Stat-arb monitor plus host-clock offset, embedded in every quality report |
| Live startup | — | Updates before the first snapshot don't count as gaps; OKX keep-alives are not duplicates |

- New flags: `price_jump`, `wide_spread`, `trade_outside_book`, `impossible_latency`.
- The approval thresholds were extended to cover all of the above.
- The generator got new fault injections: `checksum_mismatch_at`, `price_jump_at`, `invalid_trade_at`, `schema_exception_at` and `silent_venue_after`.

## 3. Execution (MR-07, MR-08)

| Problem | Fix |
| --- | --- |
| Latency never moved the market | Orders travel for an entry latency and fill against the book *on arrival*. Each fill records expected price, actual price and latency slippage (`execution_quality.json`). |
| Plans could close while exposed | Moving to `reconciled`/`closed` raises while there is exposure or an unresolved order. New plan state: `residual_breach`. |
| Instant hedge/unwind | Hedges and unwinds are real orders with latency and retries. Retries use exponential backoff and never go faster than the venue rate limit. |
| Ack timeout hid positions | New child state `unknown`. The venue is queried with retries; venue-reported fills are recorded; nothing is resent. After retries run out the order is `timed_out`, which blocks the plan and trips the kill switch. |
| Liquidity sold twice | Consumed size is withheld until the venue's book update reflects it. |
| Route scoring ignored used liquidity | Scores use remaining depth, weighted by fill probability. |
| No leg policy | `leg_dispatch = parallel \| hardest_first`, with a per-leg exposure cap. |
| IOC leftovers never cancelled | Remainder is cancelled explicitly, with an `order_expired` ledger event. |
| No maker execution | `execution_style = maker_taker`: a post-only leg queues and each fill is hedged by a taker order; the maker order is cancelled after a TTL. |
| Stale/invalid book checks never bound | Each leg now carries its book's age and validity. |
| Reduce-only could open exposure | Reduce-only intents are capped to held positions. The strategy's stance follows actual positions. |

- **Latency model:** new `entry` stage, lognormal sampling and `new_backtest` presets.
- **Session:** `TradingSession` is a single stack used by both backtest and live paper.

## 4. Risk controls (MR-10)

- **Daily loss:** realised plus unrealised PnL, net of fees and funding, marked to the books and reset each UTC day. A breach trips the kill switch.
- **Residual limits:** enforced continuously. A breach holds new risk and forces the unwind; the kill switch trips if the breach is still open after 5 s.
- **`KillSwitchSupervisor` triggers:**
  - daily loss
  - unresolved residual breach
  - unconfirmed order
  - reconciliation break
  - stale feed (30 s)
  - venue down (30 s)
  - repeated venue rejects
  - gross exposure above limit
- **After a kill:** working orders are cancelled and positions are flattened with reduce-only plans.
- **Reducing orders:** never blocked by exposure, loss or edge limits.
- **Clearing the kill switch:** requires a clean reconciliation.
- **Reconciliation:** runs periodically, whenever no order is in flight. `timed_out` is no longer treated as a terminal state.

## 5. Live feeds, paper and testnet

| Before | After |
| --- | --- |
| `PublicFeed` never used; `mr-paper` synthetic only | `ingest.py` (`LiveCapture`, `record_live`, `mr-record`) and `live.py` (`mr-paper --feed live [--record DS]`) |
| `trading_platform_poc.ingest` did not exist | The live entry point is `trading_platform_poc.minreq.ingest` |
| `execution_mode: venue_testnet` had no effect | `testnet.py` routes to Binance Spot Testnet and OKX Demo. Startup refuses a missing or unsupported adapter, and refuses a backtest or synthetic feed with testnet. |
| Paper fills labelled `venue_testnet` | Backtest fills are `internal_simulation`, paper fills `internal_paper`, and only real testnet fills are `venue_testnet` |

New CLI flags:
- `mr-record`
- `mr-paper --feed`, `--symbol`, `--duration`, `--max-records`, `--record`, `--execution-mode` and `--latency-preset`
- `mr-backtest --latency-preset`

New config keys: `minreq.latency_preset`, `minreq.feed`, `minreq.live_symbol`, `minreq.live_duration_seconds`, and the `internal_paper` execution mode.

## 6. Tests and acceptance

- **AT-09:** the partial fill now comes from liquidity consumed while the order is in flight. The test asserts the IOC remainder is cancelled and the plan closes only when flat.
- **AT-10:** adds a venue whose acks are lost. It asserts `unknown` → query → fills recorded, reconciliation is clean and no order is resent.
- **AT-07, AT-08, AT-12:** settle in-flight orders before asserting.
- **`tests/test_minreq_hardening.py`:** 30 new tests covering sections 2–5.

Results on this machine:

| Suite | Result |
| --- | --- |
| Acceptance AT-01..AT-14 | 14 / 14 pass |
| Hardening tests | 30 / 30 pass |
| Full test tree | 821 passed; the remaining failures and errors are identical to the untouched baseline (missing `tardis_dev`, `hftbacktest`, `pyarrow`, native `_ems`) |

## 7. Live verification

- **Real feeds (25 s):** Binance, OKX and Bybit books stayed valid, with zero gaps and zero stale periods.
  - **OKX checksum:** OKX's public book channel sends `checksum: 0`, which is now treated as "not published".
  - **Clock skew:** this machine's clock is 30–70 ms behind the exchanges, so live datasets fail the negative-latency check until NTP is fixed.
  - **Certificates:** capture needs `SSL_CERT_FILE` pointing at the exported macOS trust store.
- **Live paper forward test (45 s):** ran end to end with clean reconciliation, but made no trades because the signal never reached the entry threshold.
  - **Replay:** at a lower threshold, the router refused all 692 entries for negative net edge (gap ≈ ±0.0017 USDT vs fees ≈ 0.0147 USDT per 0.001 BTC).
- **Backtest vs live accuracy is not yet measured.** Run a long `mr-paper --feed live --record <ds>`, then `mr-backtest --dataset <ds>`, and compare `tca.json` and `execution_quality.json`.

## 8. Open items

- The testnet path is tested only against fake gateways; no credentials were used.
- The default `max_residual_notional` (2,500) is below one 0.05 BTC leg (≈ 3,000).
- `uv.lock` still needs `uv lock`.
- Decide whether to merge `altcoin_mm_v1`, `backtest/` and `sim_engine/`.

## 9. Files changed

**New:**

| File | Lines |
| --- | --- |
| `src/trading_platform_poc/minreq/checksums.py` | 72 |
| `src/trading_platform_poc/minreq/ingest.py` | 263 |
| `src/trading_platform_poc/minreq/live.py` | 176 |
| `src/trading_platform_poc/minreq/testnet.py` | 700 |
| `tests/test_minreq_hardening.py` | 603 |
| `CHANGES_SUMMARY.md` | this file |

**Modified** (lines added / removed):

| File | Added / removed |
| --- | --- |
| `router.py` | +1236 / −220 |
| `quality.py` | +593 / −59 |
| `backtest.py` | +488 / −263 |
| `matching.py` | +439 / −62 |
| `riskcontrols.py` | +302 / −12 |
| `latency.py` | +144 / −10 |
| `cli.py` | +147 / −11 |
| `acceptance.py` | +141 / −42 |
| `synthetic.py` | +104 / −9 |
| `pipeline.py` | +56 / −2 |
| `statarb.py` | +37 |
| `venues.py` | +19 / −6 |
| `canonical.py` | +4 |
| `ledger.py` | +3 / −1 |
| `config.py` | +5 / −1 |
| `tests/test_minreq_execution.py` | +3 |
| `MINIMUM_REQUIREMENTS.md` | +214 / −78 |
| `INTEGRATION.md` | +24 |
| `README.md` | +2 / −1 |
