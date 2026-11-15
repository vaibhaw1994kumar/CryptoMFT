# REVISION 15 Nov 2026 (owner order "Large-size gaps + probe", item 10): the plan below is APPROVED with these limits; THEY REPLACE the 300 USDT cost cap and the earlier suggestions. Nothing has been started on mainnet; the owner launches it.
- Markets: SOLUSDT, ADAUSDT, MNTUSDT on Binance USD-M and Bybit linear. FINDING: Binance USD-M has NO MNTUSDT perpetual, so 5 of the 6 cells exist (MNT on Bybit only); the runner leaves a missing coin out of that venue and records it.
- Orders: minimum size (at most 25 USDT per order, the ceiling in `mainnet.LIMITS_V2`), PostOnly at the touch or one tick back (50/50), at most 2 open per coin and venue, some cancelled after 1 / 3 / 5 / 10 s (half of them), the rest wait for a fill up to 120 s; one price amend (one tick) on 20% of orders (UNVERIFIED against the live API: written from the docs, to be proven on testnet first); mostly random times plus 25% placed right after a sharp move (the venue's own mid moved at least 3 bps in 1 s, ASSUMED; after a fall a bid, after a rise an ask), tagged with the pre-move at send.
- Caps: inventory 200 USDT per coin and venue (above 100 USDT only the reducing side is quoted: a PostOnly unwind; a reduce-only TAKER unwind only AT 200 USDT, counted in the loss account), loss cap 20 USDT per UTC day (cancel all, flatten, pause until the next day) and 200 USDT in total (kill switch). Fees in the loss account are ASSUMED (2 bps maker, 5.5 bps taker); the exchange's own fee is in the raw events.
- Kill switch (stops BOTH venues, cancels everything, flattens reduce-only): the capture book invalid for more than 5 s (feed UNSAFE), an order in an unknown state (accepted by REST but unknown to the stream and the open-order list, a cancel refused with no terminal event, a taker unwind without a fill report), the private stream down with orders resting, a stop file, the total loss cap.
- Logging: every order is written at SEND time (event `order_sent`: plan, price, size at the price, top 5 levels, pre-moves) and again to its terminal state; never-filled and rejected orders included; aggregates only in reports.
- Where it lives: `order_lifecycle_logger/ollog` (extended, no new runner): `python3.12 -m ollog fillprobe --plan perp_v2 ...`; `--paper` simulates the private side against the live public capture (nothing is sent, no keys). Mainnet orders need `--confirm-mainnet I-ACCEPT-REAL-ORDERS`, `OLLOG_MAINNET=1`, the mainnet keys in the environment, and pass `mainnet.violations_v2` (symbols, 5,000 orders, 25 per order, 200 inventory, 2 open, 20 per day, 200 total).
- Testnet hour: DONE 15 Nov 2026 (after the owner stored testnet keys in the Keychain): Bybit testnet and Binance USD-M testnet, SOLUSDT and ADAUSDT (MNT not tried), 141 and 130 orders, 0 errors, amends verified on testnet, loss account under 0.2 USDT, largest position about 29 USDT; results in LARGE_GAPS_RESULTS.md, step 10. The earlier paper hour was interrupted by a machine restart (641 orders) and not rerun.
- Recorder: the probe's own capture (on the machine that runs it) is Bybit orderbook.50 (about 20 ms) + publicTrade and Binance depth@100ms + aggTrade, REST-checked every 30 s (no gaps, 1 of 1 REST checks exact in a 40 s live run). NEW, optional `--fast-streams`: Binance bookTicker (about 240 updates/s for SOLUSDT in a 40 s live check) and Bybit orderbook.1 (about 25/s) written to capture/<venue>/bbo/ with the update id, a backwards update id counted and noted (feed health); tested offline and by a 40 s public capture-only run (no orders, no keys); 40 s is NOT a feed-health proof, only that the streams parse. No change was made to the recorder on the VM (separate approval).
- Still the owner's decisions: go on mainnet and when; where it runs (Mac or VM) and the sub-account keys (trade only, no withdrawal); the length (1 to 2 weeks) and the day split for the gates; whether the live perp arb bot logs full order lifecycles (the question below stands).

# Perp queue validation: a PLAN (15 Nov 2026; owner approval needed before ANYTHING below starts)

Nothing in this file has been run, sent, recorded or changed. No order, no key, no recorder change, no VM action, no download. Every number marked (estimate) is arithmetic on stated assumptions, not a measurement.

## Why
- The maker queue model (queue_v2 and the fill engine) has been checked against REAL orders on Bybit SPOT only (ADA, MNT, 1,976 real PostOnly orders, 19 Apr to 2 May 2026, from out/sim_validation_spot/notes.md). On perps it is unvalidated: the only perp order evidence is TESTNET
  (13 runs, 228 natural PostOnly/GTX orders, about 58 fills; Binance testnet fills ignore queue priority, so it is plumbing only) and 8 real Market IOC orders (a touch-price check, not a queue test).
- Every maker gain this follow-up measured (T5 at 1 / 10 / 100 SOL, the E split, S0 repricing) rests on that model. A gain smaller than its interval is reported as no effect for exactly this reason.
- The spot result is itself mixed (hit 83.9% against 80.5% +/- 5 passed; false rejects 2.7% against a 2% gate failed; the calibration step showed no improvement), so the perp run must be designed to find such problems, not to confirm.

## Design
1. Markets (6 cells): Binance USD-M and Bybit linear, SOLUSDT, ADAUSDT, MNTUSDT (SOL for continuity with the arb; ADA and MNT for thinner books and a larger tick in bps). Mainnet, minimum size, notional at the venues' minimum (about 5 USDT) up to a cap the owner sets per order.
   Today's own tooling (ollog mainnet module) allows only BTC and SOL, at most 200 orders and 150 USDT notional; it needs extending (own allow-list, still typed confirm plus an env switch, keys only from environment variables, never from files).
2. Order types per market (each placed unconditionally AND conditionally):
   - PostOnly / GTX at the touch (the back of the queue) and one tick back; cancel after a lifetime drawn from {1, 5, 30, 120} s, or never (left to fill or expire after 10 minutes, then cancelled);
   - amends (price one tick, size down) on a share of orders, to measure the queue loss an amend causes;
   - some orders placed right after a sharp move (the venue's own mid moves more than a threshold, for example 3 bps in 1 s, taken from the live feed at send time), against orders placed at random moments: the adverse-regime test the arb actually cares about.
3. Our own lifecycle logging AT SEND TIME (not reconstructed afterwards): local monotonic and wall stamps at send, REST return, stream New, every fill (exec id, price, size, fee, maker flag), cancel send / return / stream Cancelled, amend send / ack, PostOnly refusal codes,
   every order to a terminal state including never-filled and rejected ones; the venue's book (touch, size at our price, top 10 levels) from our own recorder at the send stamp; a clock-offset estimate from periodic time calls. The Bybit mainnet export has no cancel-send time, which is why the stamps must be ours.
4. Recorder: public streams captured alongside on ONE clock with receive stamps, after the feed-health fixes are in: Binance bookTicker, the 100 ms depth stream and aggTrade; Bybit orderbook level 1 (about 10 ms), level 50 (about 20 ms) and publicTrade. The existing streams are not changed; the new ones are added as separate files.
   Any recorder change on the VM is a separate approval.
5. Flatten policy: every fill is hedged or flattened at once with a reduce-only market order (cost counted), so the run carries no position. A hard cap on open notional and a kill switch.
6. Length: 1 to 2 weeks. Sample size (estimate): 12 cells (6 markets x touch / one tick back); 300 orders per cell detects a difference of about 8 percentage points in hit rate at 80% power (n = 2 (1.96 + 0.84)^2 p (1 - p) / d^2 with p = 0.5, d = 0.08 gives about 245 per arm) = 3,600 orders, about 360 a day over 10 days.
7. Cost (estimate, owner sets the cap): about half the orders fill; each fill costs the flatten (a taker fee of 4 to 5.5 bps plus about one spread) and the adverse move: about 6 to 12 bps of 5 to 10 USDT per fill = about 0.05 to 0.15 USDT; 1,800 fills = about 100 to 300 USDT in total. Suggested hard stop for the whole run: 300 USDT.

## What is compared (the same validation as Bybit spot)
- Fill timing (real fill time against the simulated fill time, distribution of the difference), fill rate (hit, premature, late, unfilled, false fill) per queue and cancel configuration (queue_v2 cancel models, the fill-engine settings), markouts (side-signed mid move at +1 s, +5 s, +30 s after the fill, real against simulated),
  queue position consistency (our size-ahead at send against the time and volume the fill needed), and the effect of amends and of the after-a-sharp-move placement on each.
- Gates: the same ones as spot steps 2b, 2c and 3 (taken from out/sim_validation_spot/TOLERANCES.md), written down BEFORE any perp order is placed; the data is split by day (earlier days fit, later days check), and the check days are read once.
- What it would settle: whether the maker gains of S0 and E survive on a venue and a regime the simulator has never been checked on, and whether the cancel model's perp setting matters.

## Guardrails
- Keys only from environment variables. Order data stays local: aggregates only in reports, no order IDs and no raw API responses. No writes to data/ (a new capture goes to its own directory). Nothing is applied to the VM without a separate approval.
- Mainnet orders are the owner's to launch (typed confirm), as with ollog today; I do not start them.

## Decisions the owner has to make
1. Go or no-go for mainnet orders at all, and the cost cap (suggested 300 USDT).
2. Coins and venues (suggested: the 6 cells above), and whether amends are in.
3. Where the orders run (Mac or the VM) and which keys (a sub-account with only these markets, trade permission, no withdrawal).
4. Whether the recorder may get the new streams on the VM, and the date after which the feed-health fixes are in.
5. The length (1 or 2 weeks) and the day split for the gates.

## Question to the owner (needs an answer before this plan is final)
Does the LIVE perp arb bot log full order lifecycles today: for every maker and hedge order, the send stamp, the venue acknowledgement, every fill, every cancel or amend, never-filled orders, with exchange and local times? What the code shows: the paper and backtest runner writes order_trace.jsonl, order_timestamps.jsonl and ledger.jsonl, and the execution layer keeps an in-memory intent journal; I could not confirm what the live path writes, or for how many days it has been running on mainnet.
If it already logs full lifecycles, a part of this plan could use those orders instead (at no extra cost), and only the conditional and amend arms would need new orders.
