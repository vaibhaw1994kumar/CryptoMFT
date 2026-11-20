# N-order (owner 20 Nov): N1, N2, item 9, RPI, captured-depth counter

The order was relayed by the coordinator and run in parallel with Phase 2, one writer. All of it is committed by path; nothing is pushed.

| Commit | Repo | What |
|---|---|---|
| `027c111` | core | N1, N2 |
| `b9691c8` | outer | N1, N2 tests |
| `6a82bad` | core | item 4 |
| `5f52d7e` | outer | item 4 |
| `306aa08` | core | item 5 |
| `3322ed4` | outer | item 5 |
| results commit | outer | this file and the item 9 report |

## Item 0: reproduce first (core 776c2a9, before any fix)

The cases were scripted from the order's descriptions in the existing scripted-venue harness, without ids. The MM session's own event sequences are not available here.

| Case | Pairing none (MM quote) | Arb maker quote + hedge |
|---|---|---|
| N1, part filled then amend confirmed or replacement acknowledged | REPRODUCES: `illegal parent transition PARTIAL -> ACKNOWLEDGED` | does not reproduce (fixed by 8879905) |
| N1, part filled then amend reject or timeout | reject: no; timeout: REPRODUCES as N2 (HEDGE_UNWIND) | does not reproduce |
| N2, amend timeout then query: live | REPRODUCES: TIMED_OUT -> HEDGE_UNWIND | does not reproduce (ACKNOWLEDGED) |
| N2, amend timeout then query: ended | REPRODUCES: TIMED_OUT -> HEDGE_UNWIND, plan stuck | does not reproduce (REJECTED, closes) |

**Can N1 or N2 occur on the arb path on the current core? NO.**

Why the arb's hedge-leg timeout rule reached a pairing-none plan:
- `mark_order_timed_out` moved EVERY plan that read TIMED_OUT to HEDGE_UNWIND ("timeout activates deterministic hedge/unwind policy").
- A pairing-none plan read its unconfirmed amend replacement (TIMED_OUT) as a member of the quote.
- An arb plan is judged by its maker head (`spec["maker_order_id"]`), which an unconfirmed replacement never is, so it never read TIMED_OUT there.
- It cannot happen now: `_transition` raises on HEDGE_UNWIND for a quote plan, and a test asserts it.

## Item 1, N1: DONE

- **One rule for both pairings.** `_lineage_part_filled`: a quote whose amend lineage filled part of its size reads PARTIAL whatever its head reads. Cancel-and-replace and the in-place amend share it, because the OMS models both as a lineage.
- **Unconfirmed replacements.** An unconfirmed replacement (sent, or timed out with no venue evidence) does not speak for a pairing-none quote.
- **Quantity amend.** `request_amend(..., new_quantity=TOTAL)`:
  - remaining = target less the lineage fill;
  - never enlarged (refused);
  - not at or below the fill (refused);
  - the target counts once confirmed: a pending reduction counts at once, a rejected one never;
  - the arb hedge spec covers the confirmed total.
- **Found and fixed:** `plan_store.restore_child` dropped `replaces_order_id`, `amend_to_price` and `intent_id`, so a restart lost the amend lineage. The lineage and the quantity target (AMEND_TARGET) are now rebuilt.
- **Tests** (`tests/test_oms_amend.py` `N1N2PartAmendAndTimeout`, both pairings):
  - partial, then price amend: PARTIAL through request and ack, then full fill;
  - reject and timeout keep PARTIAL;
  - partial, then quantity amend (0.008 total after 0.004: replacement 0.004, the REPLACE carries 0.008; 0.012 refused);
  - a rejected quantity amend leaves the size;
  - a fill while pending is booked once;
  - restart mid-amend.

## Item 2, N2: DONE

- No HEDGE_UNWIND for a pairing-none plan.
- After the timeout, the owed query of the old order runs, and its answer decides:
  - live at the old price: the quote carries on and the reservation is kept;
  - live at the new price (a late success): the amend is confirmed, with one live order;
  - ended: the replacement is queried as well. If the venue does not know it, the lineage has ended: the plan closes and the reservation is released.
- A later ask (and bid) on the same side is accepted.
- The arb pairing is unchanged, pinned by a test with the exact transitions of 776c2a9.

## Item 9: REPORT DONE (no profile change; owner decision)

See `item9/ITEM9.md`.
- breach turns the day's one 4 s missing-price pause into a latched kill switch, about 3 h 13 min to the end of the day.
- Pause is the profile path's default; MarginConfig's own default is breach. No profile sets the key, and none can: `apply_profile` refuses it.

## Item 4, RPI: DONE

- **Already in the core:** since 1509601 (13 Nov):
  - `MakerConfig.exclude_rpi_trades`, which joins the settings hash only when on;
  - the `rpi` argument through `on_trade` and the queue adapter;
  - `native["rpi"]`.
- **What the MM switch (5201b34, on mm-integration only) adds:**
  - a print whose `quality_flags` carry `RPI` is RPI. Upstreamed into `trade_is_rpi`; this is the only difference.
  - The MM version was an executor argument, not a MakerConfig field. The core's field form is kept.
- **Fingerprint:** every run lists `maker.exclude_rpi_trades` in `session_run_record.json` (`resolved_switches`), on or off.
  - Checked first: the run record is outside every identity comparison (`compare_sessions` skips it) and every pinned hash (GATES pins no run record).
  - **No pinned hash changes.** A manifest key would have changed every earlier manifest.

## Item 5, captured-depth counter: DONE

- **Counted per run, every strategy:**
  - resting orders placed where the capture does not reach (absent level beyond the deepest captured level);
  - fills booked on them (count and quantity);
  - taker walks that consumed the last captured level of their side inside their limit (with the quantity at that edge).
- **One definition:** `MakerExecutor._inside_captured_depth`, the rule behind the existing `outside_captured_depth` fill flag. `NO_GAP_WITHIN_CAPTURE` is not redefined.
- **Manifest:** a `captured_depth` block is written when the switch is set or anything was counted, as the participation block, so earlier manifests are unchanged.
- **Switch:** `maker.captured_depth_action`:
  - `flag` (default, left out of the settings hash);
  - `refuse`: a resting order beyond the capture is refused (reason `outside_captured_depth`); a taker walk never is, since a hedge or an unwind must go out.
- No profile was changed.

## Gates (fresh)

- **G3 v1_9 identity:** 14 of 14 equal (`g3.json`).
- **Dense arb 2i case** (`scenarios.py 2i_v110_dense2`), BEFORE (snapshot of core 776c2a9 + outer a415bbd) against AFTER (current committed code):
  - 17 of 17 files equal, the manifest included (`dense2_compare.json`);
  - amend counts unchanged: 10 requests, 4 confirmed, 6 rejected, 0 timeouts, two live orders 0, booked twice 0, overfill 0.
  - The 17 Nov outputs were moved aside to `runs/scn_2i_v110_dense2_ref17nov`, not overwritten.
- **Pair-close day** (v1_16, Phase 1's f05_close_on arguments) against Phase 1's run:
  - amend counts identical (`session_s0_audit.json` equal);
  - fills, orders, signals, ledger and every other file equal;
  - the only difference is the config echo `session_limits.config.account_tier_check`, which v1_16 gained in F07 after Phase 1's pair-close run (`pair_close_compare.json`).
- **G1:** see below.

- **G1:** the full suite, serially, alone (no replay running), on the committed code (core 306aa08, outer 44a74e8): 93 failing (92 failed + 1 error), 4051 passed, 907 s. Against the 93 baseline: UNCHANGED, no new failure, none fixed (`g1_compare.txt`).

## Per item

| Item | Result |
|---|---|
| 0 | DONE. N1 and N2 reproduce on the pairing-none quote only; on the arb path: NO |
| 1 N1 | DONE |
| 2 N2 | DONE |
| 9 | REPORT DONE; owner decision pending; no profile changed |
| 4 RPI | DONE |
| 5 captured depth | DONE |
| Gates | G1 UNCHANGED, G3 14/14, dense 2i equal (amend counts unchanged), pair-close day amend counts unchanged |
