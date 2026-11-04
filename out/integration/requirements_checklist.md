# CryptoMFT requirements checklist

Extracted from Minimal Requirements v2.1, the 29 Oct review, the L3 guide, the full execution design and the MR audit (`out/integration/reports/`). IDs are quoted as they appear in the source. Where a source gives no ID, rows are located by section number, and no ID has been created. This file records what the documents require. It does not assess any code.

## Reading notes

1. **"Live" means the Paper column.** Minimal Requirements has Backtest and Paper columns only and says Live-ready is "Not granted by this document". In the "who" columns, "both" means required for backtest and for paper/live-data. "Live only" means required at the paper stage but not for a backtest. Production live needs separate evidence under the full roadmap.
2. **Conditional clauses this platform triggers.** The MR text assumes a taker-first start, with maker and derivatives items conditional. A Bybit maker / Binance USD-M perp strategy triggers them:
   - §4: "If maker orders are introduced, queue priority, amend behaviour, post-only rejects and fill calibration become mandatory rather than optional."
   - §4 and §7: funding, margin, liquidation buffer and margin-state reconciliation are "required only" for derivatives.
3. **Two requirement baselines exist.** Part 1 is the 17 August spec (`MR-xx`, `AT-xx`). The 29 October review measures against "15 October platform requirements" (`STR01-06`, `REF01-05`, `DAT01-07`, `CLK01-07`, `SIM01-07`, `ECO01-06`, `SDK01-07`, `OMS01-04`, `EMS01-03`). That baseline is not one of the five documents, so only its domain ranges are quoted here, and its individual requirements are not listed.
4. **Execution design.** Its build plan was superseded by `canonical_execution_layer_simplified.html`. Part 4 is not a line-by-line diff against it.
5. **No numeric L3 thresholds.** The L3 guide states acceptance as invariants and says its worked numbers are illustrative. MR §9 also says numeric thresholds "should be agreed". Neither document supplies pass/fail numbers.
6. **The MR audit covers a third code base.** It audited `hft-platform-kit`. It does not mention `new_backtest` or `crypto_stat_arb`. See Part 5.

---

## 1. Minimal Requirements

- **Title:** MINIMAL REQUIREMENTS: Automated data pipeline, backtesting, paper trading and OMS/EMS. Simple multi-exchange MFT crypto statistical arbitrage. Initial research release: one strategy, one logical portfolio, two or more venues.
- **Version:** Document version 2.1 - revised minimum research and paper scope
- **Date:** 17 August 2026
- **Scope boundary:** does not authorize production capital. The complete institutional roadmap and separate live-readiness due diligence remain required.

### 1a. Capability matrix (MR-01 to MR-14)

The source marks every MR as Required in both the Backtest and Paper columns.

| ID | One-line requirement | Area | Who must have it |
|---|---|---|---|
| MR-01 | Venue and channel coverage: at least two target venues; L2 depth, trades and instrument metadata; top-of-book if used by the signal | data | both |
| MR-02 | Canonical market-data model: symbols, timestamps, sides, prices, quantities, sequences and quality flags normalized without losing native fields | data | both |
| MR-03 | Instrument / security master: effective-dated tick, lot, minimum notional, contract type, multiplier, settlement and trading status | data | both |
| MR-04 | Automated historical data pipeline: restartable raw-to-canonical ingestion, partitioning, manifests, checksums, gap reports and approved datasets | data | both |
| MR-05 | Deterministic multi-venue replay: event-driven replay with stable ordering, same strategy code and explicit data-quality and control events | simulator | both |
| MR-06 | Signal and feature timing: cross-venue alignment, feature versioning, market-data age and no forward-looking joins | simulator | both |
| MR-07 | Common OMS/EMS and routing: simulated and paper orders use the same normalized order API, risk checks, route decision and order-state model | OMS-EMS | both |
| MR-08 | Parent plan / multi-leg state: opportunity-level parent with child orders, partial fills, rejects, timeouts, residuals, hedge/unwind and idempotency | OMS-EMS | both |
| MR-09 | Fees, slippage and PnL: effective-dated fees/rebates and instrument costs; gross edge separated from realized net opportunity PnL | ledger-reconciliation | both |
| MR-10 | Minimum risk controls: instrument/status, size, price-band, gross/net, venue, residual/time limits and strategy kill switch | risk | both |
| MR-11 | Ledger and reconciliation: orders, fills, positions and balances reconcile internally; paper/testnet state also reconciles to venue responses | ledger-reconciliation | both (venue reconciliation clause: live only) |
| MR-12 | Monitoring and basic TCA: data quality, replay, execution, residual, latency, costs and PnL are visible and exportable | monitoring | both |
| MR-13 | SDK, configuration and reproducibility: stable strategy and OMS/EMS APIs, versioned config, run manifest, seed and build identifiers | other | both |
| MR-14 | Measured performance baseline: ingestion, replay and OMS/EMS overhead measured for the intended universe; no live SLA is implied | other | both |

### 1b. Acceptance tests (AT-01 to AT-14)

Per §12, AT-01 through AT-11 and AT-14 gate Backtest-ready. AT-12 and AT-13 are added for Paper-ready. The Paper-ready gate also includes all backtest-ready tests.

| ID | One-line requirement (pass condition) | Area | Who must have it |
|---|---|---|---|
| AT-01 | Venue and dataset coverage: approved historical partitions for the initial universe with required channels and metadata. Evidence: coverage and quality report | data | both |
| AT-02 | Raw-to-canonical normalization: representative raw records map to the canonical schema without loss of sequence, units, timestamps or native extensions. Evidence: schema mapping and sample trace | data | both |
| AT-03 | Deterministic books: repeated reconstruction gives identical book hashes; gaps, stale periods and invalid books are flagged and recovered. Evidence: hash and exception report | data | both |
| AT-04 | Instrument mapping and sizing: two representative assets reconcile symbols, price, quantity, notional, tick/lot/minimums and economic exposure across venues. Evidence: instrument and boundary-value test | data | both |
| AT-05 | Pipeline restart and backfill: interrupted ingestion resumes without duplication or omission and produces the same approved dataset manifest. Evidence: restart diff and manifest | data | both |
| AT-06 | Replay reproducibility: same build, manifest, config and seed produce identical opportunities, orders, fills and PnL. Evidence: two-run diff with no unexplained variance | simulator | both |
| AT-07 | Cost consistency: identical fee/rebate and relevant funding/borrow logic in route decision, simulator, TCA and PnL. Evidence: sample trade reconciliation | ledger-reconciliation | both |
| AT-08 | Normal routed opportunity: a two-venue route completes and reconciles with correct exposure, costs and net opportunity PnL. Evidence: opportunity ledger | OMS-EMS | both |
| AT-09 | Partial fill / reject / unwind: one child partially fills and another rejects; hedge or unwind completes within approved residual size/time. Evidence: state-transition and PnL report | OMS-EMS | both |
| AT-10 | Timeout / idempotency / restart: lost acknowledgement, retry and process restart do not create duplicate orders or unknown state. Evidence: fault-injection report | OMS-EMS | both |
| AT-11 | Rate limit / stale feed / kill: new risk stops and open legs follow documented cancel/hedge/unwind logic; event appears in replay and TCA. Evidence: control test and audit trail | risk | both |
| AT-12 | Paper reconciliation: local orders, fills, positions and balances reconcile to paper/testnet state after normal operation and reconnect. Evidence: venue/local reconciliation report | ledger-reconciliation | live only |
| AT-13 | Monitoring and TCA: one session traceable from source market event to final opportunity PnL with data-quality and execution report. Evidence: dashboard/export and TCA | monitoring | live only |
| AT-14 | Performance baseline: historical ingestion, replay and OMS/EMS simulation complete within agreed limits; paper overhead measured by component. Evidence: benchmark tied to host and build | other | both |

### 1c. Unnumbered requirements in §§5 to 8 (source gives no IDs)

These rows hold the detail behind the MR rows for simulation, routing, risk, reconciliation and monitoring. Every row has both a backtest and a paper minimum unless marked. §§3 and 4 pipeline and instrument tables are not expanded here, except for the maker and derivatives conditions in Reading note 2.

| § | Requirement | Area | Backtest minimum | Additional paper minimum | Acceptance evidence |
|---|---|---|---|---|---|
| 5 | Event model | simulator | Deterministic replay of market data, features, strategy and risk decisions, orders, fills, cancels, rejects, control events and economic accruals | Live-data paper emits the same event types and state transitions | Identical input produces identical opportunity/order/PnL output |
| 5 | Multi-venue replay | simulator | Align venue streams without future leakage; preserve venue-local order and book sequence | Record the same canonical events used by the paper strategy | Reproducible two-venue replay over a selected day |
| 5 | Signal / features | simulator | Version feature definitions, lookbacks, normalization and timestamps; prevent forward-looking joins | Expose data age and feature health to the strategy | Feature snapshot and no-lookahead test |
| 5 | Latency | simulator | Configurable market-data, strategy, risk, submit/ack, cancel and fill distributions by venue; preserve tail behaviour | Capture actual paper-path timestamps for calibration | Latency configuration and event-pair report |
| 5 | Matching and fills | simulator | Depth-walking taker model; partial fills; maker queue/fill model only if maker orders are enabled | Distinguish internal simulated fills from venue-testnet fills | Order-type test matrix per venue |
| 5 | Failures | simulator | Rejects, timeouts, cancel rejects, rate limits, stale feeds, data gaps, disconnects and maintenance are explicit events | Connector and risk responses exercised by controlled fault injection | Known scenario or incident replay |
| 5 | Opportunity economics | simulator | Expected and realized gross edge, fees, rebates, slippage, funding/borrow where relevant, residual duration, unwind cost and net PnL | Paper report compares expected route economics with observed outcome | Opportunity-level attribution report |
| 5 | Calibration | simulator | Parameters are versioned and prior runs remain immutable | Replay-versus-paper comparison for latency, fills, rejects, costs and missed opportunities | Calibration report and out-of-sample rerun |
| 5 | Performance baseline | other | Measure ingest speed, replay events/sec, memory, strategy/RMS/OMS overhead and output volume | Measure feed-to-strategy and route-decision latency in paper mode | Benchmark report tied to build and host |
| 5 | Paper execution modes (live only) | simulator | n/a | Internal live-data paper does not prove exchange authentication or actual venue latency; testnet liquidity and latency must not be treated as production evidence | None stated |
| 6 | Common order API | OMS-EMS | Canonical submit, cancel and replace request with instrument ID, venue, side, price, quantity, order type, TIF, post/reduce-only flags and parent opportunity ID | Route through the venue adapter using the same logical request and callbacks | API specification and end-to-end trace |
| 6 | Routing control | OMS-EMS | Strategy specifies eligible venues, instrument, side, order type, price/size, route priority and maker/taker intent | Router validates account/venue status, rate-limit headroom and order support before dispatch | Route override test across two venues |
| 6 | Parent plan | OMS-EMS | One opportunity creates a parent plan with one or more child orders and a persistent plan state | Plan persists across connector/process restart and can be reconciled to venue state | Normal, partial-fill and restart scenario |
| 6 | Risk reservation | risk | Reserve simulated position, cash/margin, residual and message capacity for the package before first dispatch | Apply the same rule to paper balances and venue/account limits | Control test showing no partial admission caused by local limits |
| 6 | Concurrent / coordinated dispatch | OMS-EMS | Two-leg concurrent dispatch or a documented deterministic sequencing policy | Measure each child dispatch and callback independently | Timestamped state-transition ledger |
| 6 | State machine | OMS-EMS | Created -> risk-reserved -> dispatched -> acknowledged / partial / filled / rejected / timed-out -> hedge/unwind -> reconciled -> closed | Exchange callbacks, REST queries and private streams converge on one deterministic state | Durable ledger with no ambiguous terminal states |
| 6 | Idempotency | OMS-EMS | Unique parent and child IDs, retry keys, duplicate suppression and deterministic replay | Lost acknowledgement, reconnect and process restart do not create duplicate orders | Fault-injection test |
| 6 | Rate limits and throttles | OMS-EMS | Explicit queue/reject/delay semantics represented in simulation | Expose current usage/headroom and exact action when limits bind | Burst and control-state test |
| 6 | Residual and unwind | risk | Configurable maximum residual size/time, hedge venue priority and unwind price/urgency | Risk-reducing orders may continue under strategy stop where policy allows | Every failed package ends flat or within approved residual bounds |
| 6 | Audit and attribution | monitoring | Link source market state, signal, risk decision, route, every child order, residual and final PnL | Persist venue IDs, response codes and operator/control events | Trace one paper opportunity end to end |
| 7 | Order / fill ledger | ledger-reconciliation | Persist every submit, ack, partial/full fill, cancel, reject, timeout and synthetic control event with correlation IDs | Reconcile to venue REST/private-stream results for venue testnet | No orphan, duplicated or unexplained order/fill |
| 7 | Position / balance state | ledger-reconciliation | Recalculate position, average price, realized/unrealized PnL, available capital and reserved capacity from the event ledger | Compare local positions and balances to venue/account state after reconnect | End-of-run and restart reconciliation |
| 7 | Trade-break handling | ledger-reconciliation | Detect missing/extra fills, conflicting states and incomplete opportunities | Suspend new risk on affected venue until resolved | Injected missing-fill and late-fill test |
| 7 | Pre-trade validation | risk | Instrument/status, price-band, tick/lot/minimum notional, maximum order, venue and strategy limits | Use live data age, venue/account status and paper balance/margin | Risk matrix and one test per limit |
| 7 | Gross / net / venue limits | risk | Track strategy gross and net exposure, per-instrument and per-venue limits | Expose current utilization and binding events | Limit-breach state transition |
| 7 | Legging controls | risk | Maximum one-sided exposure, residual size and residual duration; explicit hedge/unwind action | A global or venue control must not silently strand an open leg | Controlled binding multi-leg scenario |
| 7 | Stale / invalid feed control | risk | No new opportunities from invalid or over-age books | Cancel/suspend or risk-reducing-only behaviour documented by venue | Feed-stall and recovery test |
| 7 | Kill switches | risk | Strategy stop and cancel-all; define whether risk-reducing orders may continue | PM-visible activation, audit record and reconcile-before-resume | Demonstrated kill and recovery |
| 7 | Margin / collateral (derivatives or leveraged portfolios) | risk | Model initial/maintenance margin and liquidation buffer | Monitor venue-reported margin and alert before configured buffer | Margin-state reconciliation |
| 7 | Minimum cost and PnL model | ledger-reconciliation | Versioned fees by venue/product/tier/date; depth-based taker slippage; documented maker fill/queue model where used; funding/borrow/settlement where relevant; residual, hedge and unwind costs attributed to the originating opportunity; gross edge reported separately from net PnL | Trade-level and opportunity-level reconciliation between route economics, simulated/paper execution and PnL output (applies to both modes) | None stated separately |
| 8 | Pipeline monitoring | monitoring | Job state, data freshness, partition completeness, gaps, book validity, schema/instrument version and replay speed | Live feed age, reconnects, stale/invalid state and persistence lag | Machine-readable metrics or dashboard with thresholds |
| 8 | Strategy / execution monitoring | monitoring | Opportunity count, expected edge, route, order state, fill/reject/cancel, residual, latency and net PnL | Rate-limit usage, connector health, positions, balance/margin and kill state | One paper session monitored end to end |
| 8 | Basic TCA | monitoring | Per-run venue/instrument/order-type statistics for fills, slippage, fees, rejects, residual/unwind and missed opportunities | Automated daily paper report with raw-data lineage | Sample report and reproducible calculation |
| 8 | Structured logs | monitoring | Correlation IDs for source event, opportunity, feature state, risk decision, parent plan and child order | Central searchable connector and paper-host logs | Trace one failed opportunity |
| 8 | Alert ownership | monitoring | Define owner and severity for failed ingestion, invalid book, replay failure and risk breach | 24/7 coverage not required, but every paper alert needs a named owner and response expectation | Alert matrix and forced-alert test |
| 8 | Strategy SDK | other | Stable Python SDK exposing canonical data, replay, features, portfolio, risk and OMS/EMS interfaces | Same strategy logic runs with mode-specific paper/testnet adapters | Minimal strategy port and API acceptance test |
| 8 | Versioning / configuration | other | Version strategy code, feature definitions, models, parameters, data manifest and simulator config | Paper deployment records build, config, environment and approval owner; rollback to known version | Reproduce and roll back a paper release |
| 8 | Minimal credential security | other | Offline backtests use no exchange credentials | Secret manager, least privilege, no withdrawal permission; access and changes logged | Credential injection, rotation and revocation test |
| 8 | Restart resilience | OMS-EMS | Checkpoint replay and durable parent/order state | Heartbeat, auto-reconnect, reconcile-before-resume and single active order owner | Process restart with open paper orders |

### 1d. Release gates and evidence (§§9 and 12)

| Release label | Minimum evidence | Sign-off |
|---|---|---|
| Backtest-ready | MR-01 to MR-14 complete for historical mode; AT-01 through AT-11 and AT-14 passed or formally accepted non-critical limitations; signed limitations and defect log | Platform engineering + strategy owner |
| Paper-ready | Backtest-ready plus live public feeds, paper/testnet routing, monitoring and reconciliation approved; AT-12 and AT-13 passed | Platform engineering + risk/operations + strategy owner |
| Live-ready | Not granted by this document; requires completion and separate evidence review of the full institutional roadmap | Formal live-readiness governance |

- **Evidence standard (§9):** "A roadmap statement, design description or claim that a report can be produced is not sufficient for acceptance." Each requirement needs a working artifact tied to the tested build. Tests need reproducible evidence, a named owner and a recorded defect/limitation status.
- **Deferred to the live roadmap (§11):** enterprise portfolio scale, institutional reconciliation (drop copy, enterprise trade-break workflow), advanced firm risk, full data estate, institutional security and compliance, high availability and SLA, full API estate, and advanced research (including walk-forward orchestration).

---

## 2. Crypto MFT Logic and Readiness Review, 29 October 2026

Section acceptance text is quoted in the row marked "section acceptance". "None stated" means the review gives no test for that item. Status is documentary: the review says it is "not an implementation verdict".

| Section | Required fix | Acceptance test stated |
|---|---|---|
| Overall | Do not treat an opt-in or switchable fix as active in the production profile; separate legacy reproducibility from the validated operating configuration | None stated |
| Overall | Do not treat Bybit spot maker calibration as establishing Binance perp hedge mechanics, simultaneous cross-venue execution, derivative margin or production capacity | None stated |
| Overall | Later live-test claims must identify which of sharding, backpressure, standby and fencing were exercised | None stated |
| 1 Credit | Maker calibration: report as Bybit spot, April 2026, not as a general perp or cross-venue calibration | None stated |
| 1 Credit | Model interaction: treat 74.0% as the proportional/netting-on/depth-fix-on row, not a measured result for every later opt-in rule | None stated |
| 1 Credit | Fill evidence: supply a complete held-out post-fix table for the final combined configuration (the PDF gives only precursor ablations) | Complete held-out post-fix table |
| 1 Credit | Arrival and lifecycle: specify separate entry, response and observation timing; require approved switches and multi-order exposure settings | None stated |
| 1 Credit | Hedge realism: require matched final-profile outcomes, because several loss tables illustrate pre-remediation failures | Matched final-profile outcomes |
| 1 Credit | Accounting: retain venue-specific marks and residual basis risk; a common time is not a common executable price | None stated |
| 1 Credit | Reproducibility: tie settings and provenance to the shipped release; compare backtest, OMS and live adapter semantics | None stated |
| 1 Credit | Report early/late/unfilled outcomes and net strategy cost together (proportional raises premature fills from 6.6% to 14.6%) | None stated |
| 2 Execution profile | Publish a single resolved profile per venue/product/execution style | None stated |
| 2 Execution profile | Classify each switch as a historical compatibility option, a calibrated modelling choice or a mandatory correctness/risk control | None stated |
| 2 Execution profile | Startup must reject combinations that bypass required causal fills, shared volume, pending/UNKNOWN exposure, recovery and ledger accounting; warnings alone are insufficient | Startup rejection behaviour (no named test) |
| 2 Execution profile | Profile must contain execution models: cancel model and exponent, queue placement/floor, trade/depth and snapshot netting, trade dedup, fill evidence, trade-through policy, taker book model | None stated |
| 2 Execution profile | Profile must contain timing: actual entry/cancel/report distributions, clock source, receive ordering, venue-truth mapping, post-only rejection timing, detection and query latency | None stated |
| 2 Execution profile | Profile must contain risk and recovery: explicit clip units, hard net/gross/per-venue limits, pending/UNKNOWN commitments, circuit breaker, unwind price/attempt/time limits, escalation, manual reset | None stated |
| 2 Execution profile | Profile must contain release identity: binary/source revision, engine hash, config hash, instrument/fee snapshots, calibration cutoff, test record, SDK/OMS/live mode and active account scope | None stated |
| 2 Execution profile | Demonstrate a full maker-fill-to-hedge/recovery trace under the release profile through the SDK, real OMS, risk reservation and ledger interfaces | The demonstrated trace itself |
| 2 Execution profile | Record why fill engines differ | None stated |
| 2 Execution profile | Run identical fixtures through each active backtest/paper path | Identical fixtures through each path |
| 3 Arbitrage contract | Correct the spread: buy A and sell B, `gross_edge_bps = 10000 * (bid_B - ask_A) / reference_price`; `net_edge_bps = gross_edge_bps - explicit incremental costs` | Counterexample: both venues bid 100, ask 101 must not give +99.5 bps; plus section acceptance |
| 3 Arbitrage contract | Use depth-weighted arrival execution prices when size exceeds the top level | See section acceptance |
| 3 Arbitrage contract | For a passive buy at q on A, evaluate the distribution of the future executable hedge bid on B conditional on the maker fill, including fees, hedge slippage, funding/holding cost and failure loss | See section acceptance |
| 3 Arbitrage contract | Resolve the dispatch conflict (simultaneous hit vs hedge on confirmed maker fill) in the canonical strategy contract | See section acceptance |
| 3 Arbitrage contract | PnL guard: use the cash/inventory ledger as authority; use markout for diagnosis and attribution, not as an additional cash deduction | See section acceptance |
| 3 Arbitrage contract | Specify residual/spread construction, point-in-time universe, hedge ratios and estimation cutoff, warmup, stationarity/regime checks, entry/exit and rebalance rules, portfolio constraints and model expiry | See section acceptance |
| 3 Arbitrage contract | Confirm the platform can replay the same feature and portfolio logic used live | See section acceptance |
| 3 Arbitrage contract | Section acceptance | "equal-book/no-arbitrage example; profitable executable inversion; depth-limited trade; maker never fills; maker partial fills; maker adverse fill with delayed hedge; and funding/fees across both legs. Verify opportunity estimates and ledger PnL independently." |
| 4 Clock and scheduling | If ack latency is a proxy for entry, label and calibrate that approximation; do not charge the response twice | See section acceptance |
| 4 Clock and scheduling | Model the chains separately: decision -> strategy/risk -> dispatch -> entry -> venue arrival; arrival -> processing -> ack/reject report -> strategy learns; match -> private fill delivery -> strategy learns -> hedge dispatch | See section acceptance |
| 4 Clock and scheduling | Separate public-feed delay from private cancel/fill-report delay; verify how cancelled slots are freed | See section acceptance |
| 4 Clock and scheduling | Do not remove the whole rolling median of local minus exchange time as host drift; retain raw timestamps, measure host sync independently, estimate per-venue uncertainty, keep derived corrections separate | See section acceptance |
| 4 Clock and scheduling | Use integer time units; do not rewrite receipt order to produce a monotone exchange-time story | See section acceptance |
| 4 Clock and scheduling | Process all prior events and due timers at their own times; expose only arrived observations to the strategy; define equal-time policy and sparse-event behaviour | See section acceptance |
| 4 Clock and scheduling | Use a separate venue-truth book or an explicit approximation for arrival checks | See section acceptance |
| 4 Clock and scheduling | Resolve exchange-time vs receive-time replay into explicit observation and venue timelines | See section acceptance |
| 4 Clock and scheduling | Evaluate funding, cancels, risk deadlines and required strategy timers even when the touch has not changed | See section acceptance |
| 4 Clock and scheduling | Detect two equally stale books (relative quote skew alone does not); treat the 50 ms overlap window as an association parameter, not a universal ordering rule | See section acceptance |
| 4 Clock and scheduling | Section acceptance | "delayed pre-arrival print, timer between sparse events, equal-time cancel/fill, quiet feed versus dead feed, both books stale, offset plus asymmetric latency, and no future-data change to earlier decisions." |
| 5 Fill model validation | Withdraw "unbiased" IPCW claims; check censoring assumptions, weight support near zero, clipping and effective sample size | See Required validation row |
| 5 Fill model validation | Define the target: fill under actual cancel policy (cancellation is competing) or counterfactual fill (cancellation is censoring); condition the censoring model or disclose remaining sensitivity | See Required validation row |
| 5 Fill model validation | Label RMST as E[min(T,H)], a restricted mean; include S(0)=1; state what happens after H and for never-filled orders | See Required validation row |
| 5 Fill model validation | Do not present independent 1/5/15 s classifiers with monotonic clipping as a coherent hazard model (hazards: F(t_k)=1-product(1-h_j)); state whether adverse probability is conditional on fill or joint with fill | See Required validation row |
| 5 Fill model validation | Keep outcome provenance and transfer assumptions explicit; a gate learned from simulator fills measures the simulator | See Required validation row |
| 5 Fill model validation | Never disable staleness, risk or kill controls because a prediction model is unavailable; keep exploratory no-gate runs distinct | See Required validation row |
| 5 Fill model validation | Required validation (section acceptance) | Chronological train/calibration/holdout splits with all scalers, censoring models, loss weights and hyperparameters fit before evaluation, label horizon and markout maturity in the cutoff; reliability plots, Brier/log loss, calibration by venue/side/size/regime and early/late/unfilled partitions; clustered or day-block uncertainty; final profile evaluated once after selection; fresh holdout if the study guided further fixes |
| 6 L2 limits | Distinguish empty, fully covered non-crossing price (zero visible queue is legitimate) from price outside captured depth (queue unknown, not zero); calibrate uncertainty at arrival; the review questions labelling zero queue as inherently optimistic and the 25 SOL floor from 48 observations | None stated |
| 6 L2 limits | Do not treat all_ahead or proportional as mathematical upper or lower bounds on true strategy PnL | None stated |
| 6 L2 limits | trade_confirm needs a causal budget: side/price eligible, matched to the relevant depletion, after venue arrival, volume-limited across all own orders; a later print must not backdate strategy knowledge or fund two fills | Demonstration 3 below |
| 6 L2 limits | Netting credit must be scoped, consumed once and expired on defined sequence/epoch changes | Demonstration 3 below |
| 6 L2 limits | Preserve native trade side and IDs where the historical channel provides them; do not combine raw and aggregated trades as independent volume | None stated |
| 6 L2 limits | Treat excess trade volume as a conditional diagnostic and stress residual liquidity, not a model-free hidden-liquidity bound | None stated |
| 6 L2 limits | Give multi-order queue sharing and through-price semantics their own tests, even if C++/Python mirrors agree | Own tests (none specified) |
| 6 L2 limits | Do not read a lower fill count as conservative PnL; it can selectively omit adverse fills | None stated |
| 7 Hedge recovery | Define which venue evidence conclusively resolves a sent order, the query overlap window and late-fill handling | Demonstration 1 below |
| 7 Hedge recovery | Do not release a reservation or blindly replace an order because one listing omitted it; preserve identities across reconnect and restart | Demonstration 1 below |
| 7 Hedge recovery | Compute worst-case reachable exposure across every combination of still-fillable children and UNKNOWN orders, not just filled net | None stated |
| 7 Hedge recovery | Add gross long/short, per-venue margin and collateral limits alongside hard net | None stated |
| 7 Hedge recovery | Risk-reducing classification must be valid under uncertain states, not only under believed net position | None stated |
| 7 Hedge recovery | With an unknown sell hedge: reconcile first where possible, otherwise use an explicitly bounded contingency against the reachable exposure range | None stated |
| 7 Hedge recovery | Exhausted unwind attempts need a persistent alarm, retained exposure state and manual/alternative route, never a success label | None stated |
| 7 Hedge recovery | Validate the full final recovery profile, not the entry guard alone | Before/after table for ghost delivery, partial hedge, failed unwind, long outage and restart under the same final release |
| 7 Hedge recovery | Do not use the 7.14% break-even as an operational failure-rate limit | None stated |
| 8 Accounting and margin | Value all legs at one supported time, retaining venue-specific marks, bid/ask liquidation cost and basis exposure | Common-time per-venue marks evidence (§10 ECO row) |
| 8 Accounting and margin | Suppress entry outside common data coverage; disclose remaining position/exit policy at the cutoff | None stated |
| 8 Accounting and margin | Reconcile fees, rebates, funding, fee currencies and transfers to the actual account tier/product; treat mid fallback for a missing funding mark as an approximation | None stated |
| 8 Accounting and margin | Confirm contract multipliers, lot/notional rules, account mode, collateral haircut, maintenance tiers, reserved margin and liquidation/ADL contingency | Multiplier/lot/margin tests (§10 REF row) |
| 8 Accounting and margin | Prefund both venues and have a tested rebalancing policy | Tested rebalancing policy |
| 8 Accounting and margin | Minimum economic acceptance | Current chronological out-of-sample test for measured host latency, account fees, all funding/hedge/unwind flows, realistic sizes and combined adverse assumptions; report uncertainty, number of trials, drawdown, capacity and stranded-collateral exposure; attribute historical/modelled/injected outcomes separately |
| 8 Accounting and margin | Equity bridge | Reconcile opening equity plus realised/unrealised PnL and signed financing plus external cash flows to closing equity by venue/account/asset; explain any approximation |
| 9 Architecture | Shared venue gateway: add central atomic balance/margin reservations across cells | None stated |
| 9 Architecture | Cold journal vs durable intent: define when intent is durable and what can be lost between append, replication and external send; give backpressure on required audit durability a trading policy | None stated |
| 9 Architecture | Standby and fencing: prove egress isolation or revocation before takeover, including partition and stale-writer cases; reconciliation alone does not fence a live primary | Demonstration 2 below |
| 9 Architecture | Push only actionable changes: never drop required trades, sequence/health changes, execution reports, risk transitions or timers; separate lossy analytics from lossless execution/audit streams | None stated |
| 9 Architecture | Recovery scope: define gateway/account vs instrument ownership; private feed watermarks, unknown orders, balances and global reserves must agree before resumption; persist kill/breaker state across failover | None stated |
| 9 Architecture | Performance: compare with an approved end-to-end budget; the feed-side p99 of 634 microseconds excludes the order/risk/journal/private path and stressed concurrency | End-to-end budget comparison |
| 9 Architecture | Accepting restricted single-owner launch scope must not be claimed as institutional roadmap completion | None stated |
| 10 Coverage | `STR01-06` Strategy (Partial) | "conditional hedge economics" |
| 10 Coverage | `REF01-05` Instruments (Partial) | Actual venue/product snapshots, multiplier/lot/margin tests |
| 10 Coverage | `DAT01-07` Data (Aligned direction) | Provenance, IDs/sides, message boundaries and sequence-aware reset |
| 10 Coverage | `CLK01-07` Timing (Partial) | Dual timeline contract and causal timer/ordering fixtures |
| 10 Coverage | `SIM01-07` Fills (Substantial reported progress) | Final combined profile, multi-order conservation and target-perp validation |
| 10 Coverage | `ECO01-06` Economics (ledger aligned; financing/margin/fees need confirmation) | Independent equity bridge, common-time per-venue marks and capacity |
| 10 Coverage | `SDK01-07` Runtime (compatibility unspecified) | Clean target-host SDK example, explicit engine/fallback and restart |
| 10 Coverage | `OMS01-04` and `EMS01-03` (reported; reservation and adapter semantics need confirmation) | One executable order/hedge/recovery trace through approved OMS/risk/ledger |
| Demonstration 1 | Hedge recovery under ambiguity | Maker on venue A partially fills; venue B accepts but the ack is lost; a late private fill arrives after a query and an attempted recovery. No duplicate hedge or understated reserve is permitted |
| Demonstration 2 | Fencing | Primary loses ownership while its venue connection remains alive. A standby must not send until external egress is fenced and account state reconciled |
| Demonstration 3 | Trade/depth order independence | A trade and the matching depth update arrive in either order, with two own orders at the level. Identical final queue/fill accounting and one trade budget are required |
| Demonstration 4 | Stale books and timers | Both books stop updating without relative skew, while risk/cancel deadlines expire between market messages. Entry stops and timers still execute |
| Demonstration 5 | Fresh-period perp validation | Re-run the final combined calibration profile on a fresh period of target perpetual orders; report premature, late, unfilled, reject and financial outcomes, not just hit rate |

---

## 3. L3 Style Queue Reconstruction guide, 02 November 2026

**Thresholds:** the guide sets no numeric pass thresholds. It states "Worked numbers are illustrative, not measured Tardis data or a production fill forecast." Acceptance is by invariant and by comparison. The only stated parameters are the 1/5/15/60 second reporting horizons.

**Route:** use L2 plus trades for the Binance/Bybit perp workflow. Native L3 is a separate route.

### 3a. Checklist

| Type | Item | § |
|---|---|---|
| Module | `ProviderAdapter`: native/CSV decoding, historical schema/units, raw lineage and message boundaries | 9 |
| Module | `BookRebuilder`: authoritative L2 or native MBO state, reset/gap rules, versions and validity | 9 |
| Module | `ObservationClock`: receive ordering and strategy information; separate simulated venue arrival/private reports | 9 |
| Module | `DepletionLedger`: trade IDs and single-use trade/depth credits; scoped timing/epoch and uncertainty | 9 |
| Module | `QueueModel`: ahead/behind or cohorts, cancellation hypotheses, own FIFO and conserved print budget | 9 |
| Module | `ExecutionAdapter`: post-only, placement, cancel/amend, partials, unknowns and final report delivery | 9 |
| Module | `CalibrationReport`: frozen assumptions, measured outcomes, holdout diagnostics, size/regime stresses | 9 |
| Rule, data | Incremental L2 with snapshot/reset markers, full available depth, message/row order, exchange and collector receive time; record sequence ranges and native metadata if using raw replay | 1 |
| Rule, data | One trade representation with price, size, aggressor semantics, native or aggregate ID, exchange and capture time; do not double count raw and aggregate trades | 1 |
| Rule, data | Instrument/version history: product, multiplier, quantity units, tick/lot, lifecycle, trading rules; add mark/index/funding and actual fees for full strategy backtest | 1 |
| Rule, data | Keep provider coverage/incidents, capture semantics and own order/ack/cancel/fill history; collector receive time belongs to the capture location, not the trading host | 1 |
| Rule, data | Confirm exact symbol, date and channels; use `bybit` and `binance-futures`; do not substitute spot because the symbol string matches | 1 |
| Rule, data | Retain disconnect markers; use raw replay when native sequence, ID or message detail is required; verify the pinned client version | 1 |
| Rule, data | Manifest stores provider/exchange/product/symbol/date, dataset/channel names, file hashes, capture region, schema/client/parser versions, unit scales, gaps, snapshot count and ID availability | 1 |
| Rule, data | Inspect a small sample before bulk acquisition; licence/entitlements and raw channel availability must cover the intended use | 1 |
| Rule, data | Validate collector labels such as cancel or trade against native semantics | 1 |
| Rule, book | Keep the reconstructed public book separate from simulated own orders | 2 |
| Rule, book | Partition by venue, product, instrument and connection epoch; use fixed-point/decimal prices and quantities with explicit units | 2 |
| Rule, book | Wait for a complete initial snapshot; apply a message/snapshot batch atomically before exposing a version; reset on new snapshot/reconnect | 2 |
| Rule, book | `incremental_book_L2` amount is the new absolute level size, not a signed change; zero removes the level | 2 |
| Rule, book | Do not sort equal exchange timestamps by event kind or price; preserve native sequence/capture order | 2 |
| Rule, book | Validate sequence continuity and snapshot overlap; quarantine gaps; a top-N level leaving coverage is not necessarily deleted | 2 |
| Rule, book | Publish only complete valid book versions; save book version/sequence and raw references for every simulated order arrival | 2 |
| Rule, book | Reject negative quantities and impossible unit scales; flag crossed books and timestamp anomalies; a genuine empty book differs from absent data | 2 |
| Rule, book | Do not infer a continuous queue across an unobserved interval | 2 |
| Rule, timing | Two timelines: venue timeline drives estimated matching and arrival; observation timeline delivers market data and private reports to the strategy | 3 |
| Rule, timing | `t_dispatch = t_decision + strategy_delay + risk_delay`; `t_arrival = t_dispatch + entry_delay`; `t_observed_fill = t_match + private_fill_delay`; `t_hedge_arrival = t_observed_fill + hedge_decision_delay + hedge_entry_delay` | 3 |
| Rule, timing | If ack RTT is used as proxy for entry delay, disclose it and stress the uncertainty; keep clock correction separate from transport delay | 3 |
| Rule, timing | Equal times use native sequencing, otherwise a documented deterministic fallback | 3 |
| Rule, placement | Check post-only eligibility against the estimated venue book at arrival, not the decision book; a crossing quote creates no queue | 3 |
| Rule, placement | Complete observed level: baseline ahead A=Q; new external arrivals join behind, unless venue priority differs | 3 |
| Rule, placement | Empty, fully covered, non-crossing price: visible ahead can be zero. Outside captured depth or uncertain book: mark queue unknown. No arbitrary universal queue floor | 3 |
| Rule, lifecycle | Cancel requests have a stored effective time; the order stays fillable meanwhile; release inventory/reserves only after private confirmation or reconciliation | 3 |
| Rule, lifecycle | Restart, gap or snapshot reset changes queue knowledge; the economic commitment remains until resolved | 3 |
| Rule, attribution | Identity `Q1 = Q0 + L - C - E`; `unexplained_net_change = Q1 - Q0 + E`; lower bounds are net residuals, not gross counts | 4 |
| Rule, attribution | Match on venue, price, aggressor side, message sequence, size and a calibrated association window, not timestamp proximity alone | 4 |
| Rule, attribution | Every trade, depth change and netting credit has identity, quantity budget, time/sequence scope and connection epoch; use native IDs when present | 4 |
| Rule, attribution | Trade first: reduce queue once, save explained-volume credit, deduct it from the later depth decrease before attributing residual cancellation | 4 |
| Rule, attribution | Depth first: decrease is provisional with no unsupported own fill; when the print arrives, reclassify and credit only the ahead movement already applied | 4 |
| Rule, attribution | An unexplained decrease may remove quantity ahead but must not produce a trade-confirmed fill; a later print fills only at its causal time with unspent eligible volume | 4 |
| Rule, attribution | Credit consumes once and expires under declared rules; ambiguous association retains alternate scenarios or flags the fill | 4 |
| Rule, cancellation | Retain at least three cancellation location scenarios and a calibrated candidate; validate against actual target-product orders; report outcomes across scenarios | Intro |
| Rule, cancellation | Models: Behind first `min(A, max(0, C-B))`; Proportional `C x A/(A+B)`; Ahead first `min(C,A)`; Power family `C x A^n/(A^n+B^n)` | 5 |
| Rule, cancellation | Feasibility bounds `max(0,C-B) <= C_ahead <= min(C,A)`; handle zero totals; include A/B uncertainty; define whether C is gross or net residual | 5 |
| Rule, queue | Use aggregate cohorts; initial size becomes unknown-age cohorts; our order inserted after assumed predecessors at arrival; later additions behind it | 6 |
| Rule, queue | Per-price state covers public, model, credits and provenance (raw_ids, model_version, hypothesis_seed, confidence) fields | 6 |
| Rule, queue | Never cancel our order because anonymous public depth shrank | 6 |
| Rule, queue | Multiple own orders: one shared traversal and one print budget; second order must not pay the same external queue twice; partials keep priority; cancel-pending stays eligible | 6 |
| Rule, queue | Public book unchanged by hypothetical fills; keep shadow path separate, reconcile with a declared rule, log the approximation | 6 |
| Rule, queue | Project aggregate estimates onto feasible depth at settled boundaries without generating a fill; record the projection and uncertainty | 6 |
| Rule, queue | Define through-price behaviour per venue/product and data quality; use volume-limited conservative handling as comparison; preserve cause tags | 6 |
| Rule, native L3 | Load snapshot with sequence and native IDs; buffer overlap, discard at/before snapshot sequence; detect gaps and rebuild from a valid later snapshot | 7 |
| Rule, native L3 | Maintain `order_id -> side, price, remaining size, priority metadata`; do not assume snapshot array order is FIFO unless guaranteed | 7 |
| Rule, native L3 | Apply native add, match, cancel/done and change semantics; a later removal must not recount a fill; unknown initial priority stays unknown | 7 |
| Rule, native L3 | Coinbase semantics are a venue-specific example, not a universal schema; validate native IDs and precision before any generic MBO-to-queue adapter | 7 |
| Rule, calibration | Calibrate on own actual orders for the exact venue/product; spot can inform only an initial prior | 8 |
| Rule, calibration | Observation record: order/client/exchange IDs, quote/size/side, decision, dispatch, arrival estimate, ack, fills, cancel request/confirmation, trade IDs, post-only rejects, account/product, book versions, maker flag/fee, censoring reason; include never-filled, cancelled and rejected | 8 |
| Rule, calibration | Define first vs full fill, probability by 1/5/15/60 seconds, quantity fraction, conditional adverse markout, completion-time distribution; keep denominators and cutoffs explicit | 8 |
| Rule, calibration | Distinguish actual-policy fill from counterfactual survival; IPCW needs a defensible conditional censoring model, adequate support and weight diagnostics | 8 |
| Rule, calibration | Fit on training/calibration only; keep a fresh chronological holdout with time for labels/markouts to mature | 8 |
| Rule, calibration | Model progression: trade-confirmed baseline, cancellation scenarios, calibrated conditional model, then cohort/age or hidden-liquidity terms only if they improve held-out behaviour | 8 |
| Acceptance | Manifest and sample acceptance before bulk acquisition; deliver immutable raw files, canonical events, metadata manifest and quality report | 1 |
| Acceptance | Reconstructed depth agrees with supplied reference snapshots at matched boundaries | 2 |
| Acceptance | A trade before venue arrival never fills the order | 9 |
| Acceptance | Future observations never change earlier strategy actions | 9 |
| Acceptance | Sparse timers and equal-time events follow the declared policy | 9 |
| Acceptance | One eligible print cannot fill repeated own orders beyond its available quantity | 9 |
| Acceptance | Queue/remaining quantities are nonnegative; filled plus remaining equals original size after explicit amendments | 9 |
| Acceptance | Trade-first/book-first matched cases converge without double depletion | 9 |
| Acceptance | A zero-size or vanished level alone never produces a trade-confirmed fill | 9 |
| Acceptance | Snapshot/reconnect invalidates old association credit | 9 |
| Acceptance | Dedup preserves genuine identical prints with different IDs and removes repeated deliveries with the same identity | 9 |
| Acceptance | Own fills never mutate the authoritative historical public book | 9 |
| Acceptance | Synthetic IDs are never exported as native IDs; uncertainty remains visible | 9 |
| Acceptance | A reset restores external book state but never silently cancels a live economic commitment | 9 |
| Acceptance | Restart recovers own orders and reconciles before new risk | 9 |
| Acceptance | Every fill output has order/model IDs, arrival/match/report times, causal trade ID, quantity budget, queue estimate, netting credits, source type and uncertainty flags | 9 |
| Acceptance | Final strategy results are paired with a model/settings hash and sensitivity range | 9 |
| Acceptance | Native L3: reaggregate the order map to L2 and match independent snapshots; verify message semantics, quantities, gaps and checkpoint replay | 7 |
| Acceptance | End to end: compare predicted fills and costs with held-out actual outcomes, re-run the full test strategy with observed calibration uncertainty; validate hedge/private-report latency and failure losses separately | 8 |
| Acceptance | Report reliability, early/late/unfilled/rejected rates, quantity ratio, tail timing, Brier/log loss and financial cost by venue, product, size and regime; cluster uncertainty by day/session and related orders | 8 |
| Fixture | Cancel races | 9 |
| Fixture | Two own orders at one level | 9 |
| Fixture | Empty in-coverage level | 9 |
| Fixture | Out-of-coverage price | 9 |
| Fixture | Negative/large units | 9 |
| Fixture | Iceberg-like replenishment | 9 |
| Fixture | Data truncation | 9 |
| Fixture | Worked: ahead 10, book-first head reduction 4, then matched print 4: ahead stays 6, not 2 | 4 |
| Fixture | Worked: head reduction 10, matched print 12: only 10 is credit, remaining 2 can reach our order | 4 |
| Fixture | Worked: ahead 10, behind 5, order 2, residual cancel 4, 8-unit print: Behind first ahead 10, fill 0; Proportional ahead 7.3333, fill 0.6667; Ahead first ahead 6, fill 2.0000 (illustrative) | 5 |
| Deliverable | Data contract and manifest; book reconstruction comparison; model assumptions; worked fixtures; fill provenance ledger; calibration/holdout report; strategy cost/capacity sensitivity | 10 |
| Delivery order | Exact L2 reconstruction on a small sample; one trade-confirmed order with fixed latency; multi-order sharing, association and cancellation scenarios; then calibration on actual orders with private-report timing, hedge failures and full ledger; native-L3 adapter only when order-ID history is confirmed | 10 |

### 3b. Must never be claimed

| Rule | § |
|---|---|
| L2 plus trades: never claim "The unique true order-by-order history or exact cancellation location" | Intro table |
| Native market-by-order: never claim "Perfect counterfactual fills, hidden queue priority or market impact of our own orders" | Intro table |
| Periodic top-N snapshots: never claim "Sub-snapshot event order, reliable first-touch priority or exact maker fill timing" | Intro table |
| L2 plus trades "cannot uniquely recover individual historical orders, their exact priority or which cancellations were ahead" | Intro |
| Synthetic queue IDs "can label model hypotheses, but not recovered exchange identities" | Intro |
| Do not call one model globally conservative just because it fills less; scenario ordering need not bound final strategy PnL | Intro, 5 |
| Binance/Bybit perp L2 files are not convertible into true L3; the Coinbase reference is no such claim | 7 |
| "Acceptance should state calibrated L2 queue model or observed native L3 reconstruction accurately. The two descriptions are not interchangeable." | 10 |
| A level disappearing is not proof of a cancellation or execution | 1 |
| Worked numbers are not measured Tardis data or a production fill forecast | Intro |
| Do not confuse the `all_ahead` minimum-ever baseline with a clean ahead-first model; model names must describe actual arithmetic | 5 |
| Even complete visible L3 does not reveal undisplayed liquidity, exact future response to our orders or the production host's arrival time | 7 |
| Hidden/iceberg priority and replenishment remain model risks even with visible order IDs | 6 |
| RMST to horizon H is not unrestricted expected time to fill | 8 |
| Exact equivalence for unrelated trade/depth streams cannot be guaranteed from timestamps alone | 4 |

---

## 4. Canonical Execution Layer: Design and Implementation Plan, 3 November 2026

**Status:** build plan superseded by the simplified document. This part is not a line-by-line diff against it.

### 4a. Live vs backtest sharing

| Item | Phase | Source wording or detail |
|---|---|---|
| One execution layer between strategy and `OMS02` | 0 | "used identically by the backtester and the live engine" |
| Only the adapter differs | 0 | "only the adapter at the bottom differs between backtest and live"; backtest adapter = truth books, queue model, entry and report timelines; live adapter = Bybit linear (maker), Binance USD-M (hedge) |
| Shared tactic code | 0 | "live and backtest share the tactic code and differ only in the venue adapter" |
| Shared module and parity test | 0 | "the live engine must call the same tactic code"; "phase 0 builds tactics in a module both paths import; a parity test runs identical fixtures through both" |
| Parity test location | n/a | Stated only in Risks and open decisions. It is not in the Phase 0 deliverables, Phase 0 gate or GATE 0 of the brief |
| Module location in the brief | 0 | Brief places the code at `src/new_backtest/execution/intent.py`, `src/new_backtest/execution/tactics/`, `src/new_backtest/execution/ioc_policy.py`. No import path for the live engine is given |
| Return path | 0 | Fills and order states flow up the same path; OMS02 reports to the tactic, the tactic updates the intent, the strategy sees only intent states |
| Risk rules inside the layer | 0 | "enforced inside the layer, so no tactic or pairing can bypass them" |
| Live is out of scope for now | Post 4 | Non-goal: live deployment. Backtest and paper first, then the review's path (paper, testnet, shadow, restricted live). After phase 4, paper against live feeds, then the mainnet probe programme, before any capital decision |

### 4b. Parts by phase

| Part | Phase | Detail | Stated status |
|---|---|---|---|
| `ExecutionIntent` with `worst_price` mandatory, roles, urgency, deadline | 0 | Refused without `worst_price`, except role = flatten under the breaker; strategy never builds child orders | Deliverable |
| Intent states | 0 | accepted, working, partially_done, done, escalated, failed, unknown | Deliverable |
| Tactic base class and registry | 0 | Seven tactics; "no other way to send an order" | Deliverable |
| Journal intent before first child send | 0 | R13 direction | Deliverable |
| Mutation checks at GATE 0 | 0 | Remove the price cap; allow two live hedge children; create a hedge before the fill. Each must fail a test. The plan table lists only the first | Gate |
| `legacy_v0` and v1 bit-identical | 0 | Replays on 2022-01-05 and 2022-01-13, field by field | Gate |
| Switch classification | 0 | Price caps, reachable-exposure check, breaker and one-hedge-child rule are MANDATORY in the perp profile; startup refuses a profile that disables them | Deliverable |
| `MARKET_BANDED` | 0 and 4 | Brief step 2 implements it in phase 0 as a thin wrapper. Plan table says "moved under the layer" in phase 4 | Documents differ |
| `IOC_CAPPED` | 0, then 1 | Phase 0 is "uncapped-equivalent"; the cap arrives in phase 1 with cap = conditional break-even + buffer | Small addition to existing taker path |
| Flicker law estimator | 1 | `scripts/fit_flicker.py`, `config/flicker_binance_perp.json`; fits p0, p+, p-, eta+, eta- per side, hour, volatility tercile; QQ table, KS statistic | REPLAY_2022 |
| Empirical latency law | 1 | Two-mode mixture from testnet, not the paper's exponential | TESTNET labelled |
| Attempt DP lookup table | 1 | `V_k = max_d [P(fill given d)(S - d - c) + (1-P)(V_{k-1} - phi q^2 dt)]`, `V_0` = banded-unwind value; precomputed per venue and regime; no run-time PDE solve | Deliverable |
| Hand-computed and limiting-case tests | 1 | d >= 0 and speculative formulas; urgent = cross, patient = speculative; k = 1 with large phi equals phase 0 behaviour | Gate |
| Cap and phi sweep | 1 | `cap_buffer_bps` {0, 1, 2, 4}, phi {0, 1e-4, 1e-3, 1e-2}, `deadline_s` {1, 2, 5}; on 01-05 and 01-13 | Gate; P&L labelled EXPLORATORY |
| Fault check | 1 | `hedge_reject_fast_move`, `outage_binance_2s`; no run exceeds hard net ceiling or leaves an UNKNOWN hedge unqueried | Gate |
| `PASSIVE_IMPROVE` | 2 | One tick inside the spread when spread > 1 tick | Deliverable |
| `PASSIVE_DEEP` driven by conditional edge | 2 | Rests at the depth where expected edge after the hedge clears the minimum | Deep levels not validated |
| Queue-position and imbalance features, do-not-quote filter | 2 | Both venues; effects shown with confidence intervals; pattern checked against Albers et al. | Gate |
| `maker_maker` pairing | 3 | `escalate_after_s`, joint reachable exposure, EV gate `EV = p(G + 2r - 2a) - q(L + a)`; fixtures: one leg fills, other UNKNOWN, gateway down | EXPERIMENTAL_UNVALIDATED |
| `taker_taker` pairing | None named | Described as "existing taker mode, capped". No phase row assigns it | Not phased |
| `SCHEDULED` | 4 | TWAP or volume-paced parent; `IOC_CAPPED` for the remainder at deadline | New, thin |
| `IOC_SPECULATIVE` | 4 | Better than the touch by k ticks; repeats capped per second per venue | Follows from `IOC_CAPPED` |
| Venue selector | 4 | Which venue rests vs hedges, taker split by marginal cost, fallback when primary is unavailable; logs estimated cost of every rejected alternative; never chooses a venue whose gateway is down | Gate |
| One live hedge child per hedge intent | Rule, all phases | New attempt only after the previous one is acknowledged, rejected or resolved | Mandatory |
| No blind replacement of UNKNOWN orders | Rule, all phases | Query before any retry or unwind | Risk rule |
| Reachable exposure (R10) | Rule, all phases | Worst case over filled, resting, pending and UNKNOWN children within hard net ceiling (1x) plus named fill headroom, and gross and per-venue margin limits | Mandatory |
| Rate limits | Rule, all phases | `EMS02` scoped limiter keeps capacity for cancels | Risk rule |
| Circuit breaker separation | Rule, all phases | Pause (disconnect while flat, auto-resume) is separate from trip (manual reset) | Mandatory |
| Calibration file versioning | Rule, all phases | Content hash, product field, cutoff date, source tag: MEASURED_MAINNET, MEASURED_TESTNET, REPLAY_2022, ASSUMED | Rule |
| Event provenance labels | Rule, all phases | Every child event carries historical, modelled or injected | Rule |
| Owner decisions | Pending | Urgency phi, cap buffer, deadline, `escalate_after_s`, miss-rate limit stay DECISION_PENDING_CALIBRATION until the phase 1 sweep proposes values | Open |

---

## 5. MR Compliance Audit, hft-platform-kit

- **Code base audited:** `hft-platform-kit-main`, `src/trading_platform_poc/minreq/`. It does not name `new_backtest` or `crypto_stat_arb`.
- **Audit date:** 2026-09-03, against spec doc v2.1 (17 Aug 2026).
- **Test command:** `python3.12 -m pytest -q`, reported as "80 passed / 1 env fail / 2 skipped".
- **Overall verdict:** BACKTEST-READY; PAPER-READY (LIM-01); LIVE-READY NOT CLAIMED. MRs satisfied 14 / 14 for historical backtest mode. AT-01 to AT-14 all shown PASS. Acceptance tests 80 / 81.

Open gaps:

- LIM-01: paper mode drives synthetic data, not live public sockets; `venues.PublicFeed` can be wired as the event source. Remediation is `minreq.execution_mode: venue_testnet`, owner platform engineering. The MR Paper-ready definition requires live public feeds.
- LIM-02: benchmark thresholds are host-calibrated, not universally valid; `mr-benchmark` must run on the intended execution host before AT-14 sign-off.
- ENV-01: package version is `0.0.0+local` when run from source, so `test_build_identity_reports_the_installed_distribution_version` fails. MR-13 carries the same note.
- ENV-02: `tardis_dev` optional dependency is not installed, so `test_applications.py` is skipped.
- GAP-01: P0.2 `DELTA_WITHOUT_SNAPSHOT` and `SNAPSHOT_DELTA_SYNC` have unit tests only; AT-03 in `acceptance.py` does not inject a sync-gap scenario.
- `test_binance_testnet_smoke.py` is skipped because it requires live credentials, so no testnet smoke result is reported.
- MR-08: one logical portfolio; multi-account is deferred to the live roadmap.
- MR-09: funding/borrow is covered only where instruments require it; the audit does not record perp funding evidence.
- MR-01 matrix: the listed Binance channels and REST path are `{sym}@depth@100ms`, `{sym}@trade`, `/api/v3/exchangeInfo`. No USD-M endpoint is named.
- Count mismatches in the audit text: the header says 2 stated limitations and the body lists five items (LIM-01, LIM-02, ENV-01, ENV-02, GAP-01); the header says 80 / 81 and the footer's figures total 83 including skips.
- Sign-off: MR §12 requires signed limitations and a defect log, and the audit text shows no signature.
- Timing: the audit predates the 29 October review and the 02 November L3 guide, and it does not cover the 15 October baseline domains.
