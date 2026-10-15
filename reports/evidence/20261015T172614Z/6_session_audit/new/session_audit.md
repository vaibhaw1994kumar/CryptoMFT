# Session audit: cycle-0006-20260914T142731Z

Overall: **INPUT_MISSING** (9 issues)

| Component | Status | Failed/warned checks |
| --- | --- | --- |
| feed | WARN | BINANCE_stable_connection (6 reconnects/stale disconnects; last_error=URLError: <urlopen error timed out>); BYBIT_stable_connection (2 reconnects/stale disconnects; last_error=market_data_stale) |
| order_books | OK | - |
| time_integrity | WARN | host_clock_offset_within_policy (host clock -152.8 ms vs exchanges; enable NTP sync); cross_venue_skew_within_policy (1057 breaches) |
| historical_storage | INPUT_MISSING | part-000002_files_present (required input missing: /Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/runs/paper_watch/live/cycle-0006-20260914T142731Z/part-000002/raw.jsonl, /Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/runs/paper_watch/live/cycle-0006-20260914T142731Z/part-000002/wire.jsonl) |
| orderbook_replay | OK | - |
| backtest_replay | WARN | latency_model_matches_forward_test (latency model needs calibration: BINANCE.risk.p50_ns,BINANCE.risk.p99_ns,BINANCE.strategy.p50_ns,BINANCE.strategy.p99_ns,BYBIT.fill.p99_ns,BYBIT.risk.p50_ns,BYBIT.risk.p99_ns,BYBIT.strategy.p50_ns,BYBIT.strategy.p99_ns) |
| simulation | OK | - |
| dry_run | WARN | no_failed_opportunity_traces (31 failed traces) |
| strategies | OK | - |
| security_master | WARN | instrument_status_polls_succeeded (4 polls failed: ['URLError: <urlopen error timed out>']) |
| errors | WARN | no_structured_log_errors (62 ERROR records, all normal rejected-leg outcomes) |
