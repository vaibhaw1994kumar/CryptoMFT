# HFT Release Verification Matrix

Verified locally on 2026-07-31 with CPython 3.12.13 on macOS arm64. This matrix
maps release requirements to implementation, automated evidence, and protocol
or packaging sources. EMS behavior is an internal deterministic contract, so
exchange documentation is marked not applicable where no external protocol is
involved.

| Requirement | Implementation | Automated evidence | Official source |
|---|---|---|---|
| C++ EMS | `cpp/ems.cpp`; `CMakeLists.txt`; `hft_platform_kit._ems` | `tests/test_native_ems.py`; `tests/test_public_sdk.py`; wheel inspection and clean install | [pybind11 build systems](https://pybind11.readthedocs.io/en/stable/compiling.html); [scikit-build-core build procedure](https://scikit-build-core.readthedocs.io/en/stable/guide/build.html) |
| Single loop | Native `SingleLoop` and thread affinity; Python `ExecutionCoordinator._bind_loop` | `test_single_loop_orders_by_timestamp_then_sequence`; `test_stateful_native_objects_enforce_thread_affinity`; `test_coordinator_rejects_cross_loop_access_and_dispatches_on_one_loop` | Not applicable: internal scheduling/thread-affinity contract |
| Multi-leg | Native `RiskReservationBook`, `ExecutionManagementSystem`, recovery state machine; Python sequential/simultaneous dispatch | `test_risk_reservation_is_atomic_idempotent_and_rollback_safe`; native multi-leg state tests; EMS wiring failure, ambiguity, hedge, unwind, and reconciliation tests | Not applicable: independent venues cannot provide transaction-level atomicity |
| TWAP | Native `Scheduler.twap`; `ExecutionCoordinator.schedule_twap`; schedule-level `reconcile_schedule`, `cancel_schedule`, and `complete_schedule_recovery` | Deterministic timestamp/conservation tests plus OKX accepted/open, partial fill, cancel/query terminal, ambiguous query, late fill, held/released reservation, repeated reconciliation, and operator recovery tests in `tests/test_ems_wiring.py` | Not applicable: scheduling and aggregate reservation are internal contracts; venue order/cancel/query semantics follow the exchange sources below |
| VWAP | Native `Scheduler.vwap`; `ExecutionCoordinator.schedule_vwap`; `ScheduleResult` canonical fill accounting | Native conservation tests plus uneven-weight normalization, monotonic partial-fill accounting, and schedule reservation lifecycle tests in `tests/test_ems_wiring.py` | Not applicable: callers provide the volume weights; no market-volume model is claimed |
| SOR | Native `SmartOrderRouter`; `ExecutionCoordinator.preview_route` | Router fee, latency, tie-break, liquidity, malformed-quote, and wiring tests | Venue price/filter inputs follow the exchange sources below; scoring is an internal policy |
| Binance | `binance_feed.py`; `binance_execution.py`; `BinanceTestnetAdapter` | `test_binance_feed.py`; `test_binance_execution.py`; `test_binance_testnet_smoke.py`; public ping/time/ticker/exchange-info smoke | [Spot Testnet REST API](https://developers.binance.com/en/docs/products/spot/testnet/rest-api); [WebSocket streams](https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams); [filters](https://github.com/binance/binance-spot-api-docs/blob/master/filters.md); [REST security](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#endpoint-security-type) |
| OKX | `okx_demo.py`; `OKXDemoAdapter`; exact US origin `https://us.okx.com` | OKX cases in `tests/test_multi_exchange_adapters.py`, including HTTP 400/code 50004 submit/cancel reconciliation, bounded terminal polling, still-live ambiguity, and no duplicate POST; public US time/instrument smoke; missing-credential guard | [US demo trading](https://app.okx.com/docs-v5/en/#overview-demo-trading-services); [authentication](https://app.okx.com/docs-v5/en/#overview-rest-authentication); [system time](https://app.okx.com/docs-v5/en/#public-data-rest-api-get-system-time); [instruments](https://app.okx.com/docs-v5/en/#public-data-rest-api-get-instruments); [REST error 50004](https://app.okx.com/docs-v5/en/#error-code-rest-api-public-general-class) |
| Deribit | `deribit_test.py`; `DeribitTestAdapter` | Deribit cases in `tests/test_multi_exchange_adapters.py`; public time/instrument smoke; missing-credential guard | [API documentation](https://docs.deribit.com/); [authentication](https://docs.deribit.com/articles/authentication); [get time](https://docs.deribit.com/api-reference/supporting/public-get_time); [get instruments](https://docs.deribit.com/api-reference/market-data/public-get_instruments) |
| Data sanity | `data_quality.py`; `order_book.py`; `recorder.py` quarantine and manifests | `tests/test_data_quality.py`; `tests/test_data_quality_sanity.py`; `tests/test_order_book.py`; `tests/test_recorder.py` | [Binance WebSocket streams](https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams) for exchange sequence/payload semantics; local bounds/checksums are internal controls |
| Feed integrity | `data_quality.py` crossed/locked market, bid-ask monotonic ordering, duplicate price level, processing latency, and heartbeat silence detection | `tests/test_data_quality_sanity.py` (36 tests) covering each new anomaly code, blocking/non-blocking policy, per-symbol heartbeat scope, and replay state capture | Not applicable: message-level consistency and internal latency are local controls |
| Two-venue concurrent legs | `opportunity.py`; `opportunity_store.py`; `simulated_venue.py`; `opportunity-demo` CLI | `tests/test_opportunity.py` (38 tests) and `tests/test_opportunity_demo.py` (13 tests) covering concurrent dispatch, deterministic idempotency keys, duplicate suppression, per-leg timeouts, late acknowledgements, reject classification, deterministic unwind ranking, restart recovery, and four-way reconciliation | Not applicable: opportunity lifecycle, idempotency, and unwind ranking are internal deterministic contracts |
| Execution controls | `execution_controls.py`; `controls_demo.py`; `controls-demo` CLI | `tests/test_execution_controls.py` (296 tests) and `tests/test_controls_demo.py` (14 tests) covering all 40 control-matrix cells, the published `CONTROL_PRECEDENCE` order, risk-reducing exemptions across the full stage grid, atomic multi-resource reservation with rollback, the optional PM/strategy/instrument/per-order tiers, reservation drawdown and identity matching, per-leg instrument reservation for spreads, priority utilisation ceilings at admission, drain and reservation with the ceiling test and the reservation taken under one acquisition at all three, so neither two admissions, nor a drain and an admission, nor a reservation and an admission can both clear one ceiling, scoped drain STOP so one strategy's shortage cannot stall another, synchronous rejection of requests larger than their class's whole share, consume/release accounting, concurrent drain without duplicate release, and utilisation bands including every configured tier. `tests/test_execution_controls_shape.py` (9 tests) additionally enumerates the read-then-act shape behind the ceiling defect, resolving the wrapper delegation that hid one of its three sites from a name-based search, and holds it against a frozen baseline that fails both when a new pair appears and when an entry goes stale. Each fix is mutation-verified: reverting it fails a named test, and each mechanical check carries a positive control so it cannot pass by measuring nothing | Not applicable: rate-limit budgeting, throttle precedence, and kill-switch authority are internal risk controls; venue message limits follow the exchange sources above |
| Native distribution | `pyproject.toml`; `CMakeLists.txt`; `cpp/ems.cpp` | `cp312-cp312-macosx_26_0_arm64` wheel, `_ems.cpython-312-darwin.so` inspection, clean Python 3.12 install, both CLI entry points | [Platform compatibility tags](https://packaging.python.org/en/latest/specifications/platform-compatibility-tags/); [packaging binary extensions](https://packaging.python.org/en/latest/guides/packaging-binary-extensions/) |

## Local release results

- Full suite: **747 passed, 1 skipped**. The skip is the credential-gated
  Binance Spot Testnet signed smoke test.
- The default suite is hermetic: `tests/conftest.py` blocks outbound sockets
  for every test except those marked `binance_testnet`, so results do not
  depend on the runner's geography. This matters because Binance returns
  "Service unavailable from a restricted location" to hosted CI regions.
- Targeted EMS, multi-exchange adapter, and public SDK suites:
  **123 passed**. The focused OKX adapter selection passed **25 tests**.
- Two-venue opportunity suites: **51 passed** (`tests/test_opportunity.py` and `tests/test_opportunity_demo.py`).
  `opportunity-demo` runs the full lifecycle offline with no network,
  credentials, or native extensions, so it is reproducible on Windows.
- Artifacts:
  `hft_platform_kit-0.2.0-cp312-cp312-macosx_26_0_arm64.whl` and
  `hft_platform_kit-0.2.0.tar.gz`.
- Wheel metadata: `Root-Is-Purelib: false`,
  `Tag: cp312-cp312-macosx_26_0_arm64`; member
  `hft_platform_kit/_ems.cpython-312-darwin.so`.
- Clean install: native EMS import, public SDK import, TWAP/SOR sample,
  `hft-platform-kit`, and `trading-platform-poc` all passed.
- Public no-credential smoke: Binance Testnet and Deribit Test passed. OKX
  public time and `BTC-USDT` SPOT instrument requests passed through the sole
  configured official US origin, `https://us.okx.com`.

## Remaining limitations

- No Binance, OKX, or Deribit virtual credentials were available. Signed
  Binance smoke therefore skipped, and signed OKX/Deribit network operations
  were not run. All three missing-credential paths failed locally before
  authenticated submission.
- This release is US-targeted. Global and EEA OKX deployments require their
  separately documented regional API origins and a corresponding regional
  configuration; origins are not interchangeable.
- Public endpoint checks establish reachability and response parsing only; they
  do not validate exchange account permissions, balances, order acceptance,
  fills, cancellation, or reconciliation against a real virtual account.
- SOR is deterministic price/fee/latency ranking over caller-supplied quotes.
  It does not claim predictive fill probability, production latency
  measurement, or atomic fills across venues.
- The control-matrix test transcribes `CONTROL_MATRIX` rather than deriving it
  from the specification independently. It reliably detects an unintended
  change to a cell, but it does not by itself prove a cell matches the
  written requirement; that mapping was reviewed by hand.
- Venue message limits, throttle capacities, and staleness thresholds are
  configuration supplied by the operator. The control plane enforces whatever
  budgets it is given; it does not discover a venue's true published rate
  limit, and it does not measure real network transmission.
- `ExecutionControlPlane` reserves and accounts for message capacity but does
  not itself transmit. Wiring an admitted decision to an exchange adapter,
  and honouring `retry_after_ms`, remains the caller's responsibility.- TWAP/VWAP schedules conserve quantity and hold aggregate reservation until
  every route is query-confirmed terminal. Ambiguous exchange state requires
  reconciliation or explicit operator resolution. VWAP uses caller-supplied
  weights and does not estimate a live volume curve.
- The native wheel is specific to the building interpreter and platform. Linux,
  macOS, and Windows wheels for CPython 3.12 and 3.13 are produced by
  `.github/workflows/wheels.yml` using `cibuildwheel`. That workflow first ran
  green end to end on 2026-08-01 (run `30696301714`): all eight jobs passed and
  it published `wheels-ubuntu-latest`, `wheels-ubuntu-24.04-arm`,
  `wheels-macos-14`, `wheels-windows-latest` and `sdist`. Every earlier run had
  failed, because the retired `macos-13` runner was never scheduled and the job
  was reported as cancelled rather than failed. Locally built
  artifacts cover macOS arm64 / CPython 3.12 only. 32-bit targets, musl/Alpine,
  macOS x86_64, and CPython 3.14 are out of scope because `pyarrow` and
  `hftbacktest` publish no 32-bit wheels, numba/`llvmlite` publish no musllinux
  and no `macosx_x86_64` wheels, and upstream `numpy` pins block 3.14.
- Windows uses `winloop` instead of `uvloop`, which publishes no Windows
  wheels. Windows dependency resolution was verified with
  `uv pip compile --python-platform windows --no-build` for CPython 3.12 and
  3.13, but no test run on a Windows host has been performed.
