# Item 9: margin.missing_price_action, pause against breach (REPORT ONLY, no profile change)

| Group (run) | Action | Margin state transitions | Price pause / resume | Buffer alerts | Quotes blocked (missing price) | Kills / liquidations | Fills | Orders |
|---|---|---|---|---|---|---|---|---|
| v1_5..v1_9 (v1_9, size 1, gross 5) | pause | 14 | 1 / 1 | 1 | 69 | 0 | 16 | 13 |
| v1_5..v1_9 (v1_9, size 1, gross 5) | breach | 9 | 0 / 0 | 1 | 0 | {'KILL_SWITCH_CANCEL_ALL_COMPLETE': 1, 'KILL_SWITCH_STATE_TRANSITION': 1} | 16 | 13 |
| v1_10..v1_16 (v1_15, size 1, hedge min 5, regime file) | pause | 48 | 1 / 1 | 1 | 53 | 0 | 4 | 41 |
| v1_10..v1_16 (v1_15, size 1, hedge min 5, regime file) | breach | 26 | 0 / 0 | 1 | 0 | {'KILL_SWITCH_CANCEL_ALL_COMPLETE': 1, 'KILL_SWITCH_STATE_TRANSITION': 1} | 4 | 41 |
| v1_17 (same arguments) | pause | 48 | 1 / 1 | 1 | 53 | 0 | 4 | 41 |
| v1_17 (same arguments) | breach | 26 | 0 / 0 | 1 | 0 | {'KILL_SWITCH_CANCEL_ALL_COMPLETE': 1, 'KILL_SWITCH_STATE_TRANSITION': 1} | 4 | 41 |

The missing-price episode of the 29 Oct day:
- 20:41:56 to 20:42:00 UTC for v1_10 and later;
- 20:47:12 to 20:47:16 UTC for v1_9;
- one episode per run.

**pause:** new risk stops for 4 s (53 or 69 quotes blocked), then resumes by itself. No kill.

**breach:** the same moment trips the kill switch (`margin breach: BINANCE`, source RMS_MARGIN, `reconciliation_required_before_resume` true).
- It stays latched for the rest of the day: about 3 h 13 min.
- Fills and orders are identical on this day only because no later signal filled (the last fills are at 12:30:11 and 20:41:19).
- On a day with edge after the episode, breach would stop all quoting until a manual reset.
- There are fewer margin state transitions after the kill: 9 against 14, and 26 against 48.

**Where the two defaults differ:**
- The session runtime's config built from a profile (`crypto_stat_arb.config.raw_config_from_profile`) uses `margin.get("missing_price_action", "pause")`. Its comment says a missing price pauses new risk and never trips the kill switch (owner 11 Nov).
- `crypto_stat_arb.margin.MarginConfig.from_section` and the dataclass default use `breach`. Any core config built WITHOUT a profile (a core JSON config or a direct MarginConfig) breaches.

**Which profiles set the key explicitly:** none (v1_1 to v1_17, v1_9s, every other profile file).

**Finding:** the key CANNOT be set in a profile today. `new_backtest.profiles.apply_profile` builds the backtester's `MarginModelConfig` from the same `margin:` block and refuses it (`TypeError: unexpected keyword argument 'missing_price_action'`). So `--set margin.missing_price_action=breach` fails, and these runs set it on the raw core config through a run wrapper (`run_mpa.py`; no profile or code change).

Owner decision. No arb profile changes until then.
