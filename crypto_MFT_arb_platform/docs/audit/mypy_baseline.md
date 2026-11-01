# mypy baseline (audit item 15a)

The audit's earlier verification ran only `pyflakes`. This page records a `mypy` pass over every module the audit changed, run on 2026-10-28.

## How it was run

`mypy` isn't a project dependency. It was installed into a throwaway virtualenv (`mypy 1.11.2`) and run from `src/`:

```
mypy --ignore-missing-imports --follow-imports=silent --python-version 3.11 <files>
```

The 28 files checked are the modules the audit changed.

In `crypto_stat_arb/`:
- Data and replay: `metrics`, `capabilities`, `config_schema`, `replay`, `runner`, `canonical`, `paper_live`, `dataset_manifest`, `tca`.
- Live feed and books: `live_feed`, `monitoring`, `instrument_status`, `orderbook`, `book_gate`, `time_integrity`, `structured_logging`, `rest_reconcile`, `cross_venue`.
- Other: `maker_sim`, `tardis_compare`, `economics_gate`, `execution`, `session`, `config`, `cli`.

In `new_backtest/`: `quality_preflight`, `backtester`, `batch_backtest`.

## Result

The first pass reported 71 errors. After fixing the ones in code written during this audit, **43 remain**. None of the remaining errors is a defect.

### Fixed (code written during the audit)

- **`dataset_manifest._venue_blocks`:** it declared a 3-tuple return but returns four values. The annotation is corrected and the dicts are typed. The loop variable `key` was reused as both a string and a tuple; it is now renamed.
- **`replay.replay_order`:** the sort key now indexes a fully present ordinal list. A `type: ignore` that hid two errors is gone.
- **`cli` capabilities command:** the import is renamed to `capability_report`, so it no longer shares the `report` name used by other branches of `main()`.

### Checked and not defects (code that predates the audit)

Each of these was traced to a runtime guard or a typing limitation:

| Where | mypy says | Why it is safe |
|---|---|---|
| `book_gate.py:40` | `int \| None` passed to `mark_stale_age` | The call is inside `elif ages_ns.get(book.venue) is not None`. |
| `structured_logging.py:442, 476` | `ParentPlan \| None` assigned to `ParentPlan` | Every use is guarded by `if plan`. |
| `config.py:60` | `None` may reach `d()` | `_optional_decimal` returns before that when both are `None`. |
| `execution.py:703, 1144, 1147` | Optional limit / latency model / book | The call sites run only after the route checks that set them. |
| `runner.py` (6) | `data_quality` inferred as `dict[str, int]` | One value is a list (`silent_feeds`); `len()` of it is correct at runtime. |
| `live_feed.py` (15) | `_part_dir` Optional, `WireFrame(**dict)`, OHLC/LTP bar variable reuse, trade tuple length, `create_task` on an `Awaitable` | Parts are always open while writing; the other items are inference limits on reused names. |
| `maker_sim.py` (11) | lambdas in `clock.schedule`, `feed_lag` union, reused `size` name | Inference limits; behaviour is covered by the maker tests. |
| `canonical.py:73`, `runner.py:748` | Sink typed for `MarketEvent` receives `MarketEvent \| TradeEvent` | The dispatcher sends a sink only the types it `accepts`. |
| `session.py:268`, `cli.py:597` | Variable first assigned `None` / reused name | Inference limits. |
| `new_backtest/backtester.py:241` | `FillModelV2` passed where `FillModelV1` is annotated | Duck-typed fill models; that area is in another session's active work. |

The full list is reproducible with the command above.

## Not done

- `mypy` isn't in CI or the project dependencies.
- Making these files type-clean would mean annotating the older code above. That is hygiene, not a fix, and was left out of the audit's scope.
