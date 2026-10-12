# Client Demo — 30 Minutes, Highlights Only

**Goal:** the room should leave believing four things, in this order —
**it is real**, **it is reproducible**, **it is safe**, **it is deliverable**.

This is a *running order*, not a reference manual. It shows ten features and
deliberately skips the other twenty-five commands. Every command below was
executed and timed on a Windows 11 / Python 3.12 laptop against package
version **0.3.0**, revision **3d517f7**, on 2026-09-01. Total command runtime
is about **75 seconds** spread across thirty minutes; the rest is you talking.

Scope note — pick the right document:

| If you want | Use |
| --- | --- |
| Thirty minutes, business audience, current platform | **this document** |
| Fifteen minutes on the stat-arb layer alone | [MR_DEMO_GUIDE.md](MR_DEMO_GUIDE.md) |
| Every feature, no time limit | [DEMO_GUIDE.md](DEMO_GUIDE.md) |
| Backtesting in depth | [BACKTESTING_DEMO_PLAN.md](BACKTESTING_DEMO_PLAN.md) |
| Handing the build to the client | [CLIENT_DELIVERY.md](CLIENT_DELIVERY.md) |

Commands are **PowerShell**. `jq` is *not* required — output is filtered with
built-in `ConvertFrom-Json`, so nothing needs installing.

---

## Running order

| Time | Beat | Command | Runs in |
| --- | --- | --- | --- |
| 0:00 | Frame the session | — | — |
| 0:03 | Scope and safety statement | — | — |
| 0:05 | **Beat 1 — it is real** | `mr-acceptance` | 21 s |
| 0:10 | **Beat 2 — it is reproducible** | `mr-backtest` ×2 | 5 s each |
| 0:15 | **Beat 3 — it is safe** | `mr-acceptance --only`, `mr-reconcile` | 4.5 s, 2 s |
| 0:20 | **Beat 4 — it is honest about cost** | `mr-tca`, `mr-verify` | 2 s each |
| 0:25 | **Beat 5 — it is deliverable** | `mr-deliverables` | 2 s |
| 0:28 | Close and next steps | — | — |

The ten features shown: independent acceptance, deterministic replay,
dataset integrity, transaction-cost analysis, risk limits, the kill switch,
partial-fill unwind, ledger reconciliation, a performance baseline, and the
governance deliverables.

---

## T-10 — pre-flight

Run this before anyone joins. It is the same code path as the demo, so if it
passes, the demo passes.

```powershell
cd C:\Users\ankusin\hft-platform-kit
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
$demo = "$env:TEMP\demo"
if (Test-Path $demo) { Remove-Item -Recurse -Force $demo }

uv run --frozen hft-platform-kit mr-acceptance --output "$demo\evidence" |
  Select-String '"total"|"passed"'
```

Expect `"passed": true` and `"total": 14`. If that holds, stop touching it.

Then: **large font**, clear the scrollback, window at least 100 columns wide.
Write the output to a file you can fall back on if a live command misbehaves —
never debug in front of the room:

```powershell
uv run --frozen hft-platform-kit mr-acceptance --output "$demo\backup" > "$demo\backup.txt"
```

Everything writes to `$env:TEMP\demo`, never into the repository, so you can
re-run the demo from a clean state at any time.

---

## 0:00 — Frame the session (3 min, no screen)

Say this before touching the keyboard:

> "This is a stat-arb backtesting and paper-trading platform built to a
> written specification — fourteen requirements, each with its own acceptance
> test. In thirty minutes I want to show you four things. First, that it does
> what the specification says, judged by tests I do not get to grade myself.
> Second, that the same input always gives the same output, to the byte.
> Third, that it refuses to do damage. Fourth, that you can take it away with
> you and re-run everything I am about to show you.
>
> I will not show you profit numbers, because a proof of concept does not
> prove profitability. I will show you costs, because those are real."

That last paragraph buys you credibility. Say it.

---

## 0:03 — Scope and safety statement (2 min)

State the two accepted limitations before anyone finds them:

- The venue connectors run against **synthetic and recorded data**, not a
  live exchange session. No order ever reaches a real venue.
- The performance baseline is measured on a **laptop**, not production
  hardware, so the thresholds are deliberately conservative.

Both are recorded in the evidence bundle as accepted limitations, alongside
**zero open defects**. Say that you will show them that page at the end.

---

## Beat 1 — It is real (0:05 → 0:10)

> "Fourteen requirements. Fourteen acceptance tests. I am going to run all of
> them now, live."

```powershell
uv run --frozen hft-platform-kit mr-acceptance --output "$demo\evidence"
```

Takes about **21 seconds**. Let the silence sit — a suite that finishes while
they watch is worth more than a screenshot.

Then show the scoreboard rather than the raw JSON:

```powershell
$rep = Get-Content "$demo\evidence\acceptance_report.json" -Raw | ConvertFrom-Json
$rep.checks | ForEach-Object {
  "{0}  {1,-4}  {2}" -f $_.test_id, $(if ($_.passed) { "PASS" } else { "FAIL" }), $_.title
}
```

On screen:

```
AT-01  PASS  Venue and dataset coverage
AT-02  PASS  Raw-to-canonical normalization
AT-03  PASS  Deterministic books
AT-04  PASS  Instrument mapping and sizing
AT-05  PASS  Pipeline restart and backfill
AT-06  PASS  Replay reproducibility
AT-07  PASS  Cost consistency
AT-08  PASS  Normal routed opportunity
AT-09  PASS  Partial fill, reject and unwind
AT-10  PASS  Timeout, idempotency and restart
AT-11  PASS  Rate limit, stale feed and kill switch
AT-12  PASS  Paper reconciliation
AT-13  PASS  Monitoring and TCA
AT-14  PASS  Performance baseline
```

**The point to make:** read out three or four titles. Each one is a line from
their specification, and each test wrote its own evidence to disk as it ran.

---

## Beat 2 — It is reproducible (0:10 → 0:15)

> "Every run is stamped with a hash of its result. Same inputs, same hash —
> and if anything changes, the hash changes."

Run the same backtest twice into two different directories:

```powershell
uv run --frozen hft-platform-kit mr-backtest --output "$demo\runA" --seed 7  | Out-Null
uv run --frozen hft-platform-kit mr-backtest --output "$demo\runB" --seed 7  | Out-Null
uv run --frozen hft-platform-kit mr-backtest --output "$demo\runC" --seed 99 | Out-Null
```

About **5 seconds** each. Now compare them:

```powershell
function Get-ResultHash($p) {
  (Get-Content "$p\run_manifest.json" -Raw | ConvertFrom-Json).result_hash
}
$a = Get-ResultHash "$demo\runA"
$b = Get-ResultHash "$demo\runB"
$c = Get-ResultHash "$demo\runC"

"run A (seed 7):  $a"
"run B (seed 7):  $b"
"run C (seed 99): $c"
"A equals B (same inputs)      : " + ($a -eq $b)
"A equals C (different inputs) : " + ($a -eq $c)
```

On screen:

```
run A (seed 7):  6c88d6726bd7e0f8cb567c03ca4a85b6d4cb1f7baace03f6325c3856336b09c2
run B (seed 7):  6c88d6726bd7e0f8cb567c03ca4a85b6d4cb1f7baace03f6325c3856336b09c2
run C (seed 99): f4858c1e2dc93b04f178773865f074393a67f344f56e4703b1903dc7cf21944a
A equals B (same inputs)      : True
A equals C (different inputs) : False
```

Then land the strongest version of the claim — the order-routing log is
identical *byte for byte*, not merely equivalent:

```powershell
"route log byte-identical: " + (
  (Get-FileHash "$demo\runA\route_log.json").Hash -eq
  (Get-FileHash "$demo\runB\route_log.json").Hash)
```

**The point to make:** "If you dispute a number I show you six months from
now, you re-run it with the same seed and get the same file. That is what
makes a result auditable rather than merely reported."

---

## Beat 3 — It is safe (0:15 → 0:20)

> "The interesting question is not whether it trades. It is what it does when
> things go wrong."

Two acceptance tests cover exactly that. Run just those two, live:

```powershell
uv run --frozen hft-platform-kit mr-acceptance --output "$demo\safety" --only AT-09 AT-11
```

Takes **4.5 seconds**. `AT-09` is *partial fill, reject and unwind*; `AT-11`
is *rate limit, stale feed and kill switch*.

> "AT-09 is the half-done trade — one leg fills, the other is rejected. The
> system unwinds rather than sitting on an exposed position. AT-11 is the
> stale feed: if prices stop arriving, it stops trading. Neither of those is
> a strategy decision. They are limits the strategy cannot talk its way past."

Then show that the books balance:

```powershell
$runId = (Get-Content "$demo\runA\run_manifest.json" -Raw | ConvertFrom-Json).run_id
$rec = uv run --frozen hft-platform-kit mr-reconcile `
         --ledger "$demo\runA\ledger.sqlite" --run-id $runId | ConvertFrom-Json
$rec | Select-Object run_id, events, state_hash, chain_digest
$rec.derived | Select-Object realized_pnl, total_fees
```

On screen:

```
run_id       : mr-backtest
events       : 472
state_hash   : c05201e40f9d01dca898513874982c93846c804baca2e56f9689a2d0ac705f80
chain_digest : 11cebdc1511c40d1d98a1173ac03eaa61d6e5ae63805cbd13e8c63da60b3f342

realized_pnl : 11.8731221444444444444444505
total_fees   : 14.39942230
```

**The point to make:** the ledger is append-only, and reconciliation replays
all 472 events in it to prove the ending position is the one the trades
imply. The `chain_digest` is a hash chain over those events — if anyone
edited or removed one after the fact, the digest would not match.

> ⚠️ **Careful with `--only`.** It takes a space-separated list —
> `--only AT-09 AT-11`. If you write it comma-separated,
> `--only "AT-09,AT-11"` matches nothing, runs **zero** tests and still prints
> `"passed": true` with `"total": 0`. Always glance at `total` before you
> claim anything passed.

---

## Beat 4 — It is honest about cost (0:20 → 0:25)

> "Most backtests flatter themselves by forgetting what trading costs. This
> one itemises it."

```powershell
$tca = Get-Content "$demo\runA\tca.json" -Raw | ConvertFrom-Json
$tca.totals | Select-Object gross_edge, fees, net_pnl, slippage_cost, fills, rejects
```

On screen:

```
gross_edge    : 43.8790
fees          : 14.39942230
net_pnl       : 29.47957770
slippage_cost : 28.74150557902313371876215953
fills         : 48
rejects       : 0
```

Do the subtraction out loud — `43.8790 − 14.39942230 = 29.47957770`. It ties
exactly, and it is worth saying so:

> "A third of the gross edge went to fees. I am showing you that number
> rather than hiding it, and the arithmetic ties to the cent. There is also a
> per-venue and per-instrument breakdown behind this."

Then prove the data underneath was not tampered with:

```powershell
uv run --frozen hft-platform-kit mr-verify --dataset "$demo\runA\dataset"
```

On screen:

```json
{
  "approved": true,
  "checksum": "e0005ae318c9940e4feb429f270b176fead33e07c5552f1b826c0a0832d2f1b9",
  "recomputed_checksum": "e0005ae318c9940e4feb429f270b176fead33e07c5552f1b826c0a0832d2f1b9",
  "mismatches": [],
  "verified": true
}
```

**The point to make:** the checksum recorded when the data was captured is
recomputed from the bytes on disk right now. They match, so the result was
produced from the data it claims to have used.

---

## Beat 5 — It is deliverable (0:25 → 0:28)

> "Everything I have shown you is yours to re-run."

```powershell
uv run --frozen hft-platform-kit mr-deliverables --output "$demo\deliverables"
Get-ChildItem "$demo\deliverables" -Recurse -File | ForEach-Object { $_.Name }
```

Eight governance documents, generated from the running system rather than
maintained by hand — so they cannot drift from it:

```
canonical_schemas.json      order_state_machine.json
feature_catalogue.json      risk_limit_matrix.json
fee_schedule.yaml           security_master.yaml
monitoring_and_alerts.json  venue_capability_matrix.json
```

Now close the loop on the evidence bundle from Beat 1:

```powershell
$rep.build | Select-Object package_version, git_revision, python_version, platform
"limitations: $($rep.limitations.Count)   defects: $($rep.defects.Count)"
$f = Get-ChildItem -Recurse -File "$demo\evidence"
"bundle: {0} files, {1:N1} MB" -f $f.Count, (($f | Measure-Object Length -Sum).Sum / 1MB)
```

On screen:

```
package_version : 0.3.0
git_revision    : 3d517f722ac4c82cc147ee569796b672b808dbba
python_version  : 3.12.14
platform        : Windows-11-10.0.26200-SP0
limitations: 2   defects: 0
bundle: 177 files, 29.3 MB
```

**The point to make:** the evidence names the exact commit it was produced
from. It is not a report about the software; it is a report *by* the
software, tied to one revision. Two accepted limitations, zero open defects.

---

## 0:28 — Close (2 min, no screen)

> "Fourteen requirements, fourteen passing tests, run in front of you. Results
> that reproduce to the byte. Limits that hold when a trade goes wrong. And a
> 29-megabyte evidence bundle stamped with the commit that produced it, which
> you can re-run on your own hardware."

Then ask for the next step explicitly. Two good ones:

- **Their data.** Point the pipeline at a venue and date range they care
  about and re-run the same acceptance suite against it.
- **Their thresholds.** The risk matrix is configuration; set it to their
  real limits and show the kill switch firing on their numbers.

---

## If something goes wrong

| Symptom | Do this |
| --- | --- |
| `uv` not found | `$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"` |
| A command hangs past 60 s | Ctrl-C, show `$demo\backup.txt` from pre-flight, move on |
| `"total": 0` but `"passed": true` | You comma-separated `--only`. Re-run space-separated |
| Any single AT fails | Do not debug live. Say "that one needs investigating, here is the evidence file it wrote" and open it |
| Hashes differ across runs | Check the `--seed` values match. Different seed, different hash, by design |

Never debug in front of the room. Every beat has a saved fallback from
pre-flight; use it and keep the narrative moving.

---

## Appendix — the five-minute cut

If the meeting collapses to five minutes, run exactly two commands:

```powershell
uv run --frozen hft-platform-kit mr-acceptance --output "$demo\evidence"

uv run --frozen hft-platform-kit mr-backtest --output "$demo\runA" --seed 7 | Out-Null
uv run --frozen hft-platform-kit mr-backtest --output "$demo\runB" --seed 7 | Out-Null
(Get-FileHash "$demo\runA\route_log.json").Hash -eq (Get-FileHash "$demo\runB\route_log.json").Hash
```

Fourteen tests passing, and the same run twice producing byte-identical
output. Those two facts carry the entire argument.
