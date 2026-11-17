# W6 dislocation episodes, NOTIFY-ONLY (built 17 Nov 2026)

Design: `out/integration/designs/W6_design.md`. Profile `perp_perp_maker_taker_v1_13` = v1_12 + `dislocation` block (sha256 01e56b2c78e734e07376b2f495005c75b9cb19e73f28d2b33e8a08296f0932ff). Nothing reads an episode; no action is wired into quoting or hedging; `fail_closed.enabled` and `reference.enabled` are refused. Every threshold ASSUMED.

## Tests
- `tests/test_cross_venue.py` DislocationEpisodes (8): off and refusals; open against the baseline and close with hysteresis; thin book has no executable price at size, deep book has a net edge; a venue that stops closes UNOBSERVABLE as DATA; classes by evidence (VENUE_EVENT never by persistence alone; no feed-health monitor gives UNCLASSIFIED); all 63 combinations of DATA triggers are DATA, never TRADEABLE; the false-alarm report; the monitor imports no oms / risk / ems / session module.
- `tests/test_session_replay.py` DislocationNotifyOnlyInTheReplay: v1_13 against v1_12 on one stream, orders / fills / positions / signals / intent journal equal; only v1_13 has the episodes and the manifest key.
- Touched files together: 331 passed (plus the replay test above).

## Gates (29 Oct 2026 day, 1 SOL)
- G3 v1_9 identity against `w1d/identity_1sol_v19`: 14 of 14 equal (manifest equal outside run and release identity). `g3.json`.
- G4 v1_13 against v1_12: 17 files compared; different only `session_dislocation_episodes.jsonl` (v1_13 only) and `session_manifest.json` (profile name and hash, and the `dislocation` key). Orders, fills, signals, journals equal. `g4.json`.

## 29 Oct episode counts (EXPLORATORY, mechanics only; the capture carries REJECTED / CRITICAL)
- 81 episodes from 2,174,411 samples: DATA 75, TRADEABLE 6, VENUE_EVENT 0, UNCLASSIFIED 0. Durations: 1-10 s 62, 10-60 s 18, 60-600 s 1.
- End: 73 recovered, 8 unobservable (62.2 s unobservable in total). Reasons: FEED_NOT_HEALTHY 75, FEED_DELAYED 62, UNOBSERVABLE 36, BOOK_NOT_VALID 8, SEQUENCE_GAP 1, NET_EDGE_AT_SIZE_POSITIVE 6.
- Data limit: no derivatives (mark) stream in this replay, so VENUE_EVENT cannot be assigned.
