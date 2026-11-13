# tests/fixtures: SYNTHETIC, tests only

Small hand-made inputs for unit tests. Nothing here is recorded market data or account data: round-number BTC-USDT-SPOT books, placeholder ids (`parent-1`, `buy-1`), no keys.
They were copied byte for byte from `data/samples/` and `data/evidence/quality_gate/controlled_gap/` (owner decision 14 Nov 2026) so the tests run from a clean checkout; `data/` is untracked and is not read by any test.
Do not use these files for any P&L figure.

| Fixture | Copied from |
|---|---|
| two_venue_books.jsonl | data/samples/two_venue_books.jsonl |
| raw_depth.jsonl | data/samples/raw_depth.jsonl |
| basic_tca_report.json | data/samples/orders/basic_tca_report.json |
| failed_opportunity_trace.json | data/samples/logs/failed_opportunity_trace.json |
| binance_book_snapshot.json, bybit_book_snapshot.json | data/samples/canonical/ |
| controlled_gap_canonical_events.jsonl | data/evidence/quality_gate/controlled_gap/canonical_events.jsonl |
| project_config.yaml | config.yaml (project root, untracked): read by tests/platform_kit/test_config.py; environment-variable NAMES only, no keys |
| custom_strategy_example.py | examples/custom_strategy.py (project root, untracked): executed by tests/platform_kit/test_public_sdk.py |
