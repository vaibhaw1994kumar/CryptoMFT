# Session audit: cycle-0006-20260914T142731Z

Overall: **FAIL** (11 issues)

| Component | Status | Failed/warned checks |
| --- | --- | --- |
| feed | WARN | BINANCE_stable_connection (6 reconnects/stale disconnects; last_error=URLError: <urlopen error timed out>); BYBIT_stable_connection (2 reconnects/stale disconnects; last_error=market_data_stale) |
| order_books | OK | - |
| time_integrity | WARN | host_clock_offset_within_policy (host clock -152.8 ms vs exchanges; enable NTP sync); cross_venue_skew_within_policy (1057 breaches) |
| historical_storage | FAIL | part-000001_file_sha256 (mismatch: raw); audit_completed (FileNotFoundError: [Errno 2] No such file or directory: '/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/runs/paper_watch/live/cycle-0006-20260914T142731Z/part-000001/raw.jsonl') |
| orderbook_replay | WARN | paper_session_saw_every_stored_event (0 paper vs 79365 stored) |
| backtest_replay | FAIL | audit_completed (FileNotFoundError: [Errno 2] No such file or directory: '/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/runs/paper_watch/live/cycle-0006-20260914T142731Z/paper/canonical_events.jsonl') |
| simulation | OK | - |
| dry_run | WARN | no_failed_opportunity_traces (31 failed traces) |
| strategies | OK | - |
| security_master | WARN | instrument_status_polls_succeeded (4 polls failed: ['URLError: <urlopen error timed out>']) |
| errors | WARN | no_structured_log_errors (62 ERROR records, all normal rejected-leg outcomes) |
