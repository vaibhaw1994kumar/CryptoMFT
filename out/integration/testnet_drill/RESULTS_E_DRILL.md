# Queue item (e): the testnet amend drill on the platform OMS (build session, 17 Nov 2026)

TESTNET ONLY (Bybit api-testnet, Binance testnet.binancefuture.com), SOLUSDT linear, sizes at the venues' minimums (Bybit 0.1 SOL, Binance 0.07 SOL), keys from the Keychain helper inside the
credential broker (never printed). Network and key use allowed by the user on 17 Nov. Aggregates only; no order ids. Script: `crypto_MFT_arb_platform/scripts/testnet_amend_drill.py`.
Final run: `testnet_amend_drill.json`; earlier runs kept beside it (run 1: the public book fetch had no verified SSL context; run 2: the capability matrix still said the perpetual transports cannot
replace, so the OMS cancelled instead of amending; run 3: a book stamped before its REST fetch looked stale to the pre-trade check on Bybit).

| Step | Bybit | Binance | What it shows |
|---|---|---|---|
| A amend moves the SAME order | PASS | PASS | AMEND_CONFIRMED once, one live OMS order at the new price, ONE open drill order at the venue (no second order); cancelled clean |
| B amend to a crossing price (post-only) | ORDER ENDED BY VENUE | ORDER ENDED BY VENUE | both venues CANCEL a post-only order amended to a crossing price; the OMS recorded AMEND_TIMEOUT, made the owed venue query and shows no live order: OMS and venue agree |
| B2 amend the venue refuses | PASS | PASS | AMEND_REJECTED ("minimum order value 5 USDT" / "notional must be no smaller than 5"), the order rests unchanged at its old price, one open order |
| C amend after a fill | - | PASS (OMS level) | the quote filled (0.07); the OMS refused the amend itself (state filled), so the venue's own refusal of an amend of a filled order was not reached on the testnet (it is covered offline) |
| D reduce-only at the Binance minimum | - | ACCEPTED | a reduce-only market sell of 0.01 SOL (about 0.73 USDT, below the 5 USDT minimum notional) was FILLED: reduce-only is exempt from the minimum notional (Order 1 rule 8, now verified on the testnet) |
| Clean-up | flat, 0 open | flat, 0 open | |

Findings for the owner:
1. A post-only amend to a crossing price ENDS the order on both venues. The simulated venue ASSUMED the opposite ("rejected, order kept", 2i item 1b). Changing the simulator would change replays of
   profiles that amend (v1_9s, v1_10 and later): owner's call. The transports could also report that end at once instead of leaving it to the timeout and the query: a small follow-up.
2. The real-order amend guard stays ON: `testnet_amend_drill_passed` and `case2i_passed` are NOT changed in any profile; the owner decides.
3. The capability matrix now says the two perpetual testnet transports can replace (they amend in place); spot still cannot.
Not verified on the testnet: an amend timeout and a disconnect during an amend (not inducible on demand), duplicate Filled reports, a fill during a pending amend.
