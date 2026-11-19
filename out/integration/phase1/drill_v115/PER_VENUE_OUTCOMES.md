# Per-venue outcome table: testnet amend drill on v1_15 (F04 a, c)

Run 20 Nov 2026 (UTC date of the run: 2026-11-19 on the fixture labels), `scripts/testnet_amend_drill.py --profile perp_perp_maker_taker_v1_15`, TESTNET ONLY (Bybit linear testnet, Binance USD-M futures testnet), SOLUSDT, venue minimum sizes (Bybit 0.1 SOL, Binance 0.07 SOL), Keychain keys. Aggregates only: no order ids, no raw responses (`testnet_amend_drill.json`).

Release identity: core `b00d80e`; profile `perp_perp_maker_taker_v1_15.yaml` sha256 6b7dd81cbfb02803... (the full value in GATES.md). The drill set `oms.amend_enabled` directly; `testnet_amend_drill_passed` and `case2i_passed` were bypassed for the drill and stay false in every profile.

| Venue | Product | Order type | Step | Outcome | Source | Date |
|---|---|---|---|---|---|---|
| Bybit | USDT perp (linear) | post-only limit GTC | A: amend moves the same order | PASS: AMEND_CONFIRMED once, one OMS live order at the new price, one open order at the venue | drill_v115 | 20 Nov |
| Bybit | USDT perp (linear) | post-only limit GTC | B: post-only amend to a crossing price | PASS: order ENDED by the venue (CANCELLED, nothing resting, no second order, fills kept: 0 filled); event path AMEND_REJECTED then CANCELLED, reason AMEND_CROSS_ENDED from `rejectReason=EC_PostOnlyWillTakeLiquidity`; equal to the v1_15 simulator (end state and event path) | drill_v115 | 20 Nov |
| Bybit | USDT perp (linear) | post-only limit GTC | B2: amend the venue refuses | PASS: AMEND_REJECTED, order unchanged at its old price, one open order | drill_v115 | 20 Nov |
| Bybit | USDT perp (linear) | post-only limit GTC | C: amend after a fill | PASS (OMS level): filled 0.1, the OMS refused the amend before sending (`REFUSED:state_filled`); the venue's answer to an amend of a filled order was not exercised in this run | drill_v115 | 20 Nov |
| Bybit | USDT perp (linear) | market IOC reduce-only | D: reduce-only at the 0.1 SOL minimum QUANTITY | ACCEPTED (filled) | drill_v115 | 20 Nov |
| Bybit | USDT perp (linear) | market IOC reduce-only | reduce-only below the minimum NOTIONAL | UNTESTED (owner item 3) | - | - |
| Binance | USD-M perp | post-only limit GTX | A: amend moves the same order | PASS | drill_v115 | 20 Nov |
| Binance | USD-M perp | post-only limit GTX | B: post-only amend to a crossing price | PASS on the end state: order ENDED (`status=CANCELED`, `timeInForce=GTX` in the modify answer), nothing resting, no second order, fills kept (0); event path AMEND_REJECTED then CANCELLED with reason UNKNOWN: the venue gave NO post-only reason, so AMEND_CROSS_ENDED is not claimed and the event-path equality is NOT ASSERTED (owner condition 1a) | drill_v115 | 20 Nov |
| Binance | USD-M perp | post-only limit GTX | B2: amend the venue refuses | PASS: AMEND_REJECTED (`-5029` minimum notional 5), order unchanged, one open order | drill_v115 | 20 Nov |
| Binance | USD-M perp | post-only limit GTX | C: amend after a fill | PASS (OMS level): filled 0.07, `REFUSED:state_filled` before sending (17 Nov: the same) | drill_v115 | 20 Nov |
| Binance | USD-M perp | market IOC reduce-only | D: reduce-only 0.01 SOL, below the 5 USDT minimum notional | ACCEPTED (filled): exempt on the testnet | drill_v115 | 20 Nov |
| Both | - | - | amend timeout, disconnect during an amend, duplicate Filled | UNTESTED on the testnet (not inducible); covered on fake venues through the real-order chain (F04 b, `tests/test_perpetual_path.py` `F04bAmendFaultsThroughTheRealOrderPath`) | tests | 20 Nov |
| Both | - | - | fill during a pending amend on the testnet | UNTESTED on the testnet (not inducible on demand); fake-venue and simulator tests only | - | - |

Journal at the end (amend journal audit, aggregates): amend requests 6, confirmed 2, rejected 4, timeouts 0, ended by crossing 1 (Bybit; the Binance end is reason UNKNOWN), two live orders 0, fills booked twice 0, lineage overfill 0, no alarm. Venue-reported fills equal the OMS bookings on every drill order (Bybit 1 order compared, Binance 4, 0 mismatches). Clean-up: both positions 0, no drill order open. Transport errors 0.

Found for the owner: the Binance testnet account is at leverage 20; the profile's A8 check expects 10, so a v1_16 real-order start on that testnet account refuses until the account's leverage is set to 10 (in the account, not from code).
