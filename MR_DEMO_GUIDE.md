# Demo guide: minimum-requirements stat-arb platform (`mr-*`)

A 15-minute walkthrough that takes an audience from raw venue packets to a
reconciled, reproducible, signed-off backtest.

Everything runs **offline**. No credentials, no network, no external services.
Every number in this guide was produced by the commands as written, so you can
rehearse against it and know when something has drifted.

**The story you are telling:** *"You cannot trust a backtest you cannot
reproduce, and you cannot reproduce one whose data you never validated."*
Each act tightens that argument.

For the broader platform (live capture, HftBacktest export, EMS, testnet
execution) see [DEMO_GUIDE.md](DEMO_GUIDE.md). For the design and requirement
map behind this demo see [MINIMUM_REQUIREMENTS.md](MINIMUM_REQUIREMENTS.md).

**Contents**

- [Before you start](#before-you-start)
- [Act I — What can this thing even see?](#act-i--what-can-this-thing-even-see-2-min)
- [Act II — Prove the data is good](#act-ii--prove-the-data-is-good-4-min)
- [Act III — Run the strategy](#act-iii--run-the-strategy-4-min)
- [Act IV — Prove you can trust it](#act-iv--prove-you-can-trust-it-4-min)
- [Act V — Sign-off](#act-v--sign-off-2-min)
- [Appendix A: If you only have 5 minutes](#appendix-a--if-you-only-have-5-minutes)
- [Appendix B: Two commands that always impress](#appendix-b--two-commands-that-always-impress)
- [Appendix C: Questions you will get](#appendix-c--questions-you-will-get)
- [Appendix D: Troubleshooting](#appendix-d--troubleshooting)

---

## Before you start

```powershell
cd C:\Users\ankusin\hft-platform-kit
# Only needed behind a corporate proxy that blocks PyPI. Set it to your own
# index; leave it unset on an open network.
if ($env:PYPI_PROXY_INDEX) { $env:UV_DEFAULT_INDEX = $env:PYPI_PROXY_INDEX }
$uv  = "$env:USERPROFILE\.local\bin\uv.exe"
$D   = "$env:TEMP\mrdemo"
Remove-Item $D -Recurse -Force -ErrorAction SilentlyContinue

function mr  { & $uv run --frozen trading-platform-poc --config config.yaml @args }
function mrj { (mr @args | Out-String) | ConvertFrom-Json }
```

`mr` runs a command and shows raw JSON. `mrj` parses it so you can pull out
single fields on stage instead of scrolling through a wall of output.

Smoke-test before the audience arrives — this should print in under a second:

```powershell
mrj mr-schema | Select-Object document_version, layer_version
```

> **Rehearsal tip.** Run Act II once before the demo. The first `uv run` in a
> session pays a one-off startup cost; after that every command is fast.

---

## Act I — What can this thing even see? (2 min)

**Point:** before any strategy exists, the platform has to have an opinion
about venues, instruments and money.

### 1. The venue capability matrix

```powershell
(mrj mr-schema).venue_capability_matrix |
  Select-Object venue, order_types, time_in_force, depth_levels, max_orders_per_second
```

**Say this:** three venues, and they were not chosen for variety of logo. Each
detects a dropped message differently — binance uses `U`/`u` first-and-final
ids, okx uses `seqId`/`prevSeqId` back-links plus a checksum, bybit uses a
monotonic counter with an explicit snapshot/delta flag. A normalizer that only
ever saw one venue would quietly encode one venue's assumptions as universal
truth.

### 2. The security master is effective-dated

```powershell
(mrj mr-instruments --as-of-ns 1800000000000000000).instruments |
  Select-Object instrument_id, tick_size, quantity_step, minimum_notional, status
```

**Say this:** this is what an order gets sized against — tick size, quantity
step, minimum notional, lifecycle status. Ask it for a different timestamp and
it answers with the rules that were in force *then*, not the ones in force now.

### 3. Fees are effective-dated too

```powershell
(mrj mr-fees --as-of-ns 1700000000000000000 --venue binance).resolved |
  Where-Object liquidity -eq 'taker' | Select-Object instrument_id, rate_bps, fee_version

(mrj mr-fees --as-of-ns 1800000000000000000 --venue binance).resolved |
  Where-Object liquidity -eq 'taker' | Select-Object instrument_id, rate_bps, fee_version
```

Verified output:

```
BTC-USDT.BINANCE.SPOT   1.0   1      <- as of 2023
BTC-USDT.BINANCE.SPOT   0.9   2      <- as of 2027
```

**The moment:** the same instrument, the same query, two different answers
because you asked about two different points in time. The fee book contains a real mid-history
version bump. A backtest that prices last year's trades at today's fee rate is
lying to you, and this makes that class of bug impossible rather than unlikely.

---

## Act II — Prove the data is good (4 min)

**Point:** a dataset is not an input until something has tried to break it.

### 4. Capture a clean dataset

```powershell
mr mr-generate --output "$D\clean" --steps 400 --seed 11
```

Expected: `"approved": true`, `"quality_failures": []`, and a `checksum`.

**Say this:** raw venue payloads are kept byte-for-byte, then normalized. The
checksum covers content only — it deliberately excludes the capture timestamp
and the duplicate counter, so a dataset rebuilt after a crash hashes
*identically* to one captured in a single clean pass.

### 5. Verify it independently

```powershell
mrj mr-verify --dataset "$D\clean" | Select-Object verified, checksum
```

`verified: True`. This re-hashes the files on disk rather than trusting the
manifest's own claim about itself.

### 6. Now break it on purpose — the best 60 seconds of the demo

```powershell
mr mr-generate --output "$D\faulty" --steps 300 --seed 5 `
   --sequence-gap-at 60 --stale-gap-at 120 --crossed-book-at 180 --duplicate-at 240
```

Verified output:

```json
{
  "approved": false,
  "quality_failures": ["sequence_gaps:1>0", "crossed_books:1>0"],
  "canonical_events": 692,
  "raw_records": 685
}
```

Then show what it caught:

```powershell
(mrj mr-quality --dataset "$D\faulty").quality.by_code
```

Verified output:

```
crossed_book    : 1
duplicate       : 1
sequence_gap    : 1
snapshot_resync : 2
stale           : 2
```

**The moment — land this one slowly.** Four faults injected, four faults
caught, and the dataset **refused approval, with the reasons stated in
machine-readable form**. The two extra `snapshot_resync` flags are the book
recovering afterwards, which matters: an injected fault is always followed by a
resync, because a book that can never recover produces a cascade of downstream
false positives instead of one honest flag.

**If someone asks "so what?":** a silently dropped sequence message doesn't
crash anything. It gives you a slightly wrong book, which gives you a slightly
wrong signal, which gives you a backtest that looks *better* than reality.
That's the failure mode that costs money, and it's the one this catches.

---

## Act III — Run the strategy (4 min)

### 7. The backtest

```powershell
$A = mrj mr-backtest --output "$D\runA" --dataset "$D\clean" --run-id demo
$A | Select-Object manifest_identity, state_hash, monitoring_status
$A.tca_totals | Select-Object plans, fills, gross_edge, fees, slippage_cost, net_pnl
$A.reconciliation.breaks.Count
```

Verified numbers:

| Metric | Value |
| --- | --- |
| plans | 19 |
| fills | 38 |
| gross_edge | 38.3935 |
| fees | 11.39775555 |
| slippage_cost | 22.7225… |
| **net_pnl** | **26.99574445** |
| reconciliation breaks | 0 |

**Say this, and don't rush it:** gross edge was 38.39. Fees and slippage took
34.12 of it. The honest number is 26.99. Most toy backtests show you the 38 and
call it profit. Fees come from the cost model, and slippage is *measured* by
walking real published depth level by level — not assumed as a flat constant.

### 8. Show what it declined to trade

```powershell
$A.router
```

Verified: `plans=19, missed_opportunities=20, open_plans=0, plan_states={closed:19}`.

**Say this:** it passed on 20 opportunities — more than it took. Each one is
recorded with the reason it was skipped. A system that only logs its wins
cannot be debugged.

### 9. The book is genuinely market-neutral

```powershell
(mrj mr-reconcile --ledger "$D\runA\ledger.sqlite" --run-id demo).derived.positions |
  Select-Object instrument_id, venue, quantity, realized_pnl
```

Verified: **+0.15 BTC on binance**, **−0.15 BTC on okx**. Net exposure zero —
this is stat arb, not directional punting with extra steps.

---

## Act IV — Prove you can trust it (4 min)

### 10. Determinism — the headline claim

```powershell
$B = mrj mr-backtest --output "$D\runB" --dataset "$D\clean" --run-id demo
$A.manifest_identity -eq $B.manifest_identity
$A.state_hash        -eq $B.state_hash
```

Both print `True`. Verified identity:
`20ab61147d3a9bb49a7142f6f504d09420345807e5e2f04ca5bb3568349014b3`

**Say this:** two separate runs, different wall-clock times, byte-identical
identity. The manifest hash deliberately strips volatile fields like
`started_ns` and `created_at`, so *when* you ran it does not change *what* you
ran. Every run records its seed, git revision, config, dataset checksum and
component versions — hand someone the manifest and they can reproduce your
number exactly.

### 11. The ledger

```powershell
$r = mrj mr-reconcile --ledger "$D\runA\ledger.sqlite" --run-id demo
$r | Select-Object events, chain_digest, state_hash
```

Verified: **362 events**, chain digest `19512043c3389667…`.

**Say this:** append-only. No update path, no delete path. Positions, orders,
balances and PnL are *derived* from the event sequence, never stored as mutable
truth. That is also the restart story — recovery is a replay, not a guess.

### 12. TCA in three formats

```powershell
Get-ChildItem "$D\runA" | Select-Object -ExpandProperty Name
Get-Content "$D\runA\tca.md" | Select-Object -First 14
```

`tca.json` for machines, `tca.csv` for a spreadsheet, `tca.md` to paste into a
review. Plus `run_manifest.json`, `route_log.json`, `benchmark.json` and
`ledger.sqlite`. Open `tca.md` on screen — it is already formatted.

---

## Act V — Sign-off (2 min)

### 13. The performance baseline

```powershell
$bench = mrj mr-benchmark --output "$D\bench" --steps 400
$bench | Select-Object passed, failures
```

Verified: `passed: True`, `failures: 0` — ingestion, normalization, replay and
simulation throughput, per-order overhead, feature overhead and peak memory,
each measured against a stated threshold.

### 14. The whole acceptance suite

```powershell
$acc = mrj mr-acceptance --output "$D\acceptance"
$acc | Select-Object total, passed
Get-Content "$D\acceptance\acceptance_report.md" | Select-Object -First 26
```

Verified: **14 of 14 PASS.** The Markdown report on screen is your closing
slide — every test with its result, its owner and its evidence file, plus the
build revision and host it ran on.

**Close with this:** that report is not a summary someone typed up afterwards.
It is generated by the run, it names an accountable owner per test, and every
row links to the evidence JSON that produced it.

---

## Appendix A — If you only have 5 minutes

Run steps **6** (break the data), **7** (net PnL after costs), **10**
(determinism) and **14** (14/14). That is the entire argument.

## Appendix B — Two commands that always impress

Individual acceptance tests run standalone, which is useful when someone
challenges a specific claim:

```powershell
# "Does risk really refuse a half-executed pair?"
mr mr-acceptance --output "$D\at08" --only AT-08

# "What happens on a partial fill you cannot hedge?"
mr mr-acceptance --output "$D\at09" --only AT-09
```

AT-09 is the strongest single artifact in the project: a partial fill, a hedge
rejected for no liquidity, an unwind on the other venue, and a plan that still
ends flat and closed.

## Appendix C — Questions you will get

**"Is this real market data?"**
No, and deliberately so. It is a deterministic generator, which is what lets
the acceptance suite run identically on any machine with no credentials. Live
public feeds are implemented in `venues.PublicFeed`; pointing the paper runner
at them is configuration, not new code.

**"Why is `monitoring_status` a warning on a clean run?"**
Because thresholds sit where they would be useful in production, not where they
would make the demo look tidy. Show the alert — it is usually
`missed_opportunities`, and it is telling the truth.

**"Could the strategy be overfitted?"**
The strategy is intentionally simple; the deliverable here is the *platform*.
The point is that when you bring a real strategy, the fees, slippage, risk,
reconciliation and reproducibility are already honest.

**"How do I know the tests are not just asserting True?"**
Open the evidence file the run wrote, for example
`$D\at09\at09\state_transition_report.json`. Each test writes the actual state
transitions, hashes and numbers it checked.

**"What is not covered?"**
Live-readiness is explicitly not claimed. Section 17 of
[MINIMUM_REQUIREMENTS.md](MINIMUM_REQUIREMENTS.md) states the limitation in
writing rather than leaving it implied.

## Appendix D — Troubleshooting

| Symptom | Fix |
| --- | --- |
| `uv` not found | Use the full path: `& "$env:USERPROFILE\.local\bin\uv.exe"` |
| Network / index errors | Ensure `$env:UV_DEFAULT_INDEX` is set as shown above |
| `ConvertFrom-Json` fails | The command errored — rerun with `mr` instead of `mrj` to see the message |
| First command feels slow | One-off session startup; rerun it |
| A path has spaces | Always quote: `--output "$D\run"` |

## Cleanup

```powershell
Remove-Item $env:TEMP\mrdemo -Recurse -Force
```

Nothing is ever written inside the repository.
