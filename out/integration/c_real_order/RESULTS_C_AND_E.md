# Queue items (c) and (e): results so far (build session, 17 Nov 2026)

## (c) real-order session: order books from the live feed
Built to `out/integration/designs/C_real_order_session_design.md`. Real orders stay OFF (flag `real_order_strategy`, set by nothing; the CLI refuses `venue_testnet`).
Tests (`tests/test_paper_queue_sim.py`): a VENUE_TESTNET session with FAKE venue adapters (no network) runs the profile's strategy on views fed from the live feed, with no simulated venue and no queue
executor; the same events give the same views and venue-state readings as simulated paper; refusals without the flag, for a profile missing real-order requirements (v1_5; v1_9 has 11 missing rows,
v1_10 and v1_11 none), and the CLI's venue_testnet refusal. v1_9 1 SOL identity: 14 of 14 files equal (`identity_compare.json`). The session builder and per-record journals are W2a.

## (e) Bybit tier endpoint and the testnet drill (network allowed by the user, 17 Nov)
- Bybit risk-limit endpoint: the public TESTNET response (GET /v5/market/risk-limit?category=linear&symbol=SOLUSDT, no keys) has the fields the parser reads (riskLimitValue, maintenanceMargin,
  initialMargin, mmDeduction; also id, isLowestRisk, maxLeverage, symbol), 35 SOLUSDT tiers, and an EMPTY mmDeduction on the first tier, which the parser reads as 0. Parser VERIFIED against the
  testnet response; the mainnet response is not checked (no mainnet calls).
- Testnet keys: all six are stored in the Keychain (checked by the helper, lengths only).
- Testnet amend drill: NOT RUN. Finding: the platform's testnet transports (`testnet.py`, VenueTestnetTransport.__call__) send a REPLACE exactly as a SUBMIT: on a real venue an amend would PLACE A
  SECOND ORDER instead of moving the first. The drill (place, amend, fill during a pending amend, reject, timeout, disconnect, duplicate Filled) needs a real amend in the two perpetual transports
  first (Bybit /v5/order/amend, Binance USD-M order modify), with offline tests, then a drill script on the platform's OMS. Until then `testnet_amend_drill_passed` stays false and the real-order
  amend guard keeps every reprice a cancel plus a fresh quote (that guard is what makes this gap harmless today).
- Drill item (1), whether a Binance reduce-only order is exempt from the minimum notional: NOT RUN (it needs an open testnet position; it comes with the drill).
