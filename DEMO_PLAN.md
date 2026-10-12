# Demo Plan — 30 Minutes, Business Audience

> **Superseded for the current platform.** This plan covers the original
> two-venue execution PoC and predates the minimum-requirements stat-arb
> layer added in 0.3.0. It also assumes a bash shell with `jq` installed.
> For a 30-minute client demo of the platform as it ships today, use
> [CLIENT_DEMO_30MIN.md](CLIENT_DEMO_30MIN.md). This document is kept
> because the execution-controls and opportunity beats below are still
> accurate and are not covered there.

**Goal:** the room should leave believing three things, in this order —
**it works**, **it is safe**, **it recovers**.

This is a *running order*, not a reference manual. Every command below was
executed and timed on the machine this plan was written on. For the full
feature reference see [DEMO_GUIDE.md](DEMO_GUIDE.md); for the detailed
client walkthrough see [CLIENT_DEMO_RUNBOOK.md](CLIENT_DEMO_RUNBOOK.md).

Audience assumption: decision-makers, not engineers. No jargon on screen
that you have not defined out loud. Raw JSON is never shown unfiltered —
every command below is piped through `jq` to produce a few readable lines.

For the three client-requested features — the live L2 order book, multi-leg
orders, and cancel/modify — see
[Annex A](#annex-a--client-requested-walkthrough-15-min-technical-audience)
at the end of this plan. That annex is a separate, engineer-facing session.

---

## Running order

| Time | Segment | Command | Runs in |
| --- | --- | --- | --- |
| 0:00 | Frame the session | — | — |
| 0:02 | Safety statement | `ems-inspect` | 0.6 s |
| 0:04 | Start the test suite in the background | `pytest` | 40-60 s |
| 0:05 | **Beat 1 — it works** | `opportunity-demo` | 2.4 s |
| 0:12 | **Beat 2 — it is safe** | `controls-demo` ×4 | ~1 s each |
| 0:21 | **Beat 3 — it recovers** | `opportunity-demo --store` | 1.5 s |
| 0:26 | Proof | reveal the suite result | — |
| 0:28 | Close and next steps | — | — |

Total command runtime is under 10 seconds. The remaining 29 minutes are
you talking. Nothing in this plan makes the room wait.

Annex A adds a further 15 minutes for a technical audience. It is optional
and needs Binance Testnet credentials for everything except the order book.

---

## T-10 — pre-flight

Run this before anyone joins. It is the same command list as the demo, so
if it passes, the demo passes.

```bash
cd ~/trading-platform-poc
uv run hft-platform-kit ems-inspect       > /tmp/pre1.txt
uv run hft-platform-kit opportunity-demo  > /tmp/pre2.txt
uv run hft-platform-kit controls-demo     > /tmp/pre3.txt
echo "pre-flight exit: $?"
```

Then set the terminal to a **large font**, clear the scrollback, and widen
the window to at least 100 columns — the tables below wrap badly under 100.

Keep `/tmp/pre*.txt` open in a second tab. If any live command misbehaves,
switch to the pre-flight output rather than debugging in front of the room.

---

## 0:00 — Frame the session (2 min, no screen)

Say this before touching the keyboard:

> "This is a proof of concept for a two-venue trading system. In thirty
> minutes I want to show you three things. First, that it does the job.
> Second, that it cannot do damage — there are hard limits it will not
> cross even if a strategy asks it to. Third, that when something goes
> wrong mid-trade, it ends flat rather than stuck. I will not show you
> profit numbers, because that is not what a proof of concept proves."

That last sentence buys you enormous credibility. Say it.

---

## 0:02 — Safety statement (2 min)

```bash
uv run hft-platform-kit ems-inspect | jq '{live_trading_enabled, venues}'
```

On screen:

```json
{
  "live_trading_enabled": false,
  "venues": {
    "binance": { "environment": "testnet",
                 "rest_base_url": "https://testnet.binance.vision" },
    "deribit": { "environment": "test", ... }
  }
}
```

> "Before anything else — nothing you see today can touch real money.
> Live trading is switched off, and every venue is pointed at a test
> endpoint. That is not a demo setting I flipped this morning; it is the
> default, and turning it on is a deliberate, separate act."

**The one line that matters:** `live_trading_enabled: false`.

---

## 0:04 — Start the suite in the background (30 sec)

```bash
(uv run pytest -q > /tmp/suite.txt 2>&1 &) ; echo "suite started"
```

> "I have just started the full automated test suite. It takes under a
> minute. We will come back to it at the end."

This is a deliberate piece of staging: the proof arrives while you talk,
so the room never watches a progress bar.

---

## Beat 1 — It works (0:05 → 0:12, 7 min)

### The whole story in three lines

```bash
uv run hft-platform-kit opportunity-demo --scenario all 2>/dev/null \
  | jq -s -c '.[-1].scenarios[] | {scenario, final: .state,
      residual: .residual_quantity, balanced: .reconciliation.balanced,
      no_dupes: .duplicate_free, replay_same: .replay_is_idempotent}'
```

On screen:

```
{"scenario":"filled", "final":"closed","residual":"0",  "balanced":true,"no_dupes":true,"replay_same":true}
{"scenario":"partial","final":"closed","residual":"0.0","balanced":true,"no_dupes":true,"replay_same":true}
{"scenario":"reject", "final":"closed","residual":"0",  "balanced":true,"no_dupes":true,"replay_same":true}
```

Walk the room across the row, not down the column:

> "Three trades. In the first, everything went right. In the second, one
> side only partly filled. In the third, one side was rejected outright.
> Three completely different days.
>
> Now look at the columns. All three ended **closed** — nothing is still
> hanging. All three ended with **residual zero** — we are not left
> holding anything. All three **balanced** — what we think we own matches
> what the exchange says we own. And **no duplicates** in any of them."

### What the two right-hand columns mean

Define these in plain language; they are the two that a risk-minded
person will pick up on.

| Column | Say this |
| --- | --- |
| `no_dupes` | "The system never sent the same order twice — not even when it retried after a timeout." |
| `replay_same` | "If we replay the recorded history of this trade, we land on exactly the same answer. The books are reproducible." |

**Do not** open the full JSON. If asked what else is in there, say "about
forty fields per trade — order IDs, fees, timestamps — all of it written
down," and move on.

---

## Beat 2 — It is safe (0:12 → 0:21, 9 min)

Four short commands. Each is one idea. Do not run all six scenarios.

### 2a. The kill switch (2 min)

```bash
uv run hft-platform-kit controls-demo --scenario kill-switch 2>/dev/null \
  | jq -s -r '.[-1].scenarios[0].strategy_clear_refused'
```

On screen:

```
spread-capture-strategy is not a risk administrator and cannot clear
the kill switch
```

> "Anyone can hit the kill switch. Only the risk desk can un-hit it. The
> strategy asked to clear it and was refused by name. A trader having a
> bad day cannot turn the safety off."

Add: while it is engaged, cancels and hedges still go out — **you can
always reduce risk, you can never add it.**

### 2b. Stale market data (2 min)

```bash
uv run hft-platform-kit controls-demo --scenario stale-feed 2>/dev/null \
  | jq -s -r '.[-1].scenarios[0].after_staleness[]
              | [.label, .action, .reason] | @tsv' \
  | column -t -s$'\t'
```

On screen:

```
new-order    reject  binance feed age 250ms
replace      reject  binance feed age 250ms
resting      cancel  binance feed age 250ms
cancel       admit   admitted
hedge        admit   admitted
other-venue  admit   admitted
```

> "The Binance price feed has gone quarter-of-a-second stale. Trading on
> a stale price is how firms lose money quickly. So: new orders refused,
> changes refused, and anything already resting on the exchange is pulled.
>
> But look at the bottom three. Cancelling is still allowed. Hedging is
> still allowed. And the *other* venue is untouched, because its feed is
> fine — we don't shut the whole firm because one feed hiccuped."

This is usually the segment that lands hardest. Give it room.

### 2c. All-or-nothing reservation (3 min)

This is the most important idea in the demo and the hardest to explain.
Set it up **before** running the command:

> "Here is the nightmare in a two-venue trade: you buy on one exchange,
> and then discover you cannot send the sell on the other. Now you are
> holding something you never wanted. How do we make that impossible?"

```bash
uv run hft-platform-kit controls-demo --scenario atomic-reservation \
  2>/dev/null | jq -s -c '.[-1].scenarios[0]
    | {failed_attempt, rollback_was_complete,
       before: .capacity_before_failed_attempt,
       after:  .capacity_after_failed_attempt}'
```

On screen:

```json
{"failed_attempt":"firm has 0 of 6 messages available",
 "rollback_was_complete":true,
 "before":{"binance":8,"firm":0,"okx":8},
 "after": {"binance":8,"firm":0,"okx":8}}
```

> "The answer is that we reserve everything up front — both legs *and*
> the hedge — before a single order leaves the building. Here the second
> trade could not get what it needed. So it took **nothing**. Compare the
> before and after line: identical. It did not grab one venue's capacity
> and then fail on the other. All of it, or none of it."

### 2d. Limits queue rather than drop (2 min)

```bash
uv run hft-platform-kit controls-demo --scenario venue-rate-limit \
  2>/dev/null | jq -s -c '.[-1].scenarios[0]
    | {admitted, queued, released_after_window}'
```

On screen:

```json
{"admitted":5,"queued":3,"released_after_window":["q5","q6","q7"]}
```

> "Exchanges cap how many messages you may send. We sent eight into a
> limit of five. Three were **queued, not thrown away**, and went out
> when the window rolled over. We never breach the exchange's limit, and
> we don't silently lose your orders to do it."

If you have time, add the firm-wide version: when the firm limit binds,
queued work is released **by priority** — market making first, research
last, and emergency hedging ahead of everything.

---

## Beat 3 — It recovers (0:21 → 0:26, 5 min)

### The trade that went wrong

```bash
uv run hft-platform-kit opportunity-demo --scenario partial 2>/dev/null \
  | jq -s -r '.[-1].scenarios[0].transitions[]' | tr '\n' ' '
```

On screen:

```
created risk_reserved orders_created dispatched acknowledged timeout
partially_filled hedge_required unwinding reconciling closed
```

> "Read that left to right — it is the life of one trade. We reserved the
> risk, sent both orders, and then: **timeout**. One venue went quiet. We
> were left partly filled, exposed. The system recognised it needed a
> hedge, unwound the leftover position, checked the books, and closed.
>
> Nobody pressed anything. That is the recovery path running by itself."

### The part that survives a crash

```bash
uv run hft-platform-kit opportunity-demo --scenario partial \
  --store /tmp/demo.db > /dev/null 2>&1
sqlite3 -header -column /tmp/demo-partial.db \
  "select leg_index, venue, side, target_quantity as target,
          filled_quantity as filled, state from legs"
```

> ⚠️ **The file is `/tmp/demo-partial.db`, not `/tmp/demo.db`.** The tool
> writes one database per scenario and appends the scenario name. Typing
> the wrong filename live is the single most likely way to fumble this
> segment.

On screen:

```
leg_index  venue    side  target  filled  state
---------  -------  ----  ------  ------  --------
0          binance  buy   1       0.4     canceled
1          okx      sell  1       1       filled
1001       okx      buy   0.6     0.6     filled
```

> "This is on disk, after the program has exited. Line one: we meant to
> buy one, got four-tenths, cancelled the rest. Line two: we sold a full
> one on the other venue. So we were six-tenths short. Line three — and
> this is the one to look at — the system bought that six-tenths back.
>
> If the machine had been unplugged between those lines, this file is
> what the system reads on restart. It knows what it sent, what came
> back, and what it still owes."

---

## 0:26 — Proof (2 min)

```bash
grep -E "[0-9]+ (passed|failed)" /tmp/suite.txt \
  || echo "still running - give it a few more seconds"
```

On screen:

```
747 passed, 1 skipped in 38.68s
```

> ⚠️ Do **not** use `tail -1` here. While the suite is still running the
> last line is a wall of progress dots, and you will put that on screen in
> front of the client. The `grep` above prints the result if it is ready
> and a plain sentence if it is not.

> "That is the automated test suite I started at the beginning — 747
> checks, every one green. A large share of them exist because we
> deliberately broke this system to see whether the tests noticed. When a
> test didn't notice, we treated the *test* as the defect and rewrote it."

That last sentence is worth more than the number. Non-technical audiences
have heard "we have tests" before; they have rarely heard "we checked
that our tests actually catch things."

---

## 0:28 — Close (2 min)

State the boundary honestly. It protects you later:

> "To be clear about what this is: a proof of concept, on test exchanges,
> with simulated fills. It is built for research, integration and paper
> trading. What we have proven is the *machinery* — the state handling,
> the safety limits, the recovery. What we have not proven is that any
> particular strategy makes money. Those are separate questions and this
> was the harder one to get right."

Then offer the next step (pick one, do not list all three):
- a technical deep-dive for their engineers,
- a run against their own instrument list,
- a live paper-trading session over a full day.

---

## If something goes wrong

| Symptom | Do this |
| --- | --- |
| A command hangs | `Ctrl-C`, switch to the `/tmp/pre*.txt` tab, keep talking. Never debug live. |
| `jq: command not found` | Drop the pipe and run the bare command; scroll to the summary at the bottom. |
| SQLite file "missing" | You typed `/tmp/demo.db`. It is `/tmp/demo-partial.db`. |
| Output is unreadably wide | Widen to 100+ columns, or fall back to the narrower `--scenario` runs. |
| Suite tab shows failures | Say so plainly and move on. Do not improvise a cause on the spot. |
| Asked something you don't know | "I don't know — I'll find out and come back to you." Never guess in a risk conversation. |

---

## Questions you will be asked

| Question | Honest answer |
| --- | --- |
| "Is this making money?" | "Unknown, and not what this demo tested. This tested whether the machinery is sound." |
| "Can it trade for real?" | "Not as configured. Live trading is off and the venues are test endpoints. Enabling it is a deliberate, separate decision." |
| "What if two things fail at once?" | "The controls have a fixed order of precedence — the firm's kill switch outranks everything, then firm limits, then venue limits. It is decided in advance, not argued at runtime." |
| "What happens if the process dies mid-trade?" | "That's the database in Beat 3. State is written before we act, so on restart it can tell what it sent and what is still owed." |
| "How do you know there are no duplicate orders?" | "Every order carries a fixed identity that does not change on retry. Retrying reuses it, so the exchange can reject the second copy. The demo checks it — the `no_dupes` column." |
| "Is it fast enough?" | "Not measured under production load. It's a proof of concept; latency work comes after the design is settled." |
| "Who can turn the safety off?" | "Only a designated risk administrator can clear the kill switch. You saw a strategy try and get refused by name." |

---

## Do not claim

Written down so it does not get said in the moment:

- ❌ "It's production ready." It is version `0.x` and labelled alpha.
- ❌ "It's profitable." No strategy performance has been measured.
- ❌ "It's low latency." No latency benchmark has been run.
- ❌ "It can't lose money." It cannot breach *these controls*. That is a
  different and much smaller claim — make the smaller one.

---

# Annex A — Client-requested walkthrough (15 min, technical audience)

Three asks arrived after the plan above was written:

1. *"L2 orderbook need to be displayed in CLI for 5 mins"*
2. *"test functionality for multi leg order"*
3. *"also cancel modify delete order functionality for order placement"*

They are engineer-facing, so they get their own running order rather than
being wedged into the business demo. Run this as a **separate session**, or
bolt it on after the close if the room asks for depth.

> **Terminology, stated once:** there is no separate *delete* verb. On an
> exchange, removing a resting order **is** a cancel — `cancel` is the verb,
> and it is what "delete" means here. Saying this up front avoids a hunt for
> a command that does not and should not exist.

## Running order

| Time | Segment | Command | Credentials |
| --- | --- | --- | --- |
| 0:00 | Live L2 order book | `book` | **not** required |
| 0:04 | Order lifecycle: place → modify → cancel | `submit`, `modify`, `cancel` | required |
| 0:10 | Multi-leg atomic admission | `multi-leg` | required |

## Pre-flight

The order book needs the public internet only. Everything after it needs
Binance Testnet keys — check before the room is watching, because the
failure is a hard exit:

```bash
uv run hft-platform-kit binance-testnet ping        # public, no keys
uv run hft-platform-kit binance-testnet account | head -5   # proves keys work
```

Without keys, every order command exits with:

```
error: Binance Testnet credentials are missing.
Set BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET.
```

This applies to `multi-leg --test-only` as well — "test only" means Binance
validates without creating an order, and that validation is still a
**signed** call.

---

## A1 — Live L2 order book (4 min)

The client asked for five minutes of continuous display, so five minutes is
the **default**; you do not pass a duration to get it.

```bash
uv run hft-platform-kit book --symbol BTCUSDT --depth 10 2>/dev/null
```

Logs go to stderr by design, so `2>/dev/null` leaves a clean, redrawing
table. For a short demo pass `--duration-seconds 90`. Real output:

```
BTCUSDT  level-2 order book
elapsed    8.0s   remaining    0.0s   events 78
--------------------------------------------------------------
         bid qty           bid | ask           ask qty
         3.35643     62,957.51 | 62,957.52     3.24706
          0.1467     62,957.50 | 62,957.53     0.12794
         0.00016     62,957.49 | 62,957.54     0.00016
--------------------------------------------------------------
mid 62,957.515   spread 0.01 (0.0016 bps)
levels 1,012 x 1,006   feed HEALTHY   book fresh
```

**What to point at, in order:**

- `levels 1,012 x 1,006` — a real maintained book, not the top of the feed.
- `feed HEALTHY` / `book fresh` — the same data-quality state machine the
  business demo used to block quoting on a stale feed. It is not decorative:
  let it run and it will say `STALE` if the feed genuinely stalls.
- `mid 62,957.515` — a **half-tick** mid, printed in full. Rounding it to
  `62,957.52` would silently invent half a tick of edge.
- `0.0016 bps` — four decimals, because a two-decimal spread field reads
  `0.00` on a liquid instrument and looks broken.

**The point worth making out loud** is the part nobody sees: the book is
built by connecting the websocket **first**, then fetching the REST
snapshot, then discarding buffered updates the snapshot already contains.
Doing it the intuitive way round — snapshot first — silently loses every
update that lands during the REST call and the book never recovers. On a
sequence gap this resynchronises rather than drifting.

Add `--json` for one machine-readable state object per refresh if someone
wants to pipe it into their own tooling.

**If the numbers do not move:** that is usually the market, not the demo.
Check against an independent source before claiming a bug —
`curl -s 'https://api.binance.com/api/v3/ticker/bookTicker?symbol=BTCUSDT'`
should agree with the top of the displayed book.

---

## A2 — Place, modify, cancel (6 min)

Every mutating command requires `--confirm TESTNET`. Omit it and nothing is
sent. That is deliberate: no order is ever one keystroke away.

**Place a resting order** — priced far from the market so it rests instead
of filling:

```bash
uv run hft-platform-kit binance-testnet submit \
  --symbol BTCUSDT --side buy --type limit \
  --quantity 0.002 --price 20000 \
  --daily-net-pnl 0 --confirm TESTNET
```

**Modify it** — this is the interesting one. It is *not* a cancel followed
by a new order:

```bash
uv run hft-platform-kit binance-testnet modify \
  --symbol BTCUSDT --cancel-client-order-id <ID-FROM-ABOVE> \
  --side buy --type limit --quantity 0.002 --price 21000 \
  --daily-net-pnl 0 --confirm TESTNET
```

Under the hood this is Binance's `cancelReplace` with
`cancelReplaceMode=STOP_ON_FAILURE`. Say why that matters: if the cancel
half fails and the replacement is placed anyway, you are left holding **two
live orders** where you intended one — the exact duplicate-exposure hazard
the rest of the system exists to prevent. `STOP_ON_FAILURE` means a failed
cancel stops the replacement.

Two further points a technical audience will probe:

- **A replace does not consume open-order capacity.** It withdraws one order
  as it adds one, so the limit check counts it as net zero.
- **Only resting orders can be modified.** A filled or already-cancelled
  order is refused with a reason rather than being silently re-sent.

**Cancel it** (this is "delete"):

```bash
uv run hft-platform-kit binance-testnet cancel \
  --symbol BTCUSDT --client-order-id <ID> --confirm TESTNET
```

If the network drops mid-cancel and the response is lost, the order is not
guessed at — it is marked `RECONCILIATION_REQUIRED` and resolved by asking
the exchange. An unacknowledged order has no known state, and the system
refuses to pretend otherwise.

---

## A3 — Multi-leg orders (5 min)

Two legs, dispatched concurrently, admitted atomically:

```bash
uv run hft-platform-kit binance-testnet multi-leg \
  --leg BTCUSDT:BUY:LIMIT:0.002:20000 \
  --leg ETHUSDT:SELL:LIMIT:0.05:5000 \
  --daily-net-pnl 0 --confirm TESTNET
```

Leg syntax is `SYMBOL:SIDE:TYPE:QUANTITY[:PRICE]`; `--test-only` runs every
leg through Binance validation without creating orders.

**The claim to demonstrate, not just assert:** *if any leg cannot be
admitted, nothing is transmitted at all.* Prove it by making the second leg
unaffordable and showing the balance failure names the **total** requirement
across both legs, not just the leg that tripped it. No order reaches the
venue.

That ordering is the whole design: validate every leg, reserve risk for the
aggregate, check the combined balance, and only then dispatch — concurrently,
via `asyncio.gather`, so one slow venue does not delay the other. A failure
on one leg is reported without cancelling the other's dispatch; you are told
what happened to each leg rather than losing the outcome of both.

**Scope, stated plainly:** this command dispatches two legs against a single
venue. The two-*venue* opportunity state machine — hedging, deterministic
unwind, restart recovery — is a separate component with its own demo
(`opportunity-demo`, Beat 3 above), and it requires two distinct venues by
construction.

---

## Annex A — do not claim

- ❌ "Multi-leg is atomic at the exchange." It is not, and cannot be. What
  is atomic is **admission**: all legs are approved before any is sent.
  Once both are in flight, either can still fill alone — which is precisely
  why the hedge and unwind logic in Beat 3 exists.
- ❌ "Modify is atomic." `cancelReplace` is a single exchange call, but the
  honest claim is narrower: it will not leave two live orders behind.
- ❌ "The order book is production-grade market data." It is one symbol,
  one venue, one process, with no failover.
