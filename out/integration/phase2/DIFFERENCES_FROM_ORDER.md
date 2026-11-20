# Phase 2: differences between the order text and the code, before any change

Written 20 Nov 2026, before any Phase 2 code change. Code read at core `b00d80e`, outer `9465615`. Nothing has been changed yet. While this was written, the Phase 1 G1 rerun was running (`g1_phase1.sh`), so no code was edited.

Each point is marked **ASK** (an owner answer is needed before that part is built) or **NOTE** (I follow the order text as written; this is what it means in the code).

## F06 change (the owner's decision on the latch)

1. **ASK: what happens to v1_16.**
   - `hedge_faults.dust_max_age_s` is already set to 300 in v1_16. If the core rule is changed in place, v1_16 changes behaviour and its Phase 1 results no longer reproduce.
   - Proposal: a new switch, `hedge_faults.dust_bound_rule: latch | aggregate`. The default is `latch`, so v1_16 is unchanged. v1_17 sets `aggregate`.
   - With `aggregate`:
     - the age bound is an alarm only;
     - the notional bound blocks every new quote except dust-reducing ones;
     - carried dust is listed as an open item at the end of the session.
   - The alternative is to change the rule in place and re-record v1_16. I do not recommend it.

2. **NOTE: which quote is "dust-reducing".**
   - A dust balance is kept per hedge venue and instrument, with a signed exposure. Positive means the session is long, so a SELL hedge is owed.
   - Worked example: the Binance SOL row carries +0.01 (long, a SELL hedge owed). The quote still allowed is a **Bybit maker BUY**, because its fill is hedged by a Binance SELL and that hedge takes the 0.01 in.
   - So the allowed quote ADDS exposure on the maker venue, which looks backwards. A Bybit maker SELL stays blocked.
   - A quote reduces only the dust of its own instrument, netted across venues as the Phase 1 bound already does.
   - **Code fact, for the owner:** with the existing netting (`_hedge_for_maker_fill_dust`), a fill in the OPPOSITE direction also clears the dust whenever the combined hedge passes the venue minimum. Example: a maker SELL of 1.0 against +0.01 carried gives a hedge BUY of 0.99, and the carried dust becomes 0.
   - The order allows only the reducing direction, and I follow that.

3. **ASK: the quotes already resting when the bound is reached.**
   - Phase 1 withdraws every resting opening quote once, keeping hedges, unwinds and reduce-only closes.
   - The order says only that new quotes are blocked.
   - Proposal: still withdraw them, but keep the dust-reducing ones, so an allowed quote is not cancelled and then sent again.

4. **NOTE: the "signals blocked" figure changes meaning.**
   - In Phase 1 the session checked the bound BEFORE `strategy.evaluate`, so 370,535 counted strategy-loop passes. At that point the side of the quote is not known.
   - The directional rule needs the side, so the check moves to after `evaluate`. The counter then means opportunities blocked.
   - The rerun reports both figures:
     - loop passes while the bound was active;
     - opportunities blocked.
   - `_quote_new_risk_block`, the market-making path, gets the same directional rule, per quote action.

5. **NOTE: the "operator summary".** No output has that name. The age alarm, the end balance and the open items go to:
   - `oms.dust_report()["bound"]`, the end-of-day report;
   - the replay summary;
   - the `paper_live` session summary (`summary.json`), which is the summary an operator reads after a real-order session.

6. **NOTE: "per direction, all venues and symbols".**
   - Phase 1 values the dust by netting the same instrument across venues, then summing long and short separately over instruments. That rule is kept.
   - DUST_AGE_REACHED is recorded once per balance episode (per venue and instrument), and again only after that balance has cleared.

## F03 time source

7. **ASK: which state wins when both limits are exceeded.**
   - The order's uncertainty formula is |offset| + root dispersion + root delay / 2, so it contains the offset.
   - With chrony, a large offset therefore also makes the uncertainty large. A "tight measurement with a large offset" cannot come from chrony when the offset limit is at or above the uncertainty limit.
   - Proposal: the gate checks the offset first, so "offset" wins over "uncertain".
   - The order's two tests are written against the gate itself, with injected measurements, which is the way the A3 tests already work:
     - (offset 20 ms, uncertainty 1 ms) gives "offset";
     - (offset 1 ms, uncertainty 20 ms) gives "uncertain".
   - A chrony-built case is added for each state.
   - `max_offset_ms` is ASSUMED: placeholder 5.0, the same as the uncertainty limit, for the owner to set.

8. **ASK: "the residual after correction".**
   - `correct_host_offset`, which is on in every session profile (owner 8 Nov, Q-G4), corrects nothing on the clock. It only shifts the receive-latency CHECK in `TimeIntegrityMonitor`, by a low-quantile estimate of (receive − exchange). That estimate is at most 0 and only sees a host clock behind the venue.
   - Nothing corrects the gate's measurement or the timestamps.
   - Proposal: when the correction is on, the offset limit applies to |measured offset − the correction in force|. That is the error the latency check is still exposed to. Both numbers and the residual are recorded, and the manifest states "offset limit applied to the residual after the host-offset correction (estimate X ms)".
   - Alternative: the limit applies to the raw chrony offset, and the manifest says the correction does not touch the clock.
   - Without an answer I would build the proposal.

9. **NOTE: chrony output format.**
   - `chronyc` on this Mac fails to start (a missing library). That is a real case of "absent", and the gate gives "unknown".
   - So no real output can be recorded here. The parser reads the human-readable `chronyc tracking`, whose "seconds fast/slow of NTP time" wording carries the sign without guessing.
   - The test fixtures are built from the documented format and labelled UNVERIFIED until the measurement script records real output on the production host. The script saves the raw output for that check.
   - Offset = local minus NTP: "fast" is positive. measured_ts = the chrony Ref time (its last update), so the 900 s age rule covers a chrony that stopped updating.
   - "Unsynchronised" means any of: Leap status "Not synchronised", reference ID 00000000, or stratum 0. Each gives no measurement, so the gate is "unknown".

10. **NOTE: "both numbers recorded with every evaluation".**
    - The gate is evaluated on every strategy pass, hundreds of thousands per day, so one row per evaluation is not practical.
    - Built instead:
      - one CLOCK_MEASUREMENT row per new measurement, about every 300 s, with offset, uncertainty, the gate state and every chrony field;
      - offset and uncertainty on every gate state change (LIMIT_STATE);
      - per-state evaluation counters with the largest |offset| and uncertainty seen.

11. **NOTE: cross-check against venue server time.**
    - With `source: chrony`, the server-time probe still runs every 300 s.
    - The alarm CLOCK_CROSS_CHECK_DISAGREE is raised when |server-time offset − chrony offset| is above the probe's own uncertainty (half its round trip). That is one alarm per episode.
    - It does not stop quoting. The order says the cross-check raises an alarm and nothing more.

12. **ASK: scope of "intervals and deadlines use the monotonic clock".**
    - The OMS, the session and the live feed all run on one nanosecond timeline, `clock_ns`. That is `time.time_ns` (wall time) in `paper_live` and in `live_feed`.
    - Replays run on the data's own timestamps and are not affected.
    - Proposal, live path only:
      - `paper_live` builds a session clock anchored once to the wall clock and advanced by `time.monotonic_ns`, used for every interval and deadline in the session, OMS and feed;
      - a step detector compares wall and monotonic deltas each tick and records WALL_CLOCK_STEP (size, before, after) above a threshold. The threshold is ASSUMED at 50 ms;
      - the gap between the session clock and the wall clock is added to the gate's offset, so a step that leaves the session clock wrong stops quoting through the offset limit;
      - no re-anchoring, because re-anchoring would itself step the session timeline.
    - Replays, G3 and G4 are unchanged.

13. **NOTE: no fallback.** If chrony is absent, cannot be run, is unsynchronised, or its last update is older than 900 s, the gate is "unknown" and quotes stop. With `source: chrony`, the server-time probe never replaces it. Real-order start-up with `clock_offset` still refuses to start without a source.

14. **NOTE: the measurement script (d).**
    - It extends ollog (`order_lifecycle_logger/ollog`, tracked in the outer repo) rather than adding a new script. ollog already has a public capture for Bybit linear and Binance USD-M (wall and monotonic stamps, venue sequencing checks) and the best-of-5 server-time probe.
    - It becomes a new subcommand, `python3.12 -m ollog hostmeasure --hours 24`, which records:
      - `chronyc tracking` every 60 s (raw text kept);
      - REST round trips per venue (the time endpoint, every 10 s);
      - websocket latency per venue: receive minus the venue's event time, with the chrony offset recorded beside it;
      - the public book capture.
    - From those records, a `derive` step computes cross-venue lead-lag (which venue's mid moves first, and by how much) and the book publication interval per venue (the gaps between the venue's own event times).
    - Mainnet PUBLIC market data only, no keys and no orders. It needs `--public-mainnet`, because ollog defaults to testnet hosts.
    - I do not run it against the venues here; the owner runs it on the production host. Offline tests run on recorded or constructed frames.
    - The "pre-migration" label goes only on v1_17 and new outputs. Changing the core `singapore` preset label would change v1_9 manifests and break G3 14/14.

15. **NOTE: alignment.py (e).** This is reported in the results file. In short:
    - it merges per-venue streams by `receive_ts_ns` first (the `ORDERING_RULE`) and judges book age as `now − last_receive_ts_ns`;
    - it never reads a clock and never rewrites venue timestamps.
    - With a monotonic-anchored session clock, the receive stamps stay on one timeline, so it needs no change.

## F02 size envelope and ladder

The data inventory was read-only, with no network.

17. **ASK: the list of 40 to 45 perp symbols.** No such list exists in the repo.
    - The 22 spot symbols appear in four config files:
      - AAVE ADA APT ARB ATOM AVAX BNB BTC DOGE DOT ENA ETH FIL INJ LTC MNT NEAR OP SOL SUI UNI XRP.
    - A 30-pair list is in `new_backtest/config.py` `ALL_PAIRS`:
      - the 22, plus LDO LINK LIT TON TRX VVV WLD ONDO.
    - Proposal: the table starts with these 30 rows.
    - The owner adds the other 10 to 15 symbols, or tells me how to choose them (for example the largest open interest on both venues).

18. **ASK: contract master data for the other symbols needs a download (your yes).**
    - The code exists. `crypto_stat_arb/contract_master.py` reads Binance `exchangeInfo` and Bybit `instruments-info`: tick, step, min quantity, max quantity, min notional.
    - On disk, only SOLUSDT is present, in `config/instrument_rules_solusdt_perp.json` (current rules, 8 Nov). The rest is a BTC schema sample.
    - Two max-order fields are not captured yet:
      - Binance MARKET_LOT_SIZE (the market max);
      - Bybit `maxMktOrderQty`.
      I would add both to `contract_master.py`.
    - Proposal: one read of the two public endpoints (no keys), saved under `config/contract_master/` with the fetch date, then re-read each time the table is rebuilt.
    - Without that yes, every symbol other than SOLUSDT is NO DATA in its filter cells.

19. **ASK: risk tiers per symbol.**
    - Bybit `/v5/market/risk-limit` is public; it would come in the same download as point 18.
    - Binance `leverageBracket` is signed, so it needs mainnet keys. That is never allowed here.
    - The only Binance tiers on disk are the SOLUSDT testnet account (F07 fixture).
    - Proposal:
      - Binance initial and maintenance margin cells: ASSUMED (the v1_16 tier table) for SOL, NO DATA for the rest;
      - Bybit: from the public download.

20. **NOTE: two-venue depth.**
    - Only SOLUSDT has depth on both perp venues:
      - the 29 Oct 2026 capture: Bybit `orderbook.50`, so at most 50 levels; Binance diff-depth on a REST snapshot;
      - 2022-01-05 and 2022-01-13.
    - The 15 Nov mainnet fillprobe capture has SOL and ADA on both venues, but it is about one hour of top-20.
    - ADA is therefore reported NO DATA for the envelope, with a note that a short capture exists. No other symbol has two-venue depth.
    - March 2024 is ruled out for the hedge side: Binance is best bid/ask only there.
    - A rung whose hedge would walk beyond the captured depth is FAILED, never priced from depth that is not in the data.

21. **ASK: parent vs child, and which limits stay fixed.**
    - **How sizing works in the code:**
      - The "parent" is `arb.max_position_base`: the net ceiling the parent builds, which `run_day.py --size` sets.
      - The "child" is the quote size, `arb.max_clip_base`. It falls back to the parent size and is capped at `max_clip_notional`, 25,000 USDT by default.
      - The hedge is one child per maker fill.
    - **At SOL ≈ 96–100 USDT on 29 Oct**, the parent rungs come to:

      | Parent | SOL |
      |---|---|
      | 1k USDT | ≈ 10.2 |
      | 5k USDT | ≈ 51 |
      | 10k USDT | ≈ 102 |
      | 20k USDT | ≈ 204 |
      | 30k USDT | ≈ 306 |
      | 50k USDT | ≈ 510 |

    - **What happens to limits as size rises:**
      - Limits that follow the size already:
        - the net ceiling: (1.0 + 0.25) × size;
        - the daily loss stop: −5 USDT per SOL of clip;
        - the maximum residual, unless `below_one_lot`;
        - the margin warning, at the hard cap.
      - One that does not: `max_venue_imbalance_base` (gross per venue) is fixed at 2.0 SOL in v1_16. A 10 SOL parent cannot run unless it is raised.
      - The 25,000 USDT `max_clip_notional` caps the child below the 30k and 50k parents.
    - **Proposal for "a size increase never loosens a risk limit":**
      - These limits are CAPACITY, by definition the size of the rung, and follow it:
        - the net ceiling;
        - the per-venue gross at the same multiple as v1_16 (2× the clip);
        - `max_clip_notional`, raised only for the child of that rung.
      - These are RISK limits, kept at their v1_16 values in USDT or seconds:
        - the dust bound, 50 USDT;
        - the order rate, 2/s with a burst of 5;
        - the hedge deadline and the ack deadline;
        - the margin-warning multiple, 10×;
        - the missing-mark kill, 60 s;
        - the clock gate.
      - The daily loss stop is open. It is defined "per SOL of clip", so it grows with size.
        - Fixing it in USDT at the 1 SOL value (5 USDT) would stop every rung on its first fees.
        - Proposal: keep the per-SOL rule (the owner's rule as written) and report the USDT figure per rung.
      - A rung is FAILED if it needs any RISK limit above its v1_16 value: a longer hedge deadline, a higher order rate, or a larger residual than one parent.

22. **NOTE: what the ladder runs.**
    - The full 29 Oct day on v1_17, `singapore` latency, pair close ON (F05), so exits are exercised.
    - Two series:
      - (i) the parent rises with the child at the v1_16 rule (child = parent, capped at 25k);
      - (ii) the child is held at 1k USDT (≈ 10 SOL) while the parent rises.
    - Both directions and repeated hedges come from the day itself. "Burst fills" are the maker fills that arrive within one hedge round trip of each other, counted from the run. Nothing is injected, because a fill needs a real trade.
    - About 12 full-day replays at roughly 20 min each, run 3 at a time.
    - Per rung, the report gives:
      - cost: hedge slippage in bps against the decision price, with P50, P90 and P99;
      - completion: time to hedge, time to exit, and the share completed;
      - residual risk: peak unhedged base and USDT, and the dust;
      - margin: peak initial margin per venue and the free-margin multiple;
      - throughput: orders per second at the 2/s limit, and amends;
      - the walk depth used on the hedge venue against the depth captured.
    - All P&L is EXPLORATORY, with profile, size, day and fees.

23. **NOTE: capital (c).**
    - Per-venue headroom is the margin, at the v1_16 tiers (ASSUMED), for:
      - the worst outstanding maker fill during a cancel (the whole resting child filling);
      - plus its hedge;
      - plus the exit at the stressed price move. The move is ASSUMED: the largest 60 s move on the day.
    - It is funded on each venue separately, with no transfer.
    - An entry check stops new quotes when either venue cannot fund that. This is the existing margin-warning stop (A4) with the stressed figure added, behind a v1_17 key that is off in the core.
    - The total capital the envelope implies = the sum over symbols with data. Symbols with NO DATA are listed, not extrapolated.

## G1 for Phase 1

16. **NOTE.** The four tests run alone, serially, three times each. Then the full suite runs serially once against the 93 baseline, while nothing else runs. Results are in `g1_alone.tsv` and `g1_compare_baselines_serial.txt`.
