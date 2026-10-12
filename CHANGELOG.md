# Changelog

## Unreleased

### Added

- Tagging now performs the release. The `wheels` workflow gained an
  `acceptance` job that keeps the evidence bundle produced under the commit
  under test, and a `release` job that attaches every wheel, the sdist and
  that bundle to a GitHub release. The release job refuses a tag that
  disagrees with `project.version`, and takes its notes from the matching
  changelog section, so a tag without one fails instead of publishing an
  empty description. Releases go to GitHub and GHCR rather than PyPI: the
  repository is private, and a PyPI upload is public and permanent.
- `.github/scripts/check_evidence.py` judges an evidence bundle rather than
  trusting the exit code of the command that wrote it: fourteen named checks,
  each with an owner, a non-placeholder build identity, the defect,
  limitation and sign-off sections, and all eight Section 10 deliverables.
  Both the host and container workflows now use it, so they cannot drift
  apart.

### Fixed

- CI ran the test suite with `uv run` rather than `uv run --frozen`. `uv run`
  performs its own resolution and updates `uv.lock` when it disagrees with
  it, so the suite could pass against a dependency set the lock does not
  describe, which is the one thing the lock exists to prevent.
- The `image` workflow granted `packages: write` to every job, including the
  one that only builds and tests. The token is now scoped to the job that
  publishes.
- `hft-export` failed unreadably when handed the wrong event log. Two logs
  exist in this repository, and the recorder-normalized one it consumes is
  not the canonical one `mr-generate` writes; feeding it the latter raised a
  bare `KeyError: 'buyer_is_maker'` traceback, and feeding it the NPZ this
  command itself produces — the likely result of swapping `--data` and
  `--output` — reported a UTF-8 decode failure at byte 14. It now names the
  file, the event position and the missing field, and says which log it
  expects. Malformed JSON reports its line number instead of surfacing the
  decoder's own message.
- `hft-synthetic` wrote its dataset to `synthetic_hft_data/` in the working
  directory, which for anyone running the documented command was the
  repository root. That path was neither ignored by git nor excluded from the
  sdist, so a demo left untracked files behind and a build that followed one
  could ship them. The default is now under `data/`, which is already both.

## 0.3.0 - 2026-08-24

### Added

- A minimum-requirements statistical-arbitrage platform layer
  (`trading_platform_poc.minreq`) implements requirements MR-01 to MR-14 of
  *Minimal Requirements: MFT Stat Arb Backtesting and Paper Trading* v2.1:
  multi-venue capture with raw retention, a canonical event schema with
  versioning rules, an effective-dated security master and fee book,
  restartable ingestion with manifests and checksums, deterministic replay,
  lookahead-free features, a common OMS/EMS with a parent/child state
  machine, an 18-limit risk matrix with a kill switch, an append-only ledger
  with reconciliation, monitoring and TCA, run manifests, and a performance
  benchmark. Acceptance tests AT-01 to AT-14 run as executable checks that
  write their own evidence bundle, and fourteen `mr-*` CLI commands expose
  the whole stack. `MINIMUM_REQUIREMENTS.md` documents the Section 10
  deliverables and `MR_DEMO_GUIDE.md` scripts a fifteen-minute walkthrough.
- The acceptance report now records a defect log, accepted limitations with
  an owner and remediation date, and a release sign-off table, which
  Section 10 requires of a signed report. An empty defect log still renders,
  so a green run cannot be mistaken for a run that never produced the
  section.
- A container image builds the wheel in one stage and ships only the runtime
  in the next, so a client can reproduce the full acceptance bundle with a
  single `docker run` and no local Python, CMake or compiler. The `image`
  workflow builds the image, runs the acceptance suite inside it, checks the
  evidence bundle, and publishes to GHCR on a tag.

### Fixed

- `build_identity` reported `package_version` as `0.0.0+local` in every run
  manifest and acceptance report. It looked up the distribution
  `trading-platform-poc`, but the distribution is `hft-platform-kit`, so the
  lookup always missed and fell through to the placeholder. Evidence that
  cannot name its own version is not reproducible, so the lookup now uses
  the real distribution name.
- `build_identity` reported `git_revision` as `unavailable` outside a git
  checkout, which is exactly the case for a wheel or container install. The
  revision can now be stamped through `HFT_PLATFORM_KIT_GIT_REVISION`, which
  the image sets at build time.
- The container image could not be built at all. It copied neither
  `CMakeLists.txt` nor `cpp/`, both of which scikit-build-core needs to
  configure the `hft_platform_kit._ems` extension, and its base image had no
  compiler or CMake. The build stage now installs the toolchain and copies
  the native sources, and the runtime stage imports the extension so a
  broken build fails at build time rather than in front of a client.
- The acceptance table in `MINIMUM_REQUIREMENTS.md` transposed AT-12 and
  AT-13 against both the requirements document and the implementation,
  described AT-02 by AT-03's fault detection, and described AT-08 as a
  risk-reservation test. The same section claimed no accepted limitations
  while Section 17 stated one.

### Added

- A typed HftBacktest market-making SDK ports the complete reusable behavior
  from `market_making_backtest.ipynb`: deterministic synthetic L2/trade data,
  initial and last snapshots, configurable assets with constant or
  feed-derived latency, hash-map and ROI-vector depth, order lifecycle
  scenarios, depth imbalance, trade capture, rolling VWAP, and the corrected
  inventory-skewed GTX grid strategy. `Recorder` output and
  `LinearAssetRecord` summaries are returned as data rather than trapped in
  notebook state. The `hft-synthetic`, `hft-latency`, `hft-order-demo`, and
  `hft-grid` commands expose the same path from the CLI.
- Official HftBacktest tutorial parity now includes Binance Futures raw gzip
  conversion with timestamp correction/reordering, Tardis trade-before-depth
  conversion and snapshot modes, multi-day snapshot chaining and latency
  files, `log_prob_queue_model2`, price-denominated and normalized-inventory
  grid parameters, persisted recorder NPZs, reusable plot-capable statistics,
  notional-derived multi-asset runs, and combined portfolio equity. The
  `hft-convert-binance`, `hft-convert-tardis`, and `hft-snapshot` commands
  expose the data-preparation workflow, and
  `HFTBACKTEST_TUTORIAL_TEST_PLAN.md` provides its executable acceptance
  checklist. Synthetic generation now exposes volatility and trade
  probability for fill-producing strategy tests; undefined zero-denominator
  statistics are emitted as JSON `null` without noisy runtime warnings.
- `book` renders the maintained level-2 order book in the terminal, for
  five minutes by default. Rendering is a pure function of book state, so
  the layout is tested with no network and no clock. The first
  implementation fetched the REST snapshot before connecting the
  websocket, which is the wrong way round: every depth update arriving
  during the REST call is lost, the book opens with a gap it cannot
  close, and in practice not one delta was ever accepted. It now
  connects first, snapshots on the first delta, discards updates the
  snapshot already covers, and resynchronizes on a sequence gap. Two
  display defects went with it -- a half-tick mid printed as a whole
  tick, inventing half a tick of edge, and a spread of `0.0016 bps`
  printed as `0.00` -- and a `continue` path that skipped the duration
  check, so a five-minute display could overrun. Verified against
  Binance's independent `bookTicker` at the same instant.
- `binance-testnet modify` amends a resting order through a single
  `cancelReplace` call in `STOP_ON_FAILURE` mode. The mode is the point:
  if the cancel half fails and the replacement is placed regardless, two
  live orders exist where one was intended, which is the duplicate
  exposure the rest of the system is built to prevent. Ambiguous
  outcomes are resolved by querying the new client order ID and then the
  original, and a replacement that cannot be resolved either way is left
  `RECONCILIATION_REQUIRED` rather than assumed. A replace does not count
  against the open-order limit, because it withdraws one order as it adds
  one. There is no separate delete verb; removing a resting order is
  `cancel`.
- `binance-testnet multi-leg` dispatches two or more legs concurrently
  behind atomic admission: every leg is validated, risk-checked and
  balance-checked before any leg is transmitted, and the balance check
  sums the requirement across all legs rather than testing each in
  isolation. One unaffordable leg means nothing reaches the venue.
  Atomicity covers admission only -- once the orders are live either can
  still fill alone, which is what the existing hedge and unwind logic is
  for.
- `CLIENT_TESTNET_TEST_PLAN.md`, a hands-on plan the client runs against
  Binance Spot Testnet with their own credentials: submit, read back from
  the venue, atomic `modify`, cancel, and both multi-leg paths, with
  expected output, a pass/fail checklist, and a troubleshooting section
  covering the `-2015` key-not-recognised case. Every command and every
  quoted output in it was executed against the live Testnet, including
  two claims that turned out to be wrong when tested: an oversized order
  trips the balance check before the notional limit, so the step now uses
  a size that is affordable but over the limit; and a replacement refused
  by the price collar leaves the original order resting untouched, which
  is now asserted explicitly rather than assumed. Prices and order ids
  are shell variables rather than words to substitute by hand -- the
  first reader pasted a leg literally and got `has a non-numeric field`,
  so step 0.4 now defines a `px` helper that fills the prices in and
  every later command is copy-pasteable as printed.

### Fixed

- `config.testnet.example.yaml` carried no `risk` block, so it inherited
  the production default `max_net_position` of `0.05`. The risk position
  is derived from the *total* balance of the base asset, and Binance
  Testnet grants about 1 BTC, so the shipped configuration rejected every
  order on a funded Testnet account -- buy or sell, any size -- before it
  reached the venue. Anyone following the Testnet step of the demo
  runbook hit `risk rejected order: max_net_position` and had no way to
  proceed. The file now carries limits sized for that grant, with the
  reasoning written down next to them. `price_collar_bps` is deliberately
  left at the production default of 100: it is fat-finger protection and
  widening it to make a demo pass would defeat its purpose. Verified by
  running the whole lifecycle against Testnet using this file alone.

- The priority ceiling could be breached under concurrency. Admission
  reads the ceiling in `_binds` and takes the capacity later in
  `_reserve_capacity`; each step was locked, the gap between them was
  not. Two research messages that both read "still below the ceiling"
  both reserved, holding six against a share of five. The hard limit
  never catches this -- being over a 50% ceiling is nowhere near being
  over the budget -- so what the overshoot costs is the headroom held
  for higher-priority traffic, ultimately the capacity an emergency
  hedge is promised, taken by the lowest-priority class on the system.
  `MessageBudget.try_reserve_within()` now folds the ceiling test and
  the reservation into one acquisition, on the drain path as well as
  admission. The same two admissions run one after the other always
  admitted one and rejected the other; the concurrent outcome now
  matches.
- The same ceiling race was live in a third place, and a name-based
  search could not find it. `ReservationLedger.reserve()` tests every
  ceiling and then reserves, but it never writes `above_ceiling` --
  it goes through `self._check_ceiling(budget, ...)`, so grepping for
  the name found two of the three sites and the first fix shipped
  looking complete. The ledger's own lock is no protection: the party
  racing a reservation is `ExecutionControlPlane.evaluate()`, which
  never takes it, and both spend the *same* budget objects, because
  the plane hands its own budgets to the ledger it builds. A
  reservation is also the one call that can take a whole budget at
  once, so a ceiling a reservation can straddle is a ceiling any
  caller can evade by reserving first. Run one after the other, a
  reservation and an admission for three messages each admitted one
  and refused the other; interleaved, both took, leaving six against
  a share of five. `ReservationLedger._take()` now folds the pair for
  all three of the ledger's sites.

### Testing

- Mutual exclusion is now asserted rather than assumed. A second
  mechanical sweep -- remove one `with self._lock`, run the concurrency
  tests, repeat for all 42 acquisitions -- found that only two were held
  by any test. Deleting either lock behind the atomic reservation left
  all 727 tests green: one opportunity could be reserved twice, taking
  venue capacity twice and counting its notional against the firm limit
  twice, and two threads could take the same last message slot and send
  two messages to a venue that allowed one. Nine more locks now have
  deterministic tests covering lost spends, lost releases that shrink a
  venue allowance permanently, two legs spending the same reserved
  message, two unwind legs spending one reserved hedge message and
  taking the extra out of another opportunity's hedge reservation, and
  stranded notional that would measure every later opportunity against
  a limit short by that amount.
- The lock sweep now replaces `with self._lock:` with a no-op context
  manager rather than `if True:`. Acquiring a lock is itself a point
  where the interpreter may switch threads, so deleting the `with`
  deletes a preemption opportunity and narrows the very window the
  sweep is trying to open. Keeping both calls makes the mutant strictly
  more sensitive.
- Two race tests were written, found to be blind, and rewritten. Both
  failed the same way: they stopped a thread on the *first* read of a
  value, and the first read was not the one the code acts on. The
  reservation test parked in `_preflight`'s advisory ceiling check, so
  the reservation then went on to test and take without interruption
  and the test passed with the fix reverted. Stopping on the second
  call lands in the seam that matters, and the call count is the same
  in the fixed and the broken source, so the test is not written to
  the fix. Removing the park makes the mutant survive again, which is
  how the park is known to be load-bearing rather than decorative.
- An assertion that read `both_inside is False` was doing no work. It
  survived a `_try_release` that always skipped and a rendezvous whose
  `wait()` returned immediately -- both mutants leave the flag false
  because nothing ever arrived. It now uses `assert_exclusive`, which
  also counts arrivals, and all three mutants are caught.
- The two existing stress tests sample interleavings and were blind to
  this: they start eight threads and hope. That works on a window wide
  enough to lose 54 trials in 60, but deleting a lock leaves a window a
  few bytecodes wide that seventy-five trials never hit. The new tests
  park two threads inside the critical section at the seam between
  reading a value and acting on it, so the race is forced.
- Every venue/instrument/strategy name fold in `execution_controls.py`
  is now held by a test. A mechanical sweep -- remove one
  `_normalise_venue` call, run the suite, repeat for all 29 call sites
  -- found 9 that no test reached. The worst let `consume_hedge` skip
  its venue charge entirely, sending hedge messages the venue's own
  limit never knew it had given out; others refused a legitimate spend,
  answered `room_for` with `None` so the leg was charged twice, and let
  the hedge escape the instrument limit every other leg obeys.

### Added

- Every baselined exception in the shape test now names the test that
  holds it, with a guard that the named test still exists. An entry is
  keyed on names only, so the key does not move when the body it
  describes changes: the failure mode is not a bad exception being
  added, it is a good one's justification quietly ceasing to be true
  while the entry looks identical. The rot check is the smaller half of
  the value -- the larger half is that an exception with no nameable
  holder becomes visible when it is written, which is the only moment
  anyone is thinking about it, and "what holds this?" is an easier
  question to answer honestly than "is this benign?".
- `ADVISORY_READS` now states its membership test: can another thread
  make this return value wrong before the caller acts on it. That is
  narrower than "is this a read", and it is why `never_fits` is
  excluded -- it reads only immutable state, so there is no window for
  it to be advisory in. The earlier reasoning, that its call sites are
  branch-exclusive, was a property of the callers and would have
  evaporated in a refactor nobody thought to check.

- The refusal half of the advisory invariant is now held by a test.
  `_draw_down` reads that a reservation covers a message and spends it
  two frames later under a lock the read never held, so the read can go
  stale; the only thing between that and a control plane that raises
  instead of deciding is one `except` clause. Deleting it failed
  nothing -- every other test spends a reservation that is still there
  -- so the suite passed with the handler gone. A reservation released
  between the check and the spend now deterministically produces an
  ordinary ADMIT rather than an exception escaping `evaluate`.
- The shape enumerator recorded only the *first* call following an
  advisory read, so a take placed after a benign call on the same
  object was invisible: a `try_reserve` inserted into `_preflight`
  after its `available()` read produced no pair at all -- the original
  defect, in the original shape, undetected. It now records every
  following call, which costs nothing on this module and makes the
  positive control assert two tuples instead of one.
- A fifth guard closes the declaration under delegation. `_delegation`
  only resolves wrappers already named in `ADVISORY_READS`, so a new
  undeclared wrapper would silently reintroduce exactly the blindness
  that hid the third ceiling site. Any function forwarding an advisory
  read to one of its own arguments must now be declared.

- `ADVISORY_READS` declares which methods return an answer that may be
  stale by the time it is used -- `above_ceiling`, `room_for` and
  `_check_ceiling` -- together with the invariant they carry: every
  advisory read must be paired either with an authoritative call in the
  same acquisition, or with a handler for that call's refusal. It is
  declared rather than inferred because inferring it is what went
  wrong. A classifier that decided from the syntax which methods were
  pure and which mutated had to be corrected three times, and each
  correction came from a miss rather than from design.
- `tests/test_execution_controls_shape.py` enumerates the shape behind
  the ceiling defect rather than the names involved in it. It resolves
  the delegation that hid the third site -- `self._check_ceiling(budget,
  ...)` is a read on `budget`, not on `self` -- and holds the result
  against a frozen baseline keyed on names, never line numbers. It
  fails in both directions: a new pair is a failure rather than a
  report, and a baseline entry disappearing is a failure too, because
  an allowlist that has gone stale without anyone noticing protects
  nothing. It carries its own positive control, re-splitting the fix in
  a copy of the source and asserting the enumerator finds the damage,
  so it cannot degrade into a test that passes by finding nothing.

- `scripts/mutation_sweep.py`, which runs both sweeps on demand. Each
  derives its site list by reading the module at run time, so a site
  added later is swept automatically and one removed cannot leave a
  stale entry behind. It is a mutation harness, not a test: it rewrites
  the module on disk and must not run in CI beside the suite.
- A `concurrency` pytest marker for tests that start real threads, so
  the lock sweep selects its targets by marker rather than by a list
  that would go out of date.

- `FeedMonitor.clamped(venue)` and a `clamped_ticks` field per venue in
  `to_dict()`. Clamping a tick dated ahead of the local clock is safe
  but silent -- the venue then renders identically to a healthy one --
  so the correction is counted where an operator can watch it trend. It
  changes no decision.

### Fixed

- `FeedMonitor.record_tick` never dates a tick ahead of the local
  clock. A venue clock running fast made `age_ns` negative, and a
  negative age never exceeds the stale threshold -- so the stale-feed
  control switched off for that venue and admitted new quoting into a
  dead feed. `is_stale` already treats an unknown venue as stale rather
  than fresh; this is the same choice for the one input that failed in
  the unsafe direction.
- A refusal is counted against the tier that refused it. `_never_fits`
  names the most senior tier that can never supply the request, but the
  counter was incremented on the tier that merely bound first, so an
  operator drilling into per-tier rejection counts to decide which
  limit to raise was pointed at the wrong limit. The decision and the
  aggregate were already correct.
- `_reserve_capacity` translates a malformed `ReservationRequest`
  instead of leaking it. The request is constructed inside the `try`,
  so a `ValueError` from its validation escaped past the
  `capacity_refused` audit event, the structured log and the
  `OpportunityCapacityError` translation.

- `ReservationLedger.consume` refuses a non-positive spend, the guard
  `room_for` and `consume_hedge` already carried. `consume_reserved`
  does `_reserved -= n; _used += n`, so a negative both inflated the
  hold and drove `used` below zero; after release the venue budget
  reported more capacity than the exchange's hard limit, which is the
  breach a venue answers by dropping the session.
- `ReservationRequest` validates its own inputs, mirroring
  `MessageRequest`. A negative leg count made `try_reserve` succeed
  holding nothing while `release` *grew* the reserved figure, so
  twenty-five reserve/release cycles left the firm tier holding its
  entire limit against zero open reservations, surviving every window
  roll. A negative hedge count made the firm hold smaller than the
  venue holds it bounds, and a negative notional walked straight
  through the firm risk limit. `opportunity.py` checked these already,
  but the ledger is public API and that layer is bypassable.
  `position_quantity` stays signed: its sign carries direction, and the
  check, the charge and the release all take `abs` of it.
- `_preflight` asserts the control hierarchy's invariant -- the firm
  tier's demand is never below a tier it bounds -- rather than relying
  on one map feeding both figures.

### Changed

- Removed `ReservationRequest.total_messages`, which computed the total
  through the *unfolded* venue map. It had no callers, and it was the
  seam by which `reserve` and `release` came to disagree.

- `reserve` refuses a request whose `venue_messages` or
  `venue_instruments` contain two spellings of one venue, instead of
  silently dropping one. The two maps were folded without the duplicate
  check the config maps already had, and `reserve` then took the firm
  tier from the caller's raw total while `release` credited it back from
  the folded one. Every dropped duplicate leaked firm capacity
  permanently -- on the success path, with no error anywhere. `_roll`
  clears `used` but never `reserved`, so the leak survived every window
  roll: twenty-three cycles jammed a 100-message firm budget at 92% held
  by nothing, and firm capacity is the top of the precedence hierarchy.
- Both caller-supplied maps are folded up front, before any tier is
  taken. Detecting the collision at the point of use meant the venue,
  firm and instrument tiers were already reserved, so refusing the
  request leaked exactly what it had taken.
- A dropped `venue_instruments` entry left one leg of a spread on an
  unreserved instrument tier -- which is an *unenforced* tier, because a
  reserved message skips the capacity walk.
- `OpportunityCoordinator` folds venue names with the same rule the
  ledger uses. It accumulated under `.lower()` against the ledger's
  `strip().casefold()`, so two legs on one venue whose strings differed
  by whitespace stayed two keys in the caller and collapsed to one in
  the ledger, reserving one leg's messages for two.
- `dashboard()["affected_strategies"]` reports the folded strategy id.
  One strategy on one budget appeared under three names, inflating the
  apparent blast radius of the incident the dashboard exists to explain
  and breaking any responder that intersects it with the configured
  budget keys.

- A reservation is now drawn down whatever case the sender spells the
  strategy in. `scoped_charges` was built from the normalised id while
  `Reservation.strategy_id` kept the raw one, so a single reservation
  carried two disagreeing identities: it charged `alpha` but gated
  drawdown on `Alpha`. The gate missed and every message was charged a
  second time -- three messages drew six off every tier -- and under
  `require_reservation` the opportunity held the capacity yet could not
  send its first leg.
- `strategy_pm` is folded through a duplicate-detecting normalisation
  like the budget maps. It was the one map where last-wins changed an
  enforcement outcome rather than which equivalent budget object
  survived: a strategy the operator routed to a one-message manager was
  silently rerouted to a hundred-message one.

- A message larger than a lower tier's whole allowance is now rejected
  rather than queued. `_evaluate` returns on the first binding control,
  so the never-fits test consulted only that control; a request that fit
  the venue but could never fit the instrument was queued, was undrainable
  at every point in its life, and was dropped silently at expiry.
- `MessageBudget.never_fits` (renamed from `exceeds_ceiling`) answers
  "larger than the whole window limit" before the `ceiling_pct >= 100`
  short-circuit. `MARKET_MAKING` has a 100% ceiling, so the firm's primary
  trading class previously had no never-fits protection at all.
- PM and strategy tiers are now case-normalised like venues, at both
  construction and lookup, in `ExecutionControlPlane` and
  `ReservationLedger`. A missed lookup is indistinguishable from "no tier
  configured", which `_binds` treats as not applying — so varying the case
  of a strategy id silently removed its own limit rather than delaying it.
- `expected_recovery_ms` is computed from binding tiers only. Mixing in
  ceiling-pressured tiers let an idle daily budget at 65% report a
  24-hour recovery for a venue that refills in one second. Ceiling
  pressure now reports separately in `dashboard()["holding_back"]`, per
  tier, naming the classes each one holds back.
- `ReservationLedger.reserve` pre-flights every tier in precedence order
  before taking anything, so a refusal names the *most senior* tier that
  objects rather than whichever happened to be walked first.

### Added

- Message-level feed integrity checks: crossed/locked markets, non-monotonic
  bid/ask ordering, duplicate price levels, excessive internal processing
  latency, and per-symbol feed heartbeat silence. Ordering and the crossed
  test apply to snapshots only, because deltas legitimately carry removals.
- `DataQualityConfig.max_processing_latency_ms`, `max_heartbeat_interval_ms`,
  `reject_crossed_books`, and `reject_processing_latency`.
- `DataQualityValidator.record_heartbeat` and `check_heartbeats`, with
  heartbeat state carried through `capture_state`/`restore_state` so replay
  stays deterministic.
- Persistent two-venue concurrent-leg opportunity state machine
  (`opportunity.py`) covering the full lifecycle from creation through risk
  reservation, concurrent dispatch, acknowledgement, partial and final fills,
  reject and timeout handling, hedging, deterministic unwind, reconciliation,
  and closure.
- SQLite WAL opportunity ledger (`opportunity_store.py`) with an append-only,
  monotonically sequenced event log and database-enforced uniqueness on
  `(venue, client_order_id)` and `(venue, trade_id)`.
- Deterministic identity: opportunity IDs, per-leg client order IDs, and
  idempotency keys are pure functions of the signal, so retries and replays
  cannot duplicate an order and restart recovery needs no extra bookkeeping.
- Per-leg ack/first-fill/complete-fill timeouts, reject classification with
  per-reason actions, transport retry directives, residual exposure policy,
  and deterministic unwind venue and price selection.
- Four-way reconciliation across exchange, local, risk, and ledger positions
  with opportunity-level PnL, fees, slippage, and hedge cost.
- `SimulatedVenue`, an offline scriptable `ExchangeAdapter` implementation, and
  the `opportunity-demo` CLI command with `filled`, `partial`, and `reject`
  scenarios. Neither requires network access, credentials, or native
  extensions, so both run on locked-down Windows hosts.
- `TWO_VENUE_EXECUTION.md` design document.
- Execution control plane (`execution_controls.py`) applying venue rate
  limits, the firm-wide throttle, market-data staleness, and the kill switch
  to every outbound message through a published 10x4
  control-to-order-state matrix (`CONTROL_MATRIX`).
- Deterministic control precedence: evaluation walks the firm risk ->
  kill switch -> firm message limit -> PM -> strategy -> venue ->
  instrument -> order hierarchy and stops at the first binding control, so
  two controls can never disagree about an outcome.
- Risk-reducing traffic — cancels, hedges, and forced liquidation — is
  exempt from every throttle, and is still counted so overage stays visible.
- `ReservationLedger`, an all-or-nothing reservation across per-venue and
  firm message capacity, notional, margin, position limits, and hedge
  capacity. A partial failure releases everything already taken, so one leg
  can never execute while another has nowhere to be sent.
- Distinct synchronous rejection, queue, and delay outcomes; a queued
  message whose deadline falls before capacity returns is rejected up front
  rather than left to expire silently.
- `PriorityMessageQueue` releasing held traffic strictly by priority
  (forced liquidation, emergency hedge, risk reducing, market making, quote
  refresh, research) then arrival order.
- Pre-breach visibility bands (green/yellow/orange/red) and
  `ExecutionControlPlane.dashboard()` reporting the binding control, driving
  resource, queued and dropped counts, affected strategies, remaining
  capacity, and expected recovery.
- `controls-demo` CLI command with `venue-rate-limit`, `global-throttle`,
  `stale-feed`, `kill-switch`, `atomic-reservation`, and `control-matrix`
  scenarios, all offline and deterministic.
- `EXECUTION_CONTROLS.md` design document.
- All four control decision tables are exported from the SDK —
  `CONTROL_MATRIX`, `CONTROL_PRECEDENCE`, `EXEMPT_PRIORITIES`, and
  `RISK_REDUCING_STAGES` — so a reviewer can reproduce any transition from
  data instead of by reading the evaluator.
- Optional PM, strategy, instrument, and per-order capacity tiers, so the
  published control hierarchy is enforced at every level rather than only
  at the firm and venue ones. Each is opt-in; an unconfigured tier does not
  exist and never binds. The per-order ceiling rejects synchronously,
  because an order no window could ever carry must not be queued.
- Reserved capacity is now *drawn down* at transmission instead of charged
  a second time, and `require_reservation` makes "reserve before you send"
  enforceable at the plane.
- `ReservationRequest.venue_instruments`, per-leg instrument overrides. A
  spread is two instruments, so each instrument tier is reserved and
  charged only for the legs that trade it.
- Per-class utilisation ceilings (`PRIORITY_CEILING_PCT`, overridable via
  `priority_ceilings=`): priority now *allocates* capacity rather than only
  reordering a queue. Research stops at 60% and quote refresh at 80%, so
  headroom is held for higher classes instead of going first-come.
  Risk-reducing traffic has no ceiling.

### Changed

- Structured logs now go to **stderr** instead of stdout, so a command's JSON
  result on stdout can be piped into `jq` or another process without being
  corrupted by interleaved log lines.
- A `KillSwitch` built with no administrators is now refused: one that can
  never be cleared is a permanent outage rather than a safety device. The
  default administrator is `risk-admin`.
- Full suite grew from 326 to 645 passing tests (1 credential-gated skip).

### Fixed

- A request larger than its scheduling class's entire share is rejected
  rather than queued. It could never be released — an empty budget
  refuses it too — so queueing it parked a message no drain could take,
  held its scope against every later scan, and lost it silently at
  expiry. A request that merely has to wait is still queued, and a single
  message against a budget too small to hold one still waits rather than
  being rejected as impossible.
- A drain no longer stalls every strategy because one of them is short.
  A `STOP` verdict now carries the key of the budget that refused: the
  firm tier ends the scan, since every queued message draws on it, while a
  PM or strategy tier blocks only its own scope and the scan continues.
  Recorded keys are consulted for the rest of the scan, so a smaller
  message in a blocked scope cannot take the slot the message ahead of it
  was waiting for.
- Priority ceilings are enforced on the reservation path. A reserved
  message draws down instead of walking the capacity controls, so a
  ceiling applied only at admission was one any caller could evade by
  reserving first — and a reservation is the single call that can take a
  whole budget at once. `ReservationRequest.priority` now selects the
  ceiling, and `priority_ceilings=` is forwarded to the ledger.
- A ceiling now also refuses a request that would *cross* it, not only one
  that starts past it. The previous test allowed a single request to
  overshoot by `messages - 1`, unbounded without `max_messages_per_order`.
  A single message against an untouched budget is still admitted, which is
  what keeps a budget too small to share usable.
- `dashboard()["expected_recovery_ms"]` is built only from constraining
  tiers. It previously reported the longest configured window whether or
  not anything was queued, so an idle long-window budget read as an outage
  on a completely quiet system.
- A reservation is no longer honoured as a bearer token. It is matched
  against the requesting strategy and instrument, so a message can no
  longer skip its own PM, strategy, and instrument tiers and charge
  another opportunity's budgets instead.
- The reserved hedge allowance is offered only to risk-reducing traffic.
  It is charged to the venue unconditionally so nothing can block a hedge,
  so handing it to an ordinary order let one opportunity exceed a venue's
  hard message limit.
- Draining now stops rather than scanning past a PM or strategy budget that
  cannot cover a message. Both are shared by every queued message of that
  scope, so scanning on handed their last slot to whichever message was
  small enough — a priority inversion.
- `ExecutionControlPlane.dashboard()` and `snapshots()` include the PM,
  strategy, and instrument tiers. A tier that was binding, queueing, and
  rejecting was previously invisible, and expected recovery reported a
  venue's window when a far longer tier window was the real constraint.
- Capacity tiers are labelled with the level they enforce, so a binding
  strategy limit is no longer reported to operators as a venue limit.
- A queued message carrying no messages is refused at construction. The
  drain callback runs against every message it scans, so one malformed
  entry raised on every drain and was never removed, taking down everything
  queued behind it.

- A resumed leg that had already been sent to a venue was resubmitted
  without querying first. Binance frees a client order ID once the order
  stops being live, so the deterministic ID alone did not prevent a second
  real order. Any leg with a recorded submission is now resynced first.
- Unwind leg indexes restarted from a fixed base after a restart and
  collided with existing unwind legs, corrupting the residual calculation
  and doubling naked exposure. Indexes now continue from the highest one
  already persisted.
- A leg's venue, symbol, side, and target quantity are now immutable once
  written. The upsert previously ignored changes to them, which could book
  one venue's trade under another's identity. Repricing a limit order is
  still permitted and now persists correctly.
- A failed or cancelled `execute()` leaked its risk reservation, wedging
  every later opportunity against the aggregate limit.
- A failing leg dispatch propagated immediately while its sibling task kept
  running unsupervised and could place a live order after the caller had
  already been told the opportunity failed.
- Fees were summed across currencies and sign conventions, then subtracted
  from a quote-denominated PnL. They are now aggregated per currency.
- `reference_price` was never persisted, so slippage computed after a
  restart was measured against the limit price and was meaningless.
- The stored `signal_hash` was written but never read, so two different leg
  sets colliding onto one opportunity ID went undetected.
- `risk_positions` was recomputed from the same leg table as
  `local_positions`, so the advertised four-way reconciliation could never
  find a disagreement between them.
- `python -m trading_platform_poc.cli` and `python -m hft_platform_kit.cli`
  exited 0 without doing anything because neither module had a
  `if __name__ == "__main__"` guard. Both now run the CLI.

## 0.2.0 - 2026-07-31

### Added

- Stateful data-quality validation for timestamp ordering, future skew,
  delayed data, timestamp regression, trade timestamp consistency, price and
  quantity bounds, duplicate/out-of-order trades, abnormal price moves,
  minimum depth, excessive spread, and deterministic local checksums.
- Structured anomaly records, a thread-safe local alert queue, Prometheus counters,
  runtime status, `anomalies.jsonl`, and summary statistics.
- Recording quarantine that preserves rejected native messages while keeping
  unsafe canonical events out of JSONL and Parquet analytics datasets.
- Automated recovery reporting for sequence gaps and invalid books.
- Guarded Binance Spot Testnet gateway with signed market, limit, and
  `LIMIT_MAKER` submissions, cancel, query, account, open-order, recent-trade,
  and reconciliation operations.
- Binance exchange-filter normalization, server-time synchronization,
  rate-limit visibility, deterministic client IDs, and ambiguous-timeout
  recovery.
- Native C++20/pybind11 EMS with single-loop ordering, atomic multi-leg risk
  reservation, TWAP/VWAP scheduling, fee/latency-aware SOR, and explicit
  recovery states.
- Guarded Binance Testnet, OKX Demo, and Deribit Test execution adapters behind
  a normalized coordinator and reconciliation contract.
- scikit-build-core native wheel/sdist packaging and platform-tagged release
  validation.

### Changed

- Dataset manifests now include quarantine counts and checksums.
- Unsafe market data is blocked before reaching strategy evaluation.

## 0.1.0 - 2026-07-30

### Added

- Typed `hft_platform_kit` public SDK.
- Injectable strategy, risk, OMS, execution venue, order book, and portfolio.
- Programmatic `create_engine` and `replay_dataset` helpers.
- Binance public market-data source and deterministic L2 order book.
- JSONL and Parquet recording with checksummed manifests.
- Deterministic replay with real-time and accelerated pacing.
- Tardis historical import and HftBacktest export.
- Pre-trade risk, OMS, paper execution, PnL, status, metrics, and kill switch.
- `hft-platform-kit` CLI with backward-compatible `trading-platform-poc` alias.
- MIT license and PEP 561 `py.typed` marker.
